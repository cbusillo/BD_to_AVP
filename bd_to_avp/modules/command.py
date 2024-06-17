import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import ffmpeg

from bd_to_avp.modules.config import config
from bd_to_avp.modules.util import formatted_time_elapsed


class Spinner:
    symbols = ["🌑", "🌘", "🌗", "🌖", "🌕", "🌔", "🌓", "🌒"]
    _stop_all_spinners = False

    def __init__(self, command_name: str = "command...", update_interval: float = 0.5):
        self.command_name = command_name
        self.stop_spinner_flag = False
        self.update_interval = update_interval
        self.current_symbol = 0
        self.start_time = datetime.now()

    def _update_spinner(self) -> None:
        if not self.stop_spinner_flag:
            sys.stdout.write(f"\rRunning {self.command_name} {self.symbols[self.current_symbol]}")
            sys.stdout.flush()
            self.current_symbol = (self.current_symbol + 1) % len(self.symbols)

    def start(self, update_func: Callable[[str], None] | None = None) -> None:
        self.stop_spinner_flag = False
        Spinner._stop_all_spinners = False
        if update_func:
            update_func(f"Running {self.command_name}")
        else:
            print(f"Running {self.command_name}", end="", flush=True)

        while not self.stop_spinner_flag and not Spinner._stop_all_spinners:
            self._update_spinner()
            time.sleep(self.update_interval)

    def stop(self, update_func: Callable[[str], None] | None = None) -> None:
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
        (f'"{command}"' if isinstance(command, Path) and " " in command.as_posix() else str(command))
        for command in commands
    ]
    return commands_with_paths_as_strings


def normalize_command_elements(command: list[Any]) -> list[str | Path | bytes]:
    return [str(item) if not isinstance(item, (str, bytes, Path)) else item for item in command if item is not None]


def run_command(commands: list[Any], command_name: str = "", env: dict[str, str] | None = None) -> str:
    commands = normalize_command_elements(commands)
    if not command_name:
        command_name = str(commands[0])

    if config.output_commands:
        commands_to_print = add_quotes_to_path_if_space(commands)
        print(f"Running command:\n{' '.join(str(command) for command in commands_to_print)}")

    env = env if env else os.environ.copy()
    output_lines = []
    spinner = Spinner(command_name)
    spinner_thread = threading.Thread(target=spinner.start)
    spinner_thread.start()
    process = None
    try:

        process = subprocess.Popen(
            commands,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )
        while True and process and process.stdout:
            line = process.stdout.readline()
            if not line:
                break
            output_lines.append(line)

        process.wait()
        if process.returncode != 0:
            print("Error running command:", command_name)
            print("\n".join(output_lines))
            raise subprocess.CalledProcessError(process.returncode, commands, output="".join(output_lines))
    except KeyboardInterrupt:
        print("\nCommand interrupted.")
        if process:
            process.terminate()
        raise

    finally:
        spinner.stop()
        spinner_thread.join()
    return "".join(output_lines)


def run_ffmpeg_print_errors(stream_spec: Any, message: str, quiet: bool = True, **kwargs) -> None:
    kwargs["quiet"] = quiet
    if config.output_commands:
        output_commands_quoted = add_quotes_to_path_if_space(ffmpeg.compile(stream_spec))
        output_commands_str = " ".join(output_commands_quoted)

        print(f"Running command:\n{output_commands_str}")
    spinner = Spinner(message)
    spinner_thread = threading.Thread(target=spinner.start)
    spinner_thread.start()
    try:
        ffmpeg.run(stream_spec, **kwargs)
    except ffmpeg.Error as e:
        print("FFmpeg Error:")
        print("STDOUT:", e.stdout.decode("utf-8") if e.stdout else "")
        print("STDERR:", e.stderr.decode("utf-8") if e.stderr else "")
        raise
    finally:
        spinner.stop()
        spinner_thread.join()


def run_ffmpeg_async(command_list: list[Any], log_path: Path) -> subprocess.Popen:
    command_list = normalize_command_elements(command_list)
    if config.output_commands:
        print(f"Running command:\n{' '.join(str(command) for command in command_list)}")
    with open(log_path, "w") as log_file:
        process = subprocess.Popen(command_list, stdout=log_file, stderr=subprocess.STDOUT, text=True)
    return process


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
    except subprocess.CalledProcessError:
        pass
