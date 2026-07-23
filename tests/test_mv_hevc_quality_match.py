import unittest

from scripts import qualify_mv_hevc_quality_match


class MVHEVCQualityMatchTests(unittest.TestCase):
    def test_effective_bitrate_mbps_uses_file_size_and_duration(self) -> None:
        self.assertEqual(
            qualify_mv_hevc_quality_match.effective_bitrate_mbps(250_000, 2),
            1.0,
        )

    def test_summary_passes_when_all_direct_runs_match_quality_and_reduce_size(self) -> None:
        current_runs = [
            {"final_bytes": 165_000, "min_same_eye_ssim": 0.91},
            {"final_bytes": 166_000, "min_same_eye_ssim": 0.91},
            {"final_bytes": 164_000, "min_same_eye_ssim": 0.91},
        ]
        direct_runs = [
            {"final_bytes": 140_000, "min_same_eye_ssim": 0.9121, "sha256": "same"},
            {"final_bytes": 141_000, "min_same_eye_ssim": 0.913, "sha256": "same"},
            {"final_bytes": 139_000, "min_same_eye_ssim": 0.914, "sha256": "same"},
        ]

        summary = qualify_mv_hevc_quality_match.summarize_quality_match(
            current_runs,
            direct_runs,
            required_quality_margin=0.002,
        )

        self.assertTrue(summary["passed"])
        self.assertTrue(summary["all_direct_runs_meet_required_quality"])
        self.assertTrue(summary["all_direct_runs_quality_not_lower"])
        self.assertTrue(summary["direct_runs_byte_identical"])
        self.assertAlmostEqual(summary["direct_to_current_size_ratio"], 140_000 / 165_000)

    def test_summary_fails_when_any_direct_run_loses_quality(self) -> None:
        current_runs = [{"final_bytes": 100, "min_same_eye_ssim": 0.91}]
        direct_runs = [{"final_bytes": 90, "min_same_eye_ssim": 0.90}]

        summary = qualify_mv_hevc_quality_match.summarize_quality_match(current_runs, direct_runs)

        self.assertFalse(summary["passed"])
        self.assertFalse(summary["quality_not_lower"])

    def test_summary_requires_every_direct_run_to_meet_quality_margin(self) -> None:
        current_runs = [
            {"final_bytes": 100, "min_same_eye_ssim": 0.91},
            {"final_bytes": 100, "min_same_eye_ssim": 0.91},
            {"final_bytes": 100, "min_same_eye_ssim": 0.91},
        ]
        direct_runs = [
            {"final_bytes": 90, "min_same_eye_ssim": 0.911},
            {"final_bytes": 90, "min_same_eye_ssim": 0.913},
            {"final_bytes": 90, "min_same_eye_ssim": 0.914},
        ]

        summary = qualify_mv_hevc_quality_match.summarize_quality_match(
            current_runs,
            direct_runs,
            required_quality_margin=0.002,
        )

        self.assertTrue(summary["quality_margin_met"])
        self.assertFalse(summary["all_direct_runs_meet_required_quality"])
        self.assertFalse(summary["passed"])

    def test_summary_rejects_zero_sized_current_output(self) -> None:
        with self.assertRaisesRegex(ValueError, "current median size"):
            qualify_mv_hevc_quality_match.summarize_quality_match(
                [{"final_bytes": 0, "min_same_eye_ssim": 0.91}],
                [{"final_bytes": 1, "min_same_eye_ssim": 0.92}],
            )


if __name__ == "__main__":
    unittest.main()
