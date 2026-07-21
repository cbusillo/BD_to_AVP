import threading

from pathlib import Path
from typing import Any, Mapping, Sequence

import ffmpeg

from bd_to_avp.modules.audio_mode import AudioMode
from bd_to_avp.modules.audio_selection import (
    AudioSelectionActivityReporter,
    emit_audio_selection_warning,
    emit_unrestorable_all_languages_warning,
    load_audio_selection_manifest,
    persist_audio_selection,
    select_audio_streams,
)
from bd_to_avp.modules.config import (
    Stage,
    config,
    is_audio_m4a_preparation_enabled,
    is_direct_mvc_stream_enabled,
)
from bd_to_avp.modules.command import run_ffmpeg_print_errors, run_ffprobe, run_process_capture
from bd_to_avp.modules.languages import language_name, normalize_source_language
from bd_to_avp.modules.util import sorted_files_by_creation_filtered_on_suffix
from bd_to_avp.modules.video_mode import VideoMode
from bd_to_avp.observability import ObservabilityContext
from bd_to_avp.process_runner import CaptureOverflowPolicy
from bd_to_avp.runtime import RunContext


AUDIO_CHANNEL_LAYOUT_NAMES = {
    1: "mono",
    2: "stereo",
}
GENERIC_AUDIO_HANDLER_NAMES = frozenset({"soundhandler"})
AUDIO_DISPOSITION_NAMES = (
    "default",
    "dub",
    "original",
    "comment",
    "lyrics",
    "karaoke",
    "forced",
    "hearing_impaired",
    "visual_impaired",
    "clean_effects",
    "attached_pic",
    "timed_thumbnails",
    "non_diegetic",
    "captions",
    "descriptions",
    "metadata",
    "dependent",
    "still_image",
)


def extract_mvc_and_audio(
    input_path: Path,
    video_output_path: Path | None,
    audio_output_path: Path | None,
    *,
    activity: AudioSelectionActivityReporter | None = None,
    run_context: RunContext | None = None,
    cancellation_event: threading.Event | None = None,
    observability_context: ObservabilityContext | None = None,
) -> None:
    stream = ffmpeg.input(str(input_path))
    audio_selection = None

    output_streams = []
    if video_output_path:
        output_streams.append(
            ffmpeg.output(stream["v:0"], f"file:{video_output_path}", c="copy", bsf="h264_mp4toannexb")
        )

    if audio_output_path:
        if config.audio_preferred_language is not None:
            audio_selection = select_audio_streams(
                get_audio_stream_data(
                    input_path,
                    run_context=run_context,
                    cancellation_event=cancellation_event,
                    observability_context=observability_context,
                ),
                config.audio_preferred_language,
            )
            emit_audio_selection_warning(audio_selection, activity, stage="extract_mvc_and_audio")
        audio_inputs = (
            [stream[selected_stream.selector] for selected_stream in audio_selection.streams]
            if audio_selection is not None
            else [stream["a"]]
        )
        metadata_options = (
            audio_handler_metadata_options(
                input_path,
                selected_streams=[selected_stream.stream for selected_stream in audio_selection.streams],
                run_context=run_context,
                cancellation_event=cancellation_event,
                observability_context=observability_context,
            )
            if audio_selection is not None
            else audio_handler_metadata_options(
                input_path,
                run_context=run_context,
                cancellation_event=cancellation_event,
                observability_context=observability_context,
            )
        )
        output_streams.append(
            ffmpeg.output(
                *audio_inputs,
                f"file:{audio_output_path}",
                c="pcm_s24le",
                **metadata_options,
            )
        )

    if output_streams:
        output_message = "ffmpeg to extract MVC video and audio from source"
        run_ffmpeg_print_errors(
            output_streams,
            output_message,
            overwrite_output=True,
            run_context=run_context,
            cancellation_event=cancellation_event,
            observability_context=observability_context,
        )
        if audio_output_path is not None:
            persist_audio_selection(audio_output_path, audio_selection)


def create_muxed_file(
    audio_path: Path,
    video_path: Path,
    output_folder: Path,
    disc_name: str,
    *,
    activity: AudioSelectionActivityReporter | None = None,
    run_context: RunContext | None = None,
    cancellation_event: threading.Event | None = None,
    observability_context: ObservabilityContext | None = None,
) -> Path:
    muxed_path = output_folder / f"{disc_name}{config.final_file_tag}.mov"
    if config.start_stage.value <= Stage.CREATE_FINAL_FILE.value:
        mux_video_audio_subs(
            video_path,
            audio_path,
            muxed_path,
            output_folder,
            activity=activity,
            run_context=run_context,
            cancellation_event=cancellation_event,
            observability_context=observability_context,
        )
    return muxed_path


def create_mvc_and_audio(
    disc_name: str,
    mkv_output_path: Path,
    output_folder: Path,
    *,
    activity: AudioSelectionActivityReporter | None = None,
    run_context: RunContext | None = None,
    cancellation_event: threading.Event | None = None,
    observability_context: ObservabilityContext | None = None,
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
            activity=activity,
            run_context=run_context,
            cancellation_event=cancellation_event,
            observability_context=observability_context,
        )

    return (
        mkv_output_path if m4a_audio_preparation else audio_output_path,
        mkv_output_path if direct_mvc_stream else video_output_path,
    )


def mux_video_audio_subs(
    video_path: Path,
    audio_path: Path,
    muxed_path: Path,
    output_folder: Path,
    *,
    activity: AudioSelectionActivityReporter | None = None,
    run_context: RunContext | None = None,
    cancellation_event: threading.Event | None = None,
    observability_context: ObservabilityContext | None = None,
) -> None:
    audio_streams = get_audio_stream_data(
        audio_path,
        run_context=run_context,
        cancellation_event=cancellation_event,
        observability_context=observability_context,
    )
    if config.audio_preferred_language is not None:
        selection = select_audio_streams(audio_streams, config.audio_preferred_language)
        audio_streams = [selected_stream.stream for selected_stream in selection.streams]
        if _audio_selection_not_prepared_in_current_run():
            emit_audio_selection_warning(selection, activity, stage="create_final_file")
    elif _audio_selection_not_prepared_in_current_run():
        manifest = load_audio_selection_manifest(audio_path)
        if manifest is not None and manifest.omitted_streams and manifest.selected_stream_count == len(audio_streams):
            emit_unrestorable_all_languages_warning(
                manifest,
                activity,
                stage="create_final_file",
                restart_stage=("Extract MVC and Audio" if config.audio_mode is AudioMode.PCM else "Prepare Audio"),
            )
    has_declared_audio_default = any(
        int(stream.get("disposition", {}).get("default", 0) or 0) == 1 for stream in audio_streams
    )
    output_track_index = 1
    video_import = f"{video_path}:forcesync" if config.video_mode is VideoMode.MV_HEVC else video_path
    command = [
        config.MP4BOX_PATH,
        "-new",
        "-add",
        # QuickTime and AVP seeking depend on a useful sync sample table. MP4Box can
        # collapse imported MV-HEVC tracks to one sync sample unless this is forced.
        video_import,
    ]
    output_track_index += 1
    for audio_position, stream in enumerate(audio_streams):
        index = stream["index"] + 1
        language_code, audio_language_name = normalize_track_language(stream.get("tags", {}).get("language"))
        channel_layout = audio_channel_layout_name(stream)
        title = audio_track_title(stream)
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
    run_process_capture(
        command,
        "mux video, audio, and subtitles.",
        tool_id="mp4box",
        run_context=run_context,
        cancellation_event=cancellation_event,
        observability_context=observability_context,
        capture_overflow=CaptureOverflowPolicy.TRUNCATE,
        show_spinner=True,
    )


def audio_channel_layout_name(stream: dict[str, Any]) -> str:
    channel_layout = stream.get("channel_layout")
    if isinstance(channel_layout, str) and channel_layout.strip():
        return channel_layout.strip()

    raw_channel_count = stream.get("channels")
    if raw_channel_count is None:
        return "unknown"
    try:
        channel_count = int(raw_channel_count)
    except (TypeError, ValueError):
        return "unknown"
    return AUDIO_CHANNEL_LAYOUT_NAMES.get(channel_count, f"{channel_count}-channel")


def audio_track_title(stream: dict[str, Any]) -> str | None:
    tags = stream.get("tags", {})
    if not isinstance(tags, dict):
        return None

    for key in ("title", "name", "handler_name"):
        title = tags.get(key)
        if not isinstance(title, str):
            continue
        normalized_title = title.strip()
        if not normalized_title:
            continue
        if key == "handler_name" and normalized_title.casefold() in GENERIC_AUDIO_HANDLER_NAMES:
            continue
        return normalized_title
    return None


def audio_handler_metadata_options(
    input_path: Path,
    audio_selector: str = "a",
    *,
    selected_streams: Sequence[dict[str, Any]] | None = None,
    run_context: RunContext | None = None,
    cancellation_event: threading.Event | None = None,
    observability_context: ObservabilityContext | None = None,
) -> dict[str, str]:
    if selected_streams is None:
        streams = get_audio_stream_data(
            input_path,
            run_context=run_context,
            cancellation_event=cancellation_event,
            observability_context=observability_context,
        )
        if audio_selector != "a":
            stream_type, separator, stream_index = audio_selector.partition(":")
            if stream_type != "a" or not separator or not stream_index.isdigit():
                return {}
            selected_index = int(stream_index)
            streams = streams[selected_index : selected_index + 1]
    else:
        streams = list(selected_streams)

    options: dict[str, str] = {}
    for output_index, stream in enumerate(streams):
        title = audio_track_title(stream)
        if title is not None:
            options[f"metadata:s:a:{output_index}"] = f"handler_name={title}"
        options[f"disposition:a:{output_index}"] = audio_disposition_value(stream)
    return options


def audio_disposition_value(stream: dict[str, Any]) -> str:
    disposition = stream.get("disposition")
    if not isinstance(disposition, Mapping):
        return "0"
    enabled = [name for name in AUDIO_DISPOSITION_NAMES if _disposition_enabled(disposition.get(name))]
    return "+".join(enabled) if enabled else "0"


def _disposition_enabled(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        try:
            return int(value) != 0
        except ValueError:
            return False
    return False


def _audio_selection_not_prepared_in_current_run() -> bool:
    if config.audio_mode is AudioMode.PCM:
        return config.start_stage.value > Stage.EXTRACT_MVC_AND_AUDIO.value
    return config.start_stage.value > Stage.TRANSCODE_AUDIO.value


def normalize_track_language(language_code: object) -> tuple[str, str]:
    canonical_code = normalize_source_language(language_code)
    return canonical_code, language_name(canonical_code)


def get_audio_stream_data(
    file_path: Path,
    *,
    run_context: RunContext | None = None,
    cancellation_event: threading.Event | None = None,
    observability_context: ObservabilityContext | None = None,
) -> list[dict[str, Any]]:
    probe = run_ffprobe(
        file_path,
        run_context=run_context,
        cancellation_event=cancellation_event,
        observability_context=observability_context,
    )
    if not probe or "streams" not in probe:
        return []
    audio_streams = [stream for stream in probe["streams"] if stream["codec_type"] == "audio"]
    return audio_streams
