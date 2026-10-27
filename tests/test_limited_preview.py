from arenamcp.limited_preview import deck_preview


def test_preview_preserves_duplicate_pool_copies_and_distinct_builds():
    card = {"grp_id": 1, "name": "Example", "mana_cost": "{2}", "cmc": 2, "type_line": "Creature"}
    build = {"main_deck": [{"grp_id": 1, "count": 2}], "basic_lands": {"U": 17}}
    alternative = {"main_deck": [{"grp_id": 1, "count": 1}], "basic_lands": {"U": 18}}
    data = deck_preview({"build": build, "options": [build, alternative], "cards": [card] * 3}, "Test")
    assert len(data["options"]) == 2
    first, second = data["options"]
    assert first["recommended"] and not second["recommended"]
    assert first["cuts"][0]["count"] == 1
    assert second["cuts"][0]["count"] == 2
    assert first["total"] == 19 and first["lands"] == 17
    assert first["curve"] == {"2": 2}


def test_desktop_preview_event_and_escaped_rendering(qapp):
    from arenamcp.desktop.coach_session import CoachSession
    from arenamcp.desktop.limited_preview import LimitedDeckPreview

    card = {"grp_id": 1, "name": "<Example>", "type_line": "Creature", "cmc": 2}
    data = deck_preview({"build": {"main_deck": [{"grp_id": 1, "count": 1}]}, "cards": [card]}, "Test")
    session = CoachSession()
    dialog = LimitedDeckPreview()
    session.limitedDeckPreviewChanged.connect(dialog.set_preview)
    session._handle_process_event({"type": "limited_deck_preview", "data": data})
    from PySide6.QtWidgets import QTextBrowser

    assert "<Example>" in dialog.findChild(QTextBrowser).toPlainText()
    assert dialog.isVisible()
    dialog.close()
    session.shutdown()


def test_preview_arrives_before_arena_write_without_blocking_submission():
    from tests.draft_fakes import make_primer
    from tests.test_draft_autoplay import actions, deck_step_world

    driver, bridge, _ = deck_step_world(make_primer())
    received = []

    def preview(data):
        assert "set_limited_deck" not in actions(bridge)
        received.append(data)

    driver._deck_preview_fn = preview
    driver._step()
    assert len(received) == 1
    assert received[0]["options"][0]["total"] == 40
    assert "submit_limited_deck" in actions(bridge)
