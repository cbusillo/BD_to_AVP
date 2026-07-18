import os
import signal
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from bd_to_avp.observability import BoundedEventSink, ObservabilityEmitter, ObservabilityProgress
from bd_to_avp.process_runner import (
    CaptureOverflowPolicy,
    ChildProcessRunner,
    ProcessArtifactProbe,
    ProcessCancelled,
    ProcessExecutionError,
    ProcessOutputLimitError,
    ProcessPipeDrainError,
    ProcessRunnerError,
    ProcessSpec,
    ProcessStream,
)
from bd_to_avp.runtime import CancellationToken, ObservabilityStream, RunContext


class ChildProcessRunnerTests(unittest.TestCase):
    def test_streams_output_and_emits_lifecycle_events(self) -> None:
        sink = BoundedEventSink(maximum_events=50, maximum_bytes=100_000)
        context = RunContext(ObservabilityStream(ObservabilityEmitter.WORKER, sink))
        lines: list[tuple[ProcessStream, str]] = []

        result = ChildProcessRunner().run(
            self.spec(
                "import sys; print('out'); print('err', file=sys.stderr)",
                merge_stderr=False,
            ),
            run_context=context,
            line_handler=lambda stream, line: lines.append((stream, line)),
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.text(), "out\n")
        self.assertEqual(result.stderr.text(), "err\n")
        self.assertCountEqual(
            lines,
            [
                (ProcessStream.STDOUT, "out"),
                (ProcessStream.STDERR, "err"),
            ],
        )
        events = sink.snapshot().events
        self.assertEqual(events[0].kind, "tool.started")
        self.assertEqual(events[-1].kind, "tool.completed")
        self.assertEqual(events[-1].context.process.exit_code, 0)

    def test_invalid_utf8_is_replaced_and_counted(self) -> None:
        result = ChildProcessRunner().run(self.spec("import os; os.write(1, b'valid \\xff text\\n')"))

        self.assertEqual(result.stdout.text(), "valid � text\n")
        self.assertEqual(result.stdout.decode_replacements, 1)

    def test_multibyte_utf8_split_across_writes_is_preserved(self) -> None:
        result = ChildProcessRunner().run(
            self.spec("import os, time; os.write(1, b'\\xe2'); time.sleep(0.05); os.write(1, b'\\x82\\xac\\n')")
        )

        self.assertEqual(result.stdout.text(), "€\n")
        self.assertEqual(result.stdout.decode_replacements, 0)

    def test_line_limit_does_not_split_multibyte_utf8(self) -> None:
        lines: list[str] = []

        ChildProcessRunner().run(
            self.spec(
                "print('a' * 15 + '€')",
                line_limit_bytes=16,
            ),
            line_handler=lambda _stream, line: lines.append(line),
        )

        self.assertEqual("".join(lines), "a" * 15 + "€")
        self.assertNotIn("�", "".join(lines))

    def test_carriage_return_only_progress_is_framed(self) -> None:
        lines: list[str] = []

        ChildProcessRunner().run(
            self.spec("import os; os.write(1, b'first\\rsecond\\r')"),
            line_handler=lambda _stream, line: lines.append(line),
        )

        self.assertEqual(lines, ["first", "second"])

    def test_capture_limit_fails_without_unbounded_memory(self) -> None:
        with self.assertRaises(ProcessOutputLimitError) as raised:
            ChildProcessRunner().run(
                self.spec(
                    "import os; os.write(1, b'x' * 200000)",
                    capture_limit_bytes=32 * 1024,
                )
            )

        self.assertIsNotNone(raised.exception.stdout_snapshot)
        assert raised.exception.stdout_snapshot is not None
        self.assertTrue(raised.exception.stdout_snapshot.truncated)

    def test_nonzero_exit_preserves_raw_bounded_output_snapshots(self) -> None:
        with self.assertRaises(ProcessExecutionError) as raised:
            ChildProcessRunner().run(
                self.spec(
                    "import os, sys; os.write(2, b'\\xff' + b'x' * 10000 + b'END'); sys.exit(2)",
                    merge_stderr=False,
                    capture_limit_bytes=4096,
                    tail_limit_bytes=1024,
                    capture_overflow=CaptureOverflowPolicy.TRUNCATE,
                )
            )

        self.assertEqual(raised.exception.returncode, 2)
        self.assertEqual(raised.exception.stderr_snapshot.capture[:1], b"\xff")
        self.assertTrue(raised.exception.stderr_snapshot.truncated)
        self.assertTrue(raised.exception.stderr_snapshot.tail.endswith(b"END"))

    def test_nonzero_merged_output_preserves_called_process_error_contract(self) -> None:
        with self.assertRaises(ProcessExecutionError) as raised:
            ChildProcessRunner().run(self.spec("import sys; print('error', file=sys.stderr); sys.exit(2)"))

        self.assertIn("error", raised.exception.output)
        self.assertIsNone(raised.exception.stderr)

    def test_rejects_unmanaged_stdin_pipe(self) -> None:
        with self.assertRaisesRegex(ValueError, "stdin=subprocess.PIPE"):
            self.spec("pass", stdin=-1)

    def test_truncation_policy_returns_bounded_prefix_and_tail(self) -> None:
        result = ChildProcessRunner().run(
            self.spec(
                "import os; os.write(1, b'a' * 100000 + b'END')",
                capture_limit_bytes=4096,
                tail_limit_bytes=1024,
                capture_overflow=CaptureOverflowPolicy.TRUNCATE,
            )
        )

        self.assertEqual(len(result.stdout.capture), 4096)
        self.assertEqual(len(result.stdout.tail), 1024)
        self.assertTrue(result.stdout.tail.endswith(b"END"))
        self.assertEqual(result.stdout.total_bytes, 100003)
        self.assertEqual(result.stdout.dropped_bytes, 100003 - 4096)

    def test_silent_process_emits_heartbeat_with_output_age(self) -> None:
        sink = BoundedEventSink(maximum_events=50, maximum_bytes=100_000)
        context = RunContext(ObservabilityStream(ObservabilityEmitter.WORKER, sink))

        ChildProcessRunner().run(
            self.spec(
                "import time; time.sleep(0.18)",
                activity_interval_seconds=0.05,
            ),
            run_context=context,
        )

        heartbeats = [event for event in sink.snapshot().events if event.kind == "tool.heartbeat"]
        self.assertGreaterEqual(len(heartbeats), 2)
        self.assertIsNotNone(heartbeats[-1].data.activity)
        self.assertGreaterEqual(heartbeats[-1].data.activity.last_output_age_seconds, 0)

    def test_progress_parser_emits_structured_progress(self) -> None:
        sink = BoundedEventSink(maximum_events=50, maximum_bytes=100_000)
        context = RunContext(ObservabilityStream(ObservabilityEmitter.WORKER, sink))

        def parse_progress(_stream: ProcessStream, line: str) -> ObservabilityProgress | None:
            if not line.startswith("progress="):
                return None
            completed = float(line.partition("=")[2])
            return ObservabilityProgress(completed_units=completed, total_units=10, unit="items")

        ChildProcessRunner().run(
            self.spec("print('progress=4')"),
            run_context=context,
            progress_parser=parse_progress,
        )

        progress_events = [event for event in sink.snapshot().events if event.kind == "tool.progress"]
        self.assertEqual(len(progress_events), 1)
        self.assertEqual(progress_events[0].data.progress.completed_units, 4)

    def test_cancellation_terminates_process_group(self) -> None:
        cancellation_event = threading.Event()
        timer = threading.Timer(0.1, cancellation_event.set)
        timer.start()
        started = time.monotonic()
        try:
            with self.assertRaises(ProcessCancelled):
                ChildProcessRunner().run(
                    self.spec("import time; time.sleep(30)", termination_grace_seconds=0.1),
                    cancellation_event=cancellation_event,
                )
        finally:
            timer.cancel()

        self.assertLess(time.monotonic() - started, 3)

    def test_cancellation_escalates_when_process_ignores_sigterm(self) -> None:
        sink = BoundedEventSink(maximum_events=50, maximum_bytes=100_000)
        cancellation = CancellationToken()
        context = RunContext(ObservabilityStream(ObservabilityEmitter.WORKER, sink), cancellation)
        timer = threading.Timer(0.1, cancellation.cancel)
        timer.start()
        try:
            with self.assertRaises(ProcessCancelled):
                ChildProcessRunner().run(
                    self.spec(
                        "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)",
                        termination_grace_seconds=0.1,
                    ),
                    run_context=context,
                )
        finally:
            timer.cancel()

        cancelled = [event for event in sink.snapshot().events if event.kind == "tool.cancelled"]
        self.assertEqual(len(cancelled), 1)
        self.assertTrue(cancelled[0].data.cancellation.forced)
        self.assertEqual(cancelled[0].context.process.signal, signal.SIGKILL)

    def test_stdout_and_stderr_floods_are_drained_concurrently(self) -> None:
        result = ChildProcessRunner().run(
            self.spec(
                "import os, threading; "
                "threads = [threading.Thread(target=lambda fd=fd: os.write(fd, b'x' * 500000)) "
                "for fd in (1, 2)]; [thread.start() for thread in threads]; "
                "[thread.join() for thread in threads]",
                merge_stderr=False,
                capture_limit_bytes=600000,
            )
        )

        self.assertEqual(result.stdout.total_bytes, 500000)
        self.assertEqual(result.stderr.total_bytes, 500000)

    def test_artifact_growth_is_sampled_without_affecting_process(self) -> None:
        sink = BoundedEventSink(maximum_events=100, maximum_bytes=200_000)
        context = RunContext(ObservabilityStream(ObservabilityEmitter.WORKER, sink))
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "output.bin"
            ChildProcessRunner().run(
                self.spec(
                    "import pathlib, sys, time; path = pathlib.Path(sys.argv[1]); "
                    "path.write_bytes(b'x' * 70000); time.sleep(0.08); "
                    "path.write_bytes(b'x' * 150000); time.sleep(0.08)",
                    artifact,
                    artifacts=(ProcessArtifactProbe("intermediate", artifact),),
                    artifact_interval_seconds=0.04,
                ),
                run_context=context,
            )

        artifact_events = [event for event in sink.snapshot().events if event.kind == "tool.artifact"]
        self.assertGreaterEqual(len(artifact_events), 2)
        self.assertEqual(artifact_events[-1].data.artifact.state, "complete")
        self.assertEqual(artifact_events[-1].data.artifact.size_bytes, 131072)

    def test_blocked_event_sink_does_not_block_output_draining(self) -> None:
        release_sink = threading.Event()

        class BlockingSink:
            def emit(self, _event: object) -> None:
                release_sink.wait(timeout=5)

        context = RunContext(ObservabilityStream(ObservabilityEmitter.WORKER, BlockingSink()))
        started = time.monotonic()
        try:
            result = ChildProcessRunner().run(
                self.spec(
                    "import os; os.write(1, b'x' * 500000)",
                    capture_limit_bytes=600000,
                ),
                run_context=context,
            )
        finally:
            release_sink.set()

        self.assertEqual(result.stdout.total_bytes, 500000)
        self.assertLess(time.monotonic() - started, 2)

    def test_terminal_event_survives_saturated_async_queue(self) -> None:
        release_sink = threading.Event()
        events: list[object] = []

        class BlockingRecordingSink:
            def emit(self, event: object) -> None:
                events.append(event)
                if len(events) == 1:
                    release_sink.wait(timeout=5)

        context = RunContext(ObservabilityStream(ObservabilityEmitter.WORKER, BlockingRecordingSink()))
        try:
            ChildProcessRunner().run(
                self.spec("for index in range(300): print(index)"),
                run_context=context,
                progress_parser=lambda _stream, _line: ObservabilityProgress(
                    completed_units=1,
                    total_units=1,
                    unit="items",
                ),
            )
        finally:
            release_sink.set()

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not any(
            getattr(event, "kind", None) == "tool.completed" for event in events
        ):
            time.sleep(0.01)
        self.assertTrue(any(getattr(event, "kind", None) == "tool.completed" for event in events))

    def test_output_observer_failure_terminates_and_reraises(self) -> None:
        def fail(_stream: ProcessStream, _payload: bytes) -> None:
            raise RuntimeError("observer failed")

        started = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "observer failed"):
            ChildProcessRunner().run(
                self.spec("import time; print('data', flush=True); time.sleep(30)"),
                output_observer=fail,
            )
        self.assertLess(time.monotonic() - started, 3)

    def test_progress_parser_failure_terminates_and_reraises(self) -> None:
        def fail(_stream: ProcessStream, _line: str) -> ObservabilityProgress | None:
            raise RuntimeError("progress failed")

        started = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "progress failed"):
            ChildProcessRunner().run(
                self.spec("import time; print('data', flush=True); time.sleep(30)"),
                progress_parser=fail,
            )
        self.assertLess(time.monotonic() - started, 3)

    def test_escaped_descendant_cannot_hold_output_pipe_forever(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pid_path = Path(directory) / "escaped.pid"
            started = time.monotonic()
            with self.assertRaises(ProcessPipeDrainError):
                ChildProcessRunner().run(
                    self.spec(
                        "import os, pathlib, sys, time; pid = os.fork(); "
                        "(os.setsid(), pathlib.Path(sys.argv[1]).write_text(str(os.getpid())), "
                        "time.sleep(30), os._exit(0)) if pid == 0 else os._exit(0)",
                        pid_path,
                        pipe_drain_timeout_seconds=0.1,
                        termination_grace_seconds=0.1,
                        kill_wait_seconds=0.1,
                    )
                )
            self.assertLess(time.monotonic() - started, 3)
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline and not pid_path.exists():
                time.sleep(0.01)
            escaped_pid = int(pid_path.read_text(encoding="utf-8"))
            try:
                os.kill(escaped_pid, signal.SIGKILL)
            except ProcessLookupError:
                return

    def test_same_group_descendant_is_terminated_after_leader_exit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pid_path = Path(directory) / "descendant.pid"
            with self.assertRaisesRegex(ProcessRunnerError, "descendant remained running"):
                ChildProcessRunner().run(
                    self.spec(
                        "import os, pathlib, sys, time; pid = os.fork(); "
                        "(os.close(1), os.close(2), pathlib.Path(sys.argv[1]).write_text(str(os.getpid())), "
                        "time.sleep(30), os._exit(0)) if pid == 0 else os._exit(0)",
                        pid_path,
                        termination_grace_seconds=0.1,
                        kill_wait_seconds=0.1,
                    )
                )
            deadline = time.monotonic() + 1
            descendant_text = ""
            while time.monotonic() < deadline:
                try:
                    descendant_text = pid_path.read_text(encoding="utf-8").strip()
                except FileNotFoundError:
                    descendant_text = ""
                if descendant_text:
                    break
                time.sleep(0.01)
            if descendant_text:
                descendant_pid = int(descendant_text)
                with self.assertRaises(ProcessLookupError):
                    os.kill(descendant_pid, 0)

    def test_artifact_resolver_failure_emits_one_warning(self) -> None:
        sink = BoundedEventSink(maximum_events=50, maximum_bytes=100_000)
        context = RunContext(ObservabilityStream(ObservabilityEmitter.WORKER, sink))

        def fail() -> Path | None:
            raise RuntimeError("probe failed")

        ChildProcessRunner().run(
            self.spec(
                "import time; time.sleep(0.08)",
                artifacts=(ProcessArtifactProbe("intermediate", resolver=fail),),
                artifact_interval_seconds=0.02,
            ),
            run_context=context,
        )

        failures = [event for event in sink.snapshot().events if event.kind == "tool.artifact_probe_failed"]
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0].data.failure.code, "artifact_resolver_failed")

    def test_keyboard_interrupt_during_artifact_probe_terminates_child(self) -> None:
        def interrupt() -> Path | None:
            raise KeyboardInterrupt

        started = time.monotonic()
        with self.assertRaises(KeyboardInterrupt):
            ChildProcessRunner().run(
                self.spec(
                    "import time; time.sleep(30)",
                    artifacts=(ProcessArtifactProbe("intermediate", resolver=interrupt),),
                    artifact_interval_seconds=0.02,
                    termination_grace_seconds=0.1,
                    kill_wait_seconds=0.1,
                )
            )
        self.assertLess(time.monotonic() - started, 3)

    def test_artifact_growth_resets_when_resolver_switches_files(self) -> None:
        sink = BoundedEventSink(maximum_events=100, maximum_bytes=200_000)
        context = RunContext(ObservabilityStream(ObservabilityEmitter.WORKER, sink))
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.bin"
            second = Path(directory) / "second.bin"
            first.write_bytes(b"x" * 100000)
            second.write_bytes(b"x" * 200000)
            calls = 0

            def resolve() -> Path:
                nonlocal calls
                calls += 1
                return first if calls == 1 else second

            ChildProcessRunner().run(
                self.spec(
                    "import time; time.sleep(0.08)",
                    artifacts=(ProcessArtifactProbe("intermediate", resolver=resolve),),
                    artifact_interval_seconds=0.02,
                ),
                run_context=context,
            )

        second_events = [
            event
            for event in sink.snapshot().events
            if event.kind == "tool.artifact" and event.data.artifact.location.value.endswith("second.bin")
        ]
        self.assertGreaterEqual(len(second_events), 1)
        self.assertIsNone(second_events[0].data.artifact.growth_bytes_per_second)

    def test_precancelled_context_does_not_spawn(self) -> None:
        cancellation = CancellationToken()
        cancellation.cancel()
        context = RunContext(ObservabilityStream(ObservabilityEmitter.WORKER), cancellation)

        with self.assertRaises(ProcessCancelled):
            ChildProcessRunner().run(self.spec("raise SystemExit(99)"), run_context=context)

    @staticmethod
    def spec(
        source: str,
        *arguments: object,
        **overrides: object,
    ) -> ProcessSpec:
        values = {
            "argv": (sys.executable, "-c", source, *(os.fspath(argument) for argument in arguments)),
            "tool_id": "python-test-tool",
            "display_name": "test tool",
        }
        values.update(overrides)
        return ProcessSpec(**values)


if __name__ == "__main__":
    unittest.main()
