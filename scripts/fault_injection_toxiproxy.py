#!/usr/bin/env python
"""Transport-layer fault injection via REAL toxiproxy (the blog's setup).

Earthmover put toxiproxy in front of a rustfs object store and used LimitData /
SlowClose / latency to "poison the connection at will" and verify retry behaviour.
We mirror that for the GDAL Icechunk driver: a clean S3-compatible mock is the
upstream, **toxiproxy** sits in front, and we inject transport toxics on the byte
stream of a virtual-chunk fetch. Both GDAL and icechunk+zarr (reference) read
through the toxiproxy listener.

Prereq: `toxiproxy-server` running (brew install toxiproxy; `toxiproxy-server &`).
Complements `fault_injection.py` (HTTP-semantic faults). Acceptable outcomes for a
manifest-referenced chunk: OK_CORRECT or LOUD_ERROR; silent fill/partial = DEFECT.

Run:  source scripts/env.sh && toxiproxy-server & ; $ICPY scripts/fault_injection_toxiproxy.py
"""
import http.server
import json
import os
import shutil
import socketserver
import sys
import threading
import urllib.request

import numpy as np

os.environ.setdefault("RUST_LOG", "off")
import icechunk as ic  # noqa: E402
import zarr  # noqa: E402
from osgeo import gdal  # noqa: E402

FIX = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures")
BUCKET = "faultbucket"
TOXI_API = "http://127.0.0.1:8474"

# 256 KB chunk so byte-counting toxics (LimitData / bandwidth) actually bite mid-stream.
N = 65536
TRUTH = np.arange(1, N + 1, dtype="<i4")
SRC = TRUTH.tobytes()
LEN = len(SRC)                                   # 262144

# toxic name -> (type, attributes, note). stream=downstream (upstream->client = the data).
TOXICS = [
    ("none",       None, None,                          "control (no toxic)"),
    ("latency",    "latency",    {"latency": 300, "jitter": 50}, "300ms +/-50 latency"),
    ("bandwidth",  "bandwidth",  {"rate": 512},          "throttle 512 KB/s"),
    ("slow_close", "slow_close", {"delay": 500},         "delay TCP close 500ms (SlowClose)"),
    ("limit_data", "limit_data", {"bytes": 100000},      "cut connection after 100 KB (LimitData)"),
    ("reset_peer", "reset_peer", {"timeout": 0},         "RST the connection immediately"),
    ("timeout",    "timeout",    {"timeout": 1500},      "hold 1.5s w/o data, then drop"),
]


# ---- clean S3-compatible mock (always serves correctly) --------------------
class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _range(self):
        h = self.headers.get("Range")
        if not h or not h.startswith("bytes="):
            return 0, LEN - 1
        a, _, b = h[len("bytes="):].partition("-")
        return (int(a) if a else 0), (min(int(b), LEN - 1) if b else LEN - 1)

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", str(LEN))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", '"deadbeef"')
        self.end_headers()

    def do_GET(self):
        s, e = self._range()
        data = SRC[s:e + 1]
        self.send_response(206)
        self.send_header("Content-Range", f"bytes {s}-{e}/{LEN}")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("ETag", '"deadbeef"')
        self.end_headers()
        try:
            self.wfile.write(data)
        except Exception:
            pass


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def start_upstream():
    srv = Server(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


# ---- toxiproxy REST helpers ------------------------------------------------
def api(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(TOXI_API + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, (json.loads(r.read() or b"null"))
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:120]


def proxy_setup(upstream_port):
    api("DELETE", "/proxies/s3fault")            # clean slate
    st, resp = api("POST", "/proxies", {
        "name": "s3fault", "listen": "127.0.0.1:0",
        "upstream": f"127.0.0.1:{upstream_port}", "enabled": True})
    if st not in (200, 201):
        raise RuntimeError(f"toxiproxy proxy create failed: {st} {resp}")
    listen = resp["listen"]                       # "127.0.0.1:PORT"
    return int(listen.rsplit(":", 1)[1])


def set_toxic(ttype, attrs):
    api("DELETE", "/proxies/s3fault/toxics/t")
    if ttype is None:
        return
    api("POST", "/proxies/s3fault/toxics", {
        "name": "t", "type": ttype, "stream": "downstream",
        "toxicity": 1.0, "attributes": attrs})


# ---- repo + readers --------------------------------------------------------
def build_repo(listen_port):
    base = os.path.join(FIX, "_fault_toxi")
    shutil.rmtree(base, ignore_errors=True)
    os.makedirs(base)
    rp = os.path.join(base, "vstore")
    prefix = f"s3://{BUCKET}/"
    cfg = ic.RepositoryConfig.default()
    cfg.set_virtual_chunk_container(ic.VirtualChunkContainer(
        url_prefix=prefix,
        store=ic.s3_store(endpoint_url=f"http://127.0.0.1:{listen_port}", region="us-east-1",
                          anonymous=True, allow_http=True, force_path_style=True)))
    repo = ic.Repository.create(ic.local_filesystem_storage(rp), config=cfg,
                                authorize_virtual_chunk_access={prefix: None})
    s = repo.writable_session("main")
    g = zarr.group(s.store)
    g.create_array("v", shape=(N,), dtype="int32", chunks=(N,), compressors=None,
                   dimension_names=["x"])
    s.store.set_virtual_ref("v/c/0", f"{prefix}source.bin", offset=0, length=LEN)
    s.commit("toxi fixture")
    return rp, prefix


def with_timeout(fn, secs=12):
    box = {}

    def run():
        try:
            box["r"] = ("READ_OK", fn())
        except Exception as e:  # noqa
            box["r"] = ("RAISED", str(e).splitlines()[-1][-66:])
    t = threading.Thread(target=run, daemon=True)
    t.start(); t.join(secs)
    return ("HUNG", f">{secs}s") if t.is_alive() else box["r"]


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


def read_gdal(rp, listen_port):
    def do():
        gdal.UseExceptions()
        gdal.PushErrorHandler("CPLQuietErrorHandler")
        try:
            for k, v in [("AWS_S3_ENDPOINT", f"127.0.0.1:{listen_port}"), ("AWS_VIRTUAL_HOSTING", "FALSE"),
                         ("AWS_HTTPS", "NO"), ("AWS_NO_SIGN_REQUEST", "YES"), ("AWS_DEFAULT_REGION", "us-east-1"),
                         ("GDAL_HTTP_MAX_RETRY", "0"), ("GDAL_HTTP_TIMEOUT", "5"),
                         ("CPL_VSIL_CURL_NON_CACHED", "/vsis3/")]:
                gdal.SetConfigOption(k, v)
            ds = gdal.OpenEx("ICECHUNK:" + rp, gdal.OF_MULTIDIM_RASTER)
            return np.asarray(ds.GetRootGroup().OpenMDArray("v").ReadAsArray())
        finally:
            gdal.PopErrorHandler()
            gdal.VSICurlClearCache()
    return with_timeout(do)


def read_ref(rp, prefix):
    def do():
        repo = ic.Repository.open(ic.local_filesystem_storage(rp),
                                  authorize_virtual_chunk_access={prefix: None})
        g = zarr.open_group(repo.readonly_session("main").store, mode="r")
        return np.asarray(g["v"][:])
    return with_timeout(do)


def main():
    st, _ = api("GET", "/version")
    if st != 200:
        print("ERROR: toxiproxy-server not reachable at", TOXI_API,
              "\n  start it with:  toxiproxy-server &")
        return 2
    srv, up = start_upstream()
    listen = proxy_setup(up)
    rp, prefix = build_repo(listen)
    print(f"upstream S3-mock :{up}  ->  toxiproxy :{listen}  (chunk = {LEN//1024} KB)\n")
    print(f"  {'toxic':11} {'GDAL':22} {'reference':22} verdict   ({'note'})")
    print("  " + "-" * 86)
    defects = []
    for name, ttype, attrs, note in TOXICS:
        set_toxic(ttype, attrs)
        gs, gv = read_gdal(rp, listen)
        rs, rv = read_ref(rp, prefix)
        gc, rc = classify(gs, gv), classify(rs, rv)
        bad = gc in ("SILENT_FILL", "SILENT_PARTIAL/CORRUPT")
        verdict = "*** DEFECT ***" if bad else "ok"
        if bad:
            defects.append(name)
        print(f"  {name:11} {gc:22} {rc:22} {verdict:14} ({note})")
    api("DELETE", "/proxies/s3fault")
    srv.shutdown()
    print("  " + "-" * 86)
    print(f"  {len(defects)} silent-data defect(s): {defects or 'none'}")
    print("  (acceptable for a referenced chunk = OK_CORRECT or LOUD_ERROR; slow toxics may HANG vs timeout)")
    return 1 if defects else 0


if __name__ == "__main__":
    sys.exit(main())
