import argparse
import atexit
import itertools
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
from typing import Any, Generator

import ffmpeg  # type: ignore


@dataclass
class DiscInfo:
    name: str = "Unknown"
    frame_rate: str = "23.976"
    resolution: str = "1920x1080"
    color_depth: int = 8
    main_title_number: int = 0


class Stage(Enum):
    CREATE_MKV = auto()
    EXTRACT_MVC_AUDIO_AND_SUB = auto()
    CREATE_LEFT_RIGHT_FILES = auto()
    COMBINE_TO_MV_HEVC = auto()
    TRANSCODE_AUDIO = auto()
    CREATE_FINAL_FILE = auto()
    MOVE_FILES = auto()


@dataclass
class InputArgs:
    source_str: str
    source_path: Path | None
    output_root_path: Path
    overwrite: bool
    transcode_audio: bool
    audio_bitrate: int
    left_right_bitrate: int
    mv_hevc_quality: int
    fov: int
    frame_rate: str
    resolution: str
    keep_files: bool
    start_stage: Stage
    remove_original: bool
    source_folder: Path | None
    swap_eyes: bool
    skip_subtitles: bool
    crop_black_bars: bool


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
# HOMEBREW_PREFIX =  Path(os.getenv("HOMEBREW_PREFIX = HOMEBREW_PREFIX", "/opt/homebrew"))
HOMEBREW_PREFIX = Path("/opt/homebrew")
WINE_PATH = HOMEBREW_PREFIX / "bin/wine"
FRIM_PATH = SCRIPT_PATH / "bin" / "FRIM_x64_version_1.31" / "x64"
FRIMDECODE_PATH = FRIM_PATH / "FRIMDecode64.exe"
MP4BOX_PATH = HOMEBREW_PREFIX / "bin" / "MP4Box"
SPATIAL_MEDIA = SCRIPT_PATH / "bin" / "spatial-media-kit-tool"
MKVEXTRACT_PATH = HOMEBREW_PREFIX / "bin" / "mkvextract"

FINAL_FILE_TAG = "_AVP"
IMAGE_EXTENSIONS = [".iso", ".img", ".bin"]

stop_spinner_flag = False


@contextmanager
def mounted_image(image_path: Path):
    mount_point = None
    existing_mounts_command = ["hdiutil", "info"]
    existing_mounts_output = run_command(
        existing_mounts_command, "Check mounted images"
    )
    try:
        for line in existing_mounts_output.split("\n"):
            if str(image_path) in line:
                mount_line_index = existing_mounts_output.split("\n").index(line) + 1
                while (
                    "/dev/disk"
                    not in existing_mounts_output.split("\n")[mount_line_index]
                ):
                    mount_line_index += 1
                mount_point = existing_mounts_output.split("\n")[
                    mount_line_index
                ].split("\t")[-1]
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
    run_command(
        regedit_command, "Update the Windows registry for FRIM plugins.", regedit_env
    )
    print("Updated the Windows registry for FRIM plugins.")


def remove_folder_if_exists(folder_path: Path) -> None:
    if folder_path.is_dir():
        shutil.rmtree(folder_path, ignore_errors=True)
        print(f"Removed existing directory: {folder_path}")


def spinner(command_name: str = "command...") -> None:
    spinner_symbols = itertools.cycle(["-", "/", "|", "\\"])
    print(f"Running {command_name} ", end="", flush=True)
    while not stop_spinner_flag:
        sys.stdout.write(next(spinner_symbols))
        sys.stdout.flush()
        sleep(0.1)
        sys.stdout.write("\b")
    print("\n")


def normalize_command_elements(command: list[Any]) -> list[str | os.PathLike | bytes]:
    return [
        str(item) if not isinstance(item, (str, bytes, os.PathLike)) else item
        for item in command
    ]


def run_command(
    command_list: list[Any], command_name: str = "", env: dict[str, str] | None = None
) -> str:
    command_list = normalize_command_elements(command_list)
    if not command_name:
        command_name = str(command_list[0])

    env = env if env else os.environ.copy()
    global stop_spinner_flag
    output_lines = []
    stop_spinner_flag = False
    spinner_thread = threading.Thread(target=spinner, args=(command_name,))
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
        stop_spinner_flag = True
        spinner_thread.join()

    return "".join(output_lines)


def prepare_output_folder_for_source(disc_name: str, input_args: InputArgs) -> Path:
    output_path = input_args.output_root_path / disc_name
    if input_args.start_stage == list(Stage)[0]:
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
    print(f"Custom MakeMKV profile created at {custom_profile_path}")


def rip_disc_to_mkv(
    input_args: InputArgs, output_folder: Path, disc_info: DiscInfo
) -> None:
    custom_profile_path = output_folder / "custom_profile.mmcp.xml"
    create_custom_makemkv_profile(custom_profile_path)

    if (
        input_args.source_path
        and input_args.source_path.suffix.lower() in IMAGE_EXTENSIONS
    ):
        source = f"iso:{input_args.source_path}"
    elif input_args.source_path:
        source = input_args.source_path.as_posix()
    else:
        source = input_args.source_str
    command = [
        MAKEMKVCON_PATH,
        f"--profile={custom_profile_path}",
        "mkv",
        source,
        disc_info.main_title_number,
        output_folder,
    ]
    run_command(command, "Rip disc to MKV file.")


def sanitize_filename(name: str) -> str:
    allowed_chars = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 _-"
    return "".join(c if c in allowed_chars else "" for c in name)


def get_disc_and_mvc_video_info(source: str) -> DiscInfo:
    command = [MAKEMKVCON_PATH, "--robot", "info", source]
    output = run_command(command, "Get disc and MVC video properties")

    disc_info = DiscInfo()

    disc_name_match = re.search(r"CINFO:2,0,\"(.*?)\"", output)
    if disc_name_match:
        disc_info.name = sanitize_filename(disc_name_match.group(1))

    mvc_video_matches = list(
        re.finditer(
            r"SINFO:\d+,1,19,0,\"(\d+x\d+)\".*?SINFO:\d+,1,21,0,\"(.*?)\"",
            output,
            re.DOTALL,
        )
    )

    if not mvc_video_matches:
        print("No MVC video found in disc info.")
        raise ValueError("No MVC video found in disc info.")

    first_match = mvc_video_matches[0]
    disc_info.resolution = first_match.group(1)
    disc_info.frame_rate = first_match.group(2)
    if "/" in disc_info.frame_rate:
        disc_info.frame_rate = disc_info.frame_rate.split(" ")[0]

    title_info_pattern = re.compile(
        r'TINFO:(?P<index>\d+),\d+,\d+,"(?P<duration>\d+:\d+:\d+)"'
    )
    longest_duration = 0
    main_feature_index = 0

    for match in title_info_pattern.finditer(output):
        title_index = int(match.group("index"))
        h, m, s = map(int, match.group("duration").split(":"))
        duration_seconds = h * 3600 + m * 60 + s

        if duration_seconds > longest_duration:
            longest_duration = duration_seconds
            main_feature_index = title_index

    disc_info.main_title_number = main_feature_index

    return disc_info


def get_subtitle_tracks(input_path: Path) -> list[dict]:
    subtitle_format_extensions = {
        "hdmv_pgs_subtitle": ".sup",
        "dvd_subtitle": ".sub",
        "subrip": ".srt",
    }
    subtitle_tracks = []
    try:
        print(f"Getting subtitle tracks from {input_path}")
        probe = ffmpeg.probe(str(input_path), select_streams="s")
        subtitle_streams = probe.get("streams", [])
        for stream in subtitle_streams:
            codec_name = stream.get("codec_name", "")
            index = stream.get("index")
            extension = subtitle_format_extensions.get(codec_name, "")
            if extension:
                subtitle_tracks.append(
                    {"index": index, "extension": extension, "codec_name": codec_name}
                )
    except ffmpeg.Error as e:
        print(f"Error getting subtitle tracks: {e}")
    return subtitle_tracks


def extract_mvc_audio_and_subtitle(
    input_path: Path,
    video_output_path: Path,
    audio_output_path: Path,
    subtitle_output_path: Path | None,
) -> None:
    stream = ffmpeg.input(str(input_path))

    video_stream = ffmpeg.output(
        stream["v:0"], f"file:{video_output_path}", c="copy", bsf="h264_mp4toannexb"
    )
    audio_stream = ffmpeg.output(
        stream["a:0"], f"file:{audio_output_path}", c="pcm_s24le"
    )

    print("Running ffmpeg to extract video, audio, and subtitles from MKV")
    if subtitle_output_path:
        subtitle_stream = ffmpeg.output(
            stream["s:0"], f"file:{subtitle_output_path}", c="copy"
        )
        run_ffmpeg_print_errors(
            [video_stream, audio_stream, subtitle_stream], overwrite_output=True
        )
    else:
        run_ffmpeg_print_errors([video_stream, audio_stream], overwrite_output=True)


@contextmanager
def temporary_fifo(*names: str) -> Generator[list[Path], None, None]:
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


def cleanup_process(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()


def run_ffmpeg_async(command: list[Any], log_path: Path) -> subprocess.Popen:
    command = normalize_command_elements(command)
    with open(log_path, "w") as log_file:
        process = subprocess.Popen(
            command, stdout=log_file, stderr=subprocess.STDOUT, text=True
        )
    return process


def generate_ffmpeg_wrapper_command(
    input_fifo: Path,
    output_path: Path,
    disc_info: DiscInfo,
    bitrate: int,
    crop_params: str,
) -> list[Any]:
    pix_fmt = "yuv420p10le" if disc_info.color_depth == 10 else "yuv420p"
    stream = ffmpeg.input(
        str(input_fifo),
        f="rawvideo",
        pix_fmt=pix_fmt,
        s=disc_info.resolution,
        r=disc_info.frame_rate,
    )
    if crop_params:
        stream = ffmpeg.filter(stream, "crop", *crop_params.split(":"))
    stream = ffmpeg.output(
        stream,
        f"file:{output_path}",
        vcodec="hevc_videotoolbox",
        video_bitrate=f"{bitrate}M",
        bufsize=f"{bitrate * 2}M",
        tag="hvc1",
        vprofile="main10" if disc_info.color_depth == 10 else "main",
    )

    args = ffmpeg.compile(stream, overwrite_output=True)
    return args


def split_mvc_to_stereo(
    video_input_path: Path,
    input_args: InputArgs,
    left_output_path: Path,
    right_output_path: Path,
    disc_info: DiscInfo,
    crop_params: str,
):
    ffmpeg_left_log = left_output_path.with_suffix(".log")
    ffmpeg_right_log = right_output_path.with_suffix(".log")
    with temporary_fifo("left_fifo", "right_fifo") as (primary_fifo, secondary_fifo):
        ffmpeg_left_command = generate_ffmpeg_wrapper_command(
            primary_fifo,
            left_output_path,
            disc_info,
            input_args.left_right_bitrate,
            crop_params,
        )
        ffmpeg_right_command = generate_ffmpeg_wrapper_command(
            secondary_fifo,
            right_output_path,
            disc_info,
            input_args.left_right_bitrate,
            crop_params,
        )

        left_process = run_ffmpeg_async(ffmpeg_left_command, ffmpeg_left_log)
        right_process = run_ffmpeg_async(ffmpeg_right_command, ffmpeg_right_log)

        frim_command = [
            WINE_PATH,
            FRIMDECODE_PATH,
            "-i:mvc",
            video_input_path,
            "-o",
        ]
        if input_args.swap_eyes:
            frim_command += [secondary_fifo, primary_fifo]
        else:
            frim_command += [primary_fifo, secondary_fifo]

        atexit.register(cleanup_process, left_process)
        atexit.register(cleanup_process, right_process)

        run_command(frim_command, "FRIM to split MVC to stereo.")
        left_process.wait()
        right_process.wait()

    if not input_args.keep_files:
        video_input_path.unlink(missing_ok=True)
        left_output_path.with_suffix(".log").unlink(missing_ok=True)
        right_output_path.with_suffix(".log").unlink(missing_ok=True)

    return left_output_path, right_output_path


def combine_to_mv_hevc(
    left_video_path: Path,
    right_video_path: Path,
    output_path: Path,
    input_args: InputArgs,
) -> None:
    output_path.unlink(missing_ok=True)
    command = [
        SPATIAL_MEDIA,
        "merge",
        "-l",
        left_video_path,
        "-r",
        right_video_path,
        "-q",
        input_args.mv_hevc_quality,
        "--left-is-primary",
        "--horizontal-field-of-view",
        input_args.fov,
        "-o",
        output_path,
    ]
    run_command(command, "Combine stereo HEVC streams to MV-HEVC.")


def transcode_audio(input_path: Path, transcoded_audio_path: Path, bitrate: int):
    audio_input = ffmpeg.input(str(input_path))
    audio_transcoded = ffmpeg.output(
        audio_input,
        str(f"file:{transcoded_audio_path}"),
        acodec="aac",
        audio_bitrate=f"{bitrate}k",
    )
    run_ffmpeg_print_errors(audio_transcoded, overwrite_output=True)


def mux_video_audio_and_subtitles(
    mv_hevc_path: Path, audio_path: Path, subtitle_path: Path | None, muxed_path: Path
) -> None:
    command = [
        MP4BOX_PATH,
        "-add",
        mv_hevc_path,
        "-add",
        audio_path,
    ]
    if subtitle_path and subtitle_path.suffix.lower() != ".sup":
        command += ["-add", subtitle_path]

    command.append(muxed_path)
    run_command(command, "Remux audio and video to final output.")


def get_video_color_depth(input_path: Path) -> int | None:
    try:
        probe = ffmpeg.probe(
            str(input_path), select_streams="v:0", show_entries="stream=pix_fmt"
        )
        streams = probe.get("streams", [])
        if streams:
            pix_fmt = streams[0].get("pix_fmt")
            if "10le" in pix_fmt or "10be" in pix_fmt:
                return 10
            return None
    except ffmpeg.Error:
        print(
            f"Error getting video color depth, using default of {DiscInfo().color_depth}"
        )
    return None


def find_largest_file_with_extensions(
    folder: Path, extensions: list[str]
) -> Path | None:
    files: list[Path] = []
    for ext in extensions:
        files.extend(folder.glob(f"**/*{ext}"))

    if not files:
        print(f"\nNo files found in {folder} with extensions: {extensions}")
        return None

    return max(files, key=lambda x: x.stat().st_size)


def parse_arguments() -> InputArgs:
    parser = argparse.ArgumentParser(description="Process 3D video content.")
    source_group = parser.add_mutually_exclusive_group(required=True)

    source_group.add_argument(
        "--source",
        "-s",
        help="Source for a single disc number, MKV file path, or ISO image path.",
    )
    source_group.add_argument(
        "--source-folder",
        "-f",
        type=Path,
        help="Directory containing multiple image files or MKVs for processing (will search recusively).",
    )
    parser.add_argument(
        "--remove-original",
        "-r",
        default=False,
        action="store_true",
        help="Remove original file after processing.",
    )
    parser.add_argument(
        "--overwrite",
        default=False,
        action="store_true",
        help="Overwrite existing output file.",
    )
    parser.add_argument(
        "--output-root-folder",
        "-o",
        type=Path,
        default=Path.cwd(),
        help="Output folder path. Defaults to current directory.",
    )
    parser.add_argument(
        "--transcode-audio", action="store_true", help="Transcode audio to AAC format."
    )
    parser.add_argument(
        "--audio-bitrate",
        default=384,
        type=int,
        help="Audio bitrate for transcoding in kilobits.  Default of 384kb/s.",
    )
    parser.add_argument(
        "--skip-freaking-subtitles-because-I-dont-care",
        "--skip-subtitles",
        default=False,
        action="store_true",
        help="Skip extracting subtitles from MKV.",
    )
    parser.add_argument(
        "--left-right-bitrate",
        default=20,
        type=int,
        help="Bitrate for MV-HEVC encoding in megabits.  Default of 20Mb/s.",
    )
    parser.add_argument(
        "--mv-hevc-quality",
        default=75,
        type=int,
        help="Quality factor for MV-HEVC encoding.",
    )
    parser.add_argument(
        "--fov", default=90, type=int, help="Horizontal field of view for MV-HEVC."
    )
    parser.add_argument(
        "--frame_rate", help="Video frame rate. Detected automatically if not provided."
    )
    parser.add_argument(
        "--resolution", help="Video resolution. Detected automatically if not provided."
    )
    parser.add_argument(
        "--crop-black-bars",
        default=False,
        action="store_true",
        help="Automatically Crop black bars.",
    )
    parser.add_argument(
        "--swap-eyes",
        default=False,
        action="store_true",
        help="Swap left and right eye video streams.",
    )
    parser.add_argument(
        "--keep-files",
        default=False,
        action="store_true",
        help="Keep temporary files after processing.",
    )
    parser.add_argument(
        "--start-stage",
        type=Stage,
        action=StageEnumAction,
        default=Stage.CREATE_MKV,
        help="Stage at which to start the process. Options: "
        + ", ".join([stage.name for stage in Stage]),
    )

    args = parser.parse_args()
    input_args = InputArgs(
        source_str=args.source,
        source_path=(
            Path(args.source)
            if args.source and not args.source.startswith("disc:")
            else None
        ),
        output_root_path=Path(args.output_root_folder),
        transcode_audio=args.transcode_audio,
        audio_bitrate=args.audio_bitrate,
        left_right_bitrate=args.left_right_bitrate,
        mv_hevc_quality=args.mv_hevc_quality,
        fov=args.fov,
        frame_rate=args.frame_rate or "",
        resolution=args.resolution or "",
        keep_files=args.keep_files,
        start_stage=args.start_stage,
        remove_original=args.remove_original,
        source_folder=args.source_folder,
        overwrite=args.overwrite,
        swap_eyes=args.swap_eyes,
        skip_subtitles=args.skip_freaking_subtitles_because_I_dont_care,
        crop_black_bars=args.crop_black_bars,
    )
    return input_args


def main() -> None:
    input_args = parse_arguments()
    if input_args.source_folder:
        for source in input_args.source_folder.rglob("*"):
            if not source.is_file() or source.suffix.lower() not in IMAGE_EXTENSIONS + [
                ".mkv"
            ]:
                continue
            input_args.source_path = source
            try:
                process_each(input_args)
            except (ValueError, FileExistsError, subprocess.CalledProcessError):
                continue

    else:
        process_each(input_args)


def normalize_name(name: str) -> str:
    return name.lower().replace("_", " ").replace(" ", "_")


def file_exists_normalized(target_path: Path) -> bool:
    target_dir = target_path.parent
    normalized_target_name = normalize_name(target_path.name)
    for item in target_dir.iterdir():
        if normalize_name(item.name) == normalized_target_name:
            return True
    return False


def process_each(input_args: InputArgs) -> None:
    print(f"\nProcessing {input_args.source_path}")
    disc_info, output_folder = setup_conversion_parameters(input_args)
    completed_path = (
        input_args.output_root_path / f"{disc_info.name}{FINAL_FILE_TAG}.mov"
    )
    if not input_args.overwrite and file_exists_normalized(completed_path):
        if output_folder.exists():
            try:
                output_folder.rmdir()
            except OSError:
                print(f"Failed to remove {output_folder}")
        raise FileExistsError(
            f"Output file already exists for {disc_info.name}. Use --overwrite to replace."
        )

    mkv_output_path = create_mkv_file(input_args, output_folder, disc_info)
    crop_params = detect_crop_parameters(mkv_output_path, input_args)
    audio_output_path, video_output_path, subtitle_output_path = (
        create_mvc_audio_and_subtitle_files(
            disc_info.name, mkv_output_path, output_folder, input_args
        )
    )
    left_output_path, right_output_path = create_left_right_files(
        disc_info, output_folder, video_output_path, crop_params, input_args
    )
    mv_hevc_path = create_mv_hevc_file(
        left_output_path, right_output_path, output_folder, input_args, disc_info.name
    )
    audio_output_path = create_transcoded_audio_file(
        input_args, audio_output_path, output_folder
    )
    muxed_output_path = create_muxed_file(
        audio_output_path,
        mv_hevc_path,
        subtitle_output_path,
        output_folder,
        disc_info.name,
        input_args,
    )
    move_file_to_output_root_folder(muxed_output_path, input_args)
    if input_args.remove_original and input_args.source_path:
        if input_args.source_path.is_dir():
            remove_folder_if_exists(input_args.source_path)
        else:
            input_args.source_path.unlink(missing_ok=True)


def detect_crop_parameters(
    video_path: Path,
    input_args: InputArgs,
    start_seconds: int = 600,
    num_frames: int = 300,
) -> str:
    print("Detecting crop parameters...")
    if not input_args.crop_black_bars:
        return ""
    stream = ffmpeg.input(str(video_path), ss=start_seconds)
    stream = ffmpeg.output(
        stream,
        "null",
        format="null",
        vframes=num_frames,
        vf="cropdetect",
    )

    try:
        _, stdout = ffmpeg.run(stream, capture_stdout=True, capture_stderr=True)
        output = stdout.decode("utf-8").split("\n")
    except ffmpeg.Error as e:
        print("FFmpeg Error:")
        print(e.stderr.decode("utf-8"))
        raise

    crop_param_lines = []
    for output_line in output:
        if "crop=" in output_line:
            crop_param_lines.append(output_line.split("crop=")[1].split(" ")[0])

    return max(crop_param_lines, key=len, default="")


def move_file_to_output_root_folder(
    muxed_output_path: Path, input_args: InputArgs
) -> None:
    final_path = input_args.output_root_path / muxed_output_path.name
    muxed_output_path.replace(final_path)
    if not input_args.keep_files:
        remove_folder_if_exists(muxed_output_path.parent)


def create_mv_hevc_file(
    left_video_path, right_video_path, output_folder, input_args, disc_name: str
) -> Path:
    mv_hevc_path = output_folder / f"{disc_name}_MV-HEVC.mov"
    if input_args.start_stage.value <= Stage.COMBINE_TO_MV_HEVC.value:
        combine_to_mv_hevc(left_video_path, right_video_path, mv_hevc_path, input_args)

    if not input_args.keep_files:
        left_video_path.unlink(missing_ok=True)
        right_video_path.unlink(missing_ok=True)
    return mv_hevc_path


def create_muxed_file(
    audio_path: Path,
    mv_hevc_path: Path,
    subtitle_path: Path | None,
    output_folder: Path,
    disc_name: str,
    input_args: InputArgs,
) -> Path:
    muxed_path = output_folder / f"{disc_name}{FINAL_FILE_TAG}.mov"
    if input_args.start_stage.value <= Stage.CREATE_FINAL_FILE.value:
        mux_video_audio_and_subtitles(
            mv_hevc_path, audio_path, subtitle_path, muxed_path
        )

    if not input_args.keep_files:
        mv_hevc_path.unlink(missing_ok=True)
        audio_path.unlink(missing_ok=True)
    return muxed_path


def create_transcoded_audio_file(
    input_args: InputArgs, original_audio_path: Path, output_folder: Path
) -> Path:
    if (
        input_args.transcode_audio
        and input_args.start_stage.value <= Stage.TRANSCODE_AUDIO.value
    ):
        trancoded_audio_path = output_folder / f"{output_folder.stem}_audio_AAC.mov"
        transcode_audio(
            original_audio_path, trancoded_audio_path, input_args.audio_bitrate
        )
        if not input_args.keep_files:
            original_audio_path.unlink(missing_ok=True)
        return trancoded_audio_path
    else:
        return original_audio_path


def create_left_right_files(
    disc_info: DiscInfo,
    output_folder: Path,
    mvc_video: Path,
    crop_params: str,
    input_args: InputArgs,
) -> tuple[Path, Path]:
    left_eye_output_path = output_folder / f"{disc_info.name}_left_movie.mov"
    right_eye_output_path = output_folder / f"{disc_info.name}_right_movie.mov"
    if color_depth := get_video_color_depth(mvc_video):
        disc_info.color_depth = color_depth
    if input_args.start_stage.value <= Stage.CREATE_LEFT_RIGHT_FILES.value:
        split_mvc_to_stereo(
            mvc_video,
            input_args,
            left_eye_output_path,
            right_eye_output_path,
            disc_info,
            crop_params,
        )

    return left_eye_output_path, right_eye_output_path


def create_mvc_audio_and_subtitle_files(
    disc_name: str,
    mkv_output_path: Path | None,
    output_folder: Path,
    input_args: InputArgs,
) -> tuple[Path, Path, Path | None]:
    video_output_path = output_folder / f"{disc_name}_mvc.h264"
    audio_output_path = output_folder / f"{disc_name}_audio_PCM.mov"

    subtitle_output_path = None

    if (
        input_args.start_stage.value <= Stage.EXTRACT_MVC_AUDIO_AND_SUB.value
        and mkv_output_path
    ):
        if not input_args.skip_subtitles and (
            subtitle_formats := get_subtitle_tracks(mkv_output_path)
        ):

            subtitle_output_path = (
                output_folder
                / f"{disc_name}_subtitle{subtitle_formats[0]['extension']}"
            )
        extract_mvc_audio_and_subtitle(
            mkv_output_path, video_output_path, audio_output_path, subtitle_output_path
        )
    else:
        subtitle_extensions = [".idx", ".sup", ".srt"]
        subtitle_files = (
            file
            for file in output_folder.glob(f"{disc_name}_subtitle*")
            if file.suffix.lower() in (ext.lower() for ext in subtitle_extensions)
        )
        subtitle_output_path = next(subtitle_files, None)

    if subtitle_output_path and subtitle_output_path.suffix.lower() == ".sup":
        subtitle_output_path = convert_sup_to_idx(subtitle_output_path)

    if (
        not input_args.keep_files
        and mkv_output_path
        and input_args.source_path != mkv_output_path
    ):
        mkv_output_path.unlink(missing_ok=True)
    return audio_output_path, video_output_path, subtitle_output_path


def convert_sup_to_idx(sup_subtitle_path: Path) -> Path:
    temp_mkv_path = sup_subtitle_path.with_suffix(".mkv")
    stream = ffmpeg.input(str(sup_subtitle_path))
    subtitle_stream = ffmpeg.output(
        stream["s:0"], str(f"file:{temp_mkv_path}"), format="matroska", codec="dvdsub"
    )
    run_ffmpeg_print_errors(subtitle_stream, overwrite_output=True)

    sub_subtitle_path = sup_subtitle_path.with_suffix(".sub")

    mkvextract_command = [
        MKVEXTRACT_PATH,
        temp_mkv_path,
        "tracks",
        f"0:{sub_subtitle_path}",
    ]
    run_command(mkvextract_command, "Extract subtitle track from MKV")
    temp_mkv_path.unlink(missing_ok=True)
    sup_subtitle_path.unlink(missing_ok=True)

    return sub_subtitle_path.with_suffix(".idx")


def create_mkv_file(
    input_args: InputArgs, output_folder: Path, disc_info: DiscInfo
) -> Path:
    if input_args.source_path and input_args.source_path.suffix.lower() == ".mkv":
        return input_args.source_path

    if input_args.start_stage.value <= Stage.CREATE_MKV.value:
        rip_disc_to_mkv(input_args, output_folder, disc_info)

    if mkv_file := find_largest_file_with_extensions(output_folder, [".mkv"]):
        return mkv_file
    raise ValueError("No MKV file created.")


def setup_conversion_parameters(input_args: InputArgs) -> tuple[DiscInfo, Path]:
    disc_info = get_disc_and_mvc_video_info(
        input_args.source_path.as_posix()
        if input_args.source_path
        else input_args.source_str
    )
    output_folder = prepare_output_folder_for_source(disc_info.name, input_args)
    if input_args.frame_rate:
        disc_info.frame_rate = input_args.frame_rate
    if input_args.resolution:
        disc_info.resolution = input_args.resolution
    return disc_info, output_folder


def run_ffmpeg_print_errors(stream_spec: Any, quiet: bool = True, **kwargs) -> None:
    kwargs["quiet"] = quiet
    try:
        ffmpeg.run(stream_spec, **kwargs)
    except ffmpeg.Error as e:
        print("FFmpeg Error:")
        print("STDOUT:", e.stdout.decode("utf-8"))
        print("STDERR:", e.stderr.decode("utf-8"))
        raise


if __name__ == "__main__":
    main()
