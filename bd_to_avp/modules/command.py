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


def run_command(
    commands: list[Any],
    command_name: str = "",
    env: dict[str, str] | None = None,
    *,
    line_handler: LineHandler | None = None,
    progress_parser: ProgressParser | None = None,
    output_observer: OutputObserver | None = None,
    run_context: RunContext | None = None,
    cancellation_event: threading.Event | None = None,
    tool_id: str | None = None,
    observability_context: ObservabilityContext | None = None,
    artifacts: tuple[ProcessArtifactProbe, ...] = (),
    capture_limit_bytes: int | None = None,
    capture_overflow: CaptureOverflowPolicy = CaptureOverflowPolicy.TRUNCATE,
) -> str:
    commands = normalize_command_elements(commands)
    if not command_name:
        command_name = str(commands[0])

    if config.output_commands:
        commands_to_print = add_quotes_to_path_if_space(commands)
        print(f"Running command:\n{' '.join(str(command) for command in commands_to_print)}")

    spinner = Spinner(command_name)
    spinner_update_func = get_spinner_update_func()
    spinner_thread = threading.Thread(target=spinner.start, args=(spinner_update_func,))
    spinner_thread.start()
    try:
        result = ChildProcessRunner().run(
            ProcessSpec(
                argv=tuple(commands),
                tool_id=tool_id or default_tool_id(commands[0]),
                display_name=command_name,
                env=env if env is not None else os.environ.copy(),
                event_context=observability_context or ObservabilityContext(),
                artifacts=artifacts,
                capture_limit_bytes=(
                    DEFAULT_CAPTURE_LIMIT_BYTES if capture_limit_bytes is None else capture_limit_bytes
                ),
                capture_overflow=capture_overflow,
            ),
            run_context=run_context,
            cancellation_event=cancellation_event,
            line_handler=(None if line_handler is None else lambda _stream, line: line_handler(line)),
            output_observer=(None if output_observer is None else lambda _stream, payload: output_observer(payload)),
            progress_parser=(None if progress_parser is None else lambda _stream, line: progress_parser(line)),
        )
        return result.stdout.text()
    except subprocess.CalledProcessError as error:
        print("Error running command:", command_name)
        if error.output:
            print(error.output)
        raise
    except KeyboardInterrupt:
        print("\nCommand interrupted.")
        raise
    finally:
        spinner.stop(spinner_update_func)
        spinner_thread.join()


def default_tool_id(command: str | Path | bytes) -> str:
    decoded = os.fsdecode(command)
    name = Path(decoded).name.strip().lower()
    normalized = "".join(character if character.isalnum() else "_" for character in name).strip("_")
    if not normalized or len(normalized.encode("utf-8")) > 128:
        return "external_tool"
    return normalized


def run_process_capture(
    commands: list[Any],
    command_name: str,
    *,
    tool_id: str,
    merge_stderr: bool = False,
    run_context: RunContext | None = None,
    cancellation_event: threading.Event | None = None,
    observability_context: ObservabilityContext | None = None,
    timeout_seconds: float | None = None,
    stdout: int | BinaryIO = subprocess.PIPE,
    artifacts: tuple[ProcessArtifactProbe, ...] = (),
    capture_limit_bytes: int = DEFAULT_CAPTURE_LIMIT_BYTES,
    capture_overflow: CaptureOverflowPolicy = CaptureOverflowPolicy.FAIL,
    show_command: bool = True,
) -> ProcessResult:
    normalized_commands = normalize_command_elements(commands)
    if show_command and config.output_commands:
        print("Running command:\n" + " ".join(add_quotes_to_path_if_space(normalized_commands)))
    return ChildProcessRunner().run(
        ProcessSpec(
            argv=tuple(normalized_commands),
            tool_id=tool_id,
            display_name=command_name,
            env=os.environ.copy(),
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
    )


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
    quiet: bool = True,
    *,
    run_context: RunContext | None = None,
    cancellation_event: threading.Event | None = None,
    observability_context: ObservabilityContext | None = None,
    **kwargs: object,
) -> None:
    del quiet
    overwrite_output = bool(kwargs.pop("overwrite_output", False))
    if kwargs:
        unexpected = ", ".join(sorted(kwargs))
        raise TypeError(f"Unsupported FFmpeg execution options: {unexpected}")
    command = ffmpeg.compile(
        stream_spec,
        cmd=config.FFMPEG_PATH.as_posix(),
        overwrite_output=overwrite_output,
    )
    if config.output_commands:
        output_commands_str = " ".join(add_quotes_to_path_if_space(command))

        print(f"Running command:\n{output_commands_str}")
    spinner = Spinner(message)
    spinner_update_func = get_spinner_update_func()
    spinner_thread = threading.Thread(target=spinner.start, args=(spinner_update_func,))
    spinner_thread.start()
    try:
        try:
            run_process_capture(
                command,
                message,
                tool_id="ffmpeg",
                run_context=run_context,
                cancellation_event=cancellation_event,
                observability_context=observability_context,
                capture_overflow=CaptureOverflowPolicy.TRUNCATE,
                show_command=False,
            )
        except subprocess.CalledProcessError as error:
            raise ffmpeg_called_process_error(error, "ffmpeg") from error
        except ProcessCancelled:
            raise
        except ProcessRunnerError as error:
            raise ffmpeg_runner_error(error, "ffmpeg") from error
    except ffmpeg.Error as e:
        print("FFmpeg Error:")
        print("STDOUT:", e.stdout.decode("utf-8", errors="replace") if e.stdout else "")
        print("STDERR:", e.stderr.decode("utf-8", errors="replace") if e.stderr else "")
        raise
    finally:
        spinner.stop(spinner_update_func)
        spinner_thread.join()


def cleanup_process(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()


def terminate_process() -> None:
    Spinner.stop_all()
    kill_processes_by_name(config.PROCESS_NAMES_TO_KILL)


def kill_processes_by_name(process_names: list[str]) -> None:
    threads = []
    for process_name in process_names:
        thread = threading.Thread(target=kill_process_by_name, args=(process_name,))
        threads.append(thread)
        thread.start()


def kill_process_by_name(process_name: str) -> None:
    try:
        subprocess.run(["pkill", "-f", process_name], check=True)
    except subprocess.CalledProcessError as error:
        if error.returncode != 1:
            raise
