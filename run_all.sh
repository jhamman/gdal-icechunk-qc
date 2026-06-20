#!/usr/bin/env bash
# Reproduce every QC experiment. Assumes `bash setup.sh` has built GDAL.
#   source scripts/env.sh && bash run_all.sh
# Knobs:
#   SKIP_SLOW=1   skip the ~8-min B2 bisection and the leak-trajectory bits
#   SKIP_LIVE=1   skip live S3 cross-validation (no network / offline runs)
set -uo pipefail
cd "$(dirname "$0")"
# shellcheck disable=SC1091
source scripts/env.sh
export RUST_LOG=off
SKIP_SLOW="${SKIP_SLOW:-0}"; SKIP_LIVE="${SKIP_LIVE:-0}"

banner(){ printf '\n========== %s ==========\n' "$1"; }

banner "synthetic fixtures"
"$ICPY" scripts/make_synthetic.py

banner "bundled GDAL autotest (74 cases)"
( cd gdal/autotest && "$ICPY" -m pytest gdrivers/icechunk_driver.py -q ) || true

banner "offline synthetic checks (incl. B1 branch/tag repro)"
bash scripts/run_synthetic_checks.sh || true

banner "ROUND 2 - virtual-chunk integrity (B3)"
"$ICPY" scripts/test_virtual_chunks.py || true

banner "ROUND 2 - virtual-chunk checksum enforcement (S1)"
"$ICPY" scripts/test_checksum_s1.py || true

banner "ROUND 2 - property test (200 trials vs zarr-python)"
"$ICPY" scripts/property_test.py --trials 200 || true

banner "ROUND 2 - concurrency + memory leak"
"$ICPY" scripts/test_concurrency_leak.py || true

banner "ROUND 2 - fault injection (HTTP-semantic)"
"$ICPY" scripts/fault_injection.py || true

banner "ROUND 2 - fault injection (toxiproxy transport)"
if command -v toxiproxy-server >/dev/null 2>&1; then
  curl -s http://127.0.0.1:8474/version >/dev/null 2>&1 || { toxiproxy-server >/tmp/toxiproxy.log 2>&1 & sleep 2; }
  "$ICPY" scripts/fault_injection_toxiproxy.py || true
else
  echo "skip: toxiproxy-server not installed (brew install toxiproxy)"
fi

if [ "$SKIP_SLOW" != "1" ]; then
  banner "ROUND 2 - B2 manifest threshold bisection (~8 min)"
  "$ICPY" scripts/bisect_b2.py || true
else
  echo; echo "skip: B2 bisection (SKIP_SLOW=1)"
fi

if [ "$SKIP_LIVE" != "1" ]; then
  banner "LIVE cross-validation vs icechunk+xarray (needs network)"
  "$ICPY" scripts/compare.py --bucket icechunk-public-data --prefix v1/glad --region us-east-1 || true
  "$ICPY" scripts/compare.py --bucket dynamical-noaa-gfs --prefix noaa-gfs-forecast/v0.2.7.icechunk --region us-west-2 || true
  "$ICPY" scripts/compare.py --bucket nasa-waterinsight \
        --prefix virtual-zarr-store/icechunk/RASI/HISTORICAL --region us-west-2 \
        --virtual-anon-prefix s3://nasa-waterinsight/RASI/ || true
else
  echo; echo "skip: live cross-validation (SKIP_LIVE=1)"
fi

banner "demos + light benchmark"
"$ICPY" scripts/demo_rasterio_rioxarray.py || true
bash scripts/run_bench.sh || true

banner "done"
echo "Findings: QC_REPORT.md   |   raw logs + notes: qc/"
