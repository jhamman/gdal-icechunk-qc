#!/usr/bin/env python
"""Light performance probe: time one backend reading one slice of one Icechunk array.

Run once per (backend, target) in a FRESH process so the open is cold.
Backends: gdal (osgeo.gdal multidim), zarr (icechunk + zarr-python), xarray (icechunk + xarray).

Emits a single JSON line: {backend,target,var,open_s,cold_read_s,warm_read_s,mb,mbps}.

Examples:
  bench.py --backend gdal  --local fixtures/bench_native --var data --slice ":,:"
  bench.py --backend zarr  --bucket icechunk-public-data --prefix v1/glad --region us-east-1 \
           --var lclu --slice "0,0:4000,0:4000"
"""
import argparse, json, sys, time
import numpy as np


def parse_slice(spec, ndim):
    """'0,0:4000,:' -> list of int|slice."""
    parts = [p.strip() for p in spec.split(",")] if spec else [":"] * ndim
    out = []
    for p in parts:
        if ":" in p:
            a, _, b = p.partition(":")
            out.append(slice(int(a) if a else None, int(b) if b else None))
        else:
            out.append(int(p))
    return out


def gdal_read(args):
    from osgeo import gdal
    gdal.UseExceptions()
    if not args.local:
        gdal.SetConfigOption("AWS_NO_SIGN_REQUEST", "YES")
        gdal.SetConfigOption("AWS_REGION", args.region)
    conn = args.local or f"/vsis3/{args.bucket}/{args.prefix}".rstrip("/")
    t = time.perf_counter()
    ds = gdal.OpenEx(conn, gdal.OF_MULTIDIM_RASTER)
    ar = ds.GetRootGroup().OpenMDArray(args.var)
    shape = [d.GetSize() for d in ar.GetDimensions()]
    open_s = time.perf_counter() - t

    sl = parse_slice(args.slice, len(shape))
    start, count = [], []
    for s, n in zip(sl, shape):
        if isinstance(s, int):
            start.append(s); count.append(1)
        else:
            b = s.start or 0; e = s.stop if s.stop is not None else n
            start.append(b); count.append(e - b)

    def one():
        a = np.asarray(ar.ReadAsArray(start, count))
        return a.size, a.dtype.itemsize
    return open_s, start, count, one


def zarr_read(args):
    import icechunk as ic, zarr
    t = time.perf_counter()
    if args.local:
        repo = ic.Repository.open(ic.local_filesystem_storage(args.local))
    else:
        repo = ic.Repository.open(ic.s3_storage(bucket=args.bucket, prefix=args.prefix.rstrip("/"),
                                                region=args.region, anonymous=True))
    store = repo.readonly_session("main").store
    g = zarr.open_group(store, mode="r")
    arr = g[args.var]
    shape = arr.shape
    open_s = time.perf_counter() - t
    sl = tuple(parse_slice(args.slice, len(shape)))

    def one():
        a = arr[sl]
        return a.size, a.dtype.itemsize
    return open_s, sl, None, one


def xarray_read(args):
    import icechunk as ic, xarray as xr
    t = time.perf_counter()
    if args.local:
        repo = ic.Repository.open(ic.local_filesystem_storage(args.local))
    else:
        repo = ic.Repository.open(ic.s3_storage(bucket=args.bucket, prefix=args.prefix.rstrip("/"),
                                                region=args.region, anonymous=True))
    ds = xr.open_zarr(repo.readonly_session("main").store, consolidated=False, chunks=None)
    da = ds[args.var]
    shape = da.shape
    open_s = time.perf_counter() - t
    sl = tuple(parse_slice(args.slice, len(shape)))

    def one():
        a = np.asarray(da[sl].values)
        return a.size, a.dtype.itemsize
    return open_s, sl, None, one


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--backend", required=True, choices=["gdal", "zarr", "xarray"])
    p.add_argument("--local")
    p.add_argument("--bucket"); p.add_argument("--prefix", default=""); p.add_argument("--region", default="us-east-1")
    p.add_argument("--var", required=True)
    p.add_argument("--slice", default=":")
    p.add_argument("--reps", type=int, default=4)
    args = p.parse_args()

    fn = {"gdal": gdal_read, "zarr": zarr_read, "xarray": xarray_read}[args.backend]
    open_s, _, _, one = fn(args)

    times = []
    size = itemsize = 0
    for i in range(args.reps):
        t = time.perf_counter()
        size, itemsize = one()
        times.append(time.perf_counter() - t)
    mb = size * itemsize / 1e6
    cold = times[0]
    warm = float(np.median(times[1:])) if len(times) > 1 else times[0]
    print(json.dumps({
        "backend": args.backend, "target": args.local or f"{args.bucket}/{args.prefix}",
        "var": args.var, "open_s": round(open_s, 4),
        "cold_read_s": round(cold, 4), "warm_read_s": round(warm, 4),
        "mb": round(mb, 2), "warm_mbps": round(mb / warm, 1) if warm else None,
    }))


if __name__ == "__main__":
    sys.exit(main())
