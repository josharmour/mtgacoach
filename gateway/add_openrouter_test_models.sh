#!/usr/bin/env bash
# Add OpenRouter models to the LiteLLM gateway for owner-only head-to-head tests.
#
# Run on blackwell (10.0.0.10). UNTESTED as written on 2026-11-09 against
# LiteLLM v1.106.4: run with --dry-run first and read what it would send.
#
# Before running, the owner must:
#   1. Create an OpenRouter account and API key (and buy credits if paid models
#      are wanted; the free ":free" variants allow about 1,000 requests a day
#      once $10 of credits has ever been bought, 50 a day otherwise).
#   2. Put OPENROUTER_API_KEY=<key> in the litellm service's environment in the
#      compose stack and recreate the litellm container.
#
# This script never reads, prints or stores the OpenRouter key. Each model entry
# refers to it as "os.environ/OPENROUTER_API_KEY", resolved inside the gateway.
#
# Every entry is placed in the access group "openrouter-test". No existing key
# has that group, so no customer can call these models. Afterwards, add the
# group to the owner's own developer key in the gateway admin page
# (https://api.mtgacoach.com/ui -> Virtual Keys -> edit -> Models).
#
# Only the owner's own games should be played against these models: prompts
# leave the local network, and free variants may be logged by their providers.
#
# Remove them again with:  ./add_openrouter_test_models.sh --remove
set -euo pipefail

GATEWAY=${GATEWAY:-http://localhost:8444}
GROUP=openrouter-test
DRY=0
REMOVE=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=1 ;;
    --remove) REMOVE=1 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

# gateway model name | OpenRouter model id   (prices per million tokens, 2026-11-09)
MODELS=(
  "or-gemma-4-26b|google/gemma-4-26b-a4b-it"          # in \$0.08  out \$0.10
  "or-gemma-4-26b-free|google/gemma-4-26b-a4b-it:free"
  "or-gemma-4-31b|google/gemma-4-31b-it"              # in \$0.20  out \$0.40
  "or-gemma-4-31b-free|google/gemma-4-31b-it:free"
  "or-qwen3.8-27b|qwen/qwen3.8-27b"                   # in \$0.15  out \$1.875
  "or-qwen3.6-35b-a3b|qwen/qwen3.6-35b-a3b"           # in \$0.10  out \$0.90
  "or-qwen3.7-flash|qwen/qwen3.7-flash"               # in \$0.03  out \$0.13
  "or-glm-5.3-flash|z-ai/glm-5.3-flash"               # in \$0.15  out \$0.50  (hosted control for the local model)
)

MASTER=$(sudo docker exec litellm printenv LITELLM_MASTER_KEY)
[[ -n "$MASTER" ]] || { echo "LITELLM_MASTER_KEY is not set in the litellm container" >&2; exit 1; }
if [[ $REMOVE -eq 0 && $DRY -eq 0 ]]; then
  sudo docker exec litellm sh -c '[ -n "${OPENROUTER_API_KEY:-}" ]' \
    || { echo "OPENROUTER_API_KEY is not set in the litellm container; see the header of this script" >&2; exit 1; }
fi

call() { # method path json
  if [[ $DRY -eq 1 ]]; then
    echo "DRY RUN: $1 $GATEWAY$2"; echo "  $3"
  else
    curl -sS -m 30 -X "$1" "$GATEWAY$2" -H "Authorization: Bearer $MASTER" \
      -H 'Content-Type: application/json' -d "$3" | head -c 400; echo
  fi
}

for entry in "${MODELS[@]}"; do
  name=${entry%%|*}; rest=${entry#*|}; model=${rest%% *}
  if [[ $REMOVE -eq 1 ]]; then
    id=$(curl -sS -m 30 "$GATEWAY/model/info" -H "Authorization: Bearer $MASTER" \
      | python3 -c "import sys,json; print(next((m['model_info']['id'] for m in json.load(sys.stdin)['data'] if m['model_name']=='$name'),''))")
    [[ -n "$id" ]] && call POST /model/delete "{\"id\": \"$id\"}" || echo "$name: not present"
  else
    call POST /model/new "{\"model_name\": \"$name\", \"litellm_params\": {\"model\": \"openrouter/$model\", \"api_key\": \"os.environ/OPENROUTER_API_KEY\"}, \"model_info\": {\"access_groups\": [\"$GROUP\"]}}"
  fi
done

[[ $REMOVE -eq 1 ]] || echo "Done. Add the access group '$GROUP' to your own developer key, then test one call per model."
