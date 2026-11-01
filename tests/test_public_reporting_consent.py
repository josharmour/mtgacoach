"""Regression coverage for public GitHub reporting consent (#709).

Automatic diagnostics may save locally forever; they must never publish to
GitHub on their own.  A user-initiated report may publish only after an
explicit confirmation that shows the destination and the exact contents.
Cancelling that confirmation must not even read a credential.

Every network/subprocess/browser path here is mocked.  The dummy tokens are
set only inside the test process.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import webbrowser
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from arenamcp.bugreport import GITHUB_REPO
from arenamcp.pipe_adapter import PipeAdapter
from arenamcp.standalone_diagnostics import _DiagnosticsMixin
from arenamcp.standalone_postmatch import _PostMatchMixin


class _FakeResponse:
    ok = True
    status_code = 201
    text = ""

    def json(self):
        return {
            "html_url": f"https://github.com/{GITHUB_REPO}/issues/1",
            "number": 1,
        }


class _UI:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def log(self, message: str) -> None:
        self.lines.append(str(message))

    def error(self, message: str) -> None:
        self.lines.append(str(message))

    def status(self, *_args, **_kwargs) -> None:
        pass


class _ImmediateThread:
    """Run the target synchronously so a leaked upload cannot hide in a thread."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self) -> None:
        if self._target is not None:
            self._target(*self._args, **self._kwargs)


class _DummyDiagnostics(_DiagnosticsMixin):
    def __init__(self, tmp_path: Path) -> None:
        self.ui = _UI()
        self.settings: dict = {}
        self.saved: list[Path] = []
        self.token_calls = 0
        self._tmp_path = tmp_path

    def _get_github_token_for_auto_bug(self) -> str:
        self.token_calls += 1
        return "dummy-token"

    def save_bug_report(
        self,
        reason: str = "",
        *,
        announce: bool = True,
        progress_cb=None,
        extra_context=None,
    ) -> Path:
        path = self._tmp_path / f"bug_{len(self.saved)}.json"
        path.write_text(
            json.dumps({"reason": reason, "extra_context": extra_context or {}}),
            encoding="utf-8",
        )
        self.saved.append(path)
        return path


class _DummyPostMatch(_PostMatchMixin):
    def __init__(self, tmp_path: Path) -> None:
        self.ui = _UI()
        self.settings: dict = {}
        self.token_calls = 0
        self._tmp_path = tmp_path

    def _get_github_token_for_auto_bug(self) -> str:
        self.token_calls += 1
        return "dummy-token"


class _DummyCoach:
    def __init__(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path

    def save_bug_report(
        self,
        reason: str = "",
        *,
        announce: bool = True,
        progress_cb=None,
        extra_context=None,
    ) -> Path:
        path = self._tmp_path / "bug_reports" / "bug_test.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "timestamp": "2026-10-29T12:00:00",
                    "version": "test",
                    "reason": reason,
                    "extra_context": extra_context or {},
                }
            ),
            encoding="utf-8",
        )
        return path


@pytest.mark.parametrize("share_match_logs", [None, False, True])
def test_automatic_bridge_fallback_saves_locally_and_never_posts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    share_match_logs,
) -> None:
    """A bridge miss is automatic telemetry — local only at every setting."""
    monkeypatch.setenv("MTGACOACH_GITHUB_TOKEN", "dummy-token")
    posts: list[tuple] = []
    monkeypatch.setattr(
        requests,
        "post",
        lambda *args, **kwargs: posts.append((args, kwargs)) or _FakeResponse(),
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("automatic bridge fallback ran a subprocess"),
    )
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("automatic bridge fallback launched a subprocess"),
    )
    monkeypatch.setattr(threading, "Thread", _ImmediateThread)

    coach = _DummyDiagnostics(tmp_path)
    if share_match_logs is not None:
        coach.settings["share_match_logs"] = share_match_logs

    coach._auto_bug_report_bridge_fallback(
        "auto: bridge fallback (planner_action_stale)",
        {"auto_fallback_bug": {"reason_tag": "planner_action_stale"}},
    )

    assert len(coach.saved) == 1
    assert coach.token_calls == 0
    assert posts == []


@pytest.mark.parametrize("share_match_logs", [None, False, True])
def test_post_match_review_saves_locally_and_never_posts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    share_match_logs,
) -> None:
    """High-severity findings are evidence for a human decision, not consent."""
    import arenamcp.match_review as match_review

    posts: list[tuple] = []
    saved_reviews: list[tuple] = []
    monkeypatch.setattr(
        requests,
        "post",
        lambda *args, **kwargs: posts.append((args, kwargs)) or _FakeResponse(),
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("post-match review ran a subprocess"),
    )
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("post-match review launched a subprocess"),
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(match_review, "read_log_slice", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(
        match_review,
        "run_match_review",
        lambda **_kwargs: [SimpleNamespace(severity="high", category="test", title="finding")],
    )
    monkeypatch.setattr(
        match_review,
        "save_review",
        lambda *args, **kwargs: saved_reviews.append((args, kwargs)) or tmp_path / "review.json",
    )
    monkeypatch.setattr(match_review, "should_file_issue", lambda _findings: True)
    monkeypatch.setattr(
        match_review, "build_issue", lambda *_args, **_kwargs: ("Review title", "Review body")
    )

    coach = _DummyPostMatch(tmp_path)
    if share_match_logs is not None:
        coach.settings["share_match_logs"] = share_match_logs

    coach._run_match_review("match-1", "win", [])

    assert saved_reviews
    assert coach.token_calls == 0
    assert posts == []


@pytest.mark.parametrize("share_match_logs", [None, False, True])
def test_vision_watchdog_saves_locally_without_filing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    share_match_logs,
) -> None:
    """The vision watchdog is automatic; it must not invoke gh issue create."""
    runs: list[tuple] = []
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: (
            runs.append((args, kwargs)) or SimpleNamespace(returncode=1, stdout="", stderr="label not found")
        ),
    )
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("vision watchdog launched a subprocess"),
    )
    monkeypatch.setattr("arenamcp.standalone_postmatch.LOG_DIR", tmp_path)

    coach = _DummyPostMatch(tmp_path)
    if share_match_logs is not None:
        coach.settings["share_match_logs"] = share_match_logs
    coach._offer_missed_decision_issue([{"decision_type": "mulligan", "timestamp": "t"}])

    assert runs == []
    assert coach.token_calls == 0
    saved = list((tmp_path / "bug_reports").glob("missed_decisions_*.json"))
    assert len(saved) == 1


def _adapter_with_coach(tmp_path: Path) -> tuple[PipeAdapter, list[dict], list[str]]:
    adapter = PipeAdapter()
    adapter._coach = _DummyCoach(tmp_path)
    events: list[dict] = []
    logs: list[str] = []
    adapter._emit = events.append  # type: ignore[method-assign]
    adapter.log = logs.append  # type: ignore[method-assign]
    adapter.error = logs.append  # type: ignore[method-assign]
    return adapter, events, logs


def _confirmation_request(events: list[dict]) -> dict:
    requests_seen = [event for event in events if event.get("type") == "bug_report_confirmation_request"]
    assert len(requests_seen) == 1
    return requests_seen[0]


def test_bugreport_cancel_reads_no_credential_and_submits_nothing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Cancel is a hard stop before any credential lookup or network call."""
    adapter, events, _logs = _adapter_with_coach(tmp_path)
    monkeypatch.setattr(threading, "Thread", _ImmediateThread)
    token_calls: list[int] = []
    posts: list[tuple] = []
    runs: list[tuple] = []
    clipboard: list[str] = []

    monkeypatch.setattr("arenamcp.logging_config.LOG_DIR", tmp_path)
    monkeypatch.setattr(
        adapter,
        "_get_github_token",
        lambda: token_calls.append(1) or "dummy-token",
    )
    monkeypatch.setattr(
        adapter,
        "_copy_to_clipboard",
        lambda text: clipboard.append(text) or True,
    )
    monkeypatch.setattr(
        requests,
        "post",
        lambda *args, **kwargs: posts.append((args, kwargs)) or _FakeResponse(),
    )
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: runs.append((args, kwargs)))
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("bugreport launched a clipboard subprocess"),
    )
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(webbrowser, "open", lambda *args, **kwargs: runs.append((args, kwargs)))

    adapter._handle_bugreport("something broke")

    request_event = _confirmation_request(events)
    assert request_event["destination"].startswith(f"https://github.com/{GITHUB_REPO}")
    assert request_event["title"]
    assert request_event["body"]
    assert request_event["report_path"]

    # Nothing may have been read or sent while the user is still deciding.
    assert token_calls == []
    assert posts == []
    assert runs == []
    assert clipboard == []

    adapter._dispatch({"cmd": "cancel_bugreport", "request_id": request_event["request_id"]})

    assert token_calls == []
    assert posts == []
    assert runs == []
    assert clipboard == []


def test_confirmed_bugreport_submits_exactly_the_reviewed_contents(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Only an explicit confirmation may publish, and it publishes what was shown."""
    adapter, events, _logs = _adapter_with_coach(tmp_path)
    monkeypatch.setattr(threading, "Thread", _ImmediateThread)
    token_calls: list[int] = []
    posts: list[tuple] = []
    runs: list[tuple] = []
    clipboard: list[str] = []

    monkeypatch.setattr("arenamcp.logging_config.LOG_DIR", tmp_path)
    monkeypatch.setattr(
        adapter,
        "_get_github_token",
        lambda: token_calls.append(1) or "dummy-token",
    )
    monkeypatch.setattr(
        adapter,
        "_copy_to_clipboard",
        lambda text: clipboard.append(text) or True,
    )
    monkeypatch.setattr(
        requests,
        "post",
        lambda *args, **kwargs: posts.append((args, kwargs)) or _FakeResponse(),
    )
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: runs.append((args, kwargs)))
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("bugreport launched a clipboard subprocess"),
    )
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(webbrowser, "open", lambda *args, **kwargs: runs.append((args, kwargs)))

    adapter._handle_bugreport("something broke")

    request_event = _confirmation_request(events)
    assert posts == []
    assert token_calls == []

    adapter._dispatch({"cmd": "confirm_bugreport", "request_id": request_event["request_id"]})

    assert token_calls == [1]
    assert len(posts) == 1
    args, kwargs = posts[0]
    assert args[0] == f"https://api.github.com/repos/{GITHUB_REPO}/issues"
    payload = kwargs["json"]
    assert payload["title"] == request_event["title"]
    assert payload["body"] == request_event["body"]
    assert payload["labels"] == ["bug"]
    assert runs == []


def test_bugreport_cancel_repeat_and_unknown_ids_never_submit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A consumed/cancelled/unknown request id can never trigger a submission."""
    adapter, events, _logs = _adapter_with_coach(tmp_path)
    monkeypatch.setattr(threading, "Thread", _ImmediateThread)
    token_calls: list[int] = []
    posts: list[tuple] = []
    runs: list[tuple] = []
    clipboard: list[str] = []

    monkeypatch.setattr(
        adapter,
        "_get_github_token",
        lambda: token_calls.append(1) or "dummy-token",
    )
    monkeypatch.setattr(
        adapter,
        "_copy_to_clipboard",
        lambda text: clipboard.append(text) or True,
    )
    monkeypatch.setattr(
        requests,
        "post",
        lambda *args, **kwargs: posts.append((args, kwargs)) or _FakeResponse(),
    )
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: runs.append((args, kwargs)))
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("bugreport launched a clipboard subprocess"),
    )
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(webbrowser, "open", lambda *args, **kwargs: runs.append((args, kwargs)))

    adapter._handle_bugreport("first")
    first = _confirmation_request(events)

    # Cancel consumes the id; a later confirm of the same id must do nothing.
    adapter._dispatch({"cmd": "cancel_bugreport", "request_id": first["request_id"]})
    adapter._dispatch({"cmd": "confirm_bugreport", "request_id": first["request_id"]})
    assert token_calls == []
    assert posts == []
    assert runs == []
    assert clipboard == []

    # Unknown ids are inert for both commands.
    adapter._dispatch({"cmd": "confirm_bugreport", "request_id": "not-a-real-id"})
    adapter._dispatch({"cmd": "cancel_bugreport", "request_id": "not-a-real-id"})
    assert token_calls == []
    assert posts == []
    assert runs == []

    # A confirmed id is consumed exactly once.
    adapter._handle_bugreport("second")
    second = [e for e in events if e.get("type") == "bug_report_confirmation_request"][-1]
    adapter._dispatch({"cmd": "confirm_bugreport", "request_id": second["request_id"]})
    adapter._dispatch({"cmd": "confirm_bugreport", "request_id": second["request_id"]})
    assert token_calls == [1]
    assert len(posts) == 1
    assert runs == []
