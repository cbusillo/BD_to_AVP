import unittest

from scripts.reassess_real_mvc_feature import reassess


def run_evidence(*, duration_seconds: float, rss: int, metal: int) -> dict[str, object]:
    return {
        "acceptance": {"passed": True},
        "artifact": {"duration_seconds": duration_seconds},
        "encoder": {
            "frame_count": 240,
            "metalfx_device_final_allocated_size_bytes": metal,
            "metalfx_device_peak_delta_bytes": metal,
        },
        "resources": {
            "duration_seconds": duration_seconds,
            "peak_rss_bytes": rss,
            "processes": {
                "pipeline": {
                    "quartile_peak_rss_bytes": [rss - 20, rss - 10, rss - 5, rss],
                }
            },
            "quartile_peak_rss_bytes": [rss - 20, rss - 10, rss - 5, rss],
        },
    }


class RealMVCFeatureReassessmentTests(unittest.TestCase):
    def test_reassessment_recomputes_reviewed_resource_policy(self) -> None:
        document = {
            "acceptance": {"passed": False},
            "boundedness": {"passed": False},
            "cancellation": {"acceptance": {"passed": True}},
            "configuration": {
                "max_rss_growth_mib": 64,
                "output_duration_tolerance_seconds": 0.1,
            },
            "feature_run": run_evidence(duration_seconds=10.01, rss=600_000_000, metal=452_280_320),
            "schema_version": 2,
            "short_run": run_evidence(duration_seconds=1, rss=500_000_000, metal=419_086_336),
            "sources": {
                "feature": {
                    "packet_count": 240,
                    "video_duration_seconds": 10.01,
                }
            },
        }

        evidence = reassess(document, input_sha256="a" * 64)

        self.assertTrue(evidence["acceptance"]["passed"])
        self.assertTrue(evidence["boundedness"]["passed"])
        self.assertEqual(evidence["assessment"]["input_evidence_sha256"], "a" * 64)
        self.assertEqual(
            evidence["assessment"]["policy"],
            "per-process-and-aggregate-q3-q4-plateau-v3",
        )
        self.assertEqual(evidence["schema_version"], 3)

    def test_reassessment_requires_exact_feature_frame_count(self) -> None:
        document = {
            "acceptance": {"passed": False},
            "boundedness": {"passed": False},
            "cancellation": {"acceptance": {"passed": True}},
            "configuration": {
                "max_rss_growth_mib": 64,
                "output_duration_tolerance_seconds": 0.1,
            },
            "feature_run": run_evidence(duration_seconds=10.01, rss=600_000_000, metal=452_280_320),
            "schema_version": 2,
            "short_run": run_evidence(duration_seconds=1, rss=500_000_000, metal=419_086_336),
            "sources": {
                "feature": {
                    "packet_count": 241,
                    "video_duration_seconds": 10.01,
                }
            },
        }

        evidence = reassess(document, input_sha256="a" * 64)

        self.assertFalse(evidence["acceptance"]["complete_feature_length_frame_count"])
        self.assertFalse(evidence["acceptance"]["passed"])


if __name__ == "__main__":
    unittest.main()
