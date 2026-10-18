"""Wave-3 tests: proactive game-dynamics commentary.

Covers (synthetic snapshots, no Qt, no network, no subprocess):
- TopicSelector: role-shift fires once not repeatedly; CA/material-change
  detection; opponent-archetype development; threat detection (reuse of
  THREAT_CARDS); plan drift; observed-facts vs hypothesis evidence.
- Gate ladder in ConversationController.speak_topic_if_any: speech priority,
  speaking cooldown, repetition suppression, urgent interrupt (mock
  voice_session), observed-vs-hypothesis instruction in prompt.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from arenamcp.conversation import (
    CONVERSATION,
    DEFAULT_COOLDOWN_SECONDS,
    TOPIC_PROMPT_PREFIX,
    EventPriority,
    MatchMemory,
    TopicSelector,
)


def card(
    name: str,
    controller: int,
    instance_id: int,
    type_line: str = "Creature",
    attacking: bool = False,
    tapped: bool = False,
) -> dict:
    return {
        "name": name,
        "controller_seat_id": controller,
        "instance_id": instance_id,
        "type_line": type_line,
        "is_attacking": attacking,
        "is_tapped": tapped,
        "owner_seat_id": controller,
    }


def make_state(
    my_life: int = 20,
    opp_life: int = 20,
    battlefield: list[dict] | None = None,
    my_hand_count: int | None = 5,
    opp_hand_count: int = 5,
    stack: list[dict] | None = None,
) -> dict:
    state = {
        "local_seat_id": 1,
        "opponent_seat_id": 2,
        "players": [
            {"seat_id": 1, "life_total": my_life},
            {"seat_id": 2, "life_total": opp_life},
        ],
        "turn": {"turn_number": 5, "active_player": 1, "phase": "Phase_Main1"},
    }
    if battlefield is not None:
        state["battlefield"] = battlefield
    if stack is not None:
        state["stack"] = stack
    zones: dict = {}
    if opp_hand_count is not None:
        zones["opponent_hand_count"] = opp_hand_count
    if zones:
        state["zones"] = zones
    if my_hand_count is not None:
        state["hand"] = [{"name": f"Card {i}"} for i in range(my_hand_count)]
    return state


SELECTOR = TopicSelector()


# ---------------------------------------------------------------------------
# TopicSelector — meaningful-change detection
# ---------------------------------------------------------------------------


class TestLocalPlayTopics:
    """Play-by-play coverage (2026-09-16): your own notable resolves and
    combo synergies become commentary topics."""

    def test_your_new_creature_resolves_becomes_topic(self) -> None:
        prev = make_state(battlefield=[card("Forest", 1, 1, "Land")])
        cur = make_state(
            battlefield=[
                card("Forest", 1, 1, "Land"),
                card("Vorinclex, Voice of Hunger", 1, 2),
            ]
        )
        topics = SELECTOR.select(prev, cur, [])
        keys = {t.key for t in topics}
        assert "local_play" in keys
        play = next(t for t in topics if t.key == "local_play")
        assert "Vorinclex" in play.evidence

    def test_no_new_entries_no_topic(self) -> None:
        prev = make_state(battlefield=[card("Llanowar Elves", 1, 1)])
        cur = make_state(battlefield=[card("Llanowar Elves", 1, 1)])
        assert all(t.key != "local_play" for t in SELECTOR.select(prev, cur, []))

    def test_lands_do_not_produce_play_topics(self) -> None:
        prev = make_state(battlefield=[])
        cur = make_state(battlefield=[card("Forest", 1, 1, "Land")])
        assert all(t.key != "local_play" for t in SELECTOR.select(prev, cur, []))

    def test_shared_subtype_combo_tidbit(self) -> None:
        prev = make_state(battlefield=[])
        cur = make_state(
            battlefield=[card("Notary Hobbits", 1, 1), card("Lathiel, the Bounteous Dawn", 1, 2)]
        )
        cur["battlefield"][0]["subtypes"] = ["Halfling", "Citizen"]
        cur["battlefield"][1]["subtypes"] = ["Halfling", "Citizen"]
        topics = SELECTOR.select(prev, cur, [])
        keys = {t.key for t in topics}
        assert "combo_tidbit" in keys
        combo = next(t for t in topics if t.key == "combo_tidbit")
        # Label preserves original casing of the shared subtype (one deduped
        # pair — A+B and B+A are the same fact).
        assert "share Halfling" in combo.evidence or "share Citizen" in combo.evidence
        assert combo.evidence.count("+") == 1


class TestRoleShiftDetection:
    def test_shift_to_offense_fires_once(self) -> None:
        prev = make_state(battlefield=[card("Bear", 1, 10)])
        cur = make_state(battlefield=[card("Bear", 1, 10, attacking=True)])
        topics = SELECTOR.select(prev, cur, [])
        assert [t.key for t in topics] == ["role_shift"]
        assert topics[0].priority == EventPriority.STATE_SHIFT

    def test_sustained_role_does_not_refire(self) -> None:
        cur = make_state(battlefield=[card("Bear", 1, 10, attacking=True)])
        # Same role on the next comparison: no new shift.
        topics = SELECTOR.select(cur, dict(cur), [])
        assert [t.key for t in topics if t.key == "role_shift"] == []

    def test_shift_to_defense_fires(self) -> None:
        prev = make_state(battlefield=[card("Bear", 1, 10, attacking=True)])
        cur = make_state(battlefield=[card("Bear", 1, 10), card("Opp Bear", 2, 20, attacking=True)])
        topics = SELECTOR.select(prev, cur, [])
        shift = [t for t in topics if t.key == "role_shift"]
        assert len(shift) == 1
        assert "defense" in shift[0].evidence.lower()

    def test_no_shift_when_neither_side_attacking(self) -> None:
        state = make_state(battlefield=[card("Bear", 1, 10)])
        topics = SELECTOR.select(state, dict(state), [])
        assert [t.key for t in topics] == []


class TestMaterialShiftDetection:
    def test_life_drop_fires_state_shift(self) -> None:
        prev = make_state(my_life=20)
        cur = make_state(my_life=16)
        topics = SELECTOR.select(prev, cur, [])
        assert [t.key for t in topics] == ["material_shift"]
        assert topics[0].priority == EventPriority.STATE_SHIFT
        assert "16" in topics[0].evidence and "20" in topics[0].evidence

    def test_small_life_change_is_not_meaningful(self) -> None:
        prev = make_state(my_life=20)
        cur = make_state(my_life=19)
        assert SELECTOR.select(prev, cur, []) == []

    def test_creature_count_swing_fires(self) -> None:
        prev = make_state(battlefield=[card("Bear A", 2, 20), card("Bear B", 2, 21)])
        cur = make_state(
            battlefield=[
                card("Bear A", 2, 20),
                card("Bear B", 2, 21),
                card("Bear C", 2, 22),
                card("Bear D", 2, 23),
            ]
        )
        topics = SELECTOR.select(prev, cur, [])
        assert "material_shift" in [t.key for t in topics]
        shift = next(t for t in topics if t.key == "material_shift")
        assert "2→4" in shift.evidence

    def test_opponent_hand_gain_is_hypothesis_evidence(self) -> None:
        prev = make_state(opp_hand_count=2)
        cur = make_state(opp_hand_count=5)
        topics = SELECTOR.select(prev, cur, [])
        assert len(topics) >= 1
        shift = next(t for t in topics if t.key == "material_shift")
        assert "hypothesis" in shift.evidence.lower()
        # Observed count change is a fact; what the cards ARE is the hypothesis.
        assert "2→5" in shift.evidence

    def test_no_material_change_no_topic(self) -> None:
        state = make_state()
        assert SELECTOR.select(state, dict(state), []) == []


class TestOpponentDevelopmentAndThreats:
    def test_new_opponent_permanent_is_development(self) -> None:
        prev = make_state(battlefield=[])
        cur = make_state(battlefield=[card("Ordinary Bear", 2, 30)])
        topics = SELECTOR.select(prev, cur, [])
        assert [t.key for t in topics] == ["opponent_development"]
        assert "(observed)" in topics[0].evidence

    def test_threat_card_uses_threat_class(self) -> None:
        prev = make_state(battlefield=[])
        cur = make_state(battlefield=[card("Sheoldred, the Apocalypse", 2, 31)])
        topics = SELECTOR.select(prev, cur, [])
        assert [t.key for t in topics] == ["threat"]
        assert topics[0].priority == EventPriority.THREAT

    def test_own_new_permanent_is_not_a_topic(self) -> None:
        prev = make_state(battlefield=[])
        cur = make_state(battlefield=[card("My Bear", 1, 32)])
        assert [
            t.key for t in SELECTOR.select(prev, cur, []) if t.key in ("opponent_development", "threat")
        ] == []

    def test_threat_topic_dedupes_across_detectors(self) -> None:
        # A threat on the board matches BOTH threat scanning and development;
        # only one "threat" topic must emerge.
        prev = make_state(battlefield=[])
        cur = make_state(battlefield=[card("Farewell", 2, 33, type_line="Enchantment")])
        topics = SELECTOR.select(prev, cur, [])
        assert [t.key for t in topics].count("threat") == 1

    def test_first_snapshot_is_not_treated_as_development(self) -> None:
        # prev=None: no baseline to compare — the board scan alone must not
        # produce an "opponent revealed" topic for ordinary cards.
        cur = make_state(battlefield=[card("Ordinary Bear", 2, 34)])
        topics = SELECTOR.select(None, cur, [])
        assert [t.key for t in topics if t.key == "opponent_development"] == []


class TestPlanDrift:
    def test_changed_plan_fires_topic(self) -> None:
        memory = MatchMemory(plan_summary="race for lethal", plan_summary_prev="control")
        topics = SELECTOR.select(None, make_state(), [], memory)
        assert [t.key for t in topics] == ["plan_update"]
        assert "race for lethal" in topics[0].evidence

    def test_unchanged_plan_is_quiet(self) -> None:
        memory = MatchMemory(plan_summary="same plan")
        assert SELECTOR.select(None, make_state(), [], memory) == []

    def test_empty_memory_is_quiet(self) -> None:
        assert SELECTOR.select(None, make_state(), [], MatchMemory()) == []


class TestObservedVsHypothesis:
    def test_board_facts_are_observed(self) -> None:
        prev = make_state(battlefield=[])
        cur = make_state(battlefield=[card("Ordinary Bear", 2, 35)])
        topics = SELECTOR.select(prev, cur, [])
        assert "(observed)" in topics[0].evidence

    def test_hidden_info_claims_are_marked_hypothesis(self) -> None:
        prev = make_state(opp_hand_count=2)
        cur = make_state(opp_hand_count=5)
        topics = SELECTOR.select(prev, cur, [])
        assert "hypothesis" in topics[0].evidence.lower()

    def test_prompt_instruction_present(self) -> None:
        assert "hypothesis" in TOPIC_PROMPT_PREFIX.lower()
        assert "win probability" in TOPIC_PROMPT_PREFIX.lower()


class TestPriorityOrdering:
    def test_threat_outranks_state_shift(self) -> None:
        prev = make_state(my_life=20, battlefield=[])
        cur = make_state(my_life=16, battlefield=[card("Atraxa, Grand Unifier", 2, 36)])
        topics = SELECTOR.select(prev, cur, [])
        assert topics[0].key == "threat"
        assert topics[0].priority > topics[1].priority


# ---------------------------------------------------------------------------
# Gate ladder — ConversationController.speak_topic_if_any
# ---------------------------------------------------------------------------


def make_topic_controller(
    get_advice_result: str = "Their threat changes the plan; hold interaction.",
    cooldown: float | None = DEFAULT_COOLDOWN_SECONDS,
) -> tuple:
    """Controller over a fake coach with a controllable snapshot + clock-free
    settings; returns (controller, coach, voice, snapshot_holder)."""
    voice = MagicMock()
    voice.speak.return_value = MagicMock(played=True)
    coach = MagicMock()
    coach.voice_session = voice
    coach._voice_output = None
    coach._match_number = 1
    coach.last_match_id = "m-1"
    coach._mcp = None
    coach._game_plan_mgr = None
    inner = MagicMock()
    inner.get_advice = MagicMock(return_value=get_advice_result)
    inner._game_plan_mgr = None
    coach._coach = inner

    holder: dict = {"snap": make_state()}
    settings = MagicMock()
    settings.get = MagicMock(
        side_effect=lambda key, default=None: cooldown if key == "conversation_cooldown_seconds" else default
    )

    import arenamcp.conversation as conversation_mod

    original_get_settings = conversation_mod.get_settings
    conversation_mod.get_settings = lambda: settings

    controller = conversation_mod.ConversationController(coach, snapshot_fn=lambda: holder["snap"])
    controller.set_mode(CONVERSATION, persist=False)
    # Restore immediately — the controller captured nothing persistent.
    conversation_mod.get_settings = original_get_settings
    return controller, coach, voice, holder, inner


class TestSpeechPriority:
    def test_state_shift_topic_speaks_at_proactive_priority(self) -> None:
        ctrl, coach, voice, holder, inner = make_topic_controller()
        prev, cur = make_state(battlefield=[]), make_state(battlefield=[card("Bear", 2, 40)])
        ctrl.on_state(cur, prev, [])
        result = ctrl.speak_topic_if_any()
        assert result is not None
        assert result[1] == "proactive"
        # Only urgent topics preempt (set_mode's own stop is "mode_change").
        assert [c.args[0] for c in voice.stop_speaking.call_args_list] == ["mode_change"]


class TestCooldownAndRepetition:
    def test_zero_cooldown_allows_immediate_second_topic(self, monkeypatch) -> None:
        import arenamcp.conversation as conversation_mod

        settings = MagicMock()
        settings.get = MagicMock(side_effect=lambda key, default=None: 0)
        monkeypatch.setattr(conversation_mod, "get_settings", lambda: settings)

        voice = MagicMock()
        voice.speak.return_value = MagicMock(played=True)
        coach = MagicMock()
        coach.voice_session = voice
        coach._voice_output = None
        coach._match_number = 1
        coach.last_match_id = "m-1"
        coach._mcp = None
        coach._game_plan_mgr = None
        inner = MagicMock()
        inner.get_advice = MagicMock(return_value="Short comment.")
        inner._game_plan_mgr = None
        coach._coach = inner

        ctrl = conversation_mod.ConversationController(coach, snapshot_fn=lambda: make_state())
        ctrl.set_mode(CONVERSATION, persist=False)

        prev = make_state(battlefield=[])
        cur1 = make_state(battlefield=[card("Bear", 2, 50)])
        ctrl.on_state(cur1, prev, [])
        first = ctrl.speak_topic_if_any()
        assert first is not None

        cur2 = make_state(battlefield=[card("Bear", 2, 50), card("Otter", 2, 51)])
        ctrl.on_state(cur2, cur1, [])
        second = ctrl.speak_topic_if_any()
        # Different topic key (opponent_development still, but the cooldown is
        # 0 and repetition window 3x0=0) — the second topic speaks.
        assert second is not None
        assert voice.speak.call_count == 2

    def test_cooldown_blocks_proactive_within_window(self, monkeypatch) -> None:
        import arenamcp.conversation as conversation_mod

        settings = MagicMock()
        settings.get = MagicMock(side_effect=lambda key, default=None: 90)
        monkeypatch.setattr(conversation_mod, "get_settings", lambda: settings)

        voice = MagicMock()
        voice.speak.return_value = MagicMock(played=True)
        coach = MagicMock()
        coach.voice_session = voice
        coach._voice_output = None
        coach._match_number = 1
        coach.last_match_id = "m-1"
        coach._mcp = None
        coach._game_plan_mgr = None
        inner = MagicMock()
        inner.get_advice = MagicMock(return_value="Comment.")
        inner._game_plan_mgr = None
        coach._coach = inner

        ctrl = conversation_mod.ConversationController(coach, snapshot_fn=lambda: make_state())
        ctrl.set_mode(CONVERSATION, persist=False)

        prev = make_state(battlefield=[])
        cur1 = make_state(battlefield=[card("Bear", 2, 52)])
        ctrl.on_state(cur1, prev, [])
        assert ctrl.speak_topic_if_any() is not None

        # A THREAT arrives inside the cooldown window — urgent still speaks
        # (the cooldown gate only suppresses before priority selection, and
        # threats are not gated by the proactive cooldown).
        cur2 = make_state(battlefield=[card("Sheoldred, the Apocalypse", 2, 53)])
        ctrl.on_state(cur2, cur1, [])
        ctrl.memory.last_proactive_ts = ctrl._now()  # simulate recent proactive
        result = ctrl.speak_topic_if_any()
        assert result is not None
        assert result[1] == "urgent"
        # An urgent topic preempts whatever is speaking.
        voice.stop_speaking.assert_called_with("urgent_topic")


class TestTopicSpeechPath:
    def test_backend_error_transcript_only_never_spoken(self) -> None:
        ctrl, coach, voice, holder, inner = make_topic_controller(
            get_advice_result="[BACKEND ERROR] gateway down"
        )
        prev = make_state(battlefield=[])
        cur = make_state(battlefield=[card("Bear", 2, 58)])
        ctrl.on_state(cur, prev, [])
        result = ctrl.speak_topic_if_any()
        assert result is None
        # Never spoken…
        assert voice.speak.call_count == 0
        # …and the topic is NOT recorded as discussed (retry later).
        assert ctrl.memory.discussed_topics == {}

    def test_topic_speech_records_memory_fields(self) -> None:
        ctrl, coach, voice, holder, inner = make_topic_controller()
        prev = make_state(battlefield=[])
        cur = make_state(battlefield=[card("Bear", 2, 60)])
        ctrl.on_state(cur, prev, [])
        result = ctrl.speak_topic_if_any()
        assert result is not None
        spoken, priority, topic = result
        assert ctrl.memory.last_proactive_ts > 0
        assert topic.key in ctrl.memory.discussed_topics
        assert any(t.topic == topic.key for t in ctrl.memory.turns)
        # Voice arbiter received the identity + correct priority.
        assert voice.speak.call_count == 1
        kwargs = voice.speak.call_args.kwargs
        assert kwargs["priority"] == "proactive"
        assert kwargs["identity"] is not None

    def test_topic_prompt_contains_evidence_and_plan(self) -> None:
        ctrl, coach, voice, holder, inner = make_topic_controller()
        ctrl.memory.plan_summary = "race for lethal ~T6"
        prev = make_state(battlefield=[])
        cur = make_state(battlefield=[card("Bear", 2, 61)])
        ctrl.on_state(cur, prev, [])
        assert ctrl.speak_topic_if_any() is not None
        question = inner.get_advice.call_args.kwargs.get("question") or (
            inner.get_advice.call_args[0][1] if len(inner.get_advice.call_args[0]) > 1 else ""
        )
        assert "Bear" in question  # evidence present
        assert "race for lethal ~T6" in question  # plan present
        assert "hypothesis" in question.lower()  # observed-vs-hypothesis rule


class TestMemoryResetExtension:
    def test_reset_for_match_clears_wave3_fields(self) -> None:
        ctrl, coach, voice, holder, inner = make_topic_controller()
        ctrl.memory.last_proactive_ts = 123.0
        ctrl.memory.discussed_topics["threat"] = 456.0
        ctrl._last_topics = [
            __import__("arenamcp.conversation", fromlist=["TopicCandidate"]).TopicCandidate(
                key="threat", priority=EventPriority.THREAT, evidence="x"
            )
        ]

        ctrl.reset_for_match("new-match", 2)

        assert ctrl.memory.last_proactive_ts is None
        assert ctrl.memory.discussed_topics == {}
        assert ctrl._last_topics == []

    def test_reset_for_match_clears_threat_baseline(self) -> None:
        ctrl, _, _, _, _ = make_topic_controller()
        ctrl._threat_signature_prev = ("Sheoldred, the Apocalypse",)
        ctrl.reset_for_match("m-2", 2)
        assert ctrl._threat_signature_prev is None


# ---------------------------------------------------------------------------
# Wave 5 — M2 threat spam
# ---------------------------------------------------------------------------


class TestThreatSpamSuppression:
    def test_threat_topic_suppressed_on_unchanged_board(self) -> None:
        """M2 / SESSION F2 (repro: 4× identical Sheoldred commentary): across
        repeated batches with an IDENTICAL threat set, exactly ONE threat
        topic emerges."""
        prev = make_state(battlefield=[])
        cur = make_state(
            battlefield=[
                card("Sheoldred, the Apocalypse", 2, 70),
                card("Farewell", 2, 71, type_line="Enchantment"),
            ]
        )
        first = SELECTOR.select(prev, cur, [])
        assert [t.key for t in first] == ["threat"]

        selector_state = SELECTOR._threat_signature(cur)
        # Subsequent identical batches (board unchanged, no new trigger):
        with_baseline = SELECTOR.select(cur, dict(cur), [], None, threat_signature_prev=selector_state)
        assert [t.key for t in with_baseline if t.key == "threat"] == []

    def test_controller_suppresses_unchanged_threat_across_batches(self, monkeypatch) -> None:
        """Controller-level: repeated identical batches produce exactly one
        threat utterance (the review's 4-iteration repro)."""
        ctrl, coach, voice, holder, inner = make_topic_controller()
        prev = make_state(battlefield=[])
        cur = make_state(battlefield=[card("Sheoldred, the Apocalypse", 2, 72)])

        utterances = 0
        for i in range(4):
            ctrl.on_state(cur, cur if i else prev, [])
            result = ctrl.speak_topic_if_any()
            if result is not None:
                utterances += 1
        assert utterances == 1
        assert voice.speak.call_count == 1

    def test_urgent_threat_not_reannounced_within_window(self, monkeypatch) -> None:
        """M2: the urgent bypass lifts the global cooldown but NOT the per-key
        minimum spacing (3×cooldown) — a NEW threat trigger for a key already
        discussed inside the window does not re-announce."""
        ctrl, coach, voice, holder, inner = make_topic_controller()
        prev = make_state(battlefield=[])
        cur1 = make_state(battlefield=[card("Sheoldred, the Apocalypse", 2, 73)])
        ctrl.on_state(cur1, prev, ["threat_detected"])
        assert ctrl.speak_topic_if_any() is not None

        # A second DIFFERENT threat arrives seconds later (new threat set —
        # change detection passes), but the topic key was just spoken: the
        # per-key window must suppress it.
        cur2 = make_state(
            battlefield=[
                card("Sheoldred, the Apocalypse", 2, 73),
                card("Atraxa, Grand Unifier", 2, 74),
            ]
        )
        ctrl.on_state(cur2, cur1, ["threat_detected"])
        assert ctrl.speak_topic_if_any() is None
        assert voice.speak.call_count == 1

    def test_new_threat_after_window_speaks(self, monkeypatch) -> None:
        """The suppression is window-scoped, not permanent: with cooldown=0
        the per-key window is 0 and a changed threat set speaks again."""
        import arenamcp.conversation as conversation_mod

        settings = MagicMock()
        settings.get = MagicMock(side_effect=lambda key, default=None: 0)
        monkeypatch.setattr(conversation_mod, "get_settings", lambda: settings)

        voice = MagicMock()
        voice.speak.return_value = MagicMock(played=True)
        coach = MagicMock()
        coach.voice_session = voice
        coach._voice_output = None
        coach._match_number = 1
        coach.last_match_id = "m-1"
        coach._mcp = None
        coach._game_plan_mgr = None
        inner = MagicMock()
        inner.get_advice = MagicMock(return_value="Comment.")
        inner._game_plan_mgr = None
        coach._coach = inner

        ctrl = conversation_mod.ConversationController(coach, snapshot_fn=lambda: make_state())
        ctrl.set_mode(CONVERSATION, persist=False)

        prev = make_state(battlefield=[])
        cur1 = make_state(battlefield=[card("Sheoldred, the Apocalypse", 2, 75)])
        ctrl.on_state(cur1, prev, [])
        assert ctrl.speak_topic_if_any() is not None

        cur2 = make_state(
            battlefield=[
                card("Sheoldred, the Apocalypse", 2, 75),
                card("Atraxa, Grand Unifier", 2, 76),
            ]
        )
        ctrl.on_state(cur2, cur1, [])
        assert ctrl.speak_topic_if_any() is not None
