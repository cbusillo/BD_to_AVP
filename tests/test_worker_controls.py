import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from collections.abc import Iterator, Mapping
from typing import Any
from unittest.mock import patch
from uuid import uuid4

from bd_to_avp.observability import ObservabilityEmitter, ObservabilityProgress
from bd_to_avp.process_runner import (
    ChildProcessRunner,
    ProcessArtifactNoProgressError,
    ProcessArtifactProbe,
    ProcessPipelineRunner,
    ProcessPipelineStage,
    ProcessSpec,
    _ArtifactWatchdog,
    _ProgressSummary,
)
from bd_to_avp.runtime import ObservabilityStream, RunContext
from bd_to_avp.worker.__main__ import run_worker
from bd_to_avp.worker.controls import MAX_CONTROL_BYTES, WorkerControls, WorkerInputReader
from bd_to_avp.worker.ownership import WorkerCancelled, WorkerProcessOwner
from bd_to_avp.worker.protocol import (
    MAX_REQUEST_BYTES,
    JobSpec,
    WorkerActivityReporter,
    WorkerEventEmitter,
    WorkerProtocolError,
)
from tests.test_native_worker import request_line


class ControlHarness:
    def __init__(self, *, queue_limit: int = 16, ledger_limit: int = 256) -> None:
        self.now = 0.0
        self.job_id = str(uuid4())
        self.run_id = str(uuid4())
        self.output = io.StringIO()
        self.controls = WorkerControls(
            self.job_id,
            WorkerEventEmitter(self.output, self.job_id),
            monotonic_clock=lambda: self.now,
            queue_limit=queue_limit,
            ledger_limit=ledger_limit,
        )
        self.controls.register_run(self.run_id)
        self.watchdog = _ArtifactWatchdog(
            120, self.run_id, "mv_hevc_encoder", self.controls, lambda _: {"artifacts": [], "tool_progress": {}}
        )

    def poll(self, now: float, progress: tuple[float, ...] = (0.0,), **kwargs: Any) -> bool:
        self.now = now
        return self.watchdog.poll(progress, now, self.controls.take_commands(self.run_id), **kwargs)

    def command(self, **overrides: Any) -> dict[str, Any]:
        command = {
            "protocol_version": 13,
            "type": "job.keep_waiting",
            "command_id": str(uuid4()),
            "job_id": self.job_id,
            "tool_run_id": self.run_id,
            "stall_episode_id": self.watchdog.episode_id or str(uuid4()),
        }
        command.update(overrides)
        self.controls.receive_line(json.dumps(command).encode())
        return command

    def results(self) -> list[dict[str, Any]]:
        return [event["payload"] for event in self.events() if event["type"] == "control.result"]

    def events(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.output.getvalue().splitlines()]


class WorkerControlDecisionTests(unittest.TestCase):
    def test_grants_apply_only_on_runner_and_are_fixed_and_finite(self) -> None:
        harness = ControlHarness()
        self.assertFalse(harness.poll(60))
        harness.command()
        self.assertEqual(harness.results(), [])
        self.assertFalse(harness.poll(61))
        self.assertEqual(harness.results()[0]["grant_seconds"], 120)
        self.assertEqual(harness.results()[0]["grants_used"], 1)
        self.assertFalse(harness.poll(121))
        harness.command()
        self.assertFalse(harness.poll(122))
        harness.command()
        self.assertFalse(harness.poll(123))
        self.assertEqual(harness.results()[-1]["code"], "grant_limit")
        self.assertFalse(harness.poll(359.9))
        self.assertTrue(harness.poll(360))

    def test_no_command_preserves_unattended_timeout(self) -> None:
        harness = ControlHarness()
        self.assertFalse(harness.poll(60))
        self.assertFalse(harness.poll(119.9))
        self.assertTrue(harness.poll(120))
        self.assertEqual(harness.watchdog.grants_used, 0)

    def test_pending_duplicates_and_replayed_results_never_grant_twice(self) -> None:
        harness = ControlHarness()
        harness.poll(60)
        command = harness.command()
        harness.command(**command)
        self.assertEqual(harness.results(), [])
        harness.poll(61)
        harness.command(**command)
        harness.poll(62)
        self.assertEqual(harness.watchdog.grants_used, 1)
        self.assertTrue(harness.results()[-1]["duplicate"])
        self.assertEqual(harness.results()[-1]["grants_used"], 1)
        self.assertTrue(harness.poll(240))

    def test_conflicting_command_id_does_not_replace_original(self) -> None:
        harness = ControlHarness()
        harness.poll(60)
        command = harness.command()
        harness.command(**{**command, "stall_episode_id": str(uuid4())})
        self.assertEqual(harness.results()[-1]["code"], "command_conflict")
        harness.poll(61)
        self.assertEqual(harness.watchdog.grants_used, 1)
        self.assertTrue(harness.results()[-1]["accepted"])

    def test_wrong_job_run_and_episode_are_rejected(self) -> None:
        harness = ControlHarness()
        harness.poll(60)
        harness.command(job_id=str(uuid4()))
        harness.command(tool_run_id=str(uuid4()))
        harness.command(stall_episode_id=str(uuid4()))
        harness.poll(61)
        self.assertEqual([value["code"] for value in harness.results()], ["wrong_job", "inactive_run", "stale_episode"])
        self.assertEqual(harness.watchdog.grants_used, 0)

    def test_swift_uppercase_uuid_identities_resolve_to_same_worker_target(self) -> None:
        harness = ControlHarness()
        harness.poll(60)
        command_id = str(uuid4())
        assert harness.watchdog.episode_id is not None
        harness.command(
            command_id=command_id.upper(),
            job_id=harness.job_id.upper(),
            tool_run_id=harness.run_id.upper(),
            stall_episode_id=harness.watchdog.episode_id.upper(),
        )
        harness.poll(61)
        self.assertTrue(harness.results()[-1]["accepted"])
        self.assertEqual(harness.results()[-1]["command_id"], command_id)
        harness.command(command_id=command_id)
        self.assertTrue(harness.results()[-1]["duplicate"])
        self.assertEqual(harness.watchdog.grants_used, 1)

    def test_cancellation_wins_over_queued_extension(self) -> None:
        harness = ControlHarness()
        harness.poll(60)
        harness.command()
        harness.poll(61, cancelled=True)
        self.assertEqual(harness.results()[-1]["code"], "cancelled")
        self.assertEqual(harness.watchdog.grants_used, 0)

    def test_runner_rechecks_cancellation_or_exit_at_application(self) -> None:
        for state in ("cancelled", "run_ended"):
            with self.subTest(state=state):
                harness = ControlHarness()
                harness.poll(60)
                harness.command()
                checks = iter((False, True, True)) if state == "cancelled" else iter((True, False, False))

                def check(sequence: Iterator[bool] = checks) -> bool:
                    return next(sequence)

                kwargs = {"cancellation_check" if state == "cancelled" else "active_check": check}
                harness.poll(61, **kwargs)
                self.assertEqual(harness.results()[-1]["code"], state)
                self.assertEqual(harness.watchdog.grants_used, 0)

    def test_committed_timeout_and_completed_run_cannot_be_revived(self) -> None:
        harness = ControlHarness()
        harness.poll(120)
        harness.command()
        self.assertTrue(harness.poll(121))
        self.assertEqual(harness.results()[-1]["code"], "deadline_elapsed")
        harness.controls.unregister_run(harness.run_id)
        harness.command()
        self.assertEqual(harness.results()[-1]["code"], "inactive_run")
        harness = ControlHarness()
        harness.poll(60)
        harness.command()
        harness.poll(61, active=False)
        self.assertEqual(harness.results()[-1]["code"], "run_ended")

    def test_received_before_deadline_has_only_fixed_processing_grace(self) -> None:
        harness = ControlHarness()
        harness.poll(60)
        harness.now = 119
        harness.command()
        self.assertFalse(harness.poll(180))
        self.assertTrue(harness.results()[-1]["accepted"])
        self.assertTrue(harness.poll(240))
        harness = ControlHarness()
        harness.poll(60)
        harness.now = 119
        harness.command()
        self.assertTrue(harness.poll(241))
        self.assertEqual(harness.results()[-1]["code"], "extension_window_elapsed")

    def test_received_after_deadline_cannot_grant_even_before_timeout_poll(self) -> None:
        harness = ControlHarness()
        harness.poll(60)
        harness.now = 120
        harness.command()
        self.assertTrue(harness.poll(121))
        self.assertEqual(harness.results()[-1]["code"], "deadline_elapsed")

    def test_stale_poll_timestamp_cannot_extend_past_actual_processing_window(self) -> None:
        harness = ControlHarness()
        harness.poll(60)
        harness.now = 119
        harness.command()
        harness.watchdog._clock = lambda: 241
        self.assertTrue(harness.poll(120))
        self.assertEqual(harness.results()[-1]["code"], "extension_window_elapsed")
        self.assertEqual(harness.watchdog.grants_used, 0)

    def test_delay_during_first_ack_cannot_extend_second_command_past_hard_cap(self) -> None:
        harness = ControlHarness()
        harness.poll(60)
        harness.command()
        harness.command()
        clock = iter((61.0, 61.0, 400.0, 400.0))
        harness.watchdog._clock = lambda: next(clock)
        self.assertTrue(harness.poll(61))
        self.assertTrue(harness.results()[0]["accepted"])
        self.assertEqual(harness.results()[1]["code"], "extension_window_elapsed")
        self.assertEqual(harness.watchdog.grants_used, 1)

    def test_growing_sibling_never_resets_stalled_output_budget(self) -> None:
        harness = ControlHarness()
        harness.poll(60, (0, 59))
        episode = harness.watchdog.episode_id
        harness.command()
        harness.poll(61, (0, 61))
        harness.command()
        harness.poll(62, (0, 62))
        harness.poll(200, (0, 200))
        self.assertEqual(harness.watchdog.episode_id, episode)
        self.assertEqual(harness.watchdog.grants_used, 2)
        self.assertTrue(harness.poll(360, (0, 360)))

    def test_partial_recovery_keeps_original_hard_cap(self) -> None:
        harness = ControlHarness()
        harness.poll(60, (0, 20))
        episode = harness.watchdog.episode_id
        harness.command()
        harness.poll(61, (0, 20))
        harness.command()
        harness.poll(62, (0, 20))
        # First output recovered, second remains quiet; no fresh grant budget.
        harness.poll(200, (200, 20))
        self.assertEqual(harness.watchdog.episode_id, episode)
        self.assertTrue(harness.poll(360, (350, 20)))

    def test_full_recovery_retires_episode_and_rejects_old_action(self) -> None:
        harness = ControlHarness()
        harness.poll(60, (0, 59))
        old_episode = harness.watchdog.episode_id
        harness.command()
        harness.poll(61, (0, 61))
        harness.poll(70, (70, 70))
        self.assertIsNone(harness.watchdog.episode_id)
        harness.poll(130, (70, 129))
        self.assertNotEqual(harness.watchdog.episode_id, old_episode)
        harness.command(stall_episode_id=old_episode)
        harness.poll(131, (70, 130))
        self.assertEqual(harness.results()[-1]["code"], "stale_episode")
        self.assertEqual(harness.watchdog.grants_used, 0)

    def test_queue_and_ledger_limits_fail_closed_without_eviction(self) -> None:
        harness = ControlHarness(queue_limit=1, ledger_limit=2)
        harness.poll(60)
        granted = harness.command()
        rejected = harness.command()
        self.assertEqual(harness.results()[-1]["code"], "queue_full")
        harness.poll(61)
        harness.command()
        self.assertEqual(harness.results()[-1]["code"], "ledger_full")
        harness.command(**rejected)
        self.assertEqual(harness.results()[-1]["code"], "queue_full")
        self.assertTrue(harness.results()[-1]["duplicate"])
        harness.command(**granted)
        self.assertTrue(harness.results()[-1]["duplicate"])
        self.assertEqual(harness.watchdog.grants_used, 1)

    def test_malformed_and_oversized_controls_do_not_echo_private_text(self) -> None:
        harness = ControlHarness()
        harness.controls.receive_line(b'{"private":"/private/movies/title.mkv"')
        harness.controls.receive_line(None)
        self.assertEqual([value["code"] for value in harness.results()], ["invalid_control", "control_too_large"])
        self.assertNotIn("private", harness.output.getvalue())

    def test_deeply_nested_control_is_rejected_without_stopping_reader(self) -> None:
        harness = ControlHarness()
        harness.controls.receive_line(b"[" * 1500 + b"]" * 1500)
        self.assertEqual(harness.results()[-1]["code"], "invalid_control")
        harness.poll(60)
        harness.command()
        harness.poll(61)
        self.assertTrue(harness.results()[-1]["accepted"])

    def test_unexpected_job_end_rejects_undecided_commands(self) -> None:
        harness = ControlHarness()
        harness.poll(60)
        harness.command()
        harness.controls.take_commands(harness.run_id)
        harness.controls.close()
        harness.controls.close()
        self.assertEqual(harness.results()[-1]["code"], "job_ended")

    def test_run_end_rejects_already_dequeued_undecided_command(self) -> None:
        harness = ControlHarness()
        harness.poll(60)
        harness.command()
        harness.controls.take_commands(harness.run_id)
        harness.controls.unregister_run(harness.run_id)
        self.assertEqual(harness.results()[-1]["code"], "inactive_run")

    def test_progress_summary_distinguishes_repetition_without_raw_lines(self) -> None:
        progress = _ProgressSummary()
        for now in (0, 10, 20):
            progress.record(ObservabilityProgress(completed_units=129479, unit="frames"), now)
        snapshot = progress.payload(30)
        self.assertEqual(snapshot["changed_age_seconds"], 30)
        self.assertEqual(snapshot["updated_age_seconds"], 10)
        self.assertEqual(snapshot["repeated_updates"], 2)
        self.assertEqual(snapshot["completed_units"], 129479)


class WorkerInputFramingTests(unittest.TestCase):
    def test_first_request_wait_remains_cancellable_with_host_input_open(self) -> None:
        read_fd, write_fd = os.pipe()
        with os.fdopen(read_fd, "r") as stream:
            reader = WorkerInputReader(stream)
            try:

                def cancelled() -> None:
                    raise WorkerCancelled()

                with self.assertRaises(WorkerCancelled):
                    reader.read_job_line(cancelled)
            finally:
                os.close(write_fd)
                reader.close()

    def test_job_limit_is_independent_of_small_control_limit(self) -> None:
        request = request_line(Path("/tmp/movie.mkv")).rstrip() + " " * MAX_CONTROL_BYTES + "\n"
        with io.StringIO(request) as stream:
            reader = WorkerInputReader(stream)
            try:
                self.assertEqual(reader.read_job_line(), request)
                reader.set_control_handler(lambda _: None)
            finally:
                reader.close()

    def test_oversized_job_is_rejected_before_operation(self) -> None:
        reader = WorkerInputReader(io.StringIO("x" * (MAX_REQUEST_BYTES + 1)))
        try:
            with self.assertRaises(WorkerProtocolError) as raised:
                reader.read_job_line()
            self.assertEqual(raised.exception.code, "request_too_large")
        finally:
            reader.close()

    def test_split_coalesced_oversized_malformed_and_eof_frames(self) -> None:
        read_fd, write_fd = os.pipe()
        received = []
        complete = threading.Event()
        with os.fdopen(read_fd, "r") as stream:
            reader = WorkerInputReader(stream)
            try:
                request = request_line(Path("/tmp/movie.mkv")).encode()
                for part in (request[:7], request[7:]):
                    os.write(write_fd, part)
                self.assertEqual(reader.read_job_line().encode(), request)

                def handle(line: bytes | None) -> None:
                    received.append(line)
                    if len(received) == 4:
                        complete.set()

                reader.set_control_handler(handle)
                os.write(write_fd, b'{"split":')
                os.write(write_fd, b"1}\n{malformed}\n")
                os.write(write_fd, b"x" * (MAX_CONTROL_BYTES + 1) + b'\n{"last":true}')
                os.close(write_fd)
                write_fd = None
                self.assertTrue(complete.wait(2))
                self.assertEqual(received, [b'{"split":1}\n', b"{malformed}\n", None, b'{"last":true}'])
            finally:
                if write_fd is not None:
                    os.close(write_fd)
                reader.close()

    def test_reader_stops_while_host_keeps_input_open(self) -> None:
        read_fd, write_fd = os.pipe()
        with os.fdopen(read_fd, "r") as stream:
            reader = WorkerInputReader(stream)
            try:
                os.write(write_fd, request_line(Path("/tmp/movie.mkv")).encode())
                reader.read_job_line()
                reader.set_control_handler(lambda _: None)
                started = time.monotonic()
                reader.close()
                self.assertLess(time.monotonic() - started, 0.5)
                self.assertFalse(any(t.name == "worker-control-reader" for t in threading.enumerate()))
            finally:
                os.close(write_fd)
                reader.close()

    def test_worker_starts_before_eof_and_finishes_with_input_open(self) -> None:
        for keep_input_open in (True, False):
            with self.subTest(keep_input_open=keep_input_open):
                read_fd, write_fd = os.pipe()
                output = io.StringIO()
                operation_started = threading.Event()
                release = threading.Event()
                results = []

                def operation(
                    _job: JobSpec,
                    owner: WorkerProcessOwner,
                    _activity: WorkerActivityReporter,
                    started: threading.Event = operation_started,
                    unblock: threading.Event = release,
                ) -> dict[str, object]:
                    started.set()
                    self.assertTrue(unblock.wait(2))
                    self.assertFalse(owner.cancellation_event.is_set())
                    return {"name": "fixture"}

                with os.fdopen(read_fd, "r") as stream:
                    worker = threading.Thread(
                        target=lambda results=results, output=output: results.append(
                            run_worker(
                                stream, output, io.StringIO(), establish_session=False, operation_runner=operation
                            )
                        )
                    )
                    with (
                        patch.object(WorkerProcessOwner, "install_signal_handlers"),
                        patch.object(WorkerProcessOwner, "terminate_descendants"),
                    ):
                        worker.start()
                        try:
                            os.write(write_fd, request_line(Path("/tmp/movie.mkv")).encode())
                            self.assertTrue(operation_started.wait(2))
                            if not keep_input_open:
                                os.close(write_fd)
                                write_fd = None
                            release.set()
                            worker.join(2)
                            self.assertFalse(worker.is_alive())
                        finally:
                            release.set()
                            if write_fd is not None:
                                os.close(write_fd)
                            worker.join(2)
                self.assertEqual(results, [0])
                events = [json.loads(line) for line in output.getvalue().splitlines()]
                self.assertEqual(events[0]["payload"]["control_capabilities"], ["keep_waiting_v1"])
                self.assertEqual(events[-1]["type"], "job.completed")

    def test_protocol_12_fixture_is_rejected_by_13_worker(self) -> None:
        source = Path(__file__).parent / "fixtures/native_worker_convert_v12.json"
        with self.assertRaises(WorkerProtocolError) as raised:
            JobSpec.from_json_line(source.read_text())
        self.assertEqual(raised.exception.code, "protocol_mismatch")


class WorkerControlProcessTests(unittest.TestCase):
    def test_unattended_timeout_does_not_name_sibling_below_timeout_age(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            left = Path(directory) / "left.mov"
            right = Path(directory) / "right.mov"
            started = time.monotonic()
            script = (
                "import pathlib,sys,time; left,right=map(pathlib.Path,sys.argv[1:]); "
                "left.write_bytes(b'video'); right.write_bytes(b'video'); "
                "time.sleep(.3); right.write_bytes(b'later progress'); time.sleep(5)"
            )
            with self.assertRaises(ProcessArtifactNoProgressError) as raised:
                ChildProcessRunner(monotonic_clock=lambda: (time.monotonic() - started) * 120).run(
                    ProcessSpec(
                        argv=(sys.executable, "-c", script, left, right),
                        tool_id="ffmpeg",
                        display_name="encoder",
                        artifacts=(
                            ProcessArtifactProbe("left_eye", path=left),
                            ProcessArtifactProbe("right_eye", path=right),
                        ),
                        artifact_no_growth_timeout_seconds=120,
                        artifact_interval_seconds=2,
                    )
                )
            self.assertIn("120 seconds: left_eye", str(raised.exception))
            self.assertNotIn("right_eye", str(raised.exception))

    def test_live_controls_isolate_inherited_stdin_and_preserve_media_pipeline(self) -> None:
        harness = ControlHarness()
        context = RunContext(ObservabilityStream(ObservabilityEmitter.WORKER), process_controls=harness.controls)
        first = ProcessSpec(
            argv=(sys.executable, "-c", "import sys; assert not sys.stdin.buffer.read(); sys.stdout.write('media')"),
            tool_id="decoder",
            display_name="decoder",
        )
        last = ProcessSpec(
            argv=(sys.executable, "-c", "import sys; assert sys.stdin.read() == 'media'; print('encoded')"),
            tool_id="encoder",
            display_name="encoder",
        )
        result = ProcessPipelineRunner().run(
            (ProcessPipelineStage(first), ProcessPipelineStage(last)), run_context=context
        )
        final_stage = result.stages[-1].result
        assert final_stage is not None
        self.assertEqual(final_stage.stdout.text().strip(), "encoded")

    def test_real_runner_extends_direct_and_generated_outputs_and_reports_recovery(self) -> None:
        for output_count in (1, 2):
            with self.subTest(output_count=output_count), tempfile.TemporaryDirectory() as directory:
                started_at = time.monotonic()

                def clock(started: float = started_at) -> float:
                    return (time.monotonic() - started) * 120

                output = io.StringIO()
                job_id = str(uuid4())

                # The child advances on these markers, not on wall-clock guesses, so a slow
                # machine cannot make it finish before the runner has reported each state.
                markers = Path(directory) / "markers"
                markers.mkdir()

                class AutomaticWaitControls(WorkerControls):
                    marker_directory = markers

                    def emit_stall(self, payload: Mapping[str, object]) -> None:
                        super().emit_stall(payload)
                        (self.marker_directory / str(payload["state"])).touch()
                        if payload["state"] == "stalled":
                            self.receive_line(
                                json.dumps(
                                    dict(
                                        protocol_version=13,
                                        type="job.keep_waiting",
                                        command_id=str(uuid4()),
                                        job_id=self._job_id,
                                        tool_run_id=payload["tool_run_id"],
                                        stall_episode_id=payload["stall_episode_id"],
                                    )
                                ).encode()
                            )

                controls = AutomaticWaitControls(job_id, WorkerEventEmitter(output, job_id), monotonic_clock=clock)
                context = RunContext(ObservabilityStream(ObservabilityEmitter.WORKER), process_controls=controls)
                paths = tuple(Path(directory) / f"private-title-eye-{number}" for number in range(output_count))
                script = (
                    "import pathlib,sys,time; markers=pathlib.Path(sys.argv[1]); "
                    "paths=[pathlib.Path(p) for p in sys.argv[2:]]; "
                    "[p.write_bytes(b'video') for p in paths]; started=time.monotonic()\n"
                    "def waiting(state): return not (markers/state).exists() and time.monotonic()-started < 60\n"
                    "while waiting('extended'):\n"
                    " if len(paths)>1: paths[1].write_bytes(b'healthy sibling')\n"
                    " print('same repeated decoder line', flush=True); time.sleep(.04)\n"
                    "paths[0].write_bytes(b'recovered output')\n"
                    "while waiting('recovered'): time.sleep(.02)\n"
                )
                result = ChildProcessRunner(monotonic_clock=clock).run(
                    ProcessSpec(
                        argv=(sys.executable, "-c", script, str(markers), *paths),
                        tool_id="mv_hevc_encoder" if output_count == 1 else "ffmpeg",
                        display_name="encode",
                        artifacts=tuple(ProcessArtifactProbe(f"eye_{i}", path=path) for i, path in enumerate(paths)),
                        artifact_no_growth_timeout_seconds=120,
                        artifact_interval_seconds=2,
                        activity_interval_seconds=120,
                    ),
                    run_context=context,
                )
                self.assertEqual(result.returncode, 0)
                events = [json.loads(line) for line in output.getvalue().splitlines()]
                acknowledgements = [event["payload"] for event in events if event["type"] == "control.result"]
                self.assertEqual(len(acknowledgements), 1)
                self.assertTrue(acknowledgements[0]["accepted"])
                states = [event["payload"]["state"] for event in events if event["type"] == "tool.stall"]
                self.assertEqual(states, ["stalled", "extended", "recovered"])
                self.assertNotIn("private-title", output.getvalue())
                self.assertNotIn("same repeated decoder", output.getvalue())
                if output_count == 2:
                    stalled = next(event["payload"] for event in events if event["type"] == "tool.stall")
                    self.assertEqual([artifact["state"] for artifact in stalled["artifacts"]], ["stalled", "growing"])


if __name__ == "__main__":
    unittest.main()
