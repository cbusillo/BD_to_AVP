import json
import os
import re
import signal
import stat
import subprocess
import tempfile
from pathlib import Path

import ffmpeg

from bd_to_avp.modules.config import Stage, config, is_direct_mvc_stream_enabled
from bd_to_avp.modules.disc import DiscInfo
from bd_to_avp.modules.command import cleanup_process, run_command, run_ffmpeg_capture, run_ffprobe
from bd_to_avp.process_runner import CaptureOverflowPolicy, ProcessRunnerError


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


NATIVE_MVC_PROBE_TIMEOUT_SECONDS = 30
NATIVE_MVC_PROCESS_EXIT_TIMEOUT_SECONDS = 5
NATIVE_MVC_RETRY_SIGNALS = {
    signal.SIGABRT,
    signal.SIGBUS,
    signal.SIGFPE,
    signal.SIGILL,
    signal.SIGSEGV,
}
AV1_STEREO_VEXU_CONTENT_HEX = (
    "00000015657965730000000D737472690000000003000000187061636B00000010706B696E0000000073696465"
)


def generate_native_mvc_splitter_command(video_input_path: Path, *, single_threaded: bool = False) -> list[str | Path]:
    options = "-Osk" if single_threaded else "-Omk"
    return [
        config.EDGE264_TEST_PATH,
        video_input_path,
        options,
    ]


def should_stream_mvc_from_container(video_input_path: Path) -> bool:
    return bool(is_direct_mvc_stream_enabled() and video_input_path.suffix.lower() in [*config.MTS_EXTENSIONS, ".mkv"])


def generate_mvc_annex_b_stream_command(video_input_path: Path) -> list[str | Path]:
    return [
        config.FFMPEG_PATH,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        video_input_path,
        "-map",
        "0:v:0",
        "-c:v",
        "copy",
        "-bsf:v",
        "h264_mp4toannexb",
        "-f",
        "h264",
        "-",
    ]


def should_probe_native_multithread_splitter() -> bool:
    source_path = config.source_path
    if not source_path:
        return False
    return source_path.suffix.lower() in config.IMAGE_EXTENSIONS


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


def generate_native_mvc_av1_command(
    output_path: Path,
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
    left_stream = ffmpeg.filter(split_streams[0], "crop", source_width, source_height, 0, 0)
    right_stream = ffmpeg.filter(split_streams[1], "crop", source_width, source_height, source_width, 0)

    if crop_params:
        left_stream = ffmpeg.filter(left_stream, "crop", *crop_params.split(":"))
        right_stream = ffmpeg.filter(right_stream, "crop", *crop_params.split(":"))

    if disc_info.is_interlaced:
        left_stream = ffmpeg.filter(left_stream, "bwdif")
        right_stream = ffmpeg.filter(right_stream, "bwdif")

    if config.swap_eyes:
        left_stream, right_stream = right_stream, left_stream

    packed_stream = ffmpeg.filter([left_stream, right_stream], "hstack", inputs=2)
    output = ffmpeg.output(
        packed_stream,
        f"file:{output_path}",
        **{
            "vcodec": "libsvtav1",
            "bsf:v": ("av1_metadata=color_primaries=1:transfer_characteristics=1:matrix_coefficients=1:color_range=tv"),
            "crf": config.av1_crf,
            "preset": config.AV1_PRESET,
            "pix_fmt": "yuv420p",
            "color_primaries": "bt709",
            "color_trc": "bt709",
            "colorspace": "bt709",
            "color_range": "tv",
            "r": config.frame_rate or disc_info.frame_rate,
            "movflags": "+faststart",
        },
    )
    command = ffmpeg.compile(output, cmd=config.FFMPEG_PATH.as_posix(), overwrite_output=True)
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
    run_native_mvc_encoding(video_input_path, (left_output_path, right_output_path), ffmpeg_command)
    return left_output_path, right_output_path


def encode_mvc_to_av1_sbs_native(
    video_input_path: Path,
    output_path: Path,
    disc_info: DiscInfo,
    crop_params: str,
) -> Path:
    ffmpeg_command = generate_native_mvc_av1_command(output_path, disc_info, crop_params)
    run_native_mvc_encoding(video_input_path, (output_path,), ffmpeg_command)
    return output_path


def run_native_mvc_encoding(
    video_input_path: Path,
    output_paths: tuple[Path, ...],
    ffmpeg_command: list[str],
) -> None:
    primary_output_path = output_paths[0]
    stream_from_container = should_stream_mvc_from_container(video_input_path)
    producer_command = generate_mvc_annex_b_stream_command(video_input_path) if stream_from_container else None
    native_input_path = Path("-") if stream_from_container else prepare_native_mvc_input(video_input_path)
    splitter_log_path = primary_output_path.with_suffix(".native_mvc.log")
    ffmpeg_log_path = primary_output_path.with_suffix(".native_ffmpeg.log")
    producer_log_path = primary_output_path.with_suffix(".mvc_extract.log")
    try:
        skip_multithreaded_attempt = False
        if not stream_from_container and should_probe_native_multithread_splitter():
            print("Checking native MVC splitter with a short multi-threaded probe.")
            skip_multithreaded_attempt = native_multithread_splitter_probe_crashed(
                native_input_path,
                splitter_log_path,
            )
            if not skip_multithreaded_attempt:
                print("Native MVC splitter probe passed; proceeding with multi-threaded mode.")

        try:
            if skip_multithreaded_attempt:
                print("Native MVC splitter probe crashed; using slower single-threaded mode.")
                run_native_mvc_split_attempt(
                    native_input_path,
                    ffmpeg_command,
                    ffmpeg_log_path,
                    splitter_log_path,
                    producer_command=producer_command,
                    producer_log_path=producer_log_path,
                    single_threaded=True,
                )
            else:
                run_native_mvc_split_attempt(
                    native_input_path,
                    ffmpeg_command,
                    ffmpeg_log_path,
                    splitter_log_path,
                    producer_command=producer_command,
                    producer_log_path=producer_log_path,
                    single_threaded=False,
                )
        except subprocess.CalledProcessError as error:
            if not native_splitter_died_by_signal(error):
                raise

            print("Native MVC splitter crashed; retrying once in single-threaded mode.")
            for output_path in output_paths:
                output_path.unlink(missing_ok=True)
            run_native_mvc_split_attempt(
                native_input_path,
                ffmpeg_command,
                ffmpeg_log_path,
                splitter_log_path,
                producer_command=producer_command,
                producer_log_path=producer_log_path,
                single_threaded=True,
            )
    except subprocess.CalledProcessError as error:
        if native_splitter_should_report_crash(error):
            raise NativeMvcSplitError(build_native_splitter_failure_message(error, splitter_log_path)) from error
        raise
    finally:
        if not stream_from_container and native_input_path != video_input_path:
            native_input_path.unlink(missing_ok=True)


def native_multithread_splitter_probe_crashed(native_input_path: Path, splitter_log_path: Path) -> bool:
    splitter_command = generate_native_mvc_splitter_command(native_input_path, single_threaded=False)
    splitter_process = None
    with open(splitter_log_path, "ab") as splitter_log:
        splitter_log.write(b"\n--- Native MVC split probe: multi-threaded ---\n")
        try:
            splitter_process = subprocess.Popen(
                splitter_command,
                stdout=subprocess.DEVNULL,
                stderr=splitter_log,
            )
            splitter_process.wait(timeout=NATIVE_MVC_PROBE_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            splitter_log.write(b"\n--- Probe timed out; process stayed alive, continuing multi-threaded ---\n")
            if splitter_process:
                cleanup_process(splitter_process)
                try:
                    splitter_process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    splitter_process.kill()
                    splitter_process.wait(timeout=2)
            return False

    if splitter_process.returncode == 0:
        return False
    if splitter_process.returncode < 0:
        if native_splitter_returncode_is_retry_signal(splitter_process.returncode):
            return True
        raise subprocess.CalledProcessError(
            splitter_process.returncode,
            splitter_command,
            output=splitter_log_path.read_text(errors="replace"),
        )
    raise subprocess.CalledProcessError(
        splitter_process.returncode,
        splitter_command,
        output=splitter_log_path.read_text(errors="replace"),
    )


def run_native_mvc_split_attempt(
    native_input_path: Path,
    ffmpeg_command: list[str],
    ffmpeg_log_path: Path,
    splitter_log_path: Path,
    *,
    producer_command: list[str | Path] | None = None,
    producer_log_path: Path | None = None,
    single_threaded: bool,
) -> None:
    splitter_command = generate_native_mvc_splitter_command(native_input_path, single_threaded=single_threaded)
    attempt_name = "single-threaded" if single_threaded else "multi-threaded"
    print(f"Running native MVC split and encode ({attempt_name}).")

    if config.output_commands:
        splitter_command_text = " ".join(str(command) for command in splitter_command)
        ffmpeg_command_text = " ".join(ffmpeg_command)
        command_text = f"{splitter_command_text} | {ffmpeg_command_text}"
        if producer_command:
            producer_command_text = " ".join(str(command) for command in producer_command)
            command_text = f"{producer_command_text} | {command_text}"
        print(f"Running command:\n{command_text}")

    producer_process = None
    splitter_process = None
    ffmpeg_process = None
    try:
        producer_log_context = (
            open(producer_log_path, "a") if producer_command and producer_log_path else open(os.devnull, "w")
        )
        with (
            open(ffmpeg_log_path, "a") as ffmpeg_log,
            open(splitter_log_path, "ab") as splitter_log,
            producer_log_context as producer_log,
        ):
            ffmpeg_log.write(f"\n--- Native MVC split attempt: {attempt_name} ---\n")
            splitter_log.write(f"\n--- Native MVC split attempt: {attempt_name} ---\n".encode())
            if producer_command:
                producer_log.write(f"\n--- MVC extraction stream attempt: {attempt_name} ---\n")
                producer_process = subprocess.Popen(
                    producer_command,
                    stdout=subprocess.PIPE,
                    stderr=producer_log,
                )
            splitter_process = subprocess.Popen(
                splitter_command,
                stdin=producer_process.stdout if producer_process else None,
                stdout=subprocess.PIPE,
                stderr=splitter_log,
            )

            if producer_process and producer_process.stdout:
                producer_process.stdout.close()
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
            producer_exited_before_cleanup = producer_process is not None and producer_process.poll() is not None
            if ffmpeg_process.returncode != 0:
                terminate_native_pipeline_process(splitter_process)
                if producer_process:
                    terminate_native_pipeline_process(producer_process)
            else:
                wait_for_native_pipeline_process(splitter_process)
            if producer_process:
                wait_for_native_pipeline_process(producer_process)

        if (
            splitter_process.returncode != 0
            and splitter_failed_before_cleanup
            and native_splitter_returncode_is_retry_signal(splitter_process.returncode)
        ):
            raise subprocess.CalledProcessError(
                splitter_process.returncode,
                splitter_command,
                output=splitter_log_path.read_text(errors="replace"),
            )
        if (
            producer_process
            and producer_process.returncode != 0
            and producer_exited_before_cleanup
            and producer_process.returncode != -signal.SIGPIPE
        ):
            assert producer_command is not None
            raise subprocess.CalledProcessError(
                producer_process.returncode,
                producer_command,
                output=producer_log_path.read_text(errors="replace") if producer_log_path else "",
            )
        if ffmpeg_process.returncode != 0:
            raise subprocess.CalledProcessError(ffmpeg_process.returncode, ffmpeg_command)
        if splitter_process.returncode != 0:
            raise subprocess.CalledProcessError(
                splitter_process.returncode,
                splitter_command,
                output=splitter_log_path.read_text(errors="replace"),
            )
        if producer_process and producer_process.returncode != 0:
            assert producer_command is not None
            raise subprocess.CalledProcessError(
                producer_process.returncode,
                producer_command,
                output=producer_log_path.read_text(errors="replace") if producer_log_path else "",
            )
    finally:
        if producer_process:
            terminate_native_pipeline_process(producer_process)
        if splitter_process:
            terminate_native_pipeline_process(splitter_process)
        if ffmpeg_process:
            terminate_native_pipeline_process(ffmpeg_process)


def wait_for_native_pipeline_process(process: subprocess.Popen) -> None:
    try:
        process.wait(timeout=NATIVE_MVC_PROCESS_EXIT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        terminate_native_pipeline_process(process)


def terminate_native_pipeline_process(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=NATIVE_MVC_PROCESS_EXIT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=NATIVE_MVC_PROCESS_EXIT_TIMEOUT_SECONDS)


def native_splitter_died_by_signal(error: subprocess.CalledProcessError) -> bool:
    if error.returncode >= 0 or not error.cmd or Path(error.cmd[0]).name != config.EDGE264_TEST_PATH.name:
        return False
    return native_splitter_returncode_is_retry_signal(error.returncode)


def native_splitter_returncode_is_retry_signal(returncode: int) -> bool:
    if returncode >= 0:
        return False
    try:
        return signal.Signals(-returncode) in NATIVE_MVC_RETRY_SIGNALS
    except ValueError:
        return False


def native_splitter_should_report_crash(error: subprocess.CalledProcessError) -> bool:
    if not error.cmd or Path(error.cmd[0]).name != config.EDGE264_TEST_PATH.name:
        return False
    return error.returncode > 0 or native_splitter_died_by_signal(error)


def build_native_splitter_failure_message(error: subprocess.CalledProcessError, splitter_log_path: Path) -> str:
    if error.returncode >= 0:
        signal_name = f"exit code {error.returncode}"
    else:
        try:
            signal_name = signal.Signals(-error.returncode).name
        except ValueError:
            signal_name = f"signal {-error.returncode}"
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
            left_output_path.with_suffix(".mvc_extract.log").unlink(missing_ok=True)
            if not should_stream_mvc_from_container(video_input_path):
                video_input_path.unlink(missing_ok=True)
        return result

    raise RuntimeError(explain_native_mvc_unavailable(disc_info))


def encode_mvc_to_av1_sbs(
    video_input_path: Path,
    output_path: Path,
    disc_info: DiscInfo,
    crop_params: str,
) -> Path:
    if can_use_native_mvc_splitter(disc_info):
        result = encode_mvc_to_av1_sbs_native(video_input_path, output_path, disc_info, crop_params)
        if not config.keep_files:
            output_path.with_suffix(".native_ffmpeg.log").unlink(missing_ok=True)
            output_path.with_suffix(".native_mvc.log").unlink(missing_ok=True)
            output_path.with_suffix(".mvc_extract.log").unlink(missing_ok=True)
            if not should_stream_mvc_from_container(video_input_path):
                video_input_path.unlink(missing_ok=True)
        return result

    raise RuntimeError(explain_native_mvc_unavailable(disc_info))


def av1_stereo_patch_xml() -> str:
    return (
        '<?xml version="1.0"?>\n'
        "<GPACBOXES>\n"
        '  <Box path="trak.mdia.minf.stbl.stsd.av01.av1C+" trackID="1">\n'
        '    <BS fcc="vexu"/>\n'
        f'    <BS data="{AV1_STEREO_VEXU_CONTENT_HEX}"/>\n'
        "  </Box>\n"
        "</GPACBOXES>\n"
    )


def add_av1_stereo_metadata(input_path: Path, output_path: Path) -> None:
    output_path.unlink(missing_ok=True)
    file_descriptor, patch_name = tempfile.mkstemp(prefix="bd-to-avp-av1-stereo-", suffix=".xml")
    os.close(file_descriptor)
    patch_path = Path(patch_name)
    try:
        patch_path.write_text(av1_stereo_patch_xml(), encoding="utf-8")
        run_command(
            [config.MP4BOX_PATH, "-patch", patch_path, input_path, "-out", output_path],
            "add Apple stereo packing metadata to AV1 video.",
        )
    finally:
        patch_path.unlink(missing_ok=True)


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
        "--horizontal-disparity-adjustment",
        0,
        "--color-depth",
        color_depth,
        "--output-file",
        output_path,
    ]
    output = run_command(
        command,
        "combine stereo HEVC streams to MV-HEVC.",
        capture_overflow=CaptureOverflowPolicy.FAIL,
    )
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
        _, stderr = run_ffmpeg_capture(stream)
        output = stderr.decode("utf-8", errors="replace").splitlines()
    except ffmpeg.Error as e:
        print("FFmpeg Error:")
        print(e.stderr.decode("utf-8", errors="replace"))
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


def create_av1_sbs_file(
    disc_info: DiscInfo,
    output_folder: Path,
    mvc_video: Path,
    crop_params: str,
) -> Path:
    output_path = output_folder / f"{disc_info.name}_AV1-SBS-unmarked.mp4"
    if config.start_stage.value <= Stage.CREATE_LEFT_RIGHT_FILES.value:
        encode_mvc_to_av1_sbs(mvc_video, output_path, disc_info, crop_params)
    return output_path


def get_video_color_depth(input_path: Path) -> int:
    try:
        probe = run_ffprobe(
            input_path,
            select_streams="v:0",
            show_entries="stream=pix_fmt",
        )
        streams = probe.get("streams", [])
        if streams:
            pix_fmt = streams[0].get("pix_fmt")
            if "10le" in pix_fmt or "10be" in pix_fmt:
                return 10
            return DiscInfo.color_depth
    except (ffmpeg.Error, json.JSONDecodeError, ProcessRunnerError):
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


def create_av1_stereo_file(input_path: Path, output_folder: Path, disc_info: DiscInfo) -> Path:
    output_path = output_folder / f"{disc_info.name}_AV1-Stereo.mp4"
    if config.start_stage.value <= Stage.COMBINE_TO_MV_HEVC.value:
        add_av1_stereo_metadata(input_path, output_path)
    if not config.keep_files:
        input_path.unlink(missing_ok=True)
    return output_path


def create_upscaled_file(input_path: Path) -> Path:
    if config.fx_upscale:
        if config.start_stage.value <= Stage.UPSCALE_VIDEO.value:
            upscale_file(input_path)

        upscaled_path = input_path.with_stem(f"{input_path.stem} Upscaled")
        if not upscaled_path.exists():
            raise RuntimeError("Upscaled file not found.")

        return upscaled_path
    return input_path
