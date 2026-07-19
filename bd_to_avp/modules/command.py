import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO, Callable, ClassVar, cast

import ffmpeg

from bd_to_avp.observability import ObservabilityContext, ObservabilityProgress
from bd_to_avp.modules.config import config
from bd_to_avp.modules.util import formatted_time_elapsed
from bd_to_avp.presentation import cli_message
from bd_to_avp.process_runner import (
    CaptureOverflowPolicy,
    ChildProcessRunner,
    DEFAULT_CAPTURE_LIMIT_BYTES,
    ProcessArtifactProbe,
    ProcessCancelled,
    ProcessExecutionError,
    ProcessOutputSnapshot,
    ProcessResult,
    ProcessRunnerError,
    ProcessSpec,
)
from bd_to_avp.runtime import RunContext


SpinnerUpdate = Callable[[str], object]
LineHandler = Callable[[str], object]
ProgressParser = Callable[[str], ObservabilityProgress | None]
OutputObserver = Callable[[bytes], object]


def get_spinner_update_func() -> SpinnerUpdate | None:
    stdout = cast(Any, sys.stdout)
    if not hasattr(stdout, "current_writer"):
        return None
    writer = stdout.current_writer()
    return writer if callable(writer) else None


class Spinner:
    symbols: ClassVar[list[str]] = ["🌑", "🌘", "🌗", "🌖", "🌕", "🌔", "🌓", "🌒"]
    _stop_all_spinners = False

    def __init__(self, command_name: str = "command...", update_interval: float = 0.5):
        self.command_name = command_name
        self.stop_spinner_flag = False
        self.update_interval = update_interval
        self.current_symbol = 0
        self.start_time = datetime.now()

    def _update_spinner(self, update_func: SpinnerUpdate | None = None) -> None:
        if not self.stop_spinner_flag:
            message = f"\rRunning {self.command_name} {self.symbols[self.current_symbol]}"
            if update_func:
                update_func(message)
            else:
                sys.stdout.write(message)
                sys.stdout.flush()
            self.current_symbol = (self.current_symbol + 1) % len(self.symbols)

    def start(self, update_func: SpinnerUpdate | None = None) -> None:
        self.stop_spinner_flag = False
        Spinner._stop_all_spinners = False
        if update_func:
            update_func(f"Running {self.command_name}")
        else:
            print(f"Running {self.command_name}", end="", flush=True)

        while not self.stop_spinner_flag and not Spinner._stop_all_spinners:
            self._update_spinner(update_func)
            time.sleep(self.update_interval)

    def stop(self, update_func: SpinnerUpdate | None = None) -> None:
        self.stop_spinner_flag = True
        time_elapsed_formatted = formatted_time_elapsed(self.start_time)
        message = f"\rFinished {self.command_name} in {time_elapsed_formatted}"
        if update_func:
            update_func(message)
        else:
            print(f"\r{message}")

    @classmethod
    def stop_all(cls) -> None:
        cls._stop_all_spinners = True


def add_quotes_to_path_if_space(commands: list[str | Path | bytes]) -> list[str]:
    commands_with_paths_as_strings = [
        f'"{command}"'
        if isinstance(command, Path) and " " in command.as_posix()
        else f'"{command}"'
        if isinstance(command, str) and " " in command
        else str(command)
        for command in commands
    ]
    return commands_with_paths_as_strings


def normalize_command_elements(command: list[Any]) -> list[str | Path | bytes]:
    return [str(item) if not isinstance(item, (str, bytes, Path)) else item for item in command if item is not None]


def command_line_options(options: dict[str, object]) -> list[str]:
    arguments: list[str] = []
    for key in sorted(options):
        arguments.append(f"-{key}")
        value = options[key]
        if value is not None:
            arguments.append(str(value))
    return arguments


def run_process_capture(
    commands: list[Any],
    command_name: str,
    *,
    tool_id: str,
    env: dict[str, str] | None = None,
    merge_stderr: bool = False,
    line_handler: LineHandler | None = None,
    progress_parser: ProgressParser | None = None,
    output_observer: OutputObserver | None = None,
    run_context: RunContext | None = None,
    cancellation_event: threading.Event | None = None,
    observability_context: ObservabilityContext | None = None,
    timeout_seconds: float | None = None,
    stdout: int | BinaryIO = subprocess.PIPE,
    artifacts: tuple[ProcessArtifactProbe, ...] = (),
    capture_limit_bytes: int = DEFAULT_CAPTURE_LIMIT_BYTES,
    capture_overflow: CaptureOverflowPolicy = CaptureOverflowPolicy.FAIL,
    show_command: bool = True,
    show_spinner: bool = False,
) -> ProcessResult:
    normalized_commands = normalize_command_elements(commands)
    if show_command and config.output_commands and run_context is None:
        print("Running command:\n" + " ".join(add_quotes_to_path_if_space(normalized_commands)))
    spinner = Spinner(command_name) if show_spinner and run_context is None else None
    spinner_update_func = get_spinner_update_func() if spinner is not None else None
    spinner_thread: threading.Thread | None = None
    if spinner is not None:
        spinner_thread = threading.Thread(target=spinner.start, args=(spinner_update_func,))
        spinner_thread.start()
    try:
        return ChildProcessRunner().run(
            ProcessSpec(
                argv=tuple(normalized_commands),
                tool_id=tool_id,
                display_name=command_name,
                env=env if env is not None else os.environ.copy(),
                merge_stderr=merge_stderr,
                stdout=stdout,
                event_context=observability_context or ObservabilityContext(),
                artifacts=artifacts,
                timeout_seconds=timeout_seconds,
                capture_limit_bytes=capture_limit_bytes,
                capture_overflow=capture_overflow,
            ),
            run_context=run_context,
            cancellation_event=cancellation_event,
            line_handler=(None if line_handler is None else lambda _stream, line: line_handler(line)),
            output_observer=(None if output_observer is None else lambda _stream, payload: output_observer(payload)),
            progress_parser=(None if progress_parser is None else lambda _stream, line: progress_parser(line)),
        )
    except KeyboardInterrupt:
        if run_context is None:
            print("\nCommand interrupted.")
        raise
    finally:
        if spinner is not None and spinner_thread is not None:
            spinner.stop(spinner_update_func)
            spinner_thread.join()


def combined_process_output(result: ProcessResult) -> str:
    return "\n".join(output for output in (result.stdout.text(), result.stderr.text()) if output)


def ffmpeg_diagnostic_output(snapshot: ProcessOutputSnapshot | None) -> bytes:
    if snapshot is None:
        return b""
    if not snapshot.truncated:
        return snapshot.capture
    return snapshot.capture + b"\n[... output truncated; final output follows ...]\n" + snapshot.tail


def ffmpeg_called_process_error(error: subprocess.CalledProcessError, executable: str) -> ffmpeg.Error:
    if isinstance(error, ProcessExecutionError):
        stdout = ffmpeg_diagnostic_output(error.stdout_snapshot)
        stderr = ffmpeg_diagnostic_output(error.stderr_snapshot)
    else:
        stdout = error.output.encode("utf-8", errors="replace") if isinstance(error.output, str) else error.output
        stderr = error.stderr.encode("utf-8", errors="replace") if isinstance(error.stderr, str) else error.stderr
    return ffmpeg.Error(executable, stdout or b"", stderr or b"")


def ffmpeg_runner_error(error: ProcessRunnerError, executable: str) -> ffmpeg.Error:
    stdout = ffmpeg_diagnostic_output(error.stdout_snapshot)
    stderr = ffmpeg_diagnostic_output(error.stderr_snapshot)
    message = str(error).encode("utf-8", errors="replace")
    stderr = stderr + (b"\n" if stderr else b"") + message
    return ffmpeg.Error(executable, stdout, stderr)


def present_ffmpeg_error(error: ffmpeg.Error, run_context: RunContext | None) -> None:
    if not error.stderr:
        return
    maximum_bytes = 16 * 1024
    diagnostic = error.stderr
    if len(diagnostic) > maximum_bytes:
        diagnostic = b"[... earlier FFmpeg output omitted ...]\n" + diagnostic[-maximum_bytes:]
    cli_message(f"FFmpeg Error:\n{diagnostic.decode('utf-8', errors='replace')}", run_context=run_context)


def run_ffmpeg_capture(
    stream_spec: Any,
    *,
    overwrite_output: bool = False,
    run_context: RunContext | None = None,
    cancellation_event: threading.Event | None = None,
    observability_context: ObservabilityContext | None = None,
) -> tuple[bytes, bytes]:
    command = ffmpeg.compile(
        stream_spec,
        cmd=config.FFMPEG_PATH.as_posix(),
        overwrite_output=overwrite_output,
    )
    try:
        result = run_process_capture(
            command,
            "FFmpeg",
            tool_id="ffmpeg",
            run_context=run_context,
            cancellation_event=cancellation_event,
            observability_context=observability_context,
        )
    except subprocess.CalledProcessError as error:
        raise ffmpeg_called_process_error(error, "ffmpeg") from error
    except ProcessCancelled:
        raise
    except ProcessRunnerError as error:
        raise ffmpeg_runner_error(error, "ffmpeg") from error
    return result.stdout.capture, result.stderr.capture


def run_ffprobe(
    input_path: str | Path,
    *,
    run_context: RunContext | None = None,
    cancellation_event: threading.Event | None = None,
    observability_context: ObservabilityContext | None = None,
    **kwargs: object,
) -> dict[str, Any]:
    command: list[Any] = [
        config.FFPROBE_PATH,
        "-show_format",
        "-show_streams",
        "-of",
        "json",
        *command_line_options(kwargs),
        input_path,
    ]
    try:
        result = run_process_capture(
            command,
            "FFprobe",
            tool_id="ffprobe",
            run_context=run_context,
            cancellation_event=cancellation_event,
            observability_context=observability_context,
        )
    except subprocess.CalledProcessError as error:
        raise ffmpeg_called_process_error(error, "ffprobe") from error
    except ProcessCancelled:
        raise
    except ProcessRunnerError as error:
        raise ffmpeg_runner_error(error, "ffprobe") from error
    return cast(dict[str, Any], json.loads(result.stdout.capture.decode("utf-8")))


def run_ffmpeg_print_errors(
    stream_spec: Any,
    message: str,
    *,
    overwrite_output: bool = False,
    run_context: RunContext | None = None,
    cancellation_event: threading.Event | None = None,
    observability_context: ObservabilityContext | None = None,
) -> None:
    command = ffmpeg.compile(
        stream_spec,
        cmd=config.FFMPEG_PATH.as_posix(),
        overwrite_output=overwrite_output,
    )
    try:
        run_process_capture(
            command,
            message,
            tool_id="ffmpeg",
            run_context=run_context,
            cancellation_event=cancellation_event,
            observability_context=observability_context,
            capture_overflow=CaptureOverflowPolicy.TRUNCATE,
            show_spinner=True,
        )
    except subprocess.CalledProcessError as error:
        ffmpeg_error = ffmpeg_called_process_error(error, "ffmpeg")
        present_ffmpeg_error(ffmpeg_error, run_context)
        raise ffmpeg_error from error
    except ProcessCancelled:
        raise
    except ProcessRunnerError as error:
        ffmpeg_error = ffmpeg_runner_error(error, "ffmpeg")
        present_ffmpeg_error(ffmpeg_error, run_context)
        raise ffmpeg_error from error
