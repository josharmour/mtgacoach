"""Android phone link: MTGA runs on a phone tethered over adb (USB or wireless
debugging) with the mtgacoach probe injected; the coach runs on this computer.

- Eyes: phones have no Player.log. MTGA's own LogToFile writes the same lines
  (plus stack traces, which the parser skips) to UTC_Log files in the app's
  external files dir, only when Detailed Logs is on. The mirror streams the
  newest UTC_Log into a local file and the log watcher reads that
  (MTGA_LOG_PATH); a new MTGA session truncates the mirror, which the watcher
  already treats as a restart. The injected probe cuts LogToFile's 30 s flush
  to 0.25 s.
- Hands: the probe dials 127.0.0.1:44222 on the phone; `adb reverse` lands that
  on the coach's GRE bridge here, where it identifies as "il2cpp-android" and
  MacBridgeAdapter drives it exactly like the native-Mac client.

Installing the probe needs root and lives in spikes/android-il2cpp/install.sh
for now; this module only links a phone that already has it.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

PACKAGE = "com.wizards.mtga"
PHONE_LOG_DIR = f"/sdcard/Android/data/{PACKAGE}/files/Logs/Logs"
BRIDGE_PORT = 44222
MIRROR_PATH = Path.home() / ".arenamcp" / "android" / "Player.log"
_FILE_MARKER = b"@@MTGACOACH_UTCLOG@@"

# Streams the newest UTC_Log from its first byte and switches when MTGA starts a
# new session (new file). The leading newline keeps the marker on its own line
# even when the previous tail stopped mid-line.
_FOLLOW_SCRIPT = f"""
d={PHONE_LOG_DIR}; cur=; pid=
while :; do
  new=$(ls -t "$d"/UTC_Log* 2>/dev/null | head -n 1)
  if [ -n "$new" ] && [ "$new" != "$cur" ]; then
    [ -n "$pid" ] && kill $pid 2>/dev/null
    cur=$new
    printf '\\n{_FILE_MARKER.decode()} %s\\n' "$cur"
    tail -c +1 -f "$cur" &
    pid=$!
  fi
  sleep 1
done
"""


ANDROID_RUNTIME = "il2cpp-android"


def game_device() -> str:
    """ "desktop" or "android" (MTGACOACH_GAME_DEVICE overrides the setting)."""
    device = os.environ.get("MTGACOACH_GAME_DEVICE")
    if not device:
        try:
            from arenamcp.settings import get_settings

            device = get_settings().get("game_device")
        except Exception:
            device = None
    return "android" if device == "android" else "desktop"


def _partial_marker_len(data: bytes) -> int:
    """Length of the longest suffix of data that is a proper prefix of the marker."""
    for size in range(min(len(data), len(_FILE_MARKER) - 1), 0, -1):
        if _FILE_MARKER.startswith(data[-size:]):
            return size
    return 0


def find_adb() -> str | None:
    """adb on PATH, else the usual SDK / Homebrew locations."""
    found = shutil.which("adb")
    if found:
        return found
    home = Path.home()
    candidates = [
        Path("/opt/homebrew/bin/adb"),
        Path("/usr/local/bin/adb"),
        Path("/opt/homebrew/share/android-commandlinetools/platform-tools/adb"),
        home / "Library" / "Android" / "sdk" / "platform-tools" / "adb",
        home / "Android" / "Sdk" / "platform-tools" / "adb",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Android" / "Sdk" / "platform-tools" / "adb.exe",
    ]
    return next((str(path) for path in candidates if path.is_file()), None)


STATE_PATH = MIRROR_PATH.parent / "link.json"


def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        return {}


def _save_state(**updates: str) -> None:
    state = _load_state()
    state.update(updates)
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state))
    except OSError:
        logger.debug("android: could not save link state", exc_info=True)


def is_wifi_serial(serial: str) -> bool:
    """Wireless transports are "ip:port" or an mDNS "adb-…._adb-tls-connect._tcp" name."""
    return ":" in serial or "._adb-tls-connect." in serial


def hardware_serial(adb: str, serial: str) -> str:
    """ro.serialno: the same for the phone's USB and Wi-Fi transports."""
    try:
        out = subprocess.run(
            [adb, "-s", serial, "shell", "getprop", "ro.serialno"], capture_output=True, text=True, timeout=10
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        out = ""
    return out or serial


def find_phone(adb: str, hw_serial: str | None = None) -> str | None:
    """Best transport for the phone (USB over Wi-Fi), optionally a specific phone."""
    candidates = [d for d in list_devices(adb) if not hw_serial or hardware_serial(adb, d) == hw_serial]
    candidates.sort(key=is_wifi_serial)  # USB first: faster, and survives Wi-Fi changes
    return candidates[0] if candidates else None


def reconnect_wifi(adb: str) -> None:
    """`adb connect` the phone's last wireless-debugging endpoint (mDNS is often blocked)."""
    endpoint = _load_state().get("wifi_endpoint")
    if not endpoint:
        return
    try:
        subprocess.run([adb, "connect", endpoint], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


def list_devices(adb: str) -> list[str]:
    """Serials of devices in the "device" state (authorized and online)."""
    try:
        out = subprocess.run([adb, "devices"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [
        parts[0]
        for line in out.splitlines()[1:]
        if len(parts := line.split()) >= 2 and parts[1] == "device"
    ]


class AndroidLink:
    """Keeps `adb reverse` in place and mirrors the phone's MTGA log locally."""

    def __init__(self, adb: str, serial: str, mirror_path: Path = MIRROR_PATH) -> None:
        self.adb = adb
        self.serial = serial
        self.mirror_path = mirror_path
        self.source_file: str | None = None
        self.device_name = serial
        self.hw_serial = serial
        self._firewall_warned = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._process: subprocess.Popen | None = None
        self._received = threading.Event()

    def _adb(self, *args: str, timeout: float = 10) -> subprocess.CompletedProcess:
        return subprocess.run(
            [self.adb, "-s", self.serial, *args], capture_output=True, text=True, timeout=timeout
        )

    def model(self) -> str:
        """Marketing model name for the UI ("Pixel 10 Pro"), else the serial."""
        try:
            name = self._adb("shell", "getprop", "ro.product.model").stdout.strip()
        except (OSError, subprocess.SubprocessError):
            name = ""
        return name or self.serial

    def mtga_installed(self) -> bool:
        return f"package:{PACKAGE}" in self._adb("shell", "pm", "list", "packages", PACKAGE).stdout

    def ensure_reverse(self) -> bool:
        result = self._adb("reverse", f"tcp:{BRIDGE_PORT}", f"tcp:{BRIDGE_PORT}")
        if result.returncode != 0:
            logger.warning("android: adb reverse failed: %s", (result.stderr or result.stdout).strip())
            return False
        self._restrict_reverse_to_loopback()
        return True

    def _restrict_reverse_to_loopback(self) -> None:
        """adbd listens for the reverse on every interface, so anyone on the phone's
        Wi-Fi could reach the coach's bridge through it. Only the probe (loopback)
        should: drop everything else (needs root; resets on reboot)."""
        rules = " ; ".join(
            f"{tool} -C INPUT -p tcp --dport {BRIDGE_PORT} ! -i lo -j DROP 2>/dev/null"
            f" || {tool} -I INPUT -p tcp --dport {BRIDGE_PORT} ! -i lo -j DROP"
            for tool in ("iptables", "ip6tables")
        )
        result = self._adb("shell", f"su 0 sh -c '{rules}'")
        if result.returncode != 0 and not self._firewall_warned:
            self._firewall_warned = True
            logger.warning(
                "android: could not firewall the bridge port (no root?); it is reachable from the phone's network"
            )

    def _ensure_transport(self) -> bool:
        """Follow the phone across USB unplug/replug and Wi-Fi reconnects."""
        if self.serial in list_devices(self.adb):
            return True
        serial = find_phone(self.adb, self.hw_serial)
        if serial is None:
            reconnect_wifi(self.adb)
            serial = find_phone(self.adb, self.hw_serial)
        if serial is None:
            return False
        logger.info("android: phone now reachable as %s (was %s)", serial, self.serial)
        self.serial = serial
        if is_wifi_serial(serial) and not serial.startswith("adb-"):
            _save_state(wifi_endpoint=serial)
        return True

    def start(self, initial_wait_s: float = 5.0) -> None:
        """Start mirroring; waits briefly so the watcher's first read has data."""
        self.mirror_path.parent.mkdir(parents=True, exist_ok=True)
        self.mirror_path.write_bytes(b"")
        self._thread = threading.Thread(target=self._run, name="android-log-mirror", daemon=True)
        self._thread.start()
        if not self._received.wait(initial_wait_s):
            logger.info("android: no MTGA log from the phone yet (MTGA not running?)")

    def stop(self) -> None:
        self._stop.set()
        if self._process and self._process.poll() is None:
            self._process.terminate()

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            received = 0
            if self._ensure_transport():
                self.ensure_reverse()  # a replug or adb restart drops the reverse
                try:
                    self._process = subprocess.Popen(
                        [self.adb, "-s", self.serial, "exec-out", "sh", "-c", _FOLLOW_SCRIPT],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                    )
                    received = self._pump(self._process)
                except Exception:
                    logger.warning("android: log mirror failed", exc_info=True)
                if self._process and self._process.poll() is None:
                    self._process.terminate()
            if self._stop.is_set():
                break
            # Only a stream that delivered data resets the backoff; a missing
            # phone must not turn into a once-a-second retry loop.
            backoff = 1.0 if received else min(backoff * 2, 30.0)
            if backoff in (2.0, 30.0):
                logger.info("android: phone unreachable; retrying every %.0f s", backoff)
            self._stop.wait(backoff)

    def _pump(self, process: subprocess.Popen) -> int:
        """Copy the phone stream into the mirror; returns the bytes received."""
        assert process.stdout is not None
        out = self.mirror_path.open("ab")
        received = 0
        try:
            pending = b""
            while not self._stop.is_set():
                chunk = process.stdout.read1(65536) if hasattr(process.stdout, "read1") else process.stdout.read(65536)
                if not chunk:
                    return received
                received += len(chunk)
                pending += chunk
                while (at := pending.find(_FILE_MARKER)) >= 0:
                    out.write(pending[:at])
                    end = pending.find(b"\n", at)
                    if end < 0:
                        break  # marker line incomplete; wait for more
                    self.source_file = pending[at + len(_FILE_MARKER) : end].decode(errors="replace").strip()
                    pending = pending[end + 1 :]
                    # New MTGA session: restart the mirror so the watcher resets.
                    out.close()
                    self.mirror_path.write_bytes(b"")
                    out = self.mirror_path.open("ab")
                    logger.info("android: mirroring %s", self.source_file)
                else:
                    # Hold back only a tail that could be the start of a marker.
                    keep = _partial_marker_len(pending)
                    out.write(pending[: len(pending) - keep])
                    pending = pending[len(pending) - keep :]
                    out.flush()
                    if out.tell() > 0:
                        self._received.set()
                    continue
                out.flush()
            return received
        finally:
            out.close()


def start_android_link(serial: str | None = None) -> AndroidLink | None:
    """Link the first (or given) adb phone with MTGA and point the watcher at its log.

    Returns None when there is no usable phone; the coach then stays on the
    desktop client's Player.log.
    """
    adb = find_adb()
    if not adb:
        logger.warning("android: adb not found; install Android platform-tools")
        return None
    if serial:
        chosen = serial if serial in list_devices(adb) else None
    else:
        chosen = find_phone(adb)
        if chosen is None:
            reconnect_wifi(adb)
            chosen = find_phone(adb)
    if chosen is None:
        logger.warning("android: no authorized phone over adb (USB, or Wi-Fi via wireless debugging)")
        return None
    link = AndroidLink(adb, chosen)
    link.hw_serial = hardware_serial(adb, chosen)
    link.device_name = link.model()
    if is_wifi_serial(chosen) and not chosen.startswith("adb-"):
        _save_state(wifi_endpoint=chosen)
    if not link.mtga_installed():
        logger.warning("android: MTGA is not installed on %s", link.serial)
        return None
    os.environ["MTGA_LOG_PATH"] = str(link.mirror_path)
    link.start()
    logger.info("android: linked %s; log mirror %s", link.serial, link.mirror_path)
    return link
