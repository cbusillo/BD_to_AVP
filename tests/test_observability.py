import json
import os
import stat
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from bd_to_avp.observability import (
    BoundedEventSink,
    CompositeEventSink,
    ObservabilityContext,
    ObservabilityData,
    ObservabilityEmitter,
    ObservabilityEvent,
    ObservabilityPrivacy,
    ObservabilityProgress,
    ObservabilityRedaction,
    ObservabilitySeverity,
    ObservabilityText,
    RotatingJSONLEventSink,
    bounded_utf8,
)
from bd_to_avp.runtime import ObservabilityStream


FIXTURE = Path(__file__).parent / "fixtures" / "observability_event_v1.json"


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[ObservabilityEvent] = []
        self.lock = threading.Lock()

    def emit(self, event: ObservabilityEvent) -> None:
        with self.lock:
            self.events.append(event)


class FailingSink:
    def emit(self, event: ObservabilityEvent) -> None:
        raise OSError("unavailable")


class ObservabilityEventTests(unittest.TestCase):
    def test_shared_fixture_round_trips_exactly(self) -> None:
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))

        event = ObservabilityEvent.from_dict(fixture)

        self.assertEqual(event.to_dict(), fixture)
        self.assertEqual(json.loads(event.to_json_line()), fixture)

    def test_unknown_kind_is_preserved(self) -> None:
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        fixture["kind"] = "future.tool.signal"

        event = ObservabilityEvent.from_dict(fixture)

        self.assertEqual(event.kind, "future.tool.signal")

    def test_unknown_nested_fields_are_ignored(self) -> None:
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        fixture["context"]["correlation"]["future_field"] = "future-value"
        fixture["context"]["tool"]["future_field"] = "future-value"
        fixture["data"]["progress"]["future_field"] = 42

        event = ObservabilityEvent.from_dict(fixture)

        tool = event.context.tool
        progress = event.data.progress
        self.assertIsNotNone(tool)
        self.assertIsNotNone(progress)
        assert tool is not None
        assert progress is not None
        self.assertEqual(tool.id, "makemkvcon")
        self.assertEqual(progress.fraction, 0.42)

    def test_bounded_utf8_preserves_complete_characters(self) -> None:
        value, truncated = bounded_utf8("🙂🙂", 7)

        self.assertEqual(value, "🙂…")
        self.assertTrue(truncated)
        self.assertLessEqual(len(value.encode("utf-8")), 7)

    def test_secret_text_and_events_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ObservabilityText("token", privacy=ObservabilityPrivacy.SECRET)

        with self.assertRaises(ValueError):
            self.make_event(privacy=ObservabilityPrivacy.SECRET)

    def test_invalid_unicode_and_oversized_direct_text_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ObservabilityText("\ud800")
        with self.assertRaises(ValueError):
            ObservabilityText("x" * (64 * 1024 + 1))

    def test_non_finite_and_boolean_numbers_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ObservabilityProgress(fraction=float("nan"))
        with self.assertRaises(ValueError):
            ObservabilityProgress(total_units=float("inf"))
        with self.assertRaises(ValueError):
            ObservabilityProgress(completed_units=10**400)
        with self.assertRaises(TypeError):
            self.make_event(sequence=True)

    def test_composite_sink_isolates_failures(self) -> None:
        recording = RecordingSink()
        composite = CompositeEventSink(FailingSink(), recording)

        composite.emit(self.make_event())

        self.assertEqual(len(recording.events), 1)
        self.assertEqual(composite.failure_count, 1)

    def test_bounded_sink_drops_oldest_events_and_reports_bytes(self) -> None:
        sink = BoundedEventSink(maximum_events=2, maximum_bytes=100_000)
        for sequence in range(3):
            sink.emit(self.make_event(sequence=sequence))

        snapshot = sink.snapshot()

        self.assertEqual([event.sequence for event in snapshot.events], [1, 2])
        self.assertEqual(snapshot.total_events, 3)
        self.assertEqual(snapshot.dropped_events, 1)
        self.assertGreater(snapshot.dropped_bytes, 0)

    def test_stream_sequences_concurrent_events_without_gaps(self) -> None:
        recording = RecordingSink()
        stream = ObservabilityStream(ObservabilityEmitter.APP, recording)

        def emit_events() -> None:
            for _ in range(100):
                stream.emit("test.event")

        threads = [threading.Thread(target=emit_events) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        sequences = sorted(event.sequence for event in recording.events)
        self.assertEqual(sequences, list(range(800)))
        self.assertEqual([event.sequence for event in recording.events], list(range(800)))

    def test_stream_suppresses_recursive_sink_emission(self) -> None:
        class ReentrantSink:
            stream: ObservabilityStream | None = None

            def emit(self, event: ObservabilityEvent) -> None:
                if self.stream is not None:
                    self.stream.emit("recursive.event")

        sink = ReentrantSink()
        stream = ObservabilityStream(ObservabilityEmitter.APP, sink)
        sink.stream = stream

        event = stream.emit("outer.event")

        self.assertIsNotNone(event)
        self.assertEqual(stream.reentrant_drop_count, 1)

    def test_stream_counts_sink_failures_without_raising(self) -> None:
        stream = ObservabilityStream(ObservabilityEmitter.APP, FailingSink())

        event = stream.emit("test.event")

        self.assertIsNotNone(event)
        self.assertEqual(stream.failure_count, 1)

    def test_bounded_sink_accounts_for_concurrent_eviction(self) -> None:
        sink = BoundedEventSink(maximum_events=20, maximum_bytes=100_000)

        def emit_events(offset: int) -> None:
            for sequence in range(offset, offset + 100):
                sink.emit(self.make_event(sequence=sequence))

        threads = [threading.Thread(target=emit_events, args=(offset,)) for offset in (0, 100, 200, 300)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        snapshot = sink.snapshot()
        self.assertEqual(len(snapshot.events), 20)
        self.assertEqual(snapshot.total_events, 400)
        self.assertEqual(snapshot.dropped_events, 380)
        self.assertEqual(snapshot.failure_count, 0)

    def test_jsonl_sink_rotates_and_uses_private_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory).resolve() / "events.jsonl"
            sink = RotatingJSONLEventSink(path, maximum_bytes=850, backups=1)
            for sequence in range(6):
                sink.emit(self.make_event(sequence=sequence))

            self.assertTrue(path.exists())
            self.assertTrue(path.with_name("events.jsonl.1").exists())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(sink.snapshot().failure_count, 0)

    def test_jsonl_sink_completes_short_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory).resolve() / "events.jsonl"
            sink = RotatingJSONLEventSink(path)
            real_write = os.write

            def short_write(descriptor: int, payload: bytes | memoryview) -> int:
                chunk = bytes(payload[: max(1, len(payload) // 2)])
                return real_write(descriptor, chunk)

            with patch("bd_to_avp.observability.os.write", side_effect=short_write):
                sink.emit(self.make_event())

            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0])["kind"], "test.event")
            self.assertEqual(sink.snapshot().written_events, 1)

    def test_jsonl_sink_rejects_symlink_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory).resolve()
            target = directory / "target.txt"
            target.write_text("original", encoding="utf-8")
            path = directory / "events.jsonl"
            path.symlink_to(target)
            sink = RotatingJSONLEventSink(path)

            sink.emit(self.make_event())

            self.assertEqual(target.read_text(encoding="utf-8"), "original")
            self.assertEqual(sink.snapshot().failure_count, 1)

    def test_jsonl_sink_rejects_hard_link_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory).resolve()
            target = directory / "target.txt"
            target.write_text("original", encoding="utf-8")
            path = directory / "events.jsonl"
            os.link(target, path)
            sink = RotatingJSONLEventSink(path)

            sink.emit(self.make_event())

            self.assertEqual(target.read_text(encoding="utf-8"), "original")
            self.assertEqual(sink.snapshot().failure_count, 1)

    def test_jsonl_sink_rejects_symlinked_parent_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory).resolve()
            real_parent = directory / "real"
            real_parent.mkdir(mode=0o700)
            linked_parent = directory / "linked"
            linked_parent.symlink_to(real_parent, target_is_directory=True)
            sink = RotatingJSONLEventSink(linked_parent / "events.jsonl")

            sink.emit(self.make_event())

            self.assertFalse((real_parent / "events.jsonl").exists())
            self.assertEqual(sink.snapshot().failure_count, 1)

    def test_jsonl_sink_tightens_existing_file_permissions_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory).resolve() / "events.jsonl"
            path.write_text("", encoding="utf-8")
            path.chmod(0o644)
            sink = RotatingJSONLEventSink(path)

            sink.emit(self.make_event())

            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(sink.snapshot().failure_count, 0)

    def test_jsonl_sink_drops_event_larger_than_file_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            sink = RotatingJSONLEventSink(Path(temporary_directory).resolve() / "events.jsonl", maximum_bytes=100)

            sink.emit(self.make_event())

            self.assertEqual(sink.snapshot().dropped_events, 1)
            self.assertGreater(sink.snapshot().dropped_bytes, 100)

    @staticmethod
    def make_event(
        *,
        sequence: int = 0,
        privacy: ObservabilityPrivacy = ObservabilityPrivacy.PRIVATE,
    ) -> ObservabilityEvent:
        return ObservabilityEvent(
            emitter=ObservabilityEmitter.APP,
            stream_id="11111111-1111-4111-8111-111111111111",
            sequence=sequence,
            occurred_at=datetime(2026, 7, 18, 16, tzinfo=timezone.utc),
            elapsed_ms=sequence,
            kind="test.event",
            severity=ObservabilitySeverity.INFO,
            privacy=privacy,
            redaction=ObservabilityRedaction.RAW,
            context=ObservabilityContext(),
            data=ObservabilityData(message=ObservabilityText.bounded("test", privacy=ObservabilityPrivacy.PUBLIC)),
        )


if __name__ == "__main__":
    unittest.main()
