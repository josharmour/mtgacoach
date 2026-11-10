#!/usr/bin/env bash
# Add OpenRouter models to the LiteLLM gateway for owner-only head-to-head tests.
#
# Run on blackwell (10.0.0.10). UNTESTED as written on 2026-11-09 against
# LiteLLM v1.106.4: run with --dry-run first and read what it would send.
#
# The OpenRouter key reaches the gateway one of two ways:
#   --key-stdin   read the key from standard input and store it in each model
#                 entry (the gateway encrypts model parameters in its database).
#                 No container restart. Example, run from the machine holding
#                 the key file, which never displays it:
#                   grep -ao 'sk-or-v1-[A-Za-z0-9]*' key-file | head -1 \
#                     | ssh blackwell 'bash add_openrouter_test_models.sh --key-stdin'
#   (default)     refer to "os.environ/OPENROUTER_API_KEY", which must then be
#                 set in the litellm container's environment (needs a recreate).
#
# The script never prints the key, never puts it on a command line, and prints
# only each call's HTTP status.
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
KEY_STDIN=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=1 ;;
    --remove) REMOVE=1 ;;
    --key-stdin) KEY_STDIN=1 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

# gateway model name | OpenRouter model id   (prices per million tokens, 2026-11-09)
MODELS=(
  "or-gemma-4-26b|google/gemma-4-26b-a4b-it"          # in $0.08  out $0.10
  "or-gemma-4-26b-free|google/gemma-4-26b-a4b-it:free"
  "or-gemma-4-31b|google/gemma-4-31b-it"              # in $0.20  out $0.40
  "or-gemma-4-31b-free|google/gemma-4-31b-it:free"
  "or-qwen3.8-27b|qwen/qwen3.8-27b"                   # in $0.15  out $1.875
  "or-qwen3.6-35b-a3b|qwen/qwen3.6-35b-a3b"           # in $0.10  out $0.90
  "or-qwen3.7-flash|qwen/qwen3.7-flash"               # in $0.03  out $0.13
  "or-glm-5.3-flash|z-ai/glm-5.3-flash"               # in $0.15  out $0.50  (hosted control for the local model)
)

MASTER=$(sudo docker exec litellm printenv LITELLM_MASTER_KEY)
[[ -n "$MASTER" ]] || { echo "LITELLM_MASTER_KEY is not set in the litellm container" >&2; exit 1; }

export OR_KEY_REF="os.environ/OPENROUTER_API_KEY"
if [[ $KEY_STDIN -eq 1 ]]; then
  IFS= read -r OR_KEY_REF || true
  [[ "$OR_KEY_REF" == sk-or-* ]] || { echo "standard input did not hold an OpenRouter key" >&2; exit 1; }
  export OR_KEY_REF
elif [[ $REMOVE -eq 0 && $DRY -eq 0 ]]; then
  sudo docker exec litellm sh -c '[ -n "${OPENROUTER_API_KEY:-}" ]' \
    || { echo "OPENROUTER_API_KEY is not set in the litellm container; use --key-stdin or see the header" >&2; exit 1; }
fi

post() { # path, JSON on standard input; prints the HTTP status only
  curl -sS -m 30 -o /dev/null -w '%{http_code}' -X POST "$GATEWAY$1" \
    -H "Authorization: Bearer $MASTER" -H 'Content-Type: application/json' --data @-
}

EXISTING=$(curl -sS -m 30 "$GATEWAY/model/info" -H "Authorization: Bearer $MASTER" \
  | python3 -c "import sys,json; print('\n'.join(m['model_name']+'|'+m['model_info']['id'] for m in json.load(sys.stdin)['data']))")

for entry in "${MODELS[@]}"; do
  name=${entry%%|*}; rest=${entry#*|}; model=${rest%% *}
  id=$(printf '%s\n' "$EXISTING" | awk -F'|' -v n="$name" '$1==n{print $2; exit}')
  if [[ $REMOVE -eq 1 ]]; then
    if [[ -z "$id" ]]; then echo "$name: not present"; continue; fi
    if [[ $DRY -eq 1 ]]; then echo "DRY RUN: would delete $name"; continue; fi
    echo "$name: delete -> HTTP $(printf '{"id": "%s"}' "$id" | post /model/delete)"
    continue
  fi
  if [[ -n "$id" ]]; then echo "$name: already present, skipped"; continue; fi
  if [[ $DRY -eq 1 ]]; then echo "DRY RUN: would add $name -> openrouter/$model (group $GROUP)"; continue; fi
  status=$(NAME="$name" MODEL="$model" GROUP="$GROUP" python3 -c '
import json, os
print(json.dumps({
    "model_name": os.environ["NAME"],
    "litellm_params": {"model": "openrouter/" + os.environ["MODEL"], "api_key": os.environ["OR_KEY_REF"]},
    "model_info": {"access_groups": [os.environ["GROUP"]]},
}))' | post /model/new)
  echo "$name: add -> HTTP $status"
done

[[ $REMOVE -eq 1 ]] || echo "Done. Add the access group '$GROUP' to your own developer key, then test one call per model."
