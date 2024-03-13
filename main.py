import argparse
import os
import re
import shlex
import subprocess
from pathlib import Path


def run_command(command: str) -> str:
    try:
        result = subprocess.run(shlex.split(command), text=True, capture_output=True, check=True)
        print(result.stdout)
        return result.stdout
    except subprocess.CalledProcessError as e:
        print(f"Error executing command: {e.cmd}")
        print(e.stderr)
        return ""


def get_disc_name(source: str) -> str:
    command = f"/Applications/MakeMKV.app/Contents/MacOS/makemkvcon -r info {source}"
    stdout = run_command(command)
    disc_name = "UnknownDisc"
    for line in stdout.splitlines():
        if line.startswith("CINFO:2,"):
            match = re.search(r'"(.*)"', line)
            if match:
                disc_name = match.group(1).replace("/", "_").replace(" ", "_")
                break
    return disc_name


def rip_disc(source: str, output_folder: Path) -> str:
    disc_name = get_disc_name(source)
    output_path = output_folder / disc_name
    command = (
        f'/Applications/MakeMKV.app/Contents/MacOS/makemkvcon backup --decrypt --noscan -r --progress=-same {source} "{output_path}"'
    )
    run_command(command)
    return disc_name


def get_video_properties(file_path: Path) -> (str, str):
    command = f"ffprobe -v error -select_streams v:0 -show_entries stream=r_frame_rate,width,height -of default=noprint_wrappers=1:nokey=1 {file_path}"
    output = run_command(command)
    if output:
        lines = output.strip().split("\n")
        frame_rate_values = lines[0].split("/")
        frame_rate = float(frame_rate_values[0]) / float(frame_rate_values[1])
        resolution = f"{lines[1]}x{lines[2]}"
        return str(round(frame_rate, 3)), resolution
    return "23.976", "1920x1080"


def extract_mvc_bitstream_and_audio(input_file: Path, output_folder: Path) -> None:
    video_output = output_folder / "video.h264"
    audio_output = output_folder / "audio_5.1_LPCM.mov"
    command = (
        f"ffmpeg -i {input_file} -map 0:v -c:v copy -bsf:v h264_mp4toannexb {video_output} -map 0:a:0 -c:a pcm_s24le {audio_output}"
    )
    run_command(command)


def split_mvc_to_stereo(output_folder: Path, frame_rate: str, resolution: str) -> None:
    wine_command = f"wine FRIMDecode64.exe -i:mvc {output_folder}/video.h264 -o \\.\pipe\lefteye \\.\pipe\righteye"
    ffmpeg_left_command = f"ffmpeg -f rawvideo -s {resolution} -r {frame_rate} -pix_fmt yuv420p -i \\.\pipe\lefteye -c:v libx265 -x265-params keyint=1:min-keyint=1:lossless=1 -vtag hvc1 -movflags +faststart {output_folder}/left_movie.mov"
    ffmpeg_right_command = f"ffmpeg -f rawvideo -s {resolution} -r {frame_rate} -pix_fmt yuv420p -i \\.\pipe\righteye -c:v libx265 -x265-params keyint=1:min-keyint=1:lossless=1 -vtag hvc1 -movflags +faststart {output_folder}/right_movie.mov"
    os.system(f"{wine_command} | {ffmpeg_left_command} & {wine_command} | {ffmpeg_right_command}")


def combine_to_mv_hevc(output_folder: Path, quality: str, fov: str, left_movie: Path, right_movie: Path) -> None:
    output_file = output_folder / f"{output_folder.name}_MV-HEVC.mov"
    command = f"spatial-media-kit-tool merge -l {left_movie} -r {right_movie} -q {quality} --left-is-primary --horizontal-field-of-view {fov} -o {output_file}"
    run_command(command)


def transcode_audio(input_file: Path, output_folder: Path, bitrate: str) -> None:
    output_file = output_folder / f"{output_folder.name}_audio_AAC.mov"
    command = f"ffmpeg -i {input_file} -c:a libfdk_aac -b:a {bitrate} {output_file}"
    run_command(command)


def remux_audio(mv_hevc_file: Path, audio_file: Path, final_output: Path) -> None:
    command = f"mp4box -new -add {mv_hevc_file} -add {audio_file} {final_output}"
    run_command(command)


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


def find_main_feature(folder: Path) -> Path:
    mkv_files = list(folder.glob("*.mkv"))
    if not mkv_files:
        return Path()
    largest_mkv = max(mkv_files, key=lambda x: x.stat().st_size)
    return largest_mkv


def main() -> None:
    args = parse_arguments()

    if not Path(args.source).exists():
        print(f"The specified source does not exist: {args.source}")
        return

    source = args.source
    input_file = Path(args.source)

    iso_extensions = [".iso", ".img", ".bin"]
    if any(args.source.lower().endswith(ext) for ext in iso_extensions):
        input_type = "iso"
        source = f"iso:{args.source}"
        movie_name = get_disc_name(source)
    elif "disc:" in args.source.lower():
        input_type = "disc"
        movie_name = rip_disc(source, args.output_folder)
    elif input_file.is_file() and input_file.suffix == ".mkv":
        input_type = "mkv"
        movie_name = input_file.stem
    elif input_file.is_dir():
        input_type = "folder"
        movie_name = input_file.name
    else:
        print(f"Unsupported input type: {args.source}")
        return

    output_folder = args.output_folder / movie_name
    output_folder.mkdir(parents=True, exist_ok=True)

    if input_type in ["iso", "disc"]:
        input_file = output_folder / f"{movie_name}.mkv"  # Expected output from MakeMKV
    elif input_type == "folder":
        input_file = find_main_feature(output_folder)  # Find the main feature MKV in the folder
        if not input_file:
            print(f"No MKV files found in {output_folder}.")
            return

    # Check and detect video properties if not provided
    if not args.frame_rate or not args.resolution:
        detected_frame_rate, detected_resolution = get_video_properties(input_file)
        args.frame_rate = detected_frame_rate if detected_frame_rate else "23.976"
        args.resolution = detected_resolution if detected_resolution else "1920x1080"

    extract_mvc_bitstream_and_audio(input_file, output_folder)
    split_mvc_to_stereo(output_folder, args.frame_rate, args.resolution)

    left_movie = output_folder / "left_movie.mov"
    right_movie = output_folder / "right_movie.mov"
    combine_to_mv_hevc(output_folder, args.mv_hevc_quality, args.fov, left_movie, right_movie)

    if args.transcode_audio:
        audio_input = output_folder / "audio_5.1_LPCM.mov"
        transcode_audio(audio_input, output_folder, args.audio_bitrate)
        audio_file = output_folder / f"{movie_name}_audio_AAC.mov"
    else:
        audio_file = output_folder / "audio_5.1_LPCM.mov"

    mv_hevc_file = output_folder / f"{movie_name}_MV-HEVC.mov"
    final_output = output_folder / f"{movie_name}_AVP.mov"
    remux_audio(mv_hevc_file, audio_file, final_output)


if __name__ == "__main__":
    main()


if __name__ == "__main__":
    main()
