from pathlib import Path
from typing import Any

import ffmpeg

from bd_to_avp.modules.config import (
    Stage,
    config,
    is_audio_m4a_preparation_enabled,
    is_direct_mvc_stream_enabled,
)
from bd_to_avp.modules.command import run_command, run_ffmpeg_print_errors
from bd_to_avp.modules.languages import language_name, normalize_source_language
from bd_to_avp.modules.util import sorted_files_by_creation_filtered_on_suffix


def extract_mvc_and_audio(
    input_path: Path,
    video_output_path: Path | None,
    audio_output_path: Path | None,
) -> None:
    stream = ffmpeg.input(str(input_path))

    output_streams = []
    if video_output_path:
        output_streams.append(
            ffmpeg.output(stream["v:0"], f"file:{video_output_path}", c="copy", bsf="h264_mp4toannexb")
        )

    if audio_output_path:
        output_streams.append(ffmpeg.output(stream["a"], f"file:{audio_output_path}", c="pcm_s24le"))

    if output_streams:
        output_message = "ffmpeg to extract MVC video and audio from source"
        run_ffmpeg_print_errors(output_streams, output_message, overwrite_output=True)


def create_muxed_file(
    audio_path: Path,
    mv_hevc_path: Path,
    output_folder: Path,
    disc_name: str,
) -> Path:
    muxed_path = output_folder / f"{disc_name}{config.FINAL_FILE_TAG}.mov"
    if config.start_stage.value <= Stage.CREATE_FINAL_FILE.value:
        mux_video_audio_subs(mv_hevc_path, audio_path, muxed_path, output_folder)
    return muxed_path


def create_mvc_and_audio(
    disc_name: str,
    mkv_output_path: Path,
    output_folder: Path,
) -> tuple[Path, Path]:
    video_output_path = output_folder / f"{disc_name}_mvc.h264"
    audio_output_path = output_folder / f"{disc_name}_audio_PCM.mov"
    m4a_audio_preparation = is_audio_m4a_preparation_enabled()
    direct_mvc_stream = is_direct_mvc_stream_enabled()

    if config.start_stage.value <= Stage.EXTRACT_MVC_AND_AUDIO.value:
        extract_mvc_and_audio(
            mkv_output_path,
            None if direct_mvc_stream else video_output_path,
            None if m4a_audio_preparation else audio_output_path,
        )

    return (
        mkv_output_path if m4a_audio_preparation else audio_output_path,
        mkv_output_path if direct_mvc_stream else video_output_path,
    )


def mux_video_audio_subs(mv_hevc_path: Path, audio_path: Path, muxed_path: Path, output_folder: Path) -> None:
    audio_streams = get_audio_stream_data(audio_path)
    has_declared_audio_default = any(
        int(stream.get("disposition", {}).get("default", 0) or 0) == 1 for stream in audio_streams
    )
    output_track_index = 1
    command = [
        config.MP4BOX_PATH,
        "-new",
        "-add",
        # QuickTime and AVP seeking depend on a useful sync sample table. MP4Box can
        # collapse imported MV-HEVC tracks to one sync sample unless this is forced.
        f"{mv_hevc_path}:forcesync",
    ]
    output_track_index += 1
    for audio_position, stream in enumerate(audio_streams):
        index = stream["index"] + 1
        language_code, audio_language_name = normalize_track_language(stream.get("tags", {}).get("language"))
        channel_layout = stream.get("channel_layout", "unknown")
        tags = stream.get("tags", {})
        title = tags.get("title") or tags.get("name")
        default_disposition = int(stream.get("disposition", {}).get("default", 0) or 0) == 1

        audio_track_options = f":lang={language_code}:group=1:alternate_group=1"

        if default_disposition:
            audio_track_options += ":enabled"
        elif has_declared_audio_default or audio_position > 0:
            audio_track_options += ":disable"
        track_name = title if isinstance(title, str) and title else f"{audio_language_name} {channel_layout} Audio"

        command += [
            "-add",
            f"{audio_path}#{index}{audio_track_options}",
            "-udta",
            f"{output_track_index}:type=name:str='{track_name}'",
        ]
        output_track_index += 1

    for sub_file in sorted_files_by_creation_filtered_on_suffix(output_folder, ".srt"):
        language_code = normalize_source_language(sub_file.stem.split(".")[-1])
        subtitle_language_name = language_name(language_code)

        subtitle_options = f":hdlr=sbtl:lang={language_code}:group=2:name={subtitle_language_name} Subtitles:tx3g"
        if ".forced." in sub_file.stem:
            subtitle_options += ":txtflags=0xC0000000"
            subtitle_language_name += " Forced"

        command += [
            "-add",
            f"{sub_file}#1{subtitle_options}",
            "-udta",
            f"{output_track_index}:type=name:str='{subtitle_language_name} Subtitles'",
        ]
        output_track_index += 1

    command += [muxed_path]
    run_command(command, "mux video, audio, and subtitles.")


def normalize_track_language(language_code: object) -> tuple[str, str]:
    canonical_code = normalize_source_language(language_code)
    return canonical_code, language_name(canonical_code)


def get_audio_stream_data(file_path: Path) -> list[dict[str, Any]]:
    probe = ffmpeg.probe(str(file_path), cmd=config.FFPROBE_PATH.as_posix())
    if not probe or "streams" not in probe:
        return []
    audio_streams = [stream for stream in probe["streams"] if stream["codec_type"] == "audio"]
    return audio_streams
