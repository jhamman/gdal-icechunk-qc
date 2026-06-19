# Phase A — Code review notes (raw)

PR: OSGeo/gdal #14755 — Icechunk read-only driver. Head `rouault/gdal@icechunk`, SHA `8c2b212`. Target GDAL 3.14.
Driver: `frmts/icechunk/` (~3,705 LOC). Author: Even Rouault. License MIT.

## Architecture (confirmed by reading source)
- The Icechunk driver does NOT decode Zarr itself. On open it:
  1. parses the connection string (`ICECHUNK:<path>[?branch=|?tag=]`),
  2. resolves repo → branch/tag → snapshot (just to validate & pick default branch),
  3. rewrites the dataset name to `ZARR:"/vsiicechunk/{<path>}"` and delegates to the **Zarr driver** (`icechunkdriver.cpp:217-223`), passing through `papszOpenOptions` and open flags.
- A custom VSI filesystem `/vsiicechunk/` (`vsiicechunk.cpp`) presents the repo as a Zarr-v3 hierarchy: serves `zarr.json` (from snapshot node `user_data`) and chunk keys `.../c/i/j/...`.
- Chunk bytes are served three ways (`vsiicechunk.cpp:420-551`):
  - native/virtual → `/vsisubfile/<offset>_<length>,<vsi-url>` (range read; offset/length validated vs file size at `Open`/`Stat`).
  - inline → copied from manifest bytes into an in-memory VSI handle.
- So **all codec/dtype/consolidated-metadata/sharding handling is the Zarr driver's job**. The Icechunk driver only does ref/snapshot/manifest/chunk-location resolution + byte serving. Implication: codec support == GDAL Zarr-driver support (blosc/gzip/zstd/crc32c/transpose/sharding/pcodec); `numcodecs.shuffle`/`numcodecs.zlib` unsupported is a Zarr-driver limitation surfaced through this path.

## Format version handling — CONFIRMED: FlatBuffers-only
- `icechunkdefs.h`: magic `abySIG = "ICE\xF0\x9F\xA7\x8ACHUNK"` (12B) + impl-name(24) + spec_version(1) + file_type(1) + compression(1) = HEADER_SIZE 39. File types: SNAPSHOT=1, MANIFEST=2, TRANSACTION_LOG=4, REPO_INFO=6.
- `icechunkutils.cpp:88-95` (`DecompressFile`): accepts header spec-version byte **1 or 2 only**; else "Icechunk version N not supported". Compression algo: NONE or ZSTD only.
- `icechunkrepo.cpp:269-283`: repo flatbuffer `spec_version()` must be 1 or 2, AND must equal the header version byte.
- Autotest `scalar_array_v1` requires `icechunk.spec_version()=="1"` (pip install icechunk<2). So **"v1"/"v2" = Icechunk spec versions 1 & 2, both FlatBuffers-serialized**. icechunk 1.x writes spec_v1; icechunk 2.x writes spec_v2.
- **FINDING (spec gap, expected):** The pre-FlatBuffers msgpack era (very old icechunk) has a different/absent magic → Identify won't match, DecompressFile rejects. Such repos are unreadable. Need to confirm how old GLAD (`v1/glad`) actually is. The `v1/` in the GLAD prefix is dataset naming, NOT necessarily format version.

## Ref resolution (`icechunkrepo.cpp`, `icechunkdriver.cpp`)
- Two repo layouts handled:
  - **New (spec v2-style):** a single `repo` FlatBuffer file holds branches/tags/snapshots inline (`Open`, lines 198-466). Validates snapshot_index bounds; rejects duplicate branch/tag names (fixtures `repo_two_branches_same_name`, `repo_two_tags_same_name`); handles repo `status==Offline` (fixture `repo_offline`).
  - **Old (`refs/` dir):** `OpenV1` (lines 67-111) reads `refs/branch.<name>/ref.json` and `refs/tag.<name>/ref.json` (JSON) → snapshot ids. Snapshots/manifests themselves still must be FlatBuffers.
- Default branch logic (`icechunkdriver.cpp:141-198`): no branch/tag → use `main` if present; if branches exist but no `main` → error listing valid branch names; if no branches at all → empty `DummyDataset`. Good UX.
- `?branch=`/`?tag=` parsing (`GetFilenameFromDatasetName`, driver.cpp:39-70). **Limitations:** only a SINGLE query param supported; no `&` combination; value not URL-decoded; an unrecognized `?foo=` → hard error.
- **No snapshot-id open option and no `as_of` time-travel** — only branch/tag selection. Spec supports snapshot-id & as-of; driver does not. (Feature gap.)

## Virtual-chunk credentials/region — BETTER than expected, but partial
- `ProcessConfig` (`icechunkrepo.cpp:117-190`) reads the repo's stored `config.virtual_chunk_containers` (flexbuffers→JSON) and, per container, calls `VSISetPathSpecificOption` on the morphed VSI path:
  - s3:// → sets `AWS_DEFAULT_REGION` (from `region`), `AWS_NO_SIGN_REQUEST=YES` (if `anonymous`), warns if `requester_pays` and `AWS_REQUEST_PAYER` unset.
  - gs://, gcs:// → sets `GS_NO_SIGN_REQUEST=YES` (if anonymous). No region.
  - **az://, https:// NOT handled** for auto-credentials/region. (Azure/HTTP virtual chunks need manual VSI config.)
- **FINDING (design note):** `VSISetPathSpecificOption` mutates **process-global** VSI config keyed by path prefix; it is set at open time and persists (not undone on dataset close). Thread-safety / cross-dataset leakage concern in long-lived processes; also a reader can't easily override (path-specific options take precedence over generic config).
- URL→VSI morphing for actually fetching virtual chunks (`vsiicechunk.cpp:328-382`, `GetChunkFilename`) DOES handle s3/gs/gcs/az/azure/http/https. So azure/http virtual chunks are fetchable, just without auto-cred/region wiring.
- **Security:** non-network (local-path) chunk `location` is **disabled by default**; requires `ICECHUNK_ALLOW_LOCAL_CHUNK_LOCATION=YES` (anti-SSRF/local-file-exfil). Good. (Cosmetic bug: garbled error string "cICECHUNK_ALLOW_LOCAL_CHUNK_LOCATION configuration option", vsiicechunk.cpp:375.)

## Manifest / chunk-ref parsing (`icechunkmanifest.cpp`)
- FlatBuffers verifier + id-matches-filename check (fixture `..._mismatch_id_filename`). Compression: v1=none; v2=NONE or ZSTD-dict.
- Per ChunkRef: enforces EXACTLY ONE of {inline, chunk_id (native), location (virtual), compressed_location (virtual, zstd-dict)} via `nAlternativeCount` (fixtures `..._mutually_exclusive_info`, `..._missing_info`). inline requires offset==length==0 (fixture `..._inline_content_with_offset_length`). offset+length overflow guarded.
- Sorted-order enforcement: arrayManifests by node_id; chunkRefs by index; same dim count (fixtures `..._array_id_non_increasing`, `..._index_non_increasing`, `..._index_not_same_dim`). Binary search in `GetChunkRef`.
- **FINDING (data integrity):** virtual-chunk `checksum_etag` / `checksum_last_modified` are parsed **only under `#ifdef DEBUG`** (`icechunkmanifest.cpp:258-265`). In Release builds these are read-and-discarded → the driver does NOT detect a virtual source file that changed since the manifest was written (silent stale/incorrect data). Icechunk stores these expressly so readers can detect staleness.
- **FINDING (limitation):** decompressed virtual-chunk URL uses a FIXED 1024-byte buffer (`achTempDecompressedLocation`, manifest.cpp:174). A virtual URL whose decompressed form exceeds 1024 bytes → `ZSTD_decompressDCtx` fails → clean error, chunk unreadable. Hard cap on virtual URL length (fails safe, not a memory bug).

## Snapshot parsing (`icechunksnapshot.cpp`)
- FlatBuffers verifier + id-matches-filename.
- **Path traversal hardening** (lines 177-197): node path must start `/`, not end `/`; rejects `\`, `/./`, `/../`, trailing `/..`. Comment explicitly ties this to VSI safety (fixture `..._path_traversal`).
- Shape: v2 `shape_v2` (num_chunks/dim; 0 rejected); v1 `shape` (array_length+chunk_length → div_round_up). Overflow-guards on total chunk count.
- Scalar-array v0 special case (empty numChunks + single extent [0,1)) — the "weird scalar array"/"crs" handling (fixtures `scalar_array`, `clunky_scalar_array`, `..._missing_shape_v2`).
- Manifest extents validated vs numChunks; partial consistency check (referenced ≤ total). DEBUG-only exhaustive "each chunk referenced ≤ once" check.
- Node re-sort by path if not sorted — normalizes for icechunk spec sort-order confusion (refs earth-mover/icechunk#2183).
- `findManifestIdForChunk` (lines 626-669): thread_local last-ref guess + **linear scan** over manifestRefs. Comment notes potential bottleneck for arrays with many manifests (RTree/KDTree TODO). Perf consideration for very large arrays.

## Caching
- `IcechunkRepo::OpenManifest` uses a **process-global static LRU** of manifests keyed by `rootPath|manifestId`, validating file size + chunk-ref count vs snapshot's expected values (`icechunkrepo.cpp:521-581`). Cleared via driver `pfnClearCaches`.
- `VSIIcechunkFileSystem` has an **instance** LRU of (repo, snapshot) keyed by rootFilename+branch/tag (`vsiicechunk.cpp:85,218-240`).
- Default lru11::Cache max size unspecified here → check memory behavior for repos with many manifests.

## Build wiring (`CMakeLists.txt`)
- `add_gdal_driver(... PLUGIN_CAPABLE NO_DEPS NO_SHARED_SYMBOL_WITH_CORE)`, links `${ZSTD_TARGET}`, includes `third_party/` (vendored flatbuffers), namespace-mangles `flatbuffers→gdal_flatbuffers`/`flexbuffers→gdal_flexbuffers`/`generated→gdal_generated_icechunk`. Unity build OFF (flexbuffers private-member clash). Optional C++20 for std::ranges (commented out; std::lower_bound fallback otherwise).
- Cross-cutting: flatbuffers moved to `third_party/flatbuffers/` (FlatGeobuf must still build); `port/cpl_vsil.cpp` `RegisterHandlerLoader` for deferred plugin load on `/vsiicechunk/`.

## Tests present (`autotest/gdrivers/icechunk_driver.py`, 2710 lines, ~50 fixtures)
- Positive: empty_repo, scalar/regular arrays (v1 & v2), multi_chunks, physical_chunks (native), sparse, path_sorting, list-branches/list-tags via `gdal.alg.driver.icechunk.*`.
- Negative (~30 `test_icechunk_*` fixtures): malformed/short/missing snapshot & manifest, invalid zstd, non-increasing ids/indices, mismatched dims, chunk-ref invalid offset/length/location, beyond-file-size, mutually-exclusive/missing ref info, path traversal, offline repo, invalid spec/path/id.
- Uses Python binding surface: `gdal.OpenEx(path, gdal.OF_MULTIDIM_RASTER)`, RootGroup/OpenMDArray/Read, `gdal.ReadDirRecursive`, `gdal.VSIStatL`, and the `gdal.alg.driver.icechunk.list_branches/list_tags` algorithm API.

## Open items to verify by running (Phase B/C)
- Confirm GLAD (`v1/glad`) opens (format version actually supported?).
- RASI virtual chunks: does the repo config declare the virtual_chunk_container so anonymous+region auto-apply, or must we set AWS_NO_SIGN_REQUEST/AWS_REGION manually?
- Does `numcodecs.shuffle`/`zlib` produce a clean ERROR 6 (no crash)?
- rasterio/rioxarray reachability of a multidim-only driver.
- Run the bundled autotest suite + Zarr + FlatGeobuf (regression).
