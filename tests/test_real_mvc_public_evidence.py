import hashlib
import json
import re
import subprocess
import unittest

from pathlib import Path


EVIDENCE_PATH = Path(__file__).parents[1] / "docs/qualification/direct-4k-real-mvc-v1.json"
REPOSITORY_ROOT = EVIDENCE_PATH.parents[2]
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
PRIVATE_PATH_PATTERN = re.compile(r"(?:/(?:Users|Volumes|home|private|tmp|var)/|[A-Za-z]:\\\\|\\\\\\\\)")


class RealMVCPublicEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.evidence = json.loads(EVIDENCE_PATH.read_text(encoding="utf-8"))

    def test_all_acceptance_checks_pass(self) -> None:
        acceptance = self.evidence["acceptance"]

        self.assertTrue(acceptance["passed"])
        self.assertTrue(all(acceptance.values()))

    def test_evidence_hashes_are_final_sha256_values(self) -> None:
        evidence_files = self.evidence["evidence_files"]

        self.assertTrue(all(SHA256_PATTERN.fullmatch(value) for value in evidence_files.values()))

    def test_qualification_harness_hashes_match_reviewed_files(self) -> None:
        harness = self.evidence["qualification_harness"]

        for relative_path, expected_sha256 in harness["files"].items():
            current_digest = hashlib.sha256((REPOSITORY_ROOT / relative_path).read_bytes()).hexdigest()
            if current_digest == expected_sha256:
                continue
            commits = subprocess.run(
                ["git", "log", "--format=%H", "--all", "--", relative_path],
                cwd=REPOSITORY_ROOT,
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            ).stdout.splitlines()
            historical_match = any(
                hashlib.sha256(
                    subprocess.run(
                        ["git", "show", f"{commit}:{relative_path}"],
                        cwd=REPOSITORY_ROOT,
                        check=True,
                        stdout=subprocess.PIPE,
                    ).stdout
                ).hexdigest()
                == expected_sha256
                for commit in commits
            )
            self.assertTrue(historical_match, relative_path)

    def test_route_matrix_records_all_eight_routes(self) -> None:
        route_matrix = self.evidence["packaged_route_matrix"]
        routes = route_matrix["routes"]

        self.assertEqual(route_matrix["route_count"], 8)
        self.assertEqual(len(routes), 8)
        self.assertTrue(all(routes.values()))
        self.assertEqual(route_matrix["app_tree_sha256"], self.evidence["package"]["app_tree_sha256"])
        self.assertEqual(
            self.evidence["feature_length"]["assessment"]["input_evidence_sha256"],
            self.evidence["evidence_files"]["feature_length_input_sha256"],
        )

    def test_feature_resource_gate_is_bounded(self) -> None:
        resources = self.evidence["feature_length"]["resources"]

        self.assertLessEqual(resources["process_tree_peak_rss_bytes"], resources["process_rss_safety_ceiling_bytes"])
        self.assertLessEqual(resources["metal_peak_delta_bytes"], resources["metal_safety_ceiling_bytes"])
        self.assertLessEqual(
            resources["max_process_late_quartile_growth_bytes"],
            resources["late_quartile_growth_ceiling_bytes"],
        )
        self.assertLessEqual(
            resources["total_late_quartile_growth_bytes"],
            resources["aggregate_late_quartile_growth_ceiling_bytes"],
        )
        self.assertEqual(
            resources["max_process_late_quartile_growth_bytes"],
            max(resources["per_process_late_quartile_growth_bytes"].values()),
        )

    def test_real_mvc_quality_gate_is_repeated_and_bounded(self) -> None:
        quality = self.evidence["real_mvc_quality"]
        direct = quality["direct"]
        file_based = quality["file_based"]
        thresholds = quality["thresholds"]

        self.assertTrue(all(quality["acceptance"].values()))
        self.assertEqual(direct["run_count"], thresholds["runs_per_route"])
        self.assertEqual(file_based["run_count"], thresholds["runs_per_route"])
        self.assertGreaterEqual(
            direct["median_min_same_eye_ssim"],
            quality["summary"]["required_direct_min_same_eye_ssim"],
        )
        self.assertLessEqual(quality["summary"]["max_run_size_ratio"], thresholds["max_size_ratio"])
        self.assertGreaterEqual(direct["minimum_eye_order_margin"], thresholds["minimum_eye_order_margin"])
        self.assertGreaterEqual(file_based["minimum_eye_order_margin"], thresholds["minimum_eye_order_margin"])
        self.assertEqual(quality["source_sha256"], self.evidence["representative_segment"]["sha256"])

    def test_public_evidence_omits_private_paths_and_titles(self) -> None:
        encoded = json.dumps(self.evidence, sort_keys=True)

        for forbidden in ("file://", "Private title"):
            self.assertNotIn(forbidden, encoded)
        self.assertIsNone(PRIVATE_PATH_PATTERN.search(encoded))
        self.assertEqual(
            set(self.evidence["source"]),
            {
                "container_duration_seconds",
                "frame_rate",
                "packet_count",
                "sha256",
                "size_bytes",
                "track_count",
                "video_duration_seconds",
            },
        )
        self.assertEqual(
            set(self.evidence["representative_segment"]),
            {
                "deterministic_repeat_match",
                "duration_seconds",
                "selection",
                "sha256",
                "size_bytes",
                "supersedes",
                "tracks",
            },
        )
        forbidden_keys = {"device_name", "hardware_model", "hostname", "path", "source_title", "title", "username"}
        observed_keys: set[str] = set()

        def collect_keys(value: object) -> None:
            if isinstance(value, dict):
                observed_keys.update(str(key) for key in value)
                for item in value.values():
                    collect_keys(item)
            elif isinstance(value, list):
                for item in value:
                    collect_keys(item)

        collect_keys(self.evidence)
        self.assertFalse(forbidden_keys & observed_keys)
        self.assertFalse(self.evidence["privacy"]["source_paths_recorded"])
        self.assertFalse(self.evidence["privacy"]["source_titles_recorded"])


if __name__ == "__main__":
    unittest.main()
