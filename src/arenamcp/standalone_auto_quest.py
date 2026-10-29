"""Session-only quest control. No autoplay is started implicitly, no paid entries."""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from datetime import datetime
from functools import partial

from arenamcp.auto_quest import (
    COLORS,
    build_quest_deck,
    plan,
    quest_descriptions,
    read_snapshot,
)
from arenamcp.deck_library import DeckRecord

logger = logging.getLogger(__name__)


class AutoQuestMixin:
    def _grind_status(self, category: str, text: str) -> None:
        run = getattr(self, "_auto_quest", None)
        if category == "AUTO_QUEST" and run:
            self._announce_grind(run, ("status", text), text)
        else:
            self.ui.status(category, text)

    def _narrate_grind_progress(self, run, snapshot, descriptions):
        previous = run.setdefault("spoken_progress", {})
        messages = []
        for raw in snapshot.quests or []:
            if not isinstance(raw, dict):
                continue
            key = str(raw.get("locKey"))
            progress = raw.get("endingProgress", 0 if raw.get("isNewQuest") else None)
            goal = raw.get("goal")
            text = descriptions.get(key + "_Text", "")
            if not text or type(progress) is not int or type(goal) is not int or goal <= 0:
                continue
            value = (progress, goal)
            if previous.get(key) == value:
                continue
            first = key not in previous
            previous[key] = value
            objective = text.replace("{quantity}", str(goal))
            message = (
                f"Quest complete: {objective}"
                if progress >= goal
                else f"{'Going after the quest' if first else 'Quest progress'}: {objective} {progress} of {goal} done."
            )
            messages.append(message)
        for period, target in (("daily", 4 if run.get("goal") == "daily4" else 10), ("weekly", 15)):
            count = snapshot.rewards.get(f"_{period}RewardSequenceId")
            reset_text = snapshot.rewards.get(f"_{period}RewardResetTimestamp", "")
            try:
                reset = datetime.fromisoformat(reset_text.replace("Z", "+00:00")).timestamp()
            except (ValueError, TypeError, AttributeError):
                continue
            now = time.time()
            value = (reset_text, count)
            if (
                type(count) is not int
                or not 0 <= count <= 15
                or not now - 1800 <= snapshot.rewards_at <= now < reset
                or previous.get(period) == value
            ):
                continue
            previous[period] = value
            message = (
                f"{period.capitalize()} win reward target complete."
                if count >= target
                else f"{period.capitalize()} win rewards: {count} of {target}. {target - count} wins remaining."
            )
            messages.append(message)
        if messages:
            run["progress_revision"] = run.get("progress_revision", 0) + 1
            self._announce_grind(run, ("progress", run["progress_revision"]), " ".join(messages))

    def _announce_grind(self, run: dict, key: object, text: str) -> None:
        """Explain navigation once per transition, without slowing or breaking it."""
        if getattr(self, "_auto_quest", None) is not run:
            return
        announced = run.setdefault("announced", set())
        if key in announced:
            return
        announced.add(key)
        self.ui.status("AUTO_QUEST", text)
        logger.info("Grind: %s", text)
        show = getattr(self.ui, "advice", None)
        if callable(show):
            show(text, "GRIND")
        lock = run.setdefault("speech_lock", threading.Lock())
        with lock:
            pending = run.setdefault("speech_pending", deque())
            if key == "off":
                # Discard obsolete navigation after a manual stop, retaining completion.
                completed = [item for item in pending if item[0] in ("complete", "stopped")]
                pending.clear()
                pending.extend(completed)
            pending.append((key, text))
            if run.get("speech_running"):
                return
            run["speech_running"] = True
            worker = threading.Thread(target=self._drain_grind_speech, args=(run,), daemon=True)
            run["speech_worker"] = worker
            worker.start()

    def _drain_grind_speech(self, run: dict) -> None:
        """Serialize speech acknowledgments on a worker, never the game loop."""
        while True:
            with run["speech_lock"]:
                if not run["speech_pending"]:
                    run["speech_running"] = False
                    return
                key, text = run["speech_pending"].popleft()
            terminal = key in ("off", "complete", "stopped")

            def cancelled(terminal=terminal):
                current = getattr(self, "_auto_quest", None)
                return current is not run and not (current is None and terminal)

            if cancelled():
                continue
            try:
                voice = getattr(self, "_voice_output", None)
                if voice is None or voice.muted:
                    continue
                completion = getattr(self.ui, "speech_completion", None)
                emit = getattr(self.ui, "emit_speech_request", None)
                if completion is not None and callable(emit):
                    from arenamcp.speech_completion import narrate_and_wait

                    voice_id, voice_name = voice.current_voice
                    speed = voice.speed
                    request = partial(emit, voice_id=voice_id, voice_name=voice_name, speed=speed)

                    def send(message, speech_id, request=request):
                        request(text=message, speech_id=speech_id)

                    narrate_and_wait(
                        text,
                        send=send,
                        completion=completion,
                        cancelled=cancelled,
                        speed=speed,
                        attempts=1,
                    )
                else:
                    voice.speak(text, blocking=True)
            except Exception:
                logger.debug("Grind narration unavailable", exc_info=True)

    def _quest_session_active(self, run: dict) -> bool:
        driver = getattr(self, "_draft_event_driver", None)
        if driver is not None and driver.enabled and driver.run.event_name and not driver.run.finished:
            return False
        return (
            isinstance(run, dict)
            and getattr(self, "_auto_quest", None) is run
            and getattr(self, "_auto_queue_enabled", False)
            and time.time() < run["deadline"]
            and self._autopilot_control_status() == "AP:ON"
            and not getattr(self, "_autopilot_dry_run", False)
            and not getattr(self, "_autopilot_afk", False)
        )

    def _quest_navigation_allowed(self, run: dict) -> bool:
        """Cheap worker-side session gate, including after slow navigation reads."""
        return self._quest_session_active(run) and time.time() < run.get("valid_until", 0)

    @staticmethod
    def _card_info_batch(ids: list[int]) -> dict[int, dict]:
        from arenamcp.auto_quest import card_colors
        from arenamcp.quest_card_metadata import load_quest_card_metadata
        from arenamcp.server import _get_mtgadb, _log_path

        result = load_quest_card_metadata(ids, _log_path())
        # Existing, observed decks can still be scored if current Standard
        # construction evidence is missing. Raw cards confer no new legality.
        missing = [gid for gid in ids if gid not in result]
        if missing:
            raw = _get_mtgadb()
            for offset in range(0, len(missing), 500):
                for gid, card in raw.get_cards_batch(missing[offset : offset + 500]).items():
                    result[gid] = {
                        "name": card.name,
                        "type_line": card.type_line,
                        "colors": list(card_colors(card.colors) or []),
                        "mana_cost": card.mana_cost,
                        "is_token": card.is_token,
                    }
        return result

    def _quest_actions_allowed(self, run: dict) -> bool:
        """Recheck consent after every potentially blocking bridge or metadata read."""
        if not self._quest_session_active(run):
            return False
        from arenamcp.server import _log_path

        latest = read_snapshot(_log_path())
        fresh = (
            latest.quests == run.get("planning_quests")
            and max(run.get("planning_after", run["started"]), time.time() - 1800)
            <= latest.observed_at
            <= time.time()
        )
        if not fresh:
            return False
        bridge = self._deck_library_bridge()
        if bridge is None:
            return False
        screen = bridge.draft_command("get_screen", timeout=5.0)
        return (
            screen.get("ok") is True
            and screen.get("home") is True
            and not any(
                screen.get(key)
                for key in ("match_end", "draft", "deck_builder", "event_page", "sealed_open", "survey")
            )
            and self._quest_session_active(run)
        )

    def _create_quest_deck(
        self, bridge, decks: dict, collection: dict, cards: dict, descriptions: dict, snapshot
    ) -> tuple[DeckRecord | None, str]:
        from pathlib import Path

        from arenamcp.quest_deck_operation import QuestDeckOperation
        from arenamcp.server import _log_path

        run = self._auto_quest
        active_text = ""
        for raw in snapshot.quests or []:
            progress = raw.get("endingProgress", 0 if raw.get("isNewQuest") else None)
            if type(progress) is int and type(raw.get("goal")) is int and progress < raw["goal"]:
                active_text = descriptions.get(str(raw.get("locKey")) + "_Text", "")
                if active_text:
                    break
        ids = {int(gid) for gid in collection if str(gid).isdigit()}
        ids.update(gid for deck in decks.values() for gid in deck.deck_cards())
        cards.update(self._card_info_batch(sorted(ids)))
        spec = build_quest_deck(active_text, collection, cards)
        if spec is None:
            return None, (
                "Cannot verify 36 owned, castable quest spells and matching basics legal in Standard. "
                "Refresh Arena format data or add owned cards; no crafting will occur."
            )
        path = getattr(
            self, "_quest_deck_journal_path", Path.home() / ".arenamcp" / "quest-deck-operation.json"
        )
        operation = QuestDeckOperation(Path(path))
        color = " and ".join(name for name, symbol in COLORS.items() if symbol in spec.colors)
        self._announce_grind(
            run,
            ("prepare", active_text, color),
            f"Preparing a {color} deck for the quest using cards you own. No wildcards needed.",
        )
        target, detail = operation.step(
            bridge=bridge,
            spec=spec,
            objective=active_text,
            log_path=_log_path(),
            allowed=lambda: self._quest_actions_allowed(run),
            attempt_id=run["started"],
        )
        if target is not None:
            run.setdefault("verified_decks", {})[target.deck_id] = spec
        return target, detail

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
        self, enabled: bool, goal: str = "mastery", minutes: int = 60, deadline: float | None = None
    ) -> None:
        if not enabled:
            run = getattr(self, "_auto_quest", None)
            if run:
                self._announce_grind(run, "off", "Grind stopped. No further matches will be queued.")
            self._auto_quest = None
            self.set_auto_queue(False)
            self.ui.status("GRIND", "OFF")
            self.ui.status("AUTO_QUEST", "Off")
            return
        if goal not in {"mastery", "quests", "daily4", "daily10", "weekly15"}:
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
        self.ui.status("GRIND", "ON")
        self._grind_status("AUTO_QUEST", "Checking daily quests and daily and weekly win rewards.")
        self._check_grind_availability()

    def _check_grind_availability(self) -> bool:
        """Read-only preflight, including before the first match or with Autoplay off."""
        run = getattr(self, "_auto_quest", None)
        if not run:
            return True
        now = time.time()
        if now >= run["deadline"]:
            self._announce_grind(run, "stopped", "Grind session time ended.")
            self.set_auto_quest(False)
            self.ui.status("AUTO_QUEST", "Grind session time ended; no further matches will be queued.")
            return False
        if now < run.get("availability_poll", 0) and now < run["deadline"]:
            return True
        run["availability_poll"] = now + 10
        try:
            from arenamcp.server import _log_path

            snapshot = read_snapshot(_log_path())
            if 0 <= now - snapshot.observed_at <= 1800:
                self._narrate_grind_progress(run, snapshot, quest_descriptions())
            result = plan(
                snapshot,
                now=now,
                deadline=run["deadline"],
                goal=run["goal"],
                decks={},
                collection={},
                cards={},
                descriptions=quest_descriptions(),
            )
            if result.status == "complete":
                self._announce_grind(run, "complete", result.detail)
                self.set_auto_quest(False)
                self.ui.status("AUTO_QUEST", result.detail)
                logger.info("Grind stopped: %s", result.detail)
                return False
            if not run.get("observed"):
                detail = result.detail
                if result.status == "blocked" and detail.startswith("No verified"):
                    detail = "Mastery XP is available. Enable Autoplay and open Arena Home to begin."
                self._grind_status("AUTO_QUEST", detail)
        except Exception as exc:
            self._grind_status("AUTO_QUEST", f"Cannot check available mastery XP: {exc}")
        return True

    def _prepare_auto_quest(self, state: dict) -> bool:
        run = getattr(self, "_auto_quest", None)
        if not run:
            return True
        now = time.time()
        if now >= run["deadline"]:
            self._announce_grind(run, "stopped", "Grind session time ended.")
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
        idle = not run.get("observed") and (not current or current == ended)
        if idle and run.get("idle_started"):
            return time.time() < min(run["deadline"], run.get("valid_until", 0))
        if not idle and (
            not ended or ended != run["observed"] or completed.get("completed_at", 0) < run["started"]
        ):
            return False
        cycle = "idle" if idle else ended
        if run["prepared"] == cycle and now < run.get("valid_until", 0):
            return True
        if now < run["next_poll"]:
            return False
        run["next_poll"] = now + 10
        self._suspend_auto_queue()
        try:
            bridge = self._deck_library_bridge()
            if bridge is None:
                self._grind_status(
                    "AUTO_QUEST", "Paused: connect the bridge to verify owned decks and cards."
                )
                return False
            if idle:
                screen = bridge.draft_command("get_screen", timeout=5.0)
                if (
                    not screen.get("ok")
                    or screen.get("home") is not True
                    or any(
                        screen.get(key)
                        for key in (
                            "match_end",
                            "draft",
                            "deck_builder",
                            "event_page",
                            "sealed_open",
                            "survey",
                        )
                    )
                    or state.get("pending_decision")
                ):
                    self._grind_status(
                        "AUTO_QUEST", "Waiting for verified idle Arena Home before starting Grind."
                    )
                    return False
            elif run["home"] != ended:
                if not bridge.return_to_home():
                    self._grind_status("AUTO_QUEST", "Waiting to leave the completed match.")
                    return False
                run["home"] = ended
                return False
            snapshot = read_snapshot(_log_path())
            run["planning_quests"] = snapshot.quests
            descriptions = quest_descriptions()
            # First check freshness/completion before expensive collection and card reads.
            args = dict(
                now=now,
                deadline=run["deadline"],
                goal=run["goal"],
                # Home usually fetched quests BEFORE the user enabled Grind.
                # The opt-in timestamp is a consent boundary, not a data-age
                # boundary. Still require a refresh after the last known match.
                after=completed.get("completed_at", 0)
                if idle
                else max(run["started"], completed.get("completed_at", 0)),
                descriptions=descriptions,
            )
            run["planning_after"] = args["after"]
            result = plan(snapshot, decks={}, collection={}, cards={}, **args)
            if result.status == "blocked" and result.detail.startswith("No verified"):
                all_decks, _ = self.collect_decks()
                owned = bridge.draft_command("get_collection", timeout=12.0)
                if not owned.get("ok"):
                    self._grind_status(
                        "AUTO_QUEST", "Paused: collection could not be verified. No crafting will occur."
                    )
                    return False
                verified = run.get("verified_decks", {})
                decks = {
                    key: deck for key, deck in all_decks.items() if key in snapshot.pairs or key in verified
                }
                ids = {gid for deck in all_decks.values() for gid in deck.deck_cards()}
                cards = self._card_info_batch(sorted(ids))
                result = plan(
                    snapshot,
                    decks=decks,
                    collection=owned.get("cards") or {},
                    cards=cards,
                    verified_decks=verified,
                    **args,
                )
                if result.status == "blocked" and result.detail.startswith("No verified"):
                    generated, detail = self._create_quest_deck(
                        bridge, all_decks, owned.get("cards") or {}, cards, descriptions, snapshot
                    )
                    if generated is not None:
                        decks[generated.deck_id] = generated
                        result = plan(
                            snapshot,
                            decks=decks,
                            collection=owned.get("cards") or {},
                            cards=cards,
                            verified_decks=run.get("verified_decks", {}),
                            **args,
                        )
                        if result.status == "ready":
                            result.detail = detail + " " + result.detail
                    elif detail:
                        result.detail = detail + " No cards will be crafted."
            detail = result.detail
            if result.coverage_gaps:
                detail += " Deck coverage gaps: " + "; ".join(result.coverage_gaps)
            self.ui.status("AUTO_QUEST", detail)
            if result.status == "complete":
                self._announce_grind(run, "complete", result.detail)
                self._auto_quest = None
                self.ui.status("GRIND", "OFF")
                self.set_auto_queue(False)
                return False
            if result.status != "ready":
                self._grind_status("AUTO_QUEST", detail)
                return False
            if not self._quest_actions_allowed(run):
                return False
            if (
                result.deck_id in run.get("verified_decks", {})
                and snapshot.pairs.get(result.deck_id) != result.event_id
                and getattr(bridge, "client_runtime", None) not in {"bepinex", "il2cpp-macos"}
            ):
                self._grind_status(
                    "AUTO_QUEST",
                    f"Saved and verified {result.deck_name}. This bridge cannot queue a new constructed "
                    "deck by ID; native navigation requires its exact Recently Played tile. "
                    "Select this deck in Standard Play once to continue Grind.",
                )
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
                self._announce_grind(
                    run, "stopped", "Stopped: three matches without observed quest or reward progress."
                )
                self.set_auto_quest(False)
                self.ui.status("AUTO_QUEST", "Stopped: three matches without observed quest/reward progress.")
                return False
            run["target"] = {
                "deck_name": result.deck_name,
                "deck_id": result.deck_id,
                "event_id": result.event_id,
                "quest_mode": True,
            }
            run["prepared"] = cycle
            if idle:
                run["idle_ready"] = True
            run["valid_until"] = snapshot.observed_at + 1800
            self._narrate_grind_progress(run, snapshot, descriptions)
            self._announce_grind(
                run,
                ("queue", cycle, result.deck_id),
                f"Using {result.deck_name}. Entering the free {result.event_id} queue to make progress.",
            )
            return time.time() < run["deadline"]
        except Exception as exc:
            self._grind_status("AUTO_QUEST", f"Paused: quest state could not be verified: {exc}")
            return False
