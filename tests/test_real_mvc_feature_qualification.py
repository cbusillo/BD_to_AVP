import json
import subprocess
import tempfile
import threading
import unittest

from pathlib import Path
from unittest.mock import Mock, patch

from scripts import qualify_real_mvc_feature as feature
from scripts.qualify_real_mvc_feature import (
    FeatureQualificationFailure,
    PackagedTools,
    QualificationEventSink,
    ResourceSampler,
    app_tree_sha256,
    normalizer_command,
    powermetrics_thermal_summary,
    probe_direct_artifact,
    require_nominal_powermetrics_thermal,
    remove_private_work_directory,
    resource_boundedness,
    safe_encoder_summary,
    source_video_info,
    terminate_packaged_worker,
    validate_qualification_paths,
)
from scripts.verify_packaged_mv_hevc_routes import AppBundle


def run_evidence(
    *,
    rss: int,
    metal: int,
    duration_seconds: float,
    first_quarter_peak_rss: int | None = None,
    last_quarter_peak_rss: int | None = None,
) -> dict[str, object]:
    return {
        "resources": {
            "duration_seconds": duration_seconds,
            "first_quarter_peak_rss_bytes": first_quarter_peak_rss or rss,
            "last_quarter_peak_rss_bytes": last_quarter_peak_rss or rss,
            "peak_rss_bytes": rss,
            "quartile_peak_rss_bytes": [
                first_quarter_peak_rss or rss,
                first_quarter_peak_rss or rss,
                first_quarter_peak_rss or rss,
                last_quarter_peak_rss or rss,
            ],
            "processes": {
                "pipeline": {
                    "quartile_peak_rss_bytes": [
                        first_quarter_peak_rss or rss,
                        first_quarter_peak_rss or rss,
                        first_quarter_peak_rss or rss,
                        last_quarter_peak_rss or rss,
                    ]
                }
            },
        },
        "encoder": {
            "frame_count": 240,
            "metalfx_device_final_allocated_size_bytes": metal,
            "metalfx_device_peak_delta_bytes": metal,
            "upscale_mode": "metalfx",
        },
    }


class RealMVCFeatureQualificationTests(unittest.TestCase):
    def app_bundle(self, root: Path) -> AppBundle:
        app_path = root / "Qualification.app"
        return AppBundle(
            path=app_path,
            worker=app_path / "Contents/MacOS/worker",
            helper=app_path / "Contents/Resources/app/bd_to_avp/bin/mv-hevc-encoder",
            ffmpeg=app_path / "Contents/Resources/app/bd_to_avp/bin/ffmpeg",
            ffprobe=app_path / "Contents/Resources/app/bd_to_avp/bin/ffprobe",
            bundle_identifier="com.example.qualification",
            version="1.0",
        )

    def test_normalizer_preserves_two_1080p_eyes(self) -> None:
        command = normalizer_command(Path("ffmpeg"), frame_rate="24000/1001")
        filter_complex = command[command.index("-filter_complex") + 1]

        self.assertIn("crop=1920:1080:0:0", filter_complex)
        self.assertIn("crop=1920:1080:1920:0", filter_complex)
        self.assertIn("hstack=inputs=2", filter_complex)

    def test_resource_boundedness_accepts_documented_growth(self) -> None:
        result = resource_boundedness(
            run_evidence(rss=500_000_000, metal=419_086_336, duration_seconds=60),
            run_evidence(rss=550_000_000, metal=452_280_320, duration_seconds=600),
            max_rss_growth_bytes=64 * 1024 * 1024,
        )

        self.assertTrue(result["passed"])

    def test_resource_boundedness_rejects_unbounded_rss(self) -> None:
        result = resource_boundedness(
            run_evidence(rss=500_000_000, metal=419_086_336, duration_seconds=60),
            run_evidence(rss=3 * 1024**3, metal=419_086_336, duration_seconds=600),
            max_rss_growth_bytes=64 * 1024 * 1024,
        )

        self.assertFalse(result["feature_process_peak_passed"])
        self.assertFalse(result["passed"])

    def test_resource_boundedness_rejects_late_feature_growth(self) -> None:
        result = resource_boundedness(
            run_evidence(rss=500_000_000, metal=419_086_336, duration_seconds=60),
            run_evidence(
                rss=550_000_000,
                metal=419_086_336,
                duration_seconds=600,
                first_quarter_peak_rss=450_000_000,
                last_quarter_peak_rss=550_000_000,
            ),
            max_rss_growth_bytes=64 * 1024 * 1024,
        )

        self.assertFalse(result["feature_plateau_passed"])
        self.assertFalse(result["passed"])

    def test_resource_boundedness_allows_bounded_process_and_aggregate_growth(self) -> None:
        short_run = run_evidence(rss=500_000_000, metal=419_086_336, duration_seconds=60)
        feature_run = run_evidence(rss=600_000_000, metal=452_280_320, duration_seconds=600)
        feature_run["resources"]["quartile_peak_rss_bytes"] = [
            500_000_000,
            520_000_000,
            530_000_000,
            610_000_000,
        ]
        feature_run["resources"]["processes"] = {
            "decoder": {"quartile_peak_rss_bytes": [100_000_000, 110_000_000, 120_000_000, 160_000_000]},
            "normalizer": {"quartile_peak_rss_bytes": [400_000_000, 410_000_000, 410_000_000, 450_000_000]},
        }

        result = resource_boundedness(
            short_run,
            feature_run,
            max_rss_growth_bytes=64 * 1024 * 1024,
        )

        self.assertGreater(result["feature_total_plateau_growth_bytes"], 64 * 1024 * 1024)
        self.assertLessEqual(
            result["feature_total_plateau_growth_bytes"],
            result["feature_aggregate_plateau_allowance_bytes"],
        )
        self.assertTrue(result["feature_total_plateau_passed"])
        self.assertTrue(result["feature_plateau_passed"])
        self.assertTrue(result["passed"])

    def test_resource_boundedness_rejects_distributed_aggregate_growth(self) -> None:
        short_run = run_evidence(rss=500_000_000, metal=419_086_336, duration_seconds=60)
        feature_run = run_evidence(rss=700_000_000, metal=452_280_320, duration_seconds=600)
        feature_run["resources"]["quartile_peak_rss_bytes"] = [
            500_000_000,
            520_000_000,
            530_000_000,
            680_000_000,
        ]
        feature_run["resources"]["processes"] = {
            "decoder": {"quartile_peak_rss_bytes": [100_000_000, 110_000_000, 120_000_000, 170_000_000]},
            "encoder": {"quartile_peak_rss_bytes": [100_000_000, 110_000_000, 120_000_000, 170_000_000]},
            "normalizer": {"quartile_peak_rss_bytes": [300_000_000, 300_000_000, 290_000_000, 340_000_000]},
        }

        result = resource_boundedness(
            short_run,
            feature_run,
            max_rss_growth_bytes=64 * 1024 * 1024,
        )

        self.assertTrue(result["feature_process_plateau_passed"])
        self.assertFalse(result["feature_total_plateau_passed"])
        self.assertFalse(result["feature_plateau_passed"])
        self.assertFalse(result["passed"])

    def test_resource_boundedness_rejects_excess_metal_duration_growth(self) -> None:
        result = resource_boundedness(
            run_evidence(rss=500_000_000, metal=100_000_000, duration_seconds=60),
            run_evidence(rss=500_000_000, metal=900_000_000, duration_seconds=600),
            max_rss_growth_bytes=64 * 1024 * 1024,
        )

        self.assertFalse(result["feature_metal_duration_growth_passed"])
        self.assertFalse(result["passed"])

    def test_resource_sampler_surfaces_background_failures(self) -> None:
        sampler = ResourceSampler(QualificationEventSink(), interval_seconds=0.01)
        failure_observed = threading.Event()

        def fail_thermal_state() -> int:
            failure_observed.set()
            raise FeatureQualificationFailure("boom")

        with patch.object(feature, "thermal_state", side_effect=fail_thermal_state):
            sampler.start()
            self.assertTrue(failure_observed.wait(timeout=5))
            with self.assertRaisesRegex(FeatureQualificationFailure, "Resource sampler failed"):
                sampler.stop()

    def test_powermetrics_thermal_summary_requires_complete_values(self) -> None:
        trace = "\n".join(
            (
                "**** Thermal pressure ****",
                "Current pressure level: Nominal",
                "**** Thermal pressure ****",
                "Current pressure level: Nominal",
            )
        )

        summary = powermetrics_thermal_summary(trace)

        self.assertTrue(summary["all_nominal"])
        self.assertEqual(summary["sample_count"], 2)
        self.assertEqual(summary["counts"]["nominal"], 2)

    def test_powermetrics_gate_rejects_non_nominal_pressure(self) -> None:
        trace = "\n".join(
            (
                "**** Thermal pressure ****",
                "Current pressure level: Fair",
            )
        )

        with self.assertRaisesRegex(FeatureQualificationFailure, "non-nominal"):
            require_nominal_powermetrics_thermal(trace)

    def test_safe_encoder_summary_drops_unapproved_fields(self) -> None:
        summary = safe_encoder_summary(
            {
                "frame_count": 100,
                "metalfx_device_peak_delta_bytes": 42,
                "private_source_path": "/private/source.mkv",
                "upscale_mode": "metalfx",
            }
        )

        self.assertNotIn("private_source_path", summary)
        self.assertEqual(summary["frame_count"], 100)

    def test_source_video_info_requires_positive_packet_timing(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                {
                    "format": {"duration": "10.01"},
                    "streams": [{"avg_frame_rate": "24000/1001", "nb_read_packets": "240"}],
                }
            ),
            stderr="",
        )
        with patch.object(feature.subprocess, "run", return_value=completed):
            info = source_video_info(Path("ffprobe"), Path("source.mkv"))

        self.assertEqual(info["packet_count"], 240)
        self.assertEqual(info["frame_rate"], "24000/1001")
        self.assertAlmostEqual(info["video_duration_seconds"], 10.01)

    def test_validate_qualification_paths_rejects_private_inputs_under_work_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            work_directory = root / "work"

            with self.assertRaisesRegex(FeatureQualificationFailure, "must not contain media"):
                validate_qualification_paths(
                    self.app_bundle(root),
                    work_directory / "source.mkv",
                    root / "segment.mkv",
                    root / "evidence.json",
                    work_directory,
                    requested_work_directory=work_directory,
                )

    def test_validate_qualification_paths_rejects_symlinked_work_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            work_directory = root / "work"
            work_directory.mkdir()
            requested_work_directory = root / "work-link"
            requested_work_directory.symlink_to(work_directory, target_is_directory=True)

            with self.assertRaisesRegex(FeatureQualificationFailure, "must not be a symlink"):
                validate_qualification_paths(
                    self.app_bundle(root),
                    root / "source.mkv",
                    root / "segment.mkv",
                    root / "evidence.json",
                    work_directory,
                    requested_work_directory=requested_work_directory,
                )

    def test_private_work_directory_cleanup_removes_source_derived_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            work_directory = Path(temporary_directory) / "work"
            work_directory.mkdir()
            (work_directory / "partial.mov").write_bytes(b"partial")

            self.assertTrue(remove_private_work_directory(work_directory))
            self.assertFalse(work_directory.exists())

    def test_packaged_worker_cleanup_signals_validated_process_group(self) -> None:
        process = Mock()
        process.wait.return_value = 130

        with (
            patch.object(feature.os, "killpg") as killpg,
            patch.object(feature, "process_group_is_running", return_value=False),
        ):
            terminate_packaged_worker(process, 123)

        killpg.assert_called_once_with(123, feature.signal.SIGTERM)
        process.terminate.assert_not_called()

    def test_direct_artifact_requires_projection_box(self) -> None:
        tools = PackagedTools(
            ffmpeg=Path("ffmpeg"),
            ffprobe=Path("ffprobe"),
            edge264=Path("edge264"),
            encoder=Path("encoder"),
        )
        with (
            patch.object(feature, "verify_apple_media_compatible"),
            patch.object(feature, "box_types", return_value={"hvcC", "lhvC", "eyes", "hero", "st3d", "sv3d"}),
            self.assertRaisesRegex(FeatureQualificationFailure, "proj"),
        ):
            probe_direct_artifact(tools, Path("artifact.mov"))

    def test_app_tree_hash_includes_symlink_targets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "payload").write_text("payload", encoding="utf-8")
            (root / "link").symlink_to("payload")
            first = app_tree_sha256(root)
            (root / "link").unlink()
            (root / "link").symlink_to("different")

            self.assertNotEqual(first, app_tree_sha256(root))


if __name__ == "__main__":
    unittest.main()
