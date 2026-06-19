#!/usr/bin/env bash
# Light performance comparison: GDAL Icechunk driver vs icechunk+zarr-python vs icechunk+xarray.
# Each backend runs in a fresh process (cold open). Local test isolates open+decode (no network);
# remote test (GLAD near-origin) is a realistic anonymous-S3 cloud read.
set -u
cd "$(dirname "$0")/.."
source scripts/env.sh
export RUST_LOG=error   # quiet icechunk tracing so it doesn't pollute stdout
PY="$ICPY"
OUT=qc/bench_results.jsonl
: > "$OUT"

# ---- build local bench fixture (64 MB float32, chunks 1000x1000 zstd, 16 chunks) ----
if [ ! -d fixtures/bench_native ]; then
  echo "building fixtures/bench_native ..."
  $PY - <<'EOF'
import numpy as np, icechunk as ic, zarr
from zarr.codecs import ZstdCodec
repo = ic.Repository.create(ic.local_filesystem_storage("fixtures/bench_native"))
s = repo.writable_session("main")
g = zarr.group(s.store)
rng = np.random.default_rng(0)
data = rng.random((4000,4000), dtype="float32")     # ~incompressible -> real decode work
a = g.create_array("data", shape=(4000,4000), dtype="float32",
                   chunks=(1000,1000), compressors=[ZstdCodec(level=3)],
                   dimension_names=["y","x"])
a[:] = data
s.commit("bench fixture")
print("bench fixture committed: 4000x4000 f32, 16 zstd chunks")
EOF
fi

run() {  # $1=backend  rest=args
  local be="$1"; shift
  $PY scripts/bench.py --backend "$be" "$@" --reps 4 2>/dev/null >> "$OUT" || \
    echo "{\"backend\":\"$be\",\"error\":true}" >> "$OUT"
}

echo "== LOCAL fixtures/bench_native  (data[:, :] = 64 MB, no network) =="
for be in gdal zarr xarray; do run "$be" --local fixtures/bench_native --var data --slice ":,:"; done

# Remote read on a DENSE real field. One native chunk of GFS temperature_2m
# (block [1,105,121,121] -> ~24.6 MB) so all backends fetch+decode the same chunk.
# (GLAD near-origin is sparse/fill, so it does not measure real transfer.)
echo "== REMOTE GFS temperature_2m[0, 0:105, 0:121, 0:121] = 1 chunk ~24.6 MB, anonymous S3 us-west-2 =="
echo "   (warming the S3 chunk once so server-side caching is equal for all backends)"
GFS=dynamical-noaa-gfs; PFX=noaa-gfs-forecast/v0.2.7.icechunk; SL="0,0:105,0:121,0:121"
AWS_REGION=us-west-2 $PY scripts/bench.py --backend zarr --bucket $GFS \
   --prefix "$PFX" --region us-west-2 --var temperature_2m --slice "$SL" --reps 1 >/dev/null 2>&1 || true
for be in gdal zarr xarray; do
  run "$be" --bucket $GFS --prefix "$PFX" --region us-west-2 --var temperature_2m --slice "$SL"
done

echo; echo "== RESULTS =="
$PY - "$OUT" <<'EOF'
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1]) if l.strip().startswith("{")]
def grp(rows, label):
    print(f"\n{label}")
    print(f"  {'backend':8} {'open_s':>8} {'cold_read':>10} {'warm_read':>10} {'MB':>7} {'warm_MB/s':>10}")
    for r in rows:
        if r.get("error"): print(f"  {r['backend']:8}  ERROR"); continue
        print(f"  {r['backend']:8} {r['open_s']:8.3f} {r['cold_read_s']:10.3f} "
              f"{r['warm_read_s']:10.3f} {r['mb']:7.1f} {str(r.get('warm_mbps')):>10}")
grp([r for r in rows if "bench_native" in r.get("target","")], "LOCAL (64 MB, open+decode, no network)")
grp([r for r in rows if "gfs" in r.get("target","")], "REMOTE GFS (1 chunk ~24.6 MB, anonymous S3 us-west-2)")
print("\nNote: cold_read = first in-process read (incl. network/decode); warm_read = median of"
      "\nsubsequent reads (caches warm). Each backend ran in a fresh process (cold open).")
EOF
