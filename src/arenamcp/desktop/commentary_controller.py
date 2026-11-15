"""The desktop's single speech authority, including optional AOA proposals."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import replace

from PySide6.QtCore import QObject, QTimer, Signal

from arenamcp.chatterbox_remote import HOSTED_BASE
from arenamcp.commentary import qwen_public
from arenamcp.commentary.config import CommentaryConfig, normalize_persona, speech_rate
from arenamcp.commentary.contracts import TERMINAL_STATES, Epoch, SpeechCategory, Utterance
from arenamcp.commentary.delivery import delivery_level
from arenamcp.commentary.events import ObservationAdapter
from arenamcp.commentary.setup import server_voices
from arenamcp.commentary.timeline import valid_for
from arenamcp.commentary.voices import VoicePairUnavailable, voice_pair
from arenamcp.player_names import pronounce_players
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
        self.private_removal = None
        self.private_removal_played = set()
        self.private_removal_uid = None
        self.adapter = ObservationAdapter()
        self.timings = {}
        self.voice_names = {}
        self.preview_settings = {}
        self.engine_ids = set()
        self.commentary_ids = set()
        self.private_commentary_ids = set()
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
        if self.config.enabled and qwen_public.selected(settings) and not self.arbiter.muted:
            self.tts.warm_public_voice()

    def configure(self, config):
        updated = CommentaryConfig.from_wire(config)
        settings = get_settings()
        if (
            updated.enabled
            and not updated.private_information
            and not settings.get("muted", False)
            and qwen_public.selected(settings)
        ):
            self.tts.warm_public_voice()
        elif not self.preview_settings and hasattr(self.tts, "release_public_voice"):
            self.tts.release_public_voice()
        if updated == self.config:
            return  # The engine echoes settings; an acknowledgment is not Stop.
        self.config = updated
        self.private_removal = None
        self.arbiter.disable_commentary()
        self.observation = None
        if self.config.mode != "actions":
            self.arbiter.tick()
        self.statusChanged.emit({"state": "waiting" if self.config.enabled else "disabled"})

    def observe(self, snapshot, *, private_removal=None):
        self.private_removal = None
        try:
            observation = self.adapter.project(snapshot, self.config.private_information)
        except (ValueError, TypeError, KeyError):
            self.observation = None
            self.arbiter.disable_commentary()
            return
        self.observation = observation
        if self.config.enabled and self.config.private_information and private_removal:
            from arenamcp.commentary.private_outs import private_removal_line

            self.private_removal = private_removal_line(private_removal, snapshot)
        if observation.epoch != self.arbiter.epoch:
            self.private_removal_played.clear()
            self.arbiter.reset(observation.epoch)
        else:
            self.arbiter.tick()
        if (
            self.private_removal
            and not self.arbiter.muted
            and not self.arbiter.pending
            and not self.arbiter.active
        ):
            threat_id, text = self.private_removal
            if threat_id not in self.private_removal_played and len(self.private_removal_played) < 100:
                # The full text stays on the desktop. It never enters the
                # generated booth, heard-text history, transcript, or a remote voice.
                settings = get_settings()
                if settings.get("tts_provider", "kokoro") != "kokoro" or not self.tts.private_render_ready:
                    return
                try:
                    pair = voice_pair(settings)
                except VoicePairUnavailable:
                    return
                item = Utterance(
                    "private-outs:" + uuid.uuid4().hex,
                    text,
                    SpeechCategory.COMMENTARY,
                    observation.epoch,
                    time.monotonic() + 8,
                    role="play_by_play",
                    provider="kokoro",
                    voice=pair[0],
                    speed=speech_rate(settings),
                )
                self.private_removal_uid = (item.utterance_id, self.private_removal)
                self.private_removal_played.add(threat_id)
                self.arbiter.submit(item)

    def _valid(self, item):
        if item.utterance_id.startswith("private-outs:"):
            return bool(
                self.config.enabled
                and self.config.private_information
                and self.private_removal
                and self.observation
                and item.epoch == self.observation.epoch
                and self.private_removal_uid == (item.utterance_id, self.private_removal)
                and item.provider == "kokoro"
            )
        if item.utterance_id in self.preview_settings:
            return item.provider != qwen_public.PROVIDER or qwen_public.selected(
                self.preview_settings[item.utterance_id]
            )
        if item.utterance_id in self.private_commentary_ids and (
            not self.config.private_information
            or get_settings().get("commentary_private_information") is not True
        ):
            return False
        if item.provider == qwen_public.PROVIDER and (
            self.config.private_information or not qwen_public.selected(get_settings())
        ):
            return False
        if item.category != SpeechCategory.COMMENTARY:
            return (
                item.category in {SpeechCategory.ESSENTIAL, SpeechCategory.ADVICE}
                or self.config.mode == "actions"
            )
        return bool(self.config.enabled and self.observation and valid_for(item, self.observation))

    def proposal(self, payload, *, public_only=False):
        try:
            item = Utterance.from_wire(payload)
            if item.category != SpeechCategory.COMMENTARY or item.utterance_id.startswith("private-outs:"):
                raise ValueError()
        except (ValueError, TypeError, KeyError):
            return
        if item.utterance_id in self.arbiter._states:
            return
        settings = get_settings()
        if public_only is not True:
            if (
                not self.config.private_information
                or settings.get("commentary_private_information") is not True
            ):
                self.commentary_ids.add(item.utterance_id)
                self._status(item.utterance_id, "superseded")
                return
            self.private_commentary_ids.add(item.utterance_id)
        provider = "kokoro" if public_only is not True else str(settings.get("tts_provider") or "kokoro")
        try:
            if not self.config.private_information and qwen_public.selected(settings):
                provider, pair = qwen_public.PROVIDER, qwen_public.PAIR
            else:
                catalog = self._catalog(settings) if public_only is True else None
                pair = voice_pair(settings, catalog, provider=provider)
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
        uid = engine_id or "desktop:" + uuid.uuid4().hex
        if uid in self.arbiter._states:
            return
        if engine_id:
            self.engine_ids.add(uid)
        try:
            category = SpeechCategory(event.get("speech_category") or "workflow")
        except ValueError:
            category = SpeechCategory.WORKFLOW  # Urgent alerts must be explicitly categorized.
        if (
            category not in {SpeechCategory.ESSENTIAL, SpeechCategory.ADVICE}
            and self.config.mode != "actions"
        ):
            self._status(uid, "superseded")
            return
        self.spoken(text)
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
        return str(settings.get("chatterbox_voice_override") or "server_default")

    def _catalog(self, settings):
        if not str(settings.get("tts_provider") or "").startswith("chatterbox"):
            return None
        supplied = settings.get("_commentary_voice_catalog")
        if isinstance(supplied, dict):
            return supplied
        endpoint = HOSTED_BASE
        api_key = str(get_settings().get("license_key") or "")
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
                    value = server_voices(api_key=api_key)
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
        if duo and qwen_public.selected(options):
            provider, voices = qwen_public.PROVIDER, qwen_public.PAIR
        preview_lines = {
            "hype": (
                "The attack is on! Can this board keep up the pressure?",
                "That's the tension: commit to the race, or keep resources ready for the next threat.",
            ),
            "technical": (
                "The attack changes the position. What matters over the next few turns?",
                "The balance between pressure and resources. We'll follow how each player's route to winning develops.",
            ),
        }
        texts = (
            preview_lines[normalize_persona(options.get("commentary_persona"))]
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
                excitement=delivery_level(
                    normalize_persona(options.get("commentary_persona")),
                    "color_analyst" if index else "play_by_play",
                    "electric" if duo else "normal",
                ),
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
        private = (
            item.utterance_id.startswith("private-outs:") or item.utterance_id in self.private_commentary_ids
        )
        if item.category == SpeechCategory.COMMENTARY and not private:
            self.spoken(item.text)
        overrides = (
            {"settings_override": self.preview_settings[item.utterance_id]}
            if item.utterance_id in self.preview_settings
            else {}
        )
        speech_text = item.text
        if item.category == SpeechCategory.COMMENTARY and self.observation is not None:
            speech_text = pronounce_players(speech_text, self.observation.public_state.get("player_names"))
        self.tts.request_speech(
            **({"private": True} if private else {}),
            **overrides,
            **({"role": item.role} if item.provider == qwen_public.PROVIDER else {}),
            **self._delivery(item),
            text=speech_text,
            voice_id=item.voice,
            voice_name=self.voice_names.get(item.utterance_id, item.voice),
            speed=item.speed,
            speech_id=item.utterance_id,
            provider=None
            if item.provider == "kokoro"
            and not private
            and item.category != SpeechCategory.ESSENTIAL
            and item.utterance_id not in self.preview_settings
            else item.provider,
            category=item.category.value,
            valid=lambda: (
                self._valid(item) and item.epoch == self.arbiter.epoch and time.monotonic() < item.expires_at
            ),
        )

    def _delivery(self, item):
        from arenamcp.commentary.delivery import speech_delivery

        preview = self.preview_settings.get(item.utterance_id)
        persona = normalize_persona(preview.get("commentary_persona")) if preview else self.config.persona
        return speech_delivery(item, self.observation, persona)

    def _playback_status(self, uid, state):
        item = self.arbiter.active
        if item and item.utterance_id == uid:
            self.arbiter.playback_status(uid, state, epoch=item.epoch)

    def _status(self, uid, state):
        now = time.monotonic()
        reason = self.arbiter.reasons.get(uid, state)
        if state == "accepted":
            self.timings[uid] = {"accepted": now}
        times = self.timings.get(uid, {})
        if state == "started":
            times["started"] = now
        logger.info(
            "Speech lifecycle uid=%s state=%s reason=%s since_accept_seconds=%.3f render_seconds=%.3f",
            uid,
            state,
            reason,
            now - times.get("accepted", now),
            now - times.get("render_start", now),
        )
        if uid in self.engine_ids:
            self.send({"cmd": "speech_status", "speech_id": uid, "state": state})
        if uid in self.commentary_ids:
            self.send({"cmd": "commentary_delivery", "utterance_id": uid, "state": state, "reason": reason})
        if state in TERMINAL_STATES:
            self.timings.pop(uid, None)
            self.voice_names.pop(uid, None)
            preview = self.preview_settings.pop(uid, None)
            if preview and qwen_public.selected(preview) and not self.preview_settings:
                settings = get_settings()
                if not qwen_public.selected(settings) or settings.get("muted", False):
                    self.tts.release_public_voice()
            self.engine_ids.discard(uid)
            self.commentary_ids.discard(uid)
            self.private_commentary_ids.discard(uid)

    def stop(self, reason="user_stop"):
        had_active = self.arbiter.active is not None
        self.arbiter.clear(reason)
        if not had_active:
            self.tts.stop_speech()

    def close(self):
        self.timer.stop()
        self.stop("shutdown")
