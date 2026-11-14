#!/usr/bin/env python3
"""Generate grafana/dashboards/gateway-usage.json.

Panel factories keep the dashboard JSON typo-free; edit here and re-run
(Grafana re-provisions every 30s). Queries rely on the Infinity datasource's
backend JSON parser with explicit time-series field types.
"""
import json
import os
import zlib
from ai_performance_panels import build_performance_panels
from api_cost import NOTE as PRICING_NOTE
from dashboard_snapshot import arrange_snapshot

OUT = os.path.join(os.path.dirname(__file__), "grafana", "dashboards",
                   "gateway-usage.json")
UID = "ai-usage"
DS = {"type": "yesoreyeram-infinity-datasource", "uid": "usage-api"}
API = "http://10.0.0.10:8013"
LANGFUSE = "http://10.0.0.10:3100"
SOBRIETY_API = "http://10.0.0.10:8090"  # sobriety-copilot prod nginx

SELF_LINK = {
    "title": "Langfuse — payload drill-down",
    "type": "link",
    "url": f"{LANGFUSE}/project/default/traces?spanHours=24",
    "targetBlank": True,
}


def inf(url, fmt="table", refid="A", max_rows=500):
    """Infinity backend JSON query with server-side time macros."""
    return {
        "datasource": DS,
        "format": fmt,
        "refId": refid,
        "rootSelector": "",
        "parser": "backend",
        "source": "url",
        "type": "json",
        "url": url,
        "url_options": {"data": "", "method": "GET"},
        "maxRows": max_rows,
    }


def stat(title, url, field, unit, fmt_map=None, grid=(6, 0, 0, 4), decimals=None,
         color_mode="value"):
    """Stat panel fed by one filtered field of a one-row totals frame."""
    return {
        "datasource": DS,
        "fieldConfig": {
            "defaults": {
                # "background"/"value" are only valid as stat *options*.colorMode,
                # not fieldConfig color modes (crashes the panel in Grafana 11.6).
                "color": {"mode": "thresholds"},
                "decimals": decimals,
                "mappings": [],
                "thresholds": {
                    "mode": "absolute",
                    "steps": [
                        {"color": "green", "value": None},
                    ],
                },
                "unit": unit,
            },
            "overrides": [],
        },
        "gridPos": {"h": grid[3], "w": grid[0], "x": grid[1], "y": grid[2]},
        "id": zlib.crc32((title + field).encode()) % 100000 + 1,
        "options": {
            "colorMode": color_mode,
            "graphMode": "none",
            "justifyMode": "auto",
            "orientation": "auto",
            "percentChangeColorMode": "standard",
            "reduceOptions": {
                "calcs": ["lastNotNull"],
                "fields": "/^%s$/" % field,
                "values": False,
            },
            "showPercentChange": False,
            "textMode": "auto",
            "wideLayout": True,
        },
        "targets": [inf(url, "table")],
        "title": title,
        "type": "stat",
    }


def ts_panel(title, yid, ytitle, field, grid, span_expr=None, fill=8, line=2,
             stack=False, legend_placement="bottomh"):
    """Time-series panel fed by the long-format /api/timeseries array,
    split into one series per key_alias via partitionByValues."""
    url = API + "/api/timeseries?from=${__timeFrom}&to=${__timeTo}"
    return {
        "datasource": DS,
        "fieldConfig": {
            "defaults": {
                "color": {"mode": "palette-classic"},
                "custom": {
                    "axisBorderShow": False,
                    "axisCenteredZero": False,
                    "axisColorMode": "text",
                    "axisLabel": ytitle,
                    "axisPlacement": "left",
                    "barAlignment": 0,
                    "drawStyle": "line",
                    "fillOpacity": fill,
                    "gradientMode": "none",
                    "hideFrom": {"legend": False, "tooltip": False, "viz": False},
                    "insertNulls": False,
                    "lineInterpolation": "smooth",
                    "lineWidth": line,
                    "pointSize": 5,
                    "scaleDistribution": {"type": "linear"},
                    "showPoints": "auto",
                    "spanNulls": False,
                    "stacking": {"group": "A", "mode": "stack" if stack else "none"},
                    "thresholdsStyle": {"mode": "off"},
                },
                "mappings": [],
                "thresholds": {
                    "mode": "absolute",
                    "steps": [{"color": "green", "value": None}],
                },
                "unit": yid,
            },
            "overrides": [],
        },
        "gridPos": {"h": grid[3], "w": grid[0], "x": grid[1], "y": grid[2]},
        "id": zlib.crc32((title + field).encode()) % 100000 + 2,
        "interval": "1m",
        "options": {
            "legend": {
                "calcs": ["sum" if not stack else "max", "mean"],
                "displayMode": "table",
                "placement": legend_placement.split("h")[0],
                "showLegend": True,
                "sortBy": "Desc",
                "sortDesc": True,
            },
            "tooltip": {
                "hideZeros": False,
                "mode": "multi",
                "sort": "desc",
            },
        },
        "targets": [{**inf(url, "table", max_rows=10000), "columns": [
            {"selector": "time", "text": "time", "type": "timestamp"},
            {"selector": "key_alias", "text": "key_alias", "type": "string"},
            {"selector": field, "text": field, "type": "number"},
        ]}],
        "title": title,
        "transformations": [
            {"id": "organize", "options": {
                "excludeByName": {
                    "bucket_seconds": True, "rows": True,
                    "completion_tokens": field != "completion_tokens",
                    "prompt_tokens": field != "prompt_tokens",
                    "requests": field != "requests",
                    "cost_usd": field != "cost_usd",
                    "key_alias": False,
                    "time": False,
                },
                "indexByName": {},
                "renameByName": {},
            }},
            {"id": "partitionByValues", "options": {"fields": ["key_alias"]}},
        ],
        "type": "timeseries",
    }


def table_panel(title, url, grid, id_num, sort_col="time", order="desc",
                overrides=None):
    return {
        "datasource": DS,
        "fieldConfig": {
            "defaults": {
                "custom": {
                    "align": "auto",
                    "cellOptions": {"type": "auto"},
                    "filterable": True,
                    "inspect": False,
                },
                "mappings": [],
                "thresholds": {
                    "mode": "absolute",
                    "steps": [{"color": "green", "value": None}],
                },
            },
            "overrides": overrides or [],
        },
        "gridPos": {"h": grid[3], "w": grid[0], "x": grid[1], "y": grid[2]},
        "id": id_num,
        "options": {
            "cellHeight": "sm",
            "footer": {"countRows": False, "fields": "", "reducer": ["sum"],
                       "show": False},
            "showHeader": True,
            "sortBy": [{"desc": order == "desc", "displayName": sort_col}],
        },
        "targets": [inf(url, "table", max_rows=500)],
        "title": title,
        "type": "table",
    }


def build():
    totals_url = "%s/api/summary?view=totals&from=${__timeFrom}&to=${__timeTo}" % API
    keys_url = "%s/api/summary?view=keys&from=${__timeFrom}&to=${__timeTo}" % API
    aliases_url = "%s/api/summary?view=aliases&from=${__timeFrom}&to=${__timeTo}" % API
    reqs_url = "%s/api/requests?from=${__timeFrom}&to=${__timeTo}&limit=200" % API

    panels = [
        stat("Requests", totals_url, "requests", "none",
             grid=(4, 0, 0, 4)),
        stat("Errors", totals_url, "errors", "none",
             grid=(4, 4, 0, 4)),
        stat("Prompt tokens", totals_url, "prompt_tokens", "short",
             grid=(5, 8, 0, 4)),
        stat("Completion tokens", totals_url, "completion_tokens", "short",
             grid=(5, 13, 0, 4)),
        stat("Cloud-equivalent cost", totals_url, "cost_usd", "currencyUSD",
             grid=(6, 18, 0, 4), color_mode="background",
             decimals=4),
        ts_panel("Cost per key / bucket", "currencyUSD", "$",
                 "cost_usd", (12, 12, 12, 8)),
        ts_panel("Requests per key / bucket", "short", "requests",
                 "requests", (12, 0, 12, 8)),
        table_panel("Usage by key (cloud-equivalent $)", keys_url,
                    (12, 0, 4, 8), 3001, sort_col="cost_usd"),
        table_panel("Usage by alias (what the key is being used for)",
                    aliases_url, (12, 12, 4, 8), 3002,
                    sort_col="requests", order="desc"),
        ts_panel("Prompt tokens per key / bucket", "short", "tokens",
                 "prompt_tokens", (12, 0, 20, 8), stack=True, fill=18),
        ts_panel("Completion tokens per key / bucket", "short", "tokens",
                 "completion_tokens", (12, 12, 20, 8), stack=True, fill=18),
        table_panel(
            "Recent requests (who/what/how much)",
            reqs_url, (24, 0, 28, 10), 3004,
            sort_col="time", order="desc",
            overrides=[
                {
                    "matcher": {"id": "byName", "options": "cost_usd"},
                    "properties": [{"id": "unit", "value": "currencyUSD"},
                                   {"id": "decimals", "value": 5}],
                },
                {
                    "matcher": {"id": "byName", "options": "duration_s"},
                    "properties": [{"id": "unit", "value": "s"},
                                   {"id": "decimals", "value": 2}],
                },
                {
                    "matcher": {"id": "byName", "options": "key_alias"},
                    "properties": [{"id": "custom.width", "value": 130}],
                },
                {
                    "matcher": {"id": "byName", "options": "preview"},
                    "properties": [{"id": "custom.width", "value": 420}],
                },
                {
                    "matcher": {"id": "byName", "options": "langfuse_url"},
                    "properties": [{"id": "custom.width", "value": 110},
                                   {"id": "links", "value": [{
                                       "targetBlank": True,
                                       "title": "Open timestamp-matched trace",
                                       "url": "${__value.raw}",
                                   }]}],
                },
                {
                    "matcher": {"id": "byName", "options": "time"},
                    "properties": [{"id": "custom.width", "value": 130}],
                },
                {
                    "matcher": {"id": "byName", "options": "called_as"},
                    "properties": [{"id": "custom.width", "value": 130}],
                },
                {
                    "matcher": {"id": "byName", "options": "status"},
                    "properties": [{"id": "custom.width", "value": 70}],
                },
            ],
        ),
    ]

    # Keep the client identity and purpose visible before technical details.
    columns = {
        3001: ["key_alias", "requests", "errors", "prompt_tokens", "completion_tokens", "cost_usd", "avg_latency_s", "last_seen"],
        3002: ["alias", "requests", "errors", "prompt_tokens", "completion_tokens", "cost_usd"],
        3004: ["time", "key_alias", "called_as", "preview", "status", "prompt_tokens", "completion_tokens", "cost_usd", "duration_s", "langfuse_url", "client_ip"],
    }
    labels = {"key_alias": "Client / key", "called_as": "Model alias", "alias": "Model alias",
              "preview": "Request preview", "cost_usd": "Estimated cost", "avg_latency_s": "Avg latency",
              "prompt_tokens": "Input tokens", "completion_tokens": "Output tokens",
              "duration_s": "Duration", "langfuse_url": "Related trace (time match)",
              "last_seen": "Last seen", "client_ip": "Client IP"}
    for panel in panels:
        if panel["type"] != "table":
            continue
        panel["transformations"] = [{"id": "organize", "options": {
            "excludeByName": {"duration_ms": True, "langfuse_trace": True, "served_by": True, "call_type": True},
            "indexByName": {name: i for i, name in enumerate(columns[panel["id"]])},
        }}]
        for field, label in labels.items():
            props = [{"id": "displayName", "value": label}]
            if field == "cost_usd":
                props += [{"id": "unit", "value": "currencyUSD"}, {"id": "decimals", "value": 4}]
            elif field in ("prompt_tokens", "completion_tokens"):
                props += [{"id": "unit", "value": "short"}]
            elif field == "avg_latency_s":
                props += [{"id": "unit", "value": "s"}]
            panel["fieldConfig"]["overrides"].append({"matcher": {"id": "byName", "options": field}, "properties": props})
        for sort in panel["options"]["sortBy"]:
            sort["displayName"] = labels.get(sort["displayName"], sort["displayName"])
        if panel["id"] == 3004:
            panel["description"] = "Recent gateway requests. Related Langfuse traces and fallback previews are matched by timestamp within 3 seconds, so concurrent requests may be mismatched."

    for panel in panels:
        if panel["type"] != "stat":
            panel["gridPos"]["y"] += 43
    panels.extend(build_performance_panels())
    panels.sort(key=lambda p: (p["gridPos"]["y"], p["gridPos"]["x"]))


    # mtgacoach free-trial keys: lifecycle + abuse signals, below the stat row.
    trials_url = "%s/api/trials?view=totals&from=${__timeFrom}&to=${__timeTo}" % API
    trial_keys_url = "%s/api/trials?view=keys&from=${__timeFrom}&to=${__timeTo}" % API
    trial_y = max(p["gridPos"]["y"] + p["gridPos"]["h"] for p in panels)
    trial_stats = [("Trial keys — active", "active"), ("Trial keys — expired", "expired"),
                   ("Trials minted (range)", "minted_in_window"), ("Trials used (range)", "used_in_window"),
                   ("Trial requests (range)", "requests_in_window"), ("Machines with >1 trial key", "machines_with_multiple_keys"),
                   ("Active trials ≥80% budget", "near_budget"), ("Trial errors / rate limits (range)", "errors_in_window")]
    for i, (title, field) in enumerate(trial_stats):
        panel = stat(title, trials_url, field, "none", grid=(3, 3 * i, trial_y, 4))
        if field in ("machines_with_multiple_keys", "near_budget", "errors_in_window"):
            panel["fieldConfig"]["defaults"]["thresholds"]["steps"] = [
                {"color": "green", "value": None}, {"color": "orange", "value": 1}]
        panels.append(panel)
    cols = ["key_alias", "email", "status", "requests", "errors", "prompt_tokens", "completion_tokens",
            "budget_used_pct", "keys_for_machine", "last_seen", "created", "expires", "machine"]
    for idx, (title, path, x, desc) in enumerate([
            ("Top 10 trial keys (range)", "trials", 0,
             "mtgacoach 7-day trial keys with the most requests in the selected range. keys_for_machine > 1 means "
             "the same machine minted several trials. budget_used_pct is LiteLLM spend vs max_budget."),
            ("Top 10 subscribers (range)", "subscribers", 12,
             "Patreon subscriber keys (patreon-*) with the most requests in the selected range.")]):
        tbl = table_panel(title, "%s/api/%s?view=top&from=${__timeFrom}&to=${__timeTo}" % (API, path),
                          (12, x, trial_y + 4, 9), 4500 + idx, sort_col="requests")
        tbl["description"] = desc
        exclude = {"minted_in_window": True}
        if path == "subscribers":
            exclude.update(machine=True, keys_for_machine=True, email=True)
        tbl["transformations"] = [{"id": "organize", "options": {
            "excludeByName": exclude, "indexByName": {n: k for k, n in enumerate(cols)}}}]
        panels.append(tbl)

    users_tbl = table_panel("Top 10 mtgacoach users (range)",
                            "%s/api/mtgacoach-users?view=top&from=${__timeFrom}&to=${__timeTo}" % API,
                            (24, 0, trial_y + 13, 9), 4510, sort_col="requests")
    users_tbl["description"] = ("mtgacoach desktop requests per person: the owner, Patreon subscribers (patreon-* keys) and "
                                "7-day trials (trial-* keys). Before 2026-10-18 the owner's Mac used the LiteLLM master key; "
                                "that traffic is counted under the owner.")
    users_tbl["transformations"] = [{"id": "organize", "options": {"excludeByName": {}, "indexByName": {
        n: k for k, n in enumerate(["rank", "user", "kind", "email", "requests", "errors", "prompt_tokens",
                                    "completion_tokens", "cost_usd", "last_seen", "keys"])}}}]
    panels.append(users_tbl)

    # Sobriety Copilot unique users. The gateway only sees the app server, so
    # these come from the app itself (per-day HyperLogLogs; no identities kept).
    sc_ds = {"type": "yesoreyeram-infinity-datasource", "uid": "sobriety-stats"}
    sc_url = SOBRIETY_API + "/api/stats/active-users?days=30"
    sc_y = max(p["gridPos"]["y"] + p["gridPos"]["h"] for p in panels)
    for i, (title, field) in enumerate([("Sobriety Copilot — users today", "today"),
                                        ("Sobriety Copilot — users, last 7 days", "last_7_days"),
                                        ("Sobriety Copilot — users, last 30 days", "last_30_days")]):
        panel = stat(title, sc_url, field, "none", grid=(4, 4 * i, sc_y, 8))
        panel["datasource"] = sc_ds
        panel["targets"][0]["datasource"] = sc_ds
        panel["description"] = ("Distinct people who sent a chat, research or SMS message (app install id, else IP for "
                                "older web builds, else phone number; counted as hashes, never stored). Windows are "
                                "distinct users across the days, not sums. Days follow America/Los_Angeles.")
        panels.append(panel)
    sc_target = inf(SOBRIETY_API + "/api/stats/active-users?days=366")
    sc_target["datasource"] = sc_ds
    sc_target["rootSelector"] = "daily"
    sc_target["root_selector"] = "daily"
    sc_target["columns"] = [{"selector": "time", "text": "time", "type": "timestamp_epoch"},
                            {"selector": "users", "text": "All users", "type": "number"},
                            {"selector": "app", "text": "App", "type": "number"},
                            {"selector": "web", "text": "Web", "type": "number"},
                            {"selector": "sms", "text": "SMS", "type": "number"}]
    panels.append({
        "id": 4520, "type": "timeseries", "title": "Sobriety Copilot — unique users per day",
        "description": "Distinct users each day (America/Los_Angeles days), split by where they came from. "
                       "Someone using both the app and SMS counts once in All users. Returns up to 366 days; the chart clips to the selected range. Counting started 2026-10-17.",
        "datasource": sc_ds, "targets": [sc_target],
        "gridPos": {"h": 8, "w": 12, "x": 12, "y": sc_y},
        "fieldConfig": {"defaults": {"unit": "none", "decimals": 0, "min": 0,
                                     "color": {"mode": "palette-classic"},
                                     "custom": {"drawStyle": "bars", "barAlignment": 0, "fillOpacity": 60,
                                                "lineWidth": 1, "showPoints": "never",
                                                "stacking": {"group": "A", "mode": "none"}}},
                        "overrides": [{"matcher": {"id": "byName", "options": "All users"},
                                       "properties": [{"id": "custom.drawStyle", "value": "line"},
                                                      {"id": "custom.lineWidth", "value": 3},
                                                      {"id": "custom.fillOpacity", "value": 0},
                                                      {"id": "custom.showPoints", "value": "always"}]}]},
        "options": {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                    "tooltip": {"mode": "multi", "sort": "desc"}},
    })

    bottom = max(p["gridPos"]["y"] + p["gridPos"]["h"] for p in panels)
    for idx, (title, endpoint, height) in enumerate([("Protection monitor — delivery status", "status", 4), ("Protection alerts — safe key IDs", "events", 8)]):
        panels.append({"id": 4400+idx, "title": title, "type": "table", "datasource": DS,
            "description": "Checks every 30 seconds. Durable events use safe key IDs only. Notification delivery is not configured until an approved route is available.",
            "gridPos": {"x": 0, "y": bottom, "w": 24, "h": height},
            "targets": [inf(f"{API}/api/protection/{endpoint}")],
            "fieldConfig": {"defaults": {}, "overrides": []}, "options": {"showHeader": True}})
        bottom += height

    bottom = max(p['gridPos']['y'] + p['gridPos']['h'] for p in panels)
    panels.append({'id': 4600, 'title': 'History coverage and capacity', 'type': 'table',
        'datasource': DS, 'targets': [inf(f'{API}/api/history')],
        'gridPos': {'x': 0, 'y': bottom, 'w': 24, 'h': 6},
        'description': 'Metrics retention: 400 days with a 50 GiB capacity cap and advance warnings. Usage metadata: at least 400 days; daily totals: at least five years. Existing records are preserved. Missing historical metrics cannot be recovered by extending retention.',
        'fieldConfig': {'defaults': {}, 'overrides': []}, 'options': {'showHeader': True}})
    history_panel = panels[-1]
    visible = ['capacity_status', 'metrics_retention', 'metrics_size_cap', 'metrics_oldest_utc', 'usage_oldest_utc', 'metadata_minimum_days', 'daily_minimum_days']
    hidden = ['usage_newest_archived_utc', 'archived_requests', 'rollup_updated_epoch', 'database_bytes', 'disk_free_bytes', 'deletion_enabled', 'history_note', 'metrics_block_bytes', 'metrics_projected_400d_bytes']
    history_panel['transformations'] = [{'id': 'organize', 'options': {
        'excludeByName': {k: True for k in hidden}, 'indexByName': {k: i for i,k in enumerate(visible)},
        'renameByName': dict(zip(visible, ['Capacity status', 'Metrics retention', 'Capacity limit', 'Metrics since (UTC)', 'Usage since (UTC)', 'Request metadata days', 'Daily totals days']))}}]
    history_panel['fieldConfig']['overrides'] = [{'matcher': {'id': 'byName', 'options': 'Capacity status'}, 'properties': [{'id': 'custom.width', 'value': 280}]}]
    for item in panels:
        if item.get('datasource', {}).get('uid') == 'usage-api':
            for target in item.get('targets', []):
                if '/api/timeseries?' in target.get('url', ''):
                    target['maxRows'] = 200000
            item['description'] = item.get('description', '') + ' Ranges over 30 days use daily UTC totals; partial days remain exact. History begins with recorded usage on June 7, 2026; missing days are not invented.'
    for item in panels:
        if "cost" in item["title"].lower() or item.get("id") in (3001, 3002, 3004, 4510):
            item["description"] = PRICING_NOTE
    panels = arrange_snapshot(panels)
    dashboard = {
        "annotations": {"list": []},
        "editable": True,
        "fiscalYearStartMonth": 0,
        "graphTooltip": 1,
        "links": [SELF_LINK],
        "panels": panels,
        "preload": False,
        "refresh": "10s",
        "schemaVersion": 41,
        "tags": ["litellm", "usage", "cost"],
        "templating": {"list": []},
        "time": {"from": "now-6h", "to": "now"},
        "timepicker": {"time_options": ["6h", "24h", "7d", "30d", "90d", "1y", "400d", "5y"]},
        "timezone": "browser",
        "description": "Metrics retention is 400 days; samples before October 15, 2026 have expired. Usage records begin June 7, 2026. Long ranges use daily totals. Live active-backend performance and RTX PRO 6000 hardware. Called model names and served backend identity remain separate. Usage charts show bucket totals. " + PRICING_NOTE,
        "title": "AI Usage",
        "uid": UID,
        "version": 1,
        "weekStart": "",
    }
    with open(OUT, "w") as f:
        json.dump(dashboard, f, indent=1)
    print("wrote %s with %d panels" % (OUT, len(panels)))


if __name__ == "__main__":
    build()
