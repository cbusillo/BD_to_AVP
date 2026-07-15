import io
import unittest

from unittest.mock import Mock, call, patch

from bd_to_avp.modules import command


class RunCommandTests(unittest.TestCase):
    def test_line_handler_receives_streamed_output(self) -> None:
        process = Mock()
        process.stdout = io.StringIO("first\nsecond\r\n")
        process.returncode = 0
        lines: list[str] = []

        with (
            patch.object(command.subprocess, "Popen", return_value=process),
            patch.object(command.Spinner, "start"),
            patch.object(command.Spinner, "stop"),
        ):
            output = command.run_command(["tool"], line_handler=lines.append)

        self.assertEqual(lines, ["first", "second"])
        self.assertEqual(output, "first\nsecond\r\n")
        process.wait.assert_called_once_with()

    def test_line_handler_failure_terminates_running_process(self) -> None:
        process = Mock()
        process.stdout = io.StringIO("progress\n")
        process.poll.return_value = None

        def fail(_line: str) -> None:
            raise RuntimeError("bad progress parser")

        with (
            patch.object(command.subprocess, "Popen", return_value=process),
            patch.object(command.Spinner, "start"),
            patch.object(command.Spinner, "stop"),
            self.assertRaisesRegex(RuntimeError, "bad progress parser"),
        ):
            command.run_command(["tool"], line_handler=fail)

        process.terminate.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=command.PROCESS_TERMINATION_TIMEOUT_SECONDS)

    def test_line_handler_failure_kills_process_after_termination_timeout(self) -> None:
        process = Mock()
        process.stdout = io.StringIO("progress\n")
        process.poll.return_value = None
        process.wait.side_effect = [
            command.subprocess.TimeoutExpired(cmd="tool", timeout=command.PROCESS_TERMINATION_TIMEOUT_SECONDS),
            None,
        ]

        def fail(_line: str) -> None:
            raise RuntimeError("bad progress parser")

        with (
            patch.object(command.subprocess, "Popen", return_value=process),
            patch.object(command.Spinner, "start"),
            patch.object(command.Spinner, "stop"),
            self.assertRaisesRegex(RuntimeError, "bad progress parser"),
        ):
            command.run_command(["tool"], line_handler=fail)

        process.terminate.assert_called_once_with()
        process.kill.assert_called_once_with()
        self.assertEqual(
            process.wait.call_args_list,
            [
                call(timeout=command.PROCESS_TERMINATION_TIMEOUT_SECONDS),
                call(),
            ],
        )

    def test_keyboard_interrupt_terminates_running_process(self) -> None:
        process = Mock()
        process.stdout = io.StringIO("progress\n")
        process.poll.return_value = None

        def interrupt(_line: str) -> None:
            raise KeyboardInterrupt

        with (
            patch.object(command.subprocess, "Popen", return_value=process),
            patch.object(command.Spinner, "start"),
            patch.object(command.Spinner, "stop"),
            self.assertRaises(KeyboardInterrupt),
        ):
            command.run_command(["tool"], line_handler=interrupt)

        process.terminate.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=command.PROCESS_TERMINATION_TIMEOUT_SECONDS)


if __name__ == "__main__":
    unittest.main()
