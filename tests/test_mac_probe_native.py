"""Compile and execute the real C++ probe batch engine with mocked IL2CPP APIs."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(sys.platform != "darwin", reason="probe.cpp uses macOS Mach APIs")
def test_native_probe_run_batch(tmp_path):
    compiler = shutil.which("clang++")
    if compiler is None:
        pytest.skip("native probe harness requires clang++")
    source = Path(__file__).resolve().parents[1] / "spikes/mac-il2cpp/tests/probe_batch_test.cpp"
    executable = tmp_path / "probe_batch_test"
    compiled = subprocess.run(
        [compiler, "-std=c++17", "-O1", "-g", str(source), "-o", str(executable)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    # The test-only constructor guard prevents startup even if inherited from a
    # probe debugging shell. Strip injection variables before launching as well.
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"DYLD_INSERT_LIBRARIES", "LD_PRELOAD", "MTGACOACH_PROBE_ANY_PROCESS"}
    }
    result = subprocess.run(
        [str(executable)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.count("PASS ") == 10, result.stdout
    assert (
        "PASS new readonly struct ctor then Add(REF): payload, header identity and rooting" in result.stdout
    )
    assert "PASS value instance, inherited boxed, static and reference ctor receivers" in result.stdout
