#!/usr/bin/env bash
# Run the ASan+UBSan-instrumented GDAL over Icechunk fixtures and capture any
# sanitizer diagnostics. Drives the CLI (the ASan build has no Python bindings).
# Exercises: FlatBuffers verify/parse, zstd manifest decompress, inline/native/
# virtual chunk byte-range math, AND the error paths (corrupt/unsupported fixtures)
# where memory bugs in cleanup code tend to hide.
set -u
cd "$(dirname "$0")/.."
source scripts/env.sh

AB=build-asan
CLANGLIB=$CONDA_PREFIX_GDALIC/lib/clang/19/lib/darwin
# NB: do NOT put conda/lib on DYLD_LIBRARY_PATH -- it shadows /usr/lib/libiconv (which
# exports _iconv that the ASan libgdal needs) with conda's libiconv (only _libiconv).
# Conda deps resolve via the binary's baked @rpath (which includes conda/lib).
# Force-load the ASan runtime first (canonical macOS fix for "loaded too late"/hang).
export DYLD_LIBRARY_PATH="$PWD/$AB:$CLANGLIB"   # deliberately NOT conda/lib (see note above)
export DYLD_INSERT_LIBRARIES="$CLANGLIB/libclang_rt.asan_osx_dynamic.dylib"
export GDAL_DATA="$GDAL_INSTALL/share/gdal"
# macOS LeakSanitizer is unsupported -> detect_leaks=0. Report (don't swallow) UB.
export ASAN_OPTIONS="detect_leaks=0:abort_on_error=1:print_summary=1"
export UBSAN_OPTIONS="print_stacktrace=1:halt_on_error=0:report_error_type=1"
export RUST_LOG=off
export AWS_NO_SIGN_REQUEST=YES
export ICECHUNK_ALLOW_LOCAL_CHUNK_LOCATION=YES

MDINFO=$AB/apps/gdalmdiminfo
MDTRANS=$AB/apps/gdalmdimtranslate
SCRATCH=/private/tmp/claude-501/-Users-jhamman-workdir-gdal-icechunk-qc/dd6a230e-76fe-4109-b89c-82a712863245/scratchpad/asan_out
rm -rf "$SCRATCH"; mkdir -p "$SCRATCH"
LOG=qc/asan_run.log
: > "$LOG"

san_hits=0
run() {  # label  cmd...
  local label="$1"; shift
  echo "### $label" >> "$LOG"
  echo "    \$ $*" >> "$LOG"
  "$@" >> "$LOG" 2>&1
  local rc=$?
  # surface sanitizer diagnostics
  if grep -qE "runtime error:|AddressSanitizer|UndefinedBehaviorSanitizer|SUMMARY: .*Sanitizer|heap-buffer-overflow|stack-buffer-overflow|use-after" "$LOG"; then
    :
  fi
  echo "    -> exit $rc" >> "$LOG"
  echo "" >> "$LOG"
  printf "  %-44s exit=%s\n" "$label" "$rc"
}

echo "== POSITIVE: structure (FlatBuffers parse) =="
for f in native_inline hierarchy scalar multi_ref; do
  run "info $f" $MDINFO "ICECHUNK:fixtures/$f"
done
run "info multi_ref?tag=v1" $MDINFO "ICECHUNK:fixtures/multi_ref?tag=v1"
run "info multi_ref?branch=dev" $MDINFO "ICECHUNK:fixtures/multi_ref?branch=dev"

echo "== POSITIVE: decode (chunk byte-range + zstd) =="
run "translate native_inline/temperature" $MDTRANS "ICECHUNK:fixtures/native_inline" "$SCRATCH/t1.zarr" -array temperature -of Zarr
run "translate native_inline/native_grid" $MDTRANS "ICECHUNK:fixtures/native_inline" "$SCRATCH/t2.zarr" -array native_grid -of Zarr
run "translate hierarchy/group_a/cube"    $MDTRANS "ICECHUNK:fixtures/hierarchy"     "$SCRATCH/t3.zarr" -array /group_a/cube -of Zarr

echo "== VIRTUAL: file:// (silent-fill path) + s3 404 =="
[ -d fixtures/vc_local/vstore ] && run "translate vc_local (file://)" $MDTRANS "ICECHUNK:fixtures/vc_local/vstore" "$SCRATCH/v1.zarr" -array v -of Zarr

echo "== NEGATIVE: error/cleanup paths =="
run "info corrupt_manifest"           $MDINFO "ICECHUNK:fixtures/corrupt_manifest"
run "translate corrupt_manifest"      $MDTRANS "ICECHUNK:fixtures/corrupt_manifest" "$SCRATCH/c.zarr" -array native_grid -of Zarr
run "info unsupported_codec"          $MDINFO "ICECHUNK:fixtures/unsupported_codec"
run "info bogus-path"                 $MDINFO "ICECHUNK:fixtures/does_not_exist"

echo "== REMOTE: one GFS corner (vsis3 + manifest under ASan) =="
GFS="ICECHUNK:/vsis3/dynamical-noaa-gfs/noaa-gfs-forecast/v0.2.7.icechunk"
AWS_REGION=us-west-2 run "translate GFS temperature_2m corner" $MDTRANS "$GFS" "$SCRATCH/gfs.zarr" \
  -array temperature_2m -of Zarr 2>/dev/null || true

echo ""
echo "==== sanitizer diagnostics found in $LOG ===="
grep -nE "runtime error:|ERROR: AddressSanitizer|UndefinedBehaviorSanitizer|heap-buffer-overflow|stack-buffer-overflow|use-after-(free|return)|SUMMARY: .*Sanitizer" "$LOG" \
  && echo "(^ sanitizer hits above)" \
  || echo "NONE — no ASan/UBSan diagnostics across all runs."
