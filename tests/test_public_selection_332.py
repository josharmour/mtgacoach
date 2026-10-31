from types import SimpleNamespace

from arenamcp.decisions import build_pending_decision


def test_public_resolution_choices_keep_identity_and_rules(monkeypatch):
    from arenamcp import decisions

    objects = {
        166: SimpleNamespace(grp_id=88998, visibility="Visibility_Public", owner_seat_id=2),
        172: SimpleNamespace(grp_id=97369, visibility="Visibility_Public", owner_seat_id=2),
    }
    cards = {
        88998: {"name": "Deadly Cover-Up", "oracle_text": "Destroy all creatures.", "type_line": "Sorcery"},
        97369: {
            "name": "Day of Black Sun",
            "oracle_text": "All creatures get -X/-X until end of turn.",
            "type_line": "Sorcery",
        },
    }
    monkeypatch.setattr(decisions, "_live_object", objects.get)
    monkeypatch.setattr(decisions, "_live_game_state", lambda: SimpleNamespace(local_seat_id=1))
    monkeypatch.setattr(decisions, "_default_card_resolver", cards.get)
    poll = {
        "has_pending": True,
        "request_type": "SelectN",
        "game_state_id": 110,
        "msg_id": 160,
        "select_n_ids": [166, 172],
        "select_n_min": 1,
        "select_n_max": 1,
    }
    result = build_pending_decision(poll, resolve_instance=lambda _: "")
    assert [o.label for o in result.options] == ["Deadly Cover-Up", "Day of Black Sun"]
    assert result.options[0].meta["card"]["oracle_text"] == "Destroy all creatures."
    objects[166].visibility = "Visibility_Private"
    result = build_pending_decision(poll, resolve_instance=lambda _: "")
    assert result.options[0].label == "Option 166" and not result.options[0].meta["identity_known"]
    assert "card" not in result.options[0].meta
