#!/usr/bin/env bash
# Offline QC of the GDAL Icechunk driver against synthetic fixtures.
set -u
cd "$(dirname "$0")/.."
source scripts/env.sh
PY="$ICPY"

hr(){ echo; echo "==================== $* ===================="; }

hr "list-branches / list-tags (multi_ref)"
gdal driver icechunk list-branches fixtures/multi_ref
gdal driver icechunk list-tags     fixtures/multi_ref

hr "cross-validate native_inline"
$PY scripts/compare.py --local fixtures/native_inline

hr "cross-validate scalar"
$PY scripts/compare.py --local fixtures/scalar

hr "cross-validate hierarchy"
$PY scripts/compare.py --local fixtures/hierarchy

hr "ref selection: multi_ref main / tag v1 / tag v2 / branch dev (GDAL read)"
for sel in "" "?tag=v1" "?tag=v2" "?branch=dev"; do
  printf "  %-12s -> " "${sel:-main}"
  $PY - "fixtures/multi_ref${sel}" <<'EOF'
import sys
from osgeo import gdal
gdal.UseExceptions()
conn = sys.argv[1]
if "?" in conn:
    conn = "ICECHUNK:" + conn
ds = gdal.OpenEx(conn, gdal.OF_MULTIDIM_RASTER)
v = ds.GetRootGroup().OpenMDArray("v").ReadAsArray()
print(list(v))
EOF
done
echo "  (expected: main=[30,31,32,33] v1=[10,11,12,13] v2/dev=[20,21,22,23])"

hr "cross-validate multi_ref per ref (GDAL vs xarray)"
$PY scripts/compare.py --local fixtures/multi_ref
$PY scripts/compare.py --local fixtures/multi_ref --tag v1
$PY scripts/compare.py --local fixtures/multi_ref --branch dev

hr "NEGATIVE: unsupported codec (numcodecs.zlib) - expect clean error, no crash"
gdal mdim info fixtures/unsupported_codec 2>&1 | head -20
echo "--- try to READ the zlibbed array ---"
$PY - <<'EOF'
from osgeo import gdal
gdal.UseExceptions()
ds = gdal.OpenEx("fixtures/unsupported_codec", gdal.OF_MULTIDIM_RASTER)
rg = ds.GetRootGroup()
for nm in rg.GetMDArrayNames():
    try:
        a = rg.OpenMDArray(nm)
        v = a.ReadAsArray()
        print(f"  {nm}: READ OK -> {list(v.ravel())[:8]}")
    except Exception as e:
        print(f"  {nm}: read failed gracefully -> {type(e).__name__}: {str(e)[:120]}")
print("  process still alive: OK")
EOF

hr "NEGATIVE: corrupt (truncated) manifest - expect clean error, no crash"
$PY - <<'EOF'
from osgeo import gdal
gdal.UseExceptions()
try:
    ds = gdal.OpenEx("fixtures/corrupt_manifest", gdal.OF_MULTIDIM_RASTER)
    rg = ds.GetRootGroup()
    a = rg.OpenMDArray("native_grid")   # native (non-inline) chunks -> needs the manifest
    v = a.ReadAsArray()
    print("  READ OK ->", v.shape, "(unexpected: corrupt manifest should fail)")
except Exception as e:
    print(f"  failed gracefully -> {type(e).__name__}: {str(e)[:160]}")
print("  process still alive: OK")
EOF

hr "DONE"
