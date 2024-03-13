import argparse
import itertools
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

SCRIPT_PATH = Path(__file__).parent
MAKEMKVCON_PATH = Path("/Applications/MakeMKV.app/Contents/MacOS/makemkvcon")
HOMEBREW_PREFIX = Path(os.getenv("HOMEBREW_PREFIX", "/opt/homebrew"))
WINE_PATH = HOMEBREW_PREFIX / "bin" / "wine"
SPATIAL_MEDIA = "spatial-media"
TSMUXER_PATH = SCRIPT_PATH / "bin" / "tsMuxeR"

stop_spinner_flag = False


def remove_folder_if_exists(folder_path: Path) -> None:
    if folder_path.is_dir():
        shutil.rmtree(folder_path)
        print(f"Removed existing directory: {folder_path}")


def spinner(command_name: str = "Running command...") -> None:
    spinner_symbols = itertools.cycle(["-", "/", "|", "\\"])
    print(f"Running {command_name} ", end="", flush=True)
    while not stop_spinner_flag:
        sys.stdout.write(next(spinner_symbols))  # write the next character
        sys.stdout.flush()
        time.sleep(0.1)  # adjust the speed as needed
        sys.stdout.write("\b")  # erase the last written char


# Function to start the spinner
def start_spinner() -> threading.Thread:
    global stop_spinner_flag
    stop_spinner_flag = False
    thread = threading.Thread(target=spinner, args=("Running command...",))
    thread.start()
    return thread


# Function to stop the spinner
def stop_spinner(thread) -> None:
    global stop_spinner_flag
    stop_spinner_flag = True
    thread.join()


def run_command(command: list[str], command_name=None):
    if not command_name:
        command_name = command[0]
    global stop_spinner_flag
    output_lines = []
    stop_spinner_flag = False
    spinner_thread = threading.Thread(target=spinner, args=(command_name,))
    spinner_thread.start()

    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        while True:
            line = process.stdout.readline()
            if not line:
                break
            output_lines.append(line.strip())
            # Optional: print specific lines immediately for important feedback
            # if "important info" in line:
            #     print(line, end="")

        process.wait()  # Wait for command to complete
        if process.returncode != 0:
            raise subprocess.CalledProcessError(process.returncode, command)
    finally:
        stop_spinner_flag = True
        spinner_thread.join()
        for line in output_lines:
            print(line)
        print("Command completed.")


def prepare_output_folder_for_source(source: Path, output_folder: Path) -> (str, Path):
    disc_name = "UnknownDisc"
    if source.suffix.lower() in [".mkv", ".iso"]:
        disc_name = source.stem
    elif "disc:" in source.as_posix():
        command = [
            MAKEMKVCON_PATH,
            "-r",
            "info",
            source,
        ]
        stdout = run_command(command, "Getting disc info")
        for line in stdout.splitlines():
            if "CINFO:2," in line:
                disc_name = line.split(",")[2].strip('"').replace("/", "_").replace(" ", "_")
                break

    output_path = output_folder / disc_name
    remove_folder_if_exists(output_path)
    output_path.mkdir(parents=True, exist_ok=True)
    return disc_name, output_path


def rip_disc_to_mkv(source: str, output_folder: Path) -> Path:
    command = [
        MAKEMKVCON_PATH,
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


def get_video_properties(file_path: Path) -> (str, str):
    command = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=r_frame_rate,width,height",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(file_path),
    ]
    output = run_command(command, "Getting video properties")
    lines = output.strip().split("\n")
    frame_rate_values = lines[0].split("/")
    frame_rate = str(float(frame_rate_values[0]) / float(frame_rate_values[1]))
    resolution = f"{lines[1]}x{lines[2]}"
    return frame_rate, resolution


def extract_mvc_bitstream_and_audio(input_file: Path, output_folder: Path) -> (Path, Path):
    video_output_path = output_folder / "video.h264"
    audio_output_path = output_folder / "audio_5.1_LPCM.mov"
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


def split_mvc_to_stereo(output_folder: Path, video_input_path: Path, frame_rate: str, resolution: str) -> (Path, Path):
    left_temp_path = output_folder / "left_temp.raw"
    right_temp_path = output_folder / "right_temp.raw"

    meta_content = """
    MUXOPT --no-pcr-on-video-pid --new-audio-pes --demux --vbr --vbv-len=500
    V_MPEG4/ISO/AVC, "{input_file_path}", track=1
    V_MPEG4/ISO/MVC, "{input_file_path}", track=2
    """.format(
        input_file_path=video_input_path
    )
    video_meta_path = output_folder / "video.meta"
    with open(video_meta_path, "w") as f:
        f.write(meta_content)

    tsmuxer_command = [str(WINE_PATH), str(TSMUXER_PATH), str(video_meta_path), str(output_folder)]
    run_command(tsmuxer_command, "Splitting MVC bitstream to stereo streams.")

    left_output_path = output_folder / "left_movie.mov"
    right_output_path = output_folder / "right_movie.mov"
    ffmpeg_left_command = [
        "ffmpeg",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "yuv420p",
        "-s",
        resolution,
        "-r",
        frame_rate,
        "-i",
        str(left_temp_path),
        "-c:v",
        "libx265",
        str(left_output_path),
    ]
    ffmpeg_right_command = [
        "ffmpeg",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "yuv420p",
        "-s",
        resolution,
        "-r",
        frame_rate,
        "-i",
        str(right_temp_path),
        "-c:v",
        "libx265",
        str(right_output_path),
    ]
    run_command(ffmpeg_left_command, "Encoding left eye video to HEVC.")
    run_command(ffmpeg_right_command, "Encoding right eye video to HEVC")

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
    parser.add_argument("--source", required=True, help="Source disc number, mounted disk path, or ISO image path.")
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


def find_main_feature(folder: Path) -> Path:
    mkv_files = list(folder.glob("*.mkv"))
    if not mkv_files:
        return Path()
    largest_mkv = max(mkv_files, key=lambda x: x.stat().st_size)
    return largest_mkv


def main() -> None:
    args = parse_arguments()
    input_path = Path(args.source)
    movie_name, output_folder, mkv_output_path = None, None, None

    if "disc:" in args.source.lower() or input_path.suffix.lower() in [".iso", ".img", ".FRIM"]:
        source = "iso:" + str(input_path) if input_path.suffix.lower() in [".iso", ".img", ".FRIM"] else args.source
        movie_name, output_folder = prepare_output_folder_for_source(input_path, args.output_folder)
        mkv_output_path = rip_disc_to_mkv(source, output_folder)
    elif input_path.suffix.lower() == ".mkv":
        movie_name = input_path.stem
        output_folder = args.output_folder / movie_name
        remove_folder_if_exists(output_folder)
        output_folder.mkdir(parents=True, exist_ok=True)
        mkv_output_path = input_path
    elif input_path.is_dir():
        movie_name = input_path.name
        output_folder = args.output_folder / movie_name
        remove_folder_if_exists(output_folder)
        output_folder.mkdir(parents=True, exist_ok=True)
        mkv_output_path = find_main_feature(input_path)

    if not movie_name or not mkv_output_path:
        print("Could not determine movie name or find MKV file for processing.")
        return

    video_output_path, audio_output_path = extract_mvc_bitstream_and_audio(mkv_output_path, output_folder)
    left_eye_output_path, right_eye_output_path = split_mvc_to_stereo(
        output_folder, video_output_path, args.frame_rate or "23.976", args.resolution or "1920x1080"
    )

    mv_hevc_file = combine_to_mv_hevc(output_folder, args.mv_hevc_quality, args.fov, left_eye_output_path, right_eye_output_path)

    if args.transcode_audio:
        audio_file = transcode_audio(audio_output_path, output_folder, args.audio_bitrate)
    else:
        audio_file = audio_output_path

    final_output = output_folder / f"{movie_name}_AVP.mov"
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
