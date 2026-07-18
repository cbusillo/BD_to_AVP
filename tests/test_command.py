import sys
import unittest
from unittest.mock import patch

from bd_to_avp.modules import command
from bd_to_avp.process_runner import ProcessOutputSnapshot, ProcessResult, ProcessStream


class RunCommandTests(unittest.TestCase):
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
            output = command.run_command(["tool"], line_handler=lines.append)

        self.assertEqual(lines, ["first", "second"])
        self.assertEqual(output, "first\nsecond\r\n")

    def test_line_handler_failure_terminates_running_process(self) -> None:
        def fail(_line: str) -> None:
            raise RuntimeError("bad progress parser")

        with (
            patch.object(command.Spinner, "start"),
            patch.object(command.Spinner, "stop"),
            self.assertRaisesRegex(RuntimeError, "bad progress parser"),
        ):
            command.run_command(
                [sys.executable, "-c", "import time; print('progress', flush=True); time.sleep(30)"],
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
            command.run_command(
                [sys.executable, "-c", "import time; print('progress', flush=True); time.sleep(30)"],
                line_handler=interrupt,
            )

    def test_default_tool_id_uses_executable_name_without_path(self) -> None:
        self.assertEqual(command.default_tool_id("/Applications/Tool Suite/bin/MakeMKVCon"), "makemkvcon")
        self.assertEqual(command.default_tool_id("***"), "external_tool")

    def test_large_ignored_output_is_truncated_without_failing_command(self) -> None:
        with (
            patch.object(command.Spinner, "start"),
            patch.object(command.Spinner, "stop"),
        ):
            output = command.run_command(
                [sys.executable, "-c", "import os; os.write(1, b'x' * 100000)"],
                capture_limit_bytes=4096,
            )

        self.assertEqual(len(output), 4096)


if __name__ == "__main__":
    unittest.main()
