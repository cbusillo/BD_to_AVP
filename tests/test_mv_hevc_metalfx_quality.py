import unittest

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts import qualify_mv_hevc_metalfx


def quality_run(*, quality: float, size: int, elapsed: float, margin: float = 0.5):
    return qualify_mv_hevc_metalfx.QualityRun(
        elapsed_seconds=elapsed,
        final_bytes=size,
        left_cross_ssim=quality - margin,
        left_match_ssim=quality,
        min_eye_order_margin=margin,
        min_same_eye_ssim=quality,
        right_cross_ssim=quality - margin,
        right_match_ssim=quality,
        sha256="a" * 64,
    )


class MVHEVCMetalFXQualityTests(unittest.TestCase):
    def test_generator_downscales_one_native_4k_source_for_both_eyes(self) -> None:
        command = qualify_mv_hevc_metalfx.generator_command(
            "/tools/ffmpeg",
            frames=48,
            frame_rate=24,
        )

        self.assertIn("testsrc2=size=3840x2160:rate=24", command)
        filter_value = command[command.index("-filter_complex") + 1]
        self.assertIn("scale=1920:1080:flags=lanczos[left]", filter_value)
        self.assertIn("hflip,scale=1920:1080:flags=lanczos[right]", filter_value)
        self.assertEqual(command[command.index("-frames:v") + 1], "48")

    def test_fixture_variants_cover_dark_grain_crop_and_disparity(self) -> None:
        self.assertIn("eq=brightness=-0.35:contrast=0.75", qualify_mv_hevc_metalfx.high_resolution_source(24, "dark"))
        self.assertIn("noise=alls=12", qualify_mv_hevc_metalfx.high_resolution_source(24, "grain"))
        self.assertIn("crop=3840:2160:128:72", qualify_mv_hevc_metalfx.high_resolution_source(24, "crop"))
        disparity_command = qualify_mv_hevc_metalfx.generator_command(
            "/tools/ffmpeg",
            frames=24,
            frame_rate=24,
            fixture="disparity",
        )
        disparity_filter = disparity_command[disparity_command.index("-filter_complex") + 1]
        self.assertIn("pad=3872:2160:32:0:black,crop=3840:2160:0:0", disparity_filter)

    def test_acceptance_requires_quality_size_speed_and_eye_order(self) -> None:
        file_based = [quality_run(quality=0.95, size=1_000, elapsed=2.0)]
        metalfx = [quality_run(quality=0.951, size=900, elapsed=1.5)]
        pixel = [quality_run(quality=0.94, size=850, elapsed=1.4)]

        passing = qualify_mv_hevc_metalfx.summarize_acceptance(
            file_based,
            metalfx,
            pixel,
            quality_tolerance=0.002,
            size_target_ratio=0.99,
        )
        failing = qualify_mv_hevc_metalfx.summarize_acceptance(
            file_based,
            [quality_run(quality=0.94, size=1_100, elapsed=2.5, margin=0.1)],
            pixel,
            quality_tolerance=0.002,
            size_target_ratio=0.99,
        )

        self.assertTrue(passing["passed"])
        self.assertFalse(failing["passed"])
        self.assertFalse(failing["eye_order_preserved"])
        self.assertFalse(failing["metalfx_quality_matches_file_based"])
        self.assertFalse(failing["metalfx_size_has_headroom"])
        self.assertFalse(failing["metalfx_faster_than_file_based"])

        oversized_repeat = qualify_mv_hevc_metalfx.summarize_acceptance(
            [quality_run(quality=0.95, size=1_000, elapsed=2.0)] * 3,
            [
                quality_run(quality=0.951, size=900, elapsed=1.5),
                quality_run(quality=0.951, size=900, elapsed=1.5),
                quality_run(quality=0.951, size=1_000, elapsed=1.5),
            ],
            pixel,
            quality_tolerance=0.002,
            size_target_ratio=0.99,
        )
        self.assertFalse(oversized_repeat["metalfx_size_has_headroom"])

    def test_bitrate_search_records_and_selects_highest_probe_with_headroom(self) -> None:
        def fake_probe(*args, bitrate_mbps: float, **kwargs):
            return qualify_mv_hevc_metalfx.BitrateProbe(
                bitrate_mbps=bitrate_mbps,
                elapsed_seconds=1.0,
                final_bytes=int(bitrate_mbps * 100_000),
                sha256="b" * 64,
            )

        with TemporaryDirectory() as temporary_directory:
            with patch.object(qualify_mv_hevc_metalfx, "probe_direct_bitrate", side_effect=fake_probe):
                selected, probes = qualify_mv_hevc_metalfx.select_direct_bitrate(
                    "/tools/ffmpeg",
                    "/tools/ffprobe",
                    Path("encoder"),
                    Path(temporary_directory),
                    file_size_bytes=1_500_000,
                    fixture="motion",
                    frames=120,
                    frame_rate=24,
                    override_bitrate_mbps=None,
                    search_iterations=8,
                    search_max_bitrate_mbps=20,
                    search_min_bitrate_mbps=8,
                    search_precision_mbps=0.1,
                    size_target_ratio=0.99,
                )

        self.assertGreater(selected, 14.7)
        self.assertLessEqual(selected, 14.85)
        self.assertEqual(selected, max(probe.bitrate_mbps for probe in probes if probe.final_bytes <= 1_485_000))


if __name__ == "__main__":
    unittest.main()
