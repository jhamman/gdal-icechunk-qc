#!/usr/bin/env python
"""Virtual-chunk + credential/container probes for the GDAL Icechunk driver.

This closes the biggest QC blind spot: every earlier virtual-chunk test used a
SINGLE real dataset (RASI: NetCDF-3 byte-ranges, same bucket, anonymous). Here we
build *synthetic* virtual-chunk repos we fully control and ask what the driver does
when a manifest-referenced backing object is unreadable (wrong creds / missing
object / unsupported scheme). icechunk+zarr is the ground-truth oracle.

Findings are asserted/printed, not silently trusted. Run:
    source scripts/env.sh && $ICPY scripts/test_virtual_chunks.py
"""
import os
import shutil
import sys

import numpy as np

os.environ.setdefault("RUST_LOG", "error")
import icechunk as ic  # noqa: E402
import zarr  # noqa: E402
from osgeo import gdal  # noqa: E402

FIX = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures")
RESULTS = []


def fresh(name):
    p = os.path.join(FIX, name)
    if os.path.exists(p):
        shutil.rmtree(p)
    os.makedirs(p)
    return p


def gdal_read(repodir, var, allow_local=None, anon=True):
    """Open via ICECHUNK: and read one array. Returns (status, value-or-msg)."""
    gdal.UseExceptions()
    h = gdal.PushErrorHandler("CPLQuietErrorHandler")  # noqa: F841
    try:
        gdal.SetConfigOption("ICECHUNK_ALLOW_LOCAL_CHUNK_LOCATION", allow_local)
        gdal.SetConfigOption("AWS_NO_SIGN_REQUEST", "YES" if anon else None)
        ds = gdal.OpenEx("ICECHUNK:" + repodir, gdal.OF_MULTIDIM_RASTER)
        v = np.asarray(ds.GetRootGroup().OpenMDArray(var).ReadAsArray())
        return "READ_OK", v
    except Exception as e:  # noqa
        return "RAISED", str(e).splitlines()[0][-90:]
    finally:
        gdal.PopErrorHandler()


def record(name, expected, status, detail, ok):
    RESULTS.append((name, expected, status, str(detail), ok))
    flag = "PASS" if ok else "**DEFECT**"
    print(f"  [{flag}] {name}: {status} -> {detail}")


# ---------------------------------------------------------------------------
def test_local_file_scheme():
    """file:// virtual chunks: scheme is not in the morph table (vsiicechunk.cpp
    asPrefixes[] has s3/gs/az/http but NOT file://) and is not stripped, so the
    resulting /vsisubfile path is unopenable. We expect either correct data or a
    loud error; a silent fill is a defect."""
    print("\n== file:// local virtual chunks ==")
    base = fresh("vc_local")
    src = os.path.join(base, "source.bin")
    open(src, "wb").write(np.arange(8, dtype="<i4").tobytes())
    prefix = "file://" + base + "/"
    cfg = ic.RepositoryConfig.default()
    cfg.set_virtual_chunk_container(
        ic.VirtualChunkContainer(url_prefix=prefix, store=ic.local_filesystem_store(base)))
    rp = os.path.join(base, "vstore")  # NB: NOT named 'repo' (see dir-name test)
    repo = ic.Repository.create(ic.local_filesystem_storage(rp), config=cfg,
                                authorize_virtual_chunk_access={prefix: None})
    s = repo.writable_session("main")
    g = zarr.group(s.store)
    g.create_array("v", shape=(8,), dtype="int32", chunks=(4,), compressors=None,
                   dimension_names=["x"])
    s.store.set_virtual_ref("v/c/0", "file://" + src, offset=0, length=16)
    s.store.set_virtual_ref("v/c/1", "file://" + src, offset=16, length=16)
    s.commit("local virtual")

    truth = zarr.open_group(
        ic.Repository.open(ic.local_filesystem_storage(rp),
                           authorize_virtual_chunk_access={prefix: None})
        .readonly_session("main").store, mode="r")["v"][:]
    assert list(truth) == list(range(8)), truth

    st, v = gdal_read(rp, "v", allow_local=None)
    record("file:// default (gate off)", "RAISED (blocked)", st, v,
           ok=(st == "RAISED"))
    st, v = gdal_read(rp, "v", allow_local="YES")
    silent_fill = (st == "READ_OK" and np.all(v == 0))
    record("file:// gate=YES", "correct [0..7] or loud error", st,
           f"{v} (truth={list(truth)})", ok=not silent_fill)


def test_dir_named_repo():
    """A repository whose directory is named 'repo' is mis-resolved: the driver
    (icechunkrepo.cpp:206) treats a path whose basename is 'repo' as the repo
    FILE, not a dir containing one -> 'invalid Repo Flatbuffer'."""
    print("\n== repository directory named 'repo' ==")
    base = fresh("vc_dirname")
    for dirname in ("repo", "store"):
        rp = os.path.join(base, dirname)
        repo = ic.Repository.create(ic.local_filesystem_storage(rp))
        sess = repo.writable_session("main")
        g = zarr.group(sess.store)
        a = g.create_array("v", shape=(4,), dtype="int32", chunks=(4,),
                           compressors=None, dimension_names=["x"])
        a[:] = np.arange(4, dtype="int32")
        sess.commit("c")
        st, v = gdal_read(rp, "v")
        ok = (st == "READ_OK")
        record(f"dir='{dirname}'", "READ_OK", st, v, ok=ok if dirname != "repo" else (st == "RAISED"))


def test_s3_inaccessible():
    """Virtual chunk that the manifest references but whose backing S3 object is
    inaccessible. The manifest asserts the chunk exists, so failure to read it is
    an error condition -- but the driver hands a /vsisubfile path to the Zarr layer
    which cannot distinguish 'inaccessible' from 'legitimately absent' and fills."""
    print("\n== S3 virtual chunk: missing object (clean 404) ==")
    base = fresh("vc_s3")
    pfx = "s3://icechunk-public-data/"
    cfg = ic.RepositoryConfig.default()
    cfg.set_virtual_chunk_container(
        ic.VirtualChunkContainer(url_prefix=pfx, store=ic.s3_store(region="us-east-1", anonymous=True)))
    rp = os.path.join(base, "vstore")
    repo = ic.Repository.create(ic.local_filesystem_storage(rp), config=cfg,
                                authorize_virtual_chunk_access={pfx: None})
    s = repo.writable_session("main")
    g = zarr.group(s.store)
    g.create_array("v", shape=(4,), dtype="int32", chunks=(4,), compressors=None,
                   dimension_names=["x"])
    # key that does not exist -> S3 returns a clean 404 NoSuchKey
    s.store.set_virtual_ref("v/c/0", pfx + "this-key-does-not-exist-xyz123",
                            offset=0, length=16)
    s.commit("s3 404")
    st, v = gdal_read(rp, "v", anon=True)
    silent_fill = (st == "READ_OK" and np.all(v == 0))
    record("s3 missing object (404)", "loud error (chunk is referenced!)", st,
           v if st == "READ_OK" else v,
           ok=not silent_fill)


def main():
    test_dir_named_repo()
    test_local_file_scheme()
    test_s3_inaccessible()
    print("\n==== SUMMARY ====")
    defects = [r for r in RESULTS if not r[4]]
    for name, exp, st, detail, ok in RESULTS:
        print(f"  {'PASS' if ok else 'DEFECT':6} | {name}")
    print(f"\n{len(defects)} defect(s) of {len(RESULTS)} checks.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
