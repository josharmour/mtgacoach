"""Live validation of get_game_state against the real probe on m5.

Run on the Mac:  python3 spikes/mac-il2cpp/live_gs_check.py
Requires MTGA running with the probe injected (launch_mtga_probe.sh).
"""

import json
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from arenamcp.mac_game_state import fetch_game_state  # noqa: E402


def main() -> int:
    sock = socket.create_connection(("127.0.0.1", 44223), timeout=8)
    buffer = ""

    def send(payload, timeout=None):
        # The probe speaks newline-delimited JSON (serve_lines in probe.cpp).
        nonlocal buffer
        sock.settimeout(timeout or 8.0)
        sock.sendall((json.dumps(payload) + "\n").encode())
        while "\n" not in buffer:
            chunk = sock.recv(1 << 20)
            if not chunk:
                raise ConnectionError("connection closed")
            buffer += chunk.decode()
        line, buffer = buffer.split("\n", 1)
        buffer = buffer
        return json.loads(line)

    pong = send({"action": "ping"})
    print("ping ok:", pong.get("ok"))
    response = fetch_game_state(send, 8.0)
    sock.close()

    if not response.get("ok"):
        print("get_game_state error:", response.get("error"))
        return 1

    print("turn:", json.dumps(response["turn"]))
    for player in response["players"]:
        print(
            "player:",
            {k: player[k] for k in ("seat_id", "life_total", "is_local", "status")},
        )
    for name, zone in response.get("zones", {}).items():
        cards = zone["cards"]
        summary = ", ".join(
            f"{c['instance_id']}:{c['grp_id']}"
            + (" tapped" if c.get("is_tapped") else "")
            + (" ATK" if c.get("is_attacking") else "")
            for c in cards[:6]
        )
        print(f"zone {name}: total={zone['total_count']} shown={len(cards)} [{summary}]")
    for key in ("attack_info", "block_info"):
        if key in response:
            print(key, "=", json.dumps(response[key]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
