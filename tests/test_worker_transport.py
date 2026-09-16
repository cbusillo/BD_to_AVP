import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import psutil

from bd_to_avp.worker.__main__ import TRANSPORT_FAILURE_EXIT_CODE, run_worker
from bd_to_avp.worker.controls import WorkerControls, WorkerInputReader
from bd_to_avp.worker.diagnostics import WorkerDiagnosticRelay, WorkerDiagnosticSnapshot
from bd_to_avp.worker.ownership import WorkerProcessOwner
from bd_to_avp.worker.protocol import (
    JobSpec,
    WorkerActivityReporter,
    WorkerEventEmitter,
    WorkerEventTransportError,
    WorkerEventType,
)
from tests.test_native_worker import request_line


def fill_pipe(descriptor: int) -> None:
    was_blocking = os.get_blocking(descriptor)
    try:
        os.set_blocking(descriptor, False)
        while True:
            try:
                os.write(descriptor, b"x" * 512)
            except BlockingIOError:
                return
    finally:
        os.set_blocking(descriptor, was_blocking)


class WorkerEventTransportTests(unittest.TestCase):
    def test_heartbeat_transport_failure_stops_healthy_running_child(self) -> None:
        self._assert_background_failure_stops_worker("heartbeat")

    def test_control_reader_transport_failure_stops_healthy_running_child(self) -> None:
        self._assert_background_failure_stops_worker("control")

    def _assert_background_failure_stops_worker(self, origin: str) -> None:
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "child.pid"
            growing_output = Path(directory) / "growing.mov"
            script = """
import pathlib, sys
from bd_to_avp.process_runner import ChildProcessRunner, ProcessArtifactProbe, ProcessCancelled, ProcessSpec
from bd_to_avp.worker.__main__ import run_worker
child_code = (
    'import os,pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); '
    'output=open(sys.argv[2], "ab", buffering=0)\\n'
    'while True: output.write(b"x"*4096); time.sleep(.01)\\n'
)
def operation(job, owner, activity):
    # All operation output is silent. Only the real heartbeat or control reader
    # can fail the worker transport while this child continues healthy growth.
    try:
        ChildProcessRunner().run(
            ProcessSpec(argv=(sys.executable, '-c', child_code, sys.argv[1], sys.argv[2]),
                        tool_id='healthy-fixture', display_name='healthy fixture',
                        artifacts=(ProcessArtifactProbe('video', path=pathlib.Path(sys.argv[2])),),
                        artifact_no_growth_timeout_seconds=120, artifact_interval_seconds=.05),
            cancellation_event=owner.cancellation_event)
    except ProcessCancelled:
        owner.check_cancelled()
        raise
    return {'name': 'fixture'}
raise SystemExit(run_worker(sys.stdin, sys.stdout, sys.stderr, operation_runner=operation,
                            isolate_process_stdin=True, event_write_timeout_seconds=.2,
                            heartbeat_interval=.001 if sys.argv[3]=='heartbeat' else 30))
"""
            process = subprocess.Popen(
                [sys.executable, "-c", script, str(marker), str(growing_output), origin],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                assert process.stdin is not None
                process.stdin.write(request_line(Path("/tmp/movie.mkv")))
                process.stdin.flush()
                deadline = time.monotonic() + 2
                while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(marker.exists())
                if origin == "control":
                    # Compact invalid records fill only the outgoing pipe. The
                    # host keeps both pipes open and deliberately does not read.
                    process.stdin.write("\n" * 4096)
                    process.stdin.flush()
                self.assertEqual(process.wait(timeout=4), TRANSPORT_FAILURE_EXIT_CODE)
                self.assertGreater(growing_output.stat().st_size, 4096)
                self.assertFalse(psutil.pid_exists(int(marker.read_text())))
                assert process.stdout is not None and process.stderr is not None
                delivered = process.stdout.read()
                self.assertNotIn("artifact_no_growth", delivered)
                self.assertNotIn('"type":"job.cancelled"', delivered)
                self.assertNotIn('"type":"job.completed"', delivered)
                self.assertEqual(process.stderr.read(), "")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=2)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()
                if marker.exists():
                    try:
                        child = psutil.Process(int(marker.read_text()))
                        child.kill()
                        child.wait(timeout=2)
                    except psutil.NoSuchProcess:
                        # The worker is expected to have reaped the child before fallback cleanup.
                        pass

    def test_slow_draining_pipe_delivers_complete_ordered_records(self) -> None:
        read_fd, write_fd = os.pipe()
        fill_pipe(write_fd)
        received = bytearray()

        def drain() -> None:
            time.sleep(0.75)
            while chunk := os.read(read_fd, 8192):
                received.extend(chunk)

        reader = threading.Thread(target=drain)
        reader.start()
        try:
            with os.fdopen(write_fd, "w") as output:
                emitter = WorkerEventEmitter(output, str(uuid4()))
                emitter.emit(WorkerEventType.LOG, {"message": "a" * 65536})
                emitter.emit(WorkerEventType.JOB_COMPLETED, {"result": {"name": "fixture"}})
                self.assertTrue(emitter.terminal_emitted)
                self.assertTrue(os.get_blocking(write_fd))
            reader.join(2)
            self.assertFalse(reader.is_alive())
            event_bytes = bytes(received).lstrip(b"x")
            events = [json.loads(line) for line in event_bytes.splitlines()]
            self.assertEqual([event["sequence"] for event in events], [0, 1])
            self.assertEqual(events[0]["payload"]["message"], "a" * 65536)
            self.assertEqual(events[-1]["type"], "job.completed")
        finally:
            os.close(read_fd)
            reader.join(2)

    def test_full_pipe_does_not_hold_control_state_and_fails_closed(self) -> None:
        read_fd, write_fd = os.pipe()
        fill_pipe(write_fd)
        errors = []
        failure_notifications = []
        entered = threading.Event()
        with os.fdopen(write_fd, "w") as output:
            emitter = WorkerEventEmitter(
                output,
                str(uuid4()),
                write_timeout_seconds=0.2,
                on_transport_failure=lambda: failure_notifications.append(True),
            )
            controls = WorkerControls(str(uuid4()), emitter)
            run_id = str(uuid4())
            controls.register_run(run_id)

            def blocked_result() -> None:
                entered.set()
                try:
                    controls.receive_line(b"{invalid}")
                except WorkerEventTransportError as error:
                    errors.append(error)

            sender = threading.Thread(target=blocked_result)
            sender.start()
            self.assertTrue(entered.wait(1))
            time.sleep(0.02)
            started = time.monotonic()
            self.assertEqual(controls.take_commands(run_id), [])
            controls.close()
            self.assertFalse(emitter.terminal_emitted)
            self.assertLess(time.monotonic() - started, 0.1)
            sender.join(1)
            self.assertFalse(sender.is_alive())
            self.assertEqual(len(errors), 1)
            os.read(read_fd, 65536)
            with self.assertRaises(WorkerEventTransportError):
                emitter.emit(WorkerEventType.JOB_COMPLETED)
            self.assertEqual(failure_notifications, [True])
            os.set_blocking(read_fd, False)
            with self.assertRaises(BlockingIOError):
                os.read(read_fd, 1)
        os.close(read_fd)

    def test_blocked_control_handler_does_not_replace_result_or_skip_cleanup(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        exited = threading.Event()
        output = io.StringIO()
        original_receive = WorkerControls.receive_line

        def delayed_handler(controls: WorkerControls, line: bytes | None) -> None:
            entered.set()
            release.wait(2)
            original_receive(controls, line)
            exited.set()

        def operation(
            _job: JobSpec, _owner: WorkerProcessOwner, _activity: WorkerActivityReporter
        ) -> dict[str, object]:
            self.assertTrue(entered.wait(1))
            return {"name": "finished before handler"}

        closed_diagnostics = []
        original_close = WorkerDiagnosticRelay.close

        def close_diagnostics(relay: WorkerDiagnosticRelay) -> WorkerDiagnosticSnapshot:
            closed_diagnostics.append(True)
            return original_close(relay)

        with (
            patch.object(WorkerControls, "receive_line", delayed_handler),
            patch.object(WorkerDiagnosticRelay, "close", close_diagnostics),
            patch.object(WorkerProcessOwner, "terminate_descendants") as cleanup,
        ):
            try:
                started = time.monotonic()
                result = run_worker(
                    io.StringIO(request_line(Path("/tmp/movie.mkv")) + "{invalid}\n"),
                    output,
                    io.StringIO(),
                    establish_session=False,
                    operation_runner=operation,
                )
                self.assertLess(time.monotonic() - started, 1.2)
                self.assertEqual(result, 0)
                cleanup.assert_called_once()
                self.assertEqual(closed_diagnostics, [True])
                events = [json.loads(line) for line in output.getvalue().splitlines()]
                self.assertEqual(events[-1]["type"], "job.completed")
                self.assertEqual(events[-1]["payload"]["result"]["name"], "finished before handler")
                before_release = output.getvalue()
            finally:
                release.set()
            self.assertTrue(exited.wait(1))
            self.assertEqual(output.getvalue(), before_release)

    def test_reader_close_reports_blocked_handler_without_raising(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        reader = WorkerInputReader(io.StringIO(request_line(Path("/tmp/movie.mkv")) + "control\n"))
        reader.read_job_line()

        def handle(_line: bytes | None) -> None:
            entered.set()
            release.wait(2)

        reader.set_control_handler(handle)
        try:
            self.assertTrue(entered.wait(1))
            started = time.monotonic()
            self.assertFalse(reader.close())
            self.assertLess(time.monotonic() - started, 0.8)
        finally:
            release.set()
            self.assertTrue(reader.close())

    def test_library_reader_does_not_redirect_caller_input_descriptor(self) -> None:
        read_fd, write_fd = os.pipe()
        with os.fdopen(read_fd, "r") as source:
            reader = WorkerInputReader(source)
            try:
                os.write(write_fd, request_line(Path("/tmp/movie.mkv")).encode())
                reader.read_job_line()
                reader.set_control_handler(lambda _: None)
                self.assertTrue(reader.close())
                os.write(write_fd, b"unchanged")
                self.assertEqual(os.read(source.fileno(), 9), b"unchanged")
            finally:
                os.close(write_fd)
                reader.close()

    def test_permanently_blocked_worker_stdout_reaps_real_descendant(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "child.pid"
            script = """
import pathlib, subprocess, sys
from bd_to_avp.worker.__main__ import run_worker
children = []
def operation(job, owner, activity):
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    children.append(child)
    pathlib.Path(sys.argv[1]).write_text(str(child.pid))
    for _ in range(8):
        activity.log('x' * 65536)
    return {'name': 'fixture'}
result = run_worker(sys.stdin, sys.stdout, sys.stderr, operation_runner=operation,
                    isolate_process_stdin=True, event_write_timeout_seconds=0.2)
for child in children:
    child.wait(timeout=1)
raise SystemExit(result)
"""
            process = subprocess.Popen(
                [sys.executable, "-c", script, str(marker)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                assert process.stdin is not None
                process.stdin.write(request_line(Path("/tmp/movie.mkv")))
                process.stdin.flush()
                self.assertEqual(process.wait(timeout=4), TRANSPORT_FAILURE_EXIT_CODE)
                child_pid = int(marker.read_text())
                self.assertFalse(psutil.pid_exists(child_pid))
                assert process.stdout is not None and process.stderr is not None
                delivered = process.stdout.read()
                self.assertNotIn('"type":"job.completed"', delivered)
                self.assertNotIn("artifact_no_growth", delivered)
                self.assertEqual(process.stderr.read(), "")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=2)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()
                if marker.exists():
                    try:
                        child = psutil.Process(int(marker.read_text()))
                        child.kill()
                        child.wait(timeout=2)
                    except psutil.NoSuchProcess:
                        # A child already reaped by worker cleanup needs no additional termination.
                        pass

    def test_owned_worker_stdin_isolated_for_all_child_routes_with_live_controls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "children-tested"
            script = """
import pathlib, subprocess, sys, tempfile, time
from bd_to_avp.observability import ObservabilityEmitter
from bd_to_avp.process_runner import ChildProcessRunner, ProcessPipelineRunner, ProcessPipelineStage, ProcessSpec
from bd_to_avp.runtime import ObservabilityStream, RunContext
from bd_to_avp.worker.__main__ import run_worker
def operation(job, owner, activity):
    spec = ProcessSpec(argv=(sys.executable, '-c', 'import sys; print(len(sys.stdin.buffer.read()))'),
                       tool_id='fixture', display_name='fixture', timeout_seconds=0.8)
    contexts = (None, RunContext(ObservabilityStream(ObservabilityEmitter.WORKER)))
    for context in contexts:
        assert ChildProcessRunner().run(spec, run_context=context).stdout.text().strip() == '0'
    assert subprocess.check_output([sys.executable, '-c', 'import sys; print(len(sys.stdin.buffer.read()))'],
                                   timeout=0.8).strip() == b'0'
    with tempfile.TemporaryFile() as media:
        media.write(b'media'); media.seek(0)
        explicit = ProcessSpec(argv=spec.argv, tool_id='file', display_name='file', stdin=media, timeout_seconds=0.8)
        assert ChildProcessRunner().run(explicit).stdout.text().strip() == '5'
    first = ProcessSpec(argv=(sys.executable, '-c', "import sys; assert not sys.stdin.read(); print('media')"),
                        tool_id='first', display_name='first', timeout_seconds=0.8)
    last_code = "import sys; assert sys.stdin.read().strip() == 'media'; print('ok')"
    last = ProcessSpec(argv=(sys.executable, '-c', last_code),
                       tool_id='last', display_name='last', timeout_seconds=0.8)
    pipeline = ProcessPipelineRunner().run((ProcessPipelineStage(first), ProcessPipelineStage(last)))
    assert pipeline.stages[-1].result.stdout.text().strip() == 'ok'
    pathlib.Path(sys.argv[1]).write_text('done')
    time.sleep(0.2)
    return {'name': 'input isolated'}
raise SystemExit(run_worker(sys.stdin, sys.stdout, sys.stderr, operation_runner=operation,
                            isolate_process_stdin=True))
"""
            process = subprocess.Popen(
                [sys.executable, "-c", script, str(marker)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                assert process.stdin is not None
                process.stdin.write(request_line(Path("/tmp/movie.mkv")))
                process.stdin.flush()
                deadline = time.monotonic() + 3
                while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(marker.exists())
                process.stdin.write('{"control":"still owned by worker"}\n')
                process.stdin.flush()
                self.assertEqual(process.wait(timeout=3), 0)
                assert process.stdout is not None and process.stderr is not None
                events = [json.loads(line) for line in process.stdout.read().splitlines()]
                self.assertTrue(any(event["type"] == "control.result" for event in events))
                self.assertEqual(events[-1]["payload"]["result"]["name"], "input isolated")
                self.assertEqual(process.stderr.read(), "")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=2)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()


if __name__ == "__main__":
    unittest.main()
