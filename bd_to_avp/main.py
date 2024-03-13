import argparse
import atexit
import itertools
import os
import re
import shutil
import subprocess
import sys
import threading
from asyncio import sleep
from contextlib import contextmanager
from pathlib import Path

SCRIPT_PATH = Path(__file__).parent
MAKEMKVCON_PATH = Path("/Applications/MakeMKV.app/Contents/MacOS/makemkvcon")
HOMEBREW_PREFIX = Path(os.getenv("HOMEBREW_PREFIX", "/opt/homebrew"))
WINE_PATH = HOMEBREW_PREFIX / "bin/wine"
FRIM_PATH = SCRIPT_PATH / "FRIM_x64_version_1.31" / "x64"
FRIMDECODE_PATH = FRIM_PATH / "FRIMDecode64.exe"
MP4BOX_PATH = HOMEBREW_PREFIX / "bin" / "MP4Box"
SPATIAL_MEDIA = "spatial-media"
IMAGE_EXTENSIONS = [".iso", ".img", ".bin"]

stop_spinner_flag = False


@contextmanager
def mounted_image(image_path: Path):
    mount_point = None
    existing_mounts_output = run_command(["hdiutil", "info"], "Checking mounted images")
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
            mount_output = run_command(["hdiutil", "attach", str(image_path)], "Mounting image")
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
            run_command(["hdiutil", "detach", mount_point], "Unmounting image")
            print(f"ISO unmounted from {mount_point}")


def setup_frim() -> None:
    wine_prefix = Path(os.environ.get("WINEPREFIX", "~/.wine")).expanduser()
    frim_destination_path = wine_prefix / "drive_c/UTL/FRIM"

    if frim_destination_path.exists():
        print(f"{frim_destination_path} already exists. Skipping install.")
        return
    else:
        shutil.copytree(FRIM_PATH, frim_destination_path)
        print(f"Copied FRIM directory to {frim_destination_path}")

    reg_file_path = FRIM_PATH / "plugins64.reg"
    if not reg_file_path.exists():
        print(f"Registry file {reg_file_path} not found. Skipping registry update.")
        return

    regedit_command = [str(WINE_PATH), "regedit", str(reg_file_path)]
    regedit_env = {"WINEPREFIX": str(wine_prefix)}
    run_command(regedit_command, "Updating the Windows registry for FRIM plugins.", regedit_env)
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


def run_command(command: list[str], command_name: str = None, env: dict[str, str] = None) -> str:
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


def prepare_output_folder_for_source(disc_name: str, output_folder: Path) -> (str, Path):
    output_path = output_folder / disc_name
    # remove_folder_if_exists(output_path) #TODO: uncomment this line
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


def rip_disc_to_mkv(source: str, output_folder: Path) -> Path:
    custom_profile_path = output_folder / "custom_profile.mmcp.xml"
    create_custom_makemkv_profile(custom_profile_path)
    command = [
        str(MAKEMKVCON_PATH),
        "--profile=" + str(custom_profile_path),
        "mkv",
        source,
        "all",
        str(output_folder),
    ]
    run_command(command, "Ripping disc to MKV file.")

    mkv_files = list(output_folder.glob("*.mkv"))
    if mkv_files:
        return mkv_files[0]
    else:
        raise FileNotFoundError(f"No MKV files found in {output_folder}.")


def get_disc_and_mvc_video_info(source: Path) -> dict:
    command = [str(MAKEMKVCON_PATH), "--robot", "info", str(source)]
    output = run_command(command, "Getting disc and MVC video properties")

    info = {
        "name": "Unknown",
        "frame_rate": "23.976",
        "resolution": "1920x1080",
    }

    disc_name_match = re.search(r"CINFO:2,0,\"(.*?)\"", output)
    if disc_name_match:
        info["name"] = disc_name_match.group(1)

    mvc_video_matches = re.finditer(r"SINFO:\d+,1,19,0,\"(\d+x\d+)\".*?SINFO:\d+,1,21,0,\"(.*?)\"", output, re.DOTALL)
    for match in mvc_video_matches:
        info["resolution"] = match.group(1)
        info["frame_rate"] = match.group(2)
        break

    return info


def extract_mvc_bitstream_and_audio(input_file: Path, output_folder: Path, disc_name: str) -> (Path, Path):
    video_output_path = output_folder / f"{disc_name}_mvc.h264.mov"
    audio_output_path = output_folder / f"{disc_name}_audio_5.1_LPCM.mov"
    command = [
        "ffmpeg",
        "-i",
        str(input_file),
        "-map",
        "0:v",
        "-c:v",
        "copy",
        "-bsf:v",
        "h264_mp4toannexb",
        str(video_output_path),
        "-map",
        "0:a:0",
        "-c:a",
        "pcm_s24le",
        str(audio_output_path),
    ]
    run_command(command, "Extracting MVC bitstream and audio to temporary files.")
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


def generate_ffmpeg_command(input_fifo: Path, output_file: Path, resolution: str, frame_rate: str) -> list[str]:
    return [
        "ffmpeg",
        "-y",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "yuv420p",
        "-s",
        resolution,
        "-r",
        frame_rate,
        "-i",
        str(input_fifo),
        "-c:v",
        "hevc_videotoolbox",
        "-b:v",
        "30M",
        str(output_file),
    ]


def split_mvc_to_stereo(
    output_folder: Path, video_input_path: Path, frame_rate: str, resolution: str, disc_name: str
) -> (Path, Path):

    if "/" in frame_rate:
        frame_rate = frame_rate.split(" ")[0]

    left_output_path = output_folder / f"{disc_name}_left_movie.mov"
    right_output_path = output_folder / f"{disc_name}_right_movie.mov"

    ffmpeg_left_log = output_folder / f"{disc_name}_ffmpeg_left.log"
    ffmpeg_right_log = output_folder / f"{disc_name}_ffmpeg_right.log"
    frim_log = output_folder / f"{disc_name}_frim.log"

    with temporary_fifo("left_fifo", "right_fifo") as (left_fifo, right_fifo):
        ffmpeg_left_command = generate_ffmpeg_command(left_fifo, left_output_path, resolution, frame_rate)
        ffmpeg_right_command = generate_ffmpeg_command(right_fifo, right_output_path, resolution, frame_rate)

        ffmpeg_left_process = run_command_async(ffmpeg_left_command, ffmpeg_left_log, "ffmpeg for left eye.")
        ffmpeg_right_process = run_command_async(ffmpeg_right_command, ffmpeg_right_log, "ffmpeg for right eye")

        frim_command = [
            str(WINE_PATH),
            str(FRIMDECODE_PATH),
            "-i:mvc",
            str(video_input_path),
            "-o",
            str(left_fifo),
            str(right_fifo),
        ]
        frim_process = run_command_async(frim_command, frim_log, "Use FRIM to split MVC to stereo.")

        atexit.register(cleanup_process, frim_process)
        atexit.register(cleanup_process, ffmpeg_left_process)
        atexit.register(cleanup_process, ffmpeg_right_process)

        frim_process.wait()
        ffmpeg_left_process.wait()
        ffmpeg_right_process.wait()

    return left_output_path, right_output_path


def combine_to_mv_hevc(output_folder: Path, quality: str, fov: str, left_movie: Path, right_movie: Path) -> Path:
    output_file = output_folder / f"{output_folder.stem}_MV-HEVC.mov"
    command = [
        str(SPATIAL_MEDIA),
        "-s",
        str(left_movie),
        str(right_movie),
        "-o",
        str(output_file),
        "--fov",
        fov,
        "--quality",
        quality,
    ]
    run_command(command, "Combining stereo HEVC streams to MV-HEVC.")
    return output_file


def transcode_audio(input_file: Path, output_folder: Path, bitrate: str) -> Path:
    output_file = output_folder / f"{output_folder.stem}_audio_AAC.mov"
    command = [
        "ffmpeg",
        "-i",
        str(input_file),
        "-c:a",
        "aac",
        "-b:a",
        bitrate,
        str(output_file),
    ]
    run_command(command, "Transcoding audio to AAC format.")
    return output_file


def remux_audio(mv_hevc_file: Path, audio_file: Path, final_output: Path):
    command = [
        "mp4box",
        "-add",
        str(mv_hevc_file),
        "-add",
        str(audio_file),
        str(final_output),
    ]
    run_command(command, "Remuxing audio and video to final output.")


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Process 3D video content.")
    parser.add_argument("--source", required=True, help="Source disc number, MKV file path, or ISO image path.")
    parser.add_argument("--output_folder", type=Path, default=Path.cwd(), help="Output folder path. Defaults to current directory.")
    parser.add_argument(
        "--keep_intermediate", action="store_true", default=False, help="Keep intermediate files. Defaults to false."
    )
    parser.add_argument("--transcode_audio", action="store_true", help="Transcode audio to AAC format.")
    parser.add_argument("--audio_bitrate", default="384k", help="Audio bitrate for transcoding.")
    parser.add_argument("--mv_hevc_quality", default="75", help="Quality factor for MV-HEVC encoding.")
    parser.add_argument("--fov", default="90", help="Horizontal field of view for MV-HEVC.")
    parser.add_argument("--frame_rate", help="Video frame rate. Detected automatically if not provided.")
    parser.add_argument("--resolution", help="Video resolution. Detected automatically if not provided.")
    return parser.parse_args()


def find_main_feature(folder: Path, extensions: list[str]) -> Path:
    files = []
    for ext in extensions:
        files.extend(folder.glob(f"**/*{ext}"))

    if not files:
        return Path()

    return max(files, key=lambda x: x.stat().st_size)


def main() -> None:
    setup_frim()
    args = parse_arguments()
    input_path = Path(args.source)
    output_folder, mkv_output_path = None, None
    disc_info = get_disc_and_mvc_video_info(input_path)
    output_folder = prepare_output_folder_for_source(disc_info["name"], args.output_folder)

    if args.frame_rate:
        disc_info["frame_rate"] = args.frame_rate
    if args.resolution:
        disc_info["resolution"] = args.resolution

    if "disc:" in args.source.lower() or input_path.suffix.lower() in IMAGE_EXTENSIONS:
        source = "iso:" + str(input_path) if input_path.suffix.lower() in IMAGE_EXTENSIONS else args.source

        # mkv_output_path = rip_disc_to_mkv(source, output_folder)  # TODO: uncomment this line and remove the next line
        mkv_output_path = output_folder / f"{disc_info['name']}_t00.mkv"
    elif input_path.suffix.lower() == ".mkv":
        mkv_output_path = input_path
    elif input_path.is_dir():
        mkv_output_path = find_main_feature(input_path, [".mkv"])

    if not mkv_output_path:
        print("Could not find MKV file for processing.")
        return

    # video_output_path, audio_output_path = extract_mvc_bitstream_and_audio(
    #     mkv_output_path, output_folder, disc_info["name"]
    # )  # TODO: uncomment this line and remove the two next lines
    video_output_path = output_folder / f"{disc_info['name']}_mvc.h264.mov"
    audio_output_path = output_folder / f"{disc_info['name']}_audio_5.1_LPCM.mov"
    # left_eye_output_path, right_eye_output_path = split_mvc_to_stereo(
    #     output_folder, video_output_path, disc_info["frame_rate"], disc_info["resolution"], disc_info["name"]
    # ) # TODO: uncomment this line and remove the two next lines

    left_eye_output_path = output_folder / f"{disc_info['name']}_left_movie.mov"
    right_eye_output_path = output_folder / f"{disc_info['name']}_right_movie.mov"

    mv_hevc_file = combine_to_mv_hevc(output_folder, args.mv_hevc_quality, args.fov, left_eye_output_path, right_eye_output_path)

    if args.transcode_audio:
        audio_file = transcode_audio(audio_output_path, output_folder, args.audio_bitrate)
    else:
        audio_file = audio_output_path

    final_output = output_folder / f"{disc_info['name']}_AVP.mov"
    remux_audio(mv_hevc_file, audio_file, final_output)

    if not args.keep_intermediate:
        for file_path in [
            video_output_path,
            audio_output_path,
            left_eye_output_path,
            right_eye_output_path,
            mv_hevc_file,
            audio_file,
        ]:
            if file_path.exists():
                file_path.unlink()


if __name__ == "__main__":
    main()
