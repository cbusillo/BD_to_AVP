import json
import os
import re
import signal
import stat
import subprocess
import tempfile
import threading
from pathlib import Path

import ffmpeg

from bd_to_avp.observability import ObservabilityContext
from bd_to_avp.modules.config import Stage, config, is_direct_mvc_stream_enabled
from bd_to_avp.modules.disc import DiscInfo
from bd_to_avp.modules.command import combined_process_output, run_ffmpeg_capture, run_ffprobe, run_process_capture
from bd_to_avp.process_runner import (
    CaptureOverflowPolicy,
    ProcessArtifactNoProgressError,
    ProcessArtifactProbe,
    ProcessCancelled,
    ProcessPipelineError,
    ProcessPipelineRunner,
    ProcessPipelineStage,
    ProcessRunnerError,
    ProcessSpec,
)
from bd_to_avp.presentation import cli_message
from bd_to_avp.runtime import RunContext


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


NATIVE_MVC_ARTIFACT_NO_GROWTH_TIMEOUT_SECONDS = 120
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


def output_artifact_roles(output_count: int) -> tuple[str, ...]:
    if output_count == 1:
        return ("stereo_video_output",)
    if output_count == 2:
        return ("left_eye_video_output", "right_eye_video_output")
    return tuple(f"video_output_{index}" for index in range(1, output_count + 1))


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


def generate_direct_mv_hevc_normalizer_command(
    disc_info: DiscInfo,
    crop_params: str,
) -> list[str]:
    if disc_info.color_depth != 8:
        raise ValueError("Direct MV-HEVC encoding currently supports 8-bit 4:2:0 Blu-ray MVC sources only.")

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
        "pipe:1",
        format="yuv4mpegpipe",
        pix_fmt="yuv420p",
        r=config.frame_rate or disc_info.frame_rate,
    )
    command = ffmpeg.compile(output, cmd=config.FFMPEG_PATH.as_posix(), overwrite_output=True)
    return [arg if arg != "pipe:0" else "-" for arg in command]


def generate_direct_mv_hevc_encoder_command(
    output_path: Path,
    bitrate_mbps: int,
) -> list[str | Path]:
    return [
        config.MV_HEVC_ENCODER_PATH,
        "--output",
        output_path,
        "--bitrate-mbps",
        str(bitrate_mbps),
        "--fov",
        str(config.fov),
        "--disparity-adjustment",
        "0",
        "--overwrite",
    ]


def parse_resolution(resolution: str) -> tuple[int, int]:
    width, height = resolution.lower().split("x", 1)
    return int(width), int(height)


def split_mvc_to_stereo_native(
    video_input_path: Path,
    left_output_path: Path,
    right_output_path: Path,
    disc_info: DiscInfo,
    crop_params: str,
    *,
    run_context: RunContext | None = None,
    cancellation_event: threading.Event | None = None,
    observability_context: ObservabilityContext | None = None,
) -> tuple[Path, Path]:
    ffmpeg_command = generate_native_mvc_ffmpeg_command(left_output_path, right_output_path, disc_info, crop_params)
    run_native_mvc_encoding(
        video_input_path,
        (left_output_path, right_output_path),
        ffmpeg_command,
        run_context=run_context,
        cancellation_event=cancellation_event,
        observability_context=observability_context,
    )
    return left_output_path, right_output_path


def encode_mvc_to_av1_sbs_native(
    video_input_path: Path,
    output_path: Path,
    disc_info: DiscInfo,
    crop_params: str,
    *,
    run_context: RunContext | None = None,
    cancellation_event: threading.Event | None = None,
    observability_context: ObservabilityContext | None = None,
) -> Path:
    ffmpeg_command = generate_native_mvc_av1_command(output_path, disc_info, crop_params)
    run_native_mvc_encoding(
        video_input_path,
        (output_path,),
        ffmpeg_command,
        run_context=run_context,
        cancellation_event=cancellation_event,
        observability_context=observability_context,
    )
    return output_path


def run_native_mvc_encoding(
    video_input_path: Path,
    output_paths: tuple[Path, ...],
    ffmpeg_command: list[str],
    *,
    run_context: RunContext | None = None,
    cancellation_event: threading.Event | None = None,
    observability_context: ObservabilityContext | None = None,
) -> None:
    stream_from_container = should_stream_mvc_from_container(video_input_path)
    producer_command = generate_mvc_annex_b_stream_command(video_input_path) if stream_from_container else None
    native_input_path = Path("-") if stream_from_container else prepare_native_mvc_input(video_input_path)
    try:
        single_threaded = False
        try:
            run_native_mvc_split_attempt(
                native_input_path,
                ffmpeg_command,
                output_paths,
                producer_command=producer_command,
                single_threaded=single_threaded,
                run_context=run_context,
                cancellation_event=cancellation_event,
                observability_context=observability_context,
            )
        except (ProcessArtifactNoProgressError, subprocess.CalledProcessError) as error:
            retry_after_stall = isinstance(error, ProcessArtifactNoProgressError)
            retry_after_crash = isinstance(error, subprocess.CalledProcessError) and native_splitter_died_by_signal(
                error
            )
            if single_threaded or not (retry_after_stall or retry_after_crash):
                raise

            cli_message(
                (
                    "Native MVC splitter stopped making progress; retrying once in single-threaded mode."
                    if retry_after_stall
                    else "Native MVC splitter crashed; retrying once in single-threaded mode."
                ),
                run_context=run_context,
            )
            for output_path in output_paths:
                output_path.unlink(missing_ok=True)
            run_native_mvc_split_attempt(
                native_input_path,
                ffmpeg_command,
                output_paths,
                producer_command=producer_command,
                single_threaded=True,
                run_context=run_context,
                cancellation_event=cancellation_event,
                observability_context=observability_context,
            )
    except ProcessArtifactNoProgressError as error:
        raise NativeMvcSplitError(build_native_splitter_stall_message(error)) from error
    except subprocess.CalledProcessError as error:
        if native_splitter_should_report_crash(error):
            raise NativeMvcSplitError(build_native_splitter_failure_message(error)) from error
        raise
    finally:
        if not stream_from_container and native_input_path != video_input_path:
            native_input_path.unlink(missing_ok=True)


def run_direct_mv_hevc_encoding(
    video_input_path: Path,
    output_path: Path,
    normalizer_command: list[str],
    encoder_command: list[str | Path],
    *,
    run_context: RunContext | None = None,
    cancellation_event: threading.Event | None = None,
    observability_context: ObservabilityContext | None = None,
) -> None:
    stream_from_container = should_stream_mvc_from_container(video_input_path)
    producer_command = generate_mvc_annex_b_stream_command(video_input_path) if stream_from_container else None
    native_input_path = Path("-") if stream_from_container else prepare_native_mvc_input(video_input_path)

    try:
        try:
            run_direct_mv_hevc_attempt(
                native_input_path,
                output_path,
                normalizer_command,
                encoder_command,
                producer_command=producer_command,
                single_threaded=False,
                run_context=run_context,
                cancellation_event=cancellation_event,
                observability_context=observability_context,
            )
        except (ProcessArtifactNoProgressError, subprocess.CalledProcessError) as error:
            retry_after_crash = isinstance(error, subprocess.CalledProcessError) and native_splitter_died_by_signal(
                error
            )
            if not isinstance(error, ProcessArtifactNoProgressError) and not retry_after_crash:
                raise
            retry_reason = (
                "Direct MV-HEVC output stopped making progress; retrying once with single-threaded MVC decoding."
                if isinstance(error, ProcessArtifactNoProgressError)
                else "Native MVC splitter crashed; retrying direct MV-HEVC once in single-threaded mode."
            )
            cli_message(retry_reason, run_context=run_context)
            remove_direct_mv_hevc_attempt_artifacts(output_path)
            run_direct_mv_hevc_attempt(
                native_input_path,
                output_path,
                normalizer_command,
                encoder_command,
                producer_command=producer_command,
                single_threaded=True,
                run_context=run_context,
                cancellation_event=cancellation_event,
                observability_context=observability_context,
            )
    except ProcessArtifactNoProgressError as error:
        raise NativeMvcSplitError(build_direct_mv_hevc_stall_message(error)) from error
    except subprocess.CalledProcessError as error:
        if native_splitter_should_report_crash(error):
            raise NativeMvcSplitError(build_native_splitter_failure_message(error)) from error
        raise
    finally:
        if not stream_from_container and native_input_path != video_input_path:
            native_input_path.unlink(missing_ok=True)


def run_direct_mv_hevc_attempt(
    native_input_path: Path,
    output_path: Path,
    normalizer_command: list[str],
    encoder_command: list[str | Path],
    *,
    producer_command: list[str | Path] | None,
    single_threaded: bool,
    run_context: RunContext | None = None,
    cancellation_event: threading.Event | None = None,
    observability_context: ObservabilityContext | None = None,
) -> None:
    splitter_command = generate_native_mvc_splitter_command(native_input_path, single_threaded=single_threaded)
    attempt_name = "single-threaded" if single_threaded else "multi-threaded"
    cli_message(f"Running direct MVC to MV-HEVC encode ({attempt_name}).", run_context=run_context)

    if config.output_commands and run_context is None:
        commands: list[list[str | Path]] = [splitter_command, [*normalizer_command], encoder_command]
        if producer_command:
            commands.insert(0, producer_command)
        cli_message(
            "Running command:\n" + " | ".join(" ".join(str(argument) for argument in command) for command in commands)
        )

    event_context = observability_context or ObservabilityContext()
    stages: list[ProcessPipelineStage] = []
    if producer_command:
        stages.append(
            ProcessPipelineStage(
                ProcessSpec(
                    argv=tuple(producer_command),
                    tool_id="ffmpeg",
                    display_name=f"Extract MVC stream ({attempt_name})",
                    env=os.environ.copy(),
                    event_context=event_context,
                    capture_overflow=CaptureOverflowPolicy.TRUNCATE,
                )
            )
        )
    stages.extend(
        (
            ProcessPipelineStage(
                ProcessSpec(
                    argv=tuple(splitter_command),
                    tool_id="edge264",
                    display_name=f"Split native MVC stream ({attempt_name})",
                    env=os.environ.copy(),
                    event_context=event_context,
                    capture_overflow=CaptureOverflowPolicy.TRUNCATE,
                )
            ),
            ProcessPipelineStage(
                ProcessSpec(
                    argv=tuple(normalizer_command),
                    tool_id="ffmpeg",
                    display_name=f"Normalize direct stereo video ({attempt_name})",
                    env=os.environ.copy(),
                    event_context=event_context,
                    capture_overflow=CaptureOverflowPolicy.TRUNCATE,
                )
            ),
            ProcessPipelineStage(
                ProcessSpec(
                    argv=tuple(encoder_command),
                    tool_id="mv_hevc_encoder",
                    display_name=f"Encode direct MV-HEVC video ({attempt_name})",
                    env=os.environ.copy(),
                    event_context=event_context,
                    artifacts=(
                        ProcessArtifactProbe(
                            "stereo_video_output",
                            resolver=lambda: resolve_direct_mv_hevc_artifact(output_path),
                        ),
                    ),
                    artifact_no_growth_timeout_seconds=NATIVE_MVC_ARTIFACT_NO_GROWTH_TIMEOUT_SECONDS,
                    artifact_no_growth_retryable=not single_threaded,
                    capture_overflow=CaptureOverflowPolicy.TRUNCATE,
                )
            ),
        )
    )
    try:
        ProcessPipelineRunner(exit_grace_seconds=5).run(
            tuple(stages),
            run_context=run_context,
            cancellation_event=cancellation_event,
        )
    except ProcessPipelineError as error:
        selected_error = select_direct_mv_hevc_pipeline_error(error, producer_present=producer_command is not None)
        if selected_error is not None:
            raise selected_error from error


def select_direct_mv_hevc_pipeline_error(
    pipeline_error: ProcessPipelineError,
    *,
    producer_present: bool,
) -> BaseException | None:
    stages = pipeline_error.result.stages
    producer_index = 0 if producer_present else None
    splitter_index = 1 if producer_present else 0
    normalizer_index = splitter_index + 1
    encoder_index = normalizer_index + 1
    producer_stage = stages[producer_index] if producer_index is not None else None
    splitter_stage = stages[splitter_index]
    normalizer_stage = stages[normalizer_index]
    encoder_stage = stages[encoder_index]

    if isinstance(splitter_stage.error, subprocess.CalledProcessError) and native_splitter_died_by_signal(
        splitter_stage.error
    ):
        return splitter_stage.error
    for stage in (producer_stage, splitter_stage, normalizer_stage):
        if (
            stage is not None
            and stage.completed_before_final
            and stage.error is not None
            and not process_error_is_sigpipe(stage.error)
        ):
            return stage.error
    if encoder_stage.error is not None:
        return encoder_stage.error
    for stage in (normalizer_stage, splitter_stage, producer_stage):
        if stage is not None and stage.error is not None and not process_error_is_sigpipe(stage.error):
            return stage.error
    return None


def resolve_direct_mv_hevc_artifact(output_path: Path) -> Path | None:
    candidates: list[tuple[int, Path]] = []
    for candidate in output_path.parent.glob(f".{output_path.name}.partial-*"):
        try:
            candidates.append((candidate.stat().st_mtime_ns, candidate))
        except OSError:
            continue
    if candidates:
        return max(candidates)[1]
    return output_path if output_path.exists() else None


def remove_direct_mv_hevc_attempt_artifacts(output_path: Path) -> None:
    for partial_path in output_path.parent.glob(f".{output_path.name}.partial-*"):
        partial_path.unlink(missing_ok=True)


def run_native_mvc_split_attempt(
    native_input_path: Path,
    ffmpeg_command: list[str],
    output_paths: tuple[Path, ...],
    *,
    producer_command: list[str | Path] | None = None,
    single_threaded: bool,
    run_context: RunContext | None = None,
    cancellation_event: threading.Event | None = None,
    observability_context: ObservabilityContext | None = None,
) -> None:
    splitter_command = generate_native_mvc_splitter_command(native_input_path, single_threaded=single_threaded)
    attempt_name = "single-threaded" if single_threaded else "multi-threaded"
    cli_message(f"Running native MVC split and encode ({attempt_name}).", run_context=run_context)

    if config.output_commands and run_context is None:
        splitter_command_text = " ".join(str(command) for command in splitter_command)
        ffmpeg_command_text = " ".join(ffmpeg_command)
        command_text = f"{splitter_command_text} | {ffmpeg_command_text}"
        if producer_command:
            producer_command_text = " ".join(str(command) for command in producer_command)
            command_text = f"{producer_command_text} | {command_text}"
        cli_message(f"Running command:\n{command_text}")

    event_context = observability_context or ObservabilityContext()
    stages: list[ProcessPipelineStage] = []
    if producer_command:
        stages.append(
            ProcessPipelineStage(
                ProcessSpec(
                    argv=tuple(producer_command),
                    tool_id="ffmpeg",
                    display_name=f"Extract MVC stream ({attempt_name})",
                    env=os.environ.copy(),
                    event_context=event_context,
                    capture_overflow=CaptureOverflowPolicy.TRUNCATE,
                )
            )
        )
    stages.extend(
        (
            ProcessPipelineStage(
                ProcessSpec(
                    argv=tuple(splitter_command),
                    tool_id="edge264",
                    display_name=f"Split native MVC stream ({attempt_name})",
                    env=os.environ.copy(),
                    event_context=event_context,
                    capture_overflow=CaptureOverflowPolicy.TRUNCATE,
                )
            ),
            ProcessPipelineStage(
                ProcessSpec(
                    argv=tuple(ffmpeg_command),
                    tool_id="ffmpeg",
                    display_name=f"Encode stereo video ({attempt_name})",
                    env=os.environ.copy(),
                    event_context=event_context,
                    artifacts=tuple(
                        ProcessArtifactProbe(role, path=output_path)
                        for role, output_path in zip(
                            output_artifact_roles(len(output_paths)), output_paths, strict=True
                        )
                    ),
                    artifact_no_growth_timeout_seconds=NATIVE_MVC_ARTIFACT_NO_GROWTH_TIMEOUT_SECONDS,
                    artifact_no_growth_retryable=not single_threaded,
                    capture_overflow=CaptureOverflowPolicy.TRUNCATE,
                )
            ),
        )
    )
    try:
        ProcessPipelineRunner(exit_grace_seconds=5).run(
            tuple(stages),
            run_context=run_context,
            cancellation_event=cancellation_event,
        )
    except ProcessPipelineError as error:
        selected_error = select_native_pipeline_error(error, producer_present=producer_command is not None)
        if selected_error is not None:
            raise selected_error from error


def select_native_pipeline_error(
    pipeline_error: ProcessPipelineError,
    *,
    producer_present: bool,
) -> BaseException | None:
    stages = pipeline_error.result.stages
    producer_index = 0 if producer_present else None
    splitter_index = 1 if producer_present else 0
    ffmpeg_index = 2 if producer_present else 1
    producer_stage = stages[producer_index] if producer_index is not None else None
    splitter_stage = stages[splitter_index]
    ffmpeg_stage = stages[ffmpeg_index]

    if isinstance(splitter_stage.error, subprocess.CalledProcessError) and native_splitter_died_by_signal(
        splitter_stage.error
    ):
        return splitter_stage.error
    if (
        producer_stage is not None
        and producer_stage.completed_before_final
        and producer_stage.error is not None
        and not process_error_is_sigpipe(producer_stage.error)
    ):
        return producer_stage.error
    if ffmpeg_stage.error is not None:
        return ffmpeg_stage.error
    if splitter_stage.error is not None:
        return splitter_stage.error
    if producer_stage is not None and producer_stage.error is not None:
        if process_error_is_sigpipe(producer_stage.error):
            return None
        return producer_stage.error
    return None


def process_error_is_sigpipe(error: BaseException) -> bool:
    return isinstance(error, subprocess.CalledProcessError) and error.returncode == -signal.SIGPIPE


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


def build_native_splitter_failure_message(error: subprocess.CalledProcessError) -> str:
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
        "Please submit a diagnostic report so the bounded splitter and encoder evidence can be reviewed."
    )


def build_native_splitter_stall_message(error: ProcessArtifactNoProgressError) -> str:
    return (
        "The native MVC splitter stopped producing video in both multi-threaded and single-threaded modes. "
        "The conversion was stopped instead of waiting indefinitely. This usually means the bundled native decoder "
        "hit damaged or unsupported MVC data. Please submit a diagnostic report so the bounded splitter and encoder "
        f"evidence can be reviewed. ({error})"
    )


def build_direct_mv_hevc_stall_message(error: ProcessArtifactNoProgressError) -> str:
    return (
        "Direct MV-HEVC output stopped making progress during both multi-threaded and single-threaded MVC decoding. "
        "The conversion was stopped instead of waiting indefinitely. The splitter, geometry normalizer, or direct "
        "encoder may have stalled. Please submit a diagnostic report so the bounded pipeline evidence can be reviewed. "
        f"({error})"
    )


def split_mvc_to_stereo(
    video_input_path: Path,
    left_output_path: Path,
    right_output_path: Path,
    disc_info: DiscInfo,
    crop_params: str,
    *,
    run_context: RunContext | None = None,
    cancellation_event: threading.Event | None = None,
    observability_context: ObservabilityContext | None = None,
) -> tuple[Path, Path]:
    if can_use_native_mvc_splitter(disc_info):
        result = split_mvc_to_stereo_native(
            video_input_path,
            left_output_path,
            right_output_path,
            disc_info,
            crop_params,
            run_context=run_context,
            cancellation_event=cancellation_event,
            observability_context=observability_context,
        )
        if not config.keep_files:
            if not should_stream_mvc_from_container(video_input_path):
                video_input_path.unlink(missing_ok=True)
        return result

    raise RuntimeError(explain_native_mvc_unavailable(disc_info))


def encode_mvc_to_av1_sbs(
    video_input_path: Path,
    output_path: Path,
    disc_info: DiscInfo,
    crop_params: str,
    *,
    run_context: RunContext | None = None,
    cancellation_event: threading.Event | None = None,
    observability_context: ObservabilityContext | None = None,
) -> Path:
    if can_use_native_mvc_splitter(disc_info):
        result = encode_mvc_to_av1_sbs_native(
            video_input_path,
            output_path,
            disc_info,
            crop_params,
            run_context=run_context,
            cancellation_event=cancellation_event,
            observability_context=observability_context,
        )
        if not config.keep_files:
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


def add_av1_stereo_metadata(
    input_path: Path,
    output_path: Path,
    *,
    run_context: RunContext | None = None,
    cancellation_event: threading.Event | None = None,
    observability_context: ObservabilityContext | None = None,
) -> None:
    output_path.unlink(missing_ok=True)
    file_descriptor, patch_name = tempfile.mkstemp(prefix="bd-to-avp-av1-stereo-", suffix=".xml")
    os.close(file_descriptor)
    patch_path = Path(patch_name)
    try:
        patch_path.write_text(av1_stereo_patch_xml(), encoding="utf-8")
        run_process_capture(
            [config.MP4BOX_PATH, "-patch", patch_path, input_path, "-out", output_path],
            "add Apple stereo packing metadata to AV1 video.",
            tool_id="mp4box",
            run_context=run_context,
            cancellation_event=cancellation_event,
            observability_context=observability_context,
            capture_overflow=CaptureOverflowPolicy.TRUNCATE,
            show_spinner=True,
        )
    finally:
        patch_path.unlink(missing_ok=True)


def combine_to_mv_hevc(
    left_video_path: Path,
    right_video_path: Path,
    output_path: Path,
    color_depth: int,
    *,
    run_context: RunContext | None = None,
    cancellation_event: threading.Event | None = None,
    observability_context: ObservabilityContext | None = None,
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
    process_result = run_process_capture(
        command,
        "combine stereo HEVC streams to MV-HEVC.",
        tool_id="spatial_media_kit_tool",
        merge_stderr=False,
        capture_overflow=CaptureOverflowPolicy.FAIL,
        run_context=run_context,
        cancellation_event=cancellation_event,
        observability_context=observability_context,
        show_spinner=True,
    )
    output = combined_process_output(process_result)
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
    *,
    run_context: RunContext | None = None,
    cancellation_event: threading.Event | None = None,
    observability_context: ObservabilityContext | None = None,
) -> str:
    cli_message("Detecting crop parameters...", run_context=run_context)
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
        _, stderr = run_ffmpeg_capture(
            stream,
            run_context=run_context,
            cancellation_event=cancellation_event,
            observability_context=observability_context,
        )
        output = stderr.decode("utf-8", errors="replace").splitlines()
    except ffmpeg.Error as e:
        cli_message("FFmpeg Error:", run_context=run_context)
        cli_message(e.stderr.decode("utf-8", errors="replace"), run_context=run_context)
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


def upscale_file(
    input_path: Path,
    *,
    run_context: RunContext | None = None,
    cancellation_event: threading.Event | None = None,
    observability_context: ObservabilityContext | None = None,
) -> None:
    upscale_command = [
        config.FX_UPSCALE_PATH,
        "--bitrate-scaling-factor",
        config.upscale_quality / 100,
        input_path,
    ]
    run_process_capture(
        upscale_command,
        "Upscale video with FX Upscale plugin.",
        tool_id="fx_upscale",
        run_context=run_context,
        cancellation_event=cancellation_event,
        observability_context=observability_context,
        capture_overflow=CaptureOverflowPolicy.TRUNCATE,
        show_spinner=True,
    )

    if not config.keep_files:
        input_path.unlink(missing_ok=True)


def create_left_right_files(
    disc_info: DiscInfo,
    output_folder: Path,
    mvc_video: Path,
    crop_params: str,
    *,
    run_context: RunContext | None = None,
    cancellation_event: threading.Event | None = None,
    observability_context: ObservabilityContext | None = None,
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
            run_context=run_context,
            cancellation_event=cancellation_event,
            observability_context=observability_context,
        )

    return left_eye_output_path, right_eye_output_path


def create_av1_sbs_file(
    disc_info: DiscInfo,
    output_folder: Path,
    mvc_video: Path,
    crop_params: str,
    *,
    run_context: RunContext | None = None,
    cancellation_event: threading.Event | None = None,
    observability_context: ObservabilityContext | None = None,
) -> Path:
    output_path = output_folder / f"{disc_info.name}_AV1-SBS-unmarked.mp4"
    if config.start_stage.value <= Stage.CREATE_LEFT_RIGHT_FILES.value:
        encode_mvc_to_av1_sbs(
            mvc_video,
            output_path,
            disc_info,
            crop_params,
            run_context=run_context,
            cancellation_event=cancellation_event,
            observability_context=observability_context,
        )
    return output_path


def get_video_color_depth(
    input_path: Path,
    *,
    run_context: RunContext | None = None,
    cancellation_event: threading.Event | None = None,
    observability_context: ObservabilityContext | None = None,
) -> int:
    try:
        probe = run_ffprobe(
            input_path,
            run_context=run_context,
            cancellation_event=cancellation_event,
            observability_context=observability_context,
            select_streams="v:0",
            show_entries="stream=pix_fmt",
        )
        streams = probe.get("streams", [])
        if streams:
            pix_fmt = streams[0].get("pix_fmt")
            if "10le" in pix_fmt or "10be" in pix_fmt:
                return 10
            return DiscInfo.color_depth
    except ProcessCancelled:
        raise
    except (ffmpeg.Error, json.JSONDecodeError, ProcessRunnerError, UnicodeDecodeError):
        cli_message(
            f"Error getting video color depth, using default of {DiscInfo.color_depth}",
            run_context=run_context,
        )
    return DiscInfo.color_depth


def create_mv_hevc_file(
    left_video_path: Path,
    right_video_path: Path,
    output_folder: Path,
    disc_info: DiscInfo,
    *,
    run_context: RunContext | None = None,
    cancellation_event: threading.Event | None = None,
    observability_context: ObservabilityContext | None = None,
) -> Path:
    mv_hevc_path = output_folder / f"{disc_info.name}_MV-HEVC.mov"
    if config.start_stage.value <= Stage.COMBINE_TO_MV_HEVC.value:
        combine_to_mv_hevc(
            left_video_path,
            right_video_path,
            mv_hevc_path,
            disc_info.color_depth,
            run_context=run_context,
            cancellation_event=cancellation_event,
            observability_context=observability_context,
        )

    if not config.keep_files:
        left_video_path.unlink(missing_ok=True)
        right_video_path.unlink(missing_ok=True)
    return mv_hevc_path


def create_direct_mv_hevc_file(
    disc_info: DiscInfo,
    output_folder: Path,
    mvc_video: Path,
    crop_params: str,
    bitrate_mbps: int | None,
    *,
    run_context: RunContext | None = None,
    cancellation_event: threading.Event | None = None,
    observability_context: ObservabilityContext | None = None,
) -> Path:
    mv_hevc_path = output_folder / f"{disc_info.name}_MV-HEVC.mov"
    if config.start_stage.value <= Stage.CREATE_LEFT_RIGHT_FILES.value:
        if bitrate_mbps is None:
            raise ValueError("Direct MV-HEVC encoding requires a resolved bitrate.")
        if not can_use_native_mvc_splitter(disc_info):
            raise RuntimeError(explain_native_mvc_unavailable(disc_info))
        normalizer_command = generate_direct_mv_hevc_normalizer_command(disc_info, crop_params)
        encoder_command = generate_direct_mv_hevc_encoder_command(mv_hevc_path, bitrate_mbps)
        run_direct_mv_hevc_encoding(
            mvc_video,
            mv_hevc_path,
            normalizer_command,
            encoder_command,
            run_context=run_context,
            cancellation_event=cancellation_event,
            observability_context=observability_context,
        )
        if not config.keep_files and not should_stream_mvc_from_container(mvc_video):
            mvc_video.unlink(missing_ok=True)
    return mv_hevc_path


def create_av1_stereo_file(
    input_path: Path,
    output_folder: Path,
    disc_info: DiscInfo,
    *,
    run_context: RunContext | None = None,
    cancellation_event: threading.Event | None = None,
    observability_context: ObservabilityContext | None = None,
) -> Path:
    output_path = output_folder / f"{disc_info.name}_AV1-Stereo.mp4"
    if config.start_stage.value <= Stage.COMBINE_TO_MV_HEVC.value:
        add_av1_stereo_metadata(
            input_path,
            output_path,
            run_context=run_context,
            cancellation_event=cancellation_event,
            observability_context=observability_context,
        )
    if not config.keep_files:
        input_path.unlink(missing_ok=True)
    return output_path


def create_upscaled_file(
    input_path: Path,
    *,
    run_context: RunContext | None = None,
    cancellation_event: threading.Event | None = None,
    observability_context: ObservabilityContext | None = None,
) -> Path:
    if config.fx_upscale:
        if config.start_stage.value <= Stage.UPSCALE_VIDEO.value:
            upscale_file(
                input_path,
                run_context=run_context,
                cancellation_event=cancellation_event,
                observability_context=observability_context,
            )

        upscaled_path = input_path.with_stem(f"{input_path.stem} Upscaled")
        if not upscaled_path.exists():
            raise RuntimeError("Upscaled file not found.")

        return upscaled_path
    return input_path
