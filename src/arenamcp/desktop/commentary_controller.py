"""The desktop's single speech authority, including optional AOA proposals."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import replace

from PySide6.QtCore import QObject, QTimer, Signal

from arenamcp.commentary.config import CommentaryConfig, speech_rate
from arenamcp.commentary.contracts import TERMINAL_STATES, Epoch, SpeechCategory, Utterance
from arenamcp.commentary.events import ObservationAdapter
from arenamcp.commentary.setup import server_voices
from arenamcp.commentary.timeline import valid_for
from arenamcp.commentary.voices import VoicePairUnavailable, voice_pair
from arenamcp.settings import get_settings

from .speech_arbiter import SpeechArbiter

logger = logging.getLogger(__name__)


class CommentaryController(QObject):
    statusChanged = Signal(dict)
    catalogReady = Signal(str, object)

    def __init__(self, tts, send, spoken, parent=None):
        super().__init__(parent)
        self.tts, self.send, self.spoken = tts, send, spoken
        self.catalogs, self.catalog_pending, self.catalog_retry = {}, set(), {}
        self.catalogReady.connect(self._catalog_ready)
        settings = get_settings()
        self.config = CommentaryConfig.from_settings(settings)
        self.observation = None
        self.adapter = ObservationAdapter()
        self.timings = {}
        self.voice_names = {}
        self.preview_settings = {}
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
        self._catalog(settings)

    def configure(self, config):
        self.config = CommentaryConfig.from_wire(config)
        self.arbiter.disable_commentary()
        self.observation = None
        if self.config.mode != "actions":
            self.arbiter.tick()
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
        if item.utterance_id in self.preview_settings:
            return True
        if item.category != SpeechCategory.COMMENTARY:
            return item.category == SpeechCategory.ESSENTIAL or self.config.mode == "actions"
        return bool(self.config.enabled and self.observation and valid_for(item, self.observation))

    def proposal(self, payload):
        try:
            item = Utterance.from_wire(payload)
            if item.category != SpeechCategory.COMMENTARY:
                raise ValueError()
        except (ValueError, TypeError, KeyError):
            return
        settings = get_settings()
        provider = str(settings.get("tts_provider") or "kokoro")
        try:
            pair = voice_pair(settings, self._catalog(settings))
            voice = pair[1 if item.role == "color_analyst" else 0]
        except VoicePairUnavailable as error:
            self.commentary_ids.add(item.utterance_id)
            self._status(item.utterance_id, "failed")
            self.statusChanged.emit({"state": "voice", "message": str(error)})
            return
        item = replace(
            item,
            provider=provider,
            voice=voice,
            speed=speech_rate(settings),
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
            category = SpeechCategory(event.get("speech_category") or "workflow")
        except ValueError:
            category = SpeechCategory.WORKFLOW  # Urgent alerts must be explicitly categorized.
        if category != SpeechCategory.ESSENTIAL and self.config.mode != "actions":
            self._status(uid, "superseded")
            return
        self.arbiter.set_muted(muted)
        settings = get_settings()
        provider = str(settings.get("tts_provider") or "kokoro")
        try:
            speed = speech_rate(settings)
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
            voice=str(event.get("voice_id") or "af_heart")
            if provider == "kokoro"
            else self._chatterbox_voice(settings, provider),
            interruptible=category != SpeechCategory.WORKFLOW,
            speed=speed,
        )
        self.voice_names[uid] = str(event.get("voice_name") or item.voice)
        if not self.arbiter.submit(item):
            self._status(uid, "failed")

    @staticmethod
    def _chatterbox_voice(settings, provider):
        return str(
            settings.get("chatterbox_voice_override")
            or ("server_default" if provider == "chatterbox_remote" else "default")
        )

    def _catalog(self, settings):
        if settings.get("tts_provider") != "chatterbox_remote":
            return None
        supplied = settings.get("_commentary_voice_catalog")
        if isinstance(supplied, dict):
            return supplied
        endpoint = str(settings.get("chatterbox_server_url") or "")
        if endpoint in self.catalogs:
            return self.catalogs[endpoint]
        if (
            endpoint
            and endpoint not in self.catalog_pending
            and time.monotonic() >= self.catalog_retry.get(endpoint, 0)
        ):
            self.catalog_pending.add(endpoint)

            def read():
                try:
                    value = server_voices(endpoint)
                except Exception:
                    value = None
                try:
                    self.catalogReady.emit(endpoint, value)
                except RuntimeError:
                    pass

            threading.Thread(target=read, daemon=True, name="announcer-voices").start()
        return None

    def _catalog_ready(self, endpoint, value):
        self.catalog_pending.discard(endpoint)
        if value is None:
            self.catalog_retry[endpoint] = time.monotonic() + 30
            self.statusChanged.emit(
                {
                    "state": "voice",
                    "message": "Can't load announcer voices. Check the speech server connection or choose Kokoro.",
                }
            )
        else:
            self.catalogs[endpoint] = value

    def preview(self, options):
        self.stop()
        uid = "preview:" + uuid.uuid4().hex
        provider = str(options.get("tts_provider") or "kokoro")
        duo = options.get("autoplay_speech") == "commentary"
        try:
            voices = (
                voice_pair(options, self._catalog(options))
                if duo
                else (
                    str(options.get("voice") or "am_adam")
                    if provider == "kokoro"
                    else self._chatterbox_voice(options, provider),
                )
            )
        except VoicePairUnavailable as error:
            self.statusChanged.emit({"state": "voice", "message": str(error)})
            return
        preview_lines = {
            "classic": (
                "Welcome to the booth. I'll call the action.",
                "And I'll add the details. Good to have you with us.",
            ),
            "esports": (
                "I'm calling the action. Welcome to the booth!",
                "And I'm here for the big moments. Let's get into it!",
            ),
            "test_match": (
                "Well, here we are. I'll call the play.",
                "And I'll offer the occasional observation. Steady on.",
            ),
            "pro_tour": (
                "I'm on play-by-play. We'll keep the card language precise.",
                "I'll add the technical details supported by the game.",
            ),
        }
        texts = (
            preview_lines.get(options.get("commentary_persona"), preview_lines["classic"])
            if duo
            else ("The booth is ready. Welcome to the game.",)
        )
        for index, (voice, text) in enumerate(zip(voices, texts, strict=True)):
            item_id = uid if index == 0 else uid + ":analyst"
            self.preview_settings[item_id] = dict(options)
            item = Utterance(
                item_id,
                text,
                SpeechCategory.WORKFLOW,
                self.arbiter.epoch or Epoch("desktop", 1),
                time.monotonic() + 90,
                provider=provider,
                voice=voice,
                speed=speech_rate(options),
                role="color_analyst" if index else "play_by_play",
                anchor_id=uid if index else None,
            )
            if not self.arbiter.submit(item, allow_muted=True):
                self._status(item_id, "failed")

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
        overrides = (
            {"settings_override": self.preview_settings[item.utterance_id]}
            if item.utterance_id in self.preview_settings
            else {}
        )
        self.tts.request_speech(
            **overrides,
            text=item.text,
            voice_id=item.voice,
            voice_name=self.voice_names.get(item.utterance_id, item.voice),
            speed=item.speed,
            speech_id=item.utterance_id,
            provider=None
            if item.provider == "kokoro"
            and item.category != SpeechCategory.ESSENTIAL
            and item.utterance_id not in self.preview_settings
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
            self.preview_settings.pop(uid, None)
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
