from __future__ import annotations

import threading

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import ffmpeg

from bd_to_avp.modules.aac_layout_policy import (
    AAC_COPY_LAYOUT_CHANNELS as AAC_COPY_LAYOUT_CHANNELS,
    AacLayoutAction,
    AacLayoutDecision,
    AacLayoutPolicyError,
    resolve_aac_layout_policy,
)
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


@dataclass(frozen=True, slots=True)
class PlannedAudioLayout:
    stream_index: int
    codec_name: str
    decision: AacLayoutDecision


AAC_COPY_CODECS = frozenset({"aac"})
EXPLICITLY_UNQUALIFIED_CODECS = frozenset({"ac3", "eac3", "ac-3", "e-ac-3"})
AAC_COPY_PROFILES = frozenset({"lc", "he-aac", "he-aacv2", "mpeg-4 aac lc", "aac lc"})
AAC_COPY_SAMPLE_RATES = frozenset({44_100, 48_000})


def transcode_audio(
    input_path: Path,
    transcoded_audio_path: Path,
    bitrate: int,
    audio_selector: str = "a",
    *,
    selection: AudioSelection | None = None,
    activity: AudioActivityReporter | None = None,
    run_context: RunContext | None = None,
    cancellation_event: threading.Event | None = None,
    observability_context: ObservabilityContext | None = None,
) -> None:
    audio_input = ffmpeg.input(str(input_path))
    selected_streams = (
        [selected_stream.stream for selected_stream in selection.streams]
        if selection is not None
        else audio_streams_for_selector(
            input_path,
            audio_selector,
            run_context=run_context,
            cancellation_event=cancellation_event,
            observability_context=observability_context,
        )
    )
    layout_plan = plan_aac_layouts(selected_streams)
    enforce_aac_layout_policy(layout_plan, activity)
    selected_inputs = (
        [audio_input[selected_stream.selector] for selected_stream in selection.streams]
        if selection is not None
        else [audio_input[audio_selector]]
    )
    metadata_options = audio_handler_metadata_options(
        input_path,
        audio_selector,
        selected_streams=selected_streams,
        run_context=run_context,
        cancellation_event=cancellation_event,
        observability_context=observability_context,
    )
    source_audio_positions = audio_positions_for_output(
        selection,
        audio_selector=audio_selector,
        output_count=len(selected_streams),
    )
    audio_transcoded = ffmpeg.output(
        *selected_inputs,
        str(f"file:{transcoded_audio_path}"),
        acodec="aac",
        audio_bitrate=f"{bitrate}k",
        map_metadata=0,
        **aac_layout_options(layout_plan),
        **audio_stream_metadata_map_options(source_audio_positions),
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


def audio_streams_for_selector(
    input_path: Path,
    audio_selector: str,
    *,
    run_context: RunContext | None = None,
    cancellation_event: threading.Event | None = None,
    observability_context: ObservabilityContext | None = None,
) -> list[dict[str, Any]]:
    streams = get_audio_stream_data(
        input_path,
        run_context=run_context,
        cancellation_event=cancellation_event,
        observability_context=observability_context,
    )
    if audio_selector == "a":
        return streams
    stream_type, separator, stream_index = audio_selector.partition(":")
    if stream_type != "a" or not separator or not stream_index.isdigit():
        return []
    selected_index = int(stream_index)
    return streams[selected_index : selected_index + 1]


def plan_aac_layouts(streams: list[dict[str, Any]]) -> list[PlannedAudioLayout]:
    plans: list[PlannedAudioLayout] = []
    for output_index, stream in enumerate(streams):
        stream_index = parse_optional_int(stream.get("index"))
        codec_name = str(stream.get("codec_name") or "unknown").strip().lower() or "unknown"
        plans.append(
            PlannedAudioLayout(
                stream_index=stream_index if stream_index is not None else output_index,
                codec_name=codec_name,
                decision=resolve_aac_layout_policy(stream.get("channel_layout"), stream.get("channels")),
            )
        )
    return plans


def aac_layout_options(layout_plan: list[PlannedAudioLayout]) -> dict[str, str]:
    options: dict[str, str] = {}
    for output_index, planned_layout in enumerate(layout_plan):
        decision = planned_layout.decision
        if decision.target_layout is not None:
            options[f"channel_layout:a:{output_index}"] = decision.target_layout
        if decision.pan_filter is not None:
            options[f"filter:a:{output_index}"] = decision.pan_filter
    return options


def audio_positions_for_output(
    selection: AudioSelection | None,
    *,
    audio_selector: str,
    output_count: int,
) -> list[int]:
    if selection is not None:
        return [selected_stream.audio_position for selected_stream in selection.streams]
    if audio_selector == "a":
        return list(range(output_count))
    stream_type, separator, stream_index = audio_selector.partition(":")
    if stream_type == "a" and separator and stream_index.isdigit() and output_count == 1:
        return [int(stream_index)]
    return []


def audio_stream_metadata_map_options(source_audio_positions: list[int]) -> dict[str, str]:
    return {
        f"map_metadata:s:a:{output_index}": f"0:s:a:{source_audio_position}"
        for output_index, source_audio_position in enumerate(source_audio_positions)
    }


def enforce_aac_layout_policy(
    layout_plan: list[PlannedAudioLayout],
    activity: AudioActivityReporter | None,
) -> None:
    if not layout_plan:
        message = "Apple-compatible AAC cannot be created because the source does not contain an audio stream."
        emit_aac_layout_policy_warning(message, [], activity, rejected=True)
        raise AacLayoutPolicyError(message)

    rejected = [plan for plan in layout_plan if plan.decision.action is AacLayoutAction.FAIL]
    if rejected:
        first_rejection = rejected[0]
        decision = first_rejection.decision
        source_layout = decision.source_layout or "missing"
        channel_count = decision.source_channel_count if decision.source_channel_count is not None else "missing"
        message = (
            "Apple-compatible AAC cannot be created for audio stream "
            f"{first_rejection.stream_index}: layout {source_layout} with {channel_count} channels "
            f"is not qualified ({decision.reason})."
        )
        emit_aac_layout_policy_warning(message, layout_plan, activity, rejected=True)
        raise AacLayoutPolicyError(message)

    changed = [plan for plan in layout_plan if plan.decision.action in {AacLayoutAction.REMAP, AacLayoutAction.DOWNMIX}]
    if not changed:
        return

    transformations = ", ".join(
        f"stream {plan.stream_index} {plan.decision.source_layout} to {plan.decision.target_layout} "
        f"({plan.decision.action.value})"
        for plan in changed
    )
    message = f"Applying the qualified Apple-compatible AAC layout policy: {transformations}."
    emit_aac_layout_policy_warning(message, layout_plan, activity, rejected=False)


def emit_aac_layout_policy_warning(
    message: str,
    layout_plan: list[PlannedAudioLayout],
    activity: AudioActivityReporter | None,
    *,
    rejected: bool,
) -> None:
    if activity is None:
        cli_message(message)
        return
    activity.warning(
        message,
        stage="transcode_audio",
        code="audio_layout_policy_rejected" if rejected else "audio_layout_policy_applied",
        audio_mode=config.audio_mode.value,
        action="stop_conversion" if rejected else "normalize_aac_layouts",
        layout_decisions=[audio_layout_decision_fields(plan) for plan in layout_plan],
    )


def audio_layout_decision_fields(plan: PlannedAudioLayout) -> dict[str, object]:
    decision = plan.decision
    return {
        "stream_index": plan.stream_index,
        "codec": plan.codec_name,
        "action": decision.action.value,
        "source_layout": decision.source_layout,
        "source_channels": decision.source_channel_count,
        "target_layout": decision.target_layout,
        "target_channels": decision.target_channel_count,
        "policy_id": decision.policy.id if decision.policy is not None else None,
        "reason": decision.reason,
    }


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
    source_audio_positions = (
        [selected_stream.audio_position for selected_stream in selection.streams] if selection is not None else []
    )
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
        **audio_stream_metadata_map_options(source_audio_positions),
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
                        activity=activity,
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
                    activity=activity,
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
        validate_resumed_prepared_audio(
            prepared_audio_path,
            activity,
            run_context=run_context,
            cancellation_event=cancellation_event,
            observability_context=observability_context,
        )
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


def validate_resumed_prepared_audio(
    prepared_audio_path: Path,
    activity: AudioActivityReporter | None,
    *,
    run_context: RunContext | None = None,
    cancellation_event: threading.Event | None = None,
    observability_context: ObservabilityContext | None = None,
) -> None:
    qualifications = qualify_selected_audio_streams(
        prepared_audio_path,
        run_context=run_context,
        cancellation_event=cancellation_event,
        observability_context=observability_context,
    )
    if qualifications and all(qualification.qualified for qualification in qualifications):
        return

    message = (
        "The prepared AAC artifact is not qualified for Apple playback. "
        "Restart from Prepare Audio to regenerate it with the current layout policy."
    )
    if activity is None:
        cli_message(message)
    else:
        activity.warning(
            message,
            stage="transcode_audio",
            code="audio_layout_policy_rejected",
            audio_mode=config.audio_mode.value,
            action="restart_prepare_audio",
            prepared_audio_path=str(prepared_audio_path),
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
                for qualification in qualifications
                if not qualification.qualified
            ],
        )
    raise AacLayoutPolicyError(message)


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
    layout_decision = resolve_aac_layout_policy(channel_layout, channels)
    if layout_decision.action is AacLayoutAction.FAIL:
        return audio_qualification_result(
            stream_index,
            codec_name,
            normalized_profile,
            sample_rate,
            channels,
            channel_layout,
            False,
            layout_decision.reason,
        )
    if layout_decision.action is not AacLayoutAction.PRESERVE:
        return audio_qualification_result(
            stream_index,
            codec_name,
            normalized_profile,
            sample_rate,
            channels,
            channel_layout,
            False,
            f"channel_layout_requires_{layout_decision.action.value}",
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
