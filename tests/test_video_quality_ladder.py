import copy
import io
import json
import tempfile
import unittest

from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from scripts.validate_video_quality_ladder import CalibrationError, main, validate_manifest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPOSITORY_ROOT / "docs/qualification/video-quality-ladder-v1.json"
CORPUS_PATH = REPOSITORY_ROOT / "docs/qualification/direct-mv-hevc-corpus-v1.json"
CORPUS_SHA256 = "d6cfc14cb9f29e14639d40a96516fe14f47a64df850fe0928833b77b077e87b1"


class VideoQualityLadderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.document = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    def _validate(self, document: dict[str, object]) -> dict[str, object]:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            corpus = root / "docs/qualification/direct-mv-hevc-corpus-v1.json"
            corpus.parent.mkdir(parents=True)
            corpus.write_bytes(CORPUS_PATH.read_bytes())
            manifest = root / "video-quality-ladder.json"
            manifest.write_text(json.dumps(document), encoding="utf-8")
            return validate_manifest(manifest, repository_root=root)

    @staticmethod
    def _target(document: dict[str, object], target_id: str) -> dict[str, object]:
        targets = document["calibration_targets"]
        return next(target for target in targets if target["id"] == target_id)

    @staticmethod
    def _mapping(target: dict[str, object], step_id: str) -> dict[str, object]:
        return next(mapping for mapping in target["mappings"] if mapping["step_id"] == step_id)

    @staticmethod
    def _evidence(*, quality_delta: float, output_size_ratio: float, physical: bool = False) -> dict[str, object]:
        evidence: dict[str, object] = {
            "artifact_sha256": "a" * 64,
            "source_git_sha": "b" * 40,
            "fixture_manifest_sha256": CORPUS_SHA256,
            "quality_delta": quality_delta,
            "output_size_ratio": output_size_ratio,
            "encode_time_seconds": 60.0,
            "rationale": "Measured against the Balanced anchor.",
        }
        if physical:
            evidence["physical_playback"] = {
                "candidate_sha256": "d" * 64,
                "device_model": "Apple Vision Pro",
                "os_version": "visionOS test",
                "stereo_presentation": True,
                "beginning_seek": True,
                "middle_seek": True,
                "end_seek": True,
                "sustained_playback": True,
            }
        return evidence

    def test_committed_contract_is_valid_but_incomplete(self) -> None:
        receipt = validate_manifest()

        self.assertFalse(receipt["complete"])
        self.assertEqual(receipt["step_ids"][3], "balanced")
        self.assertEqual(receipt["custom_mode"], "exact_route_controls")
        self.assertEqual(len(receipt["targets"]), 5)
        self.assertFalse(any("#409" in blocker for blocker in receipt["blockers"]))
        self.assertTrue(any("#409" in item for item in receipt["deferred"]))

    def test_rejects_changed_balanced_production_anchor(self) -> None:
        document = copy.deepcopy(self.document)
        direct = self._target(document, "direct_mv_hevc")
        self._mapping(direct, "balanced")["values"] = {"quality": 0.71}

        with self.assertRaisesRegex(CalibrationError, "exact Balanced production default"):
            self._validate(document)

    def test_rejects_removed_balanced_production_anchor(self) -> None:
        document = copy.deepcopy(self.document)
        direct = self._target(document, "direct_mv_hevc")
        balanced = self._mapping(direct, "balanced")
        balanced.update(status="needs_calibration", values=None, evidence=None)

        with self.assertRaisesRegex(CalibrationError, "retain an exact Balanced production anchor"):
            self._validate(document)

    def test_rejects_extra_baseline_evidence_fields(self) -> None:
        document = copy.deepcopy(self.document)
        direct = self._target(document, "direct_mv_hevc")
        balanced = self._mapping(direct, "balanced")
        balanced["evidence"]["private_note"] = "must not be accepted"

        with self.assertRaisesRegex(CalibrationError, "exactly the anchor fields"):
            self._validate(document)

    def test_rejects_values_without_calibration_status(self) -> None:
        document = copy.deepcopy(self.document)
        direct = self._target(document, "direct_mv_hevc")
        self._mapping(direct, "compact")["values"] = {"quality": 0.5}

        with self.assertRaisesRegex(CalibrationError, "guessed values"):
            self._validate(document)

    def test_rejects_custom_inside_the_ladder(self) -> None:
        document = copy.deepcopy(self.document)
        document["steps"][-1] = {"id": "custom", "ordinal": 7, "label": "Custom"}

        with self.assertRaisesRegex(CalibrationError, "approved seven-step"):
            self._validate(document)

    def test_rejects_non_monotonic_route_values(self) -> None:
        document = copy.deepcopy(self.document)
        direct = self._target(document, "direct_mv_hevc")
        compact = self._mapping(direct, "compact")
        compact.update(
            status="measured",
            values={"quality": 0.8},
            evidence=self._evidence(quality_delta=-0.01, output_size_ratio=0.9),
        )

        with self.assertRaisesRegex(CalibrationError, "decreases from compact to balanced"):
            self._validate(document)

    def test_rejects_non_monotonic_measured_storage(self) -> None:
        document = copy.deepcopy(self.document)
        direct = self._target(document, "direct_mv_hevc")
        compact = self._mapping(direct, "compact")
        compact.update(
            status="measured",
            values={"quality": 0.6},
            evidence=self._evidence(quality_delta=-0.01, output_size_ratio=1.1),
        )
        balanced = self._mapping(direct, "balanced")
        balanced.update(
            status="measured",
            evidence=self._evidence(quality_delta=0.0, output_size_ratio=1.0),
        )

        with self.assertRaisesRegex(CalibrationError, "measured storage decreases"):
            self._validate(document)

    def test_baseline_anchor_bounds_measured_neighbor(self) -> None:
        document = copy.deepcopy(self.document)
        direct = self._target(document, "direct_mv_hevc")
        compact = self._mapping(direct, "compact")
        compact.update(
            status="measured",
            values={"quality": 0.6},
            evidence=self._evidence(quality_delta=-0.01, output_size_ratio=1.1),
        )

        with self.assertRaisesRegex(CalibrationError, "measured storage decreases"):
            self._validate(document)

    def test_incomplete_ladder_exposure_fails_closed(self) -> None:
        document = copy.deepcopy(self.document)
        direct = self._target(document, "direct_mv_hevc")
        direct["ladder_exposure"] = "ladder"

        with self.assertRaisesRegex(CalibrationError, "requires all seven qualified mappings"):
            self._validate(document)

    def test_measured_evidence_must_match_corpus_identity(self) -> None:
        document = copy.deepcopy(self.document)
        direct = self._target(document, "direct_mv_hevc")
        compact = self._mapping(direct, "compact")
        evidence = self._evidence(quality_delta=-0.01, output_size_ratio=0.9)
        evidence["fixture_manifest_sha256"] = "c" * 64
        compact.update(status="measured", values={"quality": 0.6}, evidence=evidence)

        with self.assertRaisesRegex(CalibrationError, "match the approved corpus"):
            self._validate(document)

    def test_qualified_physical_anchor_requires_device_evidence(self) -> None:
        document = copy.deepcopy(self.document)
        direct = self._target(document, "direct_mv_hevc")
        balanced = self._mapping(direct, "balanced")
        balanced.update(
            status="qualified",
            evidence=self._evidence(quality_delta=0.0, output_size_ratio=1.0),
        )

        with self.assertRaisesRegex(CalibrationError, "physical_playback"):
            self._validate(document)

    def test_optional_physical_evidence_uses_closed_schema(self) -> None:
        document = copy.deepcopy(self.document)
        direct = self._target(document, "direct_mv_hevc")
        compact = self._mapping(direct, "compact")
        evidence = self._evidence(quality_delta=-0.01, output_size_ratio=0.9, physical=True)
        evidence["physical_playback"]["private_note"] = "must not be accepted"
        compact.update(status="measured", values={"quality": 0.6}, evidence=evidence)

        with self.assertRaisesRegex(CalibrationError, "exactly the public evidence fields"):
            self._validate(document)

    def test_balanced_measured_evidence_must_define_comparison_baseline(self) -> None:
        document = copy.deepcopy(self.document)
        direct = self._target(document, "direct_mv_hevc")
        balanced = self._mapping(direct, "balanced")
        balanced.update(
            status="measured",
            evidence=self._evidence(quality_delta=0.001, output_size_ratio=1.0),
        )

        with self.assertRaisesRegex(CalibrationError, "0.0 quality / 1.0 size baseline"):
            self._validate(document)

    def test_av1_cannot_expose_uncalibrated_ladder_steps(self) -> None:
        document = copy.deepcopy(self.document)
        av1 = self._target(document, "av1_sbs")
        detailed = self._mapping(av1, "detailed")
        detailed.update(
            status="measured",
            values={"crf": 28},
            evidence=self._evidence(quality_delta=0.01, output_size_ratio=1.2),
        )

        with self.assertRaisesRegex(CalibrationError, "exact-CRF-only"):
            self._validate(document)

    def test_require_complete_exits_nonzero_and_writes_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "receipt.json"

            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                exit_code = main(["--output", str(output), "--require-complete"])

            self.assertEqual(exit_code, 1)
            receipt = json.loads(output.read_text(encoding="utf-8"))
            self.assertFalse(receipt["complete"])
            self.assertNotIn(str(REPOSITORY_ROOT), output.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
