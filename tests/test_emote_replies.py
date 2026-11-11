from arenamcp.emote_replies import EmoteReplies
from arenamcp.gamestate import GameState


def event(text="Phrase_Basic_Hello", **kw):
    return {"text": text, "match_id": "match", "game_number": 1, "received_at": 100, **kw}


def test_appropriate_replies_and_no_conversation_loop():
    now = [100.0]
    replies = EmoteReplies(lambda: now[0])
    assert replies.choose([event()], ("match", 1), True)["reply"] == "hello"
    now[0] = 111
    assert replies.choose([event(received_at=111)], ("match", 1), True) is None
    assert replies.choose([event("Phrase_Basic_Thanks", received_at=111)], ("match", 1), True) is None
    assert (
        replies.choose([event("Phrase_Basic_Nice", received_at=111)], ("match", 1), True)["reply"] == "thanks"
    )


def test_disabled_stale_wrong_game_and_cooldown_are_discarded():
    for enabled, overrides in [(False, {}), (True, {"received_at": 90}), (True, {"received_at": 101})]:
        replies = EmoteReplies(lambda: 100)
        assert replies.choose([event(**overrides)], ("match", 1), enabled) is None
        assert replies.choose([event()], ("match", 1), True) is None
    replies = EmoteReplies(lambda: 100)
    assert replies.choose([event(game_number=2)], ("match", 1), True) is None
    assert replies.choose([event()], ("match", 1), True)
    assert replies.choose([event("Phrase_Basic_Nice")], ("match", 1), True) is None


def test_parser_records_only_known_opponent_emotes():
    gs = GameState()
    gs.local_seat_id = 1
    gs.match_id = "match"
    gs.game_number = 1

    def message(sender=2, recipient=1, text="Phrase_Basic_Hello"):
        return {"systemSeatIds": [recipient], "uiMessage": {"seatIds": [sender], "onChat": {"text": text}}}

    gs._record_ui_message_event(message(), 100)
    gs._record_ui_message_event(message(sender=1), 100)
    gs._record_ui_message_event(message(recipient=2), 100)
    gs._record_ui_message_event(message(text="Phrase_Basic_Thanks"), 100)
    facts, seq = gs.opponent_emotes_since(0)
    assert seq == 1 and facts == [dict(event(), seq=1)]
    assert gs.opponent_emotes_since(seq) == ([], seq)


def test_mac_reply_uses_existing_guarded_wheel():
    from test_oops_emote import WHEEL, MacBridgeAdapter, clicks, emote_world

    for kind, phrase in [("hello", "Phrase_Basic_Hello"), ("thanks", "Phrase_Basic_Thanks")]:
        world = emote_world(on_wheel=(phrase,))
        result = MacBridgeAdapter(world.send).handle({"action": "send_emote", "emote": kind})
        assert result["ok"] and result["emote_id"] == phrase
        assert clicks(world) == [(WHEEL, [{"str": phrase}])]


def test_mixin_discards_startup_and_disabled_messages(monkeypatch):
    import sys
    from types import SimpleNamespace

    from arenamcp.emote_replies import _EmoteRepliesMixin

    class Coach(_EmoteRepliesMixin):
        enabled = False

        def _emote_replies_allowed(self):
            return self.enabled

    gs = GameState()
    monkeypatch.setitem(sys.modules, "arenamcp.server", SimpleNamespace(game_state=gs))
    coach = Coach()
    gs._opponent_emotes.append(dict(event(), seq=1))
    gs._emote_seq = 1
    coach._observe_emote_replies({"match_id": "match", "game_number": 1})
    assert coach._emote_reply_seq == 1
    gs._opponent_emotes.append(dict(event(), seq=2))
    gs._emote_seq = 2
    coach._emote_replies.clock = lambda: 100
    coach._observe_emote_replies({"match_id": "match", "game_number": 1})
    coach.enabled = True
    assert coach._emote_replies.choose([event()], ("match", 1), True) is None


def test_sender_rechecks_autoplay_game_and_freshness(monkeypatch):
    import sys
    from types import SimpleNamespace

    import arenamcp.emote_replies as replies
    import arenamcp.gre_bridge as bridge
    from arenamcp.emote_replies import _EmoteRepliesMixin

    class Coach(_EmoteRepliesMixin):
        enabled = True

        def _emote_replies_allowed(self):
            return self.enabled

    sent = []
    gs = SimpleNamespace(match_id="match", game_number=1)
    monkeypatch.setitem(sys.modules, "arenamcp.server", SimpleNamespace(game_state=gs))
    monkeypatch.setattr(
        bridge, "get_bridge", lambda: SimpleNamespace(send_emote=lambda *a, **kw: sent.append((a, kw)))
    )
    monkeypatch.setattr(replies.time, "time", lambda: 100)
    coach = Coach()
    e = dict(event(), reply="hello")
    coach.enabled = False
    coach._send_emote_reply(e)
    coach.enabled = True
    gs.game_number = 2
    coach._send_emote_reply(e)
    gs.game_number = 1
    coach._send_emote_reply(dict(e, received_at=90))
    assert sent == []
    coach._send_emote_reply(e)
    assert sent == [(("hello",), {"expected_match_id": "match", "expected_game_number": 1})]


def test_old_windows_plugin_does_not_enable_replies():
    from arenamcp.gre_bridge import GREBridge

    bridge = GREBridge()
    bridge._connected = True
    bridge.plugin_version = "0.7.4"
    assert bridge.emote_supported
    assert not bridge.reply_emotes_supported
    bridge.plugin_version = "0.7.5"
    assert bridge.reply_emotes_supported
    bridge._connected = False
    assert not bridge.reply_emotes_supported
