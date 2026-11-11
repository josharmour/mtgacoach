"""Automatic reviews of three consecutive losses, using captured match logs."""

import logging
import threading

from arenamcp.logging_config import LOG_DIR
from arenamcp.loss_streak import LossStreakStore

logger = logging.getLogger(__name__)


class LossStreakMixin:
    def _loss_store(self):
        store = getattr(self, "_loss_streak_store", None)
        if store is None:
            store = self._loss_streak_store = LossStreakStore(LOG_DIR / "loss_streak_reviews.json")
        return store

    def _observe_loss_review(self, state, gs):
        """Called only for live watched matches, plus completion polling."""
        store = self._loss_store()
        mid = state.get("match_id")
        if mid and mid in self._watched_games() and not state.get("game_over"):
            from arenamcp.deck_strategy import deck_identity

            eligible = (
                bool(getattr(self, "_auto_queue_enabled", False))
                and self._autopilot_control_status() == "AP:ON"
                and not getattr(self, "_autopilot_dry_run", False)
                and not getattr(self, "_autopilot_afk", False)
            )
            # Avoid disk writes on every poll when nothing changed.
            deck = deck_identity(state)
            existing = store.data["matches"].get(mid)
            # Sideboarding changes the submitted pile within the same BO3,
            # not the deck that began this match. Compare opening lists across matches.
            if (state.get("game_number") or 1) > 1 and existing and existing["deck"]:
                deck = existing["deck"]
            signature = (mid, deck, eligible)
            if signature != getattr(self, "_loss_observation", None):
                store.observe(*signature)
                self._loss_observation = signature
        completed = getattr(gs, "_loss_review_final_result", None)
        if completed:
            store.finish(*completed)
        if not getattr(self, "_loss_review_running", False) and store.pending():
            self._loss_review_running = True
            threading.Thread(target=self._run_loss_reviews, daemon=True, name="loss-streak-review").start()

    def _capture_loss_evidence(self, match_id, state, history, missed):
        # Capture bounded, redacted advice/action logs for this game only.
        # No raw Player.log or screenshots are sent or attached automatically.
        state = state or {}
        mid = state.get("match_id")
        if not mid:
            return
        entries = []
        for item in history[-150:]:
            snap = item.get("game_snapshot") or {}
            entries.append(
                {
                    k: snap[k]
                    for k in ("turn_number", "hand_count", "battlefield_count")
                    if isinstance(snap.get(k), (int, float))
                }
            )
        from arenamcp.match_bundle import redact

        names = {}
        for player in state.get("players", []):
            for field in ("name", "player_name", "screen_name", "user_id"):
                if isinstance(player.get(field), str):
                    names[player[field]] = "[player]"
        logs = [
            {
                "advice": str(item.get("advice") or "")[:2000],
                "game_context": str(item.get("game_context") or "")[:2000],
                "game_snapshot": entries[index],
            }
            for index, item in enumerate(history[-150:])
        ]
        from arenamcp.match_packets import get_current_packet

        packet = get_current_packet()
        decisions = []
        if packet is not None and packet.match_id == match_id:
            decisions = redact(
                [
                    {
                        key: entry[key]
                        for key in (
                            "executed_action",
                            "pending_decision",
                            "chosen_options",
                            "outcome",
                            "explanation",
                            "turn",
                        )
                        if key in entry
                    }
                    for entry in packet.decisions[-150:]
                ],
                names,
            )
        evidence = {
            "decision_log": decisions,
            "gameplay_snapshots": entries,
            "advice_log": redact(logs, names),
            "actions": redact(list(state.get("action_history") or [])[-150:], names),
            "missed_decision_count": len(missed),
            "deck_cards": [x for x in state.get("deck_cards", []) if isinstance(x, int)],
            "turn_number": state.get("turn_number") if isinstance(state.get("turn_number"), int) else None,
        }
        self._loss_store().evidence(mid, {"game_id": match_id, **evidence})

    @staticmethod
    def _loss_model_history(evidence):
        """Bound model input and preserve each game's attribution and order."""
        import json

        rows = []
        for game in evidence:
            rows.append(
                {
                    "advice": "GAME BOUNDARY "
                    + str(game.get("game_id"))
                    + "; recorded action sample: "
                    + json.dumps(game.get("actions", [])[-12:])[:2000]
                    + "; deck card IDs (not names): "
                    + json.dumps(game.get("deck_cards", []))[:1500]
                }
            )
            decisions = game.get("decision_log", [])
            if decisions:
                indices = sorted({round(i * (len(decisions) - 1) / 15) for i in range(16)})
                for index in indices:
                    rows.append(
                        {"advice": "Recorded decision/action: " + json.dumps(decisions[index])[:1200]}
                    )
                rows.append(
                    {
                        "advice": f"Sampled {len(indices)} of {len(decisions)} recorded decisions/actions; pending is not proof of execution."
                    }
                )
            logs = game.get("advice_log", [])
            indices = sorted({round(i * (len(logs) - 1) / 11) for i in range(12)}) if logs else []
            for index in indices:
                row = logs[index]
                rows.append(
                    {
                        "advice": row.get("advice", "")[:600],
                        "game_context": row.get("game_context", "")[:600],
                        "game_snapshot": row.get("game_snapshot", {}),
                    }
                )
            rows.append(
                {
                    "advice": f"END GAME. Sampled {len(indices)} of {len(logs)} captured advice entries; omitted evidence must not be inferred."
                }
            )
        return rows

    def _run_loss_reviews(self):
        try:
            for key, job in self._loss_store().pending():
                store = self._loss_store()
                records = [store.data["matches"][mid] for mid in job["matches"]]
                evidence = [game for r in records for game in r["evidence"].values()]
                gaps = sum(e.get("missed_decision_count", 0) for e in evidence)
                missing = any(not r["evidence"] for r in records) or any(
                    not (e.get("gameplay_snapshots") or e.get("decision_log") or e.get("actions"))
                    for e in evidence
                )
                import platform

                from arenamcp import __version__

                # Evidence-led fallback is always available, including offline.
                if gaps or missing:
                    recommendation = (
                        "Review a support report: gameplay logs are incomplete or contain missed decisions. "
                        "This does not prove a bug or that automation caused the losses."
                    )
                else:
                    recommendation = (
                        "Consider a deck modification after reviewing the three matches: compare early hand "
                        "and battlefield development before changing the mana curve or interaction. "
                        "Three losses alone do not establish a deck defect or justify crafting cards."
                    )
                report = {
                    "schema": 1,
                    "coach_version": __version__,
                    "platform": platform.system(),
                    "support_recipient": "support@mtgacoach.com",
                    "matches": job["matches"],
                    "evidence": evidence,
                    "recommendation": recommendation,
                    "analysis": "",
                    "limitations": "Captured advice and gameplay logs; advice is not proof of execution. Execution causality is not established.",
                }
                coach = getattr(self, "_coach", None)
                if coach and not missing:
                    try:
                        report["analysis"] = (
                            coach.generate_post_match_analysis(
                                advice_history=self._loss_model_history(evidence),
                                match_result="loss",
                                match_duration_turns=0,
                                deck_strategy=(
                                    "Review THREE separate consecutive lost matches with the SAME deck. "
                                    "Use only supplied evidence. Choose a supported deck modification, alternative deck "
                                    "archetype, or recommend a support report. State missing evidence and uncertainty. "
                                    "Do not invent card names, logs, causes, or claim a bug from losses alone. "
                                    "Never claim a report was sent. Do not attribute autoplay decisions to the player."
                                ),
                                narration_mode="autopilot",
                                priority=10,
                            )
                            or ""
                        )
                    except Exception:
                        logger.warning(
                            "Loss-streak model review unavailable; using evidence summary", exc_info=True
                        )
                # Save before presentation: a fast shutdown never loses the report.
                if store.complete(key, report):
                    text = (
                        "Three consecutive autoplay/auto-queue losses with the same deck. " + recommendation
                    )
                    if report["analysis"]:
                        text += "\n" + report["analysis"]
                    text += (
                        f"\nReview saved locally: {store.path}. To contact support@mtgacoach.com, "
                        "review this report and use your email app to send it. Nothing has been sent. "
                        "Include Coach version, platform and reproduction steps; attach raw logs only after reviewing them."
                    )
                    self.ui.advice(text, "SESSION REVIEW")
        except Exception:
            logger.exception("Could not complete loss-streak review; pending review retained")
        finally:
            self._loss_review_running = False
