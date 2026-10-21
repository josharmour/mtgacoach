"""BepInEx: a payment whose cost is a choice runs through the reflection adapter.

2026-10-18 22:25 (Linux/Proton): Gallia, Tragic Host's graveyard ability asked
to exile another creature card from the graveyard. The BepInEx plugin reported
PayCostsRequest with no actions, the autopilot tried auto-pay and went MANUAL
REQUIRED. The Mac adapter shapes that EffectCost selection as SelectN and
submits it, so on BepInEx the bridge borrows those two steps.
"""

from __future__ import annotations

from arenamcp.gre_bridge import GREBridge

PLUGIN_PAYCOSTS = {
    "ok": True,
    "has_pending": True,
    "request_type": "PayCosts",
    "request_class": "PayCostsRequest",
    "actions": [],
}
SHAPED = {
    "ok": True,
    "has_pending": True,
    "request_type": "SelectN",
    "payment_selection": True,
    "select_n_ids": [212, 214, 240],
    "select_n_min": 1,
    "select_n_max": 1,
}


class FakeAdapter:
    plays_matches = False

    def __init__(self, shaped):
        self.shaped, self.handled = shaped, []

    def handle(self, command, timeout=None):
        self.handled.append(command)
        if command["action"] == "get_pending_actions":
            return self.shaped
        return {"ok": True, "submitted_type": "SelectN"}


def bridge_with(plugin_reply, adapter):
    bridge = GREBridge(port=0)
    sent = []

    def send_safe(command, timeout=None):
        sent.append(command)
        return plugin_reply if command["action"] == "get_pending_actions" else {"ok": True}

    bridge._send_safe = send_safe
    bridge._mac_adapter = adapter
    return bridge, sent


def test_cost_choice_is_read_and_submitted_through_the_adapter():
    adapter = FakeAdapter(SHAPED)
    bridge, sent = bridge_with(PLUGIN_PAYCOSTS, adapter)
    pending = bridge.get_pending_actions()
    assert pending["request_type"] == "SelectN" and pending["payment_selection"]
    assert bridge.submit_selection([214]) is True
    assert adapter.handled[-1] == {"action": "submit_selection", "ids": [214]}
    assert [c["action"] for c in sent] == ["get_pending_actions"]


def test_mana_only_payment_keeps_the_plugin_reply_and_path():
    adapter = FakeAdapter({"ok": True, "has_pending": True, "request_type": "PayCosts"})
    bridge, sent = bridge_with(PLUGIN_PAYCOSTS, adapter)
    assert bridge.get_pending_actions() is PLUGIN_PAYCOSTS
    bridge.submit_selection([1])
    assert sent[-1] == {"action": "submit_selection", "ids": [1]}


def test_other_requests_never_ask_the_adapter():
    adapter = FakeAdapter(SHAPED)
    actions = {"ok": True, "has_pending": True, "request_class": "ActionsAvailableRequest"}
    bridge, sent = bridge_with(actions, adapter)
    assert bridge.get_pending_actions() is actions
    assert adapter.handled == []


def test_native_mac_adapter_is_left_to_its_own_routing():
    adapter = FakeAdapter(SHAPED)
    adapter.plays_matches = True
    bridge, _sent = bridge_with(PLUGIN_PAYCOSTS, adapter)
    assert bridge.get_pending_actions() is PLUGIN_PAYCOSTS
    assert adapter.handled == []
