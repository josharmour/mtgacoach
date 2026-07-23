"""Diagnostics/bug-report helpers for StandaloneCoach, extracted from standalone.py.

Pure move: methods are unchanged and mixed back into StandaloneCoach."""

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from arenamcp.backend_health import is_backend_error_text
from arenamcp.logging_config import LOG_DIR, LOG_FILE
from arenamcp.standalone_ui import copy_to_clipboard

logger = logging.getLogger(__name__)


class _DiagnosticsMixin:
    def save_bug_report(
        self,
        reason: str = "User Request",
        *,
        announce: bool = True,
        progress_cb=None,
        extra_context: dict[str, Any] | None = None,
    ) -> Optional["Path"]:
        """Save comprehensive bug report and return path.

        When ``announce`` is false, skip UI logging and only write the report.
        This is used by background workers so UI updates stay on the
        correct thread.
        """
        bug_dir = LOG_DIR / "bug_reports"
        bug_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        bug_file = bug_dir / f"bug_{timestamp}.json"

        try:
            # Collect comprehensive debug info
            report = self._collect_debug_info(
                progress_cb=progress_cb,
                extra_context=extra_context,
            )
            report["reason"] = reason

            with open(bug_file, "w") as f:
                json.dump(report, f, indent=2, default=str)

            # Make path clickable and copy a shareable link to clipboard
            file_url = f"file:///{str(bug_file).replace(chr(92), '/')}"
            clickable = f"\x1b]8;;{file_url}\x1b\\{bug_file}\x1b]8;;\x1b\\"

            clipboard_success = copy_to_clipboard(file_url)

            if announce:
                if clipboard_success:
                    self.ui.log(f"\n[BUG] Saved: {clickable}")
                    self.ui.log(f"[BUG] Link copied to clipboard: {file_url}\n")
                else:
                    self.ui.log(f"\n[BUG] Saved: {clickable}")
                    self.ui.log(f"[BUG] Link: {file_url}\n")

            return bug_file
        except Exception as e:
            if announce:
                self.ui.error(f"\n[BUG] Failed: {e}\n")
            logger.exception("Bug report failed")
            return None

    def _on_bug_report_hotkey(self) -> None:
        """F7 - Save comprehensive bug report and copy path to clipboard."""
        bug_path = self.save_bug_report("Hotkey F7")
        if bug_path:
            self.ui.log("[BUG] Path copied to clipboard. Paste it anywhere.")

    def _auto_bug_report_bridge_fallback(self, reason: str, extra: dict) -> None:
        """Invoked by autopilot whenever the GRE bridge can't submit an action.

        Every bridge miss = a bug. Always save a local report. Attempt a
        silent GitHub upload using the configured token/gh CLI. Rate-limiting
        is handled in autopilot so duplicate calls within a cooldown are
        suppressed; here we just save + try to upload.
        """
        try:
            bug_path = self.save_bug_report(
                reason,
                announce=False,
                extra_context=extra,
            )
            if not bug_path:
                return
            # Quick user-visible log so they know telemetry fired
            self.ui.log(f"[auto-bug] Saved: {bug_path}")
            # Attempt GitHub upload in background (non-blocking).
            import threading

            threading.Thread(
                target=self._auto_upload_bug_to_github,
                args=(bug_path, reason),
                daemon=True,
            ).start()
        except Exception as e:
            logger.debug(f"auto-bug-report failed: {e}")

    def _auto_upload_bug_to_github(self, bug_path, reason: str) -> None:
        """Background GitHub upload for an auto-filed fallback bug report."""
        try:
            import json as _json

            from arenamcp.bugreport import (
                GITHUB_REPO,
                build_issue_payload,
            )

            with open(bug_path, encoding="utf-8") as f:
                report_data = _json.loads(f.read())

            title, body = build_issue_payload(report_data, bug_path, reason)
            # Prefix title to flag these as auto-reports
            if not title.startswith("[auto]"):
                title = "[auto] " + title
            if len(title) > 80:
                title = title[:77].rstrip() + "..."

            # Try GitHub API with gh token first
            token = self._get_github_token_for_auto_bug()
            if token:
                import requests

                resp = requests.post(
                    f"https://api.github.com/repos/{GITHUB_REPO}/issues",
                    headers={
                        "Accept": "application/vnd.github+json",
                        "Authorization": f"Bearer {token}",
                        "X-GitHub-Api-Version": "2022-11-28",
                    },
                    json={"title": title, "body": body, "labels": ["bug", "auto-reported"]},
                    timeout=12,
                )
                if resp.ok:
                    url = str(resp.json().get("html_url", ""))
                    self.ui.log(f"[auto-bug] Uploaded: {url}")
                    return
                logger.debug(f"auto-bug GitHub API failed: {resp.status_code}")
            # No silent CLI fallback: we don't want to prompt the user.
        except Exception as e:
            logger.debug(f"auto-bug upload failed: {e}")

    def _get_github_token_for_auto_bug(self) -> str:
        """Return a GitHub token for silent auto-uploads, or empty string."""
        import os

        token = (
            os.environ.get("MTGACOACH_GITHUB_TOKEN", "").strip() or os.environ.get("GITHUB_TOKEN", "").strip()
        )
        if token:
            return token
        # Try gh CLI (synchronous, but short timeout)
        import shutil

        gh = shutil.which("gh")
        if not gh:
            return ""
        try:
            import subprocess

            result = subprocess.run(
                [gh, "auth", "token"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            if result.returncode == 0:
                return (result.stdout or "").strip()
        except Exception:
            pass
        return ""

    def take_screenshot_analysis(self) -> None:
        """Capture screen and request visual analysis (e.g. Mulligan).

        Tries local VLM first (fast, free), then falls back to online.
        """
        if self._screenshot_analysis_in_progress:
            self.ui.log("[yellow]Screenshot analysis already in progress...[/]")
            return

        self._screenshot_analysis_in_progress = True
        backend_lower = self.backend_name.lower()
        has_local_vlm = (
            self._vision_mapper
            and hasattr(self._vision_mapper, "_local_vlm")
            and self._vision_mapper._local_vlm.available
        )
        vision_capable = has_local_vlm or backend_lower == "online"

        if not vision_capable:
            self.ui.log("[red]No vision backend available. Need Ollama VLM or a cloud backend.[/]")
            self._screenshot_analysis_in_progress = False
            return

        try:
            import io

            from PIL import Image

            from arenamcp.input_controller import find_mtga_hwnd
            from arenamcp.screen_capture import capture_mtga_png, is_mostly_black

            window_rect = None
            if self._vision_mapper:
                window_rect = self._vision_mapper.window_rect or self._vision_mapper.refresh_window()

            bbox = None
            if window_rect:
                left, top, width, height = window_rect
                bbox = (left, top, left + width, top + height)

            try:
                hwnd = find_mtga_hwnd()
            except Exception:
                hwnd = None

            png_bytes = capture_mtga_png(hwnd=hwnd, bbox=bbox)
            if not png_bytes:
                self.ui.log("[red]Screenshot capture returned no data.[/]")
                return

            # Detect a black capture and bail with a clear message instead of
            # sending the VLM a blank frame (it will then hallucinate advice
            # like "re-upload a clear image" which is unhelpful mid-match).
            img = Image.open(io.BytesIO(png_bytes))
            if is_mostly_black(img):
                self.ui.log(
                    "[red]Screenshot came back black — MTGA's DirectX frame "
                    "wasn't captured. Try windowed mode or update Windows.[/]"
                )
                return

            img.thumbnail((1920, 1080))
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            png_bytes = buf.getvalue()

            self.ui.log("[yellow]Analyzing screenshot...[/]")
            self.ui.subtask("Analyzing screenshot")

            # Context
            try:
                game_state = self.get_game_state()
            except Exception as e:
                logger.debug(f"Could not get game state for screenshot analysis: {e}")
                game_state = {}

            ctx = ""
            if game_state:
                turn_num = game_state.get("turn", {}).get("turn_number", "?")
                phase = game_state.get("turn", {}).get("phase", "")
                life_you = 20
                life_opp = 20
                for p in game_state.get("players", []):
                    if p.get("is_local"):
                        life_you = p.get("life_total", 20)
                    else:
                        life_opp = p.get("life_total", 20)
                ctx = f" Turn {turn_num}, {phase}. Life: You {life_you}, Opp {life_opp}."

            screen_prompt = (
                "You are an expert Magic: The Gathering Arena coach. "
                "Look at this screenshot and give immediate, actionable advice.\n\n"
                "DETECT THE SITUATION AND RESPOND:\n"
                "- Keep/Mulligan: Start with KEEP or MULLIGAN, then brief reason.\n"
                "- Scry: Say TOP or BOTTOM with one-sentence reasoning.\n"
                "- Surveil: Say GRAVEYARD or LIBRARY.\n"
                "- Modal/choice: Recommend which option and why.\n"
                "- Target selection: Say which target to pick.\n"
                "- Combat: Give optimal attacks/blocks.\n"
                "- Your turn: Recommend the best play.\n"
                "- Opponent acting: Note if you should respond.\n\n"
                "BE DECISIVE. 1-2 sentences max, spoken aloud.\n"
                f"Game context:{ctx}"
            )

            advice = None
            system_prompt = screen_prompt

            # Try the current backend's multimodal API if online
            if backend_lower == "online" and self._coach and self._coach._backend:
                self.ui.log("[yellow]Analyzing via online backend...[/]")
                try:
                    be = self._coach._backend
                    if hasattr(be, "complete_with_image"):
                        advice = be.complete_with_image(
                            system_prompt,
                            f"What should I do here?{ctx}",
                            png_bytes,
                        )
                        if advice:
                            logger.info(f"[Online screen analysis] {len(advice)} chars")
                except Exception as e:
                    logger.error(f"Online screen analysis failed: {e}")
                    self.ui.log(f"[dim red]Online vision failed: {e}[/]")

            # Fall back to local VLM if online didn't produce advice
            if not advice and has_local_vlm:
                self.ui.log("[dim cyan]Falling back to local Ollama VLM...[/]")
                try:
                    import base64
                    import urllib.request

                    vlm = self._vision_mapper._local_vlm
                    b64_image = base64.b64encode(png_bytes).decode("utf-8")
                    payload = json.dumps(
                        {
                            "model": vlm.model,
                            "prompt": screen_prompt,
                            "images": [b64_image],
                            "stream": False,
                            "options": {"temperature": 0.3, "num_predict": 200},
                        }
                    ).encode("utf-8")
                    req = urllib.request.Request(
                        f"{vlm.endpoint}/api/generate",
                        data=payload,
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=vlm.timeout) as resp:
                        raw = json.loads(resp.read())
                    advice = raw.get("response", "").strip()
                    if advice:
                        logger.info(f"[Ollama screen analysis] {len(advice)} chars")
                except Exception as e:
                    logger.error(f"Ollama screen analysis failed: {e}")
                    self.ui.log(f"[dim red]Ollama VLM fallback failed: {e}[/]")

            # Detect garbled VLM output (non-vision model processing image tokens)
            if advice and self._is_garbled(advice):
                logger.warning(f"VLM returned garbled response ({len(advice)} chars), discarding")
                self.ui.log("[red]VLM returned garbled output — is the Ollama model a vision model?[/]")
                advice = None

            if advice and not is_backend_error_text(advice):
                self.ui.advice(advice, "Visual Analysis")
                if self._auto_speak:
                    self.speak_advice(advice, blocking=False)
            else:
                self.ui.log(f"[red]Vision analysis failed: {advice or 'no response'}[/]")

        except ImportError:
            self.ui.log("[red]Missing 'Pillow' library. Install with: pip install Pillow[/]")
        except Exception as e:
            self.ui.log(f"[red]Screenshot error: {e}[/]")
            print(f"Screenshot Error: {e}")
        finally:
            self.ui.subtask("")
            self._screenshot_analysis_in_progress = False

    def _collect_debug_info(
        self,
        progress_cb=None,
        extra_context: dict[str, Any] | None = None,
    ) -> dict:
        """Collect comprehensive debug information for bug reports."""
        import platform

        from arenamcp import __version__

        def _progress(message: str) -> None:
            if progress_cb is None:
                return
            try:
                progress_cb(message)
            except Exception:
                logger.debug("Bug report progress callback failed", exc_info=True)

        report = {
            "timestamp": datetime.now().isoformat(),
            "version": __version__,
            "reporter": {
                "install_id": self.settings.get("install_id") if self.settings else None,
            },
        }

        _progress("Collecting system info...")
        report["system"] = {
            "platform": platform.platform(),
            "python_version": platform.python_version(),
            "machine": platform.machine(),
            "packages": self._get_package_versions(),
        }

        report["config"] = {
            "backend": self.backend_name,
            "model": self.model_name,
            # What the gateway actually ran, from response payloads — the
            # configured alias can lie (2026-07-05 misroute).
            "served_model": self._get_served_model(),
            "voice_mode": self.voice_mode,
            "advice_style": self.advice_style,
            "advice_frequency": self.advice_frequency,
            "draft_mode": self.draft_mode,
            "set_code": self.set_code,
            "auto_speak": self._auto_speak,
        }
        report["settings"] = dict(self.settings._data) if self.settings else {}
        report["mtga_log"] = self._get_mtga_log_status()

        _progress("Capturing current game state...")
        # Bug A: prefer deep-copied completion-time snapshot over live state
        # to prevent F7 bug reports from mixing games in a BO3.
        if self._post_match_snapshot and self._post_match_analysis_text:
            report["game_state"] = self._post_match_snapshot
            report["game_state_source"] = "post_match_snapshot"
        else:
            report["game_state"] = self._mcp.get_game_state() if self._mcp else {}
            report["game_state_source"] = "live"
        report["draft_state"] = self._mcp.get_draft_pack() if self._mcp else {}

        _progress("Collecting match context...")
        report["match_context"] = self._get_match_context()
        report["voice"] = {
            "tts_enabled": self._voice_output is not None,
            "tts_muted": self._voice_output._muted if self._voice_output else None,
            "tts_voice": self._voice_output.current_voice if self._voice_output else None,
            "stt_enabled": self._voice_input is not None,
        }
        report["post_match_feedback"] = self._collect_post_match_feedback(extra_context=extra_context)
        # Screenshots passed through from the UI (coach window + MTGA window).
        if extra_context and isinstance(extra_context.get("screenshots"), dict):
            report["screenshots"] = dict(extra_context.get("screenshots") or {})
        if extra_context:
            event_context = {key: value for key, value in dict(extra_context).items() if key != "screenshots"}
            if event_context:
                report["event_context"] = event_context
                auto_bug = event_context.get("auto_fallback_bug")
                if isinstance(auto_bug, dict):
                    report["auto_fallback_bug"] = dict(auto_bug)
                auto_takeover = event_context.get("auto_user_takeover")
                if isinstance(auto_takeover, dict):
                    report["auto_user_takeover"] = dict(auto_takeover)
        report["advice_history"] = list(self._advice_history) if hasattr(self, "_advice_history") else []
        report["llm_context"] = self._get_llm_context()

        _progress("Collecting logs and diagnostics...")
        report["recent_logs"] = self._get_recent_logs(100)
        report["enrichment_failures"] = self._get_enrichment_failures()
        report["autopilot"] = self._collect_autopilot_info()
        report["bridge_state"] = self._collect_bridge_state()
        report["errors"] = list(self._recent_errors) if hasattr(self, "_recent_errors") else []
        # The in-memory error list only sees errors explicitly recorded via
        # _record_error — on 2026-07-16 it read [] while standalone.log held
        # 435 proxy 401s, so the report's "Recent Errors" section actively
        # misled debugging. Pull ERROR-level lines straight from the log.
        report["recent_log_errors"] = self._tail_log_errors()
        report["uptime_seconds"] = (
            (datetime.now() - self._start_time).total_seconds() if hasattr(self, "_start_time") else None
        )

        _progress("Collecting bridge and replay info...")
        report["bepinex_log"] = self._read_bepinex_log()
        report["replay"] = self._collect_replay_info(include_recent_replays=False)

        return report

    def _collect_post_match_feedback(
        self,
        *,
        extra_context: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        context = dict(extra_context or {})
        analysis = str(context.get("analysis") or self._post_match_analysis_text or "").strip()
        feedback = str(context.get("user_feedback") or "").strip()
        match_result = str(context.get("match_result") or self._post_match_result or "").strip()
        source = str(context.get("source") or "").strip()

        if not any((analysis, feedback, match_result, source)):
            return None

        payload: dict[str, Any] = {}
        if source:
            payload["source"] = source
        if match_result:
            payload["match_result"] = match_result
        if analysis:
            payload["analysis"] = analysis
        if feedback:
            payload["user_feedback"] = feedback

        # Bug A: include deep-copied completion-time snapshot so
        # the bug report's game_state field doesn't mix games.
        if self._post_match_snapshot:
            payload["post_match_snapshot"] = self._post_match_snapshot

        return payload

    def _collect_autopilot_info(self) -> dict:
        """Collect autopilot state for bug reports."""
        ap = getattr(self, "_autopilot", None)
        info: dict = {
            "enabled": getattr(self, "_autopilot_enabled", False),
            "dry_run": getattr(self, "_autopilot_dry_run", False),
            "afk": getattr(self, "_autopilot_afk", False),
            "initialized": ap is not None,
        }
        if ap:
            try:
                info["engine"] = ap.get_debug_info()
            except Exception as e:
                info["engine_error"] = str(e)
        return info

    def _collect_bridge_state(self) -> dict:
        """Collect GRE bridge/poller state for bug reports."""
        bp = getattr(self, "_bridge_poller", None)
        if not bp:
            return {"available": False}
        try:
            info: dict = {
                "available": True,
                "connected": bp.connected,
                "bridge_connected": bp._bridge.connected if bp._bridge else False,
                "fallback_mode": bp._fallback_mode,
                "last_request_type": bp._last_request_type,
                "last_has_pending": bp._last_has_pending,
                "last_action_sig": bp._last_action_sig,
                "consecutive_errors": bp._consecutive_errors,
                "match_boundary_age_s": round(time.time() - getattr(self, "_match_boundary_ts", 0), 1),
            }
            # Include last poll result summary (actions count, request type)
            poll = bp._last_poll_result
            if poll:
                info["last_poll"] = {
                    "has_pending": poll.get("has_pending"),
                    "request_type": poll.get("request_type"),
                    "request_class": poll.get("request_class"),
                    "num_actions": len(poll.get("actions", [])),
                    "can_pass": poll.get("can_pass"),
                    "can_cancel": poll.get("can_cancel"),
                }
            else:
                info["last_poll"] = None
            return info
        except Exception as e:
            return {"available": True, "error": str(e)}

    def _enable_replay_recording(self) -> None:
        """Auto-enable MTGA replay recording at match start."""

        def _try_enable():
            import time

            # Bridge may not be connected yet at seat detection time —
            # retry for up to 5 seconds.
            for attempt in range(10):
                try:
                    bridge = self._bridge_poller._bridge if self._bridge_poller else None
                    if bridge and bridge.connected:
                        status = bridge.get_replay_status()
                        if status and status.get("recording"):
                            logger.debug("Replay recording already active")
                            return
                        result = bridge.enable_replay("mtgacoach")
                        if result:
                            folder = result.get("replay_folder", "unknown")
                            recording = result.get("recording", False)
                            recorder_type = result.get("recorder_type", "none")
                            logger.info(
                                f"Replay recording enabled: {folder} "
                                f"(recording={recording}, recorder={recorder_type})"
                            )
                        else:
                            logger.debug("enable_replay returned None (plugin may not support it)")
                        return
                except Exception as e:
                    logger.debug(f"Replay enable attempt {attempt + 1} failed: {e}")
                time.sleep(0.5)
            logger.debug("Replay enable: bridge never connected after 5s")

        import threading

        threading.Thread(target=_try_enable, daemon=True).start()

    def _collect_replay_info(self, *, include_recent_replays: bool = True) -> dict:
        """Collect replay recording state for bug reports."""
        info: dict = {"available": False}
        try:
            bridge = self._bridge_poller._bridge if self._bridge_poller else None
            if not bridge or not bridge.connected:
                return info
            info["available"] = True
            status = bridge.get_replay_status()
            if status:
                info["recording"] = status.get("recording", False)
                info["recorder_found"] = status.get("recorder_found", False)
                info["recorder_type"] = status.get("recorder_type")
                info["replay_folder"] = status.get("replay_folder")
                info["replay_file"] = status.get("replay_file")
                info["message_count"] = status.get("message_count")
                info["prefs_enabled"] = status.get("prefs_enabled")
                if status.get("_debug_methods"):
                    info["_debug_methods"] = status.get("_debug_methods")
                if status.get("_debug_fields"):
                    info["_debug_fields"] = status.get("_debug_fields")
            if info.get("replay_file"):
                info["latest_replay_path"] = info.get("replay_file")
            if include_recent_replays:
                replays = bridge.list_replays()
                if replays:
                    replay_list = replays.get("replays", [])
                    info["recent_replays"] = replay_list[:5]
                    info["total_replays"] = len(replay_list)
                    if replay_list and not info.get("latest_replay_path"):
                        info["latest_replay_path"] = replay_list[0].get("path")
        except Exception as e:
            info["error"] = str(e)
        return info

    def _get_latest_replay_path(self) -> str | None:
        """Get the most recent replay file path from the bridge."""
        try:
            bridge = self._bridge_poller._bridge if self._bridge_poller else None
            if not bridge or not bridge.connected:
                return None
            # Check active recording first
            status = bridge.get_replay_status()
            if status and status.get("replay_file"):
                return status["replay_file"]
            # Fall back to most recent in replay folder
            replays = bridge.list_replays()
            if replays:
                replay_list = replays.get("replays", [])
                if replay_list:
                    return replay_list[0].get("path")
        except Exception as e:
            logger.debug(f"Failed to get latest replay path: {e}")
        return None

    @staticmethod
    def _get_package_versions() -> dict[str, str]:
        """Get versions of key installed packages."""
        import importlib.metadata

        versions: dict[str, str] = {}
        package_names = {
            "textual": "textual",
            "openai": "openai",
            "mcp": "mcp",
            "watchdog": "watchdog",
            "requests": "requests",
            "faster_whisper": "faster-whisper",
            "kokoro-onnx": "kokoro-onnx",
            "anthropic": "anthropic",
            "google.genai": "google-genai",
        }
        for display_name, package_name in package_names.items():
            try:
                versions[display_name] = importlib.metadata.version(package_name)
            except importlib.metadata.PackageNotFoundError:
                versions[display_name] = "not installed"
            except Exception as e:
                versions[display_name] = f"error: {e}"
        return versions

    def _bridge_capable_install(self) -> bool:
        """Cached: can a BepInEx bridge ever exist for this install?"""
        cached = getattr(self, "_bridge_capable_cache", None)
        if cached is None:
            try:
                from arenamcp.platform_integration import bridge_capable

                cached = bridge_capable()
            except Exception:
                cached = True  # fail toward the normal Disconnected message
            self._bridge_capable_cache = cached
        return cached

    def _get_served_model(self) -> str:
        """The model the gateway actually ran for the last completion."""
        try:
            backend = getattr(self._coach, "_backend", None)
            return str(getattr(backend, "last_served_model", "") or "")
        except Exception:
            return ""

    def _get_mtga_log_status(self) -> dict:
        """Get MTGA Player.log file status."""
        import os

        # Use the same path logic as watcher.py: LOCALAPPDATA (AppData\Local)
        # -> parent (AppData) -> LocalLow sibling
        _local_appdata = os.environ.get("LOCALAPPDATA", "")
        if _local_appdata:
            default_path = str(
                Path(os.path.dirname(_local_appdata))
                / "LocalLow"
                / "Wizards Of The Coast"
                / "MTGA"
                / "Player.log"
            )
        else:
            default_path = ""
        log_path = os.environ.get("MTGA_LOG_PATH", default_path)
        result: dict = {"path": log_path}
        try:
            p = Path(log_path)
            result["exists"] = p.exists()
            if p.exists():
                stat = p.stat()
                result["size_bytes"] = stat.st_size
                result["last_modified"] = datetime.fromtimestamp(stat.st_mtime).isoformat()
        except Exception as e:
            result["error"] = str(e)
        return result

    def _read_bepinex_log(self) -> str | None:
        """Read BepInEx plugin log for bridge debugging."""
        try:
            import os

            # Standard MTGA install path
            candidates = [
                Path(os.environ.get("PROGRAMFILES", "C:\\Program Files"))
                / "Wizards of the Coast"
                / "MTGA"
                / "BepInEx"
                / "LogOutput.log",
            ]
            for p in candidates:
                if p.exists():
                    text = p.read_text(encoding="utf-8", errors="replace")
                    # Return last 4KB to keep report size reasonable
                    if len(text) > 4096:
                        return "...(truncated)...\n" + text[-4096:]
                    return text
        except Exception as e:
            return f"Error reading BepInEx log: {e}"
        return None

    def _get_match_context(self) -> dict:
        """Get match-level context: match ID, opponent cards, recent events."""
        ctx: dict = {}
        try:
            if not self._mcp:
                return ctx
            from arenamcp.server import game_state

            ctx["match_id"] = getattr(game_state, "match_id", None)
            ctx["local_seat_id"] = getattr(game_state, "local_seat_id", None)
            ctx["seat_source"] = (
                game_state.get_seat_source_name() if hasattr(game_state, "get_seat_source_name") else None
            )
            # Opponent played cards
            try:
                from arenamcp.server import get_opponent_played_cards

                opp = get_opponent_played_cards()
                ctx["opponent_played_cards"] = opp if opp else []
            except Exception as e:
                logger.debug(f"Could not get opponent played cards for bug report: {e}")
                ctx["opponent_played_cards"] = []
            # Recent game events
            snapshot = game_state.get_snapshot() if hasattr(game_state, "get_snapshot") else {}
            ctx["recent_events"] = snapshot.get("recent_events", [])[-20:]
            ctx["damage_taken"] = snapshot.get("damage_taken", {})
        except Exception as e:
            ctx["error"] = str(e)
        return ctx

    def _get_llm_context(self) -> dict:
        """Get the LLM context from the most recent advice (what the LLM actually saw).

        IMPORTANT: This captures the game state that was sent to the LLM during the last
        advice generation, NOT the current game state. This prevents timing bugs where
        the game state changes between advice generation and bug report generation.
        """
        context = {
            "system_prompt": None,
            "formatted_game_state": None,
        }

        try:
            if self._coach:
                context["system_prompt"] = getattr(self._coach, "_system_prompt", None)
                context["deck_strategy"] = getattr(self._coach, "_deck_strategy", None)

                # Use the game_context from the last advice_history entry instead of
                # regenerating it, to avoid timing issues where game state has changed
                if hasattr(self, "_advice_history") and self._advice_history:
                    context["formatted_game_state"] = self._advice_history[-1].get("game_context")
                # Fallback: if no advice history, generate from current state
                elif self._mcp and hasattr(self._coach, "_format_game_context"):
                    game_state = self._mcp.get_game_state()
                    context["formatted_game_state"] = self._coach._format_game_context(game_state)
        except Exception as e:
            context["error"] = str(e)

        return context

    def _get_enrichment_failures(self) -> list:
        """Get card enrichment (oracle text lookup) failures for bug reports."""
        try:
            from arenamcp.server import get_enrichment_failures

            return get_enrichment_failures()
        except Exception as e:
            logger.debug(f"Could not get enrichment failures: {e}")
            return []

    def _get_recent_logs(self, num_lines: int = 100) -> list:
        """Get recent log entries from standalone.log."""
        try:
            if LOG_FILE.exists():
                with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
                    # Read last N lines efficiently
                    lines = f.readlines()
                    return lines[-num_lines:]
        except Exception as e:
            return [f"Error reading logs: {e}"]
        return []

    def _record_advice(
        self, advice: str, trigger: str, game_context: str = None, game_state: dict = None
    ) -> None:
        """Record advice for debug history with full game state.

        Args:
            advice: The advice text that was given
            trigger: What triggered this advice
            game_context: Pre-formatted context string (optional)
            game_state: Game state dict to use for context (optional, avoids re-polling)
        """
        if not hasattr(self, "_advice_history"):
            self._advice_history = []

        # Use provided game_state, or fetch fresh if needed
        # IMPORTANT: If game_state is provided, use it to avoid timing issues
        # where the game state changes between advice generation and recording
        if game_context is None and self._coach:
            try:
                if game_state is None and self._mcp:
                    game_state = self._mcp.get_game_state()
                if game_state and hasattr(self._coach, "_format_game_context"):
                    game_context = self._coach._format_game_context(game_state)
            except Exception as e:
                logger.debug(f"Could not format game context for advice record: {e}")

        # Extract key game state fields for bug reports (turn, life, board snapshot)
        game_snapshot = None
        if game_state:
            try:
                turn = game_state.get("turn", {})
                players = game_state.get("players", [])
                game_snapshot = {
                    "turn_number": turn.get("turn_number"),
                    "phase": turn.get("phase"),
                    "active_player": turn.get("active_player"),
                    "players": [
                        {"seat_id": p.get("seat_id"), "life_total": p.get("life_total")} for p in players
                    ],
                    "battlefield_count": len(game_state.get("battlefield", [])),
                    "hand_count": len(game_state.get("hand", [])),
                }
            except Exception as e:
                logger.debug(f"Could not extract game snapshot for advice record: {e}")

        entry = {
            "timestamp": datetime.now().isoformat(),
            "trigger": trigger,
            "advice": advice,
            "game_context": game_context[:8000] if game_context else None,
            "game_snapshot": game_snapshot,
        }
        self._advice_history.append(entry)

        # Keep only last 50 entries (enough for post-match analysis)
        if len(self._advice_history) > 50:
            self._advice_history = self._advice_history[-50:]

        # Also record to match recording for post-match analysis
        try:
            from arenamcp.match_validator import get_current_recording

            current = get_current_recording()
            if current:
                # Extract turn/phase from game state
                turn_info = game_state.get("turn", {}) if game_state else {}
                parsed_turn = turn_info.get("turn_number", 0)
                parsed_phase = turn_info.get("phase", "")
                current.add_advice_event(
                    trigger=trigger,
                    advice=advice,
                    game_context=game_context or "",
                    parsed_turn=parsed_turn,
                    parsed_phase=parsed_phase,
                )
        except Exception as e:
            logger.debug(f"Advice recording failed (non-fatal): {e}")

    def _tail_log_errors(self, max_lines: int = 20, tail_bytes: int = 262144) -> list[str]:
        """Last ERROR-level lines from the live log file, for bug reports."""
        try:
            from arenamcp.logging_config import LOG_FILE

            with open(LOG_FILE, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - tail_bytes))
                text = f.read().decode("utf-8", errors="replace")
            errors = [
                line.strip() for line in text.splitlines() if "| ERROR " in line or "| WARNING " in line
            ]
            return errors[-max_lines:]
        except Exception:
            return []

    def _record_error(self, error: str, context: str = None) -> None:
        """Record error for debug history."""
        if not hasattr(self, "_recent_errors"):
            self._recent_errors = []

        entry = {
            "timestamp": datetime.now().isoformat(),
            "error": error,
            "context": context,
        }
        self._recent_errors.append(entry)

        # Keep only last 10 errors
        if len(self._recent_errors) > 10:
            self._recent_errors = self._recent_errors[-10:]
