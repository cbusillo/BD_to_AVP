from __future__ import annotations

import threading

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import ffmpeg

from bd_to_avp.modules.audio_mode import AudioMode
from bd_to_avp.modules.audio_selection import (
    AudioSelection,
    emit_audio_selection_warning,
    persist_audio_selection,
    select_audio_streams,
)
from bd_to_avp.modules.command import run_ffmpeg_print_errors
from bd_to_avp.modules.config import Stage, config
from bd_to_avp.modules.container import audio_handler_metadata_options, get_audio_stream_data
from bd_to_avp.observability import ObservabilityContext
from bd_to_avp.presentation import cli_message
from bd_to_avp.runtime import RunContext


class AudioActivityReporter(Protocol):
    def warning(self, message: str, *, stage: str | None = None, **fields: object) -> None:
        raise NotImplementedError


@dataclass(frozen=True)
class AudioStreamQualification:
    index: int
    codec_name: str
    profile: str | None
    qualified: bool
    reason: str | None = None
    sample_rate: int | None = None
    channels: int | None = None
    channel_layout: str | None = None


AAC_COPY_CODECS = frozenset({"aac"})
EXPLICITLY_UNQUALIFIED_CODECS = frozenset({"ac3", "eac3", "ac-3", "e-ac-3"})
AAC_COPY_PROFILES = frozenset({"lc", "he-aac", "he-aacv2", "mpeg-4 aac lc", "aac lc"})
AAC_COPY_SAMPLE_RATES = frozenset({44_100, 48_000})
AAC_COPY_LAYOUT_CHANNELS = {
    "mono": 1,
    "stereo": 2,
    "5.1": 6,
    "5.1(side)": 6,
    "7.1": 8,
}


def transcode_audio(
    input_path: Path,
    transcoded_audio_path: Path,
    bitrate: int,
    audio_selector: str = "a",
    *,
    selection: AudioSelection | None = None,
    run_context: RunContext | None = None,
    cancellation_event: threading.Event | None = None,
    observability_context: ObservabilityContext | None = None,
) -> None:
    audio_input = ffmpeg.input(str(input_path))
    selected_inputs = (
        [audio_input[selected_stream.selector] for selected_stream in selection.streams]
        if selection is not None
        else [audio_input[audio_selector]]
    )
    metadata_options = (
        audio_handler_metadata_options(
            input_path,
            audio_selector,
            selected_streams=[selected_stream.stream for selected_stream in selection.streams],
            run_context=run_context,
            cancellation_event=cancellation_event,
            observability_context=observability_context,
        )
        if selection is not None
        else audio_handler_metadata_options(
            input_path,
            audio_selector,
            run_context=run_context,
            cancellation_event=cancellation_event,
            observability_context=observability_context,
        )
    )
    audio_transcoded = ffmpeg.output(
        *selected_inputs,
        str(f"file:{transcoded_audio_path}"),
        acodec="aac",
        audio_bitrate=f"{bitrate}k",
        map_metadata=0,
        **metadata_options,
    )
    run_ffmpeg_print_errors(
        audio_transcoded,
        f"transcode audio to {bitrate}kbps",
        overwrite_output=True,
        run_context=run_context,
        cancellation_event=cancellation_event,
        observability_context=observability_context,
    )


def copy_audio(
    input_path: Path,
    copied_audio_path: Path,
    *,
    selection: AudioSelection | None = None,
    run_context: RunContext | None = None,
    cancellation_event: threading.Event | None = None,
    observability_context: ObservabilityContext | None = None,
) -> None:
    audio_input = ffmpeg.input(str(input_path))
    selected_inputs = (
        [audio_input[selected_stream.selector] for selected_stream in selection.streams]
        if selection is not None
        else [audio_input["a"]]
    )
    metadata_options = (
        audio_handler_metadata_options(
            input_path,
            selected_streams=[selected_stream.stream for selected_stream in selection.streams],
            run_context=run_context,
            cancellation_event=cancellation_event,
            observability_context=observability_context,
        )
        if selection is not None
        else audio_handler_metadata_options(
            input_path,
            run_context=run_context,
            cancellation_event=cancellation_event,
            observability_context=observability_context,
        )
    )
    copied_audio = ffmpeg.output(
        *selected_inputs,
        str(f"file:{copied_audio_path}"),
        acodec="copy",
        map_metadata=0,
        **metadata_options,
    )
    run_ffmpeg_print_errors(
        copied_audio,
        "copy AAC audio tracks",
        overwrite_output=True,
        run_context=run_context,
        cancellation_event=cancellation_event,
        observability_context=observability_context,
    )


def create_prepared_audio_file(
    original_audio_path: Path,
    output_folder: Path,
    activity: AudioActivityReporter | None = None,
    *,
    run_context: RunContext | None = None,
    cancellation_event: threading.Event | None = None,
    observability_context: ObservabilityContext | None = None,
) -> Path:
    mode = config.audio_mode
    if mode is AudioMode.PCM:
        return original_audio_path

    prepared_audio_path = output_folder / f"{output_folder.stem}_audio_AAC.m4a"
    legacy_transcoded_audio_path = output_folder / f"{output_folder.stem}_audio_AAC.mov"
    should_prepare = config.start_stage.value <= Stage.TRANSCODE_AUDIO.value

    if should_prepare:
        temporary_audio_path = prepared_audio_path.with_suffix(".part.m4a")
        try:
            selection = preferred_audio_selection(
                original_audio_path,
                activity,
                stage="transcode_audio",
                run_context=run_context,
                cancellation_event=cancellation_event,
                observability_context=observability_context,
            )
            if mode is AudioMode.AUTOMATIC:
                qualifications = qualify_selected_audio_streams(
                    original_audio_path,
                    selection=selection,
                    run_context=run_context,
                    cancellation_event=cancellation_event,
                    observability_context=observability_context,
                )
                if qualifications and all(qualification.qualified for qualification in qualifications):
                    copy_audio(
                        original_audio_path,
                        temporary_audio_path,
                        selection=selection,
                        run_context=run_context,
                        cancellation_event=cancellation_event,
                        observability_context=observability_context,
                    )
                else:
                    emit_automatic_fallback_warning(qualifications, activity)
                    transcode_audio(
                        original_audio_path,
                        temporary_audio_path,
                        config.audio_bitrate,
                        selection=selection,
                        run_context=run_context,
                        cancellation_event=cancellation_event,
                        observability_context=observability_context,
                    )
            else:
                transcode_audio(
                    original_audio_path,
                    temporary_audio_path,
                    config.audio_bitrate,
                    selection=selection,
                    run_context=run_context,
                    cancellation_event=cancellation_event,
                    observability_context=observability_context,
                )
            temporary_audio_path.replace(prepared_audio_path)
            persist_audio_selection(prepared_audio_path, selection)
        finally:
            temporary_audio_path.unlink(missing_ok=True)

        return prepared_audio_path

    if prepared_audio_path.exists():
        return prepared_audio_path
    if legacy_transcoded_audio_path.exists():
        raise FileNotFoundError(
            "Legacy AAC audio artifact found. Restart from Prepare Audio to regenerate a compatible M4A file: "
            f"{legacy_transcoded_audio_path}"
        )
    raise FileNotFoundError(f"Prepared audio artifact not found: {prepared_audio_path}")


def create_transcoded_audio_file(
    original_audio_path: Path,
    output_folder: Path,
    activity: AudioActivityReporter | None = None,
    *,
    run_context: RunContext | None = None,
    cancellation_event: threading.Event | None = None,
    observability_context: ObservabilityContext | None = None,
) -> Path:
    return create_prepared_audio_file(
        original_audio_path,
        output_folder,
        activity,
        run_context=run_context,
        cancellation_event=cancellation_event,
        observability_context=observability_context,
    )


def qualify_selected_audio_streams(
    input_path: Path,
    *,
    selection: AudioSelection | None = None,
    run_context: RunContext | None = None,
    cancellation_event: threading.Event | None = None,
    observability_context: ObservabilityContext | None = None,
) -> list[AudioStreamQualification]:
    if selection is not None:
        return [qualify_audio_stream(selected_stream.stream) for selected_stream in selection.streams]
    return [
        qualify_audio_stream(stream)
        for stream in get_audio_stream_data(
            input_path,
            run_context=run_context,
            cancellation_event=cancellation_event,
            observability_context=observability_context,
        )
    ]


def preferred_audio_selection(
    input_path: Path,
    activity: AudioActivityReporter | None,
    *,
    stage: str,
    run_context: RunContext | None = None,
    cancellation_event: threading.Event | None = None,
    observability_context: ObservabilityContext | None = None,
) -> AudioSelection | None:
    preferred_language = config.audio_preferred_language
    if preferred_language is None:
        return None
    selection = select_audio_streams(
        get_audio_stream_data(
            input_path,
            run_context=run_context,
            cancellation_event=cancellation_event,
            observability_context=observability_context,
        ),
        preferred_language,
    )
    emit_audio_selection_warning(selection, activity, stage=stage)
    return selection


def qualify_audio_stream(stream: dict[str, Any]) -> AudioStreamQualification:
    codec_name = str(stream.get("codec_name") or "").strip().lower()
    profile = stream.get("profile")
    normalized_profile = str(profile).strip().lower() if profile is not None else None
    index = parse_optional_int(stream.get("index"))
    sample_rate = parse_optional_int(stream.get("sample_rate"))
    channels = parse_optional_int(stream.get("channels"))
    raw_channel_layout = stream.get("channel_layout")
    channel_layout = str(raw_channel_layout).strip().lower() if raw_channel_layout is not None else None
    stream_index = index if index is not None else -1

    if codec_name in EXPLICITLY_UNQUALIFIED_CODECS:
        return audio_qualification_result(
            stream_index,
            codec_name,
            normalized_profile,
            sample_rate,
            channels,
            channel_layout,
            False,
            "codec_not_allowed",
        )
    if codec_name not in AAC_COPY_CODECS:
        return audio_qualification_result(
            stream_index,
            codec_name or "unknown",
            normalized_profile,
            sample_rate,
            channels,
            channel_layout,
            False,
            "codec_not_aac",
        )
    if normalized_profile is None:
        return audio_qualification_result(
            stream_index,
            codec_name,
            None,
            sample_rate,
            channels,
            channel_layout,
            False,
            "aac_profile_missing",
        )
    if normalized_profile not in AAC_COPY_PROFILES:
        return audio_qualification_result(
            stream_index,
            codec_name,
            normalized_profile,
            sample_rate,
            channels,
            channel_layout,
            False,
            "aac_profile_not_qualified",
        )
    if sample_rate is None:
        return audio_qualification_result(
            stream_index,
            codec_name,
            normalized_profile,
            None,
            channels,
            channel_layout,
            False,
            "sample_rate_missing",
        )
    if sample_rate not in AAC_COPY_SAMPLE_RATES:
        return audio_qualification_result(
            stream_index,
            codec_name,
            normalized_profile,
            sample_rate,
            channels,
            channel_layout,
            False,
            "sample_rate_not_qualified",
        )
    if channels is None:
        return audio_qualification_result(
            stream_index,
            codec_name,
            normalized_profile,
            sample_rate,
            None,
            channel_layout,
            False,
            "channel_count_missing",
        )
    if channel_layout is None or not channel_layout:
        return audio_qualification_result(
            stream_index,
            codec_name,
            normalized_profile,
            sample_rate,
            channels,
            None,
            False,
            "channel_layout_missing",
        )
    expected_channels = AAC_COPY_LAYOUT_CHANNELS.get(channel_layout)
    if expected_channels is None:
        return audio_qualification_result(
            stream_index,
            codec_name,
            normalized_profile,
            sample_rate,
            channels,
            channel_layout,
            False,
            "channel_layout_not_qualified",
        )
    if channels != expected_channels:
        return audio_qualification_result(
            stream_index,
            codec_name,
            normalized_profile,
            sample_rate,
            channels,
            channel_layout,
            False,
            "channel_layout_mismatch",
        )
    return audio_qualification_result(
        stream_index,
        codec_name,
        normalized_profile,
        sample_rate,
        channels,
        channel_layout,
        True,
    )


def parse_optional_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        return None
    try:
        return int(value)
    except ValueError:
        return None


def audio_qualification_result(
    index: int,
    codec_name: str,
    profile: str | None,
    sample_rate: int | None,
    channels: int | None,
    channel_layout: str | None,
    qualified: bool,
    reason: str | None = None,
) -> AudioStreamQualification:
    return AudioStreamQualification(
        index=index,
        codec_name=codec_name,
        profile=profile,
        qualified=qualified,
        reason=reason,
        sample_rate=sample_rate,
        channels=channels,
        channel_layout=channel_layout,
    )


def emit_automatic_fallback_warning(
    qualifications: list[AudioStreamQualification],
    activity: AudioActivityReporter | None,
) -> None:
    codecs = [qualification.codec_name for qualification in qualifications]
    unqualified = [qualification for qualification in qualifications if not qualification.qualified]
    message = "Automatic audio selected AAC conversion because one or more selected tracks are not qualified AAC."
    if activity is None:
        cli_message(message)
        return

    activity.warning(
        message,
        stage="transcode_audio",
        code="audio_automatic_fallback_to_aac",
        audio_mode=AudioMode.AUTOMATIC.value,
        action="convert_aac",
        source_codecs=codecs,
        unqualified_streams=[
            {
                "index": qualification.index,
                "codec": qualification.codec_name,
                "profile": qualification.profile,
                "sample_rate": qualification.sample_rate,
                "channels": qualification.channels,
                "channel_layout": qualification.channel_layout,
                "reason": qualification.reason,
            }
            for qualification in unqualified
        ],
    )
