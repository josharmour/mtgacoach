#!/usr/bin/env bash
#
# play-real.sh — autonomous real-match data collection (MTGA "Play vs AI").
#
# Drives the vLLM model through real vs-AI matches via the GRE bridge +
# autopilot, recording decision trajectories for training. Robust to being
# invoked from any working directory.
#
# Usage:
#   ./play-real.sh                 # one match, default vLLM backend
#   ./play-real.sh --matches 10    # extra args are passed straight through
#   ./play-real.sh --dry-run       # preflight only (no launch, no match)
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PY="${REPO_DIR}/.venv/bin/python"

if [[ ! -x "${VENV_PY}" ]]; then
    echo "error: venv interpreter not found at ${VENV_PY}" >&2
    echo "       create it first (e.g. python -m venv .venv && pip install -e .[dev,full])" >&2
    exit 1
fi

echo "=============================================="
echo " mtgacoach real-match data collection"
echo " repo:   ${REPO_DIR}"
echo " python: ${VENV_PY}"
echo "=============================================="

cd "${REPO_DIR}"
# -u: unbuffered, so decision logs stream live even when redirected to a file.
exec "${VENV_PY}" -u -m arenamcp.play_real_matches "$@"
