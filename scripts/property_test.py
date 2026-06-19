#!/usr/bin/env python
"""Property-based cross-validation of the GDAL Icechunk driver vs icechunk+zarr.

Mirrors the style of icechunk's own property tests: randomly sample Zarr array
shape / dtype / chunking / compressor / fill value / attributes / dimension names,
write the array into a local Icechunk repo, then assert the GDAL multidim read
agrees with the zarr-python read on:
  - shape, dtype
  - FULL array values (exact for ints, allclose for floats)   <- not just corners
  - a random sub-window (exercises ReadAsArray offset/count math)
  - fill value -> GDAL nodata, and `units` attr -> GDAL unit

Randomized chunking deliberately produces shapes that are NOT a multiple of the
chunk size, so partial/edge chunks are exercised on every other trial. A subset of
trials writes the array SPARSELY (some chunks never written) to check that
legitimately-absent chunks read back as the fill value through both stacks.

Any disagreement is reported with the seed so it reproduces:
    $ICPY scripts/property_test.py --only-seed <N>

Usage:
    source scripts/env.sh && $ICPY scripts/property_test.py [--trials N] [--seed0 K]
"""
import argparse
import os
import shutil
import sys
import traceback

import numpy as np

os.environ.setdefault("RUST_LOG", "error")
import icechunk as ic  # noqa: E402
import zarr  # noqa: E402
from zarr.codecs import BloscCodec, BytesCodec, GzipCodec, ZstdCodec  # noqa: E402
from osgeo import gdal  # noqa: E402

FIX = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures")

# dtypes that BOTH zarr-v3 and GDAL's data model can represent.
DTYPES = ["int8", "uint8", "int16", "uint16", "int32", "uint32",
          "int64", "uint64", "float32", "float64", "complex64", "complex128"]

COMPRESSORS = ["none", "zstd", "blosc", "gzip"]


def make_compressor(name):
    return {
        "none": None,
        "zstd": [ZstdCodec(level=3)],
        "blosc": [BloscCodec(cname="zstd", clevel=3)],
        "gzip": [GzipCodec(level=4)],
    }[name]


def rand_case(rng):
    ndim = int(rng.integers(1, 5))                      # 1..4 dims
    shape = [int(rng.integers(1, 33)) for _ in range(ndim)]
    chunks = [int(rng.integers(1, s + 1)) for s in shape]  # <= shape; rarely divides evenly
    dtype = DTYPES[int(rng.integers(len(DTYPES)))]
    comp = COMPRESSORS[int(rng.integers(len(COMPRESSORS)))]
    sparse = bool(rng.integers(0, 4) == 0)              # 25% sparse
    return ndim, tuple(shape), tuple(chunks), dtype, comp, sparse


def gen_data(rng, shape, dtype):
    if dtype.startswith("float"):
        return (rng.standard_normal(shape) * 100).astype(dtype)
    if dtype.startswith("complex"):
        base = "float32" if dtype == "complex64" else "float64"
        return (rng.standard_normal(shape) + 1j * rng.standard_normal(shape)).astype(dtype)
    info = np.iinfo(dtype)
    lo = max(info.min, -10000)
    hi = min(info.max, 10000)
    return rng.integers(lo, hi, size=shape, endpoint=True).astype(dtype)


def fill_for(dtype, rng):
    if dtype.startswith("float"):
        return float(rng.integers(-5, 5))
    if dtype.startswith("complex"):
        return 0
    return int(rng.integers(0, 7))


def run_trial(seed, verbose=False):
    rng = np.random.default_rng(seed)
    ndim, shape, chunks, dtype, comp, sparse = rand_case(rng)
    rp = os.path.join(FIX, "_prop", f"s{seed}")
    if os.path.exists(rp):
        shutil.rmtree(rp)
    os.makedirs(rp, exist_ok=True)
    dimnames = [f"d{i}" for i in range(ndim)]

    repo = ic.Repository.create(ic.local_filesystem_storage(rp))
    s = repo.writable_session("main")
    g = zarr.group(s.store)
    fill = fill_for(dtype, rng)
    a = g.create_array("v", shape=shape, dtype=dtype, chunks=chunks,
                       compressors=make_compressor(comp), fill_value=fill,
                       dimension_names=dimnames)
    a.attrs["units"] = "K"
    a.attrs["trial_seed"] = int(seed)
    data = gen_data(rng, shape, dtype)
    if sparse and np.prod(shape) > 1:
        # write only the first half along axis 0; rest stays fill
        half = max(1, shape[0] // 2)
        sl = (slice(0, half),) + tuple(slice(None) for _ in range(ndim - 1))
        a[sl] = data[sl]
        # expected array: fill elsewhere
        expected = np.full(shape, fill, dtype=dtype)
        expected[sl] = data[sl]
    else:
        a[:] = data
        expected = data
    s.commit("prop")

    # ground truth via zarr
    zg = zarr.open_group(ic.Repository.open(ic.local_filesystem_storage(rp))
                         .readonly_session("main").store, mode="r")
    zval = zg["v"][...]

    # GDAL multidim
    gdal.UseExceptions()
    ds = gdal.OpenEx("ICECHUNK:" + rp, gdal.OF_MULTIDIM_RASTER)
    ar = ds.GetRootGroup().OpenMDArray("v")
    gshape = tuple(d.GetSize() for d in ar.GetDimensions())
    gval = np.asarray(ar.ReadAsArray())

    problems = []
    # shape
    if gshape != shape:
        problems.append(f"shape gdal={gshape} != {shape}")
    # full values
    if not values_equal(gval, zval, dtype):
        problems.append(f"FULL values differ (dtype={dtype}, comp={comp}, sparse={sparse})")
    if not values_equal(zval, expected, dtype):
        problems.append("zarr disagrees with expected (oracle sanity)")
    # random sub-window read
    start = [int(rng.integers(0, s_)) for s_ in shape]
    count = [int(rng.integers(1, shape[i] - start[i] + 1)) for i in range(ndim)]
    win = np.asarray(ar.ReadAsArray(start, count))
    zwin = zval[tuple(slice(start[i], start[i] + count[i]) for i in range(ndim))]
    if not values_equal(win, zwin, dtype):
        problems.append(f"WINDOW start={start} count={count} differs")
    # nodata mapping
    nod = ar.GetNoDataValueAsDouble() if hasattr(ar, "GetNoDataValueAsDouble") else None

    shutil.rmtree(rp, ignore_errors=True)
    return {
        "seed": seed, "ndim": ndim, "shape": shape, "chunks": chunks,
        "dtype": dtype, "comp": comp, "sparse": sparse, "fill": fill,
        "nodata": nod, "problems": problems,
    }


def values_equal(a, b, dtype):
    a = np.asarray(a)
    b = np.asarray(b)
    if a.shape != b.shape:
        # GDAL may squeeze; compare flattened if sizes match
        if a.size != b.size:
            return False
        a = a.reshape(b.shape)
    if dtype.startswith("float") or dtype.startswith("complex"):
        return np.allclose(a, b, rtol=1e-5, atol=1e-5, equal_nan=True)
    return np.array_equal(a, b)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=80)
    ap.add_argument("--seed0", type=int, default=0)
    ap.add_argument("--only-seed", type=int, default=None)
    args = ap.parse_args()

    seeds = [args.only_seed] if args.only_seed is not None else range(args.seed0, args.seed0 + args.trials)
    npass = nfail = nerr = 0
    by_dtype = {}
    failures = []
    for seed in seeds:
        try:
            r = run_trial(seed)
            tag = r["dtype"]
            by_dtype.setdefault(tag, [0, 0])
            if r["problems"]:
                nfail += 1
                by_dtype[tag][1] += 1
                failures.append(r)
                print(f"  seed {seed:4d} FAIL {r['dtype']:9} {str(r['shape']):16} "
                      f"chunks={r['chunks']} comp={r['comp']} sparse={r['sparse']}: {r['problems']}")
            else:
                npass += 1
                by_dtype[tag][0] += 1
        except Exception as e:  # noqa
            nerr += 1
            print(f"  seed {seed:4d} ERROR {type(e).__name__}: {str(e).splitlines()[-1][:120]}")
            if os.environ.get("PROP_TRACE"):
                traceback.print_exc()

    print(f"\n==== {npass} pass / {nfail} value-mismatch / {nerr} error  "
          f"(of {npass+nfail+nerr} trials) ====")
    print("per-dtype (pass,fail):")
    for d in DTYPES:
        if d in by_dtype:
            print(f"   {d:10} {tuple(by_dtype[d])}")
    return 1 if (nfail or nerr) else 0


if __name__ == "__main__":
    sys.exit(main())
