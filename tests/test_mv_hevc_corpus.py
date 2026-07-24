import copy
import json
import sys
import unittest

from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts import qualify_mv_hevc_corpus as corpus
from scripts.qualify_direct_mv_hevc import QualificationFailure
from scripts.qualify_mv_hevc_corpus import (
    CorpusCase,
    PreparedCase,
    derive_policy_bitrate,
    effective_bitrate_mbps,
    parse_manifest,
    qualify_case,
    redact_private_source_paths,
    summarize_quality_size_gate,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPOSITORY_ROOT / "docs/qualification/direct-mv-hevc-corpus-v1.json"


def manifest_document() -> dict[str, object]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def run_record(*, quality: float, final_bytes: int, eye_order_margin: float = 0.2) -> dict[str, object]:
    return {
        "min_same_eye_ssim": quality,
        "min_eye_order_margin": eye_order_margin,
        "final_bytes": final_bytes,
    }


class CorpusManifestTests(unittest.TestCase):
    def test_committed_manifest_covers_release_dimensions(self) -> None:
        manifest = parse_manifest(manifest_document())

        self.assertEqual(manifest.corpus_id, "direct-mv-hevc-prerelease-v1")
        self.assertEqual(manifest.supported_source_bit_depths, (8,))
        self.assertEqual(manifest.rejected_source_bit_depths, (10, 12))
        self.assertEqual(len(manifest.cases), 8)
        self.assertFalse(next(case for case in manifest.cases if case.case_id == "itu-mvcds-2").quality_gate)
        covered_tags = {tag for case in manifest.cases if case.quality_gate for tag in case.tags}
        self.assertTrue(set(manifest.required_coverage).issubset(covered_tags))
        self.assertGreaterEqual(sum("real_mvc" in case.tags for case in manifest.cases), 2)

    def test_manifest_rejects_missing_required_coverage(self) -> None:
        document = manifest_document()
        document["required_coverage"] = [*document["required_coverage"], "missing-category"]

        with self.assertRaisesRegex(QualificationFailure, "missing-category"):
            parse_manifest(document)

    def test_manifest_rejects_required_coverage_found_only_in_informational_case(self) -> None:
        document = manifest_document()
        document["required_coverage"] = ["public_conformance"]

        with self.assertRaisesRegex(QualificationFailure, "public_conformance"):
            parse_manifest(document)

    def test_manifest_rejects_unscoped_source_environment_variable(self) -> None:
        document = manifest_document()
        cases = copy.deepcopy(document["cases"])
        cases[0]["source"]["path_env"] = "HOME"
        document["cases"] = cases

        with self.assertRaisesRegex(QualificationFailure, "BD_TO_AVP_ prefix"):
            parse_manifest(document)

    def test_manifest_rejects_crop_outside_one_eye(self) -> None:
        document = manifest_document()
        cases = copy.deepcopy(document["cases"])
        cases[4]["transforms"]["crop"] = [1920, 1080, 2, 0]
        document["cases"] = cases

        with self.assertRaisesRegex(QualificationFailure, "falls outside"):
            parse_manifest(document)


class CorpusPolicyTests(unittest.TestCase):
    def test_derived_policy_uses_worst_case_with_headroom(self) -> None:
        self.assertEqual(derive_policy_bitrate([1.0, 4.0, 7.5], headroom_fraction=0.10), 9)

    def test_derived_policy_is_bounded_by_worker_contract(self) -> None:
        self.assertEqual(derive_policy_bitrate([480.0], headroom_fraction=0.10), 500)

    def test_quality_size_gate_accepts_bounded_noninferiority(self) -> None:
        result = summarize_quality_size_gate(
            [run_record(quality=0.9800, final_bytes=1_000_000)],
            [
                run_record(quality=0.9792, final_bytes=900_000),
                run_record(quality=0.9791, final_bytes=905_000),
            ],
            quality_tolerance=0.001,
            max_size_ratio=1.0,
            minimum_eye_order_margin=0.15,
        )

        self.assertTrue(result["passed"])
        self.assertAlmostEqual(result["size_ratio"], 0.9025)

    def test_quality_size_gate_rejects_any_direct_run_below_floor(self) -> None:
        result = summarize_quality_size_gate(
            [run_record(quality=0.9800, final_bytes=1_000_000)],
            [
                run_record(quality=0.9792, final_bytes=900_000),
                run_record(quality=0.9789, final_bytes=890_000),
            ],
            quality_tolerance=0.001,
            max_size_ratio=1.0,
            minimum_eye_order_margin=0.15,
        )

        self.assertFalse(result["quality_passed"])
        self.assertFalse(result["passed"])

    def test_quality_size_gate_rejects_size_regression(self) -> None:
        result = summarize_quality_size_gate(
            [run_record(quality=0.9800, final_bytes=1_000_000)],
            [run_record(quality=0.9800, final_bytes=1_010_000)],
            quality_tolerance=0.001,
            max_size_ratio=1.0,
            minimum_eye_order_margin=0.15,
        )

        self.assertFalse(result["size_passed"])
        self.assertFalse(result["passed"])

    def test_quality_size_gate_rejects_any_oversized_direct_run(self) -> None:
        result = summarize_quality_size_gate(
            [run_record(quality=0.9800, final_bytes=1_000_000)],
            [
                run_record(quality=0.9800, final_bytes=900_000),
                run_record(quality=0.9800, final_bytes=900_000),
                run_record(quality=0.9800, final_bytes=1_100_000),
            ],
            quality_tolerance=0.001,
            max_size_ratio=1.0,
            minimum_eye_order_margin=0.15,
        )

        self.assertAlmostEqual(result["size_ratio"], 0.9)
        self.assertAlmostEqual(result["max_run_size_ratio"], 1.1)
        self.assertFalse(result["size_passed"])
        self.assertFalse(result["passed"])

    def test_candidate_search_continues_after_repeat_validation_failure(self) -> None:
        prepared = PreparedCase(
            definition=CorpusCase(
                case_id="test-case",
                tags=("real_mvc",),
                source={"kind": "synthetic"},
                eye_width=320,
                eye_height=180,
                frame_rate="24",
            ),
            source_path=Path("source.mkv"),
            reference_left=Path("left.mov"),
            reference_right=Path("right.mov"),
            duration_seconds=2,
            frame_count=48,
            source_evidence={"kind": "synthetic"},
        )
        measured_runs = [
            run_record(quality=0.98, final_bytes=1_000),
            run_record(quality=0.98, final_bytes=900),
            run_record(quality=0.98, final_bytes=1_100),
            run_record(quality=0.98, final_bytes=900),
            run_record(quality=0.98, final_bytes=950),
        ]
        with (
            patch.object(corpus, "_encode_generated"),
            patch.object(corpus, "_encode_direct"),
            patch.object(corpus, "_measure_output", side_effect=measured_runs),
            patch.object(corpus, "sha256_file", return_value="sha256"),
        ):
            result = qualify_case(
                "ffmpeg",
                Path("encoder"),
                prepared,
                Path("work"),
                current_runs=1,
                direct_runs=2,
                candidate_bitrates=(1.0, 2.0),
                quality_tolerance=0.001,
                matched_max_size_ratio=1.0,
                generated_eye_bitrate_mbps=20,
                generated_merge_quality=75,
            )

        candidates = result["direct_search"]["candidates"]
        self.assertEqual(result["direct_search"]["selected_bitrate_mbps"], 2.0)
        self.assertFalse(candidates[0]["acceptance"]["passed"])
        self.assertTrue(candidates[1]["acceptance"]["passed"])
        self.assertEqual(len(result["selected_direct_runs"]), 2)

    def test_pipeline_timeout_is_bounded_and_reaped(self) -> None:
        with self.assertRaisesRegex(QualificationFailure, "timed out"):
            corpus._run_pipeline(
                [sys.executable, "-c", "import time; time.sleep(10)"],
                [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
                timeout=0.01,
            )

    def test_pipeline_reaps_producer_when_consumer_cannot_start(self) -> None:
        producer = MagicMock()
        producer.stdout = MagicMock()
        producer.stderr = MagicMock()
        producer.stderr.closed = False
        with (
            patch.object(corpus.subprocess, "Popen", side_effect=[producer, OSError("missing consumer")]),
            patch.object(corpus, "kill_and_reap") as kill_and_reap,
            self.assertRaisesRegex(QualificationFailure, "Could not start qualification pipeline consumer"),
        ):
            corpus._run_pipeline(["producer"], ["consumer"])

        producer.stdout.close.assert_called_once_with()
        producer.stderr.close.assert_called_once_with()
        kill_and_reap.assert_called_once_with(producer)

    def test_private_source_paths_are_redacted(self) -> None:
        private_path = Path("/Users/example/Private Movie/source.mkv")
        redacted = redact_private_source_paths(f"Command failed: ffmpeg -i {private_path}", [private_path])

        self.assertNotIn(str(private_path), redacted)
        self.assertIn("<private-source>", redacted)

    def test_private_source_path_is_redacted_from_preparation_failure(self) -> None:
        private_path = Path("/Users/example/Private Movie/source.mkv")
        case = CorpusCase(
            case_id="private-source",
            tags=("real_mvc",),
            source={
                "kind": "mvc_container",
                "path_env": "BD_TO_AVP_PRIVATE_SOURCE",
                "start_seconds": 0,
                "duration_seconds": 1,
            },
            eye_width=320,
            eye_height=180,
            frame_rate="24",
        )
        with (
            patch.object(corpus, "_source_path_from_environment", return_value=private_path),
            patch.object(
                corpus,
                "run",
                side_effect=QualificationFailure(f"Command failed: ffmpeg -i {private_path}"),
            ),
            self.assertRaises(QualificationFailure) as raised,
        ):
            corpus._prepare_mvc_case(case, Path("work"), ffmpeg="ffmpeg")

        self.assertNotIn(str(private_path), str(raised.exception))
        self.assertIn("<private-source>", str(raised.exception))

    def test_private_annex_b_path_is_redacted_from_fingerprint_failure(self) -> None:
        private_path = Path("/Users/example/Private Movie/source.264")
        case = CorpusCase(
            case_id="private-annex-b",
            tags=("real_mvc",),
            source={"kind": "mvc_annex_b", "path_env": "BD_TO_AVP_PRIVATE_SOURCE"},
            eye_width=320,
            eye_height=180,
            frame_rate="24",
        )
        with (
            patch.object(corpus, "_source_path_from_environment", return_value=private_path),
            patch.object(corpus, "_run_pipeline", return_value=""),
            self.assertRaises(QualificationFailure) as raised,
        ):
            corpus._prepare_mvc_case(case, Path("work"), ffmpeg="ffmpeg")

        self.assertNotIn(str(private_path), str(raised.exception))
        self.assertIn("<private-source>", str(raised.exception))

    def test_effective_bitrate_uses_decimal_megabits(self) -> None:
        self.assertEqual(effective_bitrate_mbps(1_000_000, 2), 4.0)


if __name__ == "__main__":
    unittest.main()
