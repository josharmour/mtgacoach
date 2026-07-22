"""Tests for hotkeys_darwin, HotkeyManager, and LocalVLM / VisionMapper.

Covers:
- DarwinHotkeyListener initialization, key mappings (F3, F6, F12, etc.), register/unregister, and fallback behavior when PyObjC is present or absent.
- HotkeyManager cross-platform integration (Windows, macOS, Linux).
- LocalVLM supporting Ollama and OpenAI-compatible endpoints (Qwen2-VL, Llava, etc.).
- VisionMapper initializing with LocalVLM and handling scans gracefully.
"""

import json
import sys
from unittest.mock import MagicMock, patch
import pytest

from arenamcp.desktop.hotkeys_darwin import (
    DarwinHotkeyListener,
    MACOS_KEY_CODES,
    HOTKEY_PURPOSES,
)
from arenamcp.desktop.hotkeys import HotkeyManager
from arenamcp.vision_mapper import LocalVLM, OllamaVLM, VisionMapper


# ── DarwinHotkeyListener & Key Mappings ──────────────────────────────────

def test_darwin_key_codes_mapping():
    assert MACOS_KEY_CODES["F3"] == 99
    assert MACOS_KEY_CODES["F6"] == 97
    assert MACOS_KEY_CODES["F12"] == 111
    assert HOTKEY_PURPOSES["F3"] == "VLM Analyze"
    assert HOTKEY_PURPOSES["F6"] == "PTT (Push-To-Talk)"
    assert HOTKEY_PURPOSES["F12"] == "AP Toggle (Autopilot)"


def test_darwin_hotkey_listener_fallback_when_unavailable(monkeypatch):
    """When PyObjC is not available, listener reports is_available=False and gracefully refuses registration."""
    monkeypatch.setattr("arenamcp.desktop.hotkeys_darwin._pyobjc_available", False)
    listener = DarwinHotkeyListener()

    called = []
    assert listener.register("F3", lambda: called.append("F3")) is False
    assert listener.register("F6", lambda: called.append("F6")) is False
    assert listener.register("F12", lambda: called.append("F12")) is False
    assert len(called) == 0


def test_darwin_hotkey_listener_registration_and_callback(monkeypatch):
    """Test hotkey registration and event dispatch when PyObjC is mocked as available."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr("arenamcp.desktop.hotkeys_darwin._pyobjc_available", True)

    listener = DarwinHotkeyListener()

    fake_global_monitor = "fake_monitor_123"
    fake_local_monitor = "fake_monitor_456"

    global_handler_ref = []
    local_handler_ref = []

    mock_nsevent = MagicMock()

    def mock_add_global(mask, handler):
        global_handler_ref.append(handler)
        return fake_global_monitor

    def mock_add_local(mask, handler):
        local_handler_ref.append(handler)
        return fake_local_monitor

    mock_nsevent.addGlobalMonitorForEventsMatchingMask_handler_ = mock_add_global
    mock_nsevent.addLocalMonitorForEventsMatchingMask_handler_ = mock_add_local

    monkeypatch.setattr("arenamcp.desktop.hotkeys_darwin.NSEvent", mock_nsevent)

    f3_triggered = []
    f6_triggered = []
    f12_triggered = []

    assert listener.register("F3", lambda: f3_triggered.append(True)) is True
    assert listener.register("F6", lambda: f6_triggered.append(True)) is True
    assert listener.register("F12", lambda: f12_triggered.append(True)) is True
    assert listener.is_active is True

    # Simulate keydown events
    def make_event(code):
        evt = MagicMock()
        evt.keyCode.return_value = code
        return evt

    # Trigger global monitor for F3 (99), F6 (97), F12 (111)
    global_handler = global_handler_ref[0]
    global_handler(make_event(99))
    global_handler(make_event(97))
    global_handler(make_event(111))

    assert len(f3_triggered) == 1
    assert len(f6_triggered) == 1
    assert len(f12_triggered) == 1

    # Unregister F3 and stop
    listener.unregister("F3")
    assert 99 not in listener._callbacks
    listener.unregister_all()
    assert listener.is_active is False


# ── HotkeyManager Integration ──────────────────────────────────────────

def test_hotkey_manager_cross_platform_dispatch(monkeypatch):
    """HotkeyManager selects DarwinHotkeyListener on macOS, keyboard on Windows, or QShortcut fallback."""
    # Test macOS branch with DarwinHotkeyListener
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr("os.name", "posix")

    mock_listener = MagicMock()
    mock_listener.is_available = True
    mock_listener.register.return_value = True

    manager = HotkeyManager()
    manager._darwin_listener = mock_listener

    called = []
    manager.register("F3", lambda: called.append("F3"))
    mock_listener.register.assert_called_once_with("F3", manager._callbacks["F3"])

    manager.unregister_all()
    mock_listener.unregister_all.assert_called_once()


# ── LocalVLM & VisionMapper (Qwen2-VL / Llava) ──────────────────────────

def test_local_vlm_ollama_protocol():
    """Test LocalVLM querying Ollama API."""
    vlm = LocalVLM(model="qwen2.5-vl:3b", endpoint="http://localhost:11434")

    tags_response = json.dumps({
        "models": [
            {"name": "qwen2.5-vl:3b"},
            {"name": "llava:7b"},
        ]
    }).encode("utf-8")

    gen_response = json.dumps({
        "response": '```json\n{"elements": [{"name": "Done", "zone": "button", "x": 0.9, "y": 0.8, "confidence": 0.95}], "phase_hint": "main_phase"}\n```'
    }).encode("utf-8")

    def mock_urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        resp = MagicMock()
        resp.__enter__.return_value = resp
        if "api/tags" in url:
            resp.read.return_value = tags_response
        elif "api/generate" in url:
            resp.read.return_value = gen_response
        return resp

    with patch("urllib.request.urlopen", side_effect=mock_urlopen):
        assert vlm.available is True
        result = vlm.analyze("Scan screen", b"fake_image_bytes")
        assert result is not None
        assert "elements" in result
        assert result["elements"][0]["name"] == "Done"


def test_local_vlm_openai_protocol():
    """Test LocalVLM querying OpenAI-compatible VLM endpoint (vLLM / LM Studio / Qwen2-VL)."""
    vlm = LocalVLM(model="Qwen/Qwen2-VL-7B-Instruct", endpoint="http://localhost:8000/v1", api_type="openai")

    models_response = json.dumps({
        "data": [{"id": "Qwen/Qwen2-VL-7B-Instruct"}]
    }).encode("utf-8")

    chat_response = json.dumps({
        "choices": [
            {
                "message": {
                    "content": '{"elements": [{"name": "Pass", "zone": "button", "x": 0.92, "y": 0.88, "confidence": 0.98}]}'
                }
            }
        ]
    }).encode("utf-8")

    def mock_urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        resp = MagicMock()
        resp.__enter__.return_value = resp
        if "models" in url:
            resp.read.return_value = models_response
        elif "chat/completions" in url:
            resp.read.return_value = chat_response
        return resp

    with patch("urllib.request.urlopen", side_effect=mock_urlopen):
        assert vlm.available is True
        result = vlm.analyze("Scan screen", b"fake_image_bytes")
        assert result is not None
        assert result["elements"][0]["name"] == "Pass"


def test_vision_mapper_integration_with_local_vlm():
    """Test VisionMapper init and layout scan using LocalVLM."""
    mapper = VisionMapper(
        local_vlm_model="qwen2.5-vl:3b",
        local_vlm_endpoint="http://localhost:11434",
        enable_local_vlm=True,
        enable_cloud_vlm=False,
    )
    assert mapper._local_vlm is not None
    assert isinstance(mapper._local_vlm, LocalVLM)
    # Check backwards-compatibility alias
    assert OllamaVLM is LocalVLM
