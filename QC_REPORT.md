# QC Report — GDAL Icechunk Read-Only Driver (OSGeo/gdal PR #14755)

**Subject:** OSGeo/gdal PR [#14755](https://github.com/OSGeo/gdal/pull/14755) — "Add Icechunk read-only driver" by Even Rouault.
**Reviewed at:** `rouault/gdal@icechunk`, head SHA `8c2b212`, target GDAL 3.14.
**Date:** 2026-06-18. **Reviewer:** Earthmover QC (joe@earthmover.io).

---

## 0. Summary verdict

The driver is **well-engineered, defensively coded, and works correctly on real public Icechunk data** for the cases that matter most: native chunks (GLAD), virtual chunks (RASI), the v1 `refs/` and v2 `repo` layouts, anonymous S3, branch/tag *listing*, and a large multidimensional read path delegated to GDAL's Zarr driver. The bundled test suite passes (**74/74**), and independent cross-validation against `icechunk`+`xarray` ground truth agrees to the bit on sampled values.

**Three blockers found** (all three produce *silent wrong data* or block sample datasets):

- **B1 — branch/tag data selection.** Opening a repository with `?branch=<non-main>` or `?tag=<name>` via the documented `ICECHUNK:` connection **silently returns `main`'s data and structure** — the ref is dropped before the driver delegates to the VSI layer. No error is raised. Version-controlled access (a headline Icechunk feature) is unsafe through the driver, even though `list-branches`/`list-tags` and the underlying `/vsiicechunk/` layer are correct.
- **B2 — large manifests rejected.** Reading large regions of large arrays fails with "invalid Manifest Flatbuffer". Root cause: the manifest FlatBuffers `Verifier` uses **default `max_tables` (1,000,000)**; GLAD's `lclu` has a 94 MB manifest with >1e6 chunk-ref tables. `icechunk` reads the same region fine. GDAL can therefore read only the portions of GLAD (a sample public dataset) backed by small-enough manifests. **Round 2 bisected the threshold to exactly 1,000,000 chunk refs per manifest** (a 6.6 MB manifest already fails — it is the table *count*, not size; §6.1).
- **B3 — inaccessible virtual chunk silently reads as fill (NEW, round 2).** A manifest virtual-chunk entry *asserts* a backing object exists. When that object is unreadable — a missing/moved S3 object (clean 404), or a private object the user lacks credentials for — GDAL returns the **array fill value (zeros) with no warning or error**, because the virtual ref is delegated to the Zarr layer as a chunk key and a 404 is indistinguishable from a legitimately-absent (sparse) chunk. The reference stack (icechunk+zarr) **raises** `IcechunkError: error fetching virtual reference` on the identical case. This is the most dangerous failure mode for the virtual-chunk + credentials use case: wrong/missing creds → a dataset of plausible zeros, silently. (§6.2)

A fourth, subtler item: the driver **does not validate virtual-chunk `etag`/`last_modified` checksums** in release builds, so it will read virtual chunks that the official `icechunk` client refuses as stale (observed live on RASI). In our test the bytes were in fact correct, but the driver cannot *detect* genuine source drift.

Recommendation: **not ready to merge as-is** due to B1, B2, and B3. B1/B2 are small/localized fixes; B3 needs the driver to treat a manifest-referenced-but-unreadable chunk as an error rather than a missing chunk. Everything else is either green or a documented limitation/enhancement.

**Round-2 deep QC (§6)** additionally: closed the biggest coverage gaps with **200/200 property-test trials** (12 dtypes incl. complex, 4 codecs, sparse, partial-edge chunks, random sub-windows — zero mismatches vs zarr-python); confirmed **clean concurrency** (320 concurrent reads, no crash/leak/fd-growth); quantified a small **per-open memory leak** (~1.5–2 KB/open, driver) atop a larger per-read leak that is **shared with the plain Zarr driver** (pre-existing GDAL, not introduced by this PR); and **resolved the msgpack concern** (no public msgpack-era repos exist; the oldest PyPI icechunk already writes FlatBuffers v1, which reads correctly).

| Area | Result |
|---|---|
| Build from source (driver + Zarr + Python bindings) | ✅ GDAL 3.14.0dev-8c2b212, driver `Icechunk (rov)` |
| Bundled autotest `icechunk_driver.py` | ✅ 74 passed, 0 failed |
| Native chunks, real data (GLAD near-origin; GFS forecast 25 vars) | ✅ structure + values match xarray |
| Large manifests (GLAD `lclu` full extent) | ❌ **BLOCKER B2** — "invalid Manifest Flatbuffer" (Verifier `max_tables`) |
| Virtual chunks, real data (RASI) | ✅ GDAL decodes correctly (matches source bytes) |
| Virtual chunk **unreadable** (404 / missing creds) | ❌ **BLOCKER B3** — silent fill (zeros), no error; reference raises (§6.2) |
| Virtual-chunk fault matrix (5xx, truncate, cut, reset, timeout) | ✅ all error loudly & match reference; only 404 silently fills (§6.9) |
| Branch/tag **data** selection | ❌ **BLOCKER B1** — returns `main` regardless |
| Branch/tag listing (`list-branches`/`list-tags`) | ✅ correct |
| Unsupported codec / corrupt / malformed input | ✅ graceful error, no crash (200+ negative/random cases, §6.3) |
| Property test: dtypes×codecs×shapes vs zarr-python | ✅ **200/200**, full-array + sub-window, 0 mismatch (§6.3) |
| Concurrency (8 threads, separate handles) | ✅ 320 concurrent reads, 0 errors, no fd leak (§6.4) |
| Memory: per-open / per-read leak | ⚠️ ~1.5–2 KB/open (driver); ~7.6 KB/read shared w/ plain Zarr, **not PR-introduced** (§6.5) |
| Memory-safety (ASan/UBSan) | ⚠️ built & runs but instrumented exec pathologically slow on this toolchain — no conclusive pass (§6.6) |
| Oldest PyPI icechunk (0.2.0, spec v1) | ✅ reads correctly; msgpack era predates PyPI (§6.7) |
| Virtual-chunk checksum validation | ⚠️ not done in release builds |
| rasterio / rioxarray | ✅ works via subdatasets (georeferencing propagates); demo in §C8 |
| Performance vs reference stack | ✅ competitive — ~25% faster local decode, lowest open latency (§C9) |

---

## 1. Environment

Built and tested on macOS arm64 (Darwin 25.5), 10 cores.

| Component | Version / detail |
|---|---|
| GDAL | 3.14.0dev-8c2b212c0e (PR #14755, this build) |
| Build | CMake/Ninja, Release; `-DGDAL_ENABLE_DRIVER_ZARR=ON -DGDAL_ENABLE_DRIVER_ICECHUNK=ON -DBUILD_PYTHON_BINDINGS=ON`; optional drivers off |
| Deps | conda-forge: zstd, blosc, c-blosc2, lz4, libcurl, proj, libtiff, swig; vendored flatbuffers |
| Ground truth | icechunk 2.0.6 (spec **v2**), zarr 3.2.1, xarray 2026.4.0, numpy 2.4.6, python 3.12 |
| Driver capabilities advertised | `DCAP_RASTER=YES`, `DCAP_MULTIDIM_RASTER=YES`, `DCAP_VIRTUALIO=YES`, read-only (`rov`) |
| Round-2 sanitizer build | `build-asan/` — same sources, `-fsanitize=address,undefined`, RelWithDebInfo, Python off (§6.6) |
| Round-2 old-format check | isolated venv with icechunk 0.2.0 (oldest on PyPI; spec v1) (§6.7) |

Reproduce: `source scripts/env.sh` then the scripts under `scripts/` (see §C and §6). All artifacts are in this directory.

---

## 2. (a) Code review

Driver lives in `frmts/icechunk/` (~3,705 LOC). It is **read-only** and **multidimensional-first**.

### Architecture (clean and sensible)
The driver does **not** decode Zarr itself. On open (`icechunkdriver.cpp` `DatasetOpen`) it parses the connection string, resolves repo → ref → snapshot (to validate and pick the default branch), then rewrites the dataset name to `ZARR:"/vsiicechunk/{<path>}"` and **delegates to the Zarr driver**. A custom VSI filesystem `/vsiicechunk/` (`vsiicechunk.cpp`) presents the repo as a Zarr-v3 hierarchy: it serves `zarr.json` from the snapshot node `user_data`, and serves chunk keys by mapping them to:
- **inline** → bytes copied from the manifest into an in-memory handle,
- **native** (`chunks/`) and **virtual** → `/vsisubfile/<offset>_<length>,<vsi-url>` range reads.

Consequence: **codec/dtype/consolidated-metadata/sharding support == GDAL Zarr-driver support**; the Icechunk code only does ref/snapshot/manifest/chunk-location resolution and byte serving. This is a strong design choice — minimal surface, reuses Zarr's mature codec pipeline.

### Strengths
- **Extensive input validation** matching ~30 negative test fixtures: FlatBuffers verifiers on repo/snapshot/manifest; id-matches-filename checks; manifest file-size & chunk-ref-count cross-checks vs the snapshot; sorted-order enforcement; offset/length overflow guards; mutually-exclusive chunk-ref kinds.
- **Path-traversal hardening** (`icechunksnapshot.cpp:177-197`): node paths must start `/`, not end `/`, and `\`, `/./`, `/../`, trailing `/..` are rejected — explicitly tied to `/vsiicechunk/` safety.
- **Anti-SSRF**: virtual-chunk locations that are local file paths are **disabled by default** (`vsiicechunk.cpp:362-378`); requires `ICECHUNK_ALLOW_LOCAL_CHUNK_LOCATION=YES`.
- **Both repo layouts**: v2 single `repo` FlatBuffer (refs embedded) and v1 `refs/` dir (`branch.*/ref.json`, `tag.*/ref.json`). Handles `status==Offline`, duplicate branch/tag names, invalid snapshot indices.
- **Virtual-chunk auto-config** (`icechunkrepo.cpp::ProcessConfig`): reads the repo's stored `virtual_chunk_containers` and sets `AWS_DEFAULT_REGION` / `AWS_NO_SIGN_REQUEST` (s3) and `GS_NO_SIGN_REQUEST` (gcs) per path prefix.
- Node re-sorting to normalize the spec sort-order confusion (refs earth-mover/icechunk#2183).

### Findings (correctness)
1. **[BLOCKER] `?branch=`/`?tag=` ignored for data & structure.** `DatasetOpen` builds the delegated path from the ref-**stripped** `osFilename` (`icechunkdriver.cpp:217-218`); `GetFilenameFromDatasetName` does `osFilename.resize(nQuestionMarkPos)`. The `/vsiicechunk/{...}` path therefore has no ref, and `vsiicechunk.cpp` `SplitFilename` defaults to `main` (line 194). The earlier-resolved snapshot is discarded. Result: every non-`main` branch and every tag silently returns `main`. Confirmed live (§C2). **Fix:** append the resolved `?branch=<osBranchName>`/`?tag=<osTagName>` to the delegated path (both are already computed; the VSI layer already parses and honors them — proven in §C2).

2. **[BLOCKER] Large manifests rejected by the FlatBuffers verifier.** `icechunkmanifest.cpp:99` builds `flatbuffers::Verifier verifier(buffer.get(), size)` with **default options**. FlatBuffers' default `Verifier::Options::max_tables` is **1,000,000**. GLAD `lclu`'s manifest `8MKBNVZE7V8F7DNNDFTG` is 39.7 MB on disk and **decompresses to a 94.1 MB FlatBuffer** holding >1e6 chunk-ref tables, so `VerifyManifestBuffer` returns false → `ERROR 1: … invalid Manifest Flatbuffer`. `icechunk`+`xarray` read the same region fine (§C6). The snapshot/repo verifiers have the same exposure for very large repos. **Round 2 bisected the trigger to *exactly* 1,000,000 chunk-ref tables** (990,000 reads fine; 1,000,000 fails) with a manifest of only **6.6 MB** — confirming it is the table count hitting the default `max_tables`, not byte size (§6.1). **Fix:** construct all three verifiers with `flatbuffers::Verifier::Options` raising `max_tables` (and `max_size` if a manifest can exceed ~2 GB decompressed). **Impact:** large regions of large arrays (the core Icechunk use case) are unreadable.

3. **[Data integrity] Virtual-chunk checksums ignored in release builds.** `ChunkRef.checksum_etag` / `checksum_last_modified` are read only under `#ifdef DEBUG` (`icechunkmanifest.cpp:258-265`). Release builds parse-and-discard them, so the driver cannot detect that a virtual source file changed since the manifest was written. Observed live on RASI (§C4): `icechunk` refuses the chunks (checksum mismatch) while GDAL serves them. (In that case the bytes were still correct, verified against source.)

12. **[BLOCKER B3 — silent data corruption] Inaccessible virtual chunk reads as fill, not error.** A manifest virtual-chunk entry asserts a chunk exists at `{location, offset, length}`. The driver maps it to a `/vsiicechunk/` chunk key backed by `/vsisubfile/<off>_<len>,<vsi-url>` and lets the Zarr delegate read it. When the backing object is **unreadable but reported as absent** by the VSI handler — a clean S3 **404** (missing/moved object) or an access-denied that surfaces as not-found, i.e. the *missing-credentials* case — the Zarr layer treats it as a legitimately-absent (sparse) chunk and returns the **fill value with no error or warning**. Reproduced (`scripts/test_virtual_chunks.py`): an S3 virtual ref to a non-existent key returns all-zeros silently, while `icechunk`+`zarr` **raise** `IcechunkError: error fetching virtual reference` on the identical repo. Same silent-fill for `file://` local virtual chunks (the `file://` scheme is absent from the morph table at `vsiicechunk.cpp:342-350` and not stripped, so the `/vsisubfile` target is unopenable). By contrast, transport-level failures (host unreachable) and corrupt *native* chunks **do** error. A full fault matrix (§6.9, HTTP-semantic + toxiproxy transport) **narrows the silent fill to the 404 path specifically**: 5xx, truncated/short streams, mid-flight connection cuts, resets and timeouts all error loudly and match the reference — only the not-found (404) response is silently mapped to fill. **Fix:** when resolving a manifest-referenced virtual chunk, treat a not-found/unreadable backing object as a hard error (stat/verify the range, or a "require referenced chunks" mode), and add `file://` to the morph table. **Impact:** a virtual-chunk repo whose objects were moved/deleted (→404) or live behind credentials the caller lacks (→404-as-absent) reads as plausible zeros — silently. (§6.2, §6.9)

13. **[Minor] Repository directory named `repo` mis-resolved.** `ICECHUNK:<path>` where the repository directory's basename is literally `repo` is treated as the repo *file* rather than a directory containing one (`icechunkrepo.cpp:206`, `CPLGetFilename(...) == "repo"`), yielding a confusing `invalid Repo Flatbuffer`. Real repos aren't named `repo`, so impact is low. (§6.8)

14. **[Minor] Magic signature not validated.** The 12-byte `ICE🧊CHUNK` signature is never checked; the loader keys off the version byte at offset 36 ∈ {1,2} + FlatBuffers verification of the body (`icechunkutils.cpp:88-95`). Robust in practice (FB verify rejects non-icechunk bytes — clobbering the magic alone still reads), but the documented magic is not enforced. This is also what makes pre-FlatBuffers msgpack files reject gracefully (their offset-36 byte isn't 1/2). (§6.7)

### Findings (limitations / lower severity)
4. **No snapshot-id / `as_of` time-travel** open option — only `?branch=`/`?tag=`. The Icechunk spec supports both; a feature gap.
5. **Connection-string parser** (`GetFilenameFromDatasetName`) accepts only a *single* query parameter, no `&`, no URL-decoding; an unrecognized `?foo=` is a hard error.
6. **Virtual-chunk credential model — partial & no secret-credential path.** `ProcessConfig` (`icechunkrepo.cpp:117-190`) reads the repo's *persisted* `virtual_chunk_containers` (v2: a flexbuffers blob inside the `repo` FlatBuffer, `icechunkrepo.cpp:330-349`; v1: `config.yaml`, **not currently read by `OpenV1`**) and applies, via `VSISetPathSpecificOption`, per-container `AWS_DEFAULT_REGION`/`AWS_NO_SIGN_REQUEST` (s3) and `GS_NO_SIGN_REQUEST` (gcs). Gaps confirmed in round 2 (§6.2): (a) **Azure containers get no per-path config** (no `az://` branch — only s3+gcs); (b) requester-pays only **warns**, doesn't set `AWS_REQUEST_PAYER`; (c) **secret credentials are never propagated** — in icechunk they're supplied at runtime via `Repository.open(authorize_virtual_chunk_access=…)` and are not persisted, so the driver cannot obtain them. A user reading a private cross-account virtual store must set GDAL path-specific creds manually — and if they don't, the read does not fail loudly, it **silently fills** (finding #12 / B3). RASI works because its container is declared anonymous+region in the persisted config.
7. **`VSISetPathSpecificOption` mutates process-global VSI state** at open time and is never reverted — possible cross-dataset leakage / thread-safety concern in long-lived processes.
8. **Fixed 1024-byte buffer** for a decompressed virtual-chunk URL (`icechunkmanifest.cpp:174`); URLs whose decompressed form exceeds 1024 bytes fail (cleanly).
9. **Linear scan** in `findManifestIdForChunk` (with a thread-local last-hit cache); the author notes a potential bottleneck for arrays with very many manifests (RTree/KDTree TODO).
10. **Cosmetic:** garbled error string `"cICECHUNK_ALLOW_LOCAL_CHUNK_LOCATION configuration option"` (`vsiicechunk.cpp:375`).
11. **Unsupported arrays vanish from listings.** When the Zarr layer can't decode an array's codec (e.g. `numcodecs.zlib`), `gdal mdim info` shows it under neither `arrays` — an ERROR is printed to stderr but the variable is omitted, not flagged in-band (§C3).

### Format-version scope (important context, not a bug)
The magic is the FlatBuffers-era `ICE🧊CHUNK` header; `DecompressFile` accepts spec-version bytes **1 and 2 only**. "v1"/"v2" = Icechunk spec versions 1 & 2, both FlatBuffers (icechunk 1.x writes v1, 2.x writes v2). The pre-FlatBuffers **msgpack era** has a different/absent magic and is therefore unreadable. All current public datasets we tested are v1/v2; this is unlikely to matter in practice but should be documented.

---

## 3. (b) Spec coverage matrix

Legend: ✅ verified · ⚠️ partial/conditional · ❌ broken · — not supported (by design)

| Feature | Status | Evidence |
|---|---|---|
| FlatBuffers spec v1 (icechunk 1.x) | ✅ | bundled `*_v1` fixtures pass |
| FlatBuffers spec v2 (icechunk 2.x) | ✅ | bundled fixtures + RASI/GLAD live |
| Pre-FlatBuffers msgpack format | — | code: magic/version gate rejects (§2) |
| Repo layout: v2 single `repo` file | ✅ | GLAD (has `repo`) |
| Repo layout: v1 `refs/` dir | ✅ | RASI (`refs/`, `config.yaml`, no `repo`) |
| Default branch = `main` | ✅ | all live datasets |
| Branch/tag **listing** | ✅ | `list-branches`/`list-tags` correct (§C) |
| Branch/tag **data selection** (`?branch=`/`?tag=`) | ❌ | **returns main** (§C2, blocker) |
| Snapshot-id / `as_of` time travel | — | not exposed (§2.3) |
| Inline chunks | ✅ | `scalar`, synthetic, bundled |
| Native chunks (`chunks/`) | ✅ | GLAD `lclu` (near-origin), GFS forecast 25 vars, synthetic `native_grid` |
| Large manifests (>~1e6 chunk refs) | ❌ | GLAD `lclu` 94 MB manifest → "invalid Manifest Flatbuffer" (§2.2, blocker B2) |
| Virtual chunks (external byte-ranges) | ✅ | RASI — decode matches source bytes (§C4) |
| Virtual-chunk URL morph (s3/gs/gcs/az/azure/http/https) | ✅ | `vsiicechunk.cpp`; s3 exercised live |
| Virtual-chunk checksum validation | ❌ | release builds ignore etag/last_modified (§2.3) |
| Virtual container auto-credentials (v2 repo, s3/gcs) | ⚠️ | `ProcessConfig`; not for v1 layout / azure / http |
| Metadata zstd compression + location dictionary | ✅ | code + bundled fixtures |
| Anonymous S3 access | ✅ | all live datasets (`AWS_NO_SIGN_REQUEST`) |
| Storage backends: S3, local | ✅ | live S3 + local fixtures |
| Storage backends: GCS, Azure, HTTP | ⚠️ | morphing present; untested (no public fixture) |
| Zarr v3 codecs (zstd, blosc, gzip, crc32c, sharding, …) | ✅ | via Zarr driver (GLAD zstd; RASI raw bytes) |
| `numcodecs.shuffle` / `numcodecs.zlib` | — | unsupported → graceful ERROR 6 (§C3) |
| Data types, fill values, dimension_names, CF axes | ✅ | GLAD (HORIZONTAL_X/Y, fill, units), RASI |
| Scalar / 0-d arrays incl. v0 "crs" quirk | ✅ | `scalar` fixture + bundled |
| Sparse arrays (missing chunks) | ✅ | bundled `sparse` fixture |
| Path-traversal / malformed inputs | ✅ | 74/74 incl. ~30 negative fixtures |

---

## 4. (c) Sample workflows & cross-validation

All scripts are in `scripts/`; raw logs in `qc/`. Cross-validation opens the **same** repo via GDAL multidim and via `icechunk`+`xarray`, comparing shape, dims, dtype, and sampled values (`scripts/compare.py`).

### C1. Bundled autotest (Phase A evidence)
`python -m pytest autotest/gdrivers/icechunk_driver.py` → **74 passed, 0 failed** (~1.9 s). Covers positive cases + ~30 negative fixtures (malformed/short/missing snapshot & manifest, invalid zstd, non-increasing ids/indices, path traversal, offline repo, mutually-exclusive refs).

### C2. Branch/tag selection (synthetic `fixtures/multi_ref`) — BLOCKER reproduction
main=[30,31,32,33], tag v1=[10,11,12,13], tag v2 & branch dev=[20,21,22,23].

| connection | GDAL reads | xarray truth | ok |
|---|---|---|---|
| `fixtures/multi_ref` (main) | [30,31,32,33] | [30,31,32,33] | ✅ |
| `ICECHUNK:…?tag=v1` | [30,31,32,33] | [10,11,12,13] | ❌ |
| `ICECHUNK:…?tag=v2` | [30,31,32,33] | [20,21,22,23] | ❌ |
| `ICECHUNK:…?branch=dev` | [30,31,32,33] | [20,21,22,23] | ❌ |

The VSI layer *does* honor the ref when present in the path: `ZARR:"/vsiicechunk/{…multi_ref?tag=v1}"` → [10,11,12,13], `?branch=dev` → [20,21,22,23]. So the bug is purely the driver dropping the ref before delegation. `list-branches`/`list-tags` are correct.

### C3. Negative cases (synthetic) — graceful, no crash
- Unsupported codec `numcodecs.zlib`: `ERROR 6: Unsupported codec`; `ReadAsArray` raises `RuntimeError`; process survives. `gdal mdim info` then lists `"arrays": {}` (the array is dropped — see §2.11).
- Truncated manifest (native chunks): `RuntimeError: <manifest>: too small file`; process survives.
- `scalar`/`hierarchy` fixtures cross-validate ✅.

### C4. RASI — virtual chunks (v1 `refs/` layout, us-west-2) ⭐
`ICECHUNK:/vsis3/nasa-waterinsight/virtual-zarr-store/icechunk/RASI/HISTORICAL`. Structure reads correctly (lat=152, lon=132, percentile=5, time=780; 8 Float32 percentile variables, fill −9999).

The virtual chunks reference NetCDF-3 files in `s3://nasa-waterinsight/RASI/`. Resolved chunk for `SWE_Percentiles/c/0/0/0/0`: `OFFSET=492, SIZE=401280` (= 5×152×132×4) in `…/SWE_percentiles_195001.nc`. **GDAL's decoded values exactly match the raw source bytes** (`np.frombuffer(dtype=">f4")`, `allclose=True`, identical min/max/mean).

Notably, the official `icechunk`+`xarray` client **refuses** these same chunks ("the checksum of the object owning the virtual chunk has changed") — the source files' etag/last-modified no longer match the manifest. Because GDAL ignores those checksums (§2.3), it returns the (here-correct) data anyway. This is the real-world demonstration of finding §2.3: more lenient, but unable to detect genuine drift.

### C5. rasterio / rioxarray reachability
Because the driver is **multidimensional-only**, GDAL's classic 2-D path (what rasterio uses) exposes each array as a **subdataset**, not as direct bands:
`gdal.Open("fixtures/native_inline")` → `RasterCount=0`, subdatasets `ZARR:"/vsiicechunk/{…}":/native_grid`, `:/temperature`. So the rasterio workflow is: open the repo → enumerate `.subdatasets` → open a specific subdataset (a 2-D slice / band stack). See the runnable demo in §C8 and `scripts/demo_rasterio_rioxarray.py`.

### C6. GLAD — native chunks (us-east-1): structure ✅, near-origin ✅, large-manifest ❌
`ICECHUNK:/vsis3/icechunk-public-data/v1/glad`. `list-branches` → `main` "wrote coordinate arrays" (2025-04-21). Structure: dims x=1,440,000, y=560,000, year=5 with CF axis types (HORIZONTAL_X/Y, EAST/NORTH), zstd compressor, fill values. Cross-validation of a **near-origin** sample PASSES — all 5 variables agree with xarray (incl. the 5×560000×1440000 `lclu` array).

**But** a windowed read away from the origin (`lclu` at year0, y=280000, x=720000) fails 3/3 with `ERROR 1: …/manifests/8MKBNVZE7V8F7DNNDFTG: invalid Manifest Flatbuffer`. The same region loads cleanly via **both icechunk+xarray and icechunk+zarr-python directly** (`zarr.open_group(session.store)["lclu"][0,280000:280016,720000:720016]` → values 23–24). `lclu` is `(5,560000,1440000)` uint8 with chunk grid `(1,1000,1000)` ⇒ **~4.03M chunks**; the offending manifest is 39.7 MB on disk → **94.1 MB decompressed FlatBuffer** with >1e6 chunk-ref tables → exceeds the default verifier `max_tables`. This is **blocker B2** (§2.2): the repo is valid and fully readable by the reference stack — purely a GDAL-side verifier cap — but GDAL can read only the portions of GLAD backed by small-enough manifests.

### C7. GFS forecast — native chunks (dynamical, us-west-2) ✅ (data); time coords differ by design
`ICECHUNK:/vsis3/dynamical-noaa-gfs/noaa-gfs-forecast/v0.2.7.icechunk`. 29 arrays. All **25 large 4-D fields** (7499×209×721×1440 — `temperature_2m`, `wind_{u,v}_{10m,100m}`, `precipitation_surface`, `pressure_*`, categorical precip, etc.) agree exactly with xarray on sampled corners. The only "mismatches" are CF-encoded coordinates `init_time`, `valid_time`, `lead_time`, `expected_forecast_length`: **xarray applies CF datetime/timedelta decoding while GDAL returns the raw stored integers** — expected behavior, not a driver bug (GDAL's Zarr driver returns raw values; CF decoding is the caller's responsibility). Reads used near-origin corners (small manifests) so B2 was not triggered. (Two dynamical GFS analysis repos `noaa-gfs-analysis/v0.1.0.icechunk` & `vpara0.icechunk` and MRMS `noaa-mrms-conus-analysis-hourly/v0.3.0.icechunk` were located but not value-validated; same code paths as GFS forecast.)

### C8. rasterio / rioxarray demo (the real user workflow) ⭐
Built **rasterio 1.5.0** and **rioxarray 0.22.0** from source against this GDAL (`rasterio.__gdal_version__ == 3.14.0dev-8c2b212`). Because the driver is multidim-only, the classic 2-D path exposes each array as a **subdataset** — so the user pattern is *open repo → read `.subdatasets` → open one*.

Local synthetic (`fixtures/native_inline`):
```
rasterio.open("ICECHUNK:fixtures/native_inline").subdatasets ->
   ZARR:/vsiicechunk/{fixtures/native_inline}:/native_grid
   ZARR:/vsiicechunk/{fixtures/native_inline}:/temperature
rasterio.open(":/temperature")  -> driver=Zarr count=1 W=6 H=6 dtype=float64 ; read(1) == [[0.5..5.5]…]
rioxarray.open_rasterio(":/temperature") -> dims ('band','y','x') shape (1,6,6), values match
```
Remote GLAD: `rasterio.open(repo).subdatasets` → `:/lclu`; opening it reports **count=5 (bands=year), W=1440000, H=560000, uint8, crs=EPSG:4326, transform Affine(0.00025,0,-180, 0,-0.00025,80)** — georeferencing propagates through to rasterio/rioxarray. A near-origin windowed read works; a large-manifest window hits **B2**. Full output: `qc/rasterio_demo_output.txt`; script `scripts/demo_rasterio_rioxarray.py`.

**Takeaway for users:** rasterio/rioxarray *do* work against Icechunk via this driver, but only through subdatasets (not `rasterio.open(repo).read()` directly), and georeferencing is preserved. This non-obvious workflow is why a documented demo is requested (TODO **D1**).

### C9. Light performance comparison (indicative)
GDAL Icechunk driver vs the reference stack (icechunk+zarr-python, icechunk+xarray) on identical reads. Single-threaded; each backend cold-opened in a **fresh process**; remote object pre-warmed so S3 server-side caching is equal. `scripts/bench.py` + `scripts/run_bench.sh`; raw numbers in `qc/bench_results.jsonl`. **Not rigorous — indicative only** (no concurrency, single slice).

**LOCAL** — 64 MB float32, 16 zstd chunks, no network (isolates open + decode):

| backend | open (s) | cold read (s) | warm read (s) | warm MB/s |
|---|--:|--:|--:|--:|
| gdal (multidim) | 0.003 | 0.050 | 0.047 | **1368** |
| zarr-python | 0.006 | 0.065 | 0.060 | 1070 |
| xarray | 0.003 | 0.061 | 0.059 | 1087 |

**REMOTE** — one 6.2 MB `temperature_2m` chunk from GFS forecast, anonymous S3 us-west-2:

| backend | open (s) | cold read (s) | warm read (s) |
|---|--:|--:|--:|
| gdal (multidim) | **0.255** | 0.268 | **0.001** |
| zarr-python | 0.285 | 0.224 | 0.094 |
| xarray | 0.912 | 0.172 | 0.122 |

**Takeaways:** the driver is **performance-competitive** with the reference stack — ~25% faster on local zstd decode, the **lowest open latency** (xarray's open is ~3.5× higher), and its `GDALRasterBlock` cache makes repeated reads of a region ~100× faster (zarr/xarray re-decode each read). **Cold cloud reads are network-bound and comparable across all three.** No surprises or pathological slowness. (Not measured: concurrency/parallel reads, and the large-manifest path — blocked by B2.)

---

## 5. (d) Major TODO items

### Blockers (fix before merge/use)
- **B1. Honor `?branch=`/`?tag=` in the delegated path.** Propagate the resolved ref into `ZARR:"/vsiicechunk/{<path>?branch=…|?tag=…}"` (`icechunkdriver.cpp:217`). Add a regression test asserting non-main **data values** through the `ICECHUNK:` connection (the bundled suite only checks listing + open). *(Central QC finding; §2.1, §C2.)*
- **B2. Raise the FlatBuffers verifier limits for large manifests.** Construct the manifest (and snapshot/repo) `flatbuffers::Verifier` with `Verifier::Options` raising `max_tables` (and `max_size`) — `icechunkmanifest.cpp:99`, `icechunksnapshot.cpp:88`, `icechunkrepo.cpp:255`. Without this, arrays with **≥1,000,000** chunk refs per manifest (e.g. GLAD `lclu`; bisected threshold is exactly 1e6, §6.1) are unreadable with a misleading "invalid Manifest Flatbuffer". Add a regression test that reads such a region. *(§2.2, §C6, §6.1.)*
- **B3. Fail loudly on an unreadable referenced (virtual) chunk.** A manifest virtual-chunk entry asserts the chunk exists; the driver currently lets the Zarr delegate treat a 404/inaccessible backing object as a missing (sparse) chunk and returns fill silently. Resolve virtual chunks such that an unreadable backing object is a hard error (verify the byte-range object, or a "require referenced chunks" mode), and add `file://` to the URL morph table (`vsiicechunk.cpp:342-350`) so local virtual chunks work or fail loudly. Regression test: a virtual ref to a missing object must raise, not return zeros. *(§2.12, §6.2.)*

### Should-fix
- **S1. Virtual-chunk checksum validation.** Validate `etag`/`last_modified` outside `#ifdef DEBUG` (at least optionally, e.g. a `ICECHUNK_VERIFY_VIRTUAL_CHECKSUMS` option), or document loudly that staleness is not detected. Matters for correctness of long-lived virtual stores.
- **S2. v1-layout virtual-chunk credentials + azure.** `OpenV1` should read `config.yaml` (or otherwise apply virtual_chunk_containers) so region/anonymous auto-apply for `refs/`-layout repos like RASI; extend `ProcessConfig` to **azure** (no `az://` branch today) and honor requester-pays (currently warn-only). *(§2.6, §6.2.)*
- **S3. Unsupported-codec visibility.** Surface undecodable arrays in listings (e.g. as present-but-unreadable) instead of silently omitting them.
- **S4. Per-open memory leak.** ~1.5–2 KB leaked per repo open (linear, unbounded; §6.5) — matters for long-lived processes opening many repos. (The larger ~7.6 KB/read leak is shared with the plain Zarr driver and is a pre-existing GDAL issue, worth a separate upstream report.)

### Nice-to-have
- **N1.** Snapshot-id / `as_of` time-travel open options.
- **N2.** Robust connection-string parsing (multiple params, URL-decode).
- **N3.** Avoid process-global `VSISetPathSpecificOption` side effects (scope to the dataset).
- **N4.** Fix cosmetic garbled error string (§2.10).
- **N5.** Consider spatial index for `findManifestIdForChunk` on many-manifest arrays.

### Documentation / demos
- **D1. (requested) Ship rasterio/rioxarray usage demos.** Because the driver is multidim-only, the rasterio/rioxarray path is non-obvious (open repo → `.subdatasets` → open one; not `rasterio.open(repo).read()`). A short documented recipe — and the working demo in `scripts/demo_rasterio_rioxarray.py` (§C5, §C8) — would save users significant confusion.
- **D2.** Document the format-version scope (FlatBuffers v1/v2 only; msgpack-era unreadable) and the virtual-chunk credential model (esp. v1 layout).

---

## 6. Deep QC (round 2) — integrity, robustness & coverage

A second pass targeting the QC blind spots that round 1 left thin: virtual-chunk integrity under bad credentials, dtype/shape/codec breadth (vs corner-sampling), the exact B2 boundary, memory-safety, concurrency, leaks, and the msgpack format boundary. New scripts: `test_virtual_chunks.py`, `property_test.py`, `bisect_b2.py`, `test_concurrency_leak.py`, `run_asan_checks.sh`; raw logs `qc/{virtual_chunk_results,property_test_results,b2_bisection_results,concurrency_leak_results,asan_run}.*` and `qc/phaseE_deep_qc_findings.md`.

### 6.1 B2 threshold — bisected to exactly 1,000,000 refs
`scripts/bisect_b2.py` builds single-array manifests with N one-element chunks (N chunk-ref tables in one manifest):

| refs | manifest on disk | GDAL read |
|---:|---:|---|
| 990,000 | 6.5 MB | ✅ READ_OK |
| **1,000,000** | **6.6 MB** | ❌ `invalid Manifest Flatbuffer` |
| 1,050,000 | 6.9 MB | ❌ |

First failure at **exactly 1e6** with a **6.6 MB** manifest ⇒ it is the FlatBuffers default `Verifier::Options::max_tables == 1,000,000` (the *table count*), not byte size. Confirms B2's root cause empirically.

### 6.2 Virtual-chunk integrity & credentials (the #1 round-1 blind spot) ⭐
Round 1 exercised virtual chunks only via one dataset (RASI: NetCDF-3, same bucket, anonymous). Round 2 built *synthetic* virtual-chunk repos to probe what happens when a manifest-referenced backing object is **unreadable**:

| backing-object condition | GDAL | reference (icechunk+zarr) |
|---|---|---|
| S3 object missing (clean **404**) | **READ_OK, all-zeros, no warning** ❌ | **RAISES** `error fetching virtual reference` |
| `file://` local (gate=YES) | **READ_OK, all-zeros** (truth `[0..7]`) ❌ | reads correctly |
| host unreachable / transport error | RAISES ✅ | raises |
| corrupt **native** chunk | RAISES ✅ | raises |
| `file://` local (gate=off, default) | RAISES (SSRF guard) ✅ | n/a |

This is **blocker B3**: the driver cannot distinguish "manifest-referenced object inaccessible" (should error) from "chunk legitimately absent / sparse" (fill is correct), so the *missing-credentials* and *moved/deleted-object* cases return plausible **zeros silently**. Credential-model code analysis (`ProcessConfig`, §2.6): persisted region/anonymous applied for s3/gcs only — **no azure, no secret-credential path** (icechunk supplies those at runtime, unpersisted). Net: a private cross-account virtual store the caller can't authenticate to → silent zeros, not an error.

### 6.3 Property-based cross-validation — 200/200, zero mismatches
`scripts/property_test.py` (icechunk-style randomized sampling): ndim 1–4, random shapes, random chunking (usually **not** dividing the shape → partial/edge chunks every other trial), random fill, attrs, dimension names; GDAL multidim read cross-checked against zarr-python on shape, dtype, **full-array values**, and a **random sub-window** (offset/count math). **200 trials → 0 value mismatches, 0 errors**, across all 12 dtypes (int/uint 8–64, float32/64, **complex64/128**) × {none, zstd, blosc, gzip} compressors; 25% of trials wrote arrays **sparsely** (missing chunks → fill verified through both stacks). Directly closes the round-1 "corners-only sampling" and dtype-fidelity gaps.

### 6.4 Concurrency — clean
`scripts/test_concurrency_leak.py`: 8 threads × 40 iters, each with its **own** dataset handle ⇒ **320 concurrent reads, 0 errors / 0 mismatches / no crash**; `/vsiicechunk` filesystem hammered 8×60 concurrently ⇒ 0 errors; no fd growth. *Caveat:* this is a correctness-under-load + crash check, not a formal data-race audit (that needs ThreadSanitizer — not run).

### 6.5 Memory leak — small driver open-path leak; larger read-path leak is pre-existing GDAL
Tight open/read/close loop, RSS sampled (linear, unbounded, not reclaimed by cache-clear; fds stable):

| path | leak / iteration | attribution |
|---|---:|---|
| icechunk open-only (no read) | ~1.5–2.0 KB | **driver** (repo/snapshot parse) |
| icechunk open+read | ~7.8 KB | dominated by ↓ |
| **plain Zarr** open+read (control) | **~7.6 KB** | GDAL Zarr/multidim read path — **identical**, ⇒ not PR-introduced |

Honest split: the dominant per-read leak reproduces identically with the **plain Zarr driver**, so it's a pre-existing GDAL multidim/binding issue (worth an upstream report) — but it affects Icechunk users in long-lived processes. The icechunk open path adds a smaller real ~1.5–2 KB/open (TODO S4).

### 6.6 Memory-safety (ASan + UBSan) — built; runtime inconclusive on this toolchain
A dedicated `-fsanitize=address,undefined` GDAL was built (`build-asan/`, RelWithDebInfo, Python off) and links the sanitizer runtime; ASan **initializes** (`AddressSanitizer: libc interceptors initialized`). However, on this macOS arm64 + conda-clang-19 toolchain every instrumented invocation — including a trivial `--version`/single-fixture open — spins at ~99% CPU for **many minutes without completing**, so **no conclusive ASan/UBSan pass was obtained**. This is toolchain friction, not an observed driver fault (no diagnostic was ever emitted). Memory-safety is therefore evidenced indirectly and strongly by: the 200 property trials, ~30 bundled negative/malformed fixtures, corrupt/truncated/invalid-version/corrupt-body inputs (all graceful, §6.7), virtual byte-range reads, and 320 concurrent reads — **zero crashes anywhere** in the normal build. Re-running ASan on a Linux/system-clang toolchain is the recommended way to close this fully.

### 6.7 Format boundary / msgpack era — resolved (no public exposure)
Oldest icechunk on PyPI is **0.2.0**, which already writes **FlatBuffers spec v1** (`refs/` layout) — the PR's GDAL reads a freshly-authored 0.2.0 repo correctly. The pre-FlatBuffers **msgpack** format predates 0.2.0 and was never on PyPI ⇒ **no public msgpack-era repos exist** to be unreadable. Unknown/old files reject gracefully (header version byte must be 1/2; invalid version → `invalid Snapshot Flatbuffer`, corrupt body → `ZSTD decompression failed`, no crash).

### 6.8 Minor (round 2)
- **Repo dir named `repo`** → mis-resolved as the repo file → `invalid Repo Flatbuffer` (`icechunkrepo.cpp:206`). Low impact; confusing. (§2.13)
- **Magic signature unenforced** — keyed off version byte + FlatBuffers verify instead (§2.14). Robust in practice.

### 6.9 Network fault injection — B3 precisely bounded; no other silent corruption ⭐
Mirroring Earthmover's own icechunk rigor (toxiproxy + object-store fault injection to verify retry behaviour), we faulted the virtual-chunk fetch two ways and classified every outcome as OK / loud-error / **silent-fill** / **silent-corrupt**, with icechunk+zarr as the bar. For a manifest-*referenced* chunk only OK or a loud error is acceptable.

**(a) HTTP-semantic faults** — `scripts/fault_injection.py`, a tiny S3-mock that misbehaves per object key (`qc/fault_injection_http.txt`):

| fault | GDAL | reference | verdict |
|---|---|---|---|
| ok (control) | OK_CORRECT | OK_CORRECT | ✅ |
| **HTTP 404** (not found) | **SILENT_FILL (zeros)** | LOUD_ERROR | ❌ **B3** |
| HTTP 500 / 503 / 429 | LOUD_ERROR | LOUD_ERROR | ✅ |
| truncated stream (claims full len, sends half) | LOUD_ERROR | LOUD_ERROR | ✅ |
| short Content-Length (sends half, honest) | LOUD_ERROR | LOUD_ERROR | ✅ |
| corrupt bytes (valid length) | wrong data (`-1`) | **same wrong data** | ⚠️ shared no-checksum gap (S1) |

**(b) Transport faults** — `scripts/fault_injection_toxiproxy.py`, **real toxiproxy** in front of the clean mock (`qc/fault_injection_toxiproxy.txt`):

| toxic | GDAL | reference | verdict |
|---|---|---|---|
| latency (300 ms) · bandwidth (512 KB/s) · slow_close | OK_CORRECT | OK_CORRECT | ✅ tolerated |
| **limit_data** (cut after 100 KB / 256 KB) | LOUD_ERROR | **OK_CORRECT (retried)** | ✅ no corruption |
| reset_peer · timeout | LOUD_ERROR | LOUD_ERROR | ✅ |

**Conclusions:** (1) **B3's silent fill is uniquely the HTTP 404 / object-not-found path** — `/vsis3` maps not-found to "absent chunk" and the Zarr layer fills. Every *other* fault (5xx, truncated/short stream, mid-flight cut, reset, timeout) **errors loudly** and matches the reference — so the driver is robust to the streaming-failure class that bit icechunk in production. (2) The lone silent-corruption case (valid-length wrong bytes) is the **shared no-checksum gap** (S1) — the reference returns the same wrong data, so it is not GDAL-specific. (3) On a mid-stream connection cut, **icechunk transparently retries and completes** while GDAL at the baseline `GDAL_HTTP_MAX_RETRY=0` surfaces an error — users wanting icechunk-like resilience should set `GDAL_HTTP_MAX_RETRY>0`.

---

## Appendix — reproduce

```bash
source scripts/env.sh                       # custom GDAL build + python env ($ICPY, gdal CLI)
$ICPY scripts/make_synthetic.py             # build fixtures/ (native+inline, refs, scalar, codec, corrupt)
bash  scripts/run_synthetic_checks.sh       # offline checks (incl. B1 blocker repro, negative cases)
# live cross-validation (GDAL multidim vs icechunk+xarray); raw logs land in qc/
$ICPY scripts/compare.py --bucket icechunk-public-data --prefix v1/glad --region us-east-1      # GLAD (native)
$ICPY scripts/compare.py --bucket dynamical-noaa-gfs --prefix noaa-gfs-forecast/v0.2.7.icechunk --region us-west-2  # GFS
$ICPY scripts/compare.py --bucket nasa-waterinsight \
      --prefix virtual-zarr-store/icechunk/RASI/HISTORICAL --region us-west-2 \
      --virtual-anon-prefix s3://nasa-waterinsight/RASI/   # RASI (virtual chunks)
$ICPY scripts/demo_rasterio_rioxarray.py    # rasterio/rioxarray user-workflow demo
bash  scripts/run_bench.sh                   # light perf comparison vs zarr-python/xarray (§C9)

# --- round 2 (deep QC / blind-spot closure, §6) ---
$ICPY scripts/test_virtual_chunks.py         # B3: silent-fill on inaccessible virtual chunks (§6.2)
$ICPY scripts/property_test.py --trials 200  # 200 randomized dtype×codec×shape vs zarr-python (§6.3)
$ICPY scripts/bisect_b2.py                   # bisect the 1e6-ref manifest threshold (§6.1)
$ICPY scripts/test_concurrency_leak.py       # concurrency + per-open/read leak (§6.4, §6.5)
$ICPY scripts/fault_injection.py             # HTTP-semantic fault matrix (404/5xx/truncate/...; §6.9a)
toxiproxy-server & ; $ICPY scripts/fault_injection_toxiproxy.py  # transport faults via toxiproxy (§6.9b)
bash  scripts/run_asan_checks.sh             # ASan/UBSan run (slow on this toolchain; §6.6)

# bundled driver test suite:
cd gdal/autotest && $ICPY -m pytest gdrivers/icechunk_driver.py -q   # 74 passed
```

Repos discovered for dynamical datasets: GFS forecast `noaa-gfs-forecast/v0.2.7.icechunk`, GFS analysis `noaa-gfs-analysis/v0.1.0.icechunk` (+`vpara0.icechunk`), MRMS `noaa-mrms-conus-analysis-hourly/v0.3.0.icechunk` (all bucket-root region us-west-2, anonymous).

**Artifacts in this directory:** `QC_REPORT.md` (this file) · `scripts/` (env, fixture generator, comparison harness, synthetic checks, rasterio/rioxarray demo, **round-2: virtual-chunk/property/bisect/concurrency/ASan**) · `qc/` (raw logs + phase notes incl. `phaseE_deep_qc_findings.md`) · `fixtures/` (synthetic Icechunk repos) · `gdal/` (the PR checkout + `build/`, `install/`, **round-2 `build-asan/`**).
