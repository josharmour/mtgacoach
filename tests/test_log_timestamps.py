"""standalone.log stamps milliseconds, and its parsers read both stamp forms."""

import logging
from datetime import datetime

from arenamcp import logging_config
from arenamcp import match_bundle as mb
from arenamcp.match_review import read_log_slice


def _formatted(level: int, message: str) -> str:
    formatter = logging.Formatter(logging_config.LOG_FORMAT, datefmt=logging_config.LOG_DATEFMT)
    record = logging.LogRecord("arenamcp.test", level, __file__, 1, message, None, None)
    record.created = datetime(2026, 10, 18, 12, 0, 1).timestamp() + 0.042
    record.msecs = 42.0
    return formatter.format(record)


def test_log_lines_carry_milliseconds():
    assert _formatted(logging.INFO, "hello").startswith("2026-10-18 12:00:01.042 | INFO     | arenamcp.test | hello")


def test_coach_log_slice_reads_old_and_new_stamps(tmp_path):
    path = tmp_path / "standalone.log"
    path.write_text(
        "\n".join(
            [
                "2026-10-18 11:59:59 | INFO     | arenamcp.old | before milliseconds",
                _formatted(logging.DEBUG, "dropped"),
                "debug continuation",
                _formatted(logging.INFO, "new stamp"),
                "Traceback (most recent call last):",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    lines = mb.slice_coach_log(path, 0)
    assert lines[0].endswith("before milliseconds")
    assert lines[1].endswith("new stamp") and lines[2] == "Traceback (most recent call last):"
    assert not any("dropped" in line or "continuation" in line for line in lines)


def test_match_review_slices_by_millisecond_stamps(tmp_path):
    path = tmp_path / "standalone.log"
    path.write_text(
        "2026-10-18 11:00:00.500 | INFO     | x | early\n" + _formatted(logging.INFO, "late") + "\n",
        encoding="utf-8",
    )
    assert read_log_slice(path, datetime(2026, 10, 18, 12, 0, 0)).endswith("late")
    assert "early" not in read_log_slice(path, datetime(2026, 10, 18, 12, 0, 0))
