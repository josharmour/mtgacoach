"""Live inference and RTX PRO 6000 hardware panels for the single AI Usage view."""
DS = {"type": "prometheus", "uid": "prometheus"}
JOB = '{job="vllm-glm53"}'
GPU = '{job="dcgm-gpu",modelName=~".*RTX PRO 6000.*"}'


def target(expr, legend, ref, instant=False):
    return {"datasource": DS, "expr": expr, "legendFormat": legend,
            "refId": ref, "editorMode": "code", "range": not instant,
            "instant": instant}


def panel(pid, title, unit, grid, queries, stat=False, description=""):
    result = {
        "id": pid, "title": title, "description": description,
        "type": "stat" if stat else "timeseries", "datasource": DS,
        "gridPos": dict(zip(("x", "y", "w", "h"), grid)),
        "fieldConfig": {"defaults": {"unit": unit, "decimals": 1,
            "color": {"mode": "thresholds" if stat else "palette-classic"},
            "thresholds": {"mode": "absolute", "steps": [{"color": "green", "value": None}]},
            "mappings": []}, "overrides": []},
        "targets": [target(expr, label, chr(65+i), stat) for i, (expr, label) in enumerate(queries)],
        "options": {"colorMode": "value", "graphMode": "none", "textMode": "value",
                    "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}}
                   if stat else {"legend": {"displayMode": "table", "placement": "bottom", "calcs": ["lastNotNull", "max"]},
                                 "tooltip": {"mode": "multi", "sort": "desc"}},
    }
    if not stat:
        result["fieldConfig"]["defaults"]["custom"] = {
            "drawStyle": "line", "lineInterpolation": "linear", "lineWidth": 2,
            "fillOpacity": 8, "showPoints": "never", "spanNulls": False,
            "axisPlacement": "auto", "stacking": {"mode": "none", "group": "A"},
        }
    return result


def rate(metric, window="1m"):
    return f'sum(rate(vllm:{metric}{JOB}[{window}]))'


# Zoom-adaptive rate window for range charts: Grafana widens it with the
# selected time range, so a 14d/90d view still yields nonzero per-point
# rates instead of a fixed [1m] rate that downsamples to ~0.
RI = "$__rate_interval"


def percentile(metric, q, window="5m"):
    return f'histogram_quantile({q}, sum by (le) (rate(vllm:{metric}_bucket{JOB}[{window}])))'


def active_rate(metric, window=RI, step="30s"):
    """tok/s while actively serving: token rate over the window divided by
    the fraction of the window with >=1 running request, so idle seconds
    never dilute the number (zooming out to weeks keeps real workload
    throughput instead of decaying toward 0). Falls back to an explicit 0
    when the engine was idle for the whole window (no div-by-zero gaps)."""
    num = f'sum(rate(vllm:{metric}{JOB}[{window}]))'
    den = (f'avg_over_time((sum(vllm:num_requests_running{JOB}) > bool 0)'
           f'[{window}:{step}])')
    return (f'(({num}) / ({den}) and ({den}) > 0)'
            f' or on() (sum(vllm:num_requests_running{JOB}) * 0)')


def build_performance_panels():
    output = active_rate('generation_tokens_total', '1m', '5s')
    prompt = active_rate('prompt_tokens_total', '1m', '5s')
    running = f'sum(vllm:num_requests_running{JOB})'
    waiting = f'sum(vllm:num_requests_waiting{JOB})'
    decode = f"{rate('request_time_per_output_token_seconds_count', '5m')} / {rate('request_time_per_output_token_seconds_sum', '5m')}"
    specs = [
        ('Output tok/s (total)', 'suffix: tok/s', output, 'Live output tok/s while the engine is actively serving (last minute, idle seconds excluded from the denominator). Zoom-independent: the same number at any dashboard range.'),
        ('Input tok/s (total)', 'suffix: tok/s', prompt, 'Live input tok/s while actively serving (last minute, idle seconds excluded), including cached prompt tokens. Zoom-independent.'),
        ('Active requests', 'short', running, 'Requests currently executing in vLLM; these may not appear in completed LiteLLM logs yet.'),
        ('Queued requests', 'short', waiting, 'Requests waiting for vLLM scheduling capacity.'),
        ('Decode tok/s / request', 'suffix: tok/s', decode, 'Reciprocal of mean per-request time per output token over five minutes. Excludes prefill; no value when there are no recent completed requests.'),
        ('Inference metrics', 'none', 'max(up{job="vllm-glm53"})', 'Prometheus reachability of the private backend metrics endpoint. This is not a full completion test.'),
    ]
    panels = [panel(4100+i, title, unit, (i*4, 4, 4, 4), [(expr, '')], True, desc)
              for i, (title, unit, expr, desc) in enumerate(specs)]
    panels[-1]['fieldConfig']['defaults']['mappings'] = [{"type": "value", "options": {
        "0": {"text": "DOWN", "color": "red"}, "1": {"text": "UP", "color": "green"}}}]
    charts = [
        ('RTX PRO 6000 — GPU utilization', 'percent', [(f'DCGM_FI_DEV_GPU_UTIL{GPU}', 'GPU {{gpu}}')], 'All activity on the cards, including model loading, startup warm-up and jobs outside LiteLLM. Gaps indicate missing telemetry, not idle GPUs.'),
        ('RTX PRO 6000 — VRAM used', 'bytes', [(f'DCGM_FI_DEV_FB_USED{GPU} * 1024 * 1024', 'GPU {{gpu}}')], 'Allocated GPU memory, including model weights and KV cache reservations.'),
        ('RTX PRO 6000 — power', 'watt', [(f'DCGM_FI_DEV_POWER_USAGE{GPU}', 'GPU {{gpu}}')], 'Board power reported by NVIDIA DCGM.'),
        ('RTX PRO 6000 — temperature', 'celsius', [(f'DCGM_FI_DEV_GPU_TEMP{GPU}', 'GPU {{gpu}}')], ''),
        ('Inference throughput — tokens / second', 'suffix: tok/s', [(active_rate('generation_tokens_total', RI, '5s'), 'Output'), (active_rate('prompt_tokens_total', RI, '5s'), 'Input')], 'Per-point tok/s while actively serving: the rate window is scrape-safe ($__rate_interval) and idle seconds are excluded via the running-request fraction, so zooming out to weeks keeps real workload throughput instead of decaying toward 0. Idle spans show as 0. Stat tiles above are the pure live last-minute rates.'),
        ('Request latency — p50 / p95', 's', [(percentile('time_to_first_token_seconds', .5, RI), 'First token p50'), (percentile('time_to_first_token_seconds', .95, RI), 'First token p95'), (percentile('e2e_request_latency_seconds', .95, RI), 'End-to-end p95')], 'Histogram percentiles with a zoom-adaptive rate window. No value when there are no observations in range.'),
        ('Live requests — running / queued', 'short', [(running, 'Running'), (waiting, 'Queued')], 'Live engine state, including requests that have not completed yet.'),
        ('KV cache / prefix cache hit rate', 'percentunit', [(f'max(vllm:kv_cache_usage_perc{JOB})', 'KV cache used'), (f"{rate('prefix_cache_hits_total','5m')} / {rate('prefix_cache_queries_total','5m')}", 'Prefix hit rate')], 'KV occupancy and five-minute token-weighted prefix-cache hit ratio. Hit rate is undefined when no cache queries occurred.'),
    ]
    for i, (title, unit, queries, desc) in enumerate(charts):
        panels.append(panel(4200+i, title, unit, ((i%2)*12, 12+(i//2)*7, 12, 7), queries, description=desc))
    status = [
        ('Proxy-only access', 'ai_gateway_enforcement', {"0": {"text": "CHECK ROUTING", "color": "red"}, "1": {"text": "ENFORCED", "color": "green"}}),
        ('Model readiness', 'ai_backend_ready', {"0": {"text": "NOT READY", "color": "orange"}, "1": {"text": "READY", "color": "green"}}),
        ('GPU processes', 'ai_gpu_process_count', {}),
    ]
    for i, (title, metric, values) in enumerate(status):
        expr = f'{metric} and on(job, instance) (up{{job="ai-gpu-processes"}} == 1)'
        item = panel(4300+i, title, 'none', (i*8, 8, 8, 4), [(expr, '')], True,
                     'Live host inspection. Missing data means telemetry is unavailable. Readiness checks the active backend’s authenticated models endpoint.')
        if values: item['fieldConfig']['defaults']['mappings'] = [{"type": "value", "options": values}]
        panels.append(item)
    processes = panel(4310, 'GPU processes — all compute workloads', 'bytes', (0, 40, 24, 7),
        [('ai_gpu_process_memory_bytes * on(UUID) group_left(gpu) (DCGM_FI_DEV_GPU_UTIL{job="dcgm-gpu"} * 0 + 1)', '')], True,
        'Live NVIDIA compute processes, including jobs that do not use LiteLLM. Root-owned VLLM workers are normal Docker processes. No rows can mean no processes; check GPU processes and exporter health above.')
    processes['type'] = 'table'
    processes['targets'][0]['format'] = 'table'
    processes['options'] = {"showHeader": True, "cellHeight": "sm"}
    processes['fieldConfig']['defaults']['unit'] = 'none'
    processes['fieldConfig']['overrides'] = [{"matcher": {"id": "byName", "options": "Value"}, "properties": [{"id": "unit", "value": "bytes"}, {"id": "displayName", "value": "VRAM"}]}]
    processes['transformations'] = [{"id": "organize", "options": {
        "excludeByName": {"Time": True, "__name__": True, "UUID": True, "host": True, "instance": True, "job": True},
        "indexByName": {"gpu": 0, "pid": 1, "process": 2, "owner": 3, "Value": 4},
    }}]
    panels.append(processes)
    return panels
