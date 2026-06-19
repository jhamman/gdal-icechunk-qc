#!/usr/bin/env bash
# One-shot setup for reproducing the GDAL Icechunk-driver QC from scratch.
#   bash setup.sh
# Creates the build env, clones + builds the PR, verifies the driver registers, and
# (optionally) builds rasterio/rioxarray against the fresh GDAL. Idempotent: re-running
# skips steps already done. Override knobs via env vars (see below).
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_ROOT"

GDALIC_ENV="${GDALIC_ENV:-gdalic}"
GDAL_REMOTE="${GDAL_REMOTE:-https://github.com/rouault/gdal.git}"   # or OSGeo/gdal + gh pr checkout 14755
GDAL_BRANCH="${GDAL_BRANCH:-icechunk}"
GDAL_SHA="${GDAL_SHA:-8c2b212}"                                     # commit reviewed in QC_REPORT.md
WITH_RASTERIO="${WITH_RASTERIO:-1}"
JOBS="${JOBS:-$( (sysctl -n hw.ncpu 2>/dev/null || nproc 2>/dev/null || echo 4) )}"

# package manager: prefer micromamba, fall back to conda/mamba
MM="${MM:-}"
if [ -z "$MM" ]; then
  for c in micromamba mamba conda; do command -v "$c" >/dev/null 2>&1 && { MM="$c"; break; }; done
fi
[ -n "$MM" ] || { echo "ERROR: need micromamba/mamba/conda on PATH"; exit 1; }
echo "==> package manager: $MM"

# 1. build/ground-truth env -------------------------------------------------
if ! $MM env list 2>/dev/null | grep -qE "(^|[ /])$GDALIC_ENV([ /]|$)"; then
  echo "==> creating env '$GDALIC_ENV' from env/environment.yml"
  if [ "$MM" = "conda" ]; then $MM env create -n "$GDALIC_ENV" -f env/environment.yml
  else $MM create -y -n "$GDALIC_ENV" -f env/environment.yml; fi
else
  echo "==> env '$GDALIC_ENV' already exists (skip create)"
fi

# 2. GDAL PR checkout -------------------------------------------------------
if [ ! -d gdal/.git ]; then
  echo "==> cloning $GDAL_REMOTE -> gdal/"
  git clone "$GDAL_REMOTE" gdal
fi
( cd gdal && git fetch --quiet origin "$GDAL_BRANCH" 2>/dev/null || true
  git checkout --quiet "$GDAL_SHA" 2>/dev/null || git checkout --quiet "$GDAL_BRANCH" )
echo "    GDAL at $(cd gdal && git rev-parse --short HEAD) (branch $GDAL_BRANCH)"

# 3. configure + build + install (inside env -> conda CC/CXX/flags) ---------
if [ ! -x "install/bin/gdalinfo" ]; then
  echo "==> configuring + building GDAL with the Icechunk driver (takes 10-30 min)"
  $MM run -n "$GDALIC_ENV" cmake -S gdal -B build -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DENABLE_DRIVER_Icechunk=ON \
    -DBUILD_PYTHON_BINDINGS=ON \
    -DCMAKE_INSTALL_PREFIX="$REPO_ROOT/install"
  $MM run -n "$GDALIC_ENV" cmake --build build -j "$JOBS"
  $MM run -n "$GDALIC_ENV" cmake --install build
else
  echo "==> install/ already built (skip; rm -rf build install to force)"
fi

# 4. smoke check ------------------------------------------------------------
echo "==> verifying the Icechunk driver registers"
# shellcheck disable=SC1091
source scripts/env.sh
gdalinfo --formats 2>/dev/null | grep -i icechunk || { echo "FAIL: Icechunk driver not registered"; exit 1; }
"$ICPY" -c "from osgeo import gdal; assert gdal.GetDriverByName('Icechunk'); print('OK: osgeo.gdal sees Icechunk', gdal.__version__)"

# 5. optional: rasterio + rioxarray from source vs THIS gdal (for the demo) --
if [ "$WITH_RASTERIO" = "1" ]; then
  echo "==> building rasterio + rioxarray against the custom GDAL (optional; for §C8 demo)"
  GDAL_CONFIG="$GDAL_INSTALL/bin/gdal-config" \
    "$CONDA_PREFIX_GDALIC/bin/pip" install --no-build-isolation --no-binary rasterio \
      "rasterio==1.5.0" "rioxarray==0.22.0" affine click cligj \
    || echo "    (rasterio/rioxarray build failed -- not required for core QC; demo will skip)"
fi

cat <<EOF

==> setup complete.
    source scripts/env.sh        # activate the built GDAL for this shell
    bash run_all.sh              # regenerate fixtures + run every experiment
    open QC_REPORT.md            # the findings
EOF
