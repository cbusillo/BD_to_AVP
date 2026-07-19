import json
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

from bd_to_avp.observability import (
    ObservabilityContext,
    ObservabilityData,
    ObservabilityEmitter,
    ObservabilityEvent,
    ObservabilityPrivacy,
    ObservabilityProgress,
    ObservabilityRedaction,
    ObservabilitySeverity,
    ObservabilityText,
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

    def test_long_running_and_stalled_fixtures_round_trip_exactly(self) -> None:
        fixture_directory = FIXTURE.parent
        for fixture_name in (
            "observability_long_running_tool_v1.json",
            "observability_stalled_tool_v1.json",
        ):
            with self.subTest(fixture_name=fixture_name):
                fixture = json.loads((fixture_directory / fixture_name).read_text(encoding="utf-8"))

                event = ObservabilityEvent.from_dict(fixture)

                self.assertEqual(event.to_dict(), fixture)

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

    def test_missing_required_correlation_raises_value_error(self) -> None:
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        del fixture["context"]["correlation"]

        with self.assertRaisesRegex(ValueError, "required observability object is missing"):
            ObservabilityEvent.from_dict(fixture)

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
