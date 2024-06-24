import os
import shutil
import subprocess

from bd_to_avp.modules.audio import create_transcoded_audio_file
from bd_to_avp.modules.config import config
from bd_to_avp.modules.container import create_muxed_file, create_mvc_and_audio
from bd_to_avp.modules.disc import create_mkv_file, get_disc_and_mvc_video_info
from bd_to_avp.modules.file import (
    file_exists_normalized,
    move_file_to_output_root_folder,
    prepare_output_folder_for_source,
    remove_folder_if_exists,
)
from bd_to_avp.modules.sub import create_srt_from_mkv
from bd_to_avp.modules.video import (
    create_left_right_files,
    create_mv_hevc_file,
    detect_crop_parameters,
    create_upscaled_file,
)


def process() -> None:
    if config.source_folder_path:
        for source in config.source_folder_path.rglob("*"):
            if not source.is_file() or source.suffix.lower() not in config.IMAGE_EXTENSIONS + config.MTS_EXTENSIONS + [
                ".mkv"
            ]:
                continue
            config.source_path = source
            try:
                process_each()
            except (ValueError, FileExistsError, subprocess.CalledProcessError):
                continue

        config.source_path = None

    else:
        process_each()


def process_each() -> None:
    print(f"\nProcessing {config.source_path}")
    disc_info = get_disc_and_mvc_video_info()
    output_folder = prepare_output_folder_for_source(disc_info.name)

    tmp_folder = output_folder / "tmp"
    shutil.rmtree(tmp_folder, ignore_errors=True)

    tmp_folder.mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = tmp_folder.as_posix()

    print(f"Using temporary folder: {os.environ['TMPDIR']}")

    completed_path = config.output_root_path / f"{disc_info.name}{config.FINAL_FILE_TAG}.mov"
    if not config.overwrite and file_exists_normalized(completed_path):
        raise FileExistsError(f"Output file already exists for {disc_info.name}. Use --overwrite to replace.")

    mkv_output_path = create_mkv_file(output_folder, disc_info, config.language_code)
    crop_params = detect_crop_parameters(mkv_output_path)
    audio_output_path, video_output_path = create_mvc_and_audio(disc_info.name, mkv_output_path, output_folder)
    create_srt_from_mkv(mkv_output_path, output_folder)
    left_output_path, right_output_path = create_left_right_files(
        disc_info, output_folder, video_output_path, crop_params
    )
    mv_hevc_path = create_mv_hevc_file(left_output_path, right_output_path, output_folder, disc_info)
    mv_hevc_path = create_upscaled_file(mv_hevc_path)

    audio_output_path = create_transcoded_audio_file(audio_output_path, output_folder)
    muxed_output_path = create_muxed_file(
        audio_output_path,
        mv_hevc_path,
        output_folder,
        disc_info.name,
    )
    move_file_to_output_root_folder(muxed_output_path)

    if config.remove_original and config.source_path:
        if config.source_path.is_dir():
            remove_folder_if_exists(config.source_path)
        else:
            config.source_path.unlink(missing_ok=True)


def start_process() -> None:
    process()


if __name__ == "__main__":
    start_process()
