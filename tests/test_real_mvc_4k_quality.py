import json
import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from scripts import qualify_real_mvc_4k_quality as quality
from scripts.qualify_real_mvc_4k_quality import (
    EXPECTED_RUNS,
    RealMVCQualityFailure,
    build_evidence,
    probe_eye,
    remove_private_work_directory,
    source_video_info,
    summarize_real_mvc_quality,
)


def run_record(
    *,
    quality_score: float,
    eye_order_margin: float = 0.01,
    final_bytes: int = 100,
    sha256: str = "a" * 64,
) -> dict[str, object]:
    return {
        "final_bytes": final_bytes,
        "min_eye_order_margin": eye_order_margin,
        "min_same_eye_ssim": quality_score,
        "sha256": sha256,
    }


class RealMVC4KQualityTests(unittest.TestCase):
    def test_quality_summary_accepts_three_nondeterministic_runs(self) -> None:
        file_based = [run_record(quality_score=0.95, final_bytes=110, sha256=str(index) * 64) for index in range(3)]
        direct = [run_record(quality_score=0.949, final_bytes=100, sha256=str(index + 3) * 64) for index in range(3)]

        summary = summarize_real_mvc_quality(file_based, direct)

        self.assertTrue(summary["passed"])
        self.assertEqual(summary["direct_run_count"], EXPECTED_RUNS)
        self.assertLess(summary["size_ratio"], 1)

    def test_quality_summary_rejects_direct_quality_loss(self) -> None:
        file_based = [run_record(quality_score=0.95, final_bytes=110) for _ in range(3)]
        direct = [run_record(quality_score=0.947, final_bytes=100) for _ in range(3)]

        summary = summarize_real_mvc_quality(file_based, direct)

        self.assertFalse(summary["quality_passed"])
        self.assertFalse(summary["passed"])

    def test_quality_summary_rejects_oversize_direct_run(self) -> None:
        file_based = [run_record(quality_score=0.95, final_bytes=100) for _ in range(3)]
        direct = [run_record(quality_score=0.95, final_bytes=size) for size in (100, 104, 106)]

        summary = summarize_real_mvc_quality(file_based, direct)

        self.assertFalse(summary["size_passed"])
        self.assertFalse(summary["passed"])

    def test_quality_summary_rejects_file_based_eye_swap(self) -> None:
        file_based = [run_record(quality_score=0.95, eye_order_margin=0.0005) for _ in range(3)]
        direct = [run_record(quality_score=0.95) for _ in range(3)]

        summary = summarize_real_mvc_quality(file_based, direct)

        self.assertFalse(summary["file_based_eye_order_passed"])
        self.assertFalse(summary["passed"])

    def test_quality_summary_requires_three_runs_per_route(self) -> None:
        with self.assertRaisesRegex(ValueError, "exactly 3"):
            summarize_real_mvc_quality(
                [run_record(quality_score=0.95) for _ in range(2)],
                [run_record(quality_score=0.95) for _ in range(3)],
            )

    def test_source_video_info_parses_public_timing_only(self) -> None:
        payload = json.dumps(
            {
                "format": {"duration": "65.649"},
                "streams": [{"avg_frame_rate": "24000/1001", "nb_read_packets": "1573"}],
            }
        )
        with patch.object(quality, "run_checked", return_value=payload):
            info = source_video_info(Path("ffprobe"), Path("private-source.mkv"))

        self.assertEqual(
            info,
            {"duration_seconds": 65.649, "frame_count": 1573, "frame_rate": "24000/1001"},
        )

    def test_source_video_info_rejects_missing_timing(self) -> None:
        with patch.object(quality, "run_checked", return_value='{"streams": []}'):
            with self.assertRaises(RealMVCQualityFailure):
                source_video_info(Path("ffprobe"), Path("private-source.mkv"))

    def test_quality_eye_requires_exact_frame_count(self) -> None:
        payload = json.dumps(
            {
                "streams": [
                    {
                        "height": 2160,
                        "nb_read_frames": "1572",
                        "width": 3840,
                    }
                ]
            }
        )
        with patch.object(quality, "run_checked", return_value=payload):
            with self.assertRaisesRegex(RealMVCQualityFailure, "frame count"):
                probe_eye(
                    Path("ffprobe"),
                    Path("candidate.mov"),
                    expected_dimensions=(3840, 2160),
                    expected_frames=1573,
                )

    def test_private_work_cleanup_fails_closed(self) -> None:
        with patch.object(quality.shutil, "rmtree", side_effect=OSError("busy")):
            with self.assertRaisesRegex(RealMVCQualityFailure, "private real-MVC quality work directory"):
                remove_private_work_directory(Path("private-work"))

    def test_public_quality_evidence_omits_private_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            app_path = root / "Qualification.app"
            worker = app_path / "Contents/MacOS/worker"
            binary_root = app_path / "Contents/Resources/app/bd_to_avp/bin"
            helper = binary_root / "mv-hevc-encoder"
            tools = {
                name: binary_root / name
                for name in ("edge264_test", "ffmpeg", "ffprobe", "fx-upscale", "spatial-media-kit-tool")
            }
            for path in (worker, helper, *tools.values()):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(path.name.encode())
                path.chmod(0o755)
            app = quality.AppBundle(
                path=app_path,
                worker=worker,
                helper=helper,
                ffmpeg=tools["ffmpeg"],
                ffprobe=tools["ffprobe"],
                bundle_identifier="com.example.qualification",
                version="1.0",
            )
            source = root / "private-source.mkv"
            reference_left = root / "private-left.mkv"
            reference_right = root / "private-right.mkv"
            for path in (source, reference_left, reference_right):
                path.write_bytes(path.name.encode())
            file_based = [run_record(quality_score=0.95, final_bytes=110) for _ in range(3)]
            direct = [run_record(quality_score=0.949, final_bytes=100) for _ in range(3)]

            evidence = build_evidence(
                app=app,
                source=source,
                source_info={"duration_seconds": 65.649, "frame_count": 1573, "frame_rate": "24000/1001"},
                reference_left=reference_left,
                reference_right=reference_right,
                direct_runs=direct,
                file_based_runs=file_based,
                fallback_helper_sha256="b" * 64,
            )
            encoded = json.dumps(evidence, sort_keys=True)

            self.assertTrue(evidence["acceptance"]["passed"])
            self.assertNotIn(temporary_directory, encoded)
            self.assertNotIn("private-source", encoded)


if __name__ == "__main__":
    unittest.main()
