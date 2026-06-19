#!/usr/bin/env python
"""Application-layer fault injection for the GDAL Icechunk driver's read path.

Inspired by Earthmover's icechunk rigor work (toxiproxy + rustfs fault injection to
verify retry behaviour). This script covers the faults toxiproxy CANNOT express --
HTTP-semantic faults (5xx, short Content-Length, corrupt bytes) -- by serving virtual
chunks from a tiny S3-compatible mock that misbehaves per object key. The companion
`fault_injection_toxiproxy.py` covers transport faults (latency / LimitData / SlowClose)
through real toxiproxy. Together they extend finding B3 (clean 404 -> silent zeros)
into the full failure-mode matrix.

Wiring: local Icechunk repo, one array per fault; each array's single virtual chunk
points at s3://faultbucket/<mode>/source.bin; the container's store has
endpoint_url=http://127.0.0.1:<port> (path-style, anonymous, http). Both GDAL multidim
and icechunk+zarr (the reference oracle) read through the mock.

For a manifest-referenced (virtual) chunk the ONLY acceptable outcomes are READ_OK with
correct bytes, or a LOUD ERROR. A silent fill (zeros) or silent partial/corrupt read is
a DEFECT (B3-class).

Run:  source scripts/env.sh && $ICPY scripts/fault_injection.py
"""
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
BUCKET = "faultbucket"

N = 16
TRUTH = np.arange(1, N + 1, dtype="<i4")
SRC_BYTES = TRUTH.tobytes()          # 64 bytes, raw LE (array codec = none); all non-zero
LEN = len(SRC_BYTES)

FAULTS = {
    "ok":       "serve correctly (control)",
    "http404":  "404 on the object GET (the B3 case: not-found)",
    "http500":  "500 on the object GET",
    "http503":  "503 on the object GET",
    "http429":  "429 on the object GET",
    "truncate": "206, Content-Length=full, but send half the bytes then close",
    "shortlen": "206, Content-Length=half, send half (claims a short object range)",
    "corrupt":  "206, correct length, all 0xFF (no checksum -> ?)",
}


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _mode(self):
        path = self.path.split("?", 1)[0].strip("/")
        parts = path.split("/")
        return parts[1] if len(parts) >= 2 and parts[1] in FAULTS else "ok"

    def _range(self):
        h = self.headers.get("Range")
        if not h or not h.startswith("bytes="):
            return 0, LEN - 1
        a, _, b = h[len("bytes="):].partition("-")
        start = int(a) if a else 0
        end = int(b) if b else LEN - 1
        return start, min(end, LEN - 1)          # clamp to real object size

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", str(LEN))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", '"deadbeef"')
        self.end_headers()

    def do_GET(self):
        mode = self._mode()
        start, end = self._range()
        data = SRC_BYTES[start:end + 1]
        nfull = len(data)

        if mode in ("http404", "http500", "http503", "http429"):
            self.send_response(int(mode[4:]))
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if mode == "corrupt":
            data = b"\xff" * nfull

        if mode == "shortlen":
            half = nfull // 2
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{start+half-1}/{LEN}")
            self.send_header("Content-Length", str(half))
            self.send_header("ETag", '"deadbeef"')
            self.end_headers()
            self.wfile.write(data[:half])
            return

        if mode == "truncate":
            half = nfull // 2
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{LEN}")
            self.send_header("Content-Length", str(nfull))   # claims full, delivers half
            self.send_header("ETag", '"deadbeef"')
            self.end_headers()
            try:
                self.wfile.write(data[:half]); self.wfile.flush()
            except Exception:
                pass
            self.close_connection = True
            try:
                self.connection.close()
            except Exception:
                pass
            return

        # ok / corrupt: well-formed 206
        self.send_response(206)
        self.send_header("Content-Range", f"bytes {start}-{end}/{LEN}")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("ETag", '"deadbeef"')
        self.end_headers()
        self.wfile.write(data)


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def start_server():
    srv = Server(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


def build_repo(port):
    base = os.path.join(FIX, "_fault")
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
    for fault in FAULTS:
        g.create_array(fault, shape=(N,), dtype="int32", chunks=(N,),
                       compressors=None, dimension_names=["x"])
        s.store.set_virtual_ref(f"{fault}/c/0", f"{prefix}{fault}/source.bin",
                                offset=0, length=LEN)
    s.commit("fault matrix")
    return rp, prefix, port


def with_timeout(fn, secs=8):
    box = {}

    def run():
        try:
            box["r"] = ("READ_OK", fn())
        except Exception as e:  # noqa
            box["r"] = ("RAISED", str(e).splitlines()[-1][-72:])
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


def read_gdal(rp, port, var):
    def do():
        gdal.UseExceptions()
        gdal.PushErrorHandler("CPLQuietErrorHandler")
        try:
            for k, v in [("AWS_S3_ENDPOINT", f"127.0.0.1:{port}"), ("AWS_VIRTUAL_HOSTING", "FALSE"),
                         ("AWS_HTTPS", "NO"), ("AWS_NO_SIGN_REQUEST", "YES"),
                         ("AWS_DEFAULT_REGION", "us-east-1"),
                         ("GDAL_HTTP_MAX_RETRY", "0"), ("GDAL_HTTP_TIMEOUT", "4"),
                         ("CPL_VSIL_CURL_NON_CACHED", "/vsis3/")]:
                gdal.SetConfigOption(k, v)
            ds = gdal.OpenEx("ICECHUNK:" + rp, gdal.OF_MULTIDIM_RASTER)
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


def main():
    srv, port = start_server()
    rp, prefix, port = build_repo(port)
    print(f"backing source = {list(TRUTH)} (64 B, 1 chunk) via S3-mock on :{port}\n")
    print(f"  {'fault':9} {'GDAL':25} {'reference (ic+zarr)':25} verdict")
    print("  " + "-" * 80)
    defects = []
    for fault in FAULTS:
        gs, gv = read_gdal(rp, port, fault)
        rs, rv = read_ref(rp, prefix, fault)
        gc, rc = classify(gs, gv), classify(rs, rv)
        bad = gc in ("SILENT_FILL", "SILENT_PARTIAL/CORRUPT")
        if bad and fault == "corrupt":
            verdict = "no-checksum gap (see S1; shared)" if rc == gc else "no-checksum gap (see S1)"
        elif bad:
            verdict = "*** DEFECT (silent) ***"; defects.append(fault)
        else:
            verdict = "ok"
        print(f"  {fault:9} {gc:25} {rc:25} {verdict}")
        if gs == "READ_OK" and gc != "OK_CORRECT":
            print(f"            GDAL returned: {gv}")
    print("  " + "-" * 80)
    print(f"  {len(defects)} silent-data defect(s): {defects or 'none'}")
    print("  (acceptable for a referenced chunk = OK_CORRECT or LOUD_ERROR)")
    srv.shutdown()
    return 1 if defects else 0


if __name__ == "__main__":
    sys.exit(main())
