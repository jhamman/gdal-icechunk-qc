#!/usr/bin/env python
"""Generate local Icechunk repositories to exercise the GDAL Icechunk driver
offline, including edge/negative cases.

Run inside the `gdalic` env:  micromamba run -n gdalic python scripts/make_synthetic.py

Creates repos under fixtures/ :
  - native_inline   : 2D float64 array, mix of inline (tiny) + native chunks, attrs
  - multi_ref       : multi-branch + multi-tag history (main, dev, tag v1/v2)
  - scalar          : 0-d scalar array + a "crs"-like scalar
  - unsupported_codec : array using numcodecs.shuffle/zlib (expect GDAL ERROR 6)
  - corrupt_manifest : copy of native_inline with a manifest file truncated
  - hierarchy       : nested groups + multiple arrays + 3D array
"""
import json
import os
import shutil
import sys

import numpy as np

FIX = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures")


def fresh(name):
    path = os.path.join(FIX, name)
    if os.path.exists(path):
        shutil.rmtree(path)
    os.makedirs(path, exist_ok=True)
    return path


def make_native_inline():
    import icechunk
    import zarr

    path = fresh("native_inline")
    repo = icechunk.Repository.create(icechunk.local_filesystem_storage(path))
    s = repo.writable_session("main")
    g = zarr.group(s.store)
    # 6x6 array, 3x3 chunks -> 4 chunks. float64.
    data = (np.arange(36, dtype="float64").reshape(6, 6) + 0.5)
    a = g.create_array("temperature", shape=(6, 6), dtype="float64",
                       chunks=(3, 3), compressors=None, dimension_names=["y", "x"])
    a[:] = data
    a.attrs["units"] = "K"
    # larger non-inline native chunks: 64x64 int32, 32x32 chunks -> 4 native chunks (16KB each)
    big = g.create_array("native_grid", shape=(64, 64), dtype="int32",
                         chunks=(32, 32), compressors=None, dimension_names=["ny", "nx"])
    big[:] = np.arange(64 * 64, dtype="int32").reshape(64, 64)
    # tiny 1-D array likely inlined by icechunk
    b = g.create_array("scale", shape=(3,), dtype="int16", chunks=(3,),
                       compressors=None, dimension_names=["k"])
    b[:] = np.array([1, 2, 3], dtype="int16")
    g.attrs["title"] = "synthetic native+inline"
    s.commit("native+inline commit")
    print("OK native_inline", data.sum())


def make_multi_ref():
    import icechunk
    import zarr

    path = fresh("multi_ref")
    repo = icechunk.Repository.create(icechunk.local_filesystem_storage(path))
    s = repo.writable_session("main")
    g = zarr.group(s.store)
    a = g.create_array("v", shape=(4,), dtype="int32", chunks=(2,),
                       compressors=None, dimension_names=["x"])
    a[:] = np.array([10, 11, 12, 13], dtype="int32")
    snap1 = s.commit("commit on main #1")
    repo.create_tag("v1", snapshot_id=snap1)

    # dev branch with different values
    repo.create_branch("dev", snapshot_id=snap1)
    sd = repo.writable_session("dev")
    gd = zarr.open_group(sd.store)
    gd["v"][:] = np.array([20, 21, 22, 23], dtype="int32")
    snap2 = sd.commit("commit on dev")
    repo.create_tag("v2", snapshot_id=snap2)

    # advance main
    sm = repo.writable_session("main")
    gm = zarr.open_group(sm.store)
    gm["v"][:] = np.array([30, 31, 32, 33], dtype="int32")
    sm.commit("commit on main #2")
    print("OK multi_ref (branches main/dev, tags v1/v2)")


def make_scalar():
    import icechunk
    import zarr

    path = fresh("scalar")
    repo = icechunk.Repository.create(icechunk.local_filesystem_storage(path))
    s = repo.writable_session("main")
    g = zarr.group(s.store)
    a = g.create_array("scalar", shape=(), dtype="int32", compressors=None)
    a[...] = 42
    crs = g.create_array("crs", shape=(), dtype="int32", compressors=None)
    crs[...] = 0
    crs.attrs["spatial_ref"] = "EPSG:4326"
    s.commit("scalar commit")
    print("OK scalar")


def make_hierarchy():
    import icechunk
    import zarr

    path = fresh("hierarchy")
    repo = icechunk.Repository.create(icechunk.local_filesystem_storage(path))
    s = repo.writable_session("main")
    root = zarr.group(s.store)
    grp = root.create_group("group_a")
    a3 = grp.create_array("cube", shape=(2, 4, 4), dtype="float32",
                          chunks=(1, 2, 2), compressors=None,
                          dimension_names=["t", "y", "x"])
    a3[:] = np.arange(32, dtype="float32").reshape(2, 4, 4)
    x = root.create_array("x", shape=(4,), dtype="float64", chunks=(4,),
                          compressors=None, dimension_names=["x"])
    x[:] = np.linspace(0, 3, 4)
    s.commit("hierarchy commit")
    print("OK hierarchy")


def make_unsupported_codec():
    import icechunk
    import zarr

    path = fresh("unsupported_codec")
    repo = icechunk.Repository.create(icechunk.local_filesystem_storage(path))
    s = repo.writable_session("main")
    g = zarr.group(s.store)
    made = []
    try:
        from numcodecs.zarr3 import Shuffle
        a = g.create_array("shuffled", shape=(8,), dtype="int32", chunks=(8,),
                           filters=[Shuffle(elementsize=4)], compressors=None,
                           dimension_names=["x"])
        a[:] = np.arange(8, dtype="int32")
        made.append("numcodecs.shuffle")
    except Exception as e:  # noqa
        print("  (shuffle skipped:", e, ")")
    try:
        from numcodecs.zarr3 import Zlib
        a = g.create_array("zlibbed", shape=(8,), dtype="int32", chunks=(8,),
                           compressors=[Zlib()], dimension_names=["x"])
        a[:] = np.arange(8, dtype="int32")
        made.append("numcodecs.zlib")
    except Exception as e:  # noqa
        print("  (zlib skipped:", e, ")")
    s.commit("unsupported codec commit")
    print("OK unsupported_codec ->", made or "NONE MADE")


def make_corrupt_manifest():
    src = os.path.join(FIX, "native_inline")
    if not os.path.exists(src):
        make_native_inline()
    path = fresh("corrupt_manifest")
    shutil.rmtree(path)
    shutil.copytree(src, path)
    mdir = os.path.join(path, "manifests")
    mans = [f for f in os.listdir(mdir) if not f.startswith(".")]
    if not mans:
        print("WARN no manifest to corrupt (all inline?)")
        return
    # truncate ALL manifests so any native-chunk read deterministically hits a
    # corrupt one (which manifest backs which array is not stable across runs)
    for m in mans:
        with open(os.path.join(mdir, m), "r+b") as f:
            f.truncate(20)  # keep header-ish, drop body -> should fail cleanly
    print("OK corrupt_manifest (truncated", len(mans), "manifest(s))")


if __name__ == "__main__":
    os.makedirs(FIX, exist_ok=True)
    make_native_inline()
    make_multi_ref()
    make_scalar()
    make_hierarchy()
    make_unsupported_codec()
    make_corrupt_manifest()
    print("\nAll fixtures under", FIX)
