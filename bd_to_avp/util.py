import os
import subprocess
import sys
import threading
import time

from pathlib import Path
from typing import Any, Callable

import ffmpeg  # type: ignore

from bd_to_avp.config import config


class Spinner:
    symbols = ["⣾", "⣽", "⣻", "⢿", "⡿", "⣟", "⣯", "⣷"]

    def __init__(self, command_name: str = "command...", update_interval: float = 0.5):
        self.command_name = command_name
        self.stop_spinner_flag = False
        self.update_interval = update_interval
        self.current_symbol = 0

    def _update_spinner(self) -> None:
        if not self.stop_spinner_flag:
            sys.stdout.write(
                f"\rRunning {self.command_name} {self.symbols[self.current_symbol]}"
            )
            sys.stdout.flush()
            self.current_symbol = (self.current_symbol + 1) % len(self.symbols)

    def start(self, update_func: Callable[[str], None] | None = None) -> None:
        self.stop_spinner_flag = False
        if update_func:
            update_func(f"Running {self.command_name}")
        else:
            print(f"Running {self.command_name}", end="", flush=True)

        while not self.stop_spinner_flag:
            self._update_spinner()
            time.sleep(self.update_interval)

    def stop(self, update_func: Callable[[str], None] | None = None) -> None:
        self.stop_spinner_flag = True
        if update_func:
            update_func(f"Finished {self.command_name}")
        else:
            print(f"\rFinished {self.command_name}")


def normalize_command_elements(command: list[Any]) -> list[str | Path | bytes]:
    return [
        str(item) if not isinstance(item, (str, bytes, Path)) else item
        for item in command
        if item is not None
    ]


def run_command(
    command_list: list[Any], command_name: str = "", env: dict[str, str] | None = None
) -> str:
    command_list = normalize_command_elements(command_list)
    if not command_name:
        command_name = str(command_list[0])

    if config.output_commands:
        print(f"Running command:\n{' '.join(str(command) for command in command_list)}")

    env = env if env else os.environ.copy()
    output_lines = []
    spinner = Spinner(command_name)
    spinner_thread = threading.Thread(target=spinner.start)
    spinner_thread.start()
    process = None
    try:

        process = subprocess.Popen(
            command_list,
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
            raise subprocess.CalledProcessError(
                process.returncode, command_list, output="".join(output_lines)
            )
    except KeyboardInterrupt:
        print("\nCommand interrupted.")
        if process:
            process.terminate()
        raise

    finally:
        spinner.stop()
        spinner_thread.join()
    return "".join(output_lines)


def run_ffmpeg_print_errors(stream_spec: Any, quiet: bool = True, **kwargs) -> None:
    kwargs["quiet"] = quiet
    if config.output_commands:
        print(f"Running command:\n{ffmpeg.compile(stream_spec)}")
    try:
        ffmpeg.run(stream_spec, **kwargs)
    except ffmpeg.Error as e:
        print("FFmpeg Error:")
        print("STDOUT:", e.stdout.decode("utf-8") if e.stdout else "")
        print("STDERR:", e.stderr.decode("utf-8") if e.stderr else "")
        raise


def run_ffmpeg_async(command_list: list[Any], log_path: Path) -> subprocess.Popen:
    command_list = normalize_command_elements(command_list)
    if config.output_commands:
        print(f"Running command:\n{' '.join(str(command) for command in command_list)}")
    with open(log_path, "w") as log_file:
        process = subprocess.Popen(
            command_list, stdout=log_file, stderr=subprocess.STDOUT, text=True
        )
    return process


def cleanup_process(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()


def generate_ffmpeg_wrapper_command(
    input_fifo: Path,
    output_path: Path,
    color_depth: int,
    resolution: str,
    frame_rate: str,
    bitrate: int,
    crop_params: str,
    software_encoder: bool,
) -> list[Any]:
    pix_fmt = "yuv420p10le" if color_depth == 10 else "yuv420p"
    stream = ffmpeg.input(
        str(input_fifo),
        f="rawvideo",
        pix_fmt=pix_fmt,
        s=config.resolution or resolution,
        r=config.frame_rate or frame_rate,
    )
    if crop_params:
        stream = ffmpeg.filter(stream, "crop", *crop_params.split(":"))
    stream = ffmpeg.output(
        stream,
        f"file:{output_path}",
        vcodec="hevc_videotoolbox" if not software_encoder else "libx265",
        video_bitrate=f"{bitrate}M",
        bufsize=f"{bitrate * 2}M",
        tag="hvc1",
        vprofile="main10" if color_depth == 10 else "main",
    )

    args = ffmpeg.compile(stream, overwrite_output=True)
    return args


class OutputHandler:
    def __init__(self, write_func: Callable[[str], None] | None = None):
        self.write_func = write_func

    def write(self, text: str) -> None:
        if text:  # Ignore empty lines
            sys.__stdout__.write(text)  # Write to the terminal
            if self.write_func:
                self.write_func(text.rstrip("\n"))  # Emit the signal for the GUI

    @staticmethod
    def flush() -> None:
        sys.__stdout__.flush()
