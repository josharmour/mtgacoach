#!/usr/bin/env python3
"""Send one command to the injected IL2CPP probe and print its JSON reply.

Usage: probe_client.py [command words...]   (default: ping)
Commands: ping | pending | keep | choose_start SEAT | submit_action INDEX [INSTANCE_ID]
          | submit_pass | class NAMESPACE|- NAME | static NAMESPACE|- CLASS FIELD
"""

import json
import os
import socket
import sys

PORT = int(os.environ.get("MTGACOACH_PROBE_PORT", "44223"))


def send(command: str, timeout: float = 8.0) -> dict:
    with socket.create_connection(("127.0.0.1", PORT), timeout=timeout) as conn:
        conn.sendall((command + "\n").encode())
        reply = b""
        while not reply.endswith(b"\n"):
            chunk = conn.recv(65536)
            if not chunk:
                break
            reply += chunk
    return json.loads(reply.decode())


if __name__ == "__main__":
    print(json.dumps(send(" ".join(sys.argv[1:]) or "ping"), indent=2))
