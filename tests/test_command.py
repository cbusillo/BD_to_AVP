import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from bd_to_avp.modules import command
from bd_to_avp.process_runner import (
    CaptureOverflowPolicy,
    ProcessCancelled,
    ProcessExecutionError,
    ProcessOutputLimitError,
    ProcessOutputSnapshot,
    ProcessResult,
    ProcessStream,
)


class RunProcessCaptureTests(unittest.TestCase):
    def test_line_handler_receives_streamed_output(self) -> None:
        lines: list[str] = []

        def run(_runner, _spec, **kwargs):
            kwargs["line_handler"](ProcessStream.STDOUT, "first")
            kwargs["line_handler"](ProcessStream.STDOUT, "second")
            return ProcessResult(
                tool_run_id="run-id",
                returncode=0,
                elapsed_ms=10,
                stdout=ProcessOutputSnapshot(b"first\nsecond\r\n", b"", 14, 14, 0, 0),
                stderr=ProcessOutputSnapshot(b"", b"", 0, 0, 0, 0),
            )

        with (
            patch.object(command.ChildProcessRunner, "run", autospec=True, side_effect=run),
            patch.object(command.Spinner, "start"),
            patch.object(command.Spinner, "stop"),
        ):
            result = command.run_process_capture(
                ["tool"],
                "test tool",
                tool_id="tool",
                line_handler=lines.append,
            )

        self.assertEqual(lines, ["first", "second"])
        self.assertEqual(result.stdout.text(), "first\nsecond\r\n")

    def test_line_handler_failure_terminates_running_process(self) -> None:
        def fail(_line: str) -> None:
            raise RuntimeError("bad progress parser")

        with (
            patch.object(command.Spinner, "start"),
            patch.object(command.Spinner, "stop"),
            self.assertRaisesRegex(RuntimeError, "bad progress parser"),
        ):
            command.run_process_capture(
                [sys.executable, "-c", "import time; print('progress', flush=True); time.sleep(30)"],
                "test tool",
                tool_id="python",
                line_handler=fail,
            )

    def test_keyboard_interrupt_from_line_handler_terminates_process(self) -> None:
        def interrupt(_line: str) -> None:
            raise KeyboardInterrupt

        with (
            patch.object(command.Spinner, "start"),
            patch.object(command.Spinner, "stop"),
            self.assertRaises(KeyboardInterrupt),
        ):
            command.run_process_capture(
                [sys.executable, "-c", "import time; print('progress', flush=True); time.sleep(30)"],
                "test tool",
                tool_id="python",
                line_handler=interrupt,
            )

    def test_large_ignored_output_is_truncated_without_failing_command(self) -> None:
        with (
            patch.object(command.Spinner, "start"),
            patch.object(command.Spinner, "stop"),
        ):
            result = command.run_process_capture(
                [sys.executable, "-c", "import os; os.write(1, b'x' * 100000)"],
                "test tool",
                tool_id="python",
                capture_limit_bytes=4096,
                capture_overflow=CaptureOverflowPolicy.TRUNCATE,
            )

        self.assertEqual(len(result.stdout.capture), 4096)

    def test_ffprobe_uses_configured_binary_and_keyword_options(self) -> None:
        result = ProcessResult(
            tool_run_id="run-id",
            returncode=0,
            elapsed_ms=10,
            stdout=ProcessOutputSnapshot(b'{"streams": []}', b"", 15, 15, 0, 0),
            stderr=ProcessOutputSnapshot(b"", b"", 0, 0, 0, 0),
        )
        with (
            patch.object(command.config, "FFPROBE_PATH", Path("/tools/ffprobe")),
            patch.object(command, "run_process_capture", return_value=result) as run,
        ):
            metadata = command.run_ffprobe("movie.mkv", select_streams="v:0")

        self.assertEqual(metadata, {"streams": []})
        process_command = run.call_args.args[0]
        self.assertEqual(process_command[0], Path("/tools/ffprobe"))
        self.assertIn("-select_streams", process_command)
        self.assertIn("v:0", process_command)
        self.assertEqual(process_command[-1], "movie.mkv")
        self.assertEqual(run.call_args.kwargs["tool_id"], "ffprobe")

    def test_ffprobe_preserves_ffmpeg_error_contract(self) -> None:
        error = command.subprocess.CalledProcessError(
            returncode=1,
            cmd=["ffprobe"],
            output="out",
            stderr="err",
        )
        with (
            patch.object(command, "run_process_capture", side_effect=error),
            self.assertRaises(command.ffmpeg.Error) as raised,
        ):
            command.run_ffprobe("movie.mkv")

        self.assertEqual(raised.exception.stdout, b"out")
        self.assertEqual(raised.exception.stderr, b"err")

    def test_ffprobe_rejects_invalid_utf8(self) -> None:
        result = ProcessResult(
            tool_run_id="run-id",
            returncode=0,
            elapsed_ms=10,
            stdout=ProcessOutputSnapshot(b"\xff", b"", 1, 1, 0, 1),
            stderr=ProcessOutputSnapshot(b"", b"", 0, 0, 0, 0),
        )
        with (
            patch.object(command, "run_process_capture", return_value=result),
            self.assertRaises(UnicodeDecodeError),
        ):
            command.run_ffprobe("movie.mkv")

    def test_ffprobe_translates_runner_failures_but_preserves_cancellation(self) -> None:
        runner_error = ProcessOutputLimitError("FFprobe exceeded its output capture limit")
        runner_error.attach_output(
            ProcessOutputSnapshot(b"partial", b"", 7, 7, 0, 0),
            ProcessOutputSnapshot(b"details", b"", 7, 7, 0, 0),
        )
        with (
            patch.object(command, "run_process_capture", side_effect=runner_error),
            self.assertRaises(command.ffmpeg.Error) as raised,
        ):
            command.run_ffprobe("movie.mkv")

        self.assertEqual(raised.exception.stdout, b"partial")
        self.assertIn(b"details", raised.exception.stderr)
        self.assertIn(b"capture limit", raised.exception.stderr)

        cancellation = ProcessCancelled("cancelled")
        with (
            patch.object(command, "run_process_capture", side_effect=cancellation),
            self.assertRaises(ProcessCancelled) as cancelled,
        ):
            command.run_ffprobe("movie.mkv")

        self.assertIs(cancelled.exception, cancellation)

    def test_ffmpeg_error_includes_truncated_prefix_and_final_tail(self) -> None:
        error = ProcessExecutionError(
            1,
            ["ffmpeg"],
            ProcessOutputSnapshot(b"out", b"", 3, 3, 0, 0),
            ProcessOutputSnapshot(b"prefix", b"terminal error", 100, 6, 94, 0),
        )
        with (
            patch.object(command.ffmpeg, "compile", return_value=["ffmpeg"]),
            patch.object(command, "run_process_capture", side_effect=error),
            self.assertRaises(command.ffmpeg.Error) as raised,
        ):
            command.run_ffmpeg_capture("stream")

        self.assertEqual(raised.exception.stdout, b"out")
        self.assertIn(b"output truncated", raised.exception.stderr)
        self.assertTrue(raised.exception.stderr.endswith(b"terminal error"))

    def test_ffmpeg_capture_uses_compiled_command_and_separate_streams(self) -> None:
        result = ProcessResult(
            tool_run_id="run-id",
            returncode=0,
            elapsed_ms=10,
            stdout=ProcessOutputSnapshot(b"out", b"", 3, 3, 0, 0),
            stderr=ProcessOutputSnapshot(b"err", b"", 3, 3, 0, 0),
        )
        compiled = ["/tools/ffmpeg", "-i", "movie.mkv", "null"]
        with (
            patch.object(command.ffmpeg, "compile", return_value=compiled) as compile_command,
            patch.object(command, "run_process_capture", return_value=result) as run,
        ):
            stdout, stderr = command.run_ffmpeg_capture("stream", overwrite_output=True)

        self.assertEqual((stdout, stderr), (b"out", b"err"))
        compile_command.assert_called_once_with(
            "stream",
            cmd=command.config.FFMPEG_PATH.as_posix(),
            overwrite_output=True,
        )
        self.assertEqual(run.call_args.args[0], compiled)
        self.assertEqual(run.call_args.kwargs["tool_id"], "ffmpeg")

    def test_ffmpeg_spinner_wrapper_forwards_runtime_context(self) -> None:
        result = ProcessResult(
            tool_run_id="run-id",
            returncode=0,
            elapsed_ms=10,
            stdout=ProcessOutputSnapshot(b"", b"", 0, 0, 0, 0),
            stderr=ProcessOutputSnapshot(b"", b"", 0, 0, 0, 0),
        )
        compiled = ["/tools/ffmpeg", "-i", "movie.mkv", "out.mov"]
        run_context = Mock()
        cancellation_event = threading.Event()
        with (
            patch.object(command.ffmpeg, "compile", return_value=compiled),
            patch.object(command, "run_process_capture", return_value=result) as run,
            patch.object(command.Spinner, "start"),
            patch.object(command.Spinner, "stop"),
        ):
            command.run_ffmpeg_print_errors(
                "stream",
                "encode",
                run_context=run_context,
                cancellation_event=cancellation_event,
                overwrite_output=True,
            )

        self.assertIs(run.call_args.kwargs["run_context"], run_context)
        self.assertIs(run.call_args.kwargs["cancellation_event"], cancellation_event)

    def test_ffmpeg_spinner_wrapper_presents_bounded_stderr_for_cli(self) -> None:
        stderr = b"x" * 20_000 + b"terminal failure"
        error = ProcessExecutionError(
            1,
            ["ffmpeg"],
            ProcessOutputSnapshot(b"", b"", 0, 0, 0, 0),
            ProcessOutputSnapshot(stderr, b"", len(stderr), len(stderr), 0, 0),
        )
        with (
            patch.object(command.ffmpeg, "compile", return_value=["ffmpeg"]),
            patch.object(command, "run_process_capture", side_effect=error),
            patch.object(command, "cli_message") as present,
            self.assertRaises(command.ffmpeg.Error),
        ):
            command.run_ffmpeg_print_errors("stream", "encode")

        message = present.call_args.args[0]
        self.assertIn("earlier FFmpeg output omitted", message)
        self.assertTrue(message.endswith("terminal failure"))
        self.assertLessEqual(len(message.encode("utf-8")), 17 * 1024)
        self.assertIsNone(present.call_args.kwargs["run_context"])


if __name__ == "__main__":
    unittest.main()
