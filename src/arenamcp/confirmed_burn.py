"""Fill omitted damage only from actual GRE damage events, never planned casts.

GameState's AnnotationType_DamageDealt handler emits recent_events with
source_id, target_id, amount, turn and phase. A target submission or a spell
leaving the stack is not that evidence. Missing event/public data stays unknown.
"""

from dataclasses import dataclass, field

from arenamcp.mana import get_local_seat_id
from arenamcp.play_safety import damage_effect


@dataclass
class ConfirmedBurn:
    key: tuple | None = None
    sources: dict = field(default_factory=dict)
    last_marks: dict = field(default_factory=dict)

    def reconcile(self, state: dict) -> dict:
        turn = (state.get("turn") or {}).get("turn_number")
        key = (state.get("match_id"), state.get("game_number"), turn)
        if key != self.key or not key[0] or turn is None:
            self.key, self.sources = key, {}
        self.last_marks = {"match_id": key[0], "game_number": key[1], "turn": turn, "damage": {}}
        # Cleanup removes marked damage even before the turn number advances.
        if str((state.get("turn") or {}).get("step") or "").lower().removeprefix("step_") == "cleanup":
            self.sources.clear()
            return state
        local = get_local_seat_id(state)
        if local is None:
            return state
        for zone in ("hand", "graveyard", "exile", "stack"):
            for card in state.get(zone) or []:
                iid = card.get("instance_id")
                if iid:
                    self.sources[iid] = card
        # Only narrow, identified instant/sorcery burn. Damage from infect or
        # wither isn't ordinary marked damage; unknown sources cannot fill it.
        burns = {
            iid: card
            for iid, card in self.sources.items()
            if card.get("controller_seat_id", card.get("owner_seat_id")) == local
            and any(t in str(card.get("type_line") or "").lower() for t in ("instant", "sorcery"))
            and not card.get("is_countered")
            and not any(t in str(card.get("oracle_text") or "").lower() for t in ("wither", "infect"))
            and damage_effect(card) is not None
        }
        if any(
            any(
                t in str(c.get("oracle_text") or "").lower()
                for t in ("wither", "infect", "regenerate", "remove all damage")
            )
            for c in state.get("battlefield") or []
        ):
            return state  # replacement/reset effects need stronger current damage evidence
        events = {}
        for event in state.get("recent_events") or []:
            if event.get("type") != "damage_dealt" or event.get("turn") != turn:
                continue
            source, target, amount = event.get("source_id"), event.get("target_id"), event.get("amount")
            if source not in burns or type(target) is not int or type(amount) is not int or amount <= 0:
                continue
            # One supported damage application per spell/target. Repeated log
            # delivery must not add it twice; multi-hit unknown effects aren't
            # reconstructed by this narrow burn helper.
            events[(source, target)] = max(events.get((source, target), 0), amount)
        totals = {}
        for (_source, target), amount in events.items():
            totals[target] = totals.get(target, 0) + amount
        changed = False
        battlefield = []
        for card in state.get("battlefield") or []:
            iid = card.get("instance_id")
            # An explicit damage value, including zero after a reset, wins.
            # Current public 3 plus observed 3 is still 3, never 6.
            if (
                iid in totals
                and "damage" not in card
                and card.get("damaged_this_turn") is not False
                and card.get("controller_seat_id") not in (None, local)
            ):
                card = {**card, "damage": totals[iid], "damaged_this_turn": True}
                self.last_marks["damage"][iid] = totals[iid]
                changed = True
            battlefield.append(card)
        return {**state, "battlefield": battlefield} if changed else state
