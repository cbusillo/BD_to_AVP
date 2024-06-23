from pathlib import Path
from typing import Any

import ffmpeg
from babelfish import Language

from bd_to_avp.modules.config import Stage, config
from bd_to_avp.modules.command import run_command, run_ffmpeg_print_errors
from bd_to_avp.modules.util import sorted_files_by_creation_filtered_on_suffix


def extract_mvc_and_audio(
    input_path: Path,
    video_output_path: Path,
    audio_output_path: Path,
) -> None:

    stream = ffmpeg.input(str(input_path))

    video_stream = ffmpeg.output(stream["v:0"], f"file:{video_output_path}", c="copy", bsf="h264_mp4toannexb")
    audio_track = ":0" if config.remove_extra_languages else ""
    audio_stream = ffmpeg.output(stream[f"a{audio_track}"], f"file:{audio_output_path}", c="pcm_s24le")

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


def create_mvc_and_audio(
    disc_name: str,
    mkv_output_path: Path,
    output_folder: Path,
) -> tuple[Path, Path]:
    video_output_path = output_folder / f"{disc_name}_mvc.h264"
    audio_output_path = output_folder / f"{disc_name}_audio_PCM.mov"

    if config.start_stage.value <= Stage.EXTRACT_MVC_AND_AUDIO.value:
        extract_mvc_and_audio(
            mkv_output_path,
            video_output_path,
            audio_output_path,
        )

    # if not config.keep_files and mkv_output_path and config.source_path != mkv_output_path:
    #     mkv_output_path.unlink(missing_ok=True)

    return audio_output_path, video_output_path


def mux_video_audio_subs(mv_hevc_path: Path, audio_path: Path, muxed_path: Path, output_folder: Path) -> None:
    audio_streams = get_audio_stream_data(audio_path)
    output_track_index = 1
    command = [
        config.MP4BOX_PATH,
        "-new",
        "-add",
        mv_hevc_path,
    ]
    output_track_index += 1
    for stream in audio_streams:
        index = stream["index"] + 1
        language_code_alpha3b = stream.get("tags", {}).get("language", "und")
        language_name = Language.fromietf(language_code_alpha3b).name
        channel_layout = stream.get("channel_layout", "unknown")

        audio_track_options = f":lang={language_code_alpha3b}:group=1:alternate_group=1"

        if index > 1:
            audio_track_options += ":disable"

        command += [
            "-add",
            f"{audio_path}#{index}{audio_track_options}",
            "-udta",
            f"{output_track_index}:type=name:str='{language_name} {channel_layout} Audio'",
        ]
        output_track_index += 1

    for sub_file in sorted_files_by_creation_filtered_on_suffix(output_folder, ".srt"):
        language_code_alpha2 = sub_file.stem.split(".")[-1]
        language_code_alpha3 = Language.fromalpha2(language_code_alpha2).alpha3
        language_name = Language.fromalpha2(language_code_alpha2).name

        subtitle_options = f":hdlr=sbtl:lang={language_code_alpha3}:group=2:name={language_name} Subtitles:tx3g"
        if ".forced" in sub_file.stem:
            subtitle_options += ":txtflags=0xC0000000"
            language_name += " Forced"

        command += [
            "-add",
            f"{sub_file}#1{subtitle_options}",
            "-udta",
            f"{output_track_index}:type=name:str='{language_name} Subtitles'",
        ]
        output_track_index += 1

    command += [muxed_path]
    run_command(command, "mux video, audio, and subtitles.")


def get_audio_stream_data(file_path: Path) -> list[dict[str, Any]]:
    probe = ffmpeg.probe(str(file_path))
    if not probe or "streams" not in probe:
        return []
    audio_streams = [stream for stream in probe["streams"] if stream["codec_type"] == "audio"]
    return audio_streams
