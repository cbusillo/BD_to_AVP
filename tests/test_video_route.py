import json
import unittest

from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from unittest.mock import Mock, patch

from bd_to_avp.modules.audio_mode import AudioMode
from bd_to_avp.modules.video_mode import VideoMode
from bd_to_avp.modules.video_route import (
    AUTOMATIC_DIRECT_QUALITY,
    AUTOMATIC_DIRECT_UPSCALE_QUALITY,
    AUTOMATIC_GENERATED_EYE_BITRATE_MBPS,
    DirectUpscaleMode,
    DirectMVHEVCCapability,
    VideoRouteKind,
    VideoRoutePreflightError,
    probe_direct_mv_hevc_capability,
    resolve_video_route,
)
from bd_to_avp.modules.video_quality_defaults import AUTOMATIC_GENERATED_MERGE_QUALITY
from bd_to_avp.process_runner import ProcessExecutionError, ProcessOutputSnapshot
from bd_to_avp.worker.protocol import (
    AudioOptions,
    BitrateMode,
    BitrateOptions,
    EncodingOptions,
    GeneratedFallbackOptions,
    JobOptions,
    QualityIntentMode,
    QualityStep,
    SubtitleMode,
    SubtitleOptions,
    UpscaleOptions,
    VideoOptions,
    VideoQualityIntent,
    VideoRouteIntent,
)


def mv_hevc_encoding(
    *,
    intent: VideoRouteIntent = VideoRouteIntent.AUTOMATIC,
    direct_bitrate: BitrateOptions | None = None,
    generated_eye_bitrate: BitrateOptions | None = None,
    generated_merge_quality: int | None = None,
    fallback_eye_bitrate: BitrateOptions | None = None,
    fallback_merge_quality: int = 75,
    upscale: bool = False,
    fov: int = 90,
    resolution: str = "",
    crop_black_bars: bool = False,
) -> EncodingOptions:
    if intent is VideoRouteIntent.AUTOMATIC and direct_bitrate is None:
        direct_bitrate = BitrateOptions(BitrateMode.AUTOMATIC)
    generated_fallback = None
    if intent is VideoRouteIntent.AUTOMATIC:
        generated_fallback = GeneratedFallbackOptions(
            eye_bitrate=fallback_eye_bitrate or BitrateOptions(BitrateMode.AUTOMATIC),
            merge_quality=fallback_merge_quality,
        )
    if intent is VideoRouteIntent.GENERATED:
        generated_eye_bitrate = generated_eye_bitrate or BitrateOptions(BitrateMode.AUTOMATIC)
        generated_merge_quality = generated_merge_quality if generated_merge_quality is not None else 75
    uses_custom_quality = (
        (direct_bitrate is not None and direct_bitrate.mode is BitrateMode.CUSTOM)
        or (generated_eye_bitrate is not None and generated_eye_bitrate.mode is BitrateMode.CUSTOM)
        or generated_merge_quality not in {None, 75}
        or (
            generated_fallback is not None
            and (generated_fallback.eye_bitrate.mode is BitrateMode.CUSTOM or generated_fallback.merge_quality != 75)
        )
    )
    if intent is VideoRouteIntent.EXISTING_ARTIFACT:
        quality_intent = None
    elif uses_custom_quality:
        quality_intent = VideoQualityIntent(mode=QualityIntentMode.CUSTOM)
    else:
        quality_intent = VideoQualityIntent(
            mode=QualityIntentMode.LADDER,
            step=QualityStep.BALANCED,
            mapping_version=1,
        )
    return EncodingOptions(
        audio=AudioOptions(AudioMode.AUTOMATIC, 384, "eng"),
        video=VideoOptions(
            mode=VideoMode.MV_HEVC,
            route_intent=intent,
            quality_intent=quality_intent,
            direct_bitrate=direct_bitrate,
            generated_eye_bitrate=generated_eye_bitrate,
            generated_merge_quality=generated_merge_quality,
            generated_fallback=generated_fallback,
        ),
        upscale=UpscaleOptions(enabled=upscale, quality=75 if upscale else None),
        fov=fov,
        frame_rate="",
        resolution=resolution,
        crop_black_bars=crop_black_bars,
        swap_eyes=False,
        subtitles=SubtitleOptions(SubtitleMode.PREFERRED_PLUS_OTHERS, "eng"),
    )


def av1_encoding(*, existing: bool = False) -> EncodingOptions:
    return EncodingOptions(
        audio=AudioOptions(AudioMode.AUTOMATIC, 384, "eng"),
        video=VideoOptions(
            mode=VideoMode.AV1_SBS,
            route_intent=VideoRouteIntent.EXISTING_ARTIFACT if existing else VideoRouteIntent.ENCODE,
            quality_intent=None if existing else VideoQualityIntent(mode=QualityIntentMode.CUSTOM),
            av1_crf=None if existing else 31,
        ),
        upscale=UpscaleOptions(enabled=False),
        fov=90,
        frame_rate="",
        resolution="",
        crop_black_bars=False,
        swap_eyes=False,
        subtitles=SubtitleOptions(SubtitleMode.PREFERRED_PLUS_OTHERS, "eng"),
    )


def job_options(
    *,
    start_stage: int = 1,
    keep_files: bool = False,
    software_encoder: bool = False,
) -> JobOptions:
    return JobOptions(
        start_stage=start_stage,
        keep_files=keep_files,
        overwrite=False,
        remove_original=False,
        continue_on_error=False,
        software_encoder=software_encoder,
        output_commands=False,
        keep_awake=False,
    )


class VideoRouteResolverTests(unittest.TestCase):
    def test_selects_direct_with_content_adaptive_automatic_quality(self) -> None:
        route = resolve_video_route(
            mv_hevc_encoding(),
            job_options(),
            capability_probe=lambda: DirectMVHEVCCapability(True, "direct_capability_supported"),
        )

        self.assertEqual(route.selected, VideoRouteKind.DIRECT_MV_HEVC)
        self.assertIsNone(route.direct_bitrate_mbps)
        self.assertEqual(route.direct_quality, AUTOMATIC_DIRECT_QUALITY)
        self.assertEqual(route.report()["rate_control"], "quality")
        self.assertEqual(route.report()["quality"], AUTOMATIC_DIRECT_QUALITY)
        self.assertNotIn("bitrate_mbps", route.report())
        self.assertEqual(route.report()["reason"], "direct_eligible")
        self.assertEqual(
            route.report()["quality_intent"],
            {"mode": "ladder", "step": "balanced", "mapping_version": 1},
        )
        self.assertEqual(
            route.report()["requested"],
            {"route": "direct_mv_hevc", "rate_control": "quality", "quality": 0.7},
        )

    def test_selects_direct_custom_bitrate(self) -> None:
        route = resolve_video_route(
            mv_hevc_encoding(direct_bitrate=BitrateOptions(BitrateMode.CUSTOM, 37)),
            job_options(),
            capability_probe=lambda: DirectMVHEVCCapability(True, "direct_capability_supported"),
        )

        self.assertEqual(route.direct_bitrate_mbps, 37)
        self.assertIsNone(route.direct_quality)
        self.assertEqual(route.report()["rate_control"], "average_bitrate")
        self.assertEqual(route.report()["bitrate_mbps"], 37)
        self.assertNotIn("quality", route.report())

    def test_valid_unavailable_capability_falls_back_before_input(self) -> None:
        route = resolve_video_route(
            mv_hevc_encoding(direct_bitrate=BitrateOptions(BitrateMode.CUSTOM, 37)),
            job_options(),
            capability_probe=lambda: DirectMVHEVCCapability(False, "stereo_mv_hevc_encode_unavailable"),
        )

        self.assertEqual(route.selected, VideoRouteKind.GENERATED_MV_HEVC)
        self.assertEqual(route.generated_eye_bitrate_mbps, AUTOMATIC_GENERATED_EYE_BITRATE_MBPS)
        self.assertEqual(route.generated_merge_quality, AUTOMATIC_GENERATED_MERGE_QUALITY)
        self.assertEqual(route.report()["fallback_timing"], "pre_input")
        self.assertNotIn("bitrate_mbps", route.report())
        self.assertEqual(route.report()["quality_intent"], {"mode": "custom"})
        self.assertEqual(
            route.report()["requested"],
            {"route": "direct_mv_hevc", "rate_control": "average_bitrate", "bitrate_mbps": 37},
        )

    def test_custom_fallback_uses_retained_generated_settings(self) -> None:
        route = resolve_video_route(
            mv_hevc_encoding(
                direct_bitrate=BitrateOptions(BitrateMode.CUSTOM, 37),
                fallback_eye_bitrate=BitrateOptions(BitrateMode.CUSTOM, 42),
                fallback_merge_quality=88,
            ),
            job_options(),
            capability_probe=lambda: DirectMVHEVCCapability(False, "stereo_mv_hevc_encode_unavailable"),
        )

        self.assertEqual(route.generated_eye_bitrate_mbps, 42)
        self.assertEqual(route.generated_merge_quality, 88)
        self.assertEqual(route.report()["eye_bitrate_mbps"], 42)
        self.assertEqual(route.report()["merge_quality"], 88)
        self.assertEqual(route.report()["quality_intent"], {"mode": "custom"})
        self.assertEqual(route.report()["requested"]["bitrate_mbps"], 37)

    def test_generated_request_uses_only_generated_settings(self) -> None:
        probe = Mock()
        route = resolve_video_route(
            mv_hevc_encoding(
                intent=VideoRouteIntent.GENERATED,
                generated_eye_bitrate=BitrateOptions(BitrateMode.CUSTOM, 42),
                generated_merge_quality=88,
            ),
            job_options(),
            capability_probe=probe,
        )

        self.assertEqual(route.selected, VideoRouteKind.GENERATED_MV_HEVC)
        self.assertEqual(route.generated_eye_bitrate_mbps, 42)
        self.assertEqual(route.generated_merge_quality, 88)
        probe.assert_not_called()

    def test_reusable_intermediates_force_generated_without_probe(self) -> None:
        probe = Mock()
        route = resolve_video_route(
            mv_hevc_encoding(),
            job_options(keep_files=True),
            capability_probe=probe,
        )

        self.assertEqual(route.selected, VideoRouteKind.GENERATED_MV_HEVC)
        self.assertEqual(route.reason, "reusable_intermediates_requested")
        probe.assert_not_called()

    def test_software_encoder_forces_generated_without_probe(self) -> None:
        probe = Mock()
        route = resolve_video_route(
            mv_hevc_encoding(),
            job_options(software_encoder=True),
            capability_probe=probe,
        )

        self.assertEqual(route.selected, VideoRouteKind.GENERATED_MV_HEVC)
        self.assertEqual(route.reason, "software_encoder_requested")
        probe.assert_not_called()

    def test_upscale_selects_direct_metalfx_when_supported(self) -> None:
        route = resolve_video_route(
            mv_hevc_encoding(upscale=True),
            job_options(),
            capability_probe=lambda: DirectMVHEVCCapability(
                True,
                "direct_capability_supported",
                metalfx_2x_mv_hevc_supported=True,
            ),
        )

        self.assertEqual(route.selected, VideoRouteKind.DIRECT_MV_HEVC)
        self.assertEqual(route.reason, "direct_upscale_eligible")
        self.assertEqual(route.direct_upscale_mode, DirectUpscaleMode.METAL_FX)
        self.assertEqual(route.direct_quality, AUTOMATIC_DIRECT_UPSCALE_QUALITY)
        self.assertTrue(route.uses_in_process_upscale)
        self.assertEqual(route.report()["upscale_mode"], "metalfx")

    def test_upscale_capability_falls_back_before_input(self) -> None:
        route = resolve_video_route(
            mv_hevc_encoding(upscale=True),
            job_options(),
            capability_probe=lambda: DirectMVHEVCCapability(True, "direct_capability_supported"),
        )

        self.assertEqual(route.selected, VideoRouteKind.GENERATED_MV_HEVC)
        self.assertEqual(route.fallback_reason, "metalfx_2x_mv_hevc_unavailable")
        self.assertEqual(route.report()["fallback_timing"], "pre_input")
        self.assertEqual(route.report()["requested"]["upscale_mode"], "metalfx")
        self.assertEqual(route.report()["upscale_quality"], 75)

    def test_incompatible_upscale_geometry_forces_generated_without_probe(self) -> None:
        cases = (
            (mv_hevc_encoding(upscale=True, crop_black_bars=True), "upscale_crop_requires_generated_artifacts"),
            (
                mv_hevc_encoding(upscale=True, resolution="3840x2160"),
                "upscale_resolution_requires_generated_artifacts",
            ),
        )
        for encoding, reason in cases:
            with self.subTest(reason=reason):
                probe = Mock()
                route = resolve_video_route(encoding, job_options(), capability_probe=probe)
                self.assertEqual(route.selected, VideoRouteKind.GENERATED_MV_HEVC)
                self.assertEqual(route.reason, reason)
                probe.assert_not_called()

    def test_out_of_range_direct_fov_forces_generated_without_probe(self) -> None:
        for fov in (0, 181):
            with self.subTest(fov=fov):
                probe = Mock()
                route = resolve_video_route(
                    mv_hevc_encoding(fov=fov),
                    job_options(),
                    capability_probe=probe,
                )

                self.assertEqual(route.selected, VideoRouteKind.GENERATED_MV_HEVC)
                self.assertEqual(route.reason, "field_of_view_requires_generated_route")
                probe.assert_not_called()

    def test_stage_four_and_five_force_generated(self) -> None:
        for start_stage in (4, 5):
            with self.subTest(start_stage=start_stage):
                probe = Mock()
                route = resolve_video_route(
                    mv_hevc_encoding(),
                    job_options(start_stage=start_stage),
                    capability_probe=probe,
                )
                self.assertEqual(route.selected, VideoRouteKind.GENERATED_MV_HEVC)
                probe.assert_not_called()

    def test_later_restart_uses_existing_artifact(self) -> None:
        probe = Mock()
        route = resolve_video_route(
            mv_hevc_encoding(intent=VideoRouteIntent.EXISTING_ARTIFACT),
            job_options(start_stage=6),
            capability_probe=probe,
        )

        self.assertEqual(route.selected, VideoRouteKind.EXISTING_ARTIFACT)
        self.assertIsNone(route.quality_intent)
        self.assertIsNone(route.upscale_quality)
        probe.assert_not_called()

    def test_stage_six_existing_artifact_upscale_reports_active_quality(self) -> None:
        encoding = mv_hevc_encoding(intent=VideoRouteIntent.EXISTING_ARTIFACT, upscale=True)
        encoding = replace(
            encoding,
            video=replace(
                encoding.video,
                quality_intent=VideoQualityIntent(mode=QualityIntentMode.CUSTOM),
            ),
            upscale=UpscaleOptions(enabled=True, quality=66),
        )

        route = resolve_video_route(encoding, job_options(start_stage=6))

        self.assertEqual(route.selected, VideoRouteKind.EXISTING_ARTIFACT)
        self.assertEqual(route.quality_intent, VideoQualityIntent(mode=QualityIntentMode.CUSTOM))
        self.assertEqual(route.upscale_quality, 66)
        self.assertEqual(
            route.report(),
            {
                "intent": "existing_artifact",
                "selected": "existing_artifact",
                "reason": "resume_uses_existing_video_artifact",
                "quality_intent": {"mode": "custom"},
                "requested": {"route": "existing_artifact", "upscale_quality": 66},
                "upscale_quality": 66,
            },
        )

    def test_existing_artifact_intent_before_stage_six_is_rejected(self) -> None:
        with self.assertRaisesRegex(VideoRoutePreflightError, "requires a start stage after stage 5"):
            resolve_video_route(
                mv_hevc_encoding(intent=VideoRouteIntent.EXISTING_ARTIFACT),
                job_options(start_stage=5),
            )

    def test_av1_encode_and_restart_routes(self) -> None:
        encode_route = resolve_video_route(av1_encoding(), job_options())
        restart_route = resolve_video_route(av1_encoding(existing=True), job_options(start_stage=6))

        self.assertEqual(encode_route.selected, VideoRouteKind.AV1)
        self.assertEqual(encode_route.av1_crf, 31)
        self.assertEqual(restart_route.selected, VideoRouteKind.EXISTING_ARTIFACT)

    def test_resolved_route_is_immutable(self) -> None:
        route = resolve_video_route(
            mv_hevc_encoding(),
            job_options(),
            capability_probe=lambda: DirectMVHEVCCapability(True, "direct_capability_supported"),
        )

        with self.assertRaises(FrozenInstanceError):
            route.reason = "changed"  # type: ignore[misc]


class CapabilityProbeTests(unittest.TestCase):
    def test_missing_helper_is_valid_unavailable_capability(self) -> None:
        with patch("bd_to_avp.modules.video_route.config.MV_HEVC_ENCODER_PATH", Path("/missing/helper")):
            capability = probe_direct_mv_hevc_capability()

        self.assertFalse(capability.supported)
        self.assertEqual(capability.reason, "helper_missing")

    def test_valid_unsupported_exit_is_accepted(self) -> None:
        payload = json.dumps({"schema_version": 1, "stereo_mv_hevc_encode_supported": False})
        snapshot = ProcessOutputSnapshot(payload.encode(), payload.encode(), len(payload), len(payload), 0, 0)
        empty = ProcessOutputSnapshot(b"", b"", 0, 0, 0, 0)
        error = ProcessExecutionError(2, ["mv-hevc-encoder", "--capability-probe"], snapshot, empty)
        with (
            patch("bd_to_avp.modules.video_route.config.MV_HEVC_ENCODER_PATH", Path("/tools/mv-hevc-encoder")),
            patch.object(Path, "is_file", return_value=True),
            patch("bd_to_avp.modules.video_route.os.access", return_value=True),
            patch("bd_to_avp.modules.video_route.run_process_capture", side_effect=error),
        ):
            capability = probe_direct_mv_hevc_capability()

        self.assertFalse(capability.supported)
        self.assertEqual(capability.reason, "stereo_mv_hevc_encode_unavailable")

    def test_extended_capability_reports_metalfx_support(self) -> None:
        payload = json.dumps(
            {
                "metalfx_2x_mv_hevc_supported": True,
                "metalfx_spatial_scaling_supported": True,
                "pixel_transfer_2x_mv_hevc_supported": True,
                "schema_version": 1,
                "stereo_mv_hevc_encode_supported": True,
            }
        )
        result = Mock(returncode=0, stdout=Mock(text=Mock(return_value=payload)))
        with (
            patch("bd_to_avp.modules.video_route.config.MV_HEVC_ENCODER_PATH", Path("/tools/mv-hevc-encoder")),
            patch.object(Path, "is_file", return_value=True),
            patch("bd_to_avp.modules.video_route.os.access", return_value=True),
            patch("bd_to_avp.modules.video_route.run_process_capture", return_value=result),
        ):
            capability = probe_direct_mv_hevc_capability()

        self.assertTrue(capability.supported)
        self.assertTrue(capability.metalfx_2x_mv_hevc_supported)

    def test_legacy_capability_contract_does_not_enable_metalfx(self) -> None:
        payload = json.dumps({"schema_version": 1, "stereo_mv_hevc_encode_supported": True})
        result = Mock(returncode=0, stdout=Mock(text=Mock(return_value=payload)))
        with (
            patch("bd_to_avp.modules.video_route.config.MV_HEVC_ENCODER_PATH", Path("/tools/mv-hevc-encoder")),
            patch.object(Path, "is_file", return_value=True),
            patch("bd_to_avp.modules.video_route.os.access", return_value=True),
            patch("bd_to_avp.modules.video_route.run_process_capture", return_value=result),
        ):
            capability = probe_direct_mv_hevc_capability()

        self.assertTrue(capability.supported)
        self.assertFalse(capability.metalfx_2x_mv_hevc_supported)

    def test_malformed_probe_contract_is_not_a_fallback(self) -> None:
        payload = json.dumps({"schema_version": 1, "stereo_mv_hevc_encode_supported": "yes"})
        snapshot = ProcessOutputSnapshot(payload.encode(), payload.encode(), len(payload), len(payload), 0, 0)
        empty = ProcessOutputSnapshot(b"", b"", 0, 0, 0, 0)
        error = ProcessExecutionError(2, ["mv-hevc-encoder", "--capability-probe"], snapshot, empty)
        with (
            patch("bd_to_avp.modules.video_route.config.MV_HEVC_ENCODER_PATH", Path("/tools/mv-hevc-encoder")),
            patch.object(Path, "is_file", return_value=True),
            patch("bd_to_avp.modules.video_route.os.access", return_value=True),
            patch("bd_to_avp.modules.video_route.run_process_capture", side_effect=error),
            self.assertRaises(VideoRoutePreflightError),
        ):
            probe_direct_mv_hevc_capability()

    def test_probe_crash_is_not_a_fallback(self) -> None:
        empty = ProcessOutputSnapshot(b"", b"", 0, 0, 0, 0)
        error = ProcessExecutionError(1, ["mv-hevc-encoder", "--capability-probe"], empty, empty)
        with (
            patch("bd_to_avp.modules.video_route.config.MV_HEVC_ENCODER_PATH", Path("/tools/mv-hevc-encoder")),
            patch.object(Path, "is_file", return_value=True),
            patch("bd_to_avp.modules.video_route.os.access", return_value=True),
            patch("bd_to_avp.modules.video_route.run_process_capture", side_effect=error),
            self.assertRaises(VideoRoutePreflightError),
        ):
            probe_direct_mv_hevc_capability()


if __name__ == "__main__":
    unittest.main()
