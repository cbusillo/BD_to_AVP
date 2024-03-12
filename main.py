import argparse
import subprocess
import shlex
import os


def run_command(command: str, capture_output=True) -> str:
    process = subprocess.Popen(shlex.split(command), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    stdout, stderr = process.communicate()
    if process.returncode != 0:
        print(f"Error executing command: {command}")
        print(stderr)
        return ""
    else:
        if capture_output:
            print(stdout)
        return stdout

def get_video_properties(file_path: str) -> (str, str):
    command = f"ffprobe -v error -select_streams v:0 -show_entries stream=r_frame_rate,width,height -of default=noprint_wrappers=1:nokey=1 {file_path}"
    output = run_command(command)
    if output:
        lines = output.split('\n')
        frame_rate_values = lines[0].split('/')
        frame_rate = float(frame_rate_values[0]) / float(frame_rate_values[1])
        width = lines[1]
        height = lines[2]
        resolution = f"{width}x{height}"
        return str(round(frame_rate, 3)), resolution
    return "23.976", "1920x1080"  # Default values if ffprobe fails

def extract_mvc_bitstream_and_audio(input_file: str, output_folder: str) -> None:
    command = f"ffmpeg -i {input_file} -map 0:v -c:v copy -bsf:v h264_mp4toannexb {output_folder}/video.h264 -map 0:a:0 -c:a pcm_s24le {output_folder}/audio_5.1_LPCM.mov"
    run_command(command)

def split_mvc_to_stereo(output_folder: str, frame_rate: str, resolution: str) -> None:
    wine_command = f"wine FRIMDecode64.exe -i:mvc {output_folder}/video.h264 -o \\.\pipe\lefteye \\.\pipe\righteye"
    ffmpeg_left_command = f"ffmpeg -f rawvideo -s {resolution} -r {frame_rate} -pix_fmt yuv420p -i \\.\pipe\lefteye -c:v libx265 -x265-params keyint=1:min-keyint=1:lossless=1 -vtag hvc1 -movflags +faststart {output_folder}/left_movie.mov"
    ffmpeg_right_command = f"ffmpeg -f rawvideo -s {resolution} -r {frame_rate} -pix_fmt yuv420p -i \\.\pipe\righteye -c:v libx265 -x265-params keyint=1:min-keyint=1:lossless=1 -vtag hvc1 -movflags +faststart {output_folder}/right_movie.mov"
    os.system(f"{wine_command} | {ffmpeg_left_command} & {wine_command} | {ffmpeg_right_command}")

def combine_to_mv_hevc(output_folder: str, quality: str, fov: str, left_movie: str, right_movie: str) -> None:
    command = f"spatial-media-kit-tool merge -l {left_movie} -r {right_movie} -q {quality} --left-is-primary --horizontal-field-of-view {fov} -o {output_folder}/MV-HEVC.mov"
    run_command(command)

def transcode_audio(input_file: str, output_folder: str, bitrate: str) -> None:
    command = f"ffmpeg -i {input_file} -c:a libfdk_aac -b:a {bitrate} {output_folder}/audio_AAC.mov"
    run_command(command)

def remux_audio(output_folder: str, mv_hevc_file: str, audio_file: str) -> None:
    command = f"mp4box -new -add {mv_hevc_file} -add {audio_file} {output_folder}/final_output.mov"
    run_command(command)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Process 3D video content.')
    parser.add_argument('--input_file', required=True, help='Input video file path.')
    parser.add_argument('--output_folder', default='.', help='Output folder path. Defaults to current directory.')
    parser.add_argument('--transcode_audio', action='store_true', help='Transcode audio to AAC format.')
    parser.add_argument('--audio_bitrate', default='384k', help='Audio bitrate for transcoding.')
    parser.add_argument('--mv_hevc_quality', default='75', help='Quality factor for MV-HEVC encoding.')
    parser.add_argument('--fov', default='90', help='Horizontal field of view for MV-HEVC.')
    parser.add_argument('--frame_rate', help='Video frame rate. Detected automatically if not provided.')
    parser.add_argument('--resolution', help='Video resolution. Detected automatically if not provided.')
    args = parser.parse_args()

    if not args.frame_rate or not args.resolution:
        detected_frame_rate, detected_resolution = get_video_properties(args.input_file)
        if not args.frame_rate:
            args.frame_rate = detected_frame_rate
        if not args.resolution:
            args.resolution = detected_resolution

    return args

def main() -> None:
    args = parse_arguments()
    extract_mvc_bitstream_and_audio(args.input_file, args.output_folder)
    split_mvc_to_stereo(args.output_folder, args.frame_rate, args.resolution)
    left_movie = f"{args.output_folder}/left_movie.mov"
    right_movie = f"{args.output_folder}/right_movie.mov"
    combine_to_mv_hevc(args.output_folder, args.mv_hevc_quality, args.fov, left_movie, right_movie)
    if args.transcode_audio:
        audio_input = f"{args.output_folder}/audio_5.1_LPCM.mov"
        transcode_audio(audio_input, args.output_folder, args.audio_bitrate)
        audio_file = f"{args.output_folder}/audio_AAC.mov"
    else:
        audio_file = f"{args.output_folder}/audio_5.1_LPCM.mov"
    mv_hevc_file = f"{args.output_folder}/MV-HEVC.mov"
    remux_audio(args.output_folder, mv_hevc_file, audio_file)

if __name__ == "__main__":
    main()
