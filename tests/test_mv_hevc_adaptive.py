import json
import tempfile
import unittest

from pathlib import Path

from scripts.qualify_direct_mv_hevc import QualificationFailure
from scripts.qualify_mv_hevc_adaptive import load_generated_baseline, summarize_adaptive_acceptance


class AdaptiveBaselineTests(unittest.TestCase):
    def test_loads_manifest_bound_generated_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "baseline.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "manifest": {"sha256": "manifest"},
                        "cases": [
                            {
                                "id": "case-a",
                                "generated": {"runs": [{"final_bytes": 100}]},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            cases, digest = load_generated_baseline(
                path,
                expected_manifest_sha256="manifest",
                expected_case_ids=["case-a"],
            )

        self.assertEqual(cases["case-a"], [{"final_bytes": 100}])
        self.assertEqual(len(digest), 64)

    def test_rejects_baseline_from_different_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "baseline.json"
            path.write_text(
                json.dumps({"schema_version": 1, "manifest": {"sha256": "other"}, "cases": []}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(QualificationFailure, "does not match"):
                load_generated_baseline(
                    path,
                    expected_manifest_sha256="manifest",
                    expected_case_ids=[],
                )


class AdaptiveAcceptanceTests(unittest.TestCase):
    def test_only_quality_gated_cases_control_acceptance(self) -> None:
        acceptance = summarize_adaptive_acceptance(
            [
                {
                    "quality_gate": True,
                    "direct_runs": [{"min_eye_order_margin": 0.02}],
                    "acceptance": {
                        "quality_delta": -0.0003,
                        "max_run_size_ratio": 0.9,
                        "passed": True,
                    },
                },
                {
                    "quality_gate": False,
                    "direct_runs": [{"min_eye_order_margin": -1}],
                    "acceptance": {
                        "quality_delta": -1,
                        "max_run_size_ratio": 10,
                        "passed": False,
                    },
                },
            ]
        )

        self.assertEqual(acceptance["gated_case_count"], 1)
        self.assertEqual(acceptance["minimum_quality_delta"], -0.0003)
        self.assertEqual(acceptance["maximum_size_ratio"], 0.9)
        self.assertEqual(acceptance["minimum_eye_order_margin"], 0.02)
        self.assertTrue(acceptance["passed"])


if __name__ == "__main__":
    unittest.main()
