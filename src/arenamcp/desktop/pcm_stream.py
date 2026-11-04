"""Qt-owned incremental PCM playback on Mac, Windows and Linux."""

import logging

from PySide6.QtCore import QObject, QTimer, Signal

logger = logging.getLogger(__name__)


class PcmStreamPlayer(QObject):
    started = Signal()
    finished = Signal()
    failed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.sink = self.device = None
        self.pending = bytearray()
        self.done = self.playing = False
        self.timer = QTimer(self)
        self.timer.setInterval(10)
        self.timer.timeout.connect(self._pump)

    def append(self, data):
        if len(self.pending) + len(data) > 20_000_000:
            self._fail()
            return
        self.pending.extend(data)
        if self.sink is None:
            try:
                from PySide6.QtMultimedia import QAudioFormat, QAudioSink, QMediaDevices

                format = QAudioFormat()
                format.setSampleRate(24000)
                format.setChannelCount(1)
                format.setSampleFormat(QAudioFormat.Int16)
                output = QMediaDevices.defaultAudioOutput()
                if output.isNull() or not output.isFormatSupported(format):
                    raise RuntimeError("No compatible audio output")
                self.sink = QAudioSink(output, format, self)
                self.sink.setBufferSize(9600)
                self.device = self.sink.start()
                if self.device is None:
                    raise RuntimeError("Audio output unavailable")
                self.timer.start()
            except Exception as error:
                logger.warning("Cannot open streaming audio output: %s", error)
                self._fail()
                return
        self._pump()

    def finish(self):
        self.done = True
        self._pump()

    def _pump(self):
        from PySide6.QtMultimedia import QAudio

        if self.sink is None:
            return
        # Qt 6.10 exposes both QAudio and QtAudio enum wrappers; sink results
        # can use a different Python enum class with the same Qt value.
        if self.sink.error().value not in (QAudio.NoError.value, QAudio.UnderrunError.value):
            logger.warning("Streaming audio output failed: %s", self.sink.error())
            self._fail()
            return
        count = min(len(self.pending), self.sink.bytesFree()) // 2 * 2
        if count:
            written = self.device.write(bytes(self.pending[:count]))
            if written < 0:
                self._fail()
                return
            del self.pending[:written]
            if written and not self.playing:
                self.playing = True
                self.started.emit()
        if self.done and not self.pending and self.sink.state().value == QAudio.IdleState.value:
            self.stop()
            self.finished.emit()

    def _fail(self):
        self.stop()
        self.failed.emit()

    def stop(self):
        self.timer.stop()
        if self.sink is not None:
            self.sink.reset()
            self.sink.deleteLater()
        self.sink = self.device = None
        self.pending.clear()
        self.done = self.playing = False
