"""Post-match analysis helpers for StandaloneCoach, extracted from standalone.py.

Pure move: methods are unchanged and mixed back into StandaloneCoach."""

import importlib
import json
import logging
import subprocess
import threading
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path

from arenamcp.logging_config import LOG_DIR, LOG_FILE

logger = logging.getLogger(__name__)


class _PostMatchMixin:
    def _stage_post_match_analysis(
        self,
        *,
        match_result: str | None,
        final_state: dict | None,
        replay_path: str | None,
        match_id: str | None = None,
        advice_history: list[dict] | None = None,
        missed_decisions: list[dict] | None = None,
        reason: str,
        announce_ready: bool = True,
        auto_start: bool | None = None,
    ) -> bool:
        """Persist completed-match data so analysis can be run later."""
        # End-of-match telemetry: sample up to 5 bridge-fallback bugs
        # collected during the match and file them on GitHub.
        try:
            if self._autopilot is not None and hasattr(self._autopilot, "flush_fallback_bugs_for_match"):
                n = self._autopilot.flush_fallback_bugs_for_match()
                if n:
                    logger.info(f"Dispatched {n} sampled fallback bug(s) from match")
        except Exception as e:
            logger.debug(f"flush_fallback_bugs_for_match failed: {e}")

        mid = str(match_id) if match_id is not None else f"match_{self._match_number}"

        # Always record W/L + metadata at game end (even when there's no
        # advice history to analyze) so the Performance tab reflects the
        # last few matches. A numeric advice-quality score is computed
        # automatically and backfilled in a background thread.
        try:
            self._write_performance_record(mid, match_result, final_state, replay_path)
        except Exception as e:
            logger.debug(f"performance record write failed: {e}")

        staged_history = list(self._advice_history if advice_history is None else advice_history)
        if not staged_history:
            logger.info(f"Skipping post-match staging ({reason}): no advice history")
            return False
        staged = {
            "advice_history": staged_history,
            "missed_decisions": list(
                self._missed_decisions if missed_decisions is None else missed_decisions
            ),
            "result": match_result or "unknown",
            "final_state": deepcopy(final_state) if isinstance(final_state, dict) else final_state,
            "replay_path": replay_path,
        }
        self._staged_analyses[mid] = staged

        # Automatic advice-quality score: independent of the larger opt-in
        # prose analysis (auto_post_match_analysis defaults to OFF). One small
        # LLM call per match, in a background thread so the loop isn't blocked.
        try:
            self._start_auto_advice_score(mid, match_result, staged_history)
        except Exception as e:
            logger.debug(f"auto advice score start failed: {e}")

        # Cap at last 3; evict oldest with log
        while len(self._staged_analyses) > 3:
            oldest_key = next(iter(self._staged_analyses))
            logger.info("Evicting oldest staged analysis (%s) — cap of 3 reached", oldest_key)
            del self._staged_analyses[oldest_key]

        logger.info(
            "Staged post-match analysis (%s): entries=%s result=%s replay=%s auto=%s",
            reason,
            len(staged["advice_history"]),
            staged["result"],
            replay_path or "none",
            self._auto_post_match_analysis if auto_start is None else auto_start,
        )

        should_auto_start = self._auto_post_match_analysis if auto_start is None else auto_start
        if should_auto_start:
            return self._start_post_match_analysis_worker(reason=f"{reason}/auto")

        if announce_ready:
            self.ui.status("ANALYSIS", "Ready")
            self.ui.log("[cyan]Match analysis ready. Use Analyze Match or /analyze to run it.[/]")
        return True

    def _start_post_match_analysis_worker(self, reason: str) -> bool:
        """Launch the post-match worker if saved data is available."""
        if self._post_match_analysis_running:
            logger.info(f"Post-match analysis already running; skipping launch ({reason})")
            return False

        # Find a staged analysis to run (prefer most-recently-added)
        mid = None
        staged = None
        if self._staged_analyses:
            mid = next(reversed(self._staged_analyses))
            staged = self._staged_analyses[mid]

        if not staged or not staged.get("advice_history"):
            logger.info(f"No saved post-match data; skipping launch ({reason})")
            return False

        self._post_match_analysis_running = True
        thread = threading.Thread(
            target=self._post_match_analysis_worker,
            daemon=True,
            name="post-match-analysis",
            args=(mid,),
        )
        thread.start()
        logger.info(f"Started post-match analysis worker ({reason})")
        return True

    def _detect_match_result(self) -> str:
        """Detect win/loss from game state.

        Checks multiple sources in priority order:
        1. Persistent ``last_game_result`` field on GameState (survives reset)
        2. Pre-reset snapshot's ``last_game_result``
        3. ``recent_events`` (may be cleared by reset)

        Returns "win", "loss", "draw", or "unknown".
        """
        try:
            from arenamcp.server import game_state as gs

            # Preferred: persistent field set when game_end annotation fires
            if gs.last_game_result:
                return gs.last_game_result

            # Fallback 1: pre-reset snapshot
            if gs._pre_reset_snapshot:
                snap_result = gs._pre_reset_snapshot.get("last_game_result")
                if snap_result:
                    return snap_result

            # Fallback 2: scan recent_events from snapshot (live ones may be cleared)
            snapshot = gs._pre_reset_snapshot or {}
            recent = snapshot.get("recent_events", [])
            if not recent:
                # Try live recent_events
                game_state_dict = self._mcp.get_game_state()
                recent = game_state_dict.get("recent_events", [])
            for event in reversed(recent):
                if event.get("type") == "game_end":
                    return event.get("result", "unknown")
        except Exception as e:
            logger.debug(f"Could not detect match result: {e}")
        return "unknown"

    def _has_explicit_game_end_evidence(self) -> bool:
        """Return True only when GameState has explicit match-end evidence."""
        try:
            server_mod = importlib.import_module("arenamcp.server")
            gs = getattr(server_mod, "game_state", None)
            if gs is None:
                return False
            if gs.game_ended_event.is_set():
                return True
            if getattr(gs, "last_game_result", None):
                return True
            if getattr(gs, "_pre_reset_snapshot", None):
                return True
        except Exception as e:
            logger.debug(f"Could not inspect game-end evidence: {e}")
        return False

    def _extract_replay_context(self, replay_path: str | None) -> str | None:
        """Parse a replay file and extract decision-point context for analysis.

        Returns a compact text summary of key decision points from the replay:
        - Actions the player submitted (OUT messages)
        - Decision requests the GRE sent (ActionsAvailable, SelectTargets, etc.)
        - Turn/phase context for each decision

        Returns None if replay is unavailable or unparseable.
        """
        if not replay_path:
            return None
        try:
            from pathlib import Path

            from arenamcp.arena_replay import ArenaReplay

            rp = Path(replay_path)
            if not rp.exists():
                logger.debug(f"Replay file not found: {replay_path}")
                return None

            replay = ArenaReplay(rp)
            if not replay.load():
                logger.warning(f"Failed to load replay: {replay_path}")
                return None

            lines = [f"REPLAY FILE: {rp.name} ({replay.get_message_count()} messages)"]

            # Extract player submissions (OUT messages) and GRE decision requests
            turn = 0
            phase = ""
            decision_count = 0
            action_count = 0
            max_entries = 80  # Cap to avoid prompt bloat

            for msg in replay.iter_messages():
                if decision_count + action_count >= max_entries:
                    lines.append(f"[...truncated at {max_entries} entries]")
                    break

                # Track turn/phase from game state messages
                gs_msg = msg.get("gameStateMessage", {})
                turn_info = gs_msg.get("turnInfo", {})
                if turn_info:
                    new_turn = turn_info.get("turnNumber", turn)
                    new_phase = turn_info.get("phase", phase)
                    if new_turn != turn or new_phase != phase:
                        turn = new_turn
                        phase = new_phase

                # GRE decision requests (server → client)
                direction = msg.get("_direction", "")
                msg_type = msg.get("type", "")

                if "ActionsAvailableReq" in msg_type:
                    actions_req = msg.get("actionsAvailableReq", {})
                    action_list = actions_req.get("actions", [])
                    cast_names = []
                    for a in action_list:
                        if isinstance(a, dict):
                            at = a.get("actionType", "")
                            grp = a.get("grpId", 0)
                            if "Cast" in str(at) and grp:
                                try:
                                    from arenamcp import server

                                    info = server.get_card_info(grp)
                                    cast_names.append(info.get("name", f"#{grp}"))
                                except Exception:
                                    cast_names.append(f"#{grp}")
                    summary = (
                        f"castable: {', '.join(cast_names)}" if cast_names else f"{len(action_list)} actions"
                    )
                    lines.append(f"  T{turn} {phase}: GRE ActionsAvailable ({summary})")
                    decision_count += 1

                elif "SelectTargetsReq" in msg_type or "SelectNReq" in msg_type:
                    lines.append(f"  T{turn} {phase}: GRE {msg_type.replace('GREMessageType_', '')}")
                    decision_count += 1

                # Player submissions (client → server)
                elif direction == "out":
                    client_msg = msg.get("clientToGREMessage", msg)
                    resp_type = client_msg.get("type", "")
                    if "PerformActionResp" in resp_type or "ActionResp" in resp_type:
                        lines.append(f"  T{turn} {phase}: PLAYER submitted action")
                        action_count += 1
                    elif "PassResp" in resp_type or "PassPriorityResp" in resp_type:
                        pass  # Too noisy
                    elif resp_type and "Resp" in resp_type:
                        lines.append(
                            f"  T{turn} {phase}: PLAYER {resp_type.replace('ClientMessageType_', '')}"
                        )
                        action_count += 1

            lines.append(f"Total: {decision_count} decision points, {action_count} player actions")
            context = "\n".join(lines)
            logger.info(f"Extracted replay context: {len(context)} chars from {rp.name}")
            return context

        except Exception as e:
            logger.warning(f"Failed to extract replay context: {e}", exc_info=True)
            return None

    def _post_match_analysis_worker(self, match_id: str) -> None:
        """Background worker: generate post-match strategic analysis.

        Spawned when a match ends. Uses a dedicated backend to avoid
        lock contention with real-time coaching.
        """
        # Pop our match_id from staged so no other worker can claim it.
        # This isolates each match's analysis from BO3 clobbering.
        if match_id not in self._staged_analyses:
            logger.info(f"No staged analysis for match_id={match_id}")
            self._post_match_analysis_running = False
            return
        staged = self._staged_analyses.pop(match_id)

        try:
            advice_history = staged["advice_history"]
            match_result = staged["result"]
            final_state = staged["final_state"]
            replay_path = staged["replay_path"]
            missed_decisions = staged["missed_decisions"]

            if not advice_history:
                logger.info("No advice history for post-match analysis")
                return

            if not self._coach:
                logger.warning("No coach engine for post-match analysis")
                return

            logger.info(
                f"Post-match analysis started: {len(advice_history)} entries, "
                f"result={match_result}, replay={replay_path or 'none'}"
            )
            self.ui.status("ANALYSIS", "Generating post-match analysis...")

            # Structured review runs FIRST and independently of the LLM —
            # deterministic detectors over the match's own artifacts, so the
            # analyze button always yields verifiable, actionable findings
            # even if the prose analysis times out or hallucinates.
            try:
                self._run_match_review(match_id, match_result, advice_history)
            except Exception as e:
                logger.warning(f"match-review failed: {e}")

            # Parse replay file for decision-point context
            replay_context = self._extract_replay_context(replay_path)

            # Compute match duration from advice snapshots
            turns = [
                e.get("game_snapshot", {}).get("turn_number", 0)
                for e in advice_history
                if e.get("game_snapshot")
            ]
            match_duration = max(turns) if turns else 0

            # Extract final life totals
            final_life = {}
            if final_state:
                for p in final_state.get("players", []):
                    final_life[p.get("seat_id")] = p.get("life_total")

            # Extract opponent played cards (names from battlefield/graveyard)
            opponent_cards = []
            if final_state:
                opp_cards = final_state.get("opponent_played_cards", [])
                for card in opp_cards:
                    name = card.get("name") if isinstance(card, dict) else str(card)
                    if name:
                        opponent_cards.append(name)

            # Get deck strategy
            deck_strategy = getattr(self._coach, "_deck_strategy", "") or ""

            # Reuse the existing coaching backend — the match is over so
            # there's no lock contention risk, and the warm connection
            # avoids cold-start timeouts on the proxy server.
            analysis = self._coach.generate_post_match_analysis(
                advice_history=advice_history,
                match_result=match_result,
                match_duration_turns=match_duration,
                deck_strategy=deck_strategy,
                final_life_totals=final_life,
                opponent_played_cards=opponent_cards,
                missed_decisions=missed_decisions,
                replay_context=replay_context,
            )

            if not analysis:
                logger.warning("Post-match analysis returned empty")
                self.ui.log(
                    "[yellow]Post-match analysis failed (timeout or empty response). "
                    "Try 'Analyze Match' button to retry.[/]"
                )
                self.ui.status("ANALYSIS", "")
                # Don't clear saved data so manual retry can use it
                return

            # Split off the SPOKEN: summary for TTS
            spoken_summary = ""
            display_analysis = analysis
            if "SPOKEN:" in analysis:
                parts = analysis.rsplit("SPOKEN:", 1)
                display_analysis = parts[0].strip()
                spoken_summary = parts[1].strip()

            # Display in UI
            result_label = (
                "VICTORY"
                if match_result == "win"
                else "DEFEAT"
                if match_result == "loss"
                else "DRAW"
                if match_result == "draw"
                else "MATCH ENDED"
            )
            self.ui.log("")
            self.ui.log(f"[bold cyan]{'═' * 50}[/]")
            self.ui.log(f"[bold green]  POST-MATCH ANALYSIS — {result_label}[/]")
            self.ui.log(f"[bold cyan]{'═' * 50}[/]")
            self.ui.log(display_analysis)
            self.ui.log(f"[bold cyan]{'═' * 50}[/]")
            self.ui.log("")

            self.ui.status("ANALYSIS", f"Match analysis complete ({match_result})")

            # Speak the short summary via TTS
            if spoken_summary:
                self.speak_advice(spoken_summary, blocking=False)

            logger.info(f"Post-match analysis complete: {len(analysis)} chars")

            # Store analysis for inclusion in /bugreport
            # Bug A: deep-copy live game state at completion time so F7
            # bug reports don't mix games in a BO3.
            try:
                self._post_match_snapshot = deepcopy(self._mcp.get_game_state())
            except Exception:
                self._post_match_snapshot = None
            self._post_match_analysis_text = display_analysis
            self._post_match_result = match_result

            # Best-effort 1-10 coach rating for the performance history tab.
            try:
                self._record_performance_rating(match_id, match_result, analysis)
            except Exception as e:
                logger.debug(f"performance rating failed: {e}")
            emit_feedback_request = getattr(self.ui, "emit_post_match_feedback_request", None)
            if callable(emit_feedback_request):
                emit_feedback_request(display_analysis, match_result)
            else:
                self.ui.log("[bold yellow]Type /bugreport to file coaching improvements to GitHub[/]")

            # Offer to file GH issue for missed decisions detected by vision watchdog
            self._offer_missed_decision_issue(missed_decisions=missed_decisions)

        except Exception as e:
            logger.error(f"Post-match analysis error: {e}", exc_info=True)
            self.ui.log(f"[red]Post-match analysis failed: {e}[/]")
            self.ui.status("ANALYSIS", "")
        finally:
            self._post_match_analysis_running = False

    def _run_match_review(
        self,
        match_id: str,
        match_result: str,
        advice_history: list[dict],
    ) -> None:
        """Deterministic post-match review: detect real, evidenced problems
        and file ONE consolidated GitHub issue when warranted.

        This is what makes the analyze button improve the coach: unlike the
        prose analysis (which has invented events), every finding here is
        mechanically derived from the log slice, advice history, and match
        packet, and carries its evidence lines.
        """
        from arenamcp import __version__
        from arenamcp.match_review import (
            build_issue,
            read_log_slice,
            run_match_review,
            save_review,
            should_file_issue,
        )

        # Log slice from just before the first advice entry.
        since = None
        for e in advice_history or []:
            try:
                since = datetime.fromisoformat(e["timestamp"])
                break
            except (KeyError, ValueError, TypeError):
                continue
        log_slice = ""
        if since is not None:
            log_slice = read_log_slice(LOG_FILE, since - timedelta(seconds=90))

        # Match packet (decisions + FSM outcomes), if one was recorded.
        packet = None
        try:
            packets_dir = Path.home() / ".arenamcp" / "match_packets"
            candidates = sorted(
                packets_dir.glob(f"*{match_id}*.json"),
                key=lambda p: p.stat().st_mtime,
            )
            if candidates:
                with open(candidates[-1], encoding="utf-8") as f:
                    packet = json.load(f)
        except Exception as e:
            logger.debug(f"match-review packet load failed: {e}")

        findings = run_match_review(
            advice_history=advice_history,
            match_result=match_result,
            log_slice=log_slice,
            packet=packet,
        )
        review_path = save_review(match_id, match_result, findings)
        if not findings:
            logger.info("match-review: no findings")
            return

        sev = {"high": 0, "medium": 0, "low": 0}
        for f in findings:
            sev[f.severity] = sev.get(f.severity, 0) + 1
        logger.info(
            f"match-review: {len(findings)} findings "
            f"(high={sev['high']} med={sev['medium']} low={sev['low']}) "
            f"saved={review_path}"
        )
        self.ui.log(
            f"[bold yellow]Match review: {len(findings)} verified finding(s) "
            f"({sev['high']} high, {sev['medium']} medium, {sev['low']} low)[/]"
        )
        for f in findings:
            self.ui.log(f"  [{f.severity}] {f.category}: {f.title}")

        if not should_file_issue(findings):
            self.ui.log("[yellow]Low-severity only — saved locally, no issue filed.[/]")
            return

        title, body = build_issue(match_id, match_result, findings, version=__version__)
        token = self._get_github_token_for_auto_bug()
        if not token:
            self.ui.log("[yellow]No GitHub token — review saved locally only.[/]")
            return
        try:
            import requests

            from arenamcp.bugreport import GITHUB_REPO

            resp = requests.post(
                f"https://api.github.com/repos/{GITHUB_REPO}/issues",
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {token}",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                json={"title": title, "body": body, "labels": ["match-review"]},
                timeout=12,
            )
            if resp.ok:
                url = str(resp.json().get("html_url", ""))
                self.ui.log(f"[match-review] Filed: {url}")
            else:
                logger.debug(f"match-review GitHub API failed: {resp.status_code}")
        except Exception as e:
            logger.debug(f"match-review issue filing failed: {e}")

    def _offer_missed_decision_issue(self, missed_decisions: list[dict] | None = None) -> None:
        """Offer to file a GH issue if vision detected missed decisions this match.

        Accepts missed_decisions directly to avoid coupling to instance state.
        Falls back to getattr for callers that still use the old singleton pattern.
        """
        missed = (
            missed_decisions if missed_decisions is not None else getattr(self, "_saved_missed_decisions", [])
        )
        if not missed:
            return

        count = len(missed)
        self.ui.log("")
        self.ui.log(f"[bold yellow]Vision watchdog detected {count} missed decision point(s) this match.[/]")
        self.ui.log("[yellow]Filing GitHub issue with details...[/]")

        try:
            self._file_missed_decision_gh_issue(missed)
        except Exception as e:
            logger.error(f"Failed to file GH issue for missed decisions: {e}")
            self.ui.log(f"[red]Failed to file GH issue: {e}[/]")
            # Fall back to saving a local bug report
            self._save_missed_decisions_local(missed)

    def _file_missed_decision_gh_issue(self, missed: list[dict]) -> None:
        """File a GitHub issue with missed decision details."""
        # Build issue body
        lines = [
            "## Missed Decision Points Detected by Vision Watchdog",
            "",
            f"**Match date:** {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            f"**Decisions detected:** {len(missed)}",
            "",
            "The vision watchdog detected game states where Arena was waiting for player input,",
            "but no predefined trigger fired. These represent gaps in the trigger/GRE message mapping.",
            "",
            "### Details",
            "",
        ]

        for i, d in enumerate(missed, 1):
            local_seat = d.get("local_seat", "?")
            active = d.get("active_player", "?")
            priority = d.get("priority_player", "?")
            is_ours = d.get("is_our_turn")
            turn_owner = "OURS" if is_ours else ("OPPONENT" if is_ours is False else "?")
            last_gre = d.get("last_gre_decision", "none")
            screenshot = d.get("screenshot", "")

            lines.append(f"#### Decision {i}")
            lines.append(f"- **Timestamp:** {d.get('timestamp', 'N/A')}")
            lines.append(f"- **Turn:** {d.get('turn', 'N/A')}")
            lines.append(f"- **Phase:** {d.get('phase', 'N/A')}")
            lines.append(f"- **Step:** {d.get('step', '')}")
            lines.append(f"- **Type:** {d.get('decision_type', 'N/A')}")
            lines.append(f"- **Prompt:** {d.get('prompt_text', 'N/A')}")
            lines.append(f"- **Confidence:** {d.get('confidence', 'N/A')}")
            lines.append(f"- **Stall duration:** {d.get('stall_duration_s', 'N/A')}s")
            lines.append(f"- **Avg tempo:** {d.get('avg_tempo_s', 'N/A')}s")
            lines.append(f"- **Num options:** {d.get('num_options', 'N/A')}")
            lines.append("- **Validation context:**")
            lines.append(f"  - Local seat: {local_seat} | Active player: {active} | Priority: {priority}")
            lines.append(f"  - Turn owner: **{turn_owner}** | We have priority: {priority == local_seat}")
            lines.append(f"  - Last cleared GRE decision: `{last_gre}`")
            if screenshot:
                lines.append(f"  - Screenshot: `~/.arenamcp/watchdog_screenshots/{screenshot}`")
            log_ctx = d.get("log_context", [])
            if log_ctx:
                # Show last 5 in GH issue (full 10 in local JSON sidecar)
                trimmed = log_ctx[-5:]
                lines.append(f"- **Game log ({len(trimmed)} of {len(log_ctx)} states before detection):**")
                lines.append("```")
                for log_line in trimmed:
                    lines.append(log_line)
                lines.append("```")
            lines.append("")

        lines.append("### Suggested Action")
        lines.append("")
        lines.append("Review each detection's validation context. Detections where turn owner is OPPONENT")
        lines.append("or where a GRE decision was recently cleared are likely **false positives**.")
        lines.append("True positives (our turn, our priority, no recent GRE decision) indicate gaps")
        lines.append("in the trigger/GRE message mapping.")
        lines.append("")
        lines.append("---")
        lines.append("*Auto-filed by mtgacoach vision watchdog*")

        body = "\n".join(lines)

        # Determine unique decision types for the title
        types = sorted(set(d.get("decision_type", "unknown") for d in missed))
        types_str = ", ".join(types[:3])
        if len(types) > 3:
            types_str += f" (+{len(types) - 3} more)"
        title = f"Missed decision points: {types_str}"
        if len(title) > 70:
            title = title[:67] + "..."

        result = subprocess.run(
            [
                "gh",
                "issue",
                "create",
                "--repo",
                "josharmour/mtgacoach",
                "--title",
                title,
                "--label",
                "bug,vision-watchdog",
                "--body",
                body,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode == 0:
            issue_url = result.stdout.strip()
            self.ui.log(f"[bold green]GitHub issue filed: {issue_url}[/]")
            logger.info(f"Filed GH issue for {len(missed)} missed decisions: {issue_url}")
        else:
            # Label might not exist yet — retry without labels
            if "label" in result.stderr.lower():
                result2 = subprocess.run(
                    [
                        "gh",
                        "issue",
                        "create",
                        "--repo",
                        "josharmour/mtgacoach",
                        "--title",
                        title,
                        "--body",
                        body,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if result2.returncode == 0:
                    issue_url = result2.stdout.strip()
                    self.ui.log(f"[bold green]GitHub issue filed: {issue_url}[/]")
                    logger.info(f"Filed GH issue for {len(missed)} missed decisions: {issue_url}")
                    return
            raise RuntimeError(f"gh issue create failed: {result.stderr.strip()}")

    def _save_missed_decisions_local(self, missed: list[dict]) -> None:
        """Save missed decisions to a local file as fallback when GH filing fails."""
        bug_dir = LOG_DIR / "bug_reports"
        bug_dir.mkdir(exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = bug_dir / f"missed_decisions_{timestamp}.json"
        with open(path, "w") as f:
            json.dump({"missed_decisions": missed, "timestamp": timestamp}, f, indent=2)
        self.ui.log(f"[yellow]Saved missed decisions locally: {path}[/]")
        logger.info(f"Saved {len(missed)} missed decisions to {path}")

    def trigger_match_analysis(self) -> None:
        """Manually trigger post-match analysis (from Analyze Match button).

        Prefer the most recently completed match if one was staged already.
        """
        if self._post_match_analysis_running:
            self.ui.log("[yellow]Analysis already in progress...[/]")
            return

        _staged_count = len(self._staged_analyses)
        has_saved = _staged_count > 0
        has_current = bool(self._advice_history)

        if has_saved:
            self.ui.log("[cyan]Analyzing the most recently completed match...[/]")
        elif not has_current:
            self.ui.log("[yellow]No advice history to analyze.[/]")
            return
        else:
            self.ui.log("[cyan]Analyzing the current match snapshot...[/]")
            try:
                game_state = self._mcp.get_game_state() if self._mcp else None
            except Exception as e:
                logger.debug(f"Could not capture final game state for post-match: {e}")
                game_state = None

            self._stage_post_match_analysis(
                match_result=self._detect_match_result(),
                final_state=game_state,
                replay_path=self._get_latest_replay_path(),
                reason="manual-current",
                announce_ready=False,
                auto_start=False,
            )

        self._start_post_match_analysis_worker(reason="manual")

    def _write_performance_record(self, match_id, result, final_state, replay_path) -> None:
        """Persist a match into performance history (W/L + metadata)."""
        from arenamcp.match_history import MatchHistory, parse_replay_cosmetics, record_from_game_end

        opponent_cards = []
        if isinstance(final_state, dict):
            opponent_cards = final_state.get("opponent_played_cards", []) or []

        rec = record_from_game_end(
            match_id=match_id or "",
            result=str(result) if result else "unknown",
            game_state_snapshot=final_state or {},
            opponent_cards=opponent_cards,
            replay_path=replay_path or "",
        )
        # Format is not yet captured by the game-state parser; pass it through
        # when the snapshot exposes it (best-effort — commonly empty).
        if isinstance(final_state, dict):
            for key in ("format_name", "super_format", "match_format"):
                if final_state.get(key):
                    rec.format_name = str(final_state[key])
                    break
        if replay_path:
            try:
                cos = parse_replay_cosmetics(replay_path)
                if cos and isinstance(cos.get("Opponent"), dict):
                    rec.opponent_name = cos["Opponent"].get("ScreenName") or ""
            except Exception:
                pass
        MatchHistory().add_record(rec)

    def _record_performance_rating(self, match_id, match_result, analysis) -> None:
        """Best-effort 1-10 rating; degrades silently to W/L-only."""
        rating = self._coach_rating_from_analysis(analysis)
        if rating is None and analysis and self._coach is not None:
            rating = self._rate_match_with_llm(match_result, analysis)
        if rating is not None:
            from arenamcp.match_history import MatchHistory

            MatchHistory().set_rating(match_id or "", rating)

    @staticmethod
    def _coach_rating_from_analysis(analysis) -> int | None:
        """Deterministic 1-10 extraction from the analysis prose (no LLM)."""
        import re

        text = analysis or ""
        patterns = [
            r"(?:rating|score)[^0-9]{0,12}(\d{1,2})\s*/\s*10",
            r"(?:rating|score)[^0-9]{0,12}(\d{1,2})\s*(?:/10|out\s*of\s*10)",
        ]
        for pat in patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                return max(1, min(10, int(m.group(1))))
        return None

    def _rate_match_with_llm(self, match_result, analysis) -> int | None:
        """Small STRICT-JSON rating call when the prose carried no rating."""
        try:
            import json

            be = getattr(self._coach, "_backend", None)
            if be is None:
                return None
            snippet = (analysis or "")[:2500]
            system = (
                "You are an expert MTG coach grader. Given a match result and"
                " the post-match analysis, rate how WELL THE PLAYER PLAYED"
                " from 1 to 10 (5 = average, 10 = excellent). Output STRICT"
                " JSON only, with a single field: {\"rating\": int}."
            )
            user = f"MATCH RESULT: {match_result}\n\nANALYSIS:\n{snippet}"
            resp = be.complete(system, user, max_tokens=60, temperature=0.0, request_timeout_s=30)
            if not isinstance(resp, str):
                return None
            start, end = resp.find("{"), resp.rfind("}")
            if start == -1 or end == -1:
                return None
            data = json.loads(resp[start : end + 1])
            val = int(data.get("rating") or 0)
            return max(1, min(10, val)) if val else None
        except Exception as e:
            logger.debug(f"rating LLM call failed: {e}")
            return None

    def _start_auto_advice_score(self, match_id, match_result, advice_history) -> None:
        """Kick a background thread to score advice quality for this match."""
        import threading

        thread = threading.Thread(
            target=self._score_match_advice,
            args=(match_id, match_result, advice_history),
            daemon=True,
            name="auto-advice-score",
        )
        thread.start()

    def _score_match_advice(self, match_id, match_result, advice_history) -> None:
        """One small STRICT-JSON LLM call rating the QUALITY OF THE ADVICE.

        Independent of the larger opt-in prose analysis, so every match gets a
        number automatically. Degrades silently (record stays W/L-only) on any
        error.
        """
        try:
            import json

            be = getattr(self._coach, "_backend", None)
            if be is None or not advice_history:
                return
            lines = []
            for entry in (advice_history or [])[:60]:
                snap = entry.get("game_snapshot") or {}
                turn = snap.get("turn_number", "?")
                phase = snap.get("phase", "?")
                advice = str(entry.get("advice") or "").strip()
                if advice:
                    lines.append(f"Turn {turn} {phase}: {advice[:300]}")
            if not lines:
                return
            user = (
                f"MATCH RESULT: {match_result}\n\n"
                f"COACHING ADVICE GIVEN ({len(lines)} decisions):\n" + "\n".join(lines)
            )
            system = (
                "You are an expert MTG coach grader. Given the match result and the list"
                " of coaching advice the assistant gave at each decision point, rate the"
                " QUALITY OF THE ADVICE from 1 to 10 (5 = average, 10 = expert-equivalent)."
                " Penalize illegal, unclear, or strategically wrong advice. Output STRICT"
                " JSON only: " + '{"rating": int, "reason": "one short sentence"}'
            )
            resp = be.complete(system, user, max_tokens=120, temperature=0.0, request_timeout_s=30)
            if not isinstance(resp, str):
                return
            start, end = resp.find("{"), resp.rfind("}")
            if start == -1 or end == -1:
                return
            data = json.loads(resp[start : end + 1])
            val = int(data.get("rating") or 0)
            if not val:
                return
            reason = str(data.get("reason") or "").strip()
            from arenamcp.match_history import MatchHistory

            MatchHistory().set_rating(match_id or "", max(1, min(10, val)), reason or None)
        except Exception as e:
            logger.debug(f"auto advice score failed: {e}")
