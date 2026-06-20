#!/usr/bin/env python
"""S1 re-verification: virtual-chunk checksum (timestamp/etag) enforcement.

Closes the S1 finding after the icechunk_fixes merge (OSGeo/gdal commit 00021bdac9,
"check chunk timestamp when available, and add ignore-timestamp-etag=yes"). Before the
fix the driver parsed `checksum_etag`/`checksum_last_modified` only under #ifdef DEBUG
and never enforced them -> it would serve bytes the official icechunk client rejects as
stale. We verify, against the icechunk+zarr oracle, what the release build now does.

The check stats the (morphed) backing object and compares its Last-Modified against the
recorded `checksum_last_modified`. So we drive an S3-mock that returns a CONTROLLABLE
Last-Modified header, and author refs whose recorded checksum either matches or not.

Cases (each its own array; GDAL vs icechunk+zarr oracle):
  ts_match        recorded last_modified == object's  -> expect READ_OK (both)
  ts_stale        recorded last_modified != object's  -> expect LOUD_ERROR (both)
  ts_stale_ignore ts_stale + ?ignore-timestamp-etag=yes -> expect READ_OK (GDAL bypass)
  etag_stale      recorded etag != object's           -> oracle errors; GDAL? (etag is
                    parsed but NOT compared by the driver -> probes a residual gap)

Run:  source scripts/env.sh && $ICPY scripts/test_checksum_s1.py
"""
import datetime as dt
import email.utils
import http.server
import os
import shutil
import socketserver
import sys
import threading

import numpy as np

os.environ.setdefault("RUST_LOG", "off")
import icechunk as ic  # noqa: E402
import zarr  # noqa: E402
from osgeo import gdal  # noqa: E402

FIX = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures")
BUCKET = "s1bucket"

N = 8
TRUTH = np.arange(1, N + 1, dtype="<i4")
SRC_BYTES = TRUTH.tobytes()          # 32 bytes, raw LE, all non-zero
LEN = len(SRC_BYTES)

# The object's real Last-Modified the mock advertises (fixed, second granularity).
OBJ_MTIME = 1700000000               # 2023-11-14T22:13:20Z
OBJ_ETAG = '"realetag-v1"'
OBJ_LASTMOD_HDR = email.utils.formatdate(OBJ_MTIME, usegmt=True)

RESULTS = []


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _range(self):
        h = self.headers.get("Range")
        if not h or not h.startswith("bytes="):
            return 0, LEN - 1
        a, _, b = h[len("bytes="):].partition("-")
        return (int(a) if a else 0), min(int(b) if b else LEN - 1, LEN - 1)

    def _common_headers(self):
        self.send_header("ETag", OBJ_ETAG)
        self.send_header("Last-Modified", OBJ_LASTMOD_HDR)
        self.send_header("Accept-Ranges", "bytes")

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", str(LEN))
        self._common_headers()
        self.end_headers()

    def do_GET(self):
        start, end = self._range()
        data = SRC_BYTES[start:end + 1]
        self.send_response(206)
        self.send_header("Content-Range", f"bytes {start}-{end}/{LEN}")
        self.send_header("Content-Length", str(len(data)))
        self._common_headers()
        self.end_headers()
        self.wfile.write(data)


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def start_server():
    srv = Server(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


CASES = {
    # name              checksum passed to set_virtual_ref
    "ts_match":        dt.datetime.fromtimestamp(OBJ_MTIME, dt.timezone.utc),
    "ts_stale":        dt.datetime.fromtimestamp(OBJ_MTIME - 100000, dt.timezone.utc),
    "ts_stale_ignore": dt.datetime.fromtimestamp(OBJ_MTIME - 100000, dt.timezone.utc),
    "etag_stale":      "wrong-etag-v0",
}


def build_repo(port):
    base = os.path.join(FIX, "_s1")
    shutil.rmtree(base, ignore_errors=True)
    os.makedirs(base)
    rp = os.path.join(base, "vstore")
    prefix = f"s3://{BUCKET}/"
    cfg = ic.RepositoryConfig.default()
    cfg.set_virtual_chunk_container(ic.VirtualChunkContainer(
        url_prefix=prefix,
        store=ic.s3_store(endpoint_url=f"http://127.0.0.1:{port}", region="us-east-1",
                          anonymous=True, allow_http=True, force_path_style=True)))
    repo = ic.Repository.create(ic.local_filesystem_storage(rp), config=cfg,
                                authorize_virtual_chunk_access={prefix: None})
    s = repo.writable_session("main")
    g = zarr.group(s.store)
    for name, checksum in CASES.items():
        g.create_array(name, shape=(N,), dtype="int32", chunks=(N,),
                       compressors=None, dimension_names=["x"])
        s.store.set_virtual_ref(f"{name}/c/0", f"{prefix}{name}/source.bin",
                                offset=0, length=LEN, checksum=checksum)
    s.commit("s1 checksum matrix")
    return rp, prefix


def with_timeout(fn, secs=10):
    box = {}

    def run():
        try:
            box["r"] = ("READ_OK", fn())
        except Exception as e:  # noqa
            box["r"] = ("RAISED", str(e).splitlines()[-1][-80:])
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(secs)
    if t.is_alive():
        return "HUNG", f"no response in {secs}s"
    return box["r"]


def classify(status, val):
    if status == "RAISED":
        return "LOUD_ERROR"
    if status == "HUNG":
        return "HUNG"
    if np.array_equal(val, TRUTH):
        return "OK_CORRECT"
    if np.all(val == 0):
        return "SILENT_FILL"
    return "SILENT_PARTIAL/CORRUPT"


def read_gdal(rp, port, var, ignore=False):
    def do():
        gdal.UseExceptions()
        gdal.PushErrorHandler("CPLQuietErrorHandler")
        try:
            for k, v in [("AWS_S3_ENDPOINT", f"127.0.0.1:{port}"), ("AWS_VIRTUAL_HOSTING", "FALSE"),
                         ("AWS_HTTPS", "NO"), ("AWS_NO_SIGN_REQUEST", "YES"),
                         ("AWS_DEFAULT_REGION", "us-east-1"),
                         ("GDAL_HTTP_MAX_RETRY", "0"), ("GDAL_HTTP_TIMEOUT", "5"),
                         ("CPL_VSIL_CURL_NON_CACHED", "/vsis3/")]:
                gdal.SetConfigOption(k, v)
            conn = "ICECHUNK:" + rp + ("?ignore-timestamp-etag=yes" if ignore else "")
            ds = gdal.OpenEx(conn, gdal.OF_MULTIDIM_RASTER)
            return np.asarray(ds.GetRootGroup().OpenMDArray(var).ReadAsArray())
        finally:
            gdal.PopErrorHandler()
            gdal.VSICurlClearCache()
    return with_timeout(do)


def read_ref(rp, prefix, var):
    def do():
        repo = ic.Repository.open(ic.local_filesystem_storage(rp),
                                  authorize_virtual_chunk_access={prefix: None})
        g = zarr.open_group(repo.readonly_session("main").store, mode="r")
        return np.asarray(g[var][:])
    return with_timeout(do)


def record(name, expected, gc, rc, ok, note=""):
    RESULTS.append((name, ok))
    flag = "PASS" if ok else "**DEFECT**"
    print(f"  [{flag}] {name:16} GDAL={gc:24} oracle={rc:24} {note}")


def main():
    srv, port = start_server()
    rp, prefix = build_repo(port)
    print(f"object Last-Modified = {OBJ_LASTMOD_HDR} (epoch {OBJ_MTIME}); truth={list(TRUTH)}\n")
    print(f"  {'case':16} {'GDAL':29} {'oracle (ic+zarr)':24} note")
    print("  " + "-" * 92)

    # ts_match: both should read correctly
    gc = classify(*read_gdal(rp, port, "ts_match"))
    rc = classify(*read_ref(rp, prefix, "ts_match"))
    record("ts_match", "READ_OK", gc, rc, ok=(gc == "OK_CORRECT"),
           note="recorded ts == object ts -> read")

    # ts_stale: recorded ts != object ts -> driver must error (was: served silently).
    # NB: the icechunk Python client validates a virtual checksum at ref-CREATION time
    # (against the real object metadata), not on every read of a manually-injected ref,
    # so the synthetic oracle reads OK here and is NOT a control for this case -- the
    # GDAL LOUD_ERROR is the thing under test.
    gc = classify(*read_gdal(rp, port, "ts_stale"))
    rc = classify(*read_ref(rp, prefix, "ts_stale"))
    record("ts_stale", "LOUD_ERROR", gc, rc, ok=(gc == "LOUD_ERROR"),
           note="GDAL must error; oracle not a control (see note)")

    # ts_stale_ignore: opt-out bypasses the check
    gc = classify(*read_gdal(rp, port, "ts_stale_ignore", ignore=True))
    record("ts_stale+ignore", "READ_OK", gc, "(n/a)", ok=(gc == "OK_CORRECT"),
           note="?ignore-timestamp-etag=yes bypasses check")

    # etag_stale: recorded etag != object etag. The driver parses checksum_etag but never
    # COMPARES it (only checksum_last_modified is enforced) -> GDAL serves stale-etag data
    # without error. This is a residual gap (a virtual store that records an etag instead
    # of a timestamp is unprotected), not a regression.
    gs, gv = read_gdal(rp, port, "etag_stale")
    gc = classify(gs, gv)
    rc = classify(*read_ref(rp, prefix, "etag_stale"))
    etag_unenforced = (gc == "OK_CORRECT")
    record("etag_stale", "GDAL etag unenforced (residual gap)", gc, rc, ok=True,
           note="RESIDUAL GAP: etag parsed but not compared" if etag_unenforced
                else "etag now enforced")

    print("  " + "-" * 92)
    defects = [n for n, ok in RESULTS if not ok]
    print(f"\n  {len(defects)} defect(s): {defects or 'none'}")
    if etag_unenforced:
        print("  NOTE: only checksum_last_modified is enforced; checksum_etag is parsed but")
        print("        not compared -> etag-only virtual stores remain unprotected.")
    srv.shutdown()
    return 1 if defects else 0


if __name__ == "__main__":
    sys.exit(main())
