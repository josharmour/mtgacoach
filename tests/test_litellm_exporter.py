"""Regression tests for tools/monitoring/litellm_exporter/exporter.py.

Background: the deployed exporter reset its daily gauges with

    for label_set in gauge._metrics.keys():
        gauge.remove(*label_set)

remove() mutates the same dict the loop iterates, so every scrape after the
first died with "RuntimeError: dictionary changed size during iteration" and
all token gauges stayed stale (ten empty Grafana panels). These tests pin the
fix: snapshot iteration + per-key error isolation.
"""

import importlib.util
from pathlib import Path

import pytest

pytest.importorskip("prometheus_client")
from prometheus_client import Gauge  # noqa: E402

EXPORTER_PATH = (
    Path(__file__).resolve().parents[1] / "tools" / "monitoring" / "litellm_exporter" / "exporter.py"
)


def _load_exporter():
    """Load exporter.py once (module-level prometheus registry is global)."""
    spec = importlib.util.spec_from_file_location("litellm_exporter", EXPORTER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


exporter = _load_exporter()


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._rows = []

    def execute(self, query, params=None):
        if "LiteLLM_DailyUserSpend" in query:
            self._rows = self.conn.spend_rows
        elif '"key_alias"' in query:
            self._rows = self.conn.alias_rows
        elif "LiteLLM_VerificationToken" in query:
            self._rows = self.conn.access_rows
        else:  # pragma: no cover - guard against surprise queries
            raise AssertionError(f"unexpected query: {query}")

    def fetchall(self):
        return self._rows

    def close(self):
        pass


class FakeConn:
    """Minimal psycopg2 stand-in serving canned rows per query."""

    def __init__(self, alias_rows=(), access_rows=(), spend_rows=()):
        self.alias_rows = list(alias_rows)
        self.access_rows = list(access_rows)
        self.spend_rows = list(spend_rows)
        self.closed = False

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        self.closed = True


def _error_count(stage):
    return exporter.litellm_exporter_errors_total.labels(stage=stage)._value.get()


def test_old_pattern_reproduces_dict_mutation_crash():
    """The original loop pattern raises RuntimeError — the bug was real."""
    g = Gauge("test_old_pattern_crash", "t", ["k"])
    for i in range(3):
        g.labels(k=f"k{i}").set(i)
    try:
        with pytest.raises(RuntimeError, match="dictionary changed size"):
            for label_set in g._metrics.keys():  # noqa: SIM118 — original (buggy) pattern
                g.remove(*label_set)
    finally:
        for label_set in list(g._metrics.keys()):
            g.remove(*label_set)


def test_reset_gauge_clears_all_children():
    g = Gauge("test_reset_clears", "t", ["k", "m"])
    for i in range(5):
        g.labels(k=f"k{i}", m="model").set(i)
    assert len(g._metrics) == 5
    exporter.reset_gauge(g)
    assert len(g._metrics) == 0


def test_reset_gauge_survives_repeated_cycles():
    """The exact production failure: cycle 2+ used to crash."""
    g = Gauge("test_reset_cycles", "t", ["k"])
    for cycle in range(3):
        for i in range(4):
            g.labels(k=f"k{i}").set(cycle * 10 + i)
        exporter.reset_gauge(g)
        assert len(g._metrics) == 0


def test_fetch_key_aliases_falls_back_to_hash_prefix():
    conn = FakeConn(alias_rows=[("deadbeef" + "0" * 56, None), ("cafe" + "1" * 60, "hermes")])
    aliases = exporter.fetch_key_aliases(conn)
    assert aliases["cafe" + "1" * 60] == "hermes"
    assert aliases["deadbeef" + "0" * 56] == "deadbeef0000..."


def test_update_metrics_sets_token_gauges():
    conn = FakeConn(
        access_rows=[("hashA" + "0" * 61, ["deepseek-v4-flash"])],
        spend_rows=[
            # api_key, model, model_group, pt, ct, reqs, succ, fail
            ("hashA" + "0" * 61, "openai/deepseek-v4-flash", None, 100, 250, 7, 6, 1),
        ],
    )
    exporter.update_metrics(conn, {"hashA" + "0" * 61: "josh-desktop"})
    key_hash = ("hashA" + "0" * 61)[:16] + "..."
    prompt = exporter.litellm_prompt_tokens.labels(
        key_alias="josh-desktop", model="openai/deepseek-v4-flash", api_key_hash=key_hash
    )
    total = exporter.litellm_total_tokens.labels(
        key_alias="josh-desktop", model="openai/deepseek-v4-flash", api_key_hash=key_hash
    )
    assert prompt._value.get() == 100
    assert total._value.get() == 350
    assert (
        exporter.litellm_requests_total.labels(key_alias="josh-desktop", api_key_hash=key_hash)._value.get()
        == 7
    )


def test_update_metrics_one_bad_row_does_not_kill_scrape():
    """A malformed row (api_key None) is skipped; good rows still land."""
    good = "hashG" + "0" * 60
    conn = FakeConn(
        access_rows=[(good, ["deepseek-v4-flash"])],
        spend_rows=[
            (None, "openai/deepseek-v4-flash", None, 5, 5, 1, 1, 0),  # bad row
            (good, "openai/deepseek-v4-flash", None, 42, 58, 3, 3, 0),
        ],
    )
    before = _error_count("row")
    exporter.update_metrics(conn, {good: "hermes"})
    assert _error_count("row") == before + 1
    assert (
        exporter.litellm_total_tokens.labels(
            key_alias="hermes",
            model="openai/deepseek-v4-flash",
            api_key_hash=good[:16] + "...",
        )._value.get()
        == 100
    )


def test_update_metrics_clears_stale_labelsets_between_cycles():
    """Second cycle with no rows must remove yesterday's label sets."""
    key = "hashS" + "0" * 60
    first = FakeConn(spend_rows=[(key, "openai/gemma", None, 1, 2, 1, 1, 0)])
    exporter.update_metrics(first, {key: "old-key"})
    assert len(exporter.litellm_total_tokens._metrics) > 0

    empty = FakeConn(spend_rows=[])
    exporter.update_metrics(empty, {key: "old-key"})
    for gauge in exporter.RESETTABLE_GAUGES:
        assert len(gauge._metrics) == 0


def test_update_metrics_filters_noise_aliases():
    noise = "nonono" + "0" * 58
    conn = FakeConn(spend_rows=[(noise, "openai/gemma", None, 9, 9, 2, 2, 0)])
    exporter.update_metrics(conn, {noise: "no-key-requi..."})
    assert len(exporter.litellm_total_tokens._metrics) == 0
    assert len(exporter.litellm_requests_total._metrics) == 0
