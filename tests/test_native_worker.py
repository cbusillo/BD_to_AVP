import io
import json
import signal
import stat
import subprocess
import tempfile
import time
import unittest

from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from bd_to_avp.modules.config import config
from bd_to_avp.worker.__main__ import run_worker
from bd_to_avp.worker.operations import WorkerOperationError, inspect_source
from bd_to_avp.worker.ownership import WorkerProcessOwner
from bd_to_avp.worker.protocol import (
    MAX_EVENT_BYTES,
    MAX_REQUEST_BYTES,
    PROTOCOL_VERSION,
    JobSpec,
    WorkerEventEmitter,
    WorkerEventType,
    WorkerProtocolError,
)


def request_line(source_path: Path, **overrides: object) -> str:
    request: dict[str, object] = {
        "protocol_version": PROTOCOL_VERSION,
        "type": "job.start",
        "job_id": str(uuid4()),
        "operation": "inspect_source",
        "source": {"path": str(source_path)},
    }
    request.update(overrides)
    return json.dumps(request) + "\n"


def decoded_events(output: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in output.getvalue().splitlines()]


class JobSpecTests(unittest.TestCase):
    def test_parses_valid_request(self) -> None:
        source_path = Path("/tmp/movie.m2ts")
        job = JobSpec.from_json_line(request_line(source_path))

        self.assertEqual(job.protocol_version, PROTOCOL_VERSION)
        self.assertEqual(job.operation.value, "inspect_source")
        self.assertEqual(job.source.path, source_path)

    def test_rejects_protocol_mismatch_with_job_id(self) -> None:
        job_id = str(uuid4())
        with self.assertRaises(WorkerProtocolError) as context:
            JobSpec.from_json_line(
                request_line(
                    Path("/tmp/movie.m2ts"),
                    protocol_version=PROTOCOL_VERSION + 1,
                    job_id=job_id,
                )
            )

        self.assertEqual(context.exception.code, "protocol_mismatch")
        self.assertEqual(context.exception.job_id, job_id)

    def test_rejects_boolean_protocol_version(self) -> None:
        with self.assertRaises(WorkerProtocolError) as context:
            JobSpec.from_json_line(request_line(Path("/tmp/movie.m2ts"), protocol_version=True))

        self.assertEqual(context.exception.code, "protocol_mismatch")

    def test_rejects_relative_source_path(self) -> None:
        with self.assertRaises(WorkerProtocolError) as context:
            JobSpec.from_json_line(request_line(Path("movie.m2ts")))

        self.assertEqual(context.exception.code, "source_not_absolute")

    def test_rejects_oversized_request(self) -> None:
        oversized = "{" + (" " * MAX_REQUEST_BYTES) + "}"
        with self.assertRaises(WorkerProtocolError) as context:
            JobSpec.from_json_line(oversized)

        self.assertEqual(context.exception.code, "request_too_large")


class WorkerEventEmitterTests(unittest.TestCase):
    def test_sequences_events_and_stops_after_terminal(self) -> None:
        output = io.StringIO()
        emitter = WorkerEventEmitter(output, str(uuid4()))

        emitter.emit(WorkerEventType.WORKER_READY)
        emitter.emit(WorkerEventType.JOB_COMPLETED, {"result": {"name": "movie"}})

        events = decoded_events(output)
        self.assertEqual([event["sequence"] for event in events], [0, 1])
        self.assertTrue(emitter.terminal_emitted)
        with self.assertRaises(RuntimeError):
            emitter.emit(WorkerEventType.HEARTBEAT)

    def test_oversized_event_does_not_consume_sequence(self) -> None:
        output = io.StringIO()
        emitter = WorkerEventEmitter(output, str(uuid4()))
        emitter.emit(WorkerEventType.WORKER_READY)

        with self.assertRaisesRegex(RuntimeError, "size limit"):
            emitter.emit(WorkerEventType.LOG, {"message": "x" * MAX_EVENT_BYTES})
        emitter.fail("internal_error", "The event could not be encoded.")

        events = decoded_events(output)
        self.assertEqual([event["sequence"] for event in events], [0, 1])
        self.assertEqual(events[-1]["type"], "job.failed")


class WorkerRuntimeTests(unittest.TestCase):
    def test_success_emits_structured_terminal_event_and_redirects_prints(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source_path = Path(temporary_directory) / "movie.m2ts"
            source_path.touch()
            output = io.StringIO()
            diagnostics = io.StringIO()

            def operation(job: JobSpec, _owner: WorkerProcessOwner) -> dict[str, object]:
                print("legacy output")
                self.assertEqual(job.source.path, source_path)
                time.sleep(0.02)
                return {"name": "movie", "resolution": "1920x1080"}

            exit_code = run_worker(
                io.StringIO(request_line(source_path)),
                output,
                diagnostics,
                establish_session=False,
                heartbeat_interval=0.005,
                operation_runner=operation,
            )

        events = decoded_events(output)
        self.assertEqual(exit_code, 0)
        self.assertEqual(events[0]["type"], "worker.ready")
        self.assertEqual(events[-1]["type"], "job.completed")
        self.assertIn("heartbeat", [event["type"] for event in events])
        self.assertNotIn("legacy output", output.getvalue())
        self.assertIn("legacy output", diagnostics.getvalue())

    def test_cancellation_wins_over_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source_path = Path(temporary_directory) / "movie.m2ts"
            source_path.touch()
            output = io.StringIO()

            def operation(_job: JobSpec, owner: WorkerProcessOwner) -> dict[str, object]:
                owner.request_cancel()
                owner.check_cancelled()
                return {"name": "unreachable"}

            exit_code = run_worker(
                io.StringIO(request_line(source_path)),
                output,
                io.StringIO(),
                establish_session=False,
                operation_runner=operation,
            )

        events = decoded_events(output)
        self.assertEqual(exit_code, 130)
        self.assertEqual(events[-1]["type"], "job.cancelled")
        self.assertNotIn("job.completed", [event["type"] for event in events])

    def test_malformed_request_emits_stable_failure(self) -> None:
        output = io.StringIO()

        exit_code = run_worker(io.StringIO("not-json\n"), output, io.StringIO(), establish_session=False)

        events = decoded_events(output)
        self.assertEqual(exit_code, 2)
        self.assertEqual(events[-1]["type"], "job.failed")
        self.assertEqual(events[-1]["payload"]["error"]["code"], "invalid_json")

    def test_oversized_completion_falls_back_to_contiguous_failure(self) -> None:
        source_path = Path("/tmp/movie.m2ts")

        exit_code = run_worker(
            io.StringIO(request_line(source_path)),
            io_output := io.StringIO(),
            io.StringIO(),
            establish_session=False,
            operation_runner=lambda _job, _owner: {"blob": "x" * MAX_EVENT_BYTES},
        )

        events = decoded_events(io_output)
        self.assertEqual(exit_code, 1)
        self.assertEqual([event["sequence"] for event in events], list(range(len(events))))
        self.assertEqual(events[-1]["type"], "job.failed")
        self.assertEqual(events[-1]["payload"]["error"]["code"], "internal_error")


class SourceInspectionTests(unittest.TestCase):
    def test_inspects_direct_video_sources_with_production_probe_path(self) -> None:
        for extension in (".m2ts", ".mkv"):
            with self.subTest(extension=extension), tempfile.TemporaryDirectory() as temporary_directory:
                temporary_path = Path(temporary_directory)
                source_path = temporary_path / f"movie{extension}"
                source_path.write_bytes(b"video")
                fake_ffprobe = temporary_path / "ffprobe"
                fake_ffprobe.write_text(
                    "#!/bin/sh\n"
                    'printf \'%s\\n\' \'{"streams":[{"codec_type":"audio"},'
                    '{"codec_type":"video","width":1920,"height":1080,'
                    '"avg_frame_rate":"24000/1001","field_order":"progressive"}]}\'\n'
                )
                fake_ffprobe.chmod(fake_ffprobe.stat().st_mode | stat.S_IXUSR)

                with patch.object(config, "FFPROBE_PATH", fake_ffprobe):
                    result = inspect_source(source_path, WorkerProcessOwner())

                self.assertEqual(result["name"], "movie")
                self.assertEqual(result["resolution"], "1920x1080")
                self.assertEqual(result["frame_rate"], "24000/1001")
                self.assertFalse(result["interlaced"])
                self.assertEqual(result["size_bytes"], 5)

    def test_rejects_unsupported_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source_path = Path(temporary_directory) / "movie.mp4"
            source_path.touch()

            with self.assertRaisesRegex(WorkerOperationError, "supports MKV, MTS, and M2TS"):
                inspect_source(source_path, WorkerProcessOwner())


class WorkerProcessOwnerTests(unittest.TestCase):
    def test_cancellation_terminates_owned_descendant(self) -> None:
        child = subprocess.Popen(["/bin/sleep", "30"])
        try:
            owner = WorkerProcessOwner()
            owner.request_cancel()

            child.wait(timeout=3)
            self.assertIsNotNone(child.returncode)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=3)

    def test_repeated_signals_do_not_reenter_descendant_cleanup(self) -> None:
        child = subprocess.Popen(["/bin/sh", "-c", "trap '' TERM; exec /bin/sleep 30"])
        try:
            owner = WorkerProcessOwner()

            owner._handle_signal(signal.SIGTERM, None)
            owner._handle_signal(signal.SIGTERM, None)

            self.assertTrue(owner.cancellation_event.is_set())
            self.assertIsNone(child.poll())
            owner.terminate_descendants(timeout=0.1)
            child.wait(timeout=3)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=3)


if __name__ == "__main__":
    unittest.main()
