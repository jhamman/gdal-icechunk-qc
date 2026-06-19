# CLAUDE.md — working in this repo

QC harness for **OSGeo/gdal PR #14755** (Icechunk read-only driver). The deliverable is
[`QC_REPORT.md`](QC_REPORT.md); this file is the operational guide for extending the work.
Read `QC_REPORT.md` §0 + §6 and `qc/phaseE_deep_qc_findings.md` before adding findings.

## Environment model (read this first)

- **Always** `source scripts/env.sh` before running anything. It puts the freshly-built GDAL
  (`install/`) on `PATH`, sets `DYLD_LIBRARY_PATH`/`PYTHONPATH`, and exports **`$ICPY`** — the
  env's python with `osgeo` importable.
- **Run python as `$ICPY script.py`, NOT `micromamba run -n gdalic python`.** `micromamba run`
  re-pollutes `DYLD_LIBRARY_PATH` with `conda/lib` and breaks things (a `_iconv` clash with conda's
  `libiconv`). `$ICPY` + the sourced env avoids it.
- **`env.sh` deliberately keeps `conda/lib` OFF `DYLD_LIBRARY_PATH` on macOS** (it's on
  `LD_LIBRARY_PATH` for Linux only). conda's `libiconv` exports `_libiconv` not `_iconv`, so putting
  it on macOS `DYLD_LIBRARY_PATH` shadows the system `libiconv` and **crashes any child `bash`** the
  scripts spawn (and the experiment that spawns it then silently no-ops under `|| true`). libgdal's
  conda deps resolve via its baked `@rpath`, so DYLD only needs `install/lib`. Don't "helpfully"
  re-add `conda/lib` to DYLD.
- **`export RUST_LOG=off`** in anything that creates icechunk repos, or stdout/stderr fills with
  `WARN icechunk_arrow_object_store … not safe for concurrent commits`. Most scripts already do.
- Build steps (cmake/ninja) DO use `micromamba run -n gdalic …` — that's correct (it sets conda
  `CC`/`CXX`). The DYLD issue only affects *running* the built libgdal, not building it.

## Conventions for experiments

- New experiment → `scripts/<name>.py` (or `.sh`), add it to `run_all.sh`, write results to
  `qc/<name>_results.txt`, and add a `§6.x` subsection to `QC_REPORT.md`. Keep the docstring
  explaining *what blind spot it closes* and *what counts as a defect*.
- **The acceptance bar is the reference stack.** Cross-check every read against
  `icechunk`+`zarr-python` (or `xarray`); never trust GDAL's output alone. Classify outcomes as
  `OK_CORRECT` / `LOUD_ERROR` / `SILENT_FILL` / `SILENT_PARTIAL/CORRUPT` — silent wrong data is
  the thing we hunt.
- Fixtures live in `fixtures/` and are **generated, git-ignored, and disposable**. Regenerate via
  `make_synthetic.py` and the test scripts. Use seeded RNG so failures reproduce.
- **Do not name a repository directory `repo`** — the driver mis-resolves `ICECHUNK:.../repo` as
  the repo *file* (finding §2.13). Use `vstore`, `store`, etc.

## Things that bite (learned the hard way)

- **Silent fill vs error.** A virtual chunk that 404s reads as **zeros, no error** (B3). When
  testing read paths, always compare values to a known truth — a passing "READ_OK" can be wrong.
- **ASan/UBSan build** is in `build-asan/` (`-fsanitize=address,undefined`, Python off). To run it:
  drop `conda/lib` from `DYLD_LIBRARY_PATH` (it shadows system `libiconv` which has `_iconv`) and
  force-load the runtime: `DYLD_INSERT_LIBRARIES=$CONDA_PREFIX_GDALIC/lib/clang/*/lib/darwin/libclang_rt.asan_osx_dynamic.dylib`.
  See `scripts/run_asan_checks.sh`. **Caveat:** on this macOS-arm64 + conda-clang-19 toolchain the
  instrumented binary is pathologically slow (minutes of 99% CPU on a trivial open) and did not
  complete (§6.6) — re-run on Linux/system-clang to actually close it. Don't burn time re-trying it here.
- **icechunk URL normalization** strips a custom port from `http://host:PORT/...` virtual-chunk
  locations, breaking container matching. Use the **S3 path** instead: `s3_store(endpoint_url=…,
  force_path_style=True, allow_http=True, anonymous=True)` + GDAL `AWS_S3_ENDPOINT`/`AWS_VIRTUAL_HOSTING=FALSE`/`AWS_HTTPS=NO`.
  This is how `fault_injection*.py` point GDAL at a local mock.
- **GDAL over-fetches**: a chunk read issues a Range like `bytes=0-16383` (16 KB block) even for a
  64-byte object — clamp ranges to the real object size in any mock server.
- Foreground `sleep` is blocked by the harness; poll via background tasks or short python sleeps.

## Connection syntax (driver)

- `ICECHUNK:<path>` where `<path>` is local, `/vsis3/…`, `/vsicurl/…`, etc.
- `?branch=<name>` / `?tag=<name>` — **but data selection is broken (B1)**; only listing is correct.
- Low-level: `/vsiicechunk/{<path>}` serves the repo as a Zarr-v3 hierarchy to the Zarr driver.
- Multidim-only: `gdal.OpenEx(conn, gdal.OF_MULTIDIM_RASTER)`; classic raster exposes arrays as
  **subdatasets** (rasterio reaches them that way — see `demo_rasterio_rioxarray.py`).

## Map

- `QC_REPORT.md` — findings (§0 verdict, §2 code review, §3 spec matrix, §4 workflows, §5 TODOs, §6 deep QC).
- `qc/phaseA_…`, `phaseC_…`, `phaseE_deep_qc_findings.md` — raw per-phase notes.
- `qc/*.txt` / `*.jsonl` — raw experiment output (evidence).
- `scripts/env.sh` — the only thing you must source. Driver source is under `gdal/frmts/icechunk/`
  (after `setup.sh`).
