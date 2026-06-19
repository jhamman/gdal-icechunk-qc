#!/usr/bin/env python
"""Demo: reading Icechunk through GDAL via rasterio and rioxarray.

The Icechunk driver is MULTIDIMENSIONAL-ONLY, so GDAL's classic 2-D path
(which rasterio/rioxarray use) exposes each array as a SUBDATASET
(`ZARR:"/vsiicechunk/{<repo>}":/<var>`), not as direct bands. The user
workflow is therefore: open repo -> list .subdatasets -> open one subdataset.

Run:  source scripts/env.sh && $ICPY scripts/demo_rasterio_rioxarray.py
"""
import os

import numpy as np
import rasterio
from rasterio.windows import Window

print(f"rasterio {rasterio.__version__}  (GDAL {rasterio.__gdal_version__})")
try:
    import rioxarray  # noqa
    print(f"rioxarray {rioxarray.__version__}")
except Exception as e:  # noqa
    rioxarray = None
    print("rioxarray unavailable:", e)


def hr(t):
    print("\n" + "=" * 70 + f"\n{t}\n" + "=" * 70)


# ---------------------------------------------------------------- local
hr("Part 1 — local synthetic repo (fixtures/native_inline)")
repo = "ICECHUNK:fixtures/native_inline"
with rasterio.open(repo) as ds:
    subs = list(ds.subdatasets)
print("repo subdatasets:")
for s in subs:
    print("   ", s)

temp = [s for s in subs if s.endswith(":/temperature")][0]
print(f"\nrasterio.open('{temp}')")
with rasterio.open(temp) as src:
    print(f"  driver={src.driver} count={src.count} W={src.width} H={src.height} "
          f"dtype={src.dtypes[0]} crs={src.crs}")
    arr = src.read(1)
    print("  read(1) =\n", arr)

if rioxarray is not None:
    print("\nrioxarray.open_rasterio(temperature):")
    xda = rioxarray.open_rasterio(temp)
    print("  dims:", xda.dims, "shape:", tuple(xda.shape), "dtype:", xda.dtype)
    print("  values[0]:\n", np.asarray(xda.isel(band=0).values))
    xda.close()


# --------------------------------------------------------------- remote
hr("Part 2 — remote real dataset GLAD (s3://icechunk-public-data/v1/glad)")
env = dict(AWS_NO_SIGN_REQUEST="YES", AWS_REGION="us-east-1")
os.environ.update(env)
repo = "ICECHUNK:/vsis3/icechunk-public-data/v1/glad"
try:
    with rasterio.Env(**env):
        with rasterio.open(repo) as ds:
            subs = list(ds.subdatasets)
        print("repo subdatasets:")
        for s in subs:
            print("   ", s)
        lclu = [s for s in subs if s.endswith(":/lclu")][0]
        print(f"\nrasterio.open('{lclu}')")
        with rasterio.open(lclu) as src:
            print(f"  driver={src.driver} bands(count)={src.count} "
                  f"W={src.width} H={src.height} dtype={src.dtypes[0]}")
            print(f"  crs={src.crs}")
            print(f"  transform={src.transform!r}")
            # efficient windowed read of a 64x64 block (touches only the
            # native chunk(s) overlapping the window — true range reads)
            win = Window(col_off=720000, row_off=280000, width=64, height=64)
            block = src.read(1, window=win)  # band 1 == year[0]
            print(f"  windowed read {block.shape} band1/year0: "
                  f"min={int(block.min())} max={int(block.max())} "
                  f"unique={np.unique(block)[:8].tolist()}")
        if rioxarray is not None:
            print("\nrioxarray.open_rasterio(lclu) (lazy):")
            with rasterio.Env(**env):
                xda = rioxarray.open_rasterio(lclu)
                print("  dims:", xda.dims, "shape:", tuple(xda.shape),
                      "dtype:", xda.dtype, "crs:", xda.rio.crs)
                sub = xda.isel(band=0,
                               y=slice(280000, 280016),
                               x=slice(720000, 720016)).values
                print("  lazy 16x16 window min/max:", int(sub.min()), int(sub.max()))
                xda.close()
except Exception as e:  # noqa
    print("remote demo error:", type(e).__name__, str(e)[:200])

print("\nDONE")
