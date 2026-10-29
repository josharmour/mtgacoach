"""Persistent isolated local-TTS process; called only from the render thread."""

from __future__ import annotations

import base64
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

from arenamcp.chatterbox_remote import ChatterboxError, SpeechCancelled


class LocalChatterboxClient:
    def __init__(self):
        self.process = None
        self.config = None
        self.ready = False
        self.last_metrics = {}
        self.failed = False
        self.responses = queue.Queue(maxsize=2)

    def _read(self, process, responses):
        try:
            for line in iter(lambda: process.stdout.readline(28_000_001), ""):
                if len(line) > 28_000_000:
                    raise ValueError()
                responses.put_nowait(json.loads(line))
        except (ValueError, queue.Full):
            process.kill()
        finally:
            try:
                responses.put_nowait({"type": "error", "reason": "worker_exit"})
            except queue.Full:
                pass

    def _wait(self, cancelled, timeout):
        until = time.monotonic() + timeout
        while not cancelled.is_set() and time.monotonic() < until:
            try:
                result = self.responses.get(timeout=0.05)
                if result.get("type") == "error":
                    self.close()
                    self.failed = True
                    raise ChatterboxError(
                        "Local Chatterbox failed; preview again after checking its runtime."
                    )
                return result
            except queue.Empty:
                continue
        self.close()
        if cancelled.is_set():
            raise SpeechCancelled("Local speech cancelled.")
        self.failed = True
        raise ChatterboxError("Local Chatterbox missed its deadline.")

    def render(self, *, config, text, voice, speed, cancelled):
        identity = tuple(sorted(config.items()))
        if self.config != identity:
            self.close()
            self.failed = False
            self.config = identity
        if self.failed:
            raise ChatterboxError("Local voice unavailable; select or preview it to retry.")
        if not self.ready:
            if not config.get("python"):
                raise ChatterboxError("Select a separate local Chatterbox Python runtime.")
            env = dict(os.environ)
            paths = [str(Path(__file__).resolve().parents[1])]
            if config.get("aoa_source"):
                source = Path(config["aoa_source"]).expanduser()
                paths.append(str(source / "src" if (source / "src" / "arenaonair").is_dir() else source))
            env.update(
                PYTHONPATH=os.pathsep.join(paths),
                PYTHONDONTWRITEBYTECODE="1",
                HF_HUB_OFFLINE="1",
                TRANSFORMERS_OFFLINE="1",
                OMP_NUM_THREADS="1",
            )
            self.responses = queue.Queue(maxsize=2)
            self.process = subprocess.Popen(
                [
                    str(Path(config["python"]).expanduser()),
                    "-u",
                    str(Path(__file__).with_name("commentary") / "bootstrap.py"),
                    "local_voice_worker",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                env=env,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            threading.Thread(
                target=self._read,
                args=(self.process, self.responses),
                daemon=True,
                name="local-chatterbox-ipc",
            ).start()
            self._send(
                {"cmd": "init", **{k: v for k, v in config.items() if k not in {"python", "aoa_source"}}}
            )
            self.ready = self._wait(cancelled, 90).get("type") == "ready"
        self._send({"cmd": "render", "text": text, "voice": voice, "speed": speed})
        result = self._wait(cancelled, 6)
        self.last_metrics = result.get("metrics", {})
        raw = base64.b64decode(result.get("audio", ""), validate=True)
        if not raw or len(raw) > 20_000_044 or not raw.startswith(b"RIFF"):
            raise ChatterboxError("Invalid local audio.")
        return raw

    def _send(self, value):
        self.process.stdin.write(json.dumps(value) + "\n")
        self.process.stdin.flush()

    def close(self):
        process, self.process = self.process, None
        self.ready = False
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=0.3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=0.3)
            for stream in (process.stdin, process.stdout):
                if stream:
                    stream.close()
