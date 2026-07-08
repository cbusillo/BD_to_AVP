import os
import re
import signal
import stat
import subprocess
import tempfile
from pathlib import Path

import ffmpeg

from bd_to_avp.modules.config import Stage, config
from bd_to_avp.modules.disc import DiscInfo
from bd_to_avp.modules.command import cleanup_process, run_command


def has_native_mvc_splitter() -> bool:
    if not config.EDGE264_TEST_PATH.is_file():
        return False
    if os.access(config.EDGE264_TEST_PATH, os.X_OK):
        return True

    try:
        current_mode = config.EDGE264_TEST_PATH.stat().st_mode
        config.EDGE264_TEST_PATH.chmod(current_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        return False
    return os.access(config.EDGE264_TEST_PATH, os.X_OK)


def can_use_native_mvc_splitter(disc_info: DiscInfo) -> bool:
    return disc_info.color_depth == 8 and has_native_mvc_splitter()


def explain_native_mvc_unavailable(disc_info: DiscInfo) -> str:
    if disc_info.color_depth != 8:
        return (
            "Native MVC splitting supports 8-bit Blu-ray 3D MVC sources only. "
            f"This source reports {disc_info.color_depth}-bit video and cannot be processed without the removed "
            "legacy FRIM/Wine path."
        )
    return (
        "The bundled native MVC splitter is missing or is not executable. "
        f"Expected helper at {config.EDGE264_TEST_PATH}. Reinstall BD_to_AVP or repair the app bundle."
    )


class NativeMvcSplitError(RuntimeError):
    pass


def generate_native_mvc_splitter_command(video_input_path: Path, *, single_threaded: bool = False) -> list[str | Path]:
    options = "-Osk" if single_threaded else "-Omk"
    return [
        config.EDGE264_TEST_PATH,
        video_input_path,
        options,
    ]


def prepare_native_mvc_input(video_input_path: Path) -> Path:
    if video_input_path.suffix == ".264":
        return video_input_path

    file_descriptor, temp_name = tempfile.mkstemp(prefix=f"{video_input_path.stem}_", suffix=".264")
    os.close(file_descriptor)
    temp_path = Path(temp_name)
    temp_path.unlink()
    try:
        temp_path.hardlink_to(video_input_path)
    except OSError:
        temp_path.symlink_to(video_input_path)
    return temp_path


def generate_native_mvc_ffmpeg_command(
    left_output_path: Path,
    right_output_path: Path,
    disc_info: DiscInfo,
    crop_params: str,
) -> list[str]:
    if disc_info.color_depth != 8:
        raise ValueError("Native MVC splitting currently supports 8-bit 4:2:0 Blu-ray MVC sources only.")

    source_width, source_height = parse_resolution(config.resolution or disc_info.resolution)

    stream = ffmpeg.input(
        "pipe:0",
        f="yuv4mpegpipe",
        r=config.frame_rate or disc_info.frame_rate,
    )
    split_streams = ffmpeg.filter_multi_output(stream, "split", 2)
    left_stream = split_streams[0]
    right_stream = split_streams[1]
    left_stream = ffmpeg.filter(left_stream, "crop", source_width, source_height, 0, 0)
    right_stream = ffmpeg.filter(right_stream, "crop", source_width, source_height, source_width, 0)

    if crop_params:
        left_stream = ffmpeg.filter(left_stream, "crop", *crop_params.split(":"))
        right_stream = ffmpeg.filter(right_stream, "crop", *crop_params.split(":"))

    if disc_info.is_interlaced:
        left_stream = ffmpeg.filter(left_stream, "bwdif")
        right_stream = ffmpeg.filter(right_stream, "bwdif")

    if config.swap_eyes:
        left_stream, right_stream = right_stream, left_stream

    output_kwargs = {
        "vcodec": "hevc_videotoolbox" if not config.software_encoder else "libx265",
        "video_bitrate": f"{config.left_right_bitrate}M",
        "bufsize": f"{config.left_right_bitrate * 2}M",
        "tag": "hvc1",
        "vprofile": "main",
        "r": config.frame_rate or disc_info.frame_rate,
    }

    left_output = ffmpeg.output(left_stream, f"file:{left_output_path}", **output_kwargs)
    right_output = ffmpeg.output(right_stream, f"file:{right_output_path}", **output_kwargs)
    command = ffmpeg.compile(
        ffmpeg.merge_outputs(left_output, right_output),
        cmd=config.FFMPEG_PATH.as_posix(),
        overwrite_output=True,
    )

    return [arg if arg != "pipe:0" else "-" for arg in command]


def parse_resolution(resolution: str) -> tuple[int, int]:
    width, height = resolution.lower().split("x", 1)
    return int(width), int(height)


def split_mvc_to_stereo_native(
    video_input_path: Path,
    left_output_path: Path,
    right_output_path: Path,
    disc_info: DiscInfo,
    crop_params: str,
) -> tuple[Path, Path]:
    ffmpeg_command = generate_native_mvc_ffmpeg_command(left_output_path, right_output_path, disc_info, crop_params)
    native_input_path = prepare_native_mvc_input(video_input_path)
    splitter_log_path = left_output_path.with_suffix(".native_mvc.log")
    ffmpeg_log_path = left_output_path.with_suffix(".native_ffmpeg.log")
    try:
        try:
            run_native_mvc_split_attempt(
                native_input_path,
                ffmpeg_command,
                ffmpeg_log_path,
                splitter_log_path,
                single_threaded=False,
            )
        except subprocess.CalledProcessError as error:
            if not native_splitter_died_by_signal(error):
                raise

            print("Native MVC splitter crashed; retrying once in single-threaded mode.")
            left_output_path.unlink(missing_ok=True)
            right_output_path.unlink(missing_ok=True)
            run_native_mvc_split_attempt(
                native_input_path,
                ffmpeg_command,
                ffmpeg_log_path,
                splitter_log_path,
                single_threaded=True,
            )
    except subprocess.CalledProcessError as error:
        if error.cmd and Path(error.cmd[0]).name == config.EDGE264_TEST_PATH.name:
            raise NativeMvcSplitError(build_native_splitter_failure_message(error, splitter_log_path)) from error
        raise
    finally:
        if native_input_path != video_input_path:
            native_input_path.unlink(missing_ok=True)

    return left_output_path, right_output_path


def run_native_mvc_split_attempt(
    native_input_path: Path,
    ffmpeg_command: list[str],
    ffmpeg_log_path: Path,
    splitter_log_path: Path,
    *,
    single_threaded: bool,
) -> None:
    splitter_command = generate_native_mvc_splitter_command(native_input_path, single_threaded=single_threaded)

    if config.output_commands:
        splitter_command_text = " ".join(str(command) for command in splitter_command)
        ffmpeg_command_text = " ".join(ffmpeg_command)
        print(f"Running command:\n{splitter_command_text} | {ffmpeg_command_text}")

    splitter_process = None
    ffmpeg_process = None
    try:
        with open(ffmpeg_log_path, "a") as ffmpeg_log, open(splitter_log_path, "ab") as splitter_log:
            attempt_name = "single-threaded" if single_threaded else "multi-threaded"
            ffmpeg_log.write(f"\n--- Native MVC split attempt: {attempt_name} ---\n")
            splitter_log.write(f"\n--- Native MVC split attempt: {attempt_name} ---\n".encode())
            splitter_process = subprocess.Popen(splitter_command, stdout=subprocess.PIPE, stderr=splitter_log)
            ffmpeg_process = subprocess.Popen(
                ffmpeg_command,
                stdin=splitter_process.stdout,
                stdout=ffmpeg_log,
                stderr=subprocess.STDOUT,
                text=False,
            )

            if splitter_process.stdout:
                splitter_process.stdout.close()

            ffmpeg_process.wait()
            splitter_failed_before_cleanup = splitter_process.poll() is not None
            if ffmpeg_process.returncode != 0 and not splitter_failed_before_cleanup:
                cleanup_process(splitter_process)
            splitter_process.wait()

        if splitter_process.returncode != 0 and (ffmpeg_process.returncode == 0 or splitter_failed_before_cleanup):
            raise subprocess.CalledProcessError(
                splitter_process.returncode,
                splitter_command,
                output=splitter_log_path.read_text(errors="replace"),
            )
        if ffmpeg_process.returncode != 0:
            raise subprocess.CalledProcessError(ffmpeg_process.returncode, ffmpeg_command)
    finally:
        if splitter_process:
            cleanup_process(splitter_process)
        if ffmpeg_process:
            cleanup_process(ffmpeg_process)


def native_splitter_died_by_signal(error: subprocess.CalledProcessError) -> bool:
    return error.returncode < 0 and bool(error.cmd) and Path(error.cmd[0]).name == config.EDGE264_TEST_PATH.name


def build_native_splitter_failure_message(error: subprocess.CalledProcessError, splitter_log_path: Path) -> str:
    signal_name = f"signal {-error.returncode}" if error.returncode < 0 else f"exit code {error.returncode}"
    if error.returncode < 0:
        try:
            signal_name = signal.Signals(-error.returncode).name
        except ValueError:
            pass
    return (
        "The native MVC splitter crashed while decoding this MVC video stream "
        f"({signal_name}). This usually means the bundled native decoder hit a Blu-ray MVC bitstream it does not "
        "currently support. The source may need a splitter update or a future fallback path. "
        f"Details were written to {splitter_log_path}."
    )


def split_mvc_to_stereo(
    video_input_path: Path,
    left_output_path: Path,
    right_output_path: Path,
    disc_info: DiscInfo,
    crop_params: str,
):
    if can_use_native_mvc_splitter(disc_info):
        result = split_mvc_to_stereo_native(
            video_input_path,
            left_output_path,
            right_output_path,
            disc_info,
            crop_params,
        )
        if not config.keep_files:
            left_output_path.with_suffix(".native_ffmpeg.log").unlink(missing_ok=True)
            left_output_path.with_suffix(".native_mvc.log").unlink(missing_ok=True)
            video_input_path.unlink(missing_ok=True)
        return result

    raise RuntimeError(explain_native_mvc_unavailable(disc_info))


def combine_to_mv_hevc(
    left_video_path: Path,
    right_video_path: Path,
    output_path: Path,
    color_depth: int,
) -> None:
    output_path.unlink(missing_ok=True)
    command = [
        config.SPATIAL_MEDIA_PATH,
        "merge",
        "--left-file",
        left_video_path,
        "--right-file",
        right_video_path,
        "--quality",
        config.mv_hevc_quality,
        "--left-is-primary",
        "--horizontal-field-of-view",
        config.fov,
        "--color-depth",
        color_depth,
        "--output-file",
        output_path,
    ]
    output = run_command(command, "combine stereo HEVC streams to MV-HEVC.")
    if "left and right input resolutions do not match. aborting!" in output:
        raise RuntimeError("Left and right input resolutions do not match.")
    elif "aborting!" in output:
        raise RuntimeError("Failed to combine stereo HEVC streams to MV-HEVC.")


def parse_crop_params(crop_string: str) -> tuple[int, int, int, int] | None:
    match = re.match(r"(\d+):(\d+):(\d+):(\d+)", crop_string)
    if match:
        return tuple(map(int, match.groups()))  # type: ignore
    return None


def detect_crop_parameters(
    video_path: Path,
    start_seconds: int = 600,
    num_frames: int = 300,
) -> str:
    print("Detecting crop parameters...")
    if not config.crop_black_bars:
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
        _, stdout = ffmpeg.run(
            stream,
            cmd=config.FFMPEG_PATH.as_posix(),
            capture_stdout=True,
            capture_stderr=True,
        )
        output = stdout.decode("utf-8").split("\n")
    except ffmpeg.Error as e:
        print("FFmpeg Error:")
        print(e.stderr.decode("utf-8"))
        raise

    crop_params: list[tuple[int, int, int, int]] = []
    for output_line in output:
        if "crop=" in output_line:
            crop_param = output_line.split("crop=")[1].split(" ")[0]
            parsed_params = parse_crop_params(crop_param)
            if parsed_params:
                crop_params.append(parsed_params)

    if not crop_params:
        return ""

    max_width = max(param[0] for param in crop_params)
    max_height = max(param[1] for param in crop_params)

    min_x = min(param[2] for param in crop_params)
    min_y = min(param[3] for param in crop_params)

    composite_crop = f"{max_width}:{max_height}:{min_x}:{min_y}"

    return composite_crop


def upscale_file(input_path: Path) -> None:
    upscale_command = [
        config.FX_UPSCALE_PATH,
        "--bitrate-scaling-factor",
        config.upscale_quality / 100,
        input_path,
    ]
    run_command(upscale_command, "Upscale video with FX Upscale plugin.")

    if not config.keep_files:
        input_path.unlink(missing_ok=True)


def create_left_right_files(
    disc_info: DiscInfo,
    output_folder: Path,
    mvc_video: Path,
    crop_params: str,
) -> tuple[Path, Path]:
    left_eye_output_path = output_folder / f"{disc_info.name}_left_movie.mov"
    right_eye_output_path = output_folder / f"{disc_info.name}_right_movie.mov"
    if config.start_stage.value <= Stage.CREATE_LEFT_RIGHT_FILES.value:
        split_mvc_to_stereo(
            mvc_video,
            left_eye_output_path,
            right_eye_output_path,
            disc_info,
            crop_params,
        )

    return left_eye_output_path, right_eye_output_path


def get_video_color_depth(input_path: Path) -> int:
    try:
        probe = ffmpeg.probe(
            str(input_path),
            cmd=config.FFPROBE_PATH.as_posix(),
            select_streams="v:0",
            show_entries="stream=pix_fmt",
        )
        streams = probe.get("streams", [])
        if streams:
            pix_fmt = streams[0].get("pix_fmt")
            if "10le" in pix_fmt or "10be" in pix_fmt:
                return 10
            return DiscInfo.color_depth
    except ffmpeg.Error:
        print(f"Error getting video color depth, using default of {DiscInfo.color_depth}")
    return DiscInfo.color_depth


def create_mv_hevc_file(
    left_video_path: Path, right_video_path: Path, output_folder: Path, disc_info: DiscInfo
) -> Path:
    mv_hevc_path = output_folder / f"{disc_info.name}_MV-HEVC.mov"
    if config.start_stage.value <= Stage.COMBINE_TO_MV_HEVC.value:
        combine_to_mv_hevc(left_video_path, right_video_path, mv_hevc_path, disc_info.color_depth)

    if not config.keep_files:
        left_video_path.unlink(missing_ok=True)
        right_video_path.unlink(missing_ok=True)
    return mv_hevc_path


def create_upscaled_file(input_path: Path) -> Path:
    if config.fx_upscale:
        if config.start_stage.value <= Stage.UPSCALE_VIDEO.value:
            upscale_file(input_path)

        upscaled_path = input_path.with_stem(f"{input_path.stem} Upscaled")
        if not upscaled_path.exists():
            raise RuntimeError("Upscaled file not found.")

        return upscaled_path
    return input_path
