from __future__ import annotations

import json
import os
import subprocess
import threading

from dataclasses import dataclass
from enum import StrEnum
from typing import Callable, Mapping

from bd_to_avp.modules.command import run_process_capture
from bd_to_avp.modules.config import Stage, config
from bd_to_avp.modules.video_mode import VideoMode
from bd_to_avp.observability import ObservabilityContext
from bd_to_avp.process_runner import ProcessCancelled, ProcessExecutionError, ProcessRunnerError
from bd_to_avp.runtime import RunContext
from bd_to_avp.worker.protocol import BitrateMode, BitrateOptions, EncodingOptions, JobOptions, VideoRouteIntent

AUTOMATIC_DIRECT_BITRATE_MBPS = 40
AUTOMATIC_GENERATED_EYE_BITRATE_MBPS = 20
AUTOMATIC_GENERATED_MERGE_QUALITY = 75
DIRECT_CAPABILITY_TIMEOUT_SECONDS = 15


class VideoRouteKind(StrEnum):
    DIRECT_MV_HEVC = "direct_mv_hevc"
    GENERATED_MV_HEVC = "generated_mv_hevc"
    AV1 = "av1"
    EXISTING_ARTIFACT = "existing_artifact"


@dataclass(frozen=True)
class DirectMVHEVCCapability:
    supported: bool
    reason: str


@dataclass(frozen=True)
class ResolvedVideoRoute:
    intent: VideoRouteIntent
    selected: VideoRouteKind
    reason: str
    output_mode: VideoMode
    direct_bitrate_mbps: int | None = None
    generated_eye_bitrate_mbps: int | None = None
    generated_merge_quality: int | None = None
    av1_crf: int | None = None
    fallback_reason: str | None = None

    def report(self) -> dict[str, object]:
        report: dict[str, object] = {
            "intent": self.intent.value,
            "selected": self.selected.value,
            "reason": self.reason,
        }
        if self.direct_bitrate_mbps is not None:
            report["bitrate_mbps"] = self.direct_bitrate_mbps
        if self.generated_eye_bitrate_mbps is not None:
            report["eye_bitrate_mbps"] = self.generated_eye_bitrate_mbps
        if self.generated_merge_quality is not None:
            report["merge_quality"] = self.generated_merge_quality
        if self.av1_crf is not None:
            report["crf"] = self.av1_crf
        if self.fallback_reason is not None:
            report["fallback_reason"] = self.fallback_reason
            report["fallback_timing"] = "pre_input"
        return report


class VideoRoutePreflightError(RuntimeError):
    pass


CapabilityProbe = Callable[[], DirectMVHEVCCapability]


def resolve_video_route(
    encoding: EncodingOptions,
    job: JobOptions,
    *,
    capability_probe: CapabilityProbe | None = None,
    run_context: RunContext | None = None,
    cancellation_event: threading.Event | None = None,
    observability_context: ObservabilityContext | None = None,
) -> ResolvedVideoRoute:
    video = encoding.video
    start_stage = Stage.get_stage(job.start_stage)

    if start_stage.value > Stage.COMBINE_TO_MV_HEVC.value:
        return ResolvedVideoRoute(
            intent=video.route_intent,
            selected=VideoRouteKind.EXISTING_ARTIFACT,
            reason="resume_uses_existing_video_artifact",
            output_mode=video.mode,
        )
    if video.route_intent is VideoRouteIntent.EXISTING_ARTIFACT:
        raise VideoRoutePreflightError("The existing video artifact route requires a start stage after stage 5.")

    if video.mode is VideoMode.AV1_SBS:
        if video.av1_crf is None:
            raise VideoRoutePreflightError("AV1 encoding requires an active CRF value.")
        return ResolvedVideoRoute(
            intent=video.route_intent,
            selected=VideoRouteKind.AV1,
            reason="av1_output_requested",
            output_mode=video.mode,
            av1_crf=video.av1_crf,
        )

    if video.route_intent is VideoRouteIntent.GENERATED:
        return _generated_route(encoding, reason="generated_route_requested")

    generated_reason = _generated_constraint_reason(encoding, job, start_stage)
    if generated_reason is not None:
        return _generated_route(encoding, reason=generated_reason, use_requested_settings=False)

    if video.route_intent is not VideoRouteIntent.AUTOMATIC:
        raise VideoRoutePreflightError(
            f"MV-HEVC stage {start_stage.value} cannot use route intent {video.route_intent.value!r}."
        )
    if video.direct_bitrate is None:
        raise VideoRoutePreflightError("Automatic MV-HEVC routing requires an active direct bitrate policy.")

    probe = capability_probe or (
        lambda: probe_direct_mv_hevc_capability(
            run_context=run_context,
            cancellation_event=cancellation_event,
            observability_context=observability_context,
        )
    )
    capability = probe()
    if not capability.supported:
        return _generated_route(
            encoding,
            reason="direct_capability_unavailable",
            fallback_reason=capability.reason,
            use_requested_settings=False,
        )

    return ResolvedVideoRoute(
        intent=video.route_intent,
        selected=VideoRouteKind.DIRECT_MV_HEVC,
        reason="direct_eligible",
        output_mode=video.mode,
        direct_bitrate_mbps=_resolve_bitrate(video.direct_bitrate, AUTOMATIC_DIRECT_BITRATE_MBPS),
    )


def legacy_video_route() -> ResolvedVideoRoute:
    if config.start_stage.value > Stage.COMBINE_TO_MV_HEVC.value:
        return ResolvedVideoRoute(
            intent=VideoRouteIntent.EXISTING_ARTIFACT,
            selected=VideoRouteKind.EXISTING_ARTIFACT,
            reason="legacy_resume_uses_existing_video_artifact",
            output_mode=config.video_mode,
        )
    if config.video_mode is VideoMode.AV1_SBS:
        return ResolvedVideoRoute(
            intent=VideoRouteIntent.ENCODE,
            selected=VideoRouteKind.AV1,
            reason="legacy_av1_route",
            output_mode=config.video_mode,
            av1_crf=config.av1_crf,
        )
    return ResolvedVideoRoute(
        intent=VideoRouteIntent.GENERATED,
        selected=VideoRouteKind.GENERATED_MV_HEVC,
        reason="legacy_generated_route",
        output_mode=config.video_mode,
        generated_eye_bitrate_mbps=config.left_right_bitrate,
        generated_merge_quality=config.mv_hevc_quality,
    )


def probe_direct_mv_hevc_capability(
    *,
    run_context: RunContext | None = None,
    cancellation_event: threading.Event | None = None,
    observability_context: ObservabilityContext | None = None,
) -> DirectMVHEVCCapability:
    helper_path = config.MV_HEVC_ENCODER_PATH
    if not helper_path.is_file():
        return DirectMVHEVCCapability(False, "helper_missing")
    if not os.access(helper_path, os.X_OK):
        return DirectMVHEVCCapability(False, "helper_not_executable")

    try:
        result = run_process_capture(
            [helper_path, "--capability-probe"],
            "Probe direct MV-HEVC capability",
            tool_id="mv_hevc_encoder",
            run_context=run_context,
            cancellation_event=cancellation_event,
            observability_context=observability_context,
            timeout_seconds=DIRECT_CAPABILITY_TIMEOUT_SECONDS,
            show_command=False,
        )
        return _parse_capability_payload(result.stdout.text(), returncode=result.returncode)
    except ProcessCancelled:
        raise
    except ProcessExecutionError as error:
        if error.returncode == 2:
            return _parse_capability_payload(error.stdout_snapshot.text(), returncode=error.returncode)
        raise VideoRoutePreflightError(
            f"The direct MV-HEVC capability probe failed with exit code {error.returncode}."
        ) from error
    except (OSError, subprocess.SubprocessError, ProcessRunnerError) as error:
        raise VideoRoutePreflightError("The direct MV-HEVC capability probe could not be completed.") from error


def _parse_capability_payload(payload: str, *, returncode: int) -> DirectMVHEVCCapability:
    try:
        raw = json.loads(payload)
    except json.JSONDecodeError as error:
        raise VideoRoutePreflightError("The direct MV-HEVC capability probe returned invalid JSON.") from error
    if not isinstance(raw, Mapping) or set(raw) != {"schema_version", "stereo_mv_hevc_encode_supported"}:
        raise VideoRoutePreflightError("The direct MV-HEVC capability probe returned an invalid contract.")
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
        raise VideoRoutePreflightError("The direct MV-HEVC capability probe returned an invalid contract.")
    if type(raw["stereo_mv_hevc_encode_supported"]) is not bool:
        raise VideoRoutePreflightError("The direct MV-HEVC capability probe returned an invalid contract.")

    supported = raw["stereo_mv_hevc_encode_supported"]
    if supported and returncode != 0:
        raise VideoRoutePreflightError("The direct MV-HEVC capability probe contradicted its exit status.")
    if not supported and returncode != 2:
        raise VideoRoutePreflightError("The direct MV-HEVC capability probe contradicted its exit status.")
    return DirectMVHEVCCapability(
        supported=supported,
        reason="direct_capability_supported" if supported else "stereo_mv_hevc_encode_unavailable",
    )


def _generated_constraint_reason(
    encoding: EncodingOptions,
    job: JobOptions,
    start_stage: Stage,
) -> str | None:
    if start_stage.value >= Stage.CREATE_LEFT_RIGHT_FILES.value:
        return "restart_stage_requires_generated_artifacts"
    if job.keep_files:
        return "reusable_intermediates_requested"
    if job.software_encoder:
        return "software_encoder_requested"
    if encoding.upscale.enabled:
        return "upscale_requires_generated_artifacts"
    if not 1 <= encoding.fov <= 180:
        return "field_of_view_requires_generated_route"
    return None


def _generated_route(
    encoding: EncodingOptions,
    *,
    reason: str,
    fallback_reason: str | None = None,
    use_requested_settings: bool = True,
) -> ResolvedVideoRoute:
    video = encoding.video
    if use_requested_settings:
        if video.generated_eye_bitrate is None or video.generated_merge_quality is None:
            raise VideoRoutePreflightError("Generated MV-HEVC routing requires active generated settings.")
        eye_bitrate = _resolve_bitrate(video.generated_eye_bitrate, AUTOMATIC_GENERATED_EYE_BITRATE_MBPS)
        merge_quality = video.generated_merge_quality
    else:
        eye_bitrate = AUTOMATIC_GENERATED_EYE_BITRATE_MBPS
        merge_quality = AUTOMATIC_GENERATED_MERGE_QUALITY
    return ResolvedVideoRoute(
        intent=video.route_intent,
        selected=VideoRouteKind.GENERATED_MV_HEVC,
        reason=reason,
        output_mode=video.mode,
        generated_eye_bitrate_mbps=eye_bitrate,
        generated_merge_quality=merge_quality,
        fallback_reason=fallback_reason,
    )


def _resolve_bitrate(bitrate: BitrateOptions, automatic_mbps: int) -> int:
    if bitrate.mode is BitrateMode.AUTOMATIC:
        return automatic_mbps
    if bitrate.mbps is None:
        raise VideoRoutePreflightError("Custom bitrate mode requires an Mbps value.")
    return bitrate.mbps
