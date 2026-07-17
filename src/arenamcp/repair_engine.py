"""One-pass check-and-repair engine behind the simplified Repair tab.

Design (2026-07-06 redesign): the user sees ONE action — "Check & Repair" —
which walks every known failure mode in dependency order, fixes silently
where safe, and reports a plain checklist. Each row is ok / fixed /
action_needed (with exactly one sentence of what to do) / error.

The check list is the verified failure catalog from the repair audit
(~/Desktop/repair-audit-20260706.md): file existence alone proves nothing,
so every check here verifies FUNCTION where possible — the license against
the gateway, the plugin against the packaged DLL's bytes, BepInEx's
doorstop loader files, the Proton launch options that gate injection, and
the freshness of Player.log.

GUI-free by design: unit-testable, reusable from a CLI (`mtgacoach
--repair`) so a broken GUI never strands the user (audit blocker #2).
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

ProgressCb = Optional[Callable[[str], None]]

# MTGA prints this marker into Player.log at startup on every platform.
# When DISABLED, the log carries zero GRE data and the coach is blind.
_DETAILED_LOGS_RE = re.compile(rb"DETAILED LOGS:\s*(ENABLED|DISABLED)")
# The marker sits near the top of the log; scan the head plus a generous
# tail so a mid-session re-toggle (or format drift) is still caught.
_MARKER_SCAN_BYTES = 200 * 1024


@dataclass
class CheckResult:
    key: str
    label: str
    status: str  # "ok" | "fixed" | "action_needed" | "error"
    detail: str
    action_hint: str = ""


@dataclass
class RepairReport:
    results: list[CheckResult] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return all(r.status in ("ok", "fixed") for r in self.results)

    @property
    def needs_user(self) -> list[CheckResult]:
        return [r for r in self.results if r.status == "action_needed"]

    def summary(self) -> str:
        fixed = sum(1 for r in self.results if r.status == "fixed")
        broken = [r for r in self.results if r.status in ("action_needed", "error")]
        if not broken:
            return (
                f"Everything checks out ({fixed} thing(s) repaired)."
                if fixed
                else "Everything checks out."
            )
        return (
            f"{len(broken)} item(s) need attention"
            + (f", {fixed} repaired automatically" if fixed else "")
            + "."
        )


def _file_hash(path: Path) -> Optional[str]:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


class RepairEngine:
    """Runs the ordered check/fix pipeline. Construct fresh per run."""

    def __init__(self) -> None:
        self._install = None  # populated by _check_mtga_install

    # ------------------------------------------------------------------
    def run(self, progress: ProgressCb = None) -> RepairReport:
        report = RepairReport()
        checks = [
            self._check_python_runtime,
            self._check_settings,
            self._check_license,
            self._check_mtga_install,
            self._check_player_log,
            self._check_detailed_logs,
            self._check_bepinex,
            self._check_plugin,
            self._check_launch_options,
            self._check_bridge_signal,
        ]
        for check in checks:
            name = check.__name__.replace("_check_", "")
            if progress:
                progress(name)
            try:
                result = check()
            except Exception as e:
                logger.exception(f"repair check {name} crashed")
                result = CheckResult(
                    key=name,
                    label=name.replace("_", " ").title(),
                    status="error",
                    detail=f"Check crashed: {e}",
                )
            if result is not None:
                report.results.append(result)
        return report

    # ------------------------------------------------------------------
    @staticmethod
    def _is_native_mac(install) -> bool:
        """True for the native macOS MTGA client (IL2CPP build).

        The native client has no CLR, so BepInEx/the bridge can NEVER load
        into it — only a Wine/CrossOver bottle running the Windows Mono
        build can host the plugin (docs/PLATFORM_PARITY.md §B1). Written
        defensively against the exact platform tag: anything darwin that
        is not explicitly a wine-style bottle counts as native.
        """
        plat = (getattr(install, "platform", "") or "").lower()
        if not plat.startswith("darwin"):
            return False
        return not any(t in plat for t in ("wine", "crossover", "bottle", "proton"))

    @staticmethod
    def _bridge_not_applicable(key: str, label: str) -> CheckResult:
        return CheckResult(
            key, label, "ok",
            "Not applicable: native Mac client cannot host the bridge "
            "(coaching works from the log; autopilot requires the Windows "
            "build under Wine/CrossOver — see docs/PLATFORM_PARITY.md).",
        )

    # ------------------------------------------------------------------
    def _check_python_runtime(self) -> CheckResult:
        import sys

        missing = []
        for mod in ("PySide6", "openai", "watchdog", "mcp"):
            try:
                __import__(mod)
            except Exception:
                missing.append(mod)
        if missing:
            return CheckResult(
                "python_runtime", "Python runtime", "action_needed",
                f"Missing packages: {', '.join(missing)}.",
                "Reinstall the app: pip install --force-reinstall arenamcp",
            )
        v = sys.version_info
        return CheckResult(
            "python_runtime", "Python runtime", "ok",
            f"Python {v.major}.{v.minor}.{v.micro}, all core packages import.",
        )

    def _check_settings(self) -> CheckResult:
        from arenamcp.settings import SETTINGS_FILE

        bad = SETTINGS_FILE.with_suffix(".json.bad")
        if bad.exists():
            return CheckResult(
                "settings", "Settings", "action_needed",
                "A corrupt settings file was found and preserved; current "
                "settings were reset to defaults.",
                "Re-enter your license key below, then delete "
                f"{bad.name} once you're happy.",
            )
        return CheckResult(
            "settings", "Settings", "ok", "Settings file loads cleanly.",
        )

    def _check_license(self) -> CheckResult:
        """Validate the key against the gateway — not just file presence.

        Audit blocker #3: the app went online-only and repair never noticed
        a missing/expired key; 'Fix Everything' reported a fully provisioned
        app that couldn't serve a single completion.
        """
        from arenamcp.settings import get_settings

        trial_note = ""
        key = (get_settings().get("license_key") or "").strip()
        if not key:
            # First-run: auto-provision a free 7-day trial key instead of
            # stranding the user at a key prompt (product decision
            # 2026-07-16, docs/DECISIONS.md). Falls through to the normal
            # gateway validation below when a key is obtained.
            try:
                from arenamcp.subscription import SUBSCRIBE_URL, ensure_license_key

                trial = ensure_license_key()
            except Exception as e:
                trial = {"status": "error", "message": str(e)}
            if trial.get("key"):
                key = trial["key"]
                trial_note = (
                    f" (free trial until {trial.get('expires_at', 'unknown')[:10]})"
                    if trial.get("expires_at") else " (free trial)"
                )
            elif trial.get("status") == "trial_expired":
                return CheckResult(
                    "license", "License / online backend", "action_needed",
                    "Your free trial has ended.",
                    f"Subscribe at {SUBSCRIBE_URL} to keep coaching, or "
                    "enter a license key below.",
                )
            else:
                return CheckResult(
                    "license", "License / online backend", "action_needed",
                    "No license key is configured and a free trial could "
                    f"not be started ({trial.get('message') or trial.get('status')}).",
                    "Check your internet connection and press Fix Everything "
                    "again, or enter a license key below.",
                )
        try:
            import urllib.request

            from arenamcp import __version__

            req = urllib.request.Request(
                "https://api.mtgacoach.com/v1/models",
                headers={
                    "Authorization": f"Bearer {key}",
                    # Cloudflare 403s the default Python-urllib agent —
                    # verified live 2026-07-06 (curl 200, urllib 403).
                    "User-Agent": f"mtgacoach/{__version__}",
                },
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                if resp.status == 200:
                    # Audit #24: month-old clients ride gateway aliases that
                    # have already misrouted once — verify the configured
                    # model is actually served.
                    try:
                        import json as _json

                        served = {
                            m.get("id")
                            for m in _json.load(resp).get("data", [])
                        }
                        configured = get_settings().get("model")
                        if configured and served and configured not in served:
                            get_settings().set("model", None)
                            return CheckResult(
                                "license", "License / online backend",
                                "fixed",
                                f"License OK, but your saved model "
                                f"'{configured}' is no longer served — "
                                "reset to the server default.",
                            )
                    except Exception:
                        pass
                    return CheckResult(
                        "license", "License / online backend", "ok",
                        f"License key accepted by the gateway{trial_note}.",
                    )
                status = resp.status
        except urllib.error.HTTPError as e:  # type: ignore[attr-defined]
            if e.code in (401, 403):
                if (get_settings().get("trial_expires_at") or "").strip():
                    return CheckResult(
                        "license", "License / online backend", "action_needed",
                        "Your free trial key was rejected by the gateway — "
                        "the trial has most likely ended.",
                        "Subscribe at https://mtgacoach.com/subscribe to keep "
                        "coaching, or enter a license key below.",
                    )
                return CheckResult(
                    "license", "License / online backend", "action_needed",
                    "The gateway rejected your license key (expired or wrong).",
                    "Enter a valid license key below.",
                )
            status = e.code
        except Exception as e:
            return CheckResult(
                "license", "License / online backend", "error",
                f"Could not reach api.mtgacoach.com ({e}) — check your "
                "internet connection; the service may also be down.",
            )
        return CheckResult(
            "license", "License / online backend", "error",
            f"Unexpected gateway response ({status}).",
        )

    def _check_mtga_install(self) -> CheckResult:
        from arenamcp.platform_integration import find_mtga
        from arenamcp.settings import get_settings

        install = find_mtga()
        self._install = install
        if install is None:
            return CheckResult(
                "mtga", "MTGA installation", "action_needed",
                "Could not find MTGA on this machine.",
                "Install MTGA (the official installer on Windows, Steam on "
                "Linux or macOS), or set its folder in Settings.",
            )
        # Persist the detection so every other component agrees (fix).
        settings = get_settings()
        saved = settings.get("mtga_install_dir")
        if saved != str(install.install_dir):
            settings.set("mtga_install_dir", str(install.install_dir))
            settings.save()
            return CheckResult(
                "mtga", "MTGA installation", "fixed",
                f"Found MTGA ({install.platform}) at {install.install_dir} "
                "and saved it to settings.",
            )
        return CheckResult(
            "mtga", "MTGA installation", "ok",
            f"MTGA found ({install.platform}).",
        )

    def _check_player_log(self) -> CheckResult:
        install = self._install
        if install is None:
            return CheckResult(
                "player_log", "MTGA game log", "error",
                "Skipped — MTGA was not found.",
            )
        log = install.player_log
        if log is None or not log.exists():
            return CheckResult(
                "player_log", "MTGA game log", "action_needed",
                "Player.log was not found — MTGA's detailed logging is "
                "probably off (or MTGA has never been launched).",
                "In MTGA: Options → Account → enable 'Detailed Logs "
                "(Plugin Support)', then restart MTGA.",
            )
        age_days = (time.time() - log.stat().st_mtime) / 86400.0
        if age_days > 14:
            return CheckResult(
                "player_log", "MTGA game log", "action_needed",
                f"Player.log exists but is {age_days:.0f} days old.",
                "Launch MTGA once so the log refreshes; if it stays stale, "
                "re-enable 'Detailed Logs (Plugin Support)' in MTGA options.",
            )
        return CheckResult(
            "player_log", "MTGA game log", "ok",
            "Player.log present and recent.",
        )

    def _check_detailed_logs(self) -> Optional[CheckResult]:
        """The #1 silent-blindness cause on EVERY platform.

        MTGA writes a 'DETAILED LOGS: ENABLED/DISABLED' marker into
        Player.log at startup. When the in-game toggle is off the log
        contains no GRE data at all, so the entire coach sees nothing —
        and until now nothing surfaced that to the user. Verified live:
        disabled logs literally contain 'DETAILED LOGS: DISABLED'.
        """
        install = self._install
        log = install.player_log if install is not None else None
        if log is None or not log.exists():
            # _check_player_log already reported the missing log with the
            # same enable-detailed-logs hint; don't duplicate the row.
            return None
        try:
            size = log.stat().st_size
            with open(log, "rb") as f:
                head = f.read(_MARKER_SCAN_BYTES)
                tail = b""
                if size > _MARKER_SCAN_BYTES:
                    f.seek(size - _MARKER_SCAN_BYTES)
                    tail = f.read(_MARKER_SCAN_BYTES)
        except OSError as e:
            return CheckResult(
                "detailed_logs", "Detailed logs (plugin support)", "error",
                f"Cannot read Player.log: {e}",
            )
        # Newest marker wins (head matches first, then tail — a marker in
        # the tail is by definition newer than one in the head).
        markers = _DETAILED_LOGS_RE.findall(head) + _DETAILED_LOGS_RE.findall(tail)
        if not markers:
            return CheckResult(
                "detailed_logs", "Detailed logs (plugin support)", "ok",
                "Player.log has no detailed-logs marker in the scanned "
                "region — could not verify, assuming enabled.",
            )
        if markers[-1] == b"DISABLED":
            return CheckResult(
                "detailed_logs", "Detailed logs (plugin support)",
                "action_needed",
                "MTGA's detailed logging is OFF — Player.log contains no "
                "game data, so the coach cannot see your matches.",
                "Enable Options → Account → Detailed Logs (Plugin Support) "
                "in MTGA, then restart MTGA.",
            )
        return CheckResult(
            "detailed_logs", "Detailed logs (plugin support)", "ok",
            "Detailed logs are enabled — Player.log carries full game data.",
        )

    def _check_bepinex(self) -> Optional[CheckResult]:
        install = self._install
        if install is None:
            return None
        if self._is_native_mac(install):
            return self._bridge_not_applicable("bepinex", "BepInEx loader")
        from arenamcp.desktop import runtime as _runtime

        mtga_dir = install.install_dir
        core = mtga_dir / "BepInEx" / "core" / "BepInEx.dll"
        # Audit gap #8: the doorstop loader is what antivirus quarantines;
        # its absence leaves every DLL-existence check green while nothing
        # injects.
        doorstop = mtga_dir / "winhttp.dll"
        missing = [p.name for p in (core, doorstop) if not p.exists()]
        if missing:
            try:
                _runtime.install_bepinex(str(mtga_dir))
                still = [p.name for p in (core, doorstop) if not p.exists()]
                if not still:
                    return CheckResult(
                        "bepinex", "BepInEx loader", "fixed",
                        f"Reinstalled missing BepInEx files ({', '.join(missing)}).",
                    )
                return CheckResult(
                    "bepinex", "BepInEx loader", "action_needed",
                    f"Missing after reinstall: {', '.join(still)} — your "
                    "antivirus may be quarantining the loader (winhttp.dll).",
                    "Add an antivirus exclusion for the MTGA folder, then "
                    "run Check & Repair again.",
                )
            except Exception as e:
                return CheckResult(
                    "bepinex", "BepInEx loader", "error",
                    f"Missing {', '.join(missing)}; reinstall failed: {e}",
                )
        return CheckResult(
            "bepinex", "BepInEx loader", "ok",
            "BepInEx core and doorstop loader present.",
        )

    def _check_plugin(self) -> Optional[CheckResult]:
        install = self._install
        if install is None:
            return None
        if self._is_native_mac(install):
            return self._bridge_not_applicable("plugin", "Bridge plugin")
        from arenamcp.desktop import runtime as _runtime

        packaged = _runtime.find_plugin_dll()
        deployed = install.install_dir / "BepInEx" / "plugins" / "MtgaCoachBridge.dll"
        if packaged is None:
            return CheckResult(
                "plugin", "Bridge plugin", "error",
                "This app package is missing its bundled plugin DLL — "
                "reinstall the app (pip install --force-reinstall arenamcp).",
            )
        # Audit gap #7: existence is not enough — a v2.4-era DLL speaks an
        # old protocol and reintroduces fixed bugs. Byte-compare against
        # the DLL this app version ships.
        if deployed.exists() and _file_hash(deployed) == _file_hash(packaged):
            return CheckResult(
                "plugin", "Bridge plugin", "ok",
                "Deployed plugin matches this app version.",
            )
        stale = deployed.exists()
        if _runtime.is_mtga_running():
            return CheckResult(
                "plugin", "Bridge plugin", "action_needed",
                "The deployed plugin is outdated." if stale
                else "The bridge plugin is not installed.",
                "Close MTGA, run Check & Repair again (the plugin installs "
                "automatically), then start MTGA.",
            )
        try:
            _runtime.install_plugin(str(install.install_dir))
            return CheckResult(
                "plugin", "Bridge plugin", "fixed",
                ("Updated the outdated plugin" if stale else "Installed the plugin")
                + " — it loads next time MTGA starts.",
            )
        except Exception as e:
            return CheckResult(
                "plugin", "Bridge plugin", "error", f"Install failed: {e}",
            )

    def _check_launch_options(self) -> Optional[CheckResult]:
        install = self._install
        if install is None or not install.platform.startswith("linux"):
            return None
        from arenamcp.platform_integration import proton_launch_options_ok

        ok = proton_launch_options_ok(install)
        if ok is True:
            return CheckResult(
                "launch_options", "Steam launch options", "ok",
                "WINEDLLOVERRIDES is set — BepInEx can inject under Proton.",
            )
        if ok is False:
            return CheckResult(
                "launch_options", "Steam launch options", "action_needed",
                "MTGA's Steam launch options are missing the override that "
                "lets BepInEx load — the bridge will never connect.",
                'In Steam: MTGA → Properties → Launch Options, add: '
                'WINEDLLOVERRIDES="winhttp=n,b" %command%',
            )
        return CheckResult(
            "launch_options", "Steam launch options", "action_needed",
            "Could not read Steam's launch options for MTGA.",
            'Verify in Steam: MTGA → Properties → Launch Options contains '
            'WINEDLLOVERRIDES="winhttp=n,b" %command%',
        )

    def _check_bridge_signal(self) -> Optional[CheckResult]:
        """Proof of actual injection: the plugin banner in BepInEx's log."""
        install = self._install
        if install is None:
            return None
        if self._is_native_mac(install):
            return self._bridge_not_applicable("bridge", "Bridge injection")
        log = install.install_dir / "BepInEx" / "LogOutput.log"
        if not log.exists():
            if install.platform.startswith("darwin"):
                # Wine/CrossOver bottle (native Mac was handled above):
                # there are no Steam launch options; the doorstop override
                # lives in the bottle's DLL settings instead.
                hint = (
                    'In your Wine/CrossOver bottle, set the "winhttp" DLL '
                    'override to "native, builtin" (WINEDLLOVERRIDES='
                    '"winhttp=n,b"), start MTGA once, then run Check & '
                    "Repair again."
                )
            else:
                hint = (
                    "Start MTGA once (on Linux, after fixing the launch "
                    "options above), then run Check & Repair again."
                )
            return CheckResult(
                "bridge", "Bridge injection", "action_needed",
                "BepInEx has never produced a log — it has not injected yet.",
                hint,
            )
        try:
            text = log.read_text(errors="replace")
        except OSError as e:
            return CheckResult(
                "bridge", "Bridge injection", "error", f"Cannot read log: {e}",
            )
        if "MtgaCoachBridge v" in text:
            return CheckResult(
                "bridge", "Bridge injection", "ok",
                "The bridge plugin loaded on MTGA's last launch.",
            )
        return CheckResult(
            "bridge", "Bridge injection", "action_needed",
            "BepInEx runs but the bridge plugin did not load on the last "
            "MTGA launch.",
            "Restart MTGA (the plugin was just [re]installed); if it still "
            "doesn't load, file a bug report from the Coach tab.",
        )


def set_license_key(key: str) -> CheckResult:
    """Persist a license key and validate it (the Repair tab's entry box)."""
    from arenamcp.settings import get_settings

    settings = get_settings()
    settings.set("license_key", key.strip(), save=False)
    # A manually entered key supersedes any auto-provisioned trial — clear
    # the marker so trial-specific messaging can't misfire on a real key.
    settings.set("trial_expires_at", "", save=True)
    return RepairEngine()._check_license()
