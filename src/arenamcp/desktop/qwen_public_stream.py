"""One offline MLX worker for the selected public booth, owned by the Qt thread."""

import base64
import json

from PySide6.QtCore import QObject, QProcess, QTimer, Signal

from arenamcp.commentary import qwen_public


class PublicVoiceStream(QObject):
    chunk = Signal(int, bytes)
    done = Signal(int)
    failed = Signal(int)

    def __init__(self, parent):
        super().__init__(parent)
        self.process = self.request = None
        self.ready = self.rendering = False
        self.stopping = self.wanted = False
        self.timeout = QTimer(self)
        self.timeout.setSingleShot(True)
        self.timeout.timeout.connect(self._fail)
        self.restart = QTimer(self)
        self.restart.setSingleShot(True)
        self.restart.timeout.connect(self.warm)

    def warm(self):
        self.wanted = True
        if self.stopping or (self.process is not None and self.process.state() != QProcess.NotRunning):
            return
        args = qwen_public.command()
        if not args:
            self._fail()
            return
        if self.process is None:
            # Keep a single Qt-owned wrapper even when changing native processes.
            # Deferred deletion from a process signal can outlive a renderer or
            # its parent event loop and invalidate PySide's signal bookkeeping.
            self.process = QProcess(self)
            self.process.setStandardErrorFile(QProcess.nullDevice())
            self.process.readyReadStandardOutput.connect(self._read)
            self.process.errorOccurred.connect(self._error)
            self.process.finished.connect(self._finished)
        self.timeout.start(45000)
        self.process.start(args[0], args[1:])

    def speak(self, generation, payload, valid):
        self.cancel()
        self.request = generation, payload, valid
        self.warm()
        self._dispatch()

    def _dispatch(self):
        if self.ready and self.request and not self.rendering:
            _, payload, valid = self.request
            if not valid():
                self._fail()
                return
            self.rendering = True
            self.process.write(json.dumps(payload).encode() + b"\n")
            self.timeout.start(45000)

    def _read(self):
        if self.stopping:
            self.process.readAllStandardOutput()
            return
        while not self.stopping and self.process.canReadLine():
            try:
                packet = json.loads(bytes(self.process.readLine(1_000_000)))
                event = packet["event"]
                if event == "ready" and not self.ready:
                    self.ready = True
                    self.timeout.stop()
                    self._dispatch()
                elif self.rendering and event == "pcm":
                    pcm = base64.b64decode(packet["data"], validate=True)
                    if not pcm or len(pcm) % 2:
                        raise ValueError()
                    self.chunk.emit(self.request[0], pcm)
                elif self.rendering and event == "done":
                    generation = self.request[0]
                    self.request = None
                    self.rendering = False
                    self.timeout.stop()
                    self.done.emit(generation)
                else:
                    self._fail()
            except (ValueError, KeyError, TypeError):
                self._fail()

    def _error(self, error):
        # A crashed process follows up with finished; failed-to-start does not.
        if error == QProcess.FailedToStart:
            self._finished(None, None)
        elif not self.stopping and error != QProcess.Crashed:
            self._fail()

    def _finished(self, _exit_code, _exit_status):
        self.process.readAllStandardOutput()
        if self.stopping:
            self.stopping = False
            if self.wanted:
                self.restart.start(0)
        else:
            self._fail()

    def _fail(self):
        generation = self.request[0] if self.request else None
        self.close()
        if generation is not None:
            self.failed.emit(generation)

    def cancel(self):
        if self.rendering:
            self.close()  # Interrupt native synthesis; the next request waits for the old process to exit.
        else:
            self.request = None

    def close(self):
        self.timeout.stop()
        self.restart.stop()
        self.request = None
        self.ready = self.rendering = self.wanted = False
        if self.process is not None and self.process.state() != QProcess.NotRunning:
            self.stopping = True
            self.process.kill()

    def shutdown(self):
        self.close()
        if self.process is not None and self.process.state() != QProcess.NotRunning:
            self.process.waitForFinished(1500)
