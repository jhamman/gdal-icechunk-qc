#!/usr/bin/env python
"""Cross-validate the GDAL Icechunk driver against the icechunk+xarray ground truth.

Opens the SAME repo two ways and compares structure + sampled values:
  (1) GDAL multidim: gdal.OpenEx(conn, OF_MULTIDIM_RASTER) -> RootGroup -> MDArrays
  (2) ground truth : xarray over an icechunk readonly session store

Usage:
  # local repo
  micromamba run -n gdalic python scripts/compare.py --local fixtures/native_inline
  # remote (anonymous S3 icechunk repo)
  micromamba run -n gdalic python scripts/compare.py \
      --bucket icechunk-public-data --prefix v1/glad --region us-east-1 \
      [--branch main | --tag v1] [--virtual-anon-prefix s3://nasa-waterinsight/RASI/]

Exit code 0 if all compared variables agree within tolerance, else 1.
"""
import argparse
import sys

import numpy as np
from osgeo import gdal

gdal.UseExceptions()


def gdal_conn(args):
    if args.local:
        base = args.local
    else:
        base = f"/vsis3/{args.bucket}/{args.prefix}".rstrip("/")
    if args.branch and args.branch != "main":
        return f"ICECHUNK:{base}?branch={args.branch}"
    if args.tag:
        return f"ICECHUNK:{base}?tag={args.tag}"
    return base


def collect_gdal_arrays(group, prefix=""):
    """Return {name: MDArray} recursively."""
    out = {}
    for nm in group.GetMDArrayNames() or []:
        ar = group.OpenMDArray(nm)
        if ar is not None:
            out[prefix + nm] = ar
    for gnm in group.GetGroupNames() or []:
        sub = group.OpenGroup(gnm)
        if sub is not None:
            out.update(collect_gdal_arrays(sub, prefix + gnm + "/"))
    return out


def open_xarray(args):
    import icechunk as ic
    import xarray as xr

    if args.local:
        storage = ic.local_filesystem_storage(args.local)
        repo = ic.Repository.open(storage)
    else:
        kw = dict(bucket=args.bucket, prefix=args.prefix.rstrip("/"),
                  region=args.region, anonymous=True)
        storage = ic.s3_storage(**kw)
        if args.virtual_anon_prefix:
            vcred = ic.containers_credentials(
                {args.virtual_anon_prefix: ic.s3_credentials(anonymous=True)})
            repo = ic.Repository.open(storage=storage,
                                      authorize_virtual_chunk_access=vcred)
        else:
            repo = ic.Repository.open(storage=storage)
    if args.tag:
        session = repo.readonly_session(tag=args.tag)
    else:
        session = repo.readonly_session(args.branch or "main")
    ds = xr.open_zarr(session.store, chunks=None, consolidated=False)
    return ds


def corner(shape, cap=4096):
    """Pick a small hyperslab: index 0..n per dim, product <= cap."""
    start = [0] * len(shape)
    count = [int(s) for s in shape]
    # shrink from the leading dims until product under cap
    import math
    while math.prod(count) > cap:
        # reduce the largest dim
        i = max(range(len(count)), key=lambda k: count[k])
        if count[i] <= 1:
            break
        count[i] = max(1, count[i] // 2)
    return start, count


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--local")
    p.add_argument("--bucket")
    p.add_argument("--prefix", default="")
    p.add_argument("--region", default="us-east-1")
    p.add_argument("--branch")
    p.add_argument("--tag")
    p.add_argument("--virtual-anon-prefix",
                   help="s3:// prefix to authorize anonymously for virtual chunks")
    p.add_argument("--rtol", type=float, default=1e-6)
    p.add_argument("--atol", type=float, default=1e-6)
    args = p.parse_args()

    if not args.local and not args.bucket:
        p.error("need --local or --bucket")

    if not args.local:
        gdal.SetConfigOption("AWS_NO_SIGN_REQUEST", "YES")
        gdal.SetConfigOption("AWS_REGION", args.region)

    conn = gdal_conn(args)
    print(f"== GDAL conn: {conn}")
    ds = gdal.OpenEx(conn, gdal.OF_MULTIDIM_RASTER)
    if ds is None:
        print("FAIL: GDAL could not open")
        return 2
    rg = ds.GetRootGroup()
    garrs = collect_gdal_arrays(rg)
    print(f"   GDAL arrays: {sorted(garrs)}")

    xds = open_xarray(args)
    xvars = {k: xds[k] for k in list(xds.data_vars) + list(xds.coords)}
    print(f"   xarray vars: {sorted(xvars)}")

    common = sorted(set(garrs) & set(xvars))
    only_g = sorted(set(garrs) - set(xvars))
    only_x = sorted(set(xvars) - set(garrs))
    if only_g:
        print(f"   ONLY in GDAL: {only_g}")
    if only_x:
        print(f"   ONLY in xarray: {only_x}")
    if not common:
        print("FAIL: no common variables to compare")
        return 1

    failures = []
    for name in common:
        ar = garrs[name]
        xv = xvars[name]
        gshape = tuple(d.GetSize() for d in ar.GetDimensions())
        xshape = tuple(xv.shape)
        gdims = [d.GetName() for d in ar.GetDimensions()]
        xdims = list(xv.dims)
        line = f" - {name}: gshape={gshape} xshape={xshape} gdims={gdims} xdims={xdims} gdtype={ar.GetDataType()}"
        if gshape != xshape:
            failures.append(f"{name}: shape mismatch {gshape} != {xshape}")
            print(line + "  SHAPE-MISMATCH")
            continue
        try:
            start, count = corner(gshape)
            if gshape:
                gval = ar.ReadAsArray(start, count)
                sl = tuple(slice(s, s + c) for s, c in zip(start, count))
                # slice the lazy DataArray FIRST, then read (avoids materializing
                # the whole variable — critical for huge arrays like GLAD)
                xval = np.asarray(xv[sl].values)
            else:
                gval = ar.ReadAsArray()
                xval = np.asarray(xv.values)
            gval = np.asarray(gval)
            ok = np.allclose(gval.astype("float64"), xval.astype("float64"),
                             rtol=args.rtol, atol=args.atol, equal_nan=True)
            if not ok:
                # show first diff
                d = np.abs(gval.astype("float64") - xval.astype("float64"))
                failures.append(f"{name}: value mismatch maxdiff={np.nanmax(d)}")
                print(line + f"  VALUE-MISMATCH maxdiff={np.nanmax(d)}")
            else:
                print(line + f"  OK (sampled {count})")
        except Exception as e:  # noqa
            failures.append(f"{name}: read/compare error {e}")
            print(line + f"  ERROR {e}")

    print()
    if failures:
        print("RESULT: FAIL")
        for f in failures:
            print("  *", f)
        return 1
    print(f"RESULT: PASS ({len(common)} variables agree)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
