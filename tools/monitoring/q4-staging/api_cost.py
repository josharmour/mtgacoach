"""Display-only API-equivalent estimates; never updates LiteLLM billing.

GLM-5.3-Flash checked 2026-11-03: https://docs.z.ai/guides/overview/pricing
Historical DeepSeek-V4.1-Flash-1017 checked 2026-10-29:
https://api-docs.deepseek.com/quick_start/pricing/ . USD per million tokens.
Remaining 2026 calendar: no Chinese public holidays after the Oct 29 cutover.
Calendar source: https://english.www.gov.cn/policies/featured/202511/04/content_WS6909c915c6d00ca5f9a074f7.html
Outside this verified calendar window, use a clearly disclosed peak upper estimate.
"""

SOURCE = "https://docs.z.ai/guides/overview/pricing"
CHECKED = "2026-11-03"
DEEPSEEK_SOURCE = "https://api-docs.deepseek.com/quick_start/pricing/"
GLM_RESTORE_UTC = "2026-11-04 06:04:00"
NOTE = (
    "Estimated public API-equivalent USD, not a self-hosting bill or customer charge. "
    "Current backend: GLM-5.3-Flash. Z.ai public rates per 1M tokens: "
    "input $0.15, cached input $0.03, output $0.50 (including reasoning tokens). "
    "GLM cache-aware pricing applies from the Nov 3, 2026 22:04 Pacific restoration; "
    "older configured GLM estimates are preserved. Actual stored backend identity "
    "selects pricing, never the current alias. Missing cache counts use the full "
    "input rate (upper estimate); local cache hits only approximate vendor eligibility. "
    "GLM source checked 2026-11-03: " + SOURCE + ". "
    "Historical DeepSeek-V4.1-Flash-1017 per 1M: peak cached input $0.006, "
    "uncached input $0.30, output $1.20; off-peak $0.003/$0.15/$0.60. "
    "Peak: weekdays 01:00–04:00 and 06:00–10:00 UTC excluding Chinese public holidays; "
    "schedule verified Oct 29–Dec 31, 2026, otherwise peak upper estimate. "
    "DeepSeek source checked 2026-10-29: " + DEEPSEEK_SOURCE
)


def current_glm_sql():
    return ("(coalesce(s.model,'') ~ '(^|/)glm-5[.]3-flash(-local)?$' "
            f"AND s.\"startTime\" >= TIMESTAMP '{GLM_RESTORE_UTC}')")


def deepseek_sql():
    # A preserved client alias must never decide which backend's rates apply.
    return "coalesce(s.model,'') LIKE '%deepseek-v4.1-flash%'"


def cached_sql():
    value = """coalesce(
        s.metadata #>> '{usage_object,prompt_tokens_details,cached_tokens}',
        s.metadata #>> '{usage_object,prompt_cache_hit_tokens}',
        s.response #>> '{usage,prompt_tokens_details,cached_tokens}',
        s.response #>> '{usage,prompt_cache_hit_tokens}')"""
    return f"(CASE WHEN ({value}) ~ '^[0-9]+$' THEN least(greatest(coalesce(s.prompt_tokens,0),0),({value})::numeric) END)"


def off_peak_sql():
    # SpendLog startTime is a timestamp without zone containing UTC.
    t = 's."startTime"'
    return f"""({t} >= TIMESTAMP '2026-10-29 00:00:00'
        AND {t} < TIMESTAMP '2027-01-01 00:00:00'
        AND NOT (extract(isodow from {t}) BETWEEN 1 AND 5
          AND (({t}::time >= TIME '01:00' AND {t}::time < TIME '04:00')
            OR ({t}::time >= TIME '06:00' AND {t}::time < TIME '10:00'))))"""


def cost_sql(legacy_input=0.15, legacy_output=0.50):
    p = "greatest(coalesce(s.prompt_tokens,0),0)"
    c = "greatest(coalesce(s.completion_tokens,0),0)"
    cached = f"coalesce({cached_sql()},0)"
    return f"""(CASE WHEN {deepseek_sql()} THEN
        (({p}-{cached})*0.30 + {cached}*0.006 + {c}*1.20)
        * (CASE WHEN {off_peak_sql()} THEN 0.5 ELSE 1.0 END)
        WHEN {current_glm_sql()} THEN ({p}-{cached})*0.15 + {cached}*0.03 + {c}*0.50
        ELSE {p}*{float(legacy_input)} + {c}*{float(legacy_output)} END / 1000000.0)"""


def unknown_cache_sql():
    return f"(({deepseek_sql()} OR {current_glm_sql()}) AND {cached_sql()} IS NULL)"


def pricing_sql():
    return f"""CASE WHEN {deepseek_sql()} THEN
        CASE WHEN {off_peak_sql()} THEN 'DeepSeek V4.1 Flash 1017 / off-peak'
        ELSE 'DeepSeek V4.1 Flash 1017 / peak upper estimate' END
        WHEN {current_glm_sql()} THEN 'GLM-5.3-Flash / Z.ai public API'
        ELSE 'Historical configured estimate (unchanged)' END"""
