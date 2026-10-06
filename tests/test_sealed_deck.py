"""Sealed events: deep-pool deck building with supported splashes, and the pool-opening screen."""

from __future__ import annotations

from collections import Counter
from types import SimpleNamespace

from arenamcp.draft_event import DraftEventDriver
from arenamcp.event_course import CourseTracker
from arenamcp.limited_deck import (
    _deck_cards,
    _produces,
    candidate_decks,
    deck_choice_summary,
    fallback_deck,
    score_deck,
    validate_deck,
)
from arenamcp.mac_bridge_adapter import MacBridgeAdapter


def card(grp_id, name, cost, *, gih=0.55, type_line="Creature — Soldier", text="Vigilance", rarity="common"):
    return {
        "grp_id": grp_id,
        "name": name,
        "mana_cost": cost,
        "type_line": type_line,
        "oracle_text": text,
        "rarity": rarity,
        "gih_wr": gih,
    }


def sealed_pool(*, dual=True, bomb_cost="{4}{B}"):
    """Six packs' worth: deep blue-red, a black bomb and black removal, chaff elsewhere."""
    pool = [card(i, f"Isle Wisp {i}", "{1}{U}", gih=0.56, text="Flying") for i in range(1, 9)]
    pool += [card(20 + i, f"Forge Pup {i}", "{1}{R}", gih=0.555) for i in range(1, 9)]
    pool += [
        card(
            40 + i,
            f"Firebolt {i}",
            "{1}{R}",
            gih=0.575,
            type_line="Instant",
            text="Firebolt deals 3 damage to target creature.",
        )
        for i in range(1, 4)
    ]
    pool += [
        card(50 + i, f"Tide Scholar {i}", "{2}{U}", gih=0.55, text="When this enters, draw a card.")
        for i in range(1, 6)
    ]
    pool += [
        card(
            70,
            "Grave Tyrant",
            bomb_cost,
            gih=0.66,
            rarity="mythic",
            text="Flying\nWhen this enters, destroy target creature.",
        ),
        card(71, "Night Snare", "{2}{B}", gih=0.60, type_line="Instant", text="Destroy target creature."),
        card(72, "Doom Rite", "{B}{B}", gih=0.60, type_line="Sorcery", text="Destroy target creature."),
        card(73, "Bog Rat", "{1}{B}", gih=0.53),
    ]
    pool += [card(100 + i, f"Meadow Chaff {i}", "{2}{W}", gih=0.48) for i in range(1, 25)]
    pool += [card(150 + i, f"Moss Chaff {i}", "{2}{G}", gih=0.47) for i in range(1, 25)]
    if dual:
        pool.append(
            card(
                200,
                "Ashen Tarn",
                "",
                type_line="Land",
                text="This land enters tapped.\n{oT}: Add {oU} or {oB}.",
            )
        )
    pool.append(card(201, "Stone Quarry", "", type_line="Land", text="{oT}: Add {oR}."))
    return pool


def test_arena_and_scryfall_mana_text_both_count_as_sources():
    assert _produces({"oracle_text": "{oT}: Add {oU} or {oB}."}) == {"U", "B"}
    assert _produces({"oracle_text": "{T}: Add {G}."}) == {"G"}
    room = "This land enters tapped. As it enters, choose a color.\n{oT}: Add one mana of the chosen color."
    assert _produces({"oracle_text": room}) == set("WUBRG")


def test_sealed_splashes_a_single_pip_bomb_with_enough_sources():
    pool = sealed_pool()
    build = fallback_deck(pool, fmt="sealed")
    names = Counter(c["name"] for c in _deck_cards(build, pool))
    assert names["Grave Tyrant"] == 1 and names["Night Snare"] == 1
    assert names["Doom Rite"] == 0  # double black is never splashed
    assert build["quality"]["format"] == "sealed" and build["quality"]["splash"] == "B"
    # Three black sources: the U/B dual plus basics; the mono-red nonbasic stays out.
    kept_lands = {e["name"] for e in build["main_deck"]} & {"Ashen Tarn", "Stone Quarry"}
    assert kept_lands == {"Ashen Tarn"}
    assert 1 + build["basic_lands"]["B"] >= 3
    assert build["basic_lands"]["U"] >= 6 and build["basic_lands"]["R"] >= 6
    validate_deck(build, pool, source="heuristic")
    summary = deck_choice_summary(build, pool, option=1)
    assert summary.startswith("Option 1 is blue-red splashing black for Grave Tyrant")


def test_draft_splash_needs_fixing_unless_the_card_is_a_bomb():
    pool = sealed_pool(dual=False, bomb_cost="{4}{B}")
    for candidate in candidate_decks(pool, top=40, fmt="draft"):
        splashed = set(candidate["quality"]["splash_cards"])
        assert "Night Snare" not in splashed or "Grave Tyrant" in splashed


def test_splash_costs_consistency_so_marginal_cards_stay_home():
    pool = sealed_pool()
    for c in pool:
        if c["name"] in {"Grave Tyrant", "Night Snare"}:
            c["gih_wr"] = 0.555  # no longer worth weakening the mana
    build = fallback_deck(pool, fmt="sealed")
    assert not build["quality"]["splash"]
    assert set(build["basic_lands"]) == {"U", "R"}


def test_sealed_weights_bombs_more_than_draft():
    pool = sealed_pool()
    deck = [{"grp_id": c["grp_id"], "count": 1} for c in pool if c["name"] in {"Grave Tyrant"}]
    deck += [{"grp_id": c["grp_id"], "count": 1} for c in pool if c["name"].startswith(("Isle", "Tide"))]
    assert score_deck(deck, pool, fmt="sealed")["score"] > score_deck(deck, pool, fmt="draft")["score"]


def test_review_alternative_is_a_different_pair_not_another_splash():
    pool = sealed_pool()
    build = fallback_deck(pool, fmt="sealed")
    options = build["deck_options"]
    main = lambda o: set(o["quality"]["colors"])  # noqa: E731 - main pair, splash excluded
    assert main(options[1]) != main(options[0])


# ---------------------------------------------------------------------------
# Driver and bridge: the pool-opening screen and format detection
# ---------------------------------------------------------------------------


class Bridge:
    connected = True

    def __init__(self, replies):
        self.replies, self.sent = replies, []

    def draft_command(self, action, **fields):
        self.sent.append(action)
        reply = self.replies.get(action, {"ok": False, "error": "unscripted"})
        return reply(fields) if callable(reply) else reply


def driver(bridge, **kwargs):
    service = SimpleNamespace(get=lambda code: None, ensure=lambda code: None, quick=lambda code: None)
    d = DraftEventDriver(
        bridge_fn=lambda: bridge,
        tracker_fn=lambda: CourseTracker(),
        primer_service=service,
        card_db=SimpleNamespace(get_card=lambda g: SimpleNamespace(name=f"Card {g}", expansion_code="TST")),
        **kwargs,
    )
    d.set_enabled(True)
    return d


def test_driver_opens_the_sealed_pool_then_continues():
    steps = iter(["open", "revealing", "done"])
    bridge = Bridge(
        {
            "get_screen": {"ok": True, "sealed_open": True},
            "finish_sealed_open": lambda fields: {"ok": True, "step": next(steps), "gems_added": 0},
        }
    )
    d = driver(bridge)
    for _ in range(3):
        d._step()
    assert bridge.sent.count("finish_sealed_open") == 3 and not d.paused_reason
    assert d.run.sealed_done_presses == 1


def test_driver_pauses_if_done_does_not_leave_the_pool_screen():
    bridge = Bridge(
        {
            "get_screen": {"ok": True, "sealed_open": True},
            "finish_sealed_open": {"ok": True, "step": "done", "gems_added": 50},
        }
    )
    d = driver(bridge)
    for _ in range(4):
        d._step()
    assert "claim the reward" in d.paused_reason


def test_sealed_event_builds_with_sealed_weights(monkeypatch):
    pool = sealed_pool()
    by_id = {c["grp_id"]: c for c in pool}
    sideboard = [{"grp_id": g, "count": n} for g, n in Counter(c["grp_id"] for c in pool).items()]
    seen = {}

    def fake_fallback(cards, rates=None, *, fmt="draft"):
        seen["fmt"] = fmt
        return fallback_deck(cards, rates, fmt=fmt)

    monkeypatch.setattr("arenamcp.limited_deck.fallback_deck", fake_fallback)
    bridge = Bridge(
        {
            "get_screen": {"ok": True, "deck_builder": True},
            "get_limited_pool": {
                "ok": True,
                "main_deck": [],
                "sideboard": sideboard,
                "basics_in_pool": {"7001": 99, "7002": 99, "7003": 99},
            },
            "set_limited_deck": lambda fields: {"ok": True, "main_deck": fields["main_deck"]},
            "submit_limited_deck": {"ok": True},
        }
    )
    d = driver(bridge, pool_cards_fn=lambda ids, code: [by_id[g] for g in ids])
    d.run.event_name = "Sealed_FRA_20261001"
    d._basics = {7001: "U", 7002: "R", 7003: "B"}
    d._step()
    assert seen["fmt"] == "sealed" and "submit_limited_deck" in bridge.sent


class World:
    def __init__(self, open_shown, done_shown):
        self.calls = []
        self.state = {"_openButton": open_shown, "_doneButton": done_shown}

    def send(self, command, timeout):
        results = []
        for op in command["ops"]:
            kind = op["op"]
            if kind == "find":
                results.append(
                    {"$c": op["class"], "$h": 10}
                    if op["class"] == "SealedBoosterOpenAnimation"
                    else {"$none": 1}
                )
                continue
            target = op["target"].get("h") or (results[op["target"]["ref"]] or {}).get("$h")
            if kind == "call":
                self.calls.append((target, op["method"]))
                results.append(None)
            elif op["member"] in ("_openButton", "_doneButton"):
                results.append({"$c": "CustomButton", "$h": 20 if op["member"] == "_openButton" else 30})
            elif op["member"] == "gameObject":
                results.append({"$c": "GameObject", "$h": target + 1})
            elif op["member"] == "activeSelf":
                results.append(self.state["_openButton" if target == 21 else "_doneButton"])
            elif op["member"] == "Interactable":
                results.append(True)
            elif op["member"] == "_gemsAdded":
                results.append(0)
            else:
                results.append(None)
        return {"ok": True, "results": results}


def test_bridge_presses_open_then_done_and_never_both():
    for open_shown, done_shown, step, method in (
        (True, False, "open", "OpenButton_OnClick"),
        (False, True, "done", "DoneButton_OnClick"),
        (False, False, "revealing", None),
    ):
        world = World(open_shown, done_shown)
        reply = MacBridgeAdapter(world.send).handle({"action": "finish_sealed_open"})
        assert reply["ok"] and reply["step"] == step
        assert world.calls == ([(10, method)] if method else [])
