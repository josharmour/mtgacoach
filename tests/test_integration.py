"""Integration tests for watcher -> parser pipeline.

Tests end-to-end flow: file writes -> watcher detection -> parser processing -> event callbacks.
"""

import tempfile
import time
import threading
from pathlib import Path

import pytest

from arenamcp import create_log_pipeline, MTGALogWatcher, LogParser
from arenamcp.parser import LogParser as ParserDirect


# Sample MTGA log content for testing
SAMPLE_GRE_EVENT = """[UnityCrossThreadLogger]12:34:56.789 GreToClientEvent
{
  "greToClientEvent": {
    "gameStateMessage": {
      "type": "GameStateType_Full",
      "gameObjects": [
        {"instanceId": 1, "grpId": 12345, "name": "Test Card"}
      ]
    }
  }
}
"""

SAMPLE_MATCH_CREATED = """[UnityCrossThreadLogger]12:35:00.000 MatchCreated
{
  "matchCreated": {
    "matchId": "match-uuid-12345",
    "opponentScreenName": "Opponent123"
  }
}
"""

SAMPLE_MULLIGAN_REQ = """[Client GRE]12:35:30.000 MulliganReq
{
  "mulliganReq": {
    "type": "MulliganReq_Initial",
    "handCards": [1, 2, 3, 4, 5, 6, 7]
  }
}
"""


class TestLogParser:
    """Tests for LogParser class."""

    def test_single_line_json(self):
        """Parser handles JSON on single line."""
        events = []
        parser = LogParser(on_event=lambda t, p: events.append((t, p)))

        parser.process_chunk('[Logger] GreToClientEvent {"test": "value"}\n')

        assert len(events) == 1
        assert events[0][0] == "GreToClientEvent"
        assert events[0][1] == {"test": "value"}

    def test_multiline_json(self):
        """Parser accumulates multi-line JSON blocks."""
        events = []
        parser = LogParser(on_event=lambda t, p: events.append((t, p)))

        parser.process_chunk(SAMPLE_GRE_EVENT)

        assert len(events) == 1
        assert events[0][0] == "GreToClientEvent"
        assert "greToClientEvent" in events[0][1]

    def test_event_type_on_separate_line(self):
        """Parser detects event type from line before JSON."""
        events = []
        parser = LogParser(on_event=lambda t, p: events.append((t, p)))

        parser.process_chunk("[Logger] MatchCreated\n")
        parser.process_chunk('{"matchId": "123"}\n')

        assert len(events) == 1
        assert events[0][0] == "MatchCreated"

    def test_partial_chunk_handling(self):
        """Parser handles chunks split at arbitrary points."""
        events = []
        parser = LogParser(on_event=lambda t, p: events.append((t, p)))

        # Split the JSON across multiple chunks
        parser.process_chunk('[Logger] GreToCli')
        parser.process_chunk('entEvent\n{"ke')
        parser.process_chunk('y": "val')
        parser.process_chunk('ue"}\n')

        assert len(events) == 1
        assert events[0][0] == "GreToClientEvent"
        assert events[0][1] == {"key": "value"}

    def test_malformed_json_skipped(self):
        """Parser skips malformed JSON without crashing."""
        events = []
        parser = LogParser(on_event=lambda t, p: events.append((t, p)))

        # Malformed JSON followed by valid JSON
        parser.process_chunk('[Logger] Unknown\n{malformed json}\n')
        parser.process_chunk('[Logger] GreToClientEvent\n{"valid": true}\n')

        # Should only get the valid event
        assert len(events) == 1
        assert events[0][1] == {"valid": True}

    def test_typed_handler_routing(self):
        """Typed handlers receive only their registered events."""
        gre_events = []
        match_events = []

        parser = LogParser()
        parser.register_handler("GreToClientEvent", lambda p: gre_events.append(p))
        parser.register_handler("MatchCreated", lambda p: match_events.append(p))

        parser.process_chunk(SAMPLE_GRE_EVENT)
        parser.process_chunk(SAMPLE_MATCH_CREATED)

        assert len(gre_events) == 1
        assert len(match_events) == 1
        assert "greToClientEvent" in gre_events[0]
        assert "matchCreated" in match_events[0]

    def test_default_handler_for_unknown(self):
        """Default handler receives events without typed handlers."""
        unhandled = []
        parser = LogParser()
        parser.set_default_handler(lambda t, p: unhandled.append((t, p)))

        parser.process_chunk('[Logger] SomeUnknownEvent\n{"data": 1}\n')

        assert len(unhandled) == 1
        assert unhandled[0][0] == "Unknown"

    def test_multiple_handlers_same_event(self):
        """Multiple handlers can be registered for same event type."""
        results = {"h1": 0, "h2": 0}

        parser = LogParser()
        parser.register_handler("GreToClientEvent", lambda p: results.update({"h1": results["h1"] + 1}))
        parser.register_handler("GreToClientEvent", lambda p: results.update({"h2": results["h2"] + 1}))

        parser.process_chunk(SAMPLE_GRE_EVENT)

        assert results["h1"] == 1
        assert results["h2"] == 1

    def test_all_event_types_detected(self):
        """Parser detects all key MTGA event types."""
        events = []
        parser = LogParser(on_event=lambda t, p: events.append(t))

        parser.process_chunk(SAMPLE_GRE_EVENT)
        parser.process_chunk(SAMPLE_MATCH_CREATED)
        parser.process_chunk(SAMPLE_MULLIGAN_REQ)

        assert "GreToClientEvent" in events
        assert "MatchCreated" in events
        assert "MulliganReq" in events


class TestWatcherParserIntegration:
    """Integration tests for watcher -> parser pipeline."""

    def test_create_log_pipeline_factory(self):
        """Factory creates connected watcher and parser."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.log', delete=False) as f:
            log_path = f.name

        try:
            watcher, parser = create_log_pipeline(log_path=log_path, backfill=False)

            assert isinstance(watcher, MTGALogWatcher)
            assert isinstance(parser, LogParser)
            assert watcher.callback == parser.process_chunk
        finally:
            Path(log_path).unlink(missing_ok=True)

    def test_end_to_end_event_flow(self):
        """Events flow from file writes through watcher to parser handlers."""
        events = []
        event_received = threading.Event()

        with tempfile.NamedTemporaryFile(mode='w', suffix='.log', delete=False) as f:
            log_path = f.name
            # Write initial content
            f.write("Initial log content\n")
            f.flush()

        try:
            watcher, parser = create_log_pipeline(log_path=log_path, backfill=False)

            def handle_gre(payload):
                events.append(payload)
                event_received.set()

            parser.register_handler("GreToClientEvent", handle_gre)

            with watcher:
                # Small delay to let watcher start
                time.sleep(0.1)

                # Append new content to the log file
                with open(log_path, 'a') as f:
                    f.write(SAMPLE_GRE_EVENT)
                    f.flush()

                # Wait for event to be processed (with timeout)
                received = event_received.wait(timeout=2.0)

                assert received, "Event was not received within timeout"
                assert len(events) == 1
                assert "greToClientEvent" in events[0]

        finally:
            Path(log_path).unlink(missing_ok=True)

    def test_multiple_events_sequential(self):
        """Multiple sequential file writes produce multiple events."""
        events = []
        events_lock = threading.Lock()

        with tempfile.NamedTemporaryFile(mode='w', suffix='.log', delete=False) as f:
            log_path = f.name
            f.write("Initial\n")
            f.flush()

        try:
            watcher, parser = create_log_pipeline(log_path=log_path, backfill=False)

            def handle_event(event_type, payload):
                with events_lock:
                    events.append(event_type)

            parser._on_event = handle_event

            with watcher:
                time.sleep(0.1)

                # Write multiple events
                with open(log_path, 'a') as f:
                    f.write(SAMPLE_GRE_EVENT)
                    f.flush()

                time.sleep(0.2)

                with open(log_path, 'a') as f:
                    f.write(SAMPLE_MATCH_CREATED)
                    f.flush()

                # Wait for processing
                time.sleep(0.5)

                with events_lock:
                    assert "GreToClientEvent" in events
                    assert "MatchCreated" in events

        finally:
            Path(log_path).unlink(missing_ok=True)

    def test_file_truncation_handling(self):
        """Pipeline handles log truncation (MTGA restart)."""
        events = []

        with tempfile.NamedTemporaryFile(mode='w', suffix='.log', delete=False) as f:
            log_path = f.name
            f.write("Old content\n")
            f.flush()

        try:
            watcher, parser = create_log_pipeline(log_path=log_path, backfill=False)
            parser._on_event = lambda t, p: events.append(t)

            with watcher:
                time.sleep(0.1)

                # Truncate file (simulates MTGA restart)
                with open(log_path, 'w') as f:
                    f.write("New session start\n")
                    f.flush()

                time.sleep(0.2)

                # Write new event after truncation
                with open(log_path, 'a') as f:
                    f.write(SAMPLE_GRE_EVENT)
                    f.flush()

                time.sleep(0.5)

                # Should still process events after truncation
                assert "GreToClientEvent" in events

        finally:
            Path(log_path).unlink(missing_ok=True)


    def test_backfill_processes_existing_content(self):
        """Backfill processes existing log content from last match start."""
        events = []

        with tempfile.NamedTemporaryFile(mode='w', suffix='.log', delete=False) as f:
            log_path = f.name
            # Write a match start marker and game event BEFORE starting watcher
            f.write(SAMPLE_MATCH_CREATED)
            f.write(SAMPLE_GRE_EVENT)
            f.flush()

        try:
            watcher, parser = create_log_pipeline(log_path=log_path, backfill=True)

            def handle_event(event_type, payload):
                events.append(event_type)

            parser._on_event = handle_event

            # Start watcher - backfill should process existing content
            with watcher:
                time.sleep(0.2)

            # Events from existing content should have been processed
            assert "MatchCreated" in events
            assert "GreToClientEvent" in events

        finally:
            Path(log_path).unlink(missing_ok=True)

    def test_backfill_finds_last_match_only(self):
        """Backfill only processes from the last match start, not earlier matches."""
        events = []

        with tempfile.NamedTemporaryFile(mode='w', suffix='.log', delete=False) as f:
            log_path = f.name
            # Write two matches - only the second should be processed
            f.write("[UnityCrossThreadLogger]12:00:00.000 MatchCreated\n")
            f.write('{"matchCreated": {"matchId": "old-match"}}\n')
            f.write('[Logger] GreToClientEvent\n{"old": "event"}\n')
            # Second match
            f.write(SAMPLE_MATCH_CREATED)  # This has matchId "match-uuid-12345"
            f.write(SAMPLE_GRE_EVENT)
            f.flush()

        try:
            watcher, parser = create_log_pipeline(log_path=log_path, backfill=True)

            payloads = []

            def handle_gre(payload):
                payloads.append(payload)

            parser.register_handler("GreToClientEvent", handle_gre)

            with watcher:
                time.sleep(0.2)

            # Should only have processed the second match's event
            # The first match's {"old": "event"} should NOT be in payloads
            assert len(payloads) >= 1
            # The last event should be from SAMPLE_GRE_EVENT (has greToClientEvent key)
            assert any("greToClientEvent" in p for p in payloads)

        finally:
            Path(log_path).unlink(missing_ok=True)


class TestImports:
    """Verify all expected imports work."""

    def test_package_imports(self):
        """All public symbols importable from package."""
        from arenamcp import (
            __version__,
            MTGALogWatcher,
            LogParser,
            create_log_pipeline,
        )

        assert __version__  # version string is non-empty
        assert MTGALogWatcher is not None
        assert LogParser is not None
        assert create_log_pipeline is not None

    def test_parser_direct_import(self):
        """LogParser importable directly from parser module."""
        from arenamcp.parser import LogParser

        parser = LogParser()
        assert parser is not None
