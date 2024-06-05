import itertools
import os
import subprocess
import sys
import threading

from pathlib import Path
from time import sleep
from typing import Any

import ffmpeg  # type: ignore

from bd_to_avp.config import config


class Spinner:
    def __init__(self, command_name: str = "command..."):
        self.command_name = command_name
        self.stop_spinner_flag = False
        self.spinner_thread = threading.Thread(target=self._spinner)

    def _spinner(self) -> None:
        spinner_symbols = itertools.cycle(["-", "/", "|", "\\"])
        print(f"Running {self.command_name} ", end="", flush=True)
        while not self.stop_spinner_flag:
            sys.stdout.write(next(spinner_symbols))
            sys.stdout.flush()
            sleep(0.1)
            sys.stdout.write("\b")
        print("\n")

    def start(self) -> None:
        self.stop_spinner_flag = False
        self.spinner_thread.start()

    def stop(self) -> None:
        self.stop_spinner_flag = True
        self.spinner_thread.join()


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
    spinner.start()
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
