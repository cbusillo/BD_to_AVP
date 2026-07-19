import io
import os
import threading
import time
import unittest

from bd_to_avp.worker.diagnostics import WorkerDiagnosticRelay


class BlockingTextStream(io.StringIO):
    def __init__(self) -> None:
        super().__init__()
        self.write_started = threading.Event()
        self.release_write = threading.Event()

    def write(self, text: str) -> int:
        self.write_started.set()
        self.release_write.wait(timeout=2)
        return super().write(text)


class FailingTextStream(io.StringIO):
    def write(self, text: str) -> int:
        raise OSError("diagnostic stream unavailable")


class WorkerDiagnosticRelayTests(unittest.TestCase):
    def test_preserves_split_multibyte_utf8(self) -> None:
        stream = io.StringIO()
        relay = WorkerDiagnosticRelay(stream, maximum_chunk_bytes=1)

        relay.emit("stdout", "€\n".encode())
        snapshot = relay.close(timeout=1)

        self.assertEqual(stream.getvalue(), "€\n")
        self.assertEqual(snapshot.written_bytes, len("€\n".encode()))
        self.assertEqual(snapshot.dropped_bytes, 0)
        self.assertEqual(snapshot.failure_count, 0)

    def test_pending_queue_is_bounded_and_drops_oldest_chunks(self) -> None:
        stream = BlockingTextStream()
        relay = WorkerDiagnosticRelay(
            stream,
            maximum_pending_bytes=8,
            maximum_chunk_bytes=4,
        )
        relay.emit("stdout", b"first")
        self.assertTrue(stream.write_started.wait(timeout=1))

        relay.emit("stdout", b"second-third")
        queued = relay.snapshot()
        self.assertLessEqual(queued.pending_bytes, 8)
        self.assertGreater(queued.dropped_bytes, 0)

        stream.release_write.set()
        snapshot = relay.close(timeout=1)
        self.assertEqual(snapshot.pending_bytes, 0)

    def test_partial_tail_truncation_preserves_utf8_boundaries(self) -> None:
        stream = BlockingTextStream()
        relay = WorkerDiagnosticRelay(
            stream,
            maximum_pending_bytes=5,
            maximum_chunk_bytes=5,
        )
        relay.emit("stdout", b"A")
        self.assertTrue(stream.write_started.wait(timeout=1))

        relay.emit("stdout", "🙂X".encode())
        stream.release_write.set()
        snapshot = relay.close(timeout=1)

        self.assertEqual(stream.getvalue(), "AX")
        self.assertEqual(snapshot.dropped_bytes, len("🙂".encode()))
        self.assertNotIn("�", stream.getvalue())

    def test_undrained_file_descriptor_fails_without_blocking_close(self) -> None:
        read_descriptor, write_descriptor = os.pipe()
        stream = os.fdopen(write_descriptor, "w", encoding="utf-8")
        try:
            relay = WorkerDiagnosticRelay(stream)
            relay.emit("stderr", b"x" * (1024 * 1024))

            started = time.monotonic()
            snapshot = relay.close(timeout=1)

            self.assertLess(time.monotonic() - started, 1)
            self.assertEqual(snapshot.failure_count, 1)
            self.assertEqual(snapshot.pending_bytes, 0)
        finally:
            stream.close()
            os.close(read_descriptor)

    def test_close_timeout_reports_in_flight_bytes(self) -> None:
        stream = BlockingTextStream()
        relay = WorkerDiagnosticRelay(stream)
        relay.emit("stdout", b"blocked output")
        self.assertTrue(stream.write_started.wait(timeout=1))

        timed_out = relay.close(timeout=0.01)

        self.assertEqual(timed_out.pending_bytes, len(b"blocked output"))
        stream.release_write.set()
        completed = relay.close(timeout=1)
        self.assertEqual(completed.pending_bytes, 0)

    def test_stream_failure_is_counted_and_future_output_is_dropped(self) -> None:
        relay = WorkerDiagnosticRelay(FailingTextStream())
        relay.emit("stderr", b"first")

        deadline = time.monotonic() + 1
        while relay.snapshot().failure_count == 0 and time.monotonic() < deadline:
            time.sleep(0.01)

        relay.emit("stderr", b"second")
        snapshot = relay.close(timeout=1)

        self.assertEqual(snapshot.failure_count, 1)
        self.assertGreaterEqual(snapshot.dropped_bytes, len(b"second"))


if __name__ == "__main__":
    unittest.main()
