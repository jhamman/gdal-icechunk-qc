#!/usr/bin/env python
"""Empirically bisect the B2 blocker threshold.

B2 (from QC_REPORT): large manifests fail GDAL's FlatBuffers verification. The
root-cause hypothesis was read from code: icechunkmanifest.cpp constructs
`flatbuffers::Verifier` with DEFAULT options, and FlatBuffers' default
Verifier::Options::max_tables == 1,000,000. A manifest with >1e6 chunk-ref tables
should fail. This proves it: build arrays whose single manifest holds a controlled
number of chunk refs straddling 1e6, and record the GDAL read pass/fail boundary.

Each array is shape (N,) chunks (1,) -> N one-element chunks -> N refs in one
manifest. int8 + tiny -> chunks are inlined but still one manifest table each.

Run: source scripts/env.sh && $ICPY scripts/bisect_b2.py
"""
import os
import shutil
import sys
import time

import numpy as np

os.environ.setdefault("RUST_LOG", "error")
import icechunk as ic  # noqa: E402
import zarr  # noqa: E402
from osgeo import gdal  # noqa: E402

FIX = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures")

# straddle the conjectured 1,000,000 table limit
COUNTS = [500_000, 900_000, 990_000, 1_000_000, 1_000_050, 1_050_000, 1_200_000]


def build_and_test(n):
    base = os.path.join(FIX, "_b2")
    rp = os.path.join(base, f"n{n}")
    if os.path.exists(rp):
        shutil.rmtree(rp)
    os.makedirs(rp, exist_ok=True)
    t0 = time.perf_counter()
    repo = ic.Repository.create(ic.local_filesystem_storage(rp))
    s = repo.writable_session("main")
    g = zarr.group(s.store)
    a = g.create_array("v", shape=(n,), dtype="int8", chunks=(1,),
                       compressors=None, dimension_names=["x"])
    a[:] = np.ones(n, dtype="int8")  # all ones so fill(0) != data -> detect silent fill
    s.commit(f"n={n}")
    build_s = time.perf_counter() - t0

    # how big is the (single) manifest on disk + decompressed?
    mdir = os.path.join(rp, "manifests")
    mans = [f for f in os.listdir(mdir) if not f.startswith(".")] if os.path.isdir(mdir) else []
    man_sz = max((os.path.getsize(os.path.join(mdir, m)) for m in mans), default=0)

    gdal.UseExceptions()
    gdal.PushErrorHandler("CPLQuietErrorHandler")
    status, detail = "", ""
    try:
        ds = gdal.OpenEx("ICECHUNK:" + rp, gdal.OF_MULTIDIM_RASTER)
        ar = ds.GetRootGroup().OpenMDArray("v")
        # read a few elements near the end (forces the big manifest)
        v = np.asarray(ar.ReadAsArray([n - 4], [4]))
        if np.all(v == 1):
            status, detail = "READ_OK", f"tail={v}"
        elif np.all(v == 0):
            status, detail = "SILENT_FILL", f"tail={v} (expected all 1!)"
        else:
            status, detail = "PARTIAL", f"tail={v}"
    except Exception as e:
        status, detail = "RAISED", str(e).splitlines()[0][-70:]
    finally:
        gdal.PopErrorHandler()
    shutil.rmtree(rp, ignore_errors=True)  # reclaim disk between trials
    return build_s, man_sz, status, detail


def main():
    print(f"{'refs':>10} {'build_s':>8} {'manifest_B':>11}  status")
    boundary = None
    prev_ok = True
    for n in COUNTS:
        bs, msz, st, detail = build_and_test(n)
        print(f"{n:>10} {bs:>8.1f} {msz:>11}  {st}  {detail}")
        sys.stdout.flush()
        ok = (st == "READ_OK")
        if prev_ok and not ok and boundary is None:
            boundary = n
        prev_ok = ok
    print(f"\nFirst failing ref-count: {boundary} "
          f"(conjectured FlatBuffers default max_tables = 1,000,000)")


if __name__ == "__main__":
    sys.exit(main())
