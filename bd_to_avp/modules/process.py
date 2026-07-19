import os
import subprocess
from pathlib import Path
from threading import Event
from typing import Protocol

from wakepy.modes import keep

from bd_to_avp import preflight
from bd_to_avp.modules.audio import create_transcoded_audio_file
from bd_to_avp.modules.config import config, Stage
from bd_to_avp.modules.container import create_muxed_file, create_mvc_and_audio
from bd_to_avp.modules.disc import create_mkv_file, get_disc_and_mvc_video_info, MKVCreationError
from bd_to_avp.modules.file import (
    file_exists_normalized,
    move_file_to_output_root_folder,
    path_is_relative_to,
    prepare_output_folder_for_source,
    remove_output_folder_if_safe,
    remove_folder_if_exists,
)
from bd_to_avp.modules.preview import create_bounded_preview_source
from bd_to_avp.modules.sub import create_srt_from_mkv, SRTCreationError
from bd_to_avp.modules.video_mode import VideoMode
from bd_to_avp.modules.video import (
    create_av1_sbs_file,
    create_av1_stereo_file,
    create_left_right_files,
    create_mv_hevc_file,
    detect_crop_parameters,
    create_upscaled_file,
    get_video_color_depth,
)
from bd_to_avp.observability import ObservabilityContext, ObservabilityStage
from bd_to_avp.process_runner import ProcessCancelled
from bd_to_avp.runtime import RunContext


class ProcessingCancelled(Exception):
    pass


class BatchProcessingError(Exception):
    def __init__(self, source_path: Path, error: Exception, batch_sources: tuple[Path, ...]) -> None:
        super().__init__(str(error))
        self.source_path = source_path
        self.error = error
        self.batch_sources = batch_sources


class ActivityReporter(Protocol):
    def set_stage_plan(self, stages: tuple[str, ...]) -> None:
        raise NotImplementedError

    def stage_started(self, stage: str, message: str) -> None:
        raise NotImplementedError

    def stage_progress(self, completed_units: float, total_units: float) -> None:
        raise NotImplementedError

    def log(self, message: str, *, stage: str | None = None, **fields: object) -> None:
        raise NotImplementedError

    def warning(self, message: str, *, stage: str | None = None, **fields: object) -> None:
        raise NotImplementedError


def raise_if_cancelled(cancellation_event: Event | None) -> None:
    if cancellation_event is not None and cancellation_event.is_set():
        raise ProcessingCancelled("Processing was cancelled.")


def normalized_cancellation_event(
    cancellation_event: Event | None,
    run_context: RunContext | None,
) -> Event | None:
    if run_context is None:
        return cancellation_event
    context_event = run_context.cancellation.event
    if cancellation_event is not None and cancellation_event is not context_event:
        raise ValueError("run_context and cancellation_event must share the same event")
    return context_event


def stage_observability_context(stage: str) -> ObservabilityContext:
    return ObservabilityContext(stage=ObservabilityStage(stage))


def find_batch_sources(source_folder_path: Path) -> tuple[Path, ...]:
    supported_extensions = config.IMAGE_EXTENSIONS + config.MTS_EXTENSIONS + [".mkv"]
    return tuple(
        sorted(
            (
                source
                for source in source_folder_path.rglob("*")
                if source.is_file() and source.suffix.lower() in supported_extensions
            ),
            key=lambda source: source.as_posix().casefold(),
        )
    )


def conversion_stage_plan() -> tuple[str, ...]:
    stages = ["configure"]
    if config.start_stage is Stage.MOVE_FILES:
        return (*stages, "move_files")
    stages.extend(["preflight", "inspect_source"])
    if config.start_stage.value <= Stage.CREATE_MKV.value:
        stages.append("create_mkv")
    if config.preview_range is not None:
        stages.append("prepare_preview_range")
    stages.extend(
        [
            "probe_color",
            "detect_crop",
        ]
    )
    if config.start_stage.value <= Stage.EXTRACT_MVC_AND_AUDIO.value:
        stages.append("extract_mvc_and_audio")
    if not config.skip_subtitles and config.start_stage.value <= Stage.EXTRACT_SUBTITLES.value:
        stages.append("extract_subtitles")
    if config.start_stage.value <= Stage.CREATE_LEFT_RIGHT_FILES.value:
        stages.append("encode_av1_stereo" if config.video_mode is VideoMode.AV1_SBS else "create_left_right_files")
    if config.start_stage.value <= Stage.COMBINE_TO_MV_HEVC.value:
        stages.append("finalize_av1_stereo" if config.video_mode is VideoMode.AV1_SBS else "combine_to_mv_hevc")
    if config.fx_upscale and config.start_stage.value <= Stage.UPSCALE_VIDEO.value:
        stages.append("upscale_video")
    if config.audio_mode.prepares_m4a and config.start_stage.value <= Stage.TRANSCODE_AUDIO.value:
        stages.append("transcode_audio")
    if config.start_stage.value <= Stage.CREATE_FINAL_FILE.value:
        stages.append("create_final_file")
    stages.append("move_files")
    return tuple(stages)


def process(
    gui_start_stage: Stage,
    cancellation_event: Event | None = None,
    *,
    selected_title_id: str | None = None,
    resume_source_path: Path | None = None,
    batch_start_stage: Stage | None = None,
    batch_sources: tuple[Path, ...] | None = None,
    activity: ActivityReporter | None = None,
    run_context: RunContext | None = None,
) -> Path | None:
    cancellation_event = normalized_cancellation_event(cancellation_event, run_context)
    raise_if_cancelled(cancellation_event)
    batch_start_stage = batch_start_stage or gui_start_stage
    waiting_for_resume = resume_source_path is not None
    final_output_path = None
    if config.source_folder_path:
        batch_sources = batch_sources if batch_sources is not None else find_batch_sources(config.source_folder_path)
        for source in batch_sources:
            raise_if_cancelled(cancellation_event)
            if not source.is_file():
                continue
            is_resume_source = waiting_for_resume and source == resume_source_path
            if waiting_for_resume and not is_resume_source:
                continue
            waiting_for_resume = False
            config.source_path = source
            config.start_stage = gui_start_stage if is_resume_source else batch_start_stage
            try:
                final_output_path = (
                    process_each(cancellation_event, activity=activity, run_context=run_context)
                    if activity
                    else process_each(cancellation_event, run_context=run_context)
                )
                raise_if_cancelled(cancellation_event)
            except preflight.DependencyPreflightError:
                raise
            except (MKVCreationError, SRTCreationError) as error:
                raise BatchProcessingError(source, error, batch_sources) from error
            except FileExistsError:
                continue
            except ProcessCancelled as error:
                raise ProcessingCancelled("Processing was cancelled.") from error
            except (RuntimeError, ValueError, subprocess.CalledProcessError):
                raise_if_cancelled(cancellation_event)
                if is_resume_source:
                    raise
                continue
            finally:
                config.source_path = None
                config.start_stage = batch_start_stage

        if waiting_for_resume:
            raise FileNotFoundError(f"Could not resume batch source: {resume_source_path}")

    else:
        if selected_title_id is None:
            final_output_path = (
                process_each(cancellation_event, activity=activity, run_context=run_context)
                if activity
                else process_each(cancellation_event, run_context=run_context)
            )
        else:
            final_output_path = (
                process_each(
                    cancellation_event,
                    activity=activity,
                    selected_title_id=selected_title_id,
                    run_context=run_context,
                )
                if activity
                else process_each(
                    cancellation_event,
                    selected_title_id=selected_title_id,
                    run_context=run_context,
                )
            )

    return final_output_path


def process_each(
    cancellation_event: Event | None = None,
    activity: ActivityReporter | None = None,
    *,
    selected_title_id: str | None = None,
    run_context: RunContext | None = None,
) -> Path:
    cancellation_event = normalized_cancellation_event(cancellation_event, run_context)
    raise_if_cancelled(cancellation_event)
    if config.video_mode is VideoMode.AV1_SBS and config.fx_upscale:
        raise ValueError("AV1 stereo export does not support AI FX upscale.")
    if config.video_mode is VideoMode.AV1_SBS and config.resolution:
        raise ValueError("AV1 stereo export always preserves full source resolution per eye.")
    print(f"\nProcessing {config.source_path or config.source_str}")
    if config.start_stage is Stage.MOVE_FILES:
        muxed_output_path = completed_mux_path_for_move()
        completed_path = config.output_root_path / muxed_output_path.name
        if not config.overwrite and file_exists_normalized(completed_path):
            raise FileExistsError(
                f"Output file already exists for {muxed_output_path.stem}. Use --overwrite to replace."
            )
        return move_completed_conversion(
            muxed_output_path,
            config.output_root_path / "temp_files",
            cancellation_event,
            activity,
        )
    if activity:
        activity.stage_started("preflight", "Checking required conversion tools")
    preflight.verify_runtime_ready(
        run_context=run_context,
        cancellation_event=cancellation_event,
        observability_context=stage_observability_context("preflight"),
    )
    raise_if_cancelled(cancellation_event)
    if activity:
        activity.stage_started("inspect_source", "Reading video metadata")
    disc_info = get_disc_and_mvc_video_info(
        selected_title_id,
        run_context=run_context,
        cancellation_event=cancellation_event,
        observability_context=stage_observability_context("inspect_source"),
    )
    raise_if_cancelled(cancellation_event)
    if activity:
        activity.log("Source metadata loaded", stage="inspect_source", name=disc_info.name)
    output_folder = prepare_output_folder_for_source(disc_info.name)

    tmp_folder = config.output_root_path / "temp_files"

    tmp_folder.mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = tmp_folder.as_posix()
    if not tmp_folder.exists():
        raise RuntimeError(f"Failed to create temporary folder: {tmp_folder}")

    print(f"Using temporary folder: {os.environ['TMPDIR']}")
    if activity:
        activity.log("Temporary workspace ready", stage="inspect_source", path=os.environ["TMPDIR"])

    completed_path = config.output_root_path / f"{disc_info.name}{config.final_file_tag}.mov"
    if not config.overwrite and file_exists_normalized(completed_path):
        raise FileExistsError(f"Output file already exists for {disc_info.name}. Use --overwrite to replace.")

    raise_if_cancelled(cancellation_event)
    if activity and config.start_stage.value <= Stage.CREATE_MKV.value:
        activity.stage_started("create_mkv", "Preparing source video")
    mkv_output_path = create_mkv_file(
        output_folder,
        disc_info,
        progress_callback=activity.stage_progress if activity else None,
        run_context=run_context,
        cancellation_event=cancellation_event,
        observability_context=stage_observability_context("create_mkv"),
    )
    if config.preview_range is not None:
        raise_if_cancelled(cancellation_event)
        if activity:
            activity.stage_started("prepare_preview_range", "Preparing selected preview range")
        mkv_output_path, aligned_preview_range = create_bounded_preview_source(
            mkv_output_path,
            output_folder,
            config.preview_range,
            run_context=run_context,
            cancellation_event=cancellation_event,
            observability_context=stage_observability_context("prepare_preview_range"),
        )
        config.preview_range = aligned_preview_range
    raise_if_cancelled(cancellation_event)
    if activity:
        activity.stage_started("probe_color", "Reading video color depth")
    disc_info.color_depth = get_video_color_depth(
        mkv_output_path,
        run_context=run_context,
        cancellation_event=cancellation_event,
        observability_context=stage_observability_context("probe_color"),
    )
    raise_if_cancelled(cancellation_event)
    if activity:
        activity.stage_started("detect_crop", "Checking frame crop parameters")
    crop_start_seconds = (
        min(600, max(0, int(config.preview_range.duration_seconds / 2))) if config.preview_range is not None else 600
    )
    crop_params = detect_crop_parameters(
        mkv_output_path,
        start_seconds=crop_start_seconds,
        run_context=run_context,
        cancellation_event=cancellation_event,
        observability_context=stage_observability_context("detect_crop"),
    )
    raise_if_cancelled(cancellation_event)
    if activity and config.start_stage.value <= Stage.EXTRACT_MVC_AND_AUDIO.value:
        activity.stage_started("extract_mvc_and_audio", "Extracting MVC video and audio")
    audio_output_path, video_output_path = create_mvc_and_audio(
        disc_info.name,
        mkv_output_path,
        output_folder,
        run_context=run_context,
        cancellation_event=cancellation_event,
        observability_context=stage_observability_context("extract_mvc_and_audio"),
    )
    raise_if_cancelled(cancellation_event)
    if activity and not config.skip_subtitles and config.start_stage.value <= Stage.EXTRACT_SUBTITLES.value:
        activity.stage_started("extract_subtitles", "Extracting subtitles")
    create_srt_from_mkv(
        mkv_output_path,
        output_folder,
        (lambda message: activity.warning(message, stage="extract_subtitles") if activity else None),
        run_context=run_context,
        cancellation_event=cancellation_event,
        observability_context=stage_observability_context("extract_subtitles"),
    )
    raise_if_cancelled(cancellation_event)
    if config.video_mode is VideoMode.AV1_SBS:
        if activity and config.start_stage.value <= Stage.CREATE_LEFT_RIGHT_FILES.value:
            activity.stage_started("encode_av1_stereo", "Encoding side-by-side AV1 stereo video")
        av1_sbs_path = create_av1_sbs_file(
            disc_info,
            output_folder,
            video_output_path,
            crop_params,
            run_context=run_context,
            cancellation_event=cancellation_event,
            observability_context=stage_observability_context("encode_av1_stereo"),
        )
        raise_if_cancelled(cancellation_event)
        if activity and config.start_stage.value <= Stage.COMBINE_TO_MV_HEVC.value:
            activity.stage_started("finalize_av1_stereo", "Adding Apple stereo metadata to AV1 video")
        video_path = create_av1_stereo_file(
            av1_sbs_path,
            output_folder,
            disc_info,
            run_context=run_context,
            cancellation_event=cancellation_event,
            observability_context=stage_observability_context("finalize_av1_stereo"),
        )
    else:
        if activity and config.start_stage.value <= Stage.CREATE_LEFT_RIGHT_FILES.value:
            activity.stage_started("create_left_right_files", "Creating left and right eye video")
        left_output_path, right_output_path = create_left_right_files(
            disc_info,
            output_folder,
            video_output_path,
            crop_params,
            run_context=run_context,
            cancellation_event=cancellation_event,
            observability_context=stage_observability_context("create_left_right_files"),
        )
        raise_if_cancelled(cancellation_event)
        if activity and config.start_stage.value <= Stage.COMBINE_TO_MV_HEVC.value:
            activity.stage_started("combine_to_mv_hevc", "Combining stereo video into MV-HEVC")
        video_path = create_mv_hevc_file(
            left_output_path,
            right_output_path,
            output_folder,
            disc_info,
            run_context=run_context,
            cancellation_event=cancellation_event,
            observability_context=stage_observability_context("combine_to_mv_hevc"),
        )
    raise_if_cancelled(cancellation_event)
    if activity and config.fx_upscale and config.start_stage.value <= Stage.UPSCALE_VIDEO.value:
        activity.stage_started("upscale_video", "Upscaling video")
    video_path = create_upscaled_file(
        video_path,
        run_context=run_context,
        cancellation_event=cancellation_event,
        observability_context=stage_observability_context("upscale_video"),
    )

    raise_if_cancelled(cancellation_event)
    if activity and config.audio_mode.prepares_m4a and config.start_stage.value <= Stage.TRANSCODE_AUDIO.value:
        activity.stage_started("transcode_audio", "Prepare Audio")
    audio_output_path = create_transcoded_audio_file(
        audio_output_path,
        output_folder,
        activity,
        run_context=run_context,
        cancellation_event=cancellation_event,
        observability_context=stage_observability_context("transcode_audio"),
    )
    raise_if_cancelled(cancellation_event)
    if activity and config.start_stage.value <= Stage.CREATE_FINAL_FILE.value:
        activity.stage_started("create_final_file", "Muxing final stereo video")
    muxed_output_path = create_muxed_file(
        audio_output_path,
        video_path,
        output_folder,
        disc_info.name,
        run_context=run_context,
        cancellation_event=cancellation_event,
        observability_context=stage_observability_context("create_final_file"),
    )

    return move_completed_conversion(muxed_output_path, tmp_folder, cancellation_event, activity)


def completed_mux_path_for_move() -> Path:
    output_root = config.output_root_path
    source_path = config.source_path
    if source_path is not None:
        source_name = source_path.stem
        source_candidate = output_root / source_name / f"{source_name}{config.final_file_tag}.mov"
        if source_candidate.is_file():
            return source_candidate

    candidates = sorted(
        candidate
        for output_folder in output_root.iterdir()
        if output_folder.is_dir()
        for candidate in [output_folder / f"{output_folder.name}{config.final_file_tag}.mov"]
        if candidate.is_file()
    )
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise FileNotFoundError(f"No completed movie is ready to move under {output_root}.")

    candidate_names = ", ".join(candidate.relative_to(output_root).as_posix() for candidate in candidates)
    raise RuntimeError(
        "Multiple completed movies are ready to move. Select the matching direct-file source or isolate its output "
        f"folder before resuming: {candidate_names}"
    )


def move_completed_conversion(
    muxed_output_path: Path,
    tmp_folder: Path,
    cancellation_event: Event | None,
    activity: ActivityReporter | None,
) -> Path:
    raise_if_cancelled(cancellation_event)
    if activity:
        activity.stage_started("move_files", "Moving completed video")
    final_output_path = move_file_to_output_root_folder(muxed_output_path)

    raise_if_cancelled(cancellation_event)
    if not config.keep_files:
        remove_output_folder_if_safe(tmp_folder)

    raise_if_cancelled(cancellation_event)
    if config.remove_original:
        remove_original_source(final_output_path)

    return final_output_path


def remove_original_source(completed_path: Path) -> bool:
    source_path = config.source_path
    if not source_path:
        return False
    if source_path.is_dir():
        if path_is_relative_to(completed_path, source_path):
            print(f"Refusing to remove source directory containing final output: {source_path}")
            return False
        remove_folder_if_exists(source_path)
    else:
        source_path.unlink(missing_ok=True)
    return True


def start_process(
    gui_start_stage: Stage | None = None,
    cancellation_event: Event | None = None,
    *,
    selected_title_id: str | None = None,
    resume_source_path: Path | None = None,
    batch_start_stage: Stage | None = None,
    batch_sources: tuple[Path, ...] | None = None,
    activity: ActivityReporter | None = None,
    run_context: RunContext | None = None,
) -> Path | None:
    cancellation_event = normalized_cancellation_event(cancellation_event, run_context)
    gui_start_stage = gui_start_stage or config.start_stage
    try:
        if config.keep_awake:
            with keep.running():
                return process(
                    gui_start_stage,
                    cancellation_event,
                    selected_title_id=selected_title_id,
                    resume_source_path=resume_source_path,
                    batch_start_stage=batch_start_stage,
                    batch_sources=batch_sources,
                    activity=activity,
                    run_context=run_context,
                )
        return process(
            gui_start_stage,
            cancellation_event,
            selected_title_id=selected_title_id,
            resume_source_path=resume_source_path,
            batch_start_stage=batch_start_stage,
            batch_sources=batch_sources,
            activity=activity,
            run_context=run_context,
        )
    except ProcessCancelled as error:
        raise ProcessingCancelled("Processing was cancelled.") from error


if __name__ == "__main__":
    start_process()
