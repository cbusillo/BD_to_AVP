import os
import subprocess
from pathlib import Path
from threading import Event

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
from bd_to_avp.modules.sub import create_srt_from_mkv, SRTCreationError
from bd_to_avp.modules.video import (
    create_left_right_files,
    create_mv_hevc_file,
    detect_crop_parameters,
    create_upscaled_file,
    get_video_color_depth,
)


class ProcessingCancelled(Exception):
    pass


class BatchProcessingError(Exception):
    def __init__(self, source_path: Path, error: Exception, batch_sources: tuple[Path, ...]) -> None:
        super().__init__(str(error))
        self.source_path = source_path
        self.error = error
        self.batch_sources = batch_sources


def raise_if_cancelled(cancellation_event: Event | None) -> None:
    if cancellation_event is not None and cancellation_event.is_set():
        raise ProcessingCancelled("Processing was cancelled.")


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


def process(
    gui_start_stage: Stage,
    cancellation_event: Event | None = None,
    *,
    resume_source_path: Path | None = None,
    batch_start_stage: Stage | None = None,
    batch_sources: tuple[Path, ...] | None = None,
) -> None:
    raise_if_cancelled(cancellation_event)
    batch_start_stage = batch_start_stage or gui_start_stage
    waiting_for_resume = resume_source_path is not None
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
                process_each(cancellation_event)
                raise_if_cancelled(cancellation_event)
            except preflight.DependencyPreflightError:
                raise
            except (MKVCreationError, SRTCreationError) as error:
                raise BatchProcessingError(source, error, batch_sources) from error
            except FileExistsError:
                continue
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
        process_each(cancellation_event)


def process_each(cancellation_event: Event | None = None) -> None:
    raise_if_cancelled(cancellation_event)
    print(f"\nProcessing {config.source_path}")
    preflight.verify_runtime_ready()
    raise_if_cancelled(cancellation_event)
    disc_info = get_disc_and_mvc_video_info()
    raise_if_cancelled(cancellation_event)
    output_folder = prepare_output_folder_for_source(disc_info.name)

    tmp_folder = config.output_root_path / "temp_files"

    tmp_folder.mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = tmp_folder.as_posix()
    if not tmp_folder.exists():
        raise RuntimeError(f"Failed to create temporary folder: {tmp_folder}")

    print(f"Using temporary folder: {os.environ['TMPDIR']}")

    completed_path = config.output_root_path / f"{disc_info.name}{config.FINAL_FILE_TAG}.mov"
    if not config.overwrite and file_exists_normalized(completed_path):
        raise FileExistsError(f"Output file already exists for {disc_info.name}. Use --overwrite to replace.")

    raise_if_cancelled(cancellation_event)
    mkv_output_path = create_mkv_file(output_folder, disc_info, config.language_code)
    raise_if_cancelled(cancellation_event)
    disc_info.color_depth = get_video_color_depth(mkv_output_path)
    raise_if_cancelled(cancellation_event)
    crop_params = detect_crop_parameters(mkv_output_path)
    raise_if_cancelled(cancellation_event)
    audio_output_path, video_output_path = create_mvc_and_audio(disc_info.name, mkv_output_path, output_folder)
    raise_if_cancelled(cancellation_event)
    create_srt_from_mkv(mkv_output_path, output_folder)
    raise_if_cancelled(cancellation_event)
    left_output_path, right_output_path = create_left_right_files(
        disc_info, output_folder, video_output_path, crop_params
    )
    raise_if_cancelled(cancellation_event)
    mv_hevc_path = create_mv_hevc_file(left_output_path, right_output_path, output_folder, disc_info)
    raise_if_cancelled(cancellation_event)
    mv_hevc_path = create_upscaled_file(mv_hevc_path)

    raise_if_cancelled(cancellation_event)
    audio_output_path = create_transcoded_audio_file(audio_output_path, output_folder)
    raise_if_cancelled(cancellation_event)
    muxed_output_path = create_muxed_file(
        audio_output_path,
        mv_hevc_path,
        output_folder,
        disc_info.name,
    )
    raise_if_cancelled(cancellation_event)
    move_file_to_output_root_folder(muxed_output_path)

    raise_if_cancelled(cancellation_event)
    if not config.keep_files:
        remove_output_folder_if_safe(tmp_folder)

    raise_if_cancelled(cancellation_event)
    if config.remove_original:
        remove_original_source(completed_path)


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
    resume_source_path: Path | None = None,
    batch_start_stage: Stage | None = None,
    batch_sources: tuple[Path, ...] | None = None,
) -> None:
    gui_start_stage = gui_start_stage or config.start_stage
    if config.keep_awake:
        with keep.running():
            process(
                gui_start_stage,
                cancellation_event,
                resume_source_path=resume_source_path,
                batch_start_stage=batch_start_stage,
                batch_sources=batch_sources,
            )
    else:
        process(
            gui_start_stage,
            cancellation_event,
            resume_source_path=resume_source_path,
            batch_start_stage=batch_start_stage,
            batch_sources=batch_sources,
        )


if __name__ == "__main__":
    start_process()
