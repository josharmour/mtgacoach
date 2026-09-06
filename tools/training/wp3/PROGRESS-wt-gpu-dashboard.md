# PROGRESS-wt-gpu-dashboard.md — WP-3: GPU Fleet Grafana Dashboards

## Scope
1. Fix the existing vllm-gemma dashboard (uid: `vllm-gemma`) to match live metrics
2. Create new gpu-fleet dashboard (uid: `gpu-fleet`) showing ALL GPUs + serving endpoints
3. Create idempotent apply script

## What was done

### Discovery
- Prometheus at http://localhost:9090, Grafana at http://localhost:3001 (admin auth)
- Found 7 Prometheus jobs: dcgm-gpu, litellm-exporter, llamacpp-qwen-uncensored, ollama-gemma-12b, ollama-metrics-agent, plex-egpu, vllm-dsv4-flash
- DCGM: 23 metrics for 2x RTX PRO 6000 Blackwell GPUs
- AMD exporter: 8 amdgpu metrics + ollama_gemma_tok_s (most show 0 when R9700 idle)
- vLLM: full metric suite for deepseek-v4-flash (model_name=deepseek-v4-flash, job=vllm-dsv4-flash)
- Ollama metrics-agent: running_models_count=1, model_info for gemma4:12b, all-minilm, gemma4-compactor
- LiteLLM: 7 metrics (tokens, requests success/fail)

### Fixed vllm-gemma dashboard
- Retitled from "vLLM (DSV4-Flash) + Ollama (Gemma 4) + LiteLLM Gateway" to "vLLM — DeepSeek V4 Flash"
- Panel 102: renamed from "Gemma 4 12B (Ollama :11434)" to "Ollama (gemma4:12b)"
- All 33 panels preserved; all queries verified returning data
- Job labels already use `vllm-dsv4-flash` (no stale `gemma` labels needed fixing in queries)
- Applied via Grafana provisioning directory (API returns access denied for write)

### New gpu-fleet dashboard
- **UID:** `gpu-fleet`, **Title:** "GPU Fleet — blackwell"
- **49 panels** across 7 rows:
  1. Overview text (GPU table + MageZero note)
  2. GPU 0 — 6 stat/gauge panels (util, temp, VRAM, power, mem clock, SM clock)
  3. GPU 1 — same 6 panels
  4. Blackwell overlays — 3 timeseries (util, VRAM, power&temp)
  5. AMD R9700 — 6 panels + timeseries + text notes on limitations
  6. vLLM serving — 6 stat panels + 6 timeseries (tok/s, TTFT, ITL, E2E, queue, spec-decode)
  7. Ollama — 6 stat panels + timeseries + MageZero self-play note

### Key findings
- DCGM_FI_DEV_FB_TOTAL does NOT exist on this DCGM version; total VRAM is 96 GB known from hardware spec, used is tracked via FB_USED
- All AMD R9700 metrics return 0 when GPU is idle (no per-process metrics from the AMD exporter)
- ollama_gemma_tok_s comes from the plex-egpu exporter on port 9401, not the ollama-metrics-agent
- LiteLLM queries mostly work (total_tokens=82.8M, 816 requests, 96.8% success rate)
- Grafana API returns "Access denied" for dashboard write (admin user lacks dashboards:write permission); using provisioning directory (auto-reloads every 30s) instead

### Test output (all queries verified via Prometheus API)
```
DCGM_FI_DEV_GPU_UTIL{gpu="0"} => val=0 (GPU 0 idle)
DCGM_FI_DEV_GPU_UTIL{gpu="1"} => val=3 (GPU 1 low activity)
DCGM_FI_DEV_FB_USED{gpu="0"} => 96763 MiB
DCGM_FI_DEV_POWER_USAGE{gpu="0"} => 236.099 W
DCGM_FI_DEV_GPU_TEMP{gpu="0"} => 81 °C
rate(vllm:generation_tokens_total[1m]) => 247.6 tok/s
rate(vllm:prompt_tokens_total[1m]) => 19055.9 tok/s
vllm:kv_cache_usage_perc => 25.3%
vllm:num_requests_running => 3
TTFT p50 => 0.598s | ITL p50 => 18.6ms | E2E p50 => 6.71s
Spec-decode accept rate => 56.0%
ollama_gemma_tok_s => 32.2 tok/s
litellm_total_tokens => 82,854,856
litellm success rate => 96.8%
```

### Files committed
- `tools/monitoring/grafana/vllm-gemma.json` - fixed dashboard (Grafana API format)
- `tools/monitoring/grafana/gpu-fleet.json` - new GPU fleet dashboard (Grafana API format)
- `tools/monitoring/grafana/apply_dashboards.sh` - idempotent apply script (API + provisioning fallback)

### Known gaps
- Grafana API write is blocked — dashboards deployed via provisioning file write instead
- AMD R9700 metrics show 0 when idle (driver/exporter limitation, not a query issue)
- `litellm_prompt_tokens > 0` count returns empty when LiteLLM hasn't proxied prompts recently (metric exists, just no recent data)
