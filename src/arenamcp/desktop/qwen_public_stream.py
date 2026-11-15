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
        self.retiring = set()
        self.ready = self.rendering = False
        self.timeout = QTimer(self)
        self.timeout.setSingleShot(True)
        self.timeout.timeout.connect(self._fail)

    def warm(self):
        if self.process is not None:
            return
        args = qwen_public.command()
        if not args:
            self._fail()
            return
        worker = QProcess(self)
        self.process = worker
        worker.setStandardErrorFile(QProcess.nullDevice())
        worker.readyReadStandardOutput.connect(lambda: self._read(worker))
        worker.errorOccurred.connect(lambda *_: self._exited(worker))
        worker.finished.connect(lambda *_: self._exited(worker))
        worker.start(args[0], args[1:])
        self.timeout.start(45000)

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

    def _read(self, worker):
        while self.process is worker and worker.canReadLine():
            try:
                packet = json.loads(bytes(worker.readLine(1_000_000)))
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

    def _exited(self, worker):
        if worker is self.process:
            self._fail()

    def _fail(self):
        generation = self.request[0] if self.request else None
        self.close()
        if generation is not None:
            self.failed.emit(generation)

    def cancel(self):
        if self.rendering:
            self.close()  # Interrupt native synthesis immediately; the next request starts a fresh worker.
        else:
            self.request = None

    def close(self):
        worker, self.process = self.process, None
        self.timeout.stop()
        self.request = None
        self.ready = self.rendering = False
        if worker is not None:
            if worker.state() != QProcess.NotRunning:
                self.retiring.add(worker)
                worker.finished.connect(lambda *_: self.retiring.discard(worker))
                worker.finished.connect(worker.deleteLater)
                worker.kill()
            else:
                worker.deleteLater()

    def shutdown(self):
        self.close()
        for worker in tuple(self.retiring):
            worker.waitForFinished(1500)
