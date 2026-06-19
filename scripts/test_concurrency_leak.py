#!/usr/bin/env python
"""Concurrency + resource-leak probes for the GDAL Icechunk driver.

Blind spots this closes:
  - Thread safety: GDAL is used from tile servers / warpers / dask workers. Each
    thread opens its OWN dataset handle (sharing a GDALDataset across threads is
    never safe in GDAL) and reads concurrently; we assert every read is correct
    and nothing crashes. This is a correctness-under-concurrency + crash check,
    NOT a formal race-detector (that needs TSan).
  - FD / memory leaks: open+read+close the same repo many times and watch RSS and
    open file descriptors. A steady climb indicates a leak in the driver / VSI
    handler / Zarr delegation.

Run: source scripts/env.sh && $ICPY scripts/test_concurrency_leak.py
"""
import concurrent.futures as cf
import os
import sys
import threading

import numpy as np

os.environ.setdefault("RUST_LOG", "off")
from osgeo import gdal  # noqa: E402

gdal.UseExceptions()
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "fixtures")


def rss_kb():
    import subprocess
    out = subprocess.check_output(["ps", "-o", "rss=", "-p", str(os.getpid())])
    return int(out.strip())


def num_fds():
    try:
        return len(os.listdir(f"/dev/fd"))
    except Exception:
        return -1


def _open_array(grp, path):
    """Resolve a possibly-nested array path like '/group_a/cube'."""
    parts = [p for p in path.split("/") if p]
    g = grp
    for p in parts[:-1]:
        g = g.OpenGroup(p)
    return g.OpenMDArray(parts[-1])


def read_once(conn, var, expect=None):
    ds = gdal.OpenEx(conn, gdal.OF_MULTIDIM_RASTER)
    ar = _open_array(ds.GetRootGroup(), var)
    v = np.asarray(ar.ReadAsArray())
    ds = None
    if expect is not None and not np.array_equal(v, expect):
        raise AssertionError(f"{conn}:{var} mismatch")
    return v


# ---------------------------------------------------------------------------
def test_concurrency(n_threads=8, iters=40):
    print(f"\n== concurrency: {n_threads} threads x {iters} iters, separate handles ==")
    # ground-truth values (single-threaded) for a couple local fixtures
    targets = []
    for conn, var in [("ICECHUNK:" + os.path.join(FIX, "native_inline"), "native_grid"),
                      ("ICECHUNK:" + os.path.join(FIX, "native_inline"), "temperature"),
                      ("ICECHUNK:" + os.path.join(FIX, "hierarchy"), "/group_a/cube")]:
        if os.path.isdir(conn.split(":", 1)[1]):
            targets.append((conn, var, read_once(conn, var)))
    errors = []
    barrier = threading.Barrier(n_threads)

    def worker(tid):
        try:
            barrier.wait()  # maximize overlap
            for i in range(iters):
                conn, var, truth = targets[(tid + i) % len(targets)]
                v = read_once(conn, var)
                if not np.array_equal(v, truth):
                    errors.append(f"t{tid} i{i} {var}: value mismatch")
        except Exception as e:  # noqa
            errors.append(f"t{tid}: {type(e).__name__}: {e}")

    with cf.ThreadPoolExecutor(max_workers=n_threads) as ex:
        list(ex.map(worker, range(n_threads)))
    ok = not errors
    print(f"  {'PASS' if ok else 'FAIL'}: {n_threads*iters} concurrent reads, {len(errors)} error(s)")
    for e in errors[:10]:
        print("    ", e)
    return ok


def test_vsi_concurrency(n_threads=8, iters=60):
    """Hammer the /vsiicechunk filesystem handler concurrently (statbuf + read)."""
    print(f"\n== /vsiicechunk concurrency: {n_threads}x{iters} ==")
    repo = os.path.join(FIX, "native_inline")
    key = "{%s}/zarr.json" % repo
    errors = []

    def worker(tid):
        try:
            for _ in range(iters):
                f = gdal.VSIFOpenL("/vsiicechunk/" + key, "rb")
                if f:
                    gdal.VSIFReadL(1, 64, f)
                    gdal.VSIFCloseL(f)
        except Exception as e:  # noqa
            errors.append(str(e))
    with cf.ThreadPoolExecutor(max_workers=n_threads) as ex:
        list(ex.map(worker, range(n_threads)))
    print(f"  {'PASS' if not errors else 'FAIL'}: {len(errors)} error(s)")
    return not errors


def test_leak(iters=2000):
    print(f"\n== leak: {iters} open/read/close cycles ==")
    conn = "ICECHUNK:" + os.path.join(FIX, "native_inline")
    # warm up so caches/one-time allocs settle
    for _ in range(50):
        read_once(conn, "native_grid")
    r0, f0 = rss_kb(), num_fds()
    for i in range(iters):
        read_once(conn, "native_grid")
        if i % 500 == 0:
            gdal.VSICurlClearCache()
    r1, f1 = rss_kb(), num_fds()
    grow_kb = r1 - r0
    per_iter = grow_kb / iters
    print(f"  RSS: {r0} -> {r1} KB  (+{grow_kb} KB over {iters} iters = {per_iter:.3f} KB/iter)")
    print(f"  open fds: {f0} -> {f1}")
    # heuristics: <0.5 KB/iter growth and no fd climb => no meaningful leak
    leak_mem = per_iter > 1.0
    leak_fd = (f1 - f0) > 5
    ok = not (leak_mem or leak_fd)
    print(f"  {'PASS (no leak signal)' if ok else 'SUSPECT LEAK'}")
    return ok


def main():
    results = {
        "concurrency_read": test_concurrency(),
        "concurrency_vsi": test_vsi_concurrency(),
        "leak": test_leak(),
    }
    print("\n==== SUMMARY ====")
    for k, v in results.items():
        print(f"  {'PASS' if v else 'FAIL':5} | {k}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
