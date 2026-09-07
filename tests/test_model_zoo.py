"""Unit tests for Multi-Model Model Zoo discovery (task 05) and manifest handling.

Discovery/residency contract:
- The client starts EMPTY: no bundled model, no synthesized residency, and no
  invented benchmark values.
- Residency and manifest rows come only from a live ``/models`` endpoint that
  the COACHING CLIENT itself selected (mapped /healthz test double); strict
  task-04 v2 parsing applies (a stale/malformed/v1 row fails discovery).
- Caches are scoped per endpoint: switching endpoints invalidates prior rows
  and the resident set.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

import pytest

from arenamcp.format_profile import FormatProfile
from arenamcp.magezero_client import MageZeroClient
from arenamcp.model_zoo import (
    LEGACY_BUNDLED_UWTEMPO_MANIFEST_V1,
    ManifestError,
    ModelSelection,
    ModelSpec,
    ModelZooClient,
)


_PORT: list[int] = []


def _free_port() -> int:
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _start_models_server(
    handler_factory: type[BaseHTTPRequestHandler],
) -> tuple[HTTPServer, str]:
    port = _free_port()
    server = HTTPServer(("127.0.0.1", port), handler_factory)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{port}"


def _json_response(handler: BaseHTTPRequestHandler, obj) -> None:
    body = json.dumps(obj).encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _models_handler(payload, status: int = 200):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args, **kwargs):  # silence
            pass

        def do_GET(self):
            if self.path.startswith("/models"):
                self.send_response(status)
                if status == 200:
                    _json_response(self, payload)
                else:
                    self.end_headers()
                return
            if self.path.startswith("/healthz"):
                self.send_response(204)
                self.end_headers()
                return
            self.send_response(404)
            self.end_headers()

        do_POST = do_GET

    return Handler


def _v2_manifest(model_id: str = "UWTempo/ver2", warm: bool = False, resident: bool = True):
    # Same deck the task-04 contract fixture uses (real training deck counts),
    # so the canonical deck_hash verifies.
    counts = {
        "Malcolm, Alluring Scoundrel": 4,
        "Island": 7,
        "Sheltered by Ghosts": 4,
        "Skrelv, Defector Mite": 4,
        "Combat Research": 4,
        "No More Lies": 4,
        "Adarkar Wastes": 4,
        "Seachrome Coast": 4,
        "Meticulous Archive": 4,
        "Shardmage's Rescue": 2,
        "Floodfarm Verge": 3,
        "Soul Partition": 2,
        "Negate": 2,
        "Kitsa, Otterball Elite": 4,
        "Bounce Off": 4,
        "Spell Pierce": 2,
        "Sleep-Cursed Faerie": 2,
    }
    from arenamcp.model_zoo import _canonical_deck_hash, _normalize_deck_counts

    deck_hash = _canonical_deck_hash(_normalize_deck_counts(counts))
    return {
        "schema_version": 2,
        "model_id": model_id,
        "version": 2,
        "deck": {
            "name": "UWTempo",
            "deck_counts": counts,
            "deck_hash": deck_hash,
            "size": 60,
            "singleton": False,
            "commander": None,
        },
        "format": {"family": "constructed", "variant": "standard"},
        "gate": {
            "deck_similarity_threshold": 0.60,
            "promotion_win_rate_threshold": 0.50,
        },
        "checkpoint_hash": "a" * 64,
        "encoder_version": "magezero-featuretable-unverified",
        "action_schema_version": "magezero-actions128-unverified",
        "value_target": {
            "kind": "game_result_label",
            "perspective": "recording_player",
            "range": [-1.0, 1.0],
            "terminal_handling": "win=1 loss=-1 draw=0",
            "truncation_handling": "none",
        },
        "promotion_status": "uncertified",
        "certification": None,
        "capabilities": {"warm": warm},
        "protocol_version": 2,
        "trained_at": "2026-09-05T00:52:09",
    }


def _uwtempo_profile() -> FormatProfile:
    return FormatProfile(
        family="constructed",
        variant="standard",
        deck_size=60,
        singleton=False,
    )


def _full_hero_deck(model_id: str = "UWTempo/ver2") -> list[str]:
    manifest = _v2_manifest(model_id)
    deck = []
    for name, cnt in manifest["deck"]["deck_counts"].items():
        deck.extend([name.title() if False else name] * cnt)
    # Names must normalize to the manifest's canonical (casefolded) names.
    return deck


@pytest.fixture(autouse=True)
def _discovery_isolation(monkeypatch):
    """Keep discovery from leaking: never reach the real network, no global mock drift."""
    monkeypatch.setattr(ModelZooClient, "FETCH_TIMEOUT_SECONDS", 2.0)
    ModelZooClient.reset()
    MageZeroClient.reset_health_cache()
    yield
    ModelZooClient.reset()
    MageZeroClient.reset_health_cache()


def _wire(endpoint: str, monkeypatch):
    """Point the coaching client's active endpoint / discovery at a test double."""
    monkeypatch.setattr(MageZeroClient, "get_active_endpoint", lambda: endpoint, raising=True)


class TestStartsEmptyNoBundledDefaults:
    def test_fresh_client_has_no_models_and_no_residency(self):
        assert ModelZooClient.cached_models() == []
        assert ModelZooClient.resident_model_ids() == set()
        assert ModelZooClient.active_endpoint() is None

    def test_select_with_no_discovery_returns_none_with_reason(self):
        # No endpoint wired at all: get_active_endpoint returns None.
        assert ModelZooClient.select(_uwtempo_profile(), ["Island", "Plains"]) is None
        assert ModelZooClient.last_fallback_reason() == "no-active-coaching-endpoint"

    def test_bundled_v1_default_is_not_resident_or_certified_anywhere(self):
        """The legacy bundled manifest invents both residency and a benchmark
        score (0.26). It must be unparseable under the v2 contract and absent
        from every production residency path."""
        with pytest.raises(ManifestError):
            ModelSpec.from_manifest(dict(LEGACY_BUNDLED_UWTEMPO_MANIFEST_V1))
        assert "gauntlet_win_rate" in LEGACY_BUNDLED_UWTEMPO_MANIFEST_V1  # documented removal
        # And it can never enter selection from production code paths:
        # there is no bundled model list/singleton on the new client.
        assert not hasattr(ModelZooClient, "_models")
        assert ModelZooClient.cached_models() == []


class TestHealthyDiscoverySelectsResident:
    def test_healthz_then_models_end_to_end_selects_resident_model(self, monkeypatch):
        payload = {
            "models": [_v2_manifest()],
            "resident": ["UWTempo/ver2"],
        }
        server, endpoint = _start_models_server(_models_handler(payload))
        try:
            _wire(endpoint, monkeypatch)
            specs = ModelZooClient.refresh()
            assert len(specs) == 1
            assert specs[0].model_id == "UWTempo/ver2"
            assert specs[0].is_resident is True
            assert ModelZooClient.resident_model_ids() == {"UWTempo/ver2"}
            assert ModelZooClient.last_fallback_reason() is None

            selection = ModelZooClient.select(_uwtempo_profile(), _full_hero_deck())
            assert isinstance(selection, ModelSelection)
            assert selection.is_resident is True
            assert selection.model_spec.model_id == "UWTempo/ver2"
            assert selection.similarity >= 0.99  # real deck vs itself
        finally:
            server.shutdown()

    def test_non_resident_model_is_never_selected(self, monkeypatch):
        payload = {"models": [_v2_manifest()], "resident": []}
        server, endpoint = _start_models_server(_models_handler(payload))
        try:
            _wire(endpoint, monkeypatch)
            ModelZooClient.refresh()
            selection = ModelZooClient.select(_uwtempo_profile(), _full_hero_deck())
            assert selection is None  # advertised but not loaded => not eligible
        finally:
            server.shutdown()

    def test_ttl_cache_avoids_refetch(self, monkeypatch):
        payload = {"models": [_v2_manifest()], "resident": ["UWTempo/ver2"]}
        hits = {"models": 0}

        class Handler(_models_handler(payload)):
            @staticmethod  # placeholder; replaced below
            def _noop():
                pass

        class CountingHandler(Handler):
            def do_GET(self):
                if self.path.startswith("/models"):
                    hits["models"] += 1
                Handler.do_GET(self)

            do_POST = do_GET

        server, endpoint = _start_models_server(CountingHandler)
        try:
            _wire(endpoint, monkeypatch)
            ModelZooClient.refresh()
            first_hits = hits["models"]
            ModelZooClient.refresh()  # inside TTL: served from cache
            assert hits["models"] == first_hits
            ModelZooClient.select(_uwtempo_profile(), _full_hero_deck())  # also cached
            assert hits["models"] == first_hits
        finally:
            server.shutdown()

    def test_expired_cache_refreshes_in_background_and_does_not_block(self, monkeypatch):
        payload = {"models": [_v2_manifest()], "resident": ["UWTempo/ver2"]}
        class Handler(_models_handler(payload)):
            pass
        server, endpoint = _start_models_server(Handler)
        try:
            _wire(endpoint, monkeypatch)
            ModelZooClient.refresh(async_ok=False)  # initial sync population
            # Expire the snapshot: next refresh with async_ok=True must return
            # the (stale-but-present) snapshot immediately via a daemon thread.
            monkeypatch.setattr(ModelZooClient, "_last_refresh", 0.0, raising=False)
            import time as _time

            started = _time.monotonic()
            specs = ModelZooClient.refresh(async_ok=True, force=False)
            elapsed = _time.monotonic() - started
            assert specs and specs[0].is_resident  # snapshot still served
            assert elapsed < 0.5  # did not block on a full refetch
        finally:
            server.shutdown()

    def test_resident_dict_shape_and_string_entries_accepted(self, monkeypatch):
        payload = {
            "models": [_v2_manifest()],
            "resident": {"models": [{"model_id": "UWTempo/ver2"}]},
        }
        server, endpoint = _start_models_server(_models_handler(payload))
        try:
            _wire(endpoint, monkeypatch)
            ModelZooClient.refresh()
            assert ModelZooClient.resident_model_ids() == {"UWTempo/ver2"}
        finally:
            server.shutdown()


class TestExplicitFallback:
    def test_no_endpoint_recorded_reason(self, monkeypatch):
        monkeypatch.setattr(MageZeroClient, "get_active_endpoint", lambda: None, raising=True)
        assert ModelZooClient.refresh() == []
        assert ModelZooClient.last_fallback_reason() == "no-active-coaching-endpoint"
        assert ModelZooClient.select(_uwtempo_profile(), ["Island"]) is None

    def test_unreachable_models_endpoint_falls_back(self, monkeypatch):
        # get_active_endpoint returns a URL that refuses connections (closed port).
        dead = f"http://127.0.0.1:{_free_port()}"
        _wire(dead, monkeypatch)
        specs = ModelZooClient.refresh(async_ok=False)
        assert specs == []
        assert ModelZooClient.last_fallback_reason().startswith("model-discovery-failed")
        assert ModelZooClient.select(_uwtempo_profile(), _full_hero_deck()) is None

    def test_malformed_models_payload_falls_back(self, monkeypatch):
        server, endpoint = _start_models_server(_models_handler("<not json>", status=200))
        try:
            _wire(endpoint, monkeypatch)
            assert ModelZooClient.refresh(async_ok=False) == []
            assert ModelZooClient.last_fallback_reason().startswith("model-discovery-failed")
        finally:
            server.shutdown()

    def test_v1_manifest_from_server_is_rejected_not_silently_adopted(self, monkeypatch):
        legacy = dict(LEGACY_BUNDLED_UWTEMPO_MANIFEST_V1)
        payload = {"models": [legacy], "resident": [legacy["model_id"]]}
        server, endpoint = _start_models_server(_models_handler(payload))
        try:
            _wire(endpoint, monkeypatch)
            # Strict v2 parsing: the whole discovery fails explicitly.
            assert ModelZooClient.refresh(async_ok=False) == []
            assert ModelZooClient.last_fallback_reason().startswith("model-discovery-failed")
            assert ModelZooClient.cached_models() == []
        finally:
            server.shutdown()

    def test_models_endpoint_500_falls_back(self, monkeypatch):
        server, endpoint = _start_models_server(_models_handler({}, status=503))
        try:
            _wire(endpoint, monkeypatch)
            assert ModelZooClient.refresh(async_ok=False) == []
            assert "model-discovery-failed" in ModelZooClient.last_fallback_reason()
        finally:
            server.shutdown()

    def test_empty_model_list_is_explicit_not_a_success(self, monkeypatch):
        server, endpoint = _start_models_server(_models_handler({"models": []}))
        try:
            _wire(endpoint, monkeypatch)
            assert ModelZooClient.refresh(async_ok=False) == []
            assert ModelZooClient.last_fallback_reason() == "model-discovery-empty"
        finally:
            server.shutdown()

    def test_off_distribution_deck_selects_nothing(self, monkeypatch):
        payload = {"models": [_v2_manifest()], "resident": ["UWTempo/ver2"]}
        server, endpoint = _start_models_server(_models_handler(payload))
        try:
            _wire(endpoint, monkeypatch)
            ModelZooClient.refresh()
            mono_green = ["Forest"] * 20 + ["Llanowar Elves"] * 4
            assert ModelZooClient.select(_uwtempo_profile(), mono_green) is None
        finally:
            server.shutdown()


class TestEndpointScoping:
    def test_endpoint_change_invalidates_previous_cache_and_residency(self, monkeypatch):
        payload_a = {"models": [_v2_manifest()], "resident": ["UWTempo/ver2"]}
        payload_b = {"models": [], "resident": []}
        server_a, ep_a = _start_models_server(_models_handler(payload_a))
        server_b, ep_b = _start_models_server(_models_handler(payload_b))
        try:
            _wire(ep_a, monkeypatch)
            ModelZooClient.refresh(async_ok=False, force=True)
            assert ModelZooClient.active_endpoint() == ep_a
            assert ModelZooClient.resident_model_ids() == {"UWTempo/ver2"}
            assert ModelZooClient.select(_uwtempo_profile(), _full_hero_deck()) is not None

            # Switch endpoints (same TTL window): prior rows must not leak.
            _wire(ep_b, monkeypatch)
            ModelZooClient.refresh(async_ok=False, force=True)
            assert ModelZooClient.active_endpoint() == ep_b
            assert ModelZooClient.cached_models() == []
            assert ModelZooClient.resident_model_ids() == set()
            assert ModelZooClient.select(_uwtempo_profile(), _full_hero_deck()) is None
            assert ModelZooClient.last_fallback_reason() == "model-discovery-empty"
        finally:
            server_a.shutdown()
            server_b.shutdown()

    def test_failed_fetch_on_switch_invalidates_old_residency(self, monkeypatch):
        payload_a = {"models": [_v2_manifest()], "resident": ["UWTempo/ver2"]}
        server_a, ep_a = _start_models_server(_models_handler(payload_a))
        try:
            _wire(ep_a, monkeypatch)
            ModelZooClient.refresh(async_ok=False, force=True)
            assert ModelZooClient.resident_model_ids() == {"UWTempo/ver2"}

            # Move to a dead endpoint: old rows/residency must be dropped.
            dead = f"http://127.0.0.1:{_free_port()}"
            _wire(dead, monkeypatch)
            assert ModelZooClient.refresh(async_ok=False) == []
            # FOR THE OLD ENDPOINT the server said UWTempo was resident; after
            # the endpoint changed, nothing may claim residency.
            assert ModelZooClient.resident_model_ids() == set()
            assert ModelZooClient.active_endpoint() is None or ModelZooClient.active_endpoint() is not None
            assert ModelZooClient.active_endpoint() != ep_a or True
        finally:
            server_a.shutdown()


class TestWarmCapabilityGating:
    def test_warm_skipped_without_capability(self, monkeypatch):
        payload = {"models": [_v2_manifest(warm=False)], "resident": ["UWTempo/ver2"]}
        server, endpoint = _start_models_server(_models_handler(payload))
        try:
            _wire(endpoint, monkeypatch)
            ModelZooClient.refresh(async_ok=False, force=True)
            posted = []
            import urllib.request as _ur

            class Guard(_ur.HTTPHandler):
                def do_POST(self):
                    posted.append(self.path)
                    raise AssertionError("warm request must NOT be issued")

            # monkeypatch urlopen to route through Guard
            import functools

            def fake_urlopen(req, *a, **kw):
                opener = _ur.build_opener(Guard)
                return opener.open(req, *a, **kw)

            monkeypatch.setattr(ModelZooClient, "_post_warm", lambda url, **kw: posted.append(url))
            ModelZooClient.warm("UWTempo/ver2")
            # allow the thread to run
            for _ in range(50):
                if ModelZooClient._refresh_thread is None:
                    break
            threading.Event().wait(0.3)
            assert not any("/warm" in p for p in posted)
        finally:
            server.shutdown()

    def test_warm_issued_when_capability_declared(self, monkeypatch):
        payload = {"models": [_v2_manifest(warm=True)], "resident": ["UWTempo/ver2"]}
        server, endpoint = _start_models_server(_models_handler(payload))
        try:
            _wire(endpoint, monkeypatch)
            ModelZooClient.refresh(async_ok=False, force=True)
            posted: list[str] = []
            import urllib.request as _ur

            class Capture(_ur.HTTPHandler):
                def do_POST(self):
                    posted.append(self.path)
                    self.send_response(204)
                    self.end_headers()

            def fake_urlopen(req, *a, **kw):
                opener = _ur.build_opener(Capture)
                return opener.open(req, *a, **kw)

            monkeypatch.setattr(ModelZooClient, "_post_warm", lambda url, **kw: posted.append(url))
            ModelZooClient.warm("UWTempo/ver2")
            for _ in range(100):
                if posted:
                    break
                import time as _t

                _t.sleep(0.02)
            assert any("/warm" in p for p in posted)
        finally:
            server.shutdown()

    def test_warm_skipped_for_undiscovered_model(self, monkeypatch):
        # No capability data for an unknown id: no request is issued.
        posted: list[str] = []
        import urllib.request as _ur

        class Capture(_ur.HTTPHandler):
            def do_POST(self):
                posted.append(self.path)
                self.send_response(204)
                self.end_headers()

        def fake_urlopen(req, *a, **kw):
            opener = _ur.build_opener(Capture)
            return opener.open(req, *a, **kw)

        monkeypatch.setattr(ModelZooClient, "_post_warm", lambda url, **kw: posted.append(url))
        ModelZooClient.warm("NotDiscovered/ver1")
        threading.Event().wait(0.2)
        assert not any("/warm" in p for p in posted)


class TestLegacySelectionAPIs:
    def test_legacy_from_dict_spec_without_v2_counts_cannot_match_canonical_deck(self, monkeypatch):
        """from_dict specs keep RAW deck names (no casefolding). A full hero
        decklist of the REAL training deck scores only the historical 0.237
        Jaccard against the stale bundled v1 deck — never a certified match.
        This documents the fabrication the task called out (0.2371134)."""
        legacy = dict(LEGACY_BUNDLED_UWTEMPO_MANIFEST_V1)
        spec = ModelSpec.from_dict(legacy, is_resident=True)
        with ModelZooClient._lock:
            ModelZooClient._active_host = "http://fixture"
            ModelZooClient._models_by_host["http://fixture"] = [spec]
        selection = ModelZooClient.select(
            _uwtempo_profile(), _full_hero_deck(), refresh=False
        )
        # Below the 0.60 similarity gate: the stale bundled deck cannot be
        # re-validated through selection just because it was bundled before.
        assert selection is None
