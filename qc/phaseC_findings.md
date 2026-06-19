# Phase C — workflow findings (raw)

Build: GDAL 3.14.0dev-8c2b212 (PR #14755), env gdalic (icechunk 2.0.6 spec v2, zarr 3.2.1, xarray 2026.4.0).

## Bundled autotest
`autotest/gdrivers/icechunk_driver.py`: **74 passed, 0 failed** (~1.9s). Covers positive + ~30 negative fixtures (malformed/short snapshot+manifest, invalid zstd, non-increasing ids/indices, path traversal, offline repo, mutually-exclusive refs, etc.).

## *** BLOCKER BUG: branch/tag selection ignored for data/structure ***
Reproduced on synthetic `fixtures/multi_ref` (main=[30,31,32,33], tag v1=[10,11,12,13], tag v2 & branch dev=[20,21,22,23]):

| connection | GDAL reads | xarray ground truth | correct? |
|---|---|---|---|
| `fixtures/multi_ref` (main) | [30,31,32,33] | [30,31,32,33] | ✓ |
| `ICECHUNK:fixtures/multi_ref?tag=v1` | [30,31,32,33] | [10,11,12,13] | ✗ |
| `ICECHUNK:fixtures/multi_ref?tag=v2` | [30,31,32,33] | [20,21,22,23] | ✗ |
| `ICECHUNK:fixtures/multi_ref?branch=dev` | [30,31,32,33] | [20,21,22,23] | ✗ |

The `/vsiicechunk/` VSI layer DOES honor the ref when present in the path:
- `ZARR:"/vsiicechunk/{fixtures/multi_ref?tag=v1}"` → [10,11,12,13] ✓
- `ZARR:"/vsiicechunk/{fixtures/multi_ref?tag=v2}"` → [20,21,22,23] ✓
- `ZARR:"/vsiicechunk/{fixtures/multi_ref?branch=dev}"` → [20,21,22,23] ✓

**Root cause:** `frmts/icechunk/icechunkdriver.cpp:217-218` builds the delegated dataset name as
`ZARR:"/vsiicechunk/{" + osFilename + "}"` where `osFilename` is the **ref-stripped** path
(`GetFilenameFromDatasetName` does `osFilename.resize(nQuestionMarkPos)`). The `?branch=`/`?tag=`
suffix is therefore never passed to the VSI layer, which then defaults to `main`
(`vsiicechunk.cpp:194`). The snapshot resolved earlier in `DatasetOpen` (used to pick the default
branch and to validate the ref) is then discarded.

**Impact:** A user opening `ICECHUNK:<repo>?tag=<t>` or `?branch=<non-main>` via the documented
syntax silently receives `main`'s metadata AND chunk data — no error, no warning. Affects both
structure (`gdal mdim info`) and values. list-branches / list-tags are unaffected (correct).

**Suggested fix:** propagate the ref into the delegated path, e.g. append
`?branch=<osBranchName>` / `?tag=<osTagName>` (both already resolved in `DatasetOpen`, with
`osBranchName` defaulted to `main`) to the string used for `/vsiicechunk/{...}`. The VSI layer
already parses and honors it. A regression test should assert non-main DATA values via the
`ICECHUNK:` connection (the bundled suite only checks list-branches/list-tags + open, not
cross-ref data).

## Negative cases — graceful, no crash (confirmed)
- Unsupported codec (`numcodecs.zlib`): `ERROR 6: Unsupported codec: numcodecs.zlib`; `ReadAsArray`
  raises `RuntimeError`; process survives. NOTE: `gdal mdim info` then reports `"arrays": {}` — the
  undecodable array is **omitted from the listing** (Zarr-driver behavior); an error is printed to
  stderr but a casual lister may not notice the variable is missing.
- Corrupt/truncated manifest (native chunks): `RuntimeError: <manifest>: too small file`; process
  survives. (Manifest corruption is also covered extensively by the bundled suite.)

## *** BLOCKER/HIGH BUG: large manifests fail FlatBuffers verification ***
GLAD `lclu` (5×560000×1440000 uint8). Reading near-origin succeeds, but a windowed read at
(year0, y=280000, x=720000) fails 3/3 with:
`ERROR 1: /vsis3/icechunk-public-data/v1/glad/manifests/8MKBNVZE7V8F7DNNDFTG: invalid Manifest Flatbuffer`
- icechunk+xarray reads the SAME region fine (values 23–24) → the manifest is valid.
- That manifest: 39.7 MB on disk (zstd), header spec_version=1 file_type=2 compression=1,
  **decompresses to a 94.1 MB FlatBuffer**.
- `icechunkmanifest.cpp:99` constructs `flatbuffers::Verifier verifier(buffer.get(), size)` with
  DEFAULT options. FlatBuffers default `Verifier::Options::max_tables == 1,000,000`. A 94 MB manifest
  holds >1e6 chunk-ref tables → `VerifyManifestBuffer` returns false → "invalid Manifest Flatbuffer".
- Same pattern applies to the snapshot/repo verifiers for very large repos.
**Impact:** GDAL cannot read large regions of large real datasets (flagship GLAD `lclu`), with a
confusing error. **Fix:** pass `flatbuffers::Verifier::Options` with a much larger `max_tables`
(and `max_size` if needed) to all three verifiers. Regression test: a fixture/synthetic manifest
with >1e6 refs, or document the read of a large GLAD region.

## GFS forecast (dynamical, native, us-west-2)
`noaa-gfs-forecast/v0.2.7.icechunk`. 29 variables; all 25 large 4-D fields
(7499×209×721×1440: temperature_2m, wind_*, precipitation_surface, pressure_*, etc.) sampled-corner
values MATCH xarray exactly. The only "mismatches" are CF-encoded coordinates init_time, valid_time,
lead_time, expected_forecast_length — xarray applies CF datetime/timedelta decoding; GDAL returns the
raw stored integers. NOT a driver bug (GDAL Zarr returns raw values; CF decoding is the caller's job).
Reads used near-origin corners → small manifests → no large-manifest failure here.

## rasterio / rioxarray demo (built rasterio 1.5.0 + rioxarray 0.22.0 vs custom GDAL)
- Local synthetic: rasterio.open(repo).subdatasets → open `:/temperature` subdataset (driver=Zarr,
  count=1, 6×6, float64) → read(1) correct; rioxarray.open_rasterio gives (band,y,x) DataArray, values correct.
- Remote GLAD: rasterio exposes `lclu` as 5 bands, 1.44M×560k, uint8, **crs=EPSG:4326, geotransform
  Affine(0.00025,...,-180, ... -0.00025, 80)** — georeferencing propagates! BUT the windowed read at
  the large-manifest region fails (same B2 bug). Near-origin windows work.

## Passing cross-validations (GDAL multidim vs icechunk+xarray)
- `scalar`: crs + scalar (0-d) values agree. ✓
- `hierarchy`: root `x` agrees; nested `group_a/cube` is exposed by GDAL but not surfaced by
  `xr.open_zarr` (xarray opens only the root group) — GDAL behavior correct, xarray scoping artifact.
- `multi_ref` (main): agrees. ✓

## Tolerance / interop notes (not bugs)
- GDAL reads Zarr-v3 arrays that LACK `dimension_names` (synthesizes `dim0/dim1`); xarray REFUSES
  such arrays. So GDAL is more permissive. (Real datasets carry `dimension_names`.)
- `gdal mdim info` maps a Zarr `units` attr → `unit`, and fill_value → `nodata_value`.
