"""A paused update and a user reopening Coach must never own the runtime together."""

import os
import subprocess
import sys

import pytest

fcntl = pytest.importorskip("fcntl")
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX update coordination")

PROBE = """
from arenamcp.desktop import app
from arenamcp.desktop import runtime
from pathlib import Path
import sys
runtime.get_runtime_root = lambda: Path(sys.argv[1])
acquired = app._acquire_posix_instance_lock()
print(int(acquired), flush=True)
if acquired:
    sys.stdin.readline()
    app._release_single_instance_lock()
"""


def test_update_lock_blocks_reopened_coach_until_verification_finishes(tmp_path):
    with (tmp_path / "desktop.lock").open("a+") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = subprocess.run(
            [sys.executable, "-c", PROBE, str(tmp_path)],
            input="\n",
            text=True,
            capture_output=True,
            timeout=15,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            check=True,
        )
        assert result.stdout.strip() == "0"
    result = subprocess.run(
        [sys.executable, "-c", PROBE, str(tmp_path)],
        input="\n",
        text=True,
        capture_output=True,
        timeout=15,
        check=True,
    )
    assert result.stdout.strip() == "1"


def test_reopened_coach_wins_before_update_without_lock_takeover(tmp_path):
    process = subprocess.Popen(
        [sys.executable, "-c", PROBE, str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        # Only this temporary test child is controlled; no installed app is launched.
        import select

        assert select.select([process.stdout], [], [], 15)[0]
        assert process.stdout.readline().strip() == "1"
        with (tmp_path / "desktop.lock").open("a+") as guard:
            with pytest.raises(BlockingIOError):
                fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert process.poll() is None
        process.communicate("\n", timeout=15)
        assert process.returncode == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
