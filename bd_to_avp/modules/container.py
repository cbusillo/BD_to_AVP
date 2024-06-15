from pathlib import Path

import ffmpeg
from babelfish import Language

from bd_to_avp.modules.config import Stage, config
from bd_to_avp.modules.sub import extract_subtitle_to_srt
from bd_to_avp.modules.command import run_command, run_ffmpeg_print_errors
from bd_to_avp.modules.util import sorted_files_by_creation_filtered_on_suffix


def extract_mvc_and_audio(
    input_path: Path,
    video_output_path: Path,
    audio_output_path: Path,
) -> None:

    stream = ffmpeg.input(str(input_path))

    video_stream = ffmpeg.output(stream["v:0"], f"file:{video_output_path}", c="copy", bsf="h264_mp4toannexb")
    audio_stream = ffmpeg.output(stream["a"], f"file:{audio_output_path}", c="pcm_s24le")

    output_message = "ffmpeg to extract video, audio, and subtitles from MKV"
    run_ffmpeg_print_errors([video_stream, audio_stream], output_message, overwrite_output=True)


def create_muxed_file(
    audio_path: Path,
    mv_hevc_path: Path,
    output_folder: Path,
    disc_name: str,
) -> Path:
    muxed_path = output_folder / f"{disc_name}{config.FINAL_FILE_TAG}.mov"
    if config.start_stage.value <= Stage.CREATE_FINAL_FILE.value:
        mux_video_audio_subs(mv_hevc_path, audio_path, muxed_path, output_folder)

    if not config.keep_files:
        mv_hevc_path.unlink(missing_ok=True)
        audio_path.unlink(missing_ok=True)
    return muxed_path


def create_mvc_audio_and_subtitle_files(
    disc_name: str,
    mkv_output_path: Path | None,
    output_folder: Path,
) -> tuple[Path, Path]:
    video_output_path = output_folder / f"{disc_name}_mvc.h264"
    audio_output_path = output_folder / f"{disc_name}_audio_PCM.mov"

    if config.start_stage.value <= Stage.EXTRACT_MVC_AUDIO_AND_SUB.value and mkv_output_path:
        if not config.skip_subtitles:
            extract_subtitle_to_srt(mkv_output_path, output_folder)

        extract_mvc_and_audio(
            mkv_output_path,
            video_output_path,
            audio_output_path,
        )

    if not config.keep_files and mkv_output_path and config.source_path != mkv_output_path:
        mkv_output_path.unlink(missing_ok=True)

    return audio_output_path, video_output_path


def mux_video_audio_subs(mv_hevc_path: Path, audio_path: Path, muxed_path: Path, output_folder: Path) -> None:

    command = [
        config.MP4BOX_PATH,
        "-new",
        "-lang",
        "eng",
        "-add",
        mv_hevc_path,
        "-add",
        audio_path,
    ]

    for sub_file in sorted_files_by_creation_filtered_on_suffix(output_folder, ".srt"):
        language_code = sub_file.stem.split(".")[-1]
        language_name = Language.fromalpha2(language_code).name

        subtitle_options = f"hdlr=sbtl:group=2:lang={language_code}:name={language_name} Subtitles:tx3g"
        if ".forced" in sub_file.stem:
            subtitle_options += ":forced_track=1:default_track=1"

        command += [
            "-add",
            f"{sub_file}:{subtitle_options}",
        ]

    command += [muxed_path]
    run_command(command, "Mux video, audio, and subtitles.")
