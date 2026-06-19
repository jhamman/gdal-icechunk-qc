# QC of the GDAL Icechunk read-only driver (OSGeo/gdal PR #14755)

Independent quality-control of [OSGeo/gdal #14755](https://github.com/OSGeo/gdal/pull/14755)
— "Add Icechunk read-only driver" (Even Rouault). The driver lets GDAL, and everything
that binds to it (Python `osgeo.gdal`, rasterio/rioxarray, the `gdal` CLI), read
[Icechunk](https://icechunk.io) repositories on local or cloud storage without going
through the Python `icechunk`/`xarray` stack.

This repo contains the **full QC report**, the **reproducible experiments** behind every
finding, and the tooling to rebuild the (unreleased) PR from source.

> **Reviewed at** `rouault/gdal@icechunk`, commit `8c2b212`, target GDAL 3.14.
> Ground truth: `icechunk` 2.0.6 (spec v2) + `zarr` 3.2.1 + `xarray`.

## TL;DR verdict

**Well-engineered and correct on the cases that matter** (native + virtual chunks, v1/v2
layouts, anonymous S3, the Zarr-delegated read path; bundled suite 74/74; **200/200**
randomized dtype×codec×shape trials match `zarr-python` exactly). **Not ready to merge as-is**
— three blockers, all producing *silent wrong data* or blocking sample datasets:

| ID | Blocker | Effect |
|----|---------|--------|
| **B1** | `?branch=`/`?tag=` dropped before delegation | non-`main` refs **silently return `main`'s data** |
| **B2** | manifest FlatBuffers verifier uses default `max_tables` | arrays with **≥1,000,000 chunk refs/manifest** unreadable (e.g. GLAD `lclu`); bisected to exactly 1e6 |
| **B3** | a manifest-referenced virtual chunk that 404s is treated as an absent/sparse chunk | missing object / missing credentials → **silently reads as zeros** (the reference stack *raises*) |

Full detail, evidence, spec-coverage matrix, and prioritized TODOs: **[`QC_REPORT.md`](QC_REPORT.md)**.
Round-2 deep-QC raw notes: [`qc/phaseE_deep_qc_findings.md`](qc/phaseE_deep_qc_findings.md).

## Reproduce from scratch

**Prerequisites:** `git`, a C/C++ toolchain bootstrap, and **micromamba** (or conda/mamba).
Optional: [`toxiproxy`](https://github.com/Shopify/toxiproxy) for the transport fault tests
(`brew install toxiproxy`). Tested on macOS arm64 (Darwin 25); Linux should work via the
same conda toolchain.

```bash
git clone jhamman/gdal-icechunk-qc && cd gdal-icechunk-qc

bash setup.sh            # create env, clone+build the PR, verify the driver, (opt) rasterio
                         #   -> takes 10-30 min (mostly the GDAL build)

source scripts/env.sh    # put the freshly-built GDAL on PATH for this shell
bash run_all.sh          # regenerate fixtures + run every experiment
                         #   SKIP_SLOW=1 skips the ~8-min B2 bisection
                         #   SKIP_LIVE=1 skips the network cross-validation
```

`setup.sh` is idempotent (re-running skips the env/clone/build if already present). To force
a clean rebuild: `rm -rf build install gdal`.

### What's *not* committed (and why)

`gdal/` (the PR checkout, 267 MB), `build*/`, `install*/` (build artifacts), and `fixtures/`
(synthetic Icechunk repos, 55 MB) are all **regenerated** by `setup.sh` / `run_all.sh`, so
they're git-ignored. The repo is just the code, docs, and evidence logs.

## Experiments (all under `scripts/`)

| Script | What it checks | Report § |
|--------|----------------|----------|
| `make_synthetic.py` | builds local fixtures (native/inline/virtual, multi-ref, scalar, hierarchy, corrupt, unsupported-codec) | §4 |
| `compare.py` | GDAL multidim vs `icechunk`+`xarray` on live S3 (GLAD/GFS/RASI) | §4 (C4/C6/C7) |
| `run_synthetic_checks.sh` | offline checks incl. the **B1** branch/tag repro | §C2 |
| `demo_rasterio_rioxarray.py` | the real user workflow (open repo → subdatasets → read) | §C8 |
| `bench.py` / `run_bench.sh` | light perf vs `zarr-python`/`xarray` | §C9 |
| `test_virtual_chunks.py` | **B3** — silent fill on inaccessible virtual chunks | §6.2 |
| `property_test.py` | 200 randomized dtype×codec×shape trials vs `zarr-python` | §6.3 |
| `bisect_b2.py` | bisects the **B2** 1,000,000-ref manifest threshold | §6.1 |
| `test_concurrency_leak.py` | concurrent reads + per-open/read memory leak | §6.4–6.5 |
| `fault_injection.py` | HTTP-semantic faults (404/5xx/truncate/short/corrupt) | §6.9a |
| `fault_injection_toxiproxy.py` | transport faults via **toxiproxy** (latency/LimitData/SlowClose/reset/timeout) | §6.9b |
| `run_asan_checks.sh` | ASan/UBSan run over fixtures (build via the sanitizer tree) | §6.6 |

## Layout

```
QC_REPORT.md                 the deliverable: findings, spec matrix, workflows, TODOs
README.md                    this file
CLAUDE.md                    agent-facing notes (conventions + env gotchas)
setup.sh / run_all.sh        build the PR / run every experiment
env/environment.yml          conda/micromamba build + ground-truth env
scripts/                     env.sh + all experiment scripts
qc/                          raw result logs (*.txt/*.jsonl) + phase notes (*.md)
```

## Caveats

This QC was performed on a single macOS arm64 machine. The ASan/UBSan run did **not** complete
on this conda-clang toolchain (instrumented startup is pathologically slow, §6.6) — re-running
it on Linux/system-clang is the recommended way to close that gap. The performance numbers (§C9)
are deliberately *indicative* (single-threaded, single slice). Live S3 datasets are public and
anonymous as of June 2026; bucket contents may drift.
