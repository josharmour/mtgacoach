"""Two bug reports in one second must not share (and corrupt) one file.

bug_20261018_185204.json held two user-takeover reports written into the same
``bug_<second>.json`` at once and is unparseable ("Expecting value: line 9597").
"""

from __future__ import annotations

import json
import threading

from arenamcp import standalone_diagnostics as diag


class Engine(diag._DiagnosticsMixin):
    _autopilot = None
    _match_bundle_ctx = None

    class ui:
        @staticmethod
        def log(*_a, **_k):
            pass

    def __init__(self, barrier: threading.Barrier | None = None) -> None:
        self.barrier = barrier

    def _collect_debug_info(self, progress_cb=None, extra_context=None):
        if self.barrier is not None:
            self.barrier.wait()  # both reports reach the write together
        return {"recent_logs": ["x" * 200] * 2000, "context": extra_context}


def test_same_second_reports_each_get_their_own_valid_file(tmp_path, monkeypatch):
    monkeypatch.setattr(diag, "LOG_DIR", tmp_path)
    monkeypatch.setattr(diag, "copy_to_clipboard", lambda _url: False)
    barrier = threading.Barrier(2)
    paths: list = []

    def report(tag: str) -> None:
        paths.append(Engine(barrier).save_bug_report(tag, announce=False, extra_context={"tag": tag}))

    threads = [threading.Thread(target=report, args=(tag,)) for tag in ("first", "second")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(set(paths)) == 2 and all(paths)
    reasons = sorted(json.loads(path.read_text())["reason"] for path in paths)
    assert reasons == ["first", "second"]
    assert not list(tmp_path.glob("bug_reports/*.tmp"))
