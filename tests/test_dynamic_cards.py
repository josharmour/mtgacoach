"""Dynamic-card name overlay: bridge fills, lookups consult, no re-ask spam."""

import arenamcp.dynamic_cards as dc


def setup_function(_):
    # Module-level state: reset between tests.
    dc._names.clear()
    dc._pending.clear()
    dc._asked.clear()


def test_note_take_put_roundtrip():
    dc.note_unresolved(194020)
    dc.note_unresolved(191049)
    ids = dc.take_pending()
    assert set(ids) == {194020, 191049}
    assert dc.take_pending() == []  # drained, moved to asked
    assert dc.put_names({194020: "Mightform Harmonizer"}) == 1
    assert dc.get_name(194020) == "Mightform Harmonizer"
    assert dc.get_name(191049) is None


def test_failed_ids_not_reasked_until_reset():
    dc.note_unresolved(167162)
    assert dc.take_pending() == [167162]
    dc.note_unresolved(167162)  # resolve failed; noted again
    assert dc.take_pending() == []  # suppressed — already asked
    dc.reset_asked()
    dc.note_unresolved(167162)
    assert dc.take_pending() == [167162]  # match boundary allows re-ask


def test_resolved_ids_never_pending():
    dc.put_names({999111: "Copy Thing"})
    dc.note_unresolved(999111)
    assert dc.take_pending() == []


def test_put_names_sanitizes():
    assert dc.put_names({"194019": "  Treefolk Token  ", "bad": "x", 5: ""}) == 1
    assert dc.get_name(194019) == "Treefolk Token"


def test_bridge_resolve_pending(monkeypatch):
    from arenamcp.gre_bridge import GREBridge

    dc.note_unresolved(194020)
    bridge = GREBridge.__new__(GREBridge)
    monkeypatch.setattr(
        bridge, "resolve_grp_ids",
        lambda ids: {194020: "Mightform Harmonizer"} if 194020 in ids else {},
        raising=False,
    )
    assert bridge.resolve_pending_dynamic_cards() == 1
    assert dc.get_name(194020) == "Mightform Harmonizer"
    # Queue drained — second call is a no-op.
    assert bridge.resolve_pending_dynamic_cards() == 0


def test_server_get_card_info_uses_overlay(monkeypatch):
    from arenamcp import server

    class _NullDB:
        def get_card_by_arena_id(self, arena_id):
            return None

    monkeypatch.setattr(server, "_get_card_db", lambda: _NullDB())

    # Unknown dynamic id: error + queued for bridge resolution.
    info = server.get_card_info(194020)
    assert "error" in info
    assert 194020 in dc._pending or 194020 in dc._asked

    # Once the bridge supplies a name, lookups return it.
    dc.put_names({194020: "Mightform Harmonizer"})
    info = server.get_card_info(194020)
    assert info.get("name") == "Mightform Harmonizer"
    assert info.get("dynamic") is True

    # Catalog-range misses are NOT queued (DB staleness, not dynamic).
    before = dc.stats()["pending"]
    server.get_card_info(50000)
    assert dc.stats()["pending"] == before
