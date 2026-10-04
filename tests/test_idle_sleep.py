from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest

from arenamcp import idle_sleep


@pytest.fixture
def helper(monkeypatch):
    process = Mock()
    process.poll.return_value = None
    process.wait.return_value = 0
    spawn = Mock(return_value=process)
    monkeypatch.setattr(idle_sleep.subprocess, "Popen", spawn)
    monkeypatch.setattr(idle_sleep.sys, "platform", "darwin")
    monkeypatch.setattr(idle_sleep.os, "getpid", lambda: 1234)
    return process, spawn


def test_start_is_idempotent_and_assertion_is_scoped_to_coach_pid(helper):
    process, spawn = helper
    inhibitor = idle_sleep.SleepInhibitor()
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert all(pool.map(lambda _: inhibitor.start(), range(8)))
    spawn.assert_called_once_with(
        ["/usr/bin/caffeinate", "-di", "-w", "1234"],
        stdin=idle_sleep.subprocess.DEVNULL,
        stdout=idle_sleep.subprocess.DEVNULL,
        stderr=idle_sleep.subprocess.DEVNULL,
        start_new_session=True,
    )
    inhibitor.stop()
    inhibitor.stop()
    process.terminate.assert_called_once()
    process.wait.assert_called_once_with(timeout=1.0)
    process.kill.assert_not_called()
    assert inhibitor._process is None


def test_start_replaces_an_exited_helper(helper):
    process, spawn = helper
    inhibitor = idle_sleep.SleepInhibitor()
    assert inhibitor.start()
    process.poll.return_value = 0
    replacement = Mock()
    replacement.poll.return_value = None
    spawn.return_value = replacement
    assert inhibitor.start()
    assert spawn.call_count == 2
    assert inhibitor._process is replacement


def test_stop_kills_only_retained_helper_after_termination_timeout(helper):
    process, _ = helper
    process.wait.side_effect = [idle_sleep.subprocess.TimeoutExpired("caffeinate", 1), 0]
    inhibitor = idle_sleep.SleepInhibitor()
    inhibitor.start()
    inhibitor.stop()
    process.terminate.assert_called_once()
    process.kill.assert_called_once()
    assert process.wait.call_count == 2
    assert inhibitor._process is None


def test_failed_stop_retains_handle_for_retry_without_duplicate(helper):
    process, spawn = helper
    process.terminate.side_effect = OSError("temporary failure")
    inhibitor = idle_sleep.SleepInhibitor()
    inhibitor.start()
    inhibitor.stop()
    assert inhibitor._process is process
    assert inhibitor.start()
    spawn.assert_called_once()
    process.terminate.side_effect = None
    inhibitor.stop()
    assert inhibitor._process is None


def test_missing_helper_is_nonfatal(helper):
    _, spawn = helper
    spawn.side_effect = FileNotFoundError("caffeinate missing")
    inhibitor = idle_sleep.SleepInhibitor()
    assert not inhibitor.start()
    inhibitor.stop()
    assert inhibitor._process is None


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_non_mac_does_not_spawn_or_change_power_settings(helper, monkeypatch, platform):
    _, spawn = helper
    monkeypatch.setattr(idle_sleep.sys, "platform", platform)
    inhibitor = idle_sleep.SleepInhibitor()
    assert not inhibitor.start()
    inhibitor.stop()
    spawn.assert_not_called()
