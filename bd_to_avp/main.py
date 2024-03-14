import argparse
import atexit
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from time import sleep
from typing import Any


@dataclass
class DiscInfo:
    name: str = "Unknown"
    frame_rate: str = "23.976"
    resolution: str = "1920x1080"
    color_depth: int = 8


class Stage(Enum):
    CREATE_MKV = auto()
    EXTRACT_MVC_AUDIO = auto()
    CREATE_LEFT_RIGHT_FILES = auto()
    COMBINE_TO_MV_HEVC = auto()
    TRANSCODE_AUDIO = auto()
    CREATE_FINAL_FILE = auto()


class StageEnumAction(argparse.Action):
    def __init__(self, **kwargs) -> None:
        self.enum_type = kwargs.pop("type", None)
        super(StageEnumAction, self).__init__(**kwargs)

    def __call__(self, parser, namespace, values, option_string=None) -> None:
        if self.enum_type and not isinstance(values, self.enum_type):
            enum_value = self.enum_type[values.upper()]
            setattr(namespace, self.dest, enum_value)
        else:
            setattr(namespace, self.dest, values)


SCRIPT_PATH = Path(__file__).parent
MAKEMKVCON_PATH = Path("/Applications/MakeMKV.app/Contents/MacOS/makemkvcon")
HOMEBREW_PREFIX = Path(os.getenv("HOMEBREW_PREFIX", "/opt/homebrew"))
WINE_PATH = HOMEBREW_PREFIX / "bin/wine"
FFMPEG_PATH = HOMEBREW_PREFIX / "bin/ffmpeg"
FRIM_PATH = SCRIPT_PATH / "FRIM_x64_version_1.31" / "x64"
FRIMDECODE_PATH = FRIM_PATH / "FRIMDecode64.exe"
MP4BOX_PATH = HOMEBREW_PREFIX / "bin" / "MP4Box"
SPATIAL_MEDIA = HOMEBREW_PREFIX / "bin/spatial-media-kit-tool"
IMAGE_EXTENSIONS = [".iso", ".img", ".bin"]

stop_spinner_flag = False


@contextmanager
def mounted_image(image_path: Path):
    mount_point = None
    existing_mounts_command = ["hdiutil", "info"]
    existing_mounts_output = run_command(existing_mounts_command, "Check mounted images")
    try:
        for line in existing_mounts_output.split("\n"):
            if str(image_path) in line:
                mount_line_index = existing_mounts_output.split("\n").index(line) + 1
                while "/dev/disk" not in existing_mounts_output.split("\n")[mount_line_index]:
                    mount_line_index += 1
                mount_point = existing_mounts_output.split("\n")[mount_line_index].split("\t")[-1]
                print(f"ISO is already mounted at {mount_point}")
                break

        if not mount_point:
            mount_command = ["hdiutil", "attach", image_path]
            mount_output = run_command(mount_command, "Mount image")
            for line in mount_output.split("\n"):
                if "/Volumes/" in line:
                    mount_point = line.split("\t")[-1]
                    print(f"ISO mounted successfully at {mount_point}")
                    break

        if not mount_point:
            raise RuntimeError("Failed to mount ISO or find mount point.")

        yield Path(mount_point)

    except Exception as e:
        print(f"Error during ISO mount handling: {e}")
        raise

    finally:
        if mount_point and "ISO is already mounted at" not in existing_mounts_output:
            umount_command = ["hdiutil", "detach", mount_point]
            run_command(umount_command, "Unmount image")
            print(f"ISO unmounted from {mount_point}")


def setup_frim() -> None:
    wine_prefix = Path(os.environ.get("WINEPREFIX", "~/.wine")).expanduser()
    frim_destination_path = wine_prefix / "drive_c/UTL/FRIM"

    if frim_destination_path.exists():
        print(f"{frim_destination_path} already exists. Skipping install.")
        return

    shutil.copytree(FRIM_PATH, frim_destination_path)
    print(f"Copied FRIM directory to {frim_destination_path}")

    reg_file_path = FRIM_PATH / "plugins64.reg"
    if not reg_file_path.exists():
        print(f"Registry file {reg_file_path} not found. Skipping registry update.")
        return

    regedit_command = [WINE_PATH, "regedit", reg_file_path]
    regedit_env = {"WINEPREFIX": str(wine_prefix)}
    run_command(regedit_command, "Update the Windows registry for FRIM plugins.", regedit_env)
    print("Updated the Windows registry for FRIM plugins.")


def remove_folder_if_exists(folder_path: Path) -> None:
    if folder_path.is_dir():
        shutil.rmtree(folder_path)
        print(f"Removed existing directory: {folder_path}")


def spinner(command_name: str = "command...") -> None:
    spinner_symbols = itertools.cycle(["-", "/", "|", "\\"])
    print(f"\nRunning {command_name} ", end="", flush=True)
    while not stop_spinner_flag:
        sys.stdout.write(next(spinner_symbols))
        sys.stdout.flush()
        sleep(0.1)
        sys.stdout.write("\b")


def ensure_str_command(command: list[Any]) -> list[str]:
    return [str(item) if not isinstance(item, (str, bytes, os.PathLike)) else item for item in command]


def run_command(command: list[str], command_name: str = None, env: dict[str, str] = None) -> str:
    command = ensure_str_command(command)
    if not command_name:
        command_name = " ".join(command)

    env = env if env else os.environ.copy()
    global stop_spinner_flag
    output_lines = []
    stop_spinner_flag = False
    spinner_thread = threading.Thread(target=spinner, args=(command_name,))
    spinner_thread.start()

    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
        while True:
            line = process.stdout.readline()
            if not line:
                break
            output_lines.append(line)

        process.wait()
        if process.returncode != 0:
            print("Error running command:", command_name)
            print("\n".join(output_lines))
            raise subprocess.CalledProcessError(process.returncode, command, output="".join(output_lines))
    finally:
        stop_spinner_flag = True
        spinner_thread.join()

    return "".join(output_lines)


def prepare_output_folder_for_source(disc_name: str, terminal_args: argparse.Namespace) -> (str, Path):
    output_path = terminal_args.output_root_folder / disc_name
    if terminal_args.start_stage == list(Stage)[0]:
        remove_folder_if_exists(output_path)
    output_path.mkdir(parents=True, exist_ok=True)
    return output_path


def create_custom_makemkv_profile(custom_profile_path: Path) -> None:
    custom_profile_content = """<?xml version="1.0" encoding="UTF-8"?>
<profile>
    <name lang="mogz">:5086</name>
    <Profile name="CustomProfile" description="Custom profile to include MVC tracks">
        <trackSettings input="default">
            <output outputSettingsName="copy"
                defaultSelection="+sel:all,+sel:mvcvideo">
            </output>
        </trackSettings>
    </Profile>
</profile>"""
    custom_profile_path.write_text(custom_profile_content)
    print(f"\nCustom MakeMKV profile created at {custom_profile_path}")


def rip_disc_to_mkv(source: str, output_folder: Path) -> Path:
    custom_profile_path = output_folder / "custom_profile.mmcp.xml"
    create_custom_makemkv_profile(custom_profile_path)
    command = [
        MAKEMKVCON_PATH,
        f"--profile={custom_profile_path}",
        "mkv",
        source,
        "all",
        output_folder,
    ]
    run_command(command, "Rip disc to MKV file.")

    mkv_files = list(output_folder.glob("*.mkv"))
    if mkv_files:
        return mkv_files[0]
    else:
        raise FileNotFoundError(f"No MKV files found in {output_folder}.")


def get_disc_and_mvc_video_info(source: Path) -> DiscInfo:
    command = [MAKEMKVCON_PATH, "--robot", "info", source]
    output = run_command(command, "Get disc and MVC video properties")

    disc_info = DiscInfo()

    disc_name_match = re.search(r"CINFO:2,0,\"(.*?)\"", output)
    if disc_name_match:
        disc_info.name = disc_name_match.group(1)

    mvc_video_matches = re.finditer(r"SINFO:\d+,1,19,0,\"(\d+x\d+)\".*?SINFO:\d+,1,21,0,\"(.*?)\"", output, re.DOTALL)
    for match in mvc_video_matches:
        disc_info.resolution = match.group(1)
        disc_info.frame_rate = match.group(2)
        if "/" in disc_info.frame_rate:
            disc_info.frame_rate = disc_info.frame_rate.split(" ")[0]
        break

    return disc_info


def extract_mvc_bitstream_and_audio(input_file: Path, output_folder: Path, disc_name: str) -> (Path, Path):
    video_output_path = output_folder / f"{disc_name}_mvc.h264"
    audio_output_path = output_folder / f"{disc_name}_audio_PCM.mov"
    command = [
        FFMPEG_PATH,
        "-y",
        "-i",
        input_file,
        "-map",
        "0:v",
        "-c:v",
        "copy",
        "-bsf:v",
        "h264_mp4toannexb",
        video_output_path,
        "-map",
        "0:a:0",
        "-c:a",
        "pcm_s24le",
        audio_output_path,
    ]
    run_command(command, "Extract MVC bitstream and audio to temporary files.")
    return video_output_path, audio_output_path


@contextmanager
def temporary_fifo(*names) -> list[Path]:
    if not names:
        raise ValueError("At least one FIFO name must be provided.")
    fifos = [Path(f"/tmp/{name}") for name in names]
    try:
        for fifo in fifos:
            os.mkfifo(fifo)
        yield fifos
    finally:
        for fifo in fifos:
            fifo.unlink()


def run_command_async(command: list[str], log_file_path: Path, command_name: str | None = None) -> subprocess.Popen:
    command = ensure_str_command(command)
    if not command_name:
        command_name = command[0]
    with open(log_file_path, "w") as log_file:
        print(f"\nRunning {command_name}")
        return subprocess.Popen(command, stdout=log_file, stderr=subprocess.STDOUT, text=True)


def cleanup_process(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()


def handle_process_output(process: subprocess.Popen):
    def stream_watcher(identifier: str, stream: subprocess.Popen.stdout or subprocess.Popen.stderr) -> None:
        for line in stream:
            print(f"{identifier}: {line}", end="")
        stream.close()

    stdout_thread = threading.Thread(target=stream_watcher, args=("STDOUT", process.stdout))
    stderr_thread = threading.Thread(target=stream_watcher, args=("STDERR", process.stderr))

    stdout_thread.start()
    stderr_thread.start()

    process.wait()
    stdout_thread.join()
    stderr_thread.join()


def generate_encoding_command(input_fifo: Path, output_path: Path, disc_info: DiscInfo, bitrate: int) -> list[str]:
    pix_fmt = "yuv420p10le" if disc_info.color_depth == 10 else "yuv420p"
    bitrate_str = f"{bitrate}M"
    buffer_size_str = f"{bitrate * 2}M"

    command = [
        "ffmpeg",
        "-y",
        "-f",
        "rawvideo",
        "-pix_fmt",
        pix_fmt,
        "-s",
        disc_info.resolution,
        "-r",
        disc_info.frame_rate,
        "-i",
        input_fifo,
        "-c:v",
        "hevc_videotoolbox",
        "-b:v",
        bitrate_str,
        "-bufsize",
        buffer_size_str,
        "-tag:v",
        "hvc1",
        "-profile:v",
        "main10" if disc_info.color_depth == 10 else "main",
        output_path,
    ]

    return command


def split_mvc_to_stereo(
    output_folder: Path, video_input_path: Path, disc_info: DiscInfo, terminal_args: argparse.Namespace
) -> (Path, Path):

    left_output_path = output_folder / f"{disc_info.name}_left_movie.mov"
    right_output_path = output_folder / f"{disc_info.name}_right_movie.mov"

    ffmpeg_left_log = output_folder / f"{disc_info.name}_ffmpeg_left.log"
    ffmpeg_right_log = output_folder / f"{disc_info.name}_ffmpeg_right.log"
    frim_log = output_folder / f"{disc_info.name}_frim.log"

    with temporary_fifo("left_fifo", "right_fifo") as (left_fifo, right_fifo):
        ffmpeg_left_command = generate_encoding_command(left_fifo, left_output_path, disc_info, terminal_args.left_right_bitrate)
        ffmpeg_right_command = generate_encoding_command(right_fifo, right_output_path, disc_info, terminal_args.left_right_bitrate)

        ffmpeg_left_process = run_command_async(ffmpeg_left_command, ffmpeg_left_log, "ffmpeg for left eye.")
        ffmpeg_right_process = run_command_async(ffmpeg_right_command, ffmpeg_right_log, "ffmpeg for right eye")

        frim_command = [
            WINE_PATH,
            FRIMDECODE_PATH,
            "-i:mvc",
            video_input_path,
            "-o",
            left_fifo,
            right_fifo,
        ]
        frim_process = run_command_async(frim_command, frim_log, "FRIM to split MVC to stereo.")

        atexit.register(cleanup_process, frim_process)
        atexit.register(cleanup_process, ffmpeg_left_process)
        atexit.register(cleanup_process, ffmpeg_right_process)

        frim_process.wait()
        ffmpeg_left_process.wait()
        ffmpeg_right_process.wait()

    if not terminal_args.keep_files:
        video_input_path.unlink()
        ffmpeg_right_log.unlink()
        ffmpeg_left_log.unlink()
        frim_log.unlink()

    return left_output_path, right_output_path


def combine_to_mv_hevc(output_folder: Path, quality: str, fov: str, left_movie: Path, right_movie: Path) -> Path:

    output_file = output_folder / f"{output_folder.stem}_MV-HEVC.mov"
    output_file.unlink(missing_ok=True)
    command = [
        SPATIAL_MEDIA,
        "merge",
        "-l",
        left_movie,
        "-r",
        right_movie,
        "-q",
        quality,
        "--left-is-primary",
        "--horizontal-field-of-view",
        fov,
        "-o",
        output_file,
    ]
    run_command(command, "Combine stereo HEVC streams to MV-HEVC.")
    return output_file


def transcode_audio(input_file: Path, output_folder: Path, bitrate: int) -> Path:
    output_file = output_folder / f"{output_folder.stem}_audio_AAC.mov"
    command = [
        FFMPEG_PATH,
        "-y",
        "-i",
        input_file,
        "-c:a",
        "aac",
        "-b:a",
        f"{bitrate}k",
        output_file,
    ]
    run_command(command, "Transcode audio to AAC format.")
    return output_file


def remux_audio(mv_hevc_file: Path, audio_file: Path, final_output: Path):
    command = [
        "mp4box",
        "-add",
        mv_hevc_file,
        "-add",
        audio_file,
        final_output,
    ]
    run_command(command, "Remux audio and video to final output.")


def get_video_color_depth(input_file: Path) -> int:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=pix_fmt",
        "-of",
        "json",
        input_file,
    ]
    result = run_command(command, "Get video color depth.")

    json_start = result.find("{")
    if json_start == -1:
        print("No valid JSON output from ffprobe.")
        return 8

    json_output = result[json_start:]

    try:
        ffprobe_output = json.loads(json_output)
        pix_fmt = ffprobe_output["streams"][0]["pix_fmt"]
        if "10le" in pix_fmt or "10be" in pix_fmt:
            return 10
        else:
            return 8
    except json.JSONDecodeError:
        print("Error decoding ffprobe JSON output.")
        return 8


def find_main_feature(folder: Path, extensions: list[str]) -> Path:
    files = []
    for ext in extensions:
        files.extend(folder.glob(f"**/*{ext}"))

    if not files:
        return Path()

    return max(files, key=lambda x: x.stat().st_size)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Process 3D video content.")
    parser.add_argument("--source", required=True, help="Source disc number, MKV file path, or ISO image path.")
    parser.add_argument(
        "--output_root_folder", type=Path, default=Path.cwd(), help="Output folder path. Defaults to current directory."
    )
    parser.add_argument("--transcode_audio", action="store_true", help="Transcode audio to AAC format.")
    parser.add_argument(
        "--audio_bitrate", default=384, type=int, help="Audio bitrate for transcoding in kilobits.  Default of 384kb/s."
    )
    parser.add_argument(
        "--left_right_bitrate", default=20, type=int, help="Bitrate for MV-HEVC encoding in megabits.  Default of 20Mb/s."
    )
    parser.add_argument("--mv_hevc_quality", default=90, type=int, help="Quality factor for MV-HEVC encoding.")
    parser.add_argument("--fov", default=90, type=int, help="Horizontal field of view for MV-HEVC.")
    parser.add_argument("--frame_rate", help="Video frame rate. Detected automatically if not provided.")
    parser.add_argument("--resolution", help="Video resolution. Detected automatically if not provided.")
    parser.add_argument("--keep_files", default=False, action="store_true", help="Keep temporary files after processing.")
    parser.add_argument(
        "--start_stage",
        type=Stage,
        action=StageEnumAction,
        default=Stage.CREATE_MKV,
        help="Stage at which to start the process. Options: " + ", ".join([stage.name for stage in Stage]),
    )
    return parser.parse_args()


def main() -> None:
    terminal_args = parse_arguments()

    setup_frim()
    disc_info, input_path, output_folder = setup_conversion_parameters(terminal_args)

    mkv_output_path = create_mkv_file(terminal_args, disc_info.name, input_path, output_folder)

    audio_output_path, video_output_path = create_mvc_and_audio_files(disc_info.name, mkv_output_path, output_folder, terminal_args)
    left_eye_output_path, right_eye_output_path = create_left_right_files(disc_info, output_folder, video_output_path, terminal_args)
    mv_hevc_file = create_mv_hevc_file(left_eye_output_path, output_folder, right_eye_output_path, terminal_args, disc_info.name)
    audio_output_path = create_transcoded_audio_file(terminal_args, audio_output_path, output_folder)
    create_final_file(audio_output_path, disc_info.name, mv_hevc_file, output_folder, terminal_args)


def create_mv_hevc_file(left_eye_output_path, output_folder, right_eye_output_path, terminal_args, disc_name: str) -> Path:
    if terminal_args.start_stage.value <= Stage.COMBINE_TO_MV_HEVC.value:
        mv_hevc_file = combine_to_mv_hevc(
            output_folder, terminal_args.mv_hevc_quality, terminal_args.fov, left_eye_output_path, right_eye_output_path
        )
    else:
        mv_hevc_file = output_folder / f"{disc_name}_MV-HEVC.mov"
    if not terminal_args.keep_files:
        left_eye_output_path.unlink()
        right_eye_output_path.unlink()
    return mv_hevc_file


def create_final_file(
    audio_file: Path, disc_name: str, mv_hevc_file: Path, output_folder: Path, terminal_args: argparse.Namespace
) -> Path:
    final_output = output_folder / f"{disc_name}_AVP.mov"
    remux_audio(mv_hevc_file, audio_file, final_output)
    if not terminal_args.keep_files:
        mv_hevc_file.unlink()
        audio_file.unlink()
    return final_output


def create_transcoded_audio_file(terminal_args: argparse.Namespace, original_audio_path: Path, output_folder: Path) -> Path:
    if terminal_args.transcode_audio and terminal_args.start_stage.value <= Stage.TRANSCODE_AUDIO.value:
        trancoded_audio_path = transcode_audio(original_audio_path, output_folder, terminal_args.audio_bitrate)
        if not terminal_args.keep_files:
            original_audio_path.unlink()
        return trancoded_audio_path
    else:
        return original_audio_path


def create_left_right_files(
    disc_info: DiscInfo, output_folder: Path, video_output_path: Path, terminal_args: argparse.Namespace
) -> (Path, Path):
    disc_info.color_depth = get_video_color_depth(video_output_path)
    if terminal_args.start_stage.value <= Stage.CREATE_LEFT_RIGHT_FILES.value:
        left_eye_output_path, right_eye_output_path = split_mvc_to_stereo(output_folder, video_output_path, disc_info, terminal_args)
    else:
        left_eye_output_path = output_folder / f"{disc_info.name}_left_movie.mov"
        right_eye_output_path = output_folder / f"{disc_info.name}_right_movie.mov"

    return left_eye_output_path, right_eye_output_path


def create_mvc_and_audio_files(
    disc_name: str, mkv_output_path: Path, output_folder: Path, terminal_args: argparse.Namespace
) -> (Path, Path):

    if terminal_args.start_stage.value <= Stage.EXTRACT_MVC_AUDIO.value:
        video_output_path, audio_output_path = extract_mvc_bitstream_and_audio(mkv_output_path, output_folder, disc_name)
    else:
        video_output_path = output_folder / f"{disc_name}_mvc.h264"
        audio_output_path = output_folder / f"{disc_name}_audio_PCM.mov"

    if not terminal_args.keep_files:
        mkv_output_path.unlink()
    return audio_output_path, video_output_path


def create_mkv_file(terminal_args: argparse.Namespace, disc_name: str, input_path: Path, output_folder: Path) -> Path:
    if "disc:" in terminal_args.source.lower() or input_path.suffix.lower() in IMAGE_EXTENSIONS:
        if terminal_args.start_stage.value <= Stage.CREATE_MKV.value:
            source = f"iso:{input_path}" if input_path.suffix.lower() in IMAGE_EXTENSIONS else terminal_args.source
            mkv_output_path = rip_disc_to_mkv(source, output_folder)
        else:
            mkv_output_path = output_folder / f"{disc_name}_t00.mkv"
    elif input_path.suffix.lower() == ".mkv":
        mkv_output_path = input_path
    elif input_path.is_dir():
        mkv_output_path = find_main_feature(input_path, [".mkv"])
    else:
        raise ValueError("Invalid input source.")
    if not terminal_args.keep_files:
        (Path(output_folder) / "custom_profile.mmcp.xml").unlink()
    return mkv_output_path


def setup_conversion_parameters(terminal_args: argparse.Namespace) -> tuple[DiscInfo, Path, Path]:

    input_path = Path(terminal_args.source)
    disc_info = get_disc_and_mvc_video_info(input_path)
    output_folder = prepare_output_folder_for_source(disc_info.name, terminal_args)
    if terminal_args.frame_rate:
        disc_info.frame_rate = terminal_args.frame_rate
    if terminal_args.resolution:
        disc_info.resolution = terminal_args.resolution
    return disc_info, input_path, output_folder


if __name__ == "__main__":
    main()
