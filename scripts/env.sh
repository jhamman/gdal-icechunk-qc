# Source this to use the freshly-built GDAL (PR #14755) inside the build env.
#     source scripts/env.sh
#
# Portable: derives the repo root from this file's location and resolves the conda/
# micromamba env. Override before sourcing if your layout differs:
#     GDALIC_ENV            env name                       (default: gdalic)
#     MAMBA_ROOT_PREFIX     micromamba root                (default: $HOME/micromamba)
#     CONDA_PREFIX_GDALIC   explicit env prefix            (overrides the two above)

# --- locate this script (works under bash and zsh) ---
if [ -n "${ZSH_VERSION:-}" ]; then
  eval '_ENV_SH="${(%):-%x}"'
else
  _ENV_SH="${BASH_SOURCE[0]}"
fi
REPO_ROOT="$(cd "$(dirname "$_ENV_SH")/.." && pwd)"

: "${GDALIC_ENV:=gdalic}"
: "${MAMBA_ROOT_PREFIX:=$HOME/micromamba}"
export MAMBA_ROOT_PREFIX

# --- resolve the conda/micromamba env prefix ---
if [ -z "${CONDA_PREFIX_GDALIC:-}" ]; then
  if [ -d "$MAMBA_ROOT_PREFIX/envs/$GDALIC_ENV" ]; then
    CONDA_PREFIX_GDALIC="$MAMBA_ROOT_PREFIX/envs/$GDALIC_ENV"
  elif command -v micromamba >/dev/null 2>&1; then
    CONDA_PREFIX_GDALIC="$(micromamba env list 2>/dev/null | awk -v e="$GDALIC_ENV" '$1==e{print $NF}' | head -1)"
  fi
fi
: "${CONDA_PREFIX_GDALIC:=${CONDA_PREFIX:-}}"
export CONDA_PREFIX_GDALIC

if [ ! -x "$CONDA_PREFIX_GDALIC/bin/python" ]; then
  echo "env.sh: could not find the '$GDALIC_ENV' env (looked in '$CONDA_PREFIX_GDALIC')." >&2
  echo "        create it with:  bash setup.sh   (or set CONDA_PREFIX_GDALIC=/path/to/env)" >&2
fi

# --- the freshly-built GDAL install (repo-relative) ---
export GDAL_INSTALL="$REPO_ROOT/install"
export PATH="$GDAL_INSTALL/bin:$PATH"
# macOS: ONLY install/lib. Do NOT add conda/lib here -- conda's libiconv exports
# `_libiconv` (not `_iconv`) and would shadow the system libiconv that Homebrew bash
# and other system binaries need, crashing any child `bash` spawned from these scripts.
# libgdal's conda deps resolve via its baked @rpath (which already includes conda/lib).
export DYLD_LIBRARY_PATH="$GDAL_INSTALL/lib:${DYLD_LIBRARY_PATH:-}"
# Linux: iconv lives in glibc (no separate libiconv to shadow), so conda/lib is safe
# and sometimes needed when rpath is relative.
export LD_LIBRARY_PATH="$GDAL_INSTALL/lib:$CONDA_PREFIX_GDALIC/lib:${LD_LIBRARY_PATH:-}"
export GDAL_DATA="$GDAL_INSTALL/share/gdal"
export PROJ_DATA="$CONDA_PREFIX_GDALIC/share/proj"

# python bindings live under the install's pythonX.Y/site-packages
_PYSP="$(ls -d "$GDAL_INSTALL"/lib/python*/site-packages 2>/dev/null | head -1)"
export PYTHONPATH="${_PYSP:-$GDAL_INSTALL/lib/python3.12/site-packages}:${PYTHONPATH:-}"

# the env's python, with osgeo importable via PYTHONPATH (call this directly, NOT
# `micromamba run`, which re-pollutes DYLD and breaks the custom libgdal — see CLAUDE.md)
export ICPY="$CONDA_PREFIX_GDALIC/bin/python"

# anonymous public S3 by default (all live datasets are anonymous)
export AWS_NO_SIGN_REQUEST=YES
