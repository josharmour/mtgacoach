"""Autonomous real-match data collection for MTGA "Play vs AI".

Drives the vLLM model through real vs-AI matches using the EXISTING autopilot
decision path (``AutopilotEngine`` + ``ActionPlanner`` + GRE bridge), recording
every decision as a training trajectory via :class:`TrajectoryRecorder`. The
emitted JSONL matches ``arenamcp.self_play`` so ``tools/training/build_dataset.py``
consumes real-match data the same way as self-play data.

Flow (per the bridge-authoritative product direction):
  1. Preflight the vLLM backend (GET ``<base_url>/models``).
  2. Ensure MTGA is running (launch via ``arenamcp.proton_launch`` if needed).
  3. Own the GRE bridge server on 44222 and wait for the BepInEx plugin.
  4. Send ``start_practice_match`` and wait for ``ok:true`` (you must be on the
     Home screen — the plugin's error text is surfaced clearly).
  5. Drive the match through the autopilot, recording each decision.
  6. On match end, flush the trajectory with the winner; loop for --matches N.

This module is wiring + recording only. All MTG logic lives in the reused
autopilot/planner/bridge code; nothing here reimplements gameplay.

Usage::

    python -m arenamcp.play_real_matches --matches 5
    python -m arenamcp.play_real_matches --dry-run
    python -m arenamcp.play_real_matches --deck "Mono Red" --no-launch
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import Any, Optional

# Ensure in-repo src and repo root are importable (mirrors self_play.py).
REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

# Reuse the self-play helpers wholesale (preflight, MTGA launch/discovery,
# port check, bridge port constant) so behaviour stays consistent.
from arenamcp import self_play
from arenamcp.self_play import (
    GRE_BRIDGE_PORT,
    DEFAULT_BACKEND,
    _backend_base_url,
    _port_bindable,
    ensure_mtga_running,
    find_mtga_install,
    mtga_is_running,
    vllm_preflight,
)
from arenamcp.trajectory_recorder import (
    DEFAULT_TRAJECTORY_PATH,
    TrajectoryRecorder,
    normalize_winner,
)

logger = logging.getLogger("arenamcp.play_real_matches")


# ---------------------------------------------------------------------------
# Dry-run readiness check
# ---------------------------------------------------------------------------

def run_dry_run(backend_spec: str) -> int:
    """Preflight readiness WITHOUT launching MTGA or starting a match.

    Reports: vLLM reachability (:8003), MTGA install found, and GRE bridge
    port 44222 status. Returns a process exit code (0 = ready).
    """
    logger.info("=== play-real readiness check (--dry-run) ===")
    ok = True

    # 1. vLLM reachability for the openai-compatible base_url (if any).
    base = _backend_base_url(backend_spec)
    if base:
        results = vllm_preflight([base])
        good, detail = results.get(base, (False, "no result"))
        if good:
            logger.info("vLLM reachable: %s -> models=%s", base, detail)
        else:
            logger.error("vLLM unreachable: %s (%s)", base, detail)
            ok = False
    else:
        logger.info("Backend %s is not openai-compatible; no vLLM preflight.", backend_spec)

    # 2. MTGA install location (informational + readiness).
    install, source = find_mtga_install()
    if install:
        logger.info("MTGA install: FOUND (%s) -> %s", source, install)
    else:
        logger.error("MTGA install: NOT FOUND")
        ok = False

    # 3. MTGA process state (informational only).
    logger.info("MTGA process running: %s", mtga_is_running())

    # 4. GRE bridge port availability (do NOT bind permanently).
    if _port_bindable(GRE_BRIDGE_PORT):
        logger.info("GRE bridge port %d: bindable (free)", GRE_BRIDGE_PORT)
    else:
        logger.error(
            "GRE bridge port %d: NOT bindable (in use). Stop the desktop "
            "coach/bridge before running.",
            GRE_BRIDGE_PORT,
        )
        ok = False

    if ok:
        logger.info("=== readiness: OK ===")
        return 0
    logger.error("=== readiness: NOT READY ===")
    return 1


# ---------------------------------------------------------------------------
# Autopilot construction (mirrors StandaloneCoach._init_autopilot, headless)
# ---------------------------------------------------------------------------

def _build_autopilot(backend_spec: str, recorder: TrajectoryRecorder, license_key: str):
    """Construct an AutopilotEngine wired to the real game-state pipeline.

    Reuses ``server.get_game_state`` (the authoritative bridge+log snapshot) as
    the state source — exactly what the desktop autopilot uses — so no MTG
    logic is reimplemented here.
    """
    from tools.eval.run import BackendSpec
    from arenamcp.action_planner import ActionPlanner
    from arenamcp.autopilot import AutopilotConfig, AutopilotEngine
    from arenamcp.input_controller import InputController
    from arenamcp.screen_mapper import ScreenMapper
    from arenamcp import server

    # Start the in-process log watcher so server.get_game_state() is live.
    server.start_watching()

    spec = BackendSpec.parse(backend_spec, license_key)
    backend = spec.build()

    config = AutopilotConfig(
        dry_run=False,
        afk_mode=False,
        enable_tts_preview=False,  # headless data collection: no voice
    )
    planner = ActionPlanner(
        backend,
        timeout=config.planning_timeout,
        land_drop_first=config.land_drop_first,
    )
    mapper = ScreenMapper()
    controller = InputController(dry_run=False)

    engine = AutopilotEngine(
        planner=planner,
        mapper=mapper,
        controller=controller,
        get_game_state=server.get_game_state,
        config=config,
    )
    # Attach the recorder — this is the single integration point that turns on
    # trajectory capture in the autopilot's planning path.
    engine._trajectory_recorder = recorder
    # Label the recorder with the resolved backend so build_dataset can tell
    # which model produced the moves.
    recorder.backend_label = getattr(spec, "label", backend_spec)
    return engine, server


# ---------------------------------------------------------------------------
# Match orchestration
# ---------------------------------------------------------------------------

def _start_practice_match(bridge, deck_name: Optional[str], attempts: int = 30) -> bool:
    """Send ``start_practice_match`` and wait for ``ok:true`` (with retries).

    The user must be on the MTGA Home screen. Surfaces the plugin's error text
    so the operator knows why a start failed (e.g. "not on home screen").
    """
    cmd: dict[str, Any] = {"action": "start_practice_match"}
    if deck_name:
        cmd["deck_name"] = deck_name

    last_err = "no response"
    for attempt in range(1, attempts + 1):
        resp = bridge._send_safe(cmd, timeout=15.0)
        if resp and resp.get("ok"):
            logger.info(
                "start_practice_match OK: deck=%s (id=%s) event=%s",
                resp.get("deck_name"), resp.get("deck_id"), resp.get("event"),
            )
            return True
        last_err = (resp or {}).get("error") if resp else "timeout/no response"
        logger.info(
            "start_practice_match attempt %d/%d failed: %s "
            "(make sure MTGA is on the Home screen). Retrying in 2s...",
            attempt, attempts, last_err,
        )
        time.sleep(2.0)

    logger.error("start_practice_match failed after %d attempts: %s", attempts, last_err)
    return False


def _wait_for_match_active(server_mod, poller, timeout: float = 90.0) -> bool:
    """Wait until the GRE bridge starts presenting in-match decisions."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        trig = poller.poll()
        if trig and trig.get("has_pending"):
            return True
        try:
            state = server_mod.get_game_state()
            if (state.get("turn", {}) or {}).get("turn_number", 0) > 0:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def _drive_match(engine, server_mod, poller, poll_interval: float = 0.4) -> Optional[str]:
    """Drive a single match through the autopilot until it ends.

    Returns the raw game result string ("win"/"loss"/None) consumed from the
    GameState game-end signal. The autopilot records each decision via the
    recorder attached in ``_build_autopilot``.
    """
    gs = getattr(server_mod, "game_state", None)
    last_fire = 0.0  # monotonic time of the last process_trigger call

    while True:
        # Match-end detection: the authoritative game-end signal (same path
        # the desktop coach consumes). Clears itself so the next match starts
        # clean.
        if gs is not None and gs.game_ended_event.is_set():
            result, _snap = gs.consume_game_end()
            logger.info("Match ended (result=%s)", result)
            return result

        try:
            trigger = poller.poll()
        except Exception as e:
            logger.debug("poller error (ignored): %s", e)
            trigger = None

        fire = bool(trigger and trigger.get("trigger") == "decision_required")

        # Stale-discard / latency recovery: the poller only emits a fresh
        # "decision_required" when the decision *changes*. If a plan went stale
        # at a turn transition (the autopilot discarded it and went idle) the
        # same decision is still pending but unchanged, so the poller stays
        # quiet and the bot sits forever. Re-fire process_trigger whenever a
        # bridge decision is still pending and we haven't fired recently — on
        # the player's own turn the state is stable, so the re-plan executes.
        # process_trigger is synchronous, so this never overlaps a running plan.
        if not fire:
            try:
                snap = server_mod.get_game_state()
                breq = snap.get("_bridge_request_type") or snap.get("_bridge_request_class") or ""
                pending = (
                    bool(breq)
                    and not snap.get("_bridge_in_intermission")
                    and not snap.get("match_ended")
                )
                if pending and (time.monotonic() - last_fire) > 2.0:
                    fire = True
            except Exception:
                pass

        if fire:
            try:
                state = server_mod.get_game_state()
                # Belt-and-suspenders: surface intermission/match-end as end.
                if state.get("_bridge_in_intermission") or state.get("match_ended"):
                    if gs is not None and gs.game_ended_event.is_set():
                        result, _snap = gs.consume_game_end()
                        return result
                poller.enrich_snapshot(state)
                last_fire = time.monotonic()
                engine.process_trigger(state, "decision_required")
            except Exception as e:
                logger.warning("process_trigger failed (continuing): %s", e)

        time.sleep(poll_interval)


def run_matches(
    backend_spec: str,
    matches: int,
    deck_name: Optional[str],
    out_path: Path,
    license_key: str,
    launch: bool,
    attach: bool = False,
) -> int:
    """Full autonomous run: preflight, bridge, N matches, clean shutdown.

    When ``attach`` is True, do not launch MTGA or start a new match — just
    connect to the bridge and play/record the match that is already in progress
    (a single match).
    """
    # 1. vLLM preflight (informational; we still try even if it warns).
    base = _backend_base_url(backend_spec)
    if base:
        vllm_preflight([base])

    # 2. Ensure MTGA is running.
    if attach:
        if not mtga_is_running():
            logger.error("--attach given but MTGA is not running. Aborting.")
            return 5
        matches = 1  # attach plays only the current in-progress match
    elif launch:
        ensure_mtga_running()
    elif not mtga_is_running():
        logger.error("MTGA is not running and --no-launch was given. Aborting.")
        return 5

    # 3. Build recorder + autopilot (also starts the log watcher).
    recorder = TrajectoryRecorder(out_path, seat="local")
    try:
        engine, server_mod = _build_autopilot(backend_spec, recorder, license_key)
    except Exception as e:
        logger.error("Failed to build autopilot: %s", e, exc_info=True)
        return 6

    # 4. Own the GRE bridge and wait for the plugin to connect (~180s).
    from arenamcp.gre_bridge import BridgeDecisionPoller, get_bridge

    bridge = get_bridge()
    logger.info("Waiting for MTGA BepInEx plugin to connect on port %d...", GRE_BRIDGE_PORT)
    connected = False
    max_wait = 180
    for i in range(max_wait):
        if bridge.connected or bridge.connect():
            connected = True
            break
        if i % 15 == 0:
            logger.info("waiting for MTGA + bridge plugin... (%d/%ds)", i, max_wait)
        time.sleep(1.0)
    if not connected:
        logger.error(
            "Could not connect to MTGA BepInEx plugin. Ensure MTGA is running "
            "with the bridge plugin installed."
        )
        return 2

    poller = BridgeDecisionPoller(bridge)

    # 5. Play N matches.
    completed = 0
    try:
        for m in range(1, matches + 1):
            logger.info("=== Starting match %d of %d ===", m, matches)
            poller.reset()

            if attach:
                logger.info(
                    "--attach: playing the match already in progress "
                    "(not starting a new one)."
                )
            elif not _start_practice_match(bridge, deck_name):
                logger.error("Could not start match %d; stopping run.", m)
                break

            if not _wait_for_match_active(server_mod, poller, timeout=120.0):
                logger.warning(
                    "Match %d did not become active within timeout; "
                    "discarding and stopping.", m,
                )
                recorder.discard_match()
                break

            result = _drive_match(engine, server_mod, poller)
            winner = normalize_winner(result, seat="local")
            n = recorder.flush_match(winner)
            completed += 1
            logger.info(
                "Match %d complete: result=%s winner=%s decisions=%d",
                m, result, winner, n,
            )
    except KeyboardInterrupt:
        logger.warning("Interrupted; flushing current match buffer.")
        recorder.flush_match(None)
    finally:
        # 6. Clean shutdown.
        try:
            bridge.stop_keepalive()
        except Exception:
            pass
        try:
            bridge.disconnect()
        except Exception:
            pass

    logger.info(
        "Run complete: %d/%d matches, %d decisions recorded -> %s",
        completed, matches, recorder.total_flushed, out_path,
    )
    return 0


def main() -> None:
    p = argparse.ArgumentParser(
        description="Autonomous real-match data collection for MTGA Play vs AI.",
    )
    p.add_argument("--matches", type=int, default=1, help="Number of matches to play.")
    p.add_argument("--deck", default=None, help="Optional deck name to pass to start_practice_match.")
    p.add_argument(
        "--out", type=Path, default=DEFAULT_TRAJECTORY_PATH,
        help=f"Output trajectory JSONL (default: {DEFAULT_TRAJECTORY_PATH}).",
    )
    p.add_argument(
        "--backend", default=DEFAULT_BACKEND,
        help=f"Backend spec for the playing model (default: {DEFAULT_BACKEND}).",
    )
    import os
    p.add_argument("--license-key", default=os.environ.get("MTGACOACH_LICENSE_KEY", ""))
    p.add_argument(
        "--launch-mtga", dest="launch", action="store_true", default=True,
        help="Launch MTGA via proton_launch if not running (default).",
    )
    p.add_argument(
        "--no-launch", dest="launch", action="store_false",
        help="Do NOT launch MTGA; require it to already be running.",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Preflight only: vLLM reachable + MTGA found + port check. "
             "Never launches MTGA or starts a match.",
    )
    p.add_argument(
        "--attach", action="store_true",
        help="Play/record the match already in progress instead of starting "
             "one (implies --no-launch, single match).",
    )
    args = p.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    if args.dry_run:
        sys.exit(run_dry_run(args.backend))

    sys.exit(
        run_matches(
            backend_spec=args.backend,
            matches=args.matches,
            deck_name=args.deck,
            out_path=args.out,
            license_key=args.license_key,
            launch=args.launch,
            attach=args.attach,
        )
    )


if __name__ == "__main__":
    main()
