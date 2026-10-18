#!/usr/bin/env bash
# Run the test suite from a local-disk mirror, in parallel.
#
# The repo lives on a network share; reading ~500 source/test files and the
# venv's packages over SMB made a full run take 2.5-3 min (one `import openai`
# alone took 34 s). From local disk with pytest-xdist it takes ~20 s.
#
#   scripts/test_fast.sh                     # whole suite
#   scripts/test_fast.sh tests/test_foo.py   # any pytest args
#
# Works from any worktree: it mirrors the checkout you run it from.
# Python: $MTGACOACH_TEST_PYTHON, else ~/.venvs/mtgacoach-dev (created with uv
# on first run from pyproject's dependencies + dev extras + pytest-xdist).
set -euo pipefail

src="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
git_dir="$(git -C "$src" rev-parse --absolute-git-dir)"
key="$(printf '%s' "$src" | shasum | cut -c1-12)"
mirror="${XDG_CACHE_HOME:-$HOME/.cache}/mtgacoach-testrun/$key"
venv="$HOME/.venvs/mtgacoach-dev"
py="${MTGACOACH_TEST_PYTHON:-$venv/bin/python}"

if [[ ! -x "$py" ]]; then
  uv="$(command -v uv || echo "$HOME/.local/bin/uv")"
  echo "Creating $venv (one time)..."
  "$uv" venv -q --python 3.12 "$venv"
  "$uv" pip install -q --python "$venv/bin/python" -r "$src/pyproject.toml" --extra dev pytest-xdist
  py="$venv/bin/python"
fi

mkdir -p "$mirror"
rsync -a --delete \
  --exclude .git --exclude '.venv*' --exclude __pycache__ --exclude .claude \
  --exclude re-output --exclude dist --exclude build --exclude node_modules \
  "$src/" "$mirror/"

cd "$mirror"
[[ $# -eq 0 ]] && set -- tests
# GIT_DIR: a few tests read history (git show <rev>:path) from the real repo.
GIT_DIR="$git_dir" PYTHONPATH="$mirror/src" QT_QPA_PLATFORM=offscreen \
  exec "$py" -m pytest -q -p no:cacheprovider -n auto "$@"
