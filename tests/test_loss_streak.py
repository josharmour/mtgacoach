from arenamcp.loss_streak import LossStreakStore


def finish(store, mid, result="loss", deck="deck", eligible=True):
    store.observe(mid, deck, eligible)
    store.finish(mid, result)


def test_three_distinct_matches_and_dedup(tmp_path):
    store = LossStreakStore(tmp_path / "state.json")
    finish(store, "a")
    store.finish("a", "loss")
    finish(store, "b")
    assert not store.pending()
    finish(store, "c")
    assert len(store.pending()) == 1
    store.finish("c", "loss")
    assert len(store.pending()) == 1


def test_restart_and_repeat_triplets(tmp_path):
    path = tmp_path / "state.json"
    store = LossStreakStore(path)
    finish(store, "a")
    finish(store, "b")
    store = LossStreakStore(path)
    finish(store, "c")
    key, _ = store.pending()[0]
    store.complete(key, {"analysis": "saved"})
    for mid in "def":
        finish(store, mid)
    assert len(store.pending()) == 1
    assert len(store.data["jobs"]) == 2


def test_win_draw_unknown_manual_and_deck_change_break_runs(tmp_path):
    for breaker in ("win", "draw", "unknown", "manual", "deck"):
        store = LossStreakStore(tmp_path / breaker)
        finish(store, "a")
        finish(store, "b")
        finish(
            store,
            "c",
            result=breaker if breaker in ("win", "draw", "unknown") else "loss",
            eligible=breaker != "manual",
            deck="other" if breaker == "deck" else "deck",
        )
        finish(store, "d")
        assert not store.pending()


def test_disabled_mid_match_disqualifies_and_correction_cancels(tmp_path):
    store = LossStreakStore(tmp_path / "s")
    finish(store, "a")
    finish(store, "b")
    finish(store, "c")
    store.finish("b", "win")
    assert not store.pending()
    store.observe("d", "deck", True)
    store.observe("d", "deck", False)
    store.observe("d", "deck", True)
    store.finish("d", "loss")
    assert not store.data["matches"]["d"]["eligible"]


def test_unwatched_and_missing_identity_do_not_count(tmp_path):
    store = LossStreakStore(tmp_path / "s")
    store.finish("stale", "loss")
    for mid in "abc":
        finish(store, mid, deck="")
    assert not store.pending()


def test_corrupt_state_recovery(tmp_path):
    path = tmp_path / "s"
    path.write_text("broken")
    store = LossStreakStore(path)
    finish(store, "a")
    assert store.data["matches"]["a"]["result"] == "loss"


def test_abandoned_match_does_not_block_future_reviews(tmp_path):
    store = LossStreakStore(tmp_path / "s")
    store.observe("abandoned", "deck", True)
    for mid in "abc":
        finish(store, mid)
    assert len(store.pending()) == 1


def test_capture_and_review_redacts_and_preserves_games(tmp_path):
    from types import SimpleNamespace

    from arenamcp.standalone_loss_streak import LossStreakMixin

    class Harness(LossStreakMixin):
        pass

    engine = Harness()
    engine._loss_streak_store = LossStreakStore(tmp_path / "s")
    output = []
    engine.ui = SimpleNamespace(advice=lambda text, label: output.append(text))
    engine._coach = None
    for mid in "abc":
        engine._loss_streak_store.observe(mid, "deck", True)
        for game in (1, 2):
            engine._capture_loss_evidence(
                f"{mid}-g{game}",
                {"match_id": mid, "deck_cards": [123], "players": [{"name": "PrivateName"}]},
                [
                    {
                        "advice": "PrivateName used a card; contact me@example.com",
                        "game_snapshot": {"turn_number": 3},
                    }
                ],
                [],
            )
        engine._loss_streak_store.finish(mid, "loss")
    engine._run_loss_reviews()
    assert len(output) == 1
    assert "Nothing has been sent" in output[0]
    report = next(iter(engine._loss_streak_store.data["jobs"].values()))["report"]
    assert len(report["evidence"]) == 6
    assert "PrivateName" not in str(report)
    assert "me@example.com" not in str(report)
    engine._run_loss_reviews()
    assert len(output) == 1


def test_match_scope_not_last_game_and_unknown_identity():
    from arenamcp.loss_streak import resolve_match_result

    rows = [
        {"scope": "MatchScope_Game", "winningTeamId": 1},
        {"scope": "MatchScope_Match", "winningTeamId": 2},
    ]
    assert resolve_match_result(rows, 1, 1) == "loss"
    assert resolve_match_result(rows[:1], 1, 1) is None
    assert resolve_match_result(rows, None, None) is None


def test_explicit_new_queue_session_breaks_streak(tmp_path):
    store = LossStreakStore(tmp_path / "s")
    finish(store, "a")
    finish(store, "b")
    store.new_session()
    finish(store, "c")
    assert not store.pending()


def test_history_retention_does_not_retrigger_completed_triplets(tmp_path):
    store = LossStreakStore(tmp_path / "s")
    for i in range(180):
        finish(store, str(i))
        for key, _ in store.pending():
            store.complete(key, {"analysis": "done"})
    assert len(store.data["order"]) <= 63
    assert len(store.data["jobs"]) <= 20
    assert not store.pending()
    for i in range(180, 183):
        finish(store, str(i))
    assert len(store.pending()) == 1
