"""Post-fix verification: feed the EXACT duplicated-HTML oracle blocks observed
in the real RL corpus (tools/training/data/*.jsonl) through the renderer and
assert (a) each ability appears exactly once and (b) no HTML tags remain.
"""
import re
import collections
from arenamcp.coach import CoachEngine

# Exactly the oracle_text carried by the real RL data (gate_play_decisions).
OOZE = (
    "Whenever a creature you control with a +1/+1 counter on it leaves the "
    "battlefield, create a Mutagen token for each +1/+1 counter on it.\n"
    "Whenever a creature you control with a <nobr>+1/+1</nobr> counter on it "
    "leaves the battlefield, create a Mutagen token for each <nobr>+1/+1</nobr> "
    "counter on it.\n"
    "Whenever a creature you control with a +1/+1 counter on it leaves the "
    "battlefield, create a Mutagen token for each +1/+1 counter on it.\n"
    "{oT}: Exile target card from a graveyard. Create a Mutagen token."
)
SILKGUARD = (
    "Put a +1/+1 counter on each of up to X target creatures you control.\n"
    "Put a <nobr>+1/+1</nobr> counter on each of up to X target creatures you "
    "control.\n"
    "Put a +1/+1 counter on each of up to X target creatures you control.\n"
    "Auras, Equipment, and modified creatures you control gain hexproof until "
    "end of turn."
)

game_state = {
    "players": [
        {"seat_id": 1, "is_local": True, "life_total": 20, "lands_played": 1},
        {"seat_id": 2, "is_local": False, "life_total": 12},
    ],
    "turn": {
        "turn_number": 3,
        "active_player": 1,
        "priority_player": 1,
        "phase": "Phase_Main1",
        "step": "",
    },
    "battlefield": [
        {
            "instance_id": 501,
            "name": "Haughty Djinn",
            "type_line": "Creature — Djinn Wizard",
            "owner_seat_id": 2,
            "controller_seat_id": 2,
            "power": 5, "toughness": 4,
            "turn_entered_battlefield": 2,
            "is_tapped": False,
            "oracle_text": "Flying, lifelink\n<nobr>Whenever <i>Haughty Djinn</i> enters</nobr>",
        }
    ],
    "hand": [
        {"instance_id": 1, "name": "The Ooze", "type_line": "Artifact", "mana_cost": "{2}",
         "owner_seat_id": 1, "controller_seat_id": 1, "oracle_text": OOZE},
        {"instance_id": 2, "name": "Silkguard", "type_line": "Instant", "mana_cost": "{X}{G}",
         "owner_seat_id": 1, "controller_seat_id": 1, "oracle_text": SILKGUARD},
    ],
    "stack": [], "graveyard": [], "command": [], "exile": [],
    "revealed_cards": {}, "legal_actions": [], "pending_decision": None,
    "damage_taken": {}, "recent_events": [],
}

engine = CoachEngine.__new__(CoachEngine)
prompt = engine._format_game_context(game_state, for_planner=True)

print("SAMPLE PROMPT (for_planner=True):\n" + "-" * 70)
print(prompt)
print("-" * 70)

# --- assertions ---
checks = {
    "Ooze trigger line": (
        "Whenever a creature you control with a +1/+1 counter on it leaves the "
        "battlefield, create a Mutagen token for each +1/+1 counter on it."
    ),
    "Ooze activated ability": (
        "{oT}: Exile target card from a graveyard. Create a Mutagen token."
    ),
    "Silkguard trigger line": (
        "Put a +1/+1 counter on each of up to X target creatures you control."
    ),
    "Silkguard static line": (
        "Auras, Equipment, and modified creatures you control gain hexproof until end of turn."
    ),
}
print("\nOCCURRENCE COUNTS (must each be exactly 1):")
ok = True
for label, line in checks.items():
    n = prompt.count(line)
    print(f"  {label:28s}: {n}x")
    if n != 1:
        ok = False

tags = sorted(set(re.findall(r"<[^>]+>", prompt)))
print(f"\nRaw HTML tags remaining in prompt: {tags if tags else 'NONE'}")
if tags:
    ok = False

print("\nRESULT:", "PASS ✅ (each ability once, zero HTML)" if ok else "FAIL ❌")
