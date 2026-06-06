#!/usr/bin/env bash
#
# selfplay.sh — launch the headless MTGA self-play orchestrator.
#
# Runs the bot-vs-bot self-play loop over the TCP bridge (no mouse/screen).
# Robust to being invoked from any working directory.
#
# Usage:
#   ./selfplay.sh                 # default run
#   ./selfplay.sh --matches 10    # extra args are passed straight through
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
echo " mtgacoach self-play (headless, TCP bridge)"
echo " repo:   ${REPO_DIR}"
echo " python: ${VENV_PY}"
echo "=============================================="

cd "${REPO_DIR}"
exec "${VENV_PY}" -m arenamcp.self_play --auto "$@"
