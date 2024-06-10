import atexit
from pathlib import Path

import ffmpeg

from bd_to_avp.modules.config import Stage, config
from bd_to_avp.modules.disc import DiscInfo
from bd_to_avp.modules.file import temporary_fifo
from bd_to_avp.modules.util import cleanup_process, run_command, run_ffmpeg_async


def generate_ffmpeg_wrapper_command(
    input_fifo: Path,
    output_path: Path,
    disc_color_depth: int,
    disc_resolution: str,
    disc_frame_rate: str,
    bitrate: int,
    crop_params: str,
    software_encoder: bool,
) -> list[str | Path]:
    pix_fmt = "yuv420p10le" if disc_color_depth == 10 else "yuv420p"
    stream = ffmpeg.input(
        str(input_fifo),
        f="rawvideo",
        pix_fmt=pix_fmt,
        s=config.resolution or disc_resolution,
        r=config.frame_rate or disc_frame_rate,
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
        vprofile="main10" if disc_color_depth == 10 else "main",
        r=config.frame_rate or disc_frame_rate,
    )

    args = ffmpeg.compile(stream, overwrite_output=True)
    return args


def split_mvc_to_stereo(
    video_input_path: Path,
    left_output_path: Path,
    right_output_path: Path,
    disc_info: DiscInfo,
    crop_params: str,
):
    ffmpeg_left_log = left_output_path.with_suffix(".log")
    ffmpeg_right_log = right_output_path.with_suffix(".log")
    is_mts = None
    if config.source_path and config.source_path.suffix.lower() == ".mts":
        is_mts = video_input_path
        video_input_path = config.source_path
    with temporary_fifo("left_fifo", "right_fifo") as (primary_fifo, secondary_fifo):
        ffmpeg_left_command = generate_ffmpeg_wrapper_command(
            primary_fifo,
            left_output_path,
            disc_info.color_depth,
            disc_info.resolution,
            disc_info.frame_rate,
            config.left_right_bitrate,
            crop_params,
            config.software_encoder,
        )
        ffmpeg_right_command = generate_ffmpeg_wrapper_command(
            secondary_fifo,
            right_output_path,
            disc_info.color_depth,
            disc_info.resolution,
            disc_info.frame_rate,
            config.left_right_bitrate,
            crop_params,
            config.software_encoder,
        )

        left_process = run_ffmpeg_async(ffmpeg_left_command, ffmpeg_left_log)
        right_process = run_ffmpeg_async(ffmpeg_right_command, ffmpeg_right_log)

        frim_command = [
            config.WINE_PATH,
            config.FRIMDECODE_PATH,
            "-ts" if is_mts else None,
            "-i:mvc",
            video_input_path,
            video_input_path if is_mts else None,
            "-o",
        ]
        if config.swap_eyes:
            frim_command += [secondary_fifo, primary_fifo]
        else:
            frim_command += [primary_fifo, secondary_fifo]

        atexit.register(cleanup_process, left_process)
        atexit.register(cleanup_process, right_process)

        run_command(frim_command, "FRIM to split MVC to stereo.")
        left_process.wait()
        right_process.wait()

    if not config.keep_files:
        left_output_path.with_suffix(".log").unlink(missing_ok=True)
        right_output_path.with_suffix(".log").unlink(missing_ok=True)
        if is_mts:
            is_mts.unlink(missing_ok=True)
        else:
            video_input_path.unlink(missing_ok=True)

    return left_output_path, right_output_path


def combine_to_mv_hevc(
    left_video_path: Path,
    right_video_path: Path,
    output_path: Path,
) -> None:
    output_path.unlink(missing_ok=True)
    command = [
        config.SPATIAL_MEDIA_PATH,
        "merge",
        "-l",
        left_video_path,
        "-r",
        right_video_path,
        "-q",
        config.mv_hevc_quality,
        "--left-is-primary",
        "--horizontal-field-of-view",
        config.fov,
        "-o",
        output_path,
    ]
    # TODO: figure out why spatial media is throwing a false resolution does not match.
    output = run_command(command, "Combine stereo HEVC streams to MV-HEVC.")
    if "left and right input resolutions do not match. aborting!" in output:
        raise RuntimeError("Left and right input resolutions do not match. Try without AI Upscaling.")
    elif "aborting!" in output:
        raise RuntimeError("Failed to combine stereo HEVC streams to MV-HEVC.")


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


def upscale_file(input_path: Path) -> None:
    upscale_command = [
        config.FX_UPSCALE_PATH,
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
    if color_depth := get_video_color_depth(mvc_video):
        disc_info.color_depth = color_depth
    if config.start_stage.value <= Stage.CREATE_LEFT_RIGHT_FILES.value:
        split_mvc_to_stereo(
            mvc_video,
            left_eye_output_path,
            right_eye_output_path,
            disc_info,
            crop_params,
        )

    if config.fx_upscale:
        if config.start_stage.value <= Stage.UPSCALE_VIDEO.value:
            upscale_file(left_eye_output_path)
            upscale_file(right_eye_output_path)
        left_eye_output_path = left_eye_output_path.with_stem(f"{left_eye_output_path.stem} Upscaled")
        right_eye_output_path = right_eye_output_path.with_stem(f"{right_eye_output_path.stem} Upscaled")

    return left_eye_output_path, right_eye_output_path


def get_video_color_depth(input_path: Path) -> int | None:
    try:
        probe = ffmpeg.probe(str(input_path), select_streams="v:0", show_entries="stream=pix_fmt")
        streams = probe.get("streams", [])
        if streams:
            pix_fmt = streams[0].get("pix_fmt")
            if "10le" in pix_fmt or "10be" in pix_fmt:
                return 10
            return None
    except ffmpeg.Error:
        print(f"Error getting video color depth, using default of {DiscInfo().color_depth}")
    return None


def create_mv_hevc_file(left_video_path: Path, right_video_path: Path, output_folder: Path, disc_name: str) -> Path:
    mv_hevc_path = output_folder / f"{disc_name}_MV-HEVC.mov"
    if config.start_stage.value <= Stage.COMBINE_TO_MV_HEVC.value:
        combine_to_mv_hevc(left_video_path, right_video_path, mv_hevc_path)

    if not config.keep_files:
        left_video_path.unlink(missing_ok=True)
        right_video_path.unlink(missing_ok=True)
    return mv_hevc_path
