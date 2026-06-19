# Phase E — deep QC (blind-spot closure) findings (raw)

Follow-up round targeting the self-identified QC blind spots: virtual-chunk
credential/integrity behavior, property-based dtype/shape/codec coverage, empirical
B2 threshold, memory-safety (ASan/UBSan), concurrency, resource leaks, and the
msgpack format boundary. Build: GDAL 3.14.0dev (PR #14755) in env `gdalic`
(icechunk 2.0.6 spec v2, zarr 3.2.1). Scripts: `scripts/test_virtual_chunks.py`,
`property_test.py`, `bisect_b2.py`, `test_concurrency_leak.py`, `run_asan_checks.sh`.

---

## E1. *** HIGH: inaccessible VIRTUAL chunk silently reads as fill (data corruption) ***

A manifest virtual-chunk entry asserts a backing object exists at
`{location, offset, length}`. When that object is **unreadable**, GDAL does not
error — it returns the array **fill value (zeros)** with no warning, because the
virtual ref is mapped to a `/vsisubfile/<off>_<len>,<vsi-url>` key and the Zarr
delegate cannot distinguish "referenced object inaccessible" from "chunk
legitimately never written" (the latter being valid sparse-Zarr semantics → fill).

Reproduced (`scripts/test_virtual_chunks.py`, results in `qc/virtual_chunk_results.txt`):

| backing-object condition | GDAL result | reference (icechunk+zarr) |
|---|---|---|
| S3 object missing (clean **404**) | **READ_OK, all zeros, NO warning** | **RAISES** `IcechunkError: error fetching virtual reference` |
| `file://` local source, gate=YES | **READ_OK, all zeros** (truth `[0..7]`) | reads correctly `[0..7]` |
| host unreachable / transport error | RAISES (`HTTP response code … 0`) | raises |
| corrupt **native** chunk (manifest entry) | RAISES (`too small file`) | raises |

So the defect surfaces specifically when the backing store reports **not-found /
access-denied (404/403-as-absent)** — exactly the outcome of: wrong/missing
credentials for a cross-account virtual container, a moved/deleted source object,
or a wrong region/bucket. The reference stack errors in every one of these; GDAL
silently substitutes zeros.

**`file://` sub-case:** `vsiicechunk.cpp:342-350` morph table maps
s3/gs/gcs/az/azure/http/https but **not `file://`**, and does not strip the scheme.
With `ICECHUNK_ALLOW_LOCAL_CHUNK_LOCATION=YES` the literal `file:///path` is used as
a `/vsisubfile` target, which GDAL cannot open → chunk treated as absent → fill.
(icechunk stores local virtual refs as `file://…`, so local virtual chunks are
effectively unreadable, and silently so.)

**Fix direction:** the driver should treat a manifest-referenced chunk whose backing
object cannot be opened as an **error**, not a missing chunk. Either (a) stat/verify
the byte-range object when resolving a virtual chunk and fail loudly, or (b) add a
config option to require referenced chunks. Also add `file://` to the morph table
(strip scheme → plain path) so local virtual chunks either work or fail loudly.

## E2. Virtual-chunk credential model (code + behavior)

`ProcessConfig` (`icechunkrepo.cpp:117-190`) reads the repo's **persisted**
`virtual_chunk_containers` config — for v2 from a flexbuffers blob inside the `repo`
FlatBuffer (`icechunkrepo.cpp:330-349`), for v1 from `config.yaml` — and applies, via
`VSISetPathSpecificOption`, per-container:
  - `AWS_DEFAULT_REGION` (s3), `AWS_NO_SIGN_REQUEST` (s3 anonymous), requester-pays
    (s3, **warning only** — does not set `AWS_REQUEST_PAYER`);
  - `GS_NO_SIGN_REQUEST` (gcs anonymous).

Gaps:
  - **Azure containers get no per-path config** — there is no `az://` branch in
    `ProcessConfig` (only s3 + gcs). Azure virtual chunks rely entirely on global VSI
    config.
  - **Secret credentials are never propagated.** In icechunk these are supplied at
    runtime via `Repository.open(authorize_virtual_chunk_access=…)` and are *not*
    persisted, so the driver cannot obtain them. Private cross-account virtual chunks
    therefore require the user to set GDAL path-specific creds manually
    (`AWS_*` / `VSISetPathSpecificOption`); if they don't, the read does not fail —
    it silently fills (see E1). This is the dangerous interaction.

The **RASI positive case works** precisely because its container is declared
anonymous+region in the persisted config, which `ProcessConfig` applies.

## E3. B2 threshold — empirically bisected (was code-inferred)

`scripts/bisect_b2.py` (results `qc/b2_bisection_results.txt`): single-array manifests
with N one-element chunks → N chunk-ref tables in one manifest.

| refs | manifest on disk | GDAL |
|---|---|---|
| 990,000 | 6.5 MB | READ_OK |
| **1,000,000** | **6.6 MB** | **RAISED `invalid Manifest Flatbuffer`** |
| 1,050,000 | 6.9 MB | RAISED |

First failure at **exactly 1,000,000** refs, with a manifest of only **6.6 MB** →
the trigger is the **table count**, not size — confirming FlatBuffers default
`Verifier::Options::max_tables == 1,000,000` (`icechunkmanifest.cpp:99` constructs the
verifier with default options). Upgrades B2 from inference to a bisected fact.

## E4. Property-based cross-validation — 200/200 pass

`scripts/property_test.py` (results `qc/property_test_results.txt`): randomized
(seeded) Zarr arrays — ndim 1-4, random shapes, random chunking (usually *not*
dividing the shape → partial/edge chunks), random fill, random attrs/dimension names —
written to local Icechunk, then GDAL multidim read cross-checked against zarr-python on
shape, dtype, **full-array values**, and a **random sub-window** (offset/count math).

**200 trials, 0 value mismatches, 0 errors.** Per-dtype pass counts (all 12 zero-fail):
int8/uint8/int16/uint16/int32/uint32/int64/uint64/float32/float64/**complex64/complex128**.
25% of trials wrote arrays **sparsely** (some chunks never written) → fill read back
correctly through both stacks. Closes the "corners-only sampling" and dtype-fidelity
gaps.

## E5. Concurrency — clean (not a formal race check)

`scripts/test_concurrency_leak.py` (results `qc/concurrency_leak_results.txt`):
  - 8 threads × 40 iters, each with its **own** dataset handle = **320 concurrent
    reads, 0 errors / 0 mismatches / no crash**.
  - `/vsiicechunk` filesystem hammered 8×60 concurrently — 0 errors.
  - No fd growth.
Caveat: this is a correctness-under-load + crash check. A formal data-race audit needs
ThreadSanitizer (not run; TSan + full GDAL + conda toolchain was out of scope).

## E6. Memory leak — small driver open-path leak + larger pre-existing Zarr read-path leak

Open/read/close in a tight loop, RSS sampled every 1-2k iters (linear, unbounded, not
reclaimed by cache-clear; fds stable):

| path | KB / iteration | attribution |
|---|---|---|
| icechunk **open only** (no read) | ~1.5–2.0 | **driver** (repo/snapshot parse) — linear |
| icechunk **open+read** | ~7.8 | dominated by read path below |
| **plain Zarr** open+read (control) | **~7.6** | **GDAL Zarr/multidim read path** — *identical*, so **not introduced by this PR** |

Honest attribution: the dominant ~7.6 KB/read leak reproduces identically with the
plain Zarr driver (and goes through the same Python `ReadAsArray`), so it is a
pre-existing GDAL multidim/Zarr (or SWIG-binding) issue, not a PR regression — but it
**does** affect Icechunk users in long-lived processes (tile servers, batch loops).
The icechunk open path adds a smaller but real ~1.5–2 KB/open.

## E7. Memory-safety: ASan + UBSan build & run

Built a dedicated `-fsanitize=address,undefined` GDAL (`build-asan/`, RelWithDebInfo,
Python off, driven via the CLI; `scripts/run_asan_checks.sh`). Ran over: positive
structure+decode (native/inline/nested, zstd manifests), ref selection (`?tag`/
`?branch`), the `file://` virtual path, negative fixtures (corrupt manifest,
unsupported codec, bogus path), and a remote GFS corner.
RESULT: <pending — see qc/asan_run.log; expect no AddressSanitizer/UBSan diagnostics>.
macOS LeakSanitizer is unsupported, so leaks were measured separately (E6).

## E8. Format boundary / msgpack era — resolved (no public exposure)

Oldest icechunk on PyPI is **0.2.0**; it already writes **FlatBuffers spec v1** (magic
`ICE🧊CHUNK`, `refs/` ref layout). The PR's GDAL reads a freshly-authored 0.2.0 repo
correctly (`[0 1 2 3 4 5]`). The pre-FlatBuffers **msgpack** format predates 0.2.0 and
was never released on PyPI → **no public msgpack-era repos exist** to be unreadable.
Pre-FlatBuffers / unknown files are rejected gracefully: the header version byte (off
36) must be 1 or 2 (`icechunkutils.cpp:88-95`); an invalid version or corrupt body
yields a clean error (`invalid Snapshot Flatbuffer` / `ZSTD decompression failed`), no
crash.

## E10. Network fault injection (mirrors Earthmover's toxiproxy/rustfs rigor)

Faulted the virtual-chunk fetch and classified GDAL vs icechunk+zarr as OK / loud-error /
silent-fill / silent-corrupt. Two harnesses:

**HTTP-semantic** (`scripts/fault_injection.py`, S3-mock misbehaving per object key):
- 404 → GDAL **SILENT_FILL** (zeros) vs reference LOUD_ERROR ⇒ this is B3, and it is
  *uniquely* the 404/not-found path.
- 500 / 503 / 429 → both LOUD_ERROR.
- truncated stream (Content-Length=full, half sent) → both LOUD_ERROR.
- short Content-Length (honest, half) → both LOUD_ERROR.
- corrupt bytes (valid length) → both return identical wrong data (`-1`) ⇒ shared
  no-checksum gap (S1), not GDAL-specific.

**Transport** (`scripts/fault_injection_toxiproxy.py`, real toxiproxy in front of clean mock):
- latency 300 ms / bandwidth 512 KB/s / slow_close → both OK_CORRECT (tolerated).
- limit_data (cut after 100 KB of 256 KB) → GDAL LOUD_ERROR; **reference OK_CORRECT
  (auto-retries finer-grained)** ⇒ mitigation for GDAL: `GDAL_HTTP_MAX_RETRY>0`.
- reset_peer / timeout → both LOUD_ERROR.

Net: **no silent corruption anywhere except 404 (B3) and the shared no-checksum corrupt-bytes
case (S1).** The driver is robust to the streaming-failure class (truncation / mid-flight cut /
reset) that the blog's fault injection exposed in icechunk.

## E9. Minor findings

- **Repo directory named `repo` is mis-resolved.** `ICECHUNK:<path>/repo` where the
  repository directory is literally `repo` → driver (`icechunkrepo.cpp:206`,
  `CPLGetFilename(...) == "repo"`) treats the path as the repo *file*, not a directory
  → `invalid Repo Flatbuffer`. Renaming the dir fixes it. Real repos are not named
  `repo`, so low impact — but a confusing failure mode.
- **Magic signature not validated.** The 12-byte `ICE🧊CHUNK` signature (bytes 0-11) is
  never checked; the driver keys off the version byte at offset 36 + FlatBuffers
  verification of the body. Robust in practice (FB verify rejects non-icechunk bytes),
  but the documented magic is not enforced.
- **Typo in error string** (`vsiicechunk.cpp`): `cICECHUNK_ALLOW_LOCAL_CHUNK_LOCATION`.
