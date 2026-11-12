"""Session and subprocess coordinator for MTGA Coach desktop."""

from __future__ import annotations

import contextlib
import logging
import os
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QTimer, Signal

from .coach_process import CoachProcess
from .tts_manager import TtsManager

logger = logging.getLogger(__name__)


SelectionIdentity = tuple[str | None, str | None, str | None]


def _selection_identity() -> SelectionIdentity:
    """The full launch-selection identity of this process."""
    root = os.environ.get("MTGACOACH_APP_ROOT", "").strip() or None
    sha = os.environ.get("MTGACOACH_SNAPSHOT_SHA", "").strip() or None
    snapshot_root = os.environ.get("MTGACOACH_SNAPSHOT_ROOT", "").strip() or None
    return root, sha, snapshot_root


def _require_prepared_snapshot_dep_env(target_root: str) -> str | None:
    """Verify the PREPARED dependency environment for an adopted snapshot.

    Reload NEVER installs anything: this is a read-only lookup of the
    environment an explicit preparation step (launcher-selected launch or a
    full Restart Coach) already built for exactly the interpreter a later
    engine start from ``target_root`` will select. Ordinary launches and
    reloads never reach it, and a missing environment is a visible deferral,
    never a silent installation.

    Returns ``None`` when the prepared environment exists, else a
    human-readable reason; the caller keeps the current engine running and
    defers visibly on failure.
    """
    from .coach_process import engine_interpreter_for
    from .dep_env import prepared_dep_env

    try:
        python_exe, _source = engine_interpreter_for(target_root)
        prepared = prepared_dep_env(Path(target_root), python_executable=python_exe)
    except Exception as exc:  # noqa: BLE001 - any failure must defer, not crash
        logger.warning("Snapshot dependency lookup failed: %s", exc)
        return str(exc)
    if prepared is None:
        return (
            "no prepared dependency environment for this snapshot; prepare it "
            "from the launcher or use Restart Coach"
        )
    return None


def _apply_selection_identity(root: str | None, sha: str | None, snapshot_root: str | None) -> None:
    """Move the whole selection identity at once.

    A new SHA paired with a stale SNAPSHOT_ROOT would let readers trust the
    wrong tree; all three variables are updated (or cleared) together.
    """
    for key, value in (
        ("MTGACOACH_APP_ROOT", root),
        ("MTGACOACH_SNAPSHOT_SHA", sha),
        ("MTGACOACH_SNAPSHOT_ROOT", snapshot_root),
    ):
        if value:
            os.environ[key] = value
        else:
            os.environ.pop(key, None)


def _warn_rollback(session: Any, attempted_sha: str, previous: SelectionIdentity) -> None:
    logger.warning(
        "Adopted snapshot %s failed; rolling back to %s",
        attempted_sha[:7] or "?",
        previous[0] or "(checkout)",
    )
    session.logEmitted.emit(
        "New engine snapshot failed to start; rolling back to the previous version.",
        "error",
    )


class CoachSession(QObject):
    """Coordinates the background standalone coach process and TTS engine."""

    # Core state & advice signals
    gameStateChanged = Signal(dict)
    commentaryStatusChanged = Signal(dict)
    turnPlanChanged = Signal(object)
    gamePlanChanged = Signal(object)
    # Auto-concede countdown: {"state": offering|armed|conceding|sent|conceded|cancelled|aborted|failed|unconfirmed, "id", ...}
    concedeCountdownChanged = Signal(dict)
    statusChanged = Signal(str, str)
    startupStatusChanged = Signal(dict)
    # Transient backend health (ok/degraded/down) from the engine (#215):
    # rendered as a small status line, never as the startup card.
    backendHealthChanged = Signal(dict)
    autopilotBugStatusChanged = Signal(dict)
    spokenLine = Signal(str)
    logEmitted = Signal(str, str)  # message, role
    adviceReceived = Signal(str, str)  # text, label
    deckListChanged = Signal(list, str)  # decks, note
    deckAnalysisChanged = Signal(dict)
    limitedDeckPreviewChanged = Signal(dict)
    collectionChanged = Signal(dict)  # owned cards + wildcard balances (#204)
    deckOpResult = Signal(dict)  # update_deck / create_deck / craft result (#204)
    bugReportSaved = Signal(str, str)  # path, error
    # A user-initiated /bugreport is waiting for explicit confirmation before
    # anything is published. Payload: request_id, destination, title, body,
    # report_path.
    bugReportConfirmationRequested = Signal(dict)
    errorOccurred = Signal(str)

    # Tactical-search result for the sidebar's tactical line
    mctsUpdated = Signal(object)

    # Lifecycle signals
    started = Signal()
    stopped = Signal()
    processExited = Signal(int)
    # The engine asks to be reloaded (its code changed; it is between matches).
    # The window routes this to the same slot as the Reload engine button.
    engineReloadRequested = Signal(str)  # reason

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._process = CoachProcess(self)
        self._process.event_received.connect(self._handle_process_event)
        self._process.stderr_line.connect(self._handle_stderr)
        self._process.exited.connect(self._handle_exited)

        self._tts = TtsManager(self)
        from .commentary_controller import CommentaryController

        self._speech = CommentaryController(
            self._tts, lambda payload: self._process.send_payload(payload), self.spokenLine.emit, self
        )
        self._speech.statusChanged.connect(self.commentaryStatusChanged)
        self._tts.status_line.connect(
            lambda message: self.commentaryStatusChanged.emit({"state": "voice", "message": message})
        )
        self._tts.error_line.connect(
            lambda message: self.commentaryStatusChanged.emit({"state": "voice", "message": message})
        )
        # Lazy-start: TtsManager.request_speech() starts the Kokoro worker on
        # demand (and start() re-arms it). Eagerly spawning a worker per
        # CoachSession means every Constructed test panel owns a live Kokoro
        # subprocess that dies inside the QObject destructor cascade at GC —
        # the source of the intermittent SIGSEGV/QProcess-destroyed noise in
        # offscreen pytest runs.

        self._last_game_state: dict[str, Any] = {}
        self._statuses: dict[str, str] = {}
        self._autopilot_active = False
        self._muted = self._speech.arbiter.muted
        self._last_startup_status: dict[str, Any] = {}
        self._last_backend_health: dict[str, Any] = {}
        self._last_autopilot_bug_status: dict[str, Any] = {}
        self._last_concede: dict[str, Any] = {}
        self._start_options = {"autopilot": False, "dry_run": False, "afk": False}
        self._pending_restart: dict[str, bool] | None = None
        # Generation counter guarding deferred-reload watchdog timers: bumped
        # whenever a pending reload is finished, cancelled or torn down so a
        # late timer can never act on a superseded reload.
        self._reload_generation = 0
        # Snapshot handoff target for the NEXT engine start: (root, commit).
        # Consumed by _finish_restart; set only when a reload plan decided to
        # adopt a published snapshot at a safe checkpoint.
        self._pending_snapshot_target: tuple[str, str, str] | None = None
        # Set when the running engine announces its committed handoff
        # (``reload_ready``) while a restart is pending. A later process exit
        # is only treated as a committed adoption when this was seen;
        # otherwise the exit recovers the CURRENT engine version instead of
        # adopting a snapshot the dying engine never announced.
        self._reload_commit_seen = False
        self._adoption_generation = 0
        self._pending_rollback: tuple[tuple[str | None, str | None, str | None], dict[str, bool]] | None = (
            None
        )
        self._last_start_options: dict[str, bool] = {}
        self._shutting_down = False

    @property
    def is_running(self) -> bool:
        return self._process.is_running

    @property
    def last_game_state(self) -> dict[str, Any]:
        return self._last_game_state

    @property
    def last_startup_status(self) -> dict[str, Any]:
        return dict(self._last_startup_status)

    @property
    def last_backend_health(self) -> dict[str, Any]:
        return dict(self._last_backend_health)

    @property
    def last_autopilot_bug_status(self) -> dict[str, Any]:
        return dict(self._last_autopilot_bug_status)

    def _set_autopilot_bug_status(self, status: dict[str, Any]) -> None:
        self._last_autopilot_bug_status = dict(status)
        self.autopilotBugStatusChanged.emit(dict(status))

    def _set_startup_status(self, status: dict[str, Any]) -> None:
        if status.get("ready"):
            # Handshake complete: any armed adoption watchdog is satisfied.
            self._adoption_generation += 1
        self._last_startup_status = dict(status)
        self.startupStatusChanged.emit(dict(status))

    def start(
        self,
        autopilot: bool = False,
        dry_run: bool = False,
        afk: bool = False,
        *,
        engine_reload: bool = False,
    ) -> bool:
        """Start the background coaching subprocess.

        Returns True when the process was started; False when it was skipped
        or failed. Callers that must react to a failed snapshot adoption use
        the return value to roll back to the previous root.
        """
        if self.is_running or self._shutting_down:
            return False
        self._start_options = {"autopilot": autopilot, "dry_run": dry_run, "afk": afk}
        self._last_start_options = dict(self._start_options)
        self._set_startup_status(
            {
                "phase": "starting",
                "message": "Reloading coaching engine…" if engine_reload else "Starting coach…",
                "ready": False,
            }
        )
        with contextlib.suppress(Exception):
            if not self._tts.is_running:
                self._tts.start()
        try:
            self._process.start(autopilot=autopilot, dry_run=dry_run, afk=afk, engine_reload=engine_reload)
            self.started.emit()
            self.logEmitted.emit("✅ Coach process started.", "status")
            return True
        except Exception as e:
            logger.error(f"Failed to start coach process: {e}")
            self._set_startup_status(
                {"phase": "error", "message": f"Coach could not start: {e}", "ready": False}
            )
            self.errorOccurred.emit(str(e))
            self.logEmitted.emit(f"❌ Failed to start coach process: {e}", "error")
            return False

    def stop(self) -> None:
        """Stop the background coaching subprocess."""
        self._pending_restart = None
        self._reload_generation += 1
        self._adoption_generation += 1
        if not self.is_running:
            return
        self._process.stop()

    def _defer_engine_reload_visibly(self, reason: str) -> None:
        """Incompatible/unknown engine version: keep the current engine and
        tell the user a full Restart Coach is required. Never reload silently."""
        logger.warning("Engine reload deferred: %s", reason)
        self.logEmitted.emit(reason, "error")
        self._set_startup_status(
            {
                "phase": "restart_required",
                "message": reason,
                "ready": True,
            }
        )

    def _gate_engine_reload_plan(self, plan: Any) -> bool:
        """Single decision gate shared by both reload entry points.

        Returns ``True`` when a reload may proceed (plain reload, or adoption
        of a compatible snapshot whose dependency environment is ALREADY
        prepared); otherwise defers visibly and returns ``False``.

        Reload performs NO dependency work: adoption only verifies — read-only
        — that a prepared environment exists. Preparation happens exclusively
        at explicit opt-in points outside the reload path (the launcher's
        selected-snapshot launch or a full Restart Coach). A missing
        environment defers visibly and keeps the current engine running.
        """
        if plan.action == "require_full_restart":
            self._defer_engine_reload_visibly(plan.reason)
            return False
        if plan.action == "adopt":
            logger.info("Engine reload will adopt a newer snapshot: %s", plan.reason)
            self.logEmitted.emit(plan.reason, "status")
            # Prepared-environment-only: verify (read-only) that the target's
            # dependency environment already exists. If it does not, keep the
            # current engine and defer visibly — never install from a reload;
            # preparation requires the launcher or a full Restart Coach.
            prep_failure = _require_prepared_snapshot_dep_env(plan.target_root)
            if prep_failure is not None:
                self._defer_engine_reload_visibly(
                    f"New engine snapshot is not prepared; keeping the current engine. ({prep_failure})"
                )
                return False
            self._pending_snapshot_target = (
                plan.target_root,
                plan.target_commit,
                plan.target_root,
            )
        return True

    def restart(
        self,
        autopilot: bool | None = None,
        dry_run: bool | None = None,
        afk: bool | None = None,
    ) -> None:
        """Reload Python code after the old engine has fully exited.

        Preserve the UI and current runtime controls. The new child skips
        the optional remote warmup; the remote model server stays running.
        """
        if self._shutting_down or self._pending_restart is not None:
            return
        # Consult the published selection exactly once, at this safe
        # checkpoint. Manual Reload Engine and engine-requested reloads both
        # come through here, so neither can bypass a newly published
        # compatible snapshot or silently reload into an incompatible one.
        plan = self._plan_snapshot_reload()
        if not self._gate_engine_reload_plan(plan):
            return
        options = dict(self._start_options)
        if "AUTOPILOT" in self._statuses:
            options["autopilot"] = self._autopilot_active
        if "AFK" in self._statuses:
            options["afk"] = "ON" in self._statuses["AFK"]
        if "DRY_RUN" in self._statuses:
            options["dry_run"] = "ON" in self._statuses["DRY_RUN"]
        for key, value in (("autopilot", autopilot), ("dry_run", dry_run), ("afk", afk)):
            if value is not None:
                options[key] = value
        self._pending_restart = options
        self._reload_disposition_pending = False
        self._reload_commit_seen = False
        self._speech.stop()
        self._set_startup_status(
            {
                "phase": "reloading",
                "message": "Reloading engine — saving match context and stopping current work…",
                "ready": False,
            }
        )
        self.logEmitted.emit("Reloading coaching engine; the model server stays running.", "status")
        if self.is_running:
            self._process.stop_async(command="prepare_engine_reload")
            self._arm_safe_reload_deferral()
        else:
            self._finish_restart()

    def _arm_safe_reload_deferral(self) -> None:
        """Defer — never force-terminate — an engine that has not exited yet.

        ``prepare_engine_reload`` asks the engine to finish its active
        decision, checkpoint and exit on its own. If it does not exit within
        the window (for example because the active decision boundary is still
        unsafe), the pending reload is cancelled visibly and the current
        engine simply keeps running. Nothing here terminates or kills the
        child.
        """
        try:
            timeout_s = float(os.environ.get("MTGACOACH_SAFE_RELOAD_TIMEOUT_S", "120"))
        except ValueError:
            timeout_s = 120.0
        timeout_s = max(timeout_s, 5.0)
        self._reload_generation += 1
        generation = self._reload_generation

        def _deferred() -> None:
            self._safe_reload_deferred(generation)

        QTimer.singleShot(int(timeout_s * 1000), _deferred)

    def _safe_reload_deferred(self, generation: int) -> None:
        if generation != self._reload_generation or self._shutting_down:
            return
        if self._pending_restart is None or not self.is_running:
            return
        # Tell the engine to disarm: a late checkpoint/exit after the UI gave
        # up must never stop an engine the user believes is running.
        #
        # The pending restart is deliberately RETAINED here. Clearing it now
        # raced the engine's own completion: a cancellation arriving while
        # ``_complete_engine_reload`` was already committing left the UI
        # believing "cancelled" while the engine exited anyway, stranding the
        # handoff. The pending restart is released only on a definitive
        # disposition — the engine's ``engine_reload_cancelled``
        # acknowledgement, or its committed exit (reload_ready + process
        # exit) which proceeds with the restart.
        try:
            self.send_command("cancel_engine_reload")
        except Exception:  # pragma: no cover - best-effort signalling
            logger.warning("Could not send cancel_engine_reload to the engine")
        self._reload_disposition_pending = True
        reason = (
            "Engine did not reach its safe checkpoint in time; asking it to "
            "cancel — waiting for the engine's confirmation…"
        )
        logger.warning("Safe reload deferred: %s", reason)
        self.logEmitted.emit(reason, "error")
        try:
            grace_s = float(os.environ.get("MTGACOACH_SAFE_RELOAD_TIMEOUT_S", "120"))
        except ValueError:
            grace_s = 120.0
        grace_s = max(grace_s, 5.0)
        QTimer.singleShot(
            int(grace_s * 1000),
            lambda: self._reload_disposition_expired(generation),
        )

    def _reload_disposition_expired(self, generation: int) -> None:
        """Honest timeout: recovery responsibility is RETAINED, not dropped.

        The engine never acknowledged either disposition (cancel or commit)
        within a second full window. The pending restart and the awaited
        disposition stay armed so the session keeps responsibility for the
        outcome: a late ``engine_reload_cancelled`` acknowledgement releases
        the UI with the current engine still running, and a late committed
        exit (``reload_ready`` + process exit) proceeds with the restart.
        Only a definitive disposition — or shutdown/stop — ends the wait.
        """
        if generation != self._reload_generation or self._shutting_down:
            return
        if not getattr(self, "_reload_disposition_pending", False):
            return
        if self._pending_restart is None or not self.is_running:
            self._reload_disposition_pending = False
            return
        reason = (
            "Engine has not confirmed the reload cancellation; recovery stays "
            "armed — if the engine exits late the coach restarts automatically, "
            "and if it confirms cancellation it keeps running."
        )
        logger.warning("Safe reload unconfirmed after the grace window: %s", reason)
        self.logEmitted.emit(reason, "error")
        # Deliberately NOT "ready": claiming the current engine keeps running
        # here would be a promise the engine has not confirmed.
        self._set_startup_status({"phase": "reloading", "message": reason, "ready": False})

    def _finish_restart(self) -> None:
        options = self._pending_restart
        if options is None or self._shutting_down or self.is_running:
            return
        self._pending_restart = None
        # The committed handoff is being consumed; nothing is awaited anymore.
        self._reload_disposition_pending = False
        target = self._pending_snapshot_target
        self._pending_snapshot_target = None
        previous_identity = _selection_identity()
        switched = False
        if target is not None:
            target_root, target_commit, target_snapshot_root = target
            # Established launcher handoff: the new engine starts from the
            # adopted absolute versioned root. The whole selection identity
            # (APP_ROOT + SNAPSHOT_SHA + SNAPSHOT_ROOT) moves together so no
            # reader ever sees a new SHA paired with a stale root. The UI
            # itself keeps its launch-time code (old UI / new engine through
            # the declared protocol).
            _apply_selection_identity(target_root, target_commit, target_snapshot_root)
            switched = True
        if self.start(**options, engine_reload=True):
            if switched:
                self._arm_ready_watchdog(previous_identity)
            return
        if not switched:
            return
        # Adoption failed to spawn: roll back to the previous identity so the
        # current (previous) engine version stays usable.
        _warn_rollback(self, os.environ.get("MTGACOACH_SNAPSHOT_SHA", "?"), previous_identity)
        _apply_selection_identity(*previous_identity)
        if not self.start(**options, engine_reload=True):
            logger.error("Rollback start also failed; the coach is stopped.")
            self.logEmitted.emit(
                "Previous engine version also failed to start; start Coach manually.",
                "error",
            )

    def _arm_ready_watchdog(self, previous_identity: tuple[str | None, str | None, str | None]) -> None:
        """Require READY/handshake from an adopted engine, not just a spawn.

        If the adopted snapshot never becomes ready within the timeout the
        child is stopped and the previous identity is restarted; spawn-alone
        success is never treated as a successful adoption.
        """
        try:
            timeout_s = float(os.environ.get("MTGACOACH_READY_TIMEOUT_S", "90"))
        except ValueError:
            timeout_s = 90.0
        timeout_s = max(timeout_s, 5.0)
        self._adoption_generation += 1
        generation = self._adoption_generation
        adopted_sha = os.environ.get("MTGACOACH_SNAPSHOT_SHA", "")

        def _fire() -> None:
            self._ready_watchdog_fired(generation, adopted_sha, previous_identity)

        QTimer.singleShot(int(timeout_s * 1000), _fire)

    def _ready_watchdog_fired(
        self,
        generation: int,
        adopted_sha: str,
        previous_identity: tuple[str | None, str | None, str | None],
    ) -> None:
        if generation != self._adoption_generation or self._shutting_down:
            return
        status = self.last_startup_status or {}
        if status.get("ready"):
            return
        logger.warning("Adopted snapshot %s never became READY; rolling back", adopted_sha[:7] or "?")
        self.logEmitted.emit(
            "New engine did not become ready in time; rolling back to the previous version.",
            "error",
        )
        self._adoption_generation += 1
        if not self.is_running:
            _apply_selection_identity(*previous_identity)
            options = dict(self._last_start_options)
            if not self.start(**options, engine_reload=True):
                logger.error("Rollback start also failed; the coach is stopped.")
                self.logEmitted.emit(
                    "Previous engine version also failed to start; start Coach manually.",
                    "error",
                )
            return
        # Graceful stop first; the exited handler performs the rollback start
        # so two engines never control Arena at once.
        self._pending_rollback = (previous_identity, dict(self._last_start_options))
        self._process.stop_async()

    def _plan_snapshot_reload(self) -> Any:
        """Consult published snapshots once, at this safe checkpoint.

        Called only from the request_engine_reload boundary — after the engine
        signalled it is between matches — never mid-decision.
        """
        from .runtime_snapshots import (
            UI_PROTOCOL_VERSION,
            plan_engine_reload,
            read_pointer_once,
        )

        current = os.environ.get("MTGACOACH_SNAPSHOT_SHA", "").strip() or None
        try:
            pointer = read_pointer_once()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Snapshot lookup failed; reloading the current root: %s", exc)
            pointer = None
        # Judge compatibility against the RUNNING UI's declared protocol, not
        # the candidate pointer's self-declared UI version: an old UI may
        # adopt a newer engine only through the declared tested protocol pair.
        return plan_engine_reload(current, pointer, running_ui_protocol=UI_PROTOCOL_VERSION)

    def send_command(self, command: str, *args: Any) -> None:
        """Send a JSON command to the coach subprocess."""
        self._process.send_command(command, *args)

    def confirm_bug_report(self, request_id: str) -> None:
        """Tell the engine to publish the exact report the user reviewed."""
        self._process.send_payload({"cmd": "confirm_bugreport", "request_id": request_id})

    def cancel_bug_report(self, request_id: str) -> None:
        """Discard a pending public filing; the local report is kept."""
        self._process.send_payload({"cmd": "cancel_bugreport", "request_id": request_id})

    def request_deck_list(self) -> None:
        self._process.send_payload({"cmd": "list_decks"})

    def request_collection(self) -> None:
        self._process.send_payload({"cmd": "get_collection"})

    def request_update_deck(self, deck_id: str, piles: dict) -> None:
        self._process.send_payload({"cmd": "update_deck", "deck_id": deck_id, "piles": piles})

    def request_create_deck(self, source_deck_id: str, name: str) -> None:
        self._process.send_payload({"cmd": "create_deck", "source_deck_id": source_deck_id, "name": name})

    def request_craft(self, cards: list) -> None:
        # Only called after the Decks tab's confirmation dialog; the adapter
        # re-checks confirm=True before touching wildcards.
        self._process.send_payload({"cmd": "craft", "cards": cards})

    def analyze_deck(self, deck_id: str, *, refresh: bool = False) -> None:
        self._process.send_payload({"cmd": "analyze_deck", "deck_id": deck_id, "refresh": bool(refresh)})

    def analyze_all_decks(self) -> None:
        self._process.send_payload({"cmd": "analyze_all_decks"})

    def _report_speech_status(self, speech_id: str, state: str) -> None:
        """Tell the engine how an utterance it is waiting on ended (or started)."""
        if speech_id:
            self._process.send_payload({"cmd": "speech_status", "speech_id": speech_id, "state": state})

    def toggle_autopilot(self) -> None:
        self.send_command("toggle_autopilot")

    def configure_commentary(self, config: dict) -> None:
        self._speech.stop("mode_changed")
        self._speech.configure(config)
        self._process.send_payload({"cmd": "configure_commentary", "config": config})

    def set_speech_enabled(self, enabled: bool) -> None:
        self._muted = not enabled
        self._speech.arbiter.set_muted(self._muted)
        self._process.send_payload({"cmd": "sync_voice_preferences", "muted": self._muted})

    def preview_voice(self, options: dict | None = None) -> None:
        if options is None:
            from arenamcp.settings import get_settings

            settings = get_settings()
            options = {
                key: settings.get(key)
                for key in (
                    "tts_provider",
                    "autoplay_speech",
                    "commentary_persona",
                    "commentary_kokoro_pair",
                    "commentary_chatterbox_pair",
                    "voice",
                    "voice_speed",
                    "speech_speed",
                    "chatterbox_voice_override",
                    "chatterbox_speed",
                    "chatterbox_server_url",
                    "chatterbox_key_file",
                    "chatterbox_local_python",
                    "chatterbox_model_dir",
                    "chatterbox_model_revision",
                    "chatterbox_device",
                    "commentary_aoa_source",
                )
            }
        self._speech.preview(options)

    def set_speech_speed(self, speed: float) -> None:
        self._speech.stop("speed_changed")
        self._process.send_payload(
            {"cmd": "sync_voice_preferences", "voice_speed": speed, "speech_speed": speed}
        )

    def set_voice(self, voice_id: str) -> None:
        """Select a Kokoro voice by id in the running engine (saved there too)."""
        self._process.send_payload({"cmd": "sync_voice_preferences", "voice": str(voice_id)})

    def set_draft_commentary(self, enabled: bool) -> None:
        """Turn spoken draft-pick explanations on or off in the running engine."""
        self._process.send_payload({"cmd": "set_draft_commentary", "enabled": bool(enabled)})

    def set_draft_research_online(self, enabled: bool) -> None:
        """Let the set strategy research read published draft guides too."""
        self._process.send_payload({"cmd": "set_draft_research_online", "enabled": bool(enabled)})

    def set_guard_mode(self, guard: str, mode: str) -> None:
        """Set an autopilot guard ('line', 'attack_push', ...) to 'off', 'shadow' or 'on'."""
        self._process.send_payload({"cmd": "set_guard_mode", "guard": str(guard), "mode": str(mode)})

    def set_auto_concede(self, enabled: bool) -> None:
        """Choose whether autoplay concedes a game the board math says is lost."""
        self._process.send_payload({"cmd": "set_auto_concede", "enabled": bool(enabled)})

    def set_oops_emote(self, enabled: bool) -> None:
        """Choose whether autoplay sends Arena's "Oops" emote when it gets stuck or blunders."""
        self._process.send_payload({"cmd": "set_oops_emote", "enabled": bool(enabled)})

    def set_share_match_logs(self, enabled: bool) -> None:
        """Choose whether a redacted record of each finished match is uploaded to mtgacoach.com."""
        self._process.send_payload({"cmd": "set_share_match_logs", "enabled": bool(enabled)})

    def cancel_concede(self) -> None:
        """Stop a running auto-concede countdown and keep playing this game."""
        self._process.send_payload({"cmd": "cancel_concede"})

    def inspect_auto_quest(self) -> None:
        self._process.send_payload({"cmd": "inspect_auto_quest"})

    def set_auto_quest(
        self, enabled: bool, goal: str = "mastery", minutes: int = 60, deadline: float | None = None
    ) -> None:
        self._process.send_payload(
            {
                "cmd": "set_auto_quest",
                "enabled": enabled,
                "goal": goal,
                "minutes": minutes,
                "deadline": deadline,
            }
        )

    def set_auto_queue(self, enabled: bool) -> None:
        """Choose whether autoplay may repeat the recent queue after a match."""
        self._process.send_payload({"cmd": "set_auto_queue", "enabled": bool(enabled)})

    def toggle_mute(self) -> None:
        self.send_command("toggle_mute")

    def toggle_style(self) -> None:
        self.send_command("toggle_style")

    def cycle_speed(self) -> None:
        self.send_command("cycle_speed")

    def cycle_voice(self) -> None:
        self.send_command("cycle_voice")

    def stop_speaking(self) -> None:
        """Stop current TTS playback and tell the engine (``stop_speech``)."""
        self._speech.stop()
        self.send_command("stop_speech")

    def capture_screenshots(self) -> dict[str, str]:
        """Capture coach window + MTGA window screenshots into bug_reports directory."""
        from datetime import datetime
        from pathlib import Path

        from arenamcp.logging_config import LOG_DIR

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        try:
            bug_dir = Path(LOG_DIR) / "bug_reports"
        except Exception:
            bug_dir = Path.home() / ".arenamcp" / "logs" / "bug_reports"
        bug_dir.mkdir(parents=True, exist_ok=True)

        out: dict[str, str] = {}

        # 1. Coach PySide window screenshot
        try:
            from PySide6.QtWidgets import QApplication, QWidget

            active_win = QApplication.activeWindow()
            if active_win is None:
                parent = self.parent()
                if isinstance(parent, QWidget):
                    active_win = parent.window()
            if active_win is None:
                for w in QApplication.topLevelWidgets():
                    if w.isVisible():
                        active_win = w
                        break
            if active_win is not None:
                pix = active_win.grab()
                if not pix.isNull():
                    coach_path = bug_dir / f"bug_{ts}_coach.png"
                    if pix.save(str(coach_path), "PNG"):
                        out["coach"] = str(coach_path)
        except Exception as e:
            logger.debug("Coach screenshot failed: %s", e)

        # 2. MTGA window screenshot (only this user action may request access).
        from .screen_capture_permissions import ensure_screen_capture_permission

        if not ensure_screen_capture_permission(self.parent()):
            self.logEmitted.emit(
                "Arena screenshot omitted; the report can still be saved without screen recording access.",
                "info",
            )
            return out
        try:
            from arenamcp.autopilot_bug_capture import capture_mtga_screenshot

            out.update(capture_mtga_screenshot(bug_dir, f"bug_{ts}"))
        except PermissionError as e:
            self.logEmitted.emit(str(e), "error")
        except Exception as e:
            logger.debug("MTGA screenshot failed: %s", e)

        return out

    def trigger_autopilot_bug(self) -> None:
        """Pause/capture first, then attach Arena's screenshot for manual recovery."""
        from datetime import datetime, timezone
        from pathlib import Path
        from uuid import uuid4

        from arenamcp.autopilot_bug_capture import AutopilotBugCapture, capture_mtga_screenshot
        from arenamcp.logging_config import LOG_DIR

        if self._last_autopilot_bug_status.get("phase") in {"capturing", "recording"}:
            return
        clicked_at = datetime.now(timezone.utc)
        capture_id = clicked_at.strftime("%Y%m%d_%H%M%S_%f") + "_" + uuid4().hex[:8]
        self._speech.stop()
        self._set_autopilot_bug_status(
            {
                "phase": "capturing",
                "capture_id": capture_id,
                "message": "Capturing autopilot bug and requesting pause…",
            }
        )
        engine_running = self.is_running
        if engine_running:
            # Send before screenshot work so the engine snapshots its intent
            # and stops input promptly even if screen capture is slow.
            self._process.send_payload(
                {"cmd": "autopilot_bug", "capture_id": capture_id, "clicked_at": clicked_at.isoformat()}
            )
        screenshots: dict[str, str] = {}
        screenshot_error = ""
        try:
            from .screen_capture_permissions import ensure_screen_capture_permission

            if not ensure_screen_capture_permission(self.parent()):
                raise PermissionError("Arena screenshot skipped; screen recording access is needed.")
            screenshots = capture_mtga_screenshot(
                Path(LOG_DIR) / "bug_reports", f"autopilot_bug_{capture_id}"
            )
            if not screenshots:
                screenshot_error = "Arena window unavailable for screenshot"
        except Exception as exc:
            screenshot_error = str(exc)
            logger.debug("Autopilot bug screenshot failed: %s", exc)
        if screenshot_error:
            self.logEmitted.emit(f"Autopilot bug screenshot unavailable: {screenshot_error}", "error")
        if engine_running:
            self._process.send_payload(
                {
                    "cmd": "autopilot_bug_screenshots",
                    "capture_id": capture_id,
                    "screenshots": screenshots,
                    "screenshot_error": screenshot_error,
                }
            )
        else:
            try:
                recorder = AutopilotBugCapture(Path(LOG_DIR) / "bug_reports")
                path = recorder.begin(
                    capture_id,
                    {
                        "timestamp": clicked_at.isoformat(),
                        "game_state": self._last_game_state,
                        "game_state_source": "desktop_last_known_state",
                        "autopilot": {"available": False, "statuses": dict(self._statuses)},
                        "screenshots": screenshots,
                        "screenshot_error": screenshot_error,
                    },
                )
                recorder.finish("engine_unavailable")
                self.bugReportSaved.emit(str(path), "")
                self._set_autopilot_bug_status(
                    dict(
                        recorder.status,
                        message="Saved local screenshot and last known state. Engine unavailable; recovery recording could not start.",
                    )
                )
            except Exception as exc:
                self._set_autopilot_bug_status(
                    {
                        "phase": "error",
                        "capture_id": capture_id,
                        "message": f"Autopilot bug capture failed: {exc}",
                    }
                )

    def trigger_debug_report(self) -> None:
        """Capture screenshots and request bug report creation."""
        screenshots = self.capture_screenshots()
        if self.is_running:
            self._process.send_payload(
                {
                    "cmd": "debug_report",
                    "screenshots": screenshots,
                }
            )
        else:
            self._save_offline_debug_report(screenshots)

    def _save_offline_debug_report(self, screenshots: dict[str, str]) -> None:
        """Create a local debug report directly when coach subprocess is not running."""
        import json
        import platform
        from datetime import datetime
        from pathlib import Path

        from arenamcp import __version__
        from arenamcp.logging_config import LOG_DIR

        try:
            bug_dir = Path(LOG_DIR) / "bug_reports"
        except Exception:
            bug_dir = Path.home() / ".arenamcp" / "logs" / "bug_reports"
        bug_dir.mkdir(parents=True, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        bug_file = bug_dir / f"bug_{ts}.json"

        try:
            report = {
                "timestamp": datetime.now().isoformat(),
                "version": __version__,
                "reason": "Desktop Bug Report (offline)",
                "system": {
                    "platform": platform.platform(),
                    "python_version": platform.python_version(),
                    "machine": platform.machine(),
                },
                "game_state": self._last_game_state,
                "statuses": self._statuses,
                "screenshots": screenshots,
                "process_running": False,
            }
            with open(bug_file, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=2, default=str)
            self.bugReportSaved.emit(str(bug_file), "")
        except Exception as e:
            logger.exception("Offline bug report failed")
            self.bugReportSaved.emit("", str(e))

    def _handle_process_event(self, event: Any) -> None:
        """Decode pipe protocol JSON event from standalone coach."""
        if not isinstance(event, dict):
            return

        ev_type = str(event.get("type") or event.get("event") or "")
        if self._pending_restart is not None and ev_type in {
            "startup_status",
            "speak_request",
            "speak",
            "speak_audio",
            "advice",
            "emit_advice",
        }:
            # The outgoing engine can finish a model request while stopping.
            # Its advice/readiness is obsolete once a reload is requested.
            return

        if ev_type == "reload_ready":
            # The engine committed its handoff at the safe boundary. Record
            # it so the subsequent process exit is treated as a committed
            # adoption; an exit WITHOUT this announcement recovers the
            # current engine version instead (see _handle_exited).
            if self._pending_restart is not None and not self._shutting_down:
                self._reload_commit_seen = True
            return

        if ev_type == "engine_reload_cancelled":
            # Definitive disposition from the engine: it disarmed the reload
            # (acknowledged cancel/commit ordering guarantees it was not
            # already committing), so the current engine keeps running and
            # the retained pending restart is released. Only honoured while a
            # cancellation disposition is actually awaited — a stray or late
            # acknowledgement must never drop a committed handoff.
            if not getattr(self, "_reload_disposition_pending", False):
                return
            self._reload_disposition_pending = False
            if self._pending_restart is not None and not self._shutting_down:
                reason = "Engine reload cancelled — the current engine keeps running."
                logger.warning("%s", reason)
                self._pending_restart = None
                self._pending_snapshot_target = None
                self._reload_generation += 1
                self.logEmitted.emit(reason, "status")
                self._set_startup_status({"phase": "ready", "message": reason, "ready": True})
            return

        if ev_type == "startup_status":
            status = event.get("data", event)
            if isinstance(status, dict):
                self._set_startup_status(status)

        elif ev_type == "backend_health":
            # Pipe adapter sends flat state/detail/timestamp plus the full
            # snapshot under "data"; prefer the snapshot.
            snap = event.get("data")
            if not isinstance(snap, dict):
                snap = {k: v for k, v in event.items() if k != "type"}
            self._last_backend_health = dict(snap)
            self.backendHealthChanged.emit(dict(snap))

        elif ev_type == "autopilot_bug_status":
            status = event.get("data", event)
            if isinstance(status, dict):
                incoming_id = status.get("capture_id")
                current_id = self._last_autopilot_bug_status.get("capture_id")
                prior_finished = self._last_autopilot_bug_status.get("phase") in ("completed", "error")
                if not incoming_id or not current_id or incoming_id == current_id or prior_finished:
                    self._set_autopilot_bug_status(status)

        elif ev_type in ("game_state", "emit_game_state"):
            state = event.get("data") if "data" in event else event.get("game_state", {})
            if isinstance(state, dict):
                self._last_game_state = state
                self._speech.observe(state)
                self.gameStateChanged.emit(state)

        elif ev_type in ("turn_plan", "emit_turn_plan"):
            plan = event.get("data") if "data" in event else event.get("turn_plan")
            self.turnPlanChanged.emit(plan)

        elif ev_type in ("game_plan", "emit_game_plan"):
            plan = event.get("data") if "data" in event else event.get("game_plan")
            self.gamePlanChanged.emit(plan)

        elif ev_type == "concede_countdown":
            payload = event.get("data", event)
            if isinstance(payload, dict):
                self._last_concede = dict(payload)
                self.concedeCountdownChanged.emit(dict(payload))

        elif ev_type in ("status", "emit_status"):
            key = str(event.get("key") or "")
            val = str(event.get("value") or "")
            if key:
                self._statuses[key] = val
                if key == "AUTOPILOT":
                    self._autopilot_active = "ON" in val or "PAUSED" in val
                elif key == "MUTE":
                    self._muted = val.strip().lower() in {"on", "muted", "true"}
                    self._speech.arbiter.set_muted(self._muted)
                self.statusChanged.emit(key, val)

        elif ev_type == "speak_stop":
            # The engine stopped its voice output (stop command, urgent-topic
            # preemption, shutdown), so desktop audio must halt too.
            # Explicit Stop also suppresses opening retries in the engine command handler.
            self._speech.stop("engine_stop")

        elif ev_type in ("speak_request", "speak", "speak_audio"):
            self._speech.speech(event, muted=self._muted)

        elif ev_type == "commentary_proposal":
            self._speech.proposal(event.get("utterance") or {})
        elif ev_type == "commentary_clear":
            self._speech.arbiter.disable_commentary()
        elif ev_type == "commentary_config":
            self._speech.configure(event.get("config") or {})
        elif ev_type == "commentary_status":
            self.commentaryStatusChanged.emit(event.get("data") or {})

        elif ev_type in ("advice", "emit_advice"):
            text = str(event.get("text") or event.get("advice") or "")
            label = str(event.get("seat_info") or event.get("label") or "COACH")
            if text:
                self.adviceReceived.emit(text, label)

        elif ev_type in ("log", "emit_log"):
            msg = str(event.get("message") or event.get("log") or "")
            role = str(event.get("role") or "info")
            if msg:
                self.logEmitted.emit(msg, role)

        elif ev_type in ("mcts_tree", "emit_mcts"):
            payload = event.get("data") if "data" in event else event.get("mcts")
            self.mctsUpdated.emit(payload)

        elif ev_type in ("bug_report_saved", "emit_bug_report_saved"):
            path = str(event.get("path") or "")
            err = str(event.get("error") or "")
            self.bugReportSaved.emit(path, err)

        elif ev_type == "bug_report_confirmation_request":
            self.bugReportConfirmationRequested.emit(dict(event))

        elif ev_type in ("error", "emit_error"):
            err = str(event.get("message") or event.get("error") or "Unknown error")
            self.errorOccurred.emit(err)

        elif ev_type == "deck_list":
            decks = event.get("decks")
            self.deckListChanged.emit(
                list(decks) if isinstance(decks, list) else [], str(event.get("error") or "")
            )

        elif ev_type == "limited_deck_preview":
            if isinstance(event.get("data"), dict):
                self.limitedDeckPreviewChanged.emit(event["data"])

        elif ev_type == "deck_analysis":
            self.deckAnalysisChanged.emit({k: v for k, v in event.items() if k != "type"})

        elif ev_type == "collection":
            self.collectionChanged.emit({k: v for k, v in event.items() if k != "type"})

        elif ev_type == "deck_op":
            self.deckOpResult.emit({k: v for k, v in event.items() if k != "type"})

        elif ev_type == "request_engine_reload":
            # Sent between matches when the engine's code changed on disk
            # (standalone_auto_reload.py). A reload already under way, or a
            # closing app, takes precedence: never a second engine.
            reason = str(event.get("reason") or "The engine asked to reload")
            if self._pending_restart is None and not self._shutting_down and self.is_running:
                # Consult the published selection BEFORE announcing a reload:
                # an incompatible/unknown engine version must never fire the
                # reload signal -- it defers visibly to a full Restart Coach.
                plan = self._plan_snapshot_reload()
                if not self._gate_engine_reload_plan(plan):
                    return
                logger.info("Engine requested a reload: %s", reason)
                self.logEmitted.emit(f"{reason}; reloading the engine between matches.", "status")
                self.engineReloadRequested.emit(reason)
                # Same selection path as a manual Reload Engine: restart()
                # consults the published pointer once and adopts only a
                # compatible newer snapshot (or defers visibly).
                self.restart()
            else:
                logger.info(
                    "Ignored an engine reload request (%s): a reload or shutdown is under way", reason
                )

    def _handle_stderr(self, line: str) -> None:
        logger.debug(f"[coach stderr] {line}")
        if "Traceback" in line or "Error:" in line or "Exception:" in line:
            self.errorOccurred.emit(line)

    def _handle_exited(self, code: int) -> None:
        logger.info(f"Coach process exited with code {code}")
        # A process exit is a definitive disposition for any awaited reload
        # cancellation; the pending restart (if retained) drives what happens
        # next below.
        self._reload_disposition_pending = False
        rollback = self._pending_rollback
        if rollback is not None and not self._shutting_down:
            # A READY-watchdog rollback: previous identity back on before the
            # replacement engine starts.
            self._pending_rollback = None
            previous_identity, options = rollback
            _apply_selection_identity(*previous_identity)
            if not self.start(**options, engine_reload=True):
                logger.error("Rollback start also failed; the coach is stopped.")
                self.logEmitted.emit(
                    "Previous engine version also failed to start; start Coach manually.",
                    "error",
                )
            return
        if self._last_concede.get("state") in ("offering", "armed", "conceding", "sent"):
            # The engine's countdown died with it; take the banner down.
            self._last_concede = {"state": "aborted", "reason": "the coach stopped"}
            self.concedeCountdownChanged.emit(dict(self._last_concede))
        self.processExited.emit(code)
        self.stopped.emit()
        if self._last_autopilot_bug_status.get("phase") in {"capturing", "recording"}:
            self._set_autopilot_bug_status(
                dict(
                    self._last_autopilot_bug_status,
                    phase="error",
                    message="Engine stopped during capture. Any saved recovery snapshots remain in the local report folder.",
                )
            )
        if self._pending_restart is not None and not self._shutting_down:
            if not getattr(self, "_reload_commit_seen", False):
                # The engine exited without ever announcing the committed
                # handoff (no reload_ready): this is not an adoption. Drop
                # the snapshot target so the recovery restart brings the
                # CURRENT engine version back up instead of adopting a
                # snapshot the dying engine never confirmed.
                self._pending_snapshot_target = None
            self._reload_commit_seen = False
            # Defer until QProcess.finished has returned and cleaned up the
            # old child; never allow two engines to control Arena at once.
            QTimer.singleShot(0, self._finish_restart)
        else:
            self._set_startup_status(
                {
                    "phase": "stopped" if code == 0 or self._shutting_down else "error",
                    "message": "Coach stopped."
                    if code == 0 or self._shutting_down
                    else f"Coach exited (code {code}).",
                    "ready": False,
                }
            )

    def shutdown(self) -> None:
        """Cleanly terminate subprocess and TTS manager."""
        self._shutting_down = True
        self._pending_restart = None
        self._reload_disposition_pending = False
        self._reload_commit_seen = False
        self._reload_generation += 1
        self._speech.close()
        if self._tts is not None:
            self._tts.shutdown()
        self.stop()
