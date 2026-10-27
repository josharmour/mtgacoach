"""Session-only quest control. No autoplay is started implicitly, no paid entries."""

from __future__ import annotations

import math
import time

from arenamcp.auto_quest import plan, quest_descriptions, read_snapshot


class AutoQuestMixin:
    def inspect_auto_quest(self) -> None:
        """Read-only inspection works without enabling Autoplay or starting a match."""
        from arenamcp.server import _log_path

        try:
            snapshot = read_snapshot(_log_path())
            descriptions = quest_descriptions()
            age = time.time() - snapshot.observed_at
            if snapshot.quests is None or not 0 <= age <= 1800:
                detail = "Quest state unknown/stale; open Arena Home to refresh."
            else:
                rows = []
                for quest in snapshot.quests:
                    text = descriptions.get(str(quest.get("locKey")) + "_Text", str(quest.get("locKey")))
                    progress = quest.get("endingProgress", 0 if quest.get("isNewQuest") else "unknown")
                    rows.append(f"{text}: {progress}/{quest.get('goal', 'unknown')}")
                detail = "; ".join(rows) or "No active quests in the latest response."
                detail += f" Observed {int(age)} seconds ago."
            detail += " Mastery XP/season: unknown."
            self.ui.status("AUTO_QUEST", detail)
        except Exception as exc:
            self.ui.status("AUTO_QUEST", f"Quest state unavailable: {exc}")

    def set_auto_quest(
        self, enabled: bool, goal: str = "quests", minutes: int = 60, deadline: float | None = None
    ) -> None:
        if not enabled:
            self._auto_quest = None
            self.set_auto_queue(False)
            self.ui.status("AUTO_QUEST", "Off")
            return
        if goal not in {"quests", "daily4", "daily10", "weekly15"}:
            raise ValueError("Unknown quest goal")
        if type(minutes) is not int or not 1 <= minutes <= 1440:
            raise ValueError("Session must last 1–1440 minutes")
        now = time.time()
        end = min(now + minutes * 60, float(deadline)) if deadline is not None else now + minutes * 60
        if not math.isfinite(end) or end <= now:
            raise ValueError("Deadline must be in the future")
        self.set_auto_queue(True)
        # Explicit start required after a runtime restart; never restore quest navigation.
        self.settings.set("auto_queue_enabled", False)
        self._auto_quest = dict(
            goal=goal,
            deadline=end,
            started=now,
            next_poll=0,
            observed=None,
            prepared=None,
            home=None,
            target=None,
        )
        self.ui.status("AUTO_QUEST", "Waiting for a match played with Autoplay; free queues only.")

    def _prepare_auto_quest(self, state: dict) -> bool:
        run = getattr(self, "_auto_quest", None)
        if not run:
            return True
        now = time.time()
        if now >= run["deadline"]:
            self.set_auto_quest(False)
            self.ui.status("AUTO_QUEST", "Grind window ended; no further matches will be queued.")
            return False
        from arenamcp.server import _log_path, get_completed_match_for_navigation

        completed = get_completed_match_for_navigation()
        ended = completed.get("match_id")
        current = state.get("match_id")
        if current and current != ended:
            run["observed"] = current
            return True
        if not ended or ended != run["observed"] or completed.get("completed_at", 0) < run["started"]:
            return False
        if run["prepared"] == ended and now < run.get("valid_until", 0):
            return True
        if now < run["next_poll"]:
            return False
        run["next_poll"] = now + 10
        self._suspend_auto_queue()
        try:
            bridge = self._deck_library_bridge()
            if bridge is None:
                self.ui.status("AUTO_QUEST", "Paused: connect the bridge to verify owned decks and cards.")
                return False
            if run["home"] != ended:
                if not bridge.return_to_home():
                    self.ui.status("AUTO_QUEST", "Waiting to leave the completed match.")
                    return False
                run["home"] = ended
                return False
            snapshot = read_snapshot(_log_path())
            descriptions = quest_descriptions()
            # First check freshness/completion before expensive collection and card reads.
            args = dict(
                now=now,
                deadline=run["deadline"],
                goal=run["goal"],
                after=max(run["started"], completed.get("completed_at", 0)),
                descriptions=descriptions,
            )
            result = plan(snapshot, decks={}, collection={}, cards={}, **args)
            if result.status == "blocked" and result.detail.startswith("No verified"):
                from arenamcp.server import get_card_info

                decks, _ = self.collect_decks()
                owned = bridge.draft_command("get_collection", timeout=12.0)
                if not owned.get("ok"):
                    self.ui.status(
                        "AUTO_QUEST", "Paused: collection could not be verified. No crafting will occur."
                    )
                    return False
                decks = {key: deck for key, deck in decks.items() if key in snapshot.pairs}
                ids = {gid for deck in decks.values() for gid in deck.deck_cards()}
                cards = {gid: get_card_info(gid) for gid in ids}
                result = plan(snapshot, decks=decks, collection=owned.get("cards") or {}, cards=cards, **args)
            detail = result.detail
            if result.coverage_gaps:
                detail += " Deck coverage gaps: " + "; ".join(result.coverage_gaps)
            self.ui.status("AUTO_QUEST", detail)
            if result.status == "complete":
                self._auto_quest = None
                self.set_auto_queue(False)
                return False
            if result.status != "ready":
                return False
            fingerprint = repr(
                (
                    snapshot.quests,
                    snapshot.rewards.get("_dailyRewardSequenceId"),
                    snapshot.rewards.get("_weeklyRewardSequenceId"),
                )
            )
            run["stalled"] = run.get("stalled", 0) + 1 if run.get("fingerprint") == fingerprint else 0
            run["fingerprint"] = fingerprint
            if run["stalled"] >= 3:
                self.set_auto_quest(False)
                self.ui.status("AUTO_QUEST", "Stopped: three matches without observed quest/reward progress.")
                return False
            run["target"] = {
                "deck_name": result.deck_name,
                "deck_id": result.deck_id,
                "event_id": result.event_id,
                "quest_mode": True,
            }
            run["prepared"] = ended
            run["valid_until"] = snapshot.observed_at + 1800
            return time.time() < run["deadline"]
        except Exception as exc:
            self.ui.status("AUTO_QUEST", f"Paused: quest state could not be verified: {exc}")
            return False
