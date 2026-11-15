"""Sanitized public cast and separate resolution events from Nov 14 at 19:39."""

import copy
import json
import time
from dataclasses import replace

import pytest
from test_headless_booth import source  # noqa: F401
from test_nov14_strategic_replays import report

from arenamcp.card_facts.reviewed_notes import reviewed_print
from arenamcp.commentary.card_annotations import observed_annotations
from arenamcp.commentary.config import CommentaryConfig
from arenamcp.commentary.headless import HeadlessBooth


def test_real_opposing_cast_then_entry_links_only_the_observed_partner():
    obs = report("193910")
    cast, entry = [e for e in obs.events if e.kind in {"action_observed", "spell_resolved"}]
    threat = list(observed_annotations(obs, cast).values())
    assert [f["kind"] for f in threat] == ["card_threat"]
    assert threat[0]["card"] == "Lunar Convocation" and threat[0]["controller"] == 2
    assert threat[0]["scope"] == "plan" and threat[0]["visibility"] == "public"
    facts = list(observed_annotations(obs, entry).values())
    pair = next(f for f in facts if f["kind"] == "card_relationship")
    assert pair["observed_partner"] == {
        "name": "Case of the Uneaten Feast",
        "controller": 2,
        "zone": "battlefield",
    }
    assert pair["scope"] == "possibility" and pair["activation"].startswith("not_established")
    assert not observed_annotations(obs, replace(entry, payload={**entry.payload, "to_zone": "graveyard"}))
    assert not observed_annotations(obs, replace(cast, visibility="private"))
    invalid = copy.deepcopy(obs)
    focal = next(c for c in invalid.public_state["zones"]["battlefield"] if c.get("grp_id") == 91759)
    focal["oracle_text"] += " changed"
    assert not observed_annotations(invalid, cast)
    assert not observed_annotations(invalid, entry)
    assert not reviewed_print(dict(focal, face_down=True))


@pytest.mark.usefixtures("source")
def test_normal_booth_generation_and_audit_can_cite_public_pair():
    obs = report("193910")
    entry = next(e for e in obs.events if e.kind == "spell_resolved")
    obs = replace(obs, events=(entry,), private_state={"hand": [{"name": "SecretCanary"}]})
    calls = []

    def rpc(system, ctx, audit):
        calls.append(audit)
        assert "SecretCanary" not in json.dumps(ctx)
        keys = [k for k, v in ctx["facts"].items() if v.get("kind") == "card_relationship"]
        assert len(keys) == 1 and ctx["facts"][keys[0]]["event_kind"] == "permanent_entered"
        if audit:
            return {
                "checks": [
                    {
                        "index": 0,
                        "adds_insight": True,
                        "scope": "position",
                        "supported": True,
                        "unsupported_claim": "",
                    }
                ]
            }
        return {
            "turns": [
                {
                    "role": "color_analyst",
                    "text": "Future life gain could pressure the opponent even without a clean attack.",
                    "refs": [ctx["new_events"][0], keys[0]],
                }
            ]
        }

    booth = HeadlessBooth(CommentaryConfig(mode="commentary", generative=True), rpc, time.monotonic)
    lines, status = booth.generate(obs)
    assert calls == [False, True] and len(lines) == 1, status


def test_observed_transfer_without_cast_is_an_entry_and_never_a_resolution():
    from test_outcomes import add, game

    from arenamcp.gamestate_models import ZoneType

    g = game()
    g.zones[2].zone_type = ZoneType.BATTLEFIELD
    add(g, 102, "Public card", kind="Return", source=1, destination=2)
    assert [(row["kind"], row["payload"]["to_zone"]) for row in g._commentary_journal] == [
        ("permanent_entered", "battlefield")
    ]


def test_same_name_rebalanced_print_supplies_no_ordinary_winota_claim():
    import dataclasses

    from arenamcp.card_facts.reviewed_notes import corpus

    printed = corpus()[0][71283]
    card = dict(dataclasses.asdict(printed), grp_id=71283)
    assert reviewed_print(card)
    assert not reviewed_print(dict(card, grp_id=96294))
    assert not reviewed_print(
        dict(card, oracle_text=card["oracle_text"].replace("a non-Human", "one or more non-Human"))
    )
