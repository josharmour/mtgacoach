"""The desktop's single speech authority, including optional AOA proposals."""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import replace

from PySide6.QtCore import QObject, QTimer, Signal

from arenamcp.commentary.config import CommentaryConfig
from arenamcp.commentary.contracts import TERMINAL_STATES, Epoch, SpeechCategory, Utterance
from arenamcp.commentary.events import ObservationAdapter
from arenamcp.settings import get_settings

from .speech_arbiter import SpeechArbiter

logger = logging.getLogger(__name__)


class CommentaryController(QObject):
    statusChanged = Signal(dict)

    def __init__(self, tts, send, spoken, parent=None):
        super().__init__(parent)
        self.tts, self.send, self.spoken = tts, send, spoken
        settings = get_settings()
        self.config = CommentaryConfig.from_settings(settings)
        self.observation = None
        self.adapter = ObservationAdapter()
        self.timings = {}
        self.voice_names = {}
        self.engine_ids = set()
        self.commentary_ids = set()
        self.arbiter = SpeechArbiter(
            start=self._start, stop=lambda: tts.stop_speech(), status=self._status, valid=self._valid
        )
        self.arbiter.reset(Epoch("desktop", 1))
        self.arbiter.set_muted(bool(settings.get("muted", False)))
        self.tts.speechStatus.connect(self._playback_status)
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.arbiter.tick)
        self.timer.start()

    def configure(self, config):
        self.config = CommentaryConfig.from_wire(config)
        self.arbiter.disable_commentary()
        self.observation = None
        if self.config.mode != "actions":
            self.arbiter.disable_routine()
        self.statusChanged.emit({"state": "waiting" if self.config.enabled else "disabled"})

    def observe(self, snapshot):
        try:
            observation = self.adapter.project(snapshot, self.config.private_information)
        except (ValueError, TypeError, KeyError):
            self.observation = None
            self.arbiter.disable_commentary()
            return
        self.observation = observation
        if observation.epoch != self.arbiter.epoch:
            self.arbiter.reset(observation.epoch)
        else:
            self.arbiter.tick()

    def _valid(self, item):
        if item.category != SpeechCategory.COMMENTARY:
            return True
        return bool(
            self.config.enabled
            and self.observation
            and item.epoch == self.observation.epoch
            and item.snapshot_id == self.observation.snapshot_id
        )

    def proposal(self, payload):
        try:
            item = Utterance.from_wire(payload)
            if item.category != SpeechCategory.COMMENTARY:
                raise ValueError()
        except (ValueError, TypeError, KeyError):
            return
        settings = get_settings()
        provider = str(settings.get("tts_provider") or "kokoro")
        # AOA voices are provider-specific; no Atlas name is sent to Kokoro.
        if provider == "kokoro":
            voice = str(
                settings.get("commentary_kokoro_analyst" if item.role == "color_analyst" else "voice")
                or ("am_onyx" if item.role == "color_analyst" else "am_adam")
            )
        else:
            voice = str(
                settings.get(
                    "commentary_chatterbox_analyst" if item.role == "color_analyst" else "chatterbox_voice"
                )
                or ("mira" if item.role == "color_analyst" else "atlas")
            )
        item = replace(
            item,
            provider=provider,
            voice=voice,
            speed=float(settings.get("chatterbox_speed" if provider != "kokoro" else "voice_speed") or 1),
        )
        if item.utterance_id in self.arbiter._states:
            return
        self.commentary_ids.add(item.utterance_id)
        if not self.arbiter.submit(item):
            self._status(item.utterance_id, "failed")

    def speech(self, event, *, muted=False):
        text = str(event.get("text") or event.get("data") or "")
        engine_id = str(event.get("speech_id") or "")
        if not text.strip() or len(text) > 16000:
            if engine_id:
                self.send({"cmd": "speech_status", "speech_id": engine_id, "state": "failed"})
            return
        self.spoken(text)
        uid = engine_id or "desktop:" + uuid.uuid4().hex
        if uid in self.arbiter._states:
            return
        if engine_id:
            self.engine_ids.add(uid)
        try:
            category = SpeechCategory(
                event.get("speech_category") or ("workflow" if engine_id else "essential")
            )
        except ValueError:
            category = SpeechCategory.ESSENTIAL  # Unknown messages must not silently lose urgent speech.
        if category == SpeechCategory.ROUTINE and self.config.mode != "actions":
            self._status(uid, "superseded")
            return
        self.arbiter.set_muted(muted)
        settings = get_settings()
        provider = (
            "kokoro"
            if category == SpeechCategory.ESSENTIAL
            else str(settings.get("tts_provider") or "kokoro")
        )
        try:
            speed = (
                float(settings.get("chatterbox_speed") or 1)
                if provider != "kokoro"
                else float(event.get("speed") or 1)
            )
            if not 0.5 <= speed <= 2:
                raise ValueError()
        except (TypeError, ValueError):
            self._status(uid, "failed")
            return
        item = Utterance(
            uid,
            text,
            category,
            self.arbiter.epoch or Epoch("desktop", 1),
            time.monotonic() + 300,
            provider=provider,
            voice=str(event.get("voice_id") or "af_heart"),
            interruptible=category != SpeechCategory.WORKFLOW,
            speed=speed,
        )
        self.voice_names[uid] = str(event.get("voice_name") or item.voice)
        if not self.arbiter.submit(item):
            self._status(uid, "failed")

    def _start(self, item):
        times = self.timings.setdefault(item.utterance_id, {"accepted": time.monotonic()})
        times["render_start"] = time.monotonic()
        logger.info(
            "Speech render uid=%s category=%s provider=%s queue_seconds=%.3f",
            item.utterance_id,
            item.category.value,
            item.provider,
            times["render_start"] - times["accepted"],
        )
        if item.category == SpeechCategory.COMMENTARY:
            self.spoken(item.text)
        self.tts.request_speech(
            text=item.text,
            voice_id=item.voice,
            voice_name=self.voice_names.get(item.utterance_id, item.voice),
            speed=item.speed,
            speech_id=item.utterance_id,
            provider=None
            if item.provider == "kokoro" and item.category != SpeechCategory.ESSENTIAL
            else item.provider,
            category=item.category.value,
            valid=lambda: (
                self._valid(item) and item.epoch == self.arbiter.epoch and time.monotonic() < item.expires_at
            ),
        )

    def _playback_status(self, uid, state):
        item = self.arbiter.active
        if item and item.utterance_id == uid:
            self.arbiter.playback_status(uid, state, epoch=item.epoch)

    def _status(self, uid, state):
        now = time.monotonic()
        if state == "accepted":
            self.timings[uid] = {"accepted": now}
        times = self.timings.get(uid, {})
        if state == "started":
            times["started"] = now
        logger.info(
            "Speech lifecycle uid=%s state=%s since_accept_seconds=%.3f render_seconds=%.3f",
            uid,
            state,
            now - times.get("accepted", now),
            now - times.get("render_start", now),
        )
        if uid in self.engine_ids:
            self.send({"cmd": "speech_status", "speech_id": uid, "state": state})
        if uid in self.commentary_ids:
            self.send({"cmd": "commentary_delivery", "utterance_id": uid, "state": state})
        if state in TERMINAL_STATES:
            self.timings.pop(uid, None)
            self.voice_names.pop(uid, None)
            self.engine_ids.discard(uid)
            self.commentary_ids.discard(uid)

    def stop(self):
        had_active = self.arbiter.active is not None
        self.arbiter.clear()
        if not had_active:
            self.tts.stop_speech()

    def close(self):
        self.timer.stop()
        self.stop()
