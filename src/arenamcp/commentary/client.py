"""Bounded worker lifecycle/IPC. Only the calling background service may wait."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from .contracts import PROTOCOL_VERSION


class WorkerUnavailable(RuntimeError):
    pass


class CommentaryClient:
    def __init__(self):
        self.process = None
        self.responses = queue.Queue(maxsize=32)
        self._write_lock = threading.Lock()
        self.configuration = None

    def start(self, config):
        identity = (config.python, config.aoa_source)
        if self.process is not None and self.process.poll() is None and self.configuration == identity:
            return
        self.close()
        environment = dict(os.environ)
        sources = [str(Path(__file__).resolve().parents[2])]
        if config.aoa_source:
            source = Path(config.aoa_source).expanduser()
            sources.append(str(source / "src" if (source / "src" / "arenaonair").is_dir() else source))
        environment["PYTHONPATH"] = os.pathsep.join(sources)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTHONUNBUFFERED"] = "1"
        self.responses = queue.Queue(maxsize=32)
        python = str(Path(config.python).expanduser()) if config.python else sys.executable
        if getattr(sys, "frozen", False) and not config.python:
            from arenamcp.desktop.runtime import find_python_executable

            python, _ = find_python_executable()
            if not python:
                raise WorkerUnavailable("separate_python_required")
        self.process = subprocess.Popen(
            [
                python,
                "-u",
                str(Path(__file__).with_name("bootstrap.py")),
                "worker",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            env=environment,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        process, responses = self.process, self.responses

        def read():
            try:
                while True:
                    line = process.stdout.readline(1_048_577)
                    if not line:
                        break
                    if len(line) > 1_048_576:
                        raise ValueError()
                    value = json.loads(line)
                    if value.get("protocol_version") != PROTOCOL_VERSION:
                        raise ValueError()
                    responses.put_nowait(value)
            except (ValueError, queue.Full):
                process.kill()
            finally:
                try:
                    responses.put_nowait({"type": "exit"})
                except queue.Full:
                    pass

        threading.Thread(target=read, daemon=True, name="commentary-ipc").start()
        self.configuration = identity
        try:
            ready = self.responses.get(timeout=5).get("type") == "ready"
        except queue.Empty:
            ready = False
        if not ready:
            self.close()
            raise WorkerUnavailable("protocol_handshake")

    def send(self, payload):
        raw = json.dumps({"protocol_version": PROTOCOL_VERSION, **payload})
        if len(raw.encode()) > 1_048_576:
            raise WorkerUnavailable("command_size")
        with self._write_lock:
            if self.process is None or self.process.poll() is not None:
                raise WorkerUnavailable("worker_exited")
            self.process.stdin.write(raw + "\n")
            self.process.stdin.flush()

    def generate(self, observation, config, rpc, cancelled):
        self.start(config)
        job = uuid.uuid4().hex
        self.send(
            {
                "type": "observe",
                "job_id": job,
                "observation": observation.to_wire(),
                "config": config.to_wire(),
            }
        )
        deadline = time.monotonic() + 6
        while not cancelled.is_set() and time.monotonic() < deadline:
            try:
                event = self.responses.get(timeout=0.05)
            except queue.Empty:
                continue
            if event.get("type") == "exit":
                raise WorkerUnavailable("worker_exited")
            if event.get("job_id") != job:
                continue
            if event.get("type") == "result":
                return event
            if event.get("type") == "llm_request":
                try:
                    result = rpc(event["system"], event["context"], event["audit"] is True)
                    self.send({"type": "llm_result", "job_id": job, "result": result})
                except Exception:
                    self.send({"type": "llm_result", "job_id": job, "error": "admission_or_request_failed"})
        self.close()
        raise WorkerUnavailable("cancelled_or_deadline")

    def delivered(self, uid, state):
        if self.process is not None and self.process.poll() is None:
            self.send({"type": "delivery", "utterance_id": uid, "state": state})

    def close(self):
        process, self.process = self.process, None
        if process is None:
            return
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
