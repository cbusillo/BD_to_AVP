import copy
import hashlib
import json
import stat
import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from scripts.qualify_direct_mv_hevc import QualificationFailure
from scripts.qualify_direct_mv_hevc_mapping_confirmation import (
    DEFAULT_CONFIRMATION_PLAN,
    EXPECTED_CANDIDATES,
    EXPECTED_CASE_IDS,
    _finalize_receipt,
    _read_frozen_json,
    evaluate_confirmation,
    exit_code_for_confirmation,
    main,
    parse_confirmation_plan,
)
from scripts.qualify_direct_mv_hevc_quality_sweep import SweepCandidate, SweepPlan, _refresh_summaries
from scripts.qualify_mv_hevc_corpus import CorpusCase


def _plan():
    return parse_confirmation_plan(json.loads(DEFAULT_CONFIRMATION_PLAN.read_text(encoding="utf-8")))


def _run(run_index: int, quality: float, size: int) -> dict[str, object]:
    return {
        "run_index": run_index,
        "min_same_eye_ssim": quality,
        "final_bytes": size,
        "elapsed_seconds": 1.0,
        "min_eye_order_margin": 0.2,
        "sha256": hashlib.sha256(f"{run_index}-{quality}-{size}".encode()).hexdigest(),
    }


def _complete_evidence(*, collapsed_pair: tuple[str, str] | None = None, repeat_spread: bool = False):
    plan = _plan()
    sweep_plan = SweepPlan(
        sweep_id=plan.raw_sweep_id,
        corpus_path=plan.corpus_path,
        corpus_id=plan.corpus_id,
        corpus_sha256=plan.corpus_sha256,
        balanced_quality=0.7,
        runs_per_candidate=3,
        candidates=tuple(SweepCandidate(candidate.candidate_id, candidate.quality) for candidate in plan.candidates),
        relative_path="docs/qualification/direct-mv-hevc-quality-sweep-confirmation-v1.json",
    )
    definitions: dict[str, CorpusCase] = {}
    cases: list[dict[str, object]] = []
    for case_index, case_id in enumerate(EXPECTED_CASE_IDS):
        tags = ("animation",) if case_id == "synthetic-animation" else ("real_mvc",)
        if case_id in {"production-grain-rain", "production-snow-detail"}:
            tags = (*tags, "grain")
        definitions[case_id] = CorpusCase(
            case_id=case_id,
            tags=tags,
            source={"kind": "synthetic"},
            eye_width=2,
            eye_height=2,
            frame_rate="24",
            minimum_eye_order_margin=0.01,
        )
        candidate_records: list[dict[str, object]] = []
        for candidate_index, candidate in enumerate(plan.candidates):
            quality_step = candidate_index
            if collapsed_pair is not None and candidate.candidate_id == collapsed_pair[1]:
                lower_index = next(
                    index for index, planned in enumerate(plan.candidates) if planned.candidate_id == collapsed_pair[0]
                )
                quality_step = lower_index
            runs = []
            for run_index in range(3):
                score = 0.92 + case_index * 0.00001 + quality_step * 0.0002
                if repeat_spread and candidate.candidate_id == "q060" and run_index == 2:
                    score += 0.001
                runs.append(_run(run_index, score, 1000 + candidate_index * 100 + run_index))
            candidate_records.append(
                {
                    "id": candidate.candidate_id,
                    "quality": candidate.quality,
                    "runs": runs,
                    "summary": None,
                }
            )
        cases.append(
            {
                "id": case_id,
                "tags": list(tags),
                "quality_gate": True,
                "source": {"kind": "synthetic"},
                "prepared": {
                    "duration_seconds": 4.0,
                    "frame_count": 96,
                    "eye_width": 2,
                    "eye_height": 2,
                    "frame_rate": "24",
                },
                "candidates": candidate_records,
            }
        )
    evidence: dict[str, object] = {
        "schema_version": 1,
        "sweep_id": plan.raw_sweep_id,
        "updated_at": "2026-08-01T00:00:00+00:00",
        "source_git_sha": "a" * 40,
        "source_tree_dirty": False,
        "sweep_plan": {
            "path": "docs/qualification/direct-mv-hevc-quality-sweep-confirmation-v1.json",
            "sha256": plan.raw_sweep_sha256,
        },
        "manifest": {
            "path": "direct-mv-hevc-corpus-v1.json",
            "corpus_id": plan.corpus_id,
            "sha256": plan.corpus_sha256,
        },
        "selected_case_ids": list(EXPECTED_CASE_IDS),
        "candidates": [{"id": candidate.candidate_id, "quality": candidate.quality} for candidate in plan.candidates],
        "cases": cases,
        "candidate_summaries": [],
        "monotonicity_warnings": [],
    }
    _refresh_summaries(evidence, sweep_plan, definitions, all_gated_case_ids=set(definitions))
    sources = {
        "coarse": {"verified": True},
        "upper": {"verified": True},
        "automated_full_length_anchor": {"verified": True, "retained_artifacts": []},
    }
    return plan, evidence, sources


class DirectConfirmationPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.document = json.loads(DEFAULT_CONFIRMATION_PLAN.read_text(encoding="utf-8"))

    def test_plan_preregisters_exact_seven_step_mapping(self) -> None:
        plan = parse_confirmation_plan(self.document)

        self.assertEqual(
            tuple((candidate.step_id, candidate.candidate_id, candidate.quality) for candidate in plan.candidates),
            EXPECTED_CANDIDATES,
        )
        self.assertEqual(plan.thresholds.maximum_q085_to_q070_size_ratio, 4.9)
        self.assertEqual(plan.thresholds.sensitive_case_ids, ("production-grain-rain", "production-snow-detail"))

    def test_plan_rejects_candidate_or_threshold_changes(self) -> None:
        candidate_change = copy.deepcopy(self.document)
        candidate_change["candidates"][4]["quality"] = 0.76
        with self.assertRaisesRegex(QualificationFailure, "candidate mapping"):
            parse_confirmation_plan(candidate_change)

        threshold_change = copy.deepcopy(self.document)
        threshold_change["adjacent_boundary_policy"]["minimum_corpus_median_ssim_improvement"] = 0.0
        with self.assertRaisesRegex(QualificationFailure, "thresholds"):
            parse_confirmation_plan(threshold_change)

    def test_plan_forbids_public_mapping_changes(self) -> None:
        document = copy.deepcopy(self.document)
        document["decision_policy"]["public_mapping_changes_forbidden"] = False

        with self.assertRaisesRegex(QualificationFailure, "decision policy"):
            parse_confirmation_plan(document)


class FrozenReceiptTests(unittest.TestCase):
    def test_frozen_receipt_requires_exact_hash_and_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "receipt.json"
            data = b'{"ok":true}\n'
            path.write_bytes(data)
            digest = hashlib.sha256(data).hexdigest()

            with self.assertRaisesRegex(QualificationFailure, "0444"):
                _read_frozen_json(path, digest, "receipt")

            path.chmod(0o444)
            self.assertEqual(_read_frozen_json(path, digest, "receipt"), {"ok": True})
            with self.assertRaisesRegex(QualificationFailure, "SHA-256"):
                _read_frozen_json(path, "0" * 64, "receipt")

    def test_frozen_receipt_rejects_file_and_parent_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            real_directory = root / "real"
            real_directory.mkdir()
            receipt = real_directory / "receipt.json"
            data = b'{"ok":true}\n'
            receipt.write_bytes(data)
            receipt.chmod(0o444)
            digest = hashlib.sha256(data).hexdigest()
            file_link = root / "receipt-link.json"
            file_link.symlink_to(receipt)
            parent_link = root / "parent-link"
            parent_link.symlink_to(real_directory, target_is_directory=True)

            with self.assertRaisesRegex(QualificationFailure, "symlinks"):
                _read_frozen_json(file_link, digest, "receipt")
            with self.assertRaisesRegex(QualificationFailure, "symlinks"):
                _read_frozen_json(parent_link / "receipt.json", digest, "receipt")


class DirectConfirmationDecisionTests(unittest.TestCase):
    def test_complete_distinct_chain_selects_all_seven(self) -> None:
        plan, evidence, sources = _complete_evidence()

        confirmation = evaluate_confirmation(plan, evidence, sources)

        self.assertTrue(confirmation["acceptance"]["objective_decision_ready"])
        self.assertEqual(confirmation["acceptance"]["record_count"], 147)
        self.assertEqual(len(confirmation["boundary_evaluations"]), 6)
        self.assertTrue(all(boundary["boundary_passed"] for boundary in confirmation["boundary_evaluations"]))
        self.assertEqual(
            confirmation["selected_subset"]["candidate_ids"],
            [candidate.candidate_id for candidate in plan.candidates],
        )
        wrapped = dict(evidence)
        wrapped["confirmation"] = confirmation
        self.assertEqual(exit_code_for_confirmation(wrapped), 0)

    def test_failed_boundary_collapses_only_disconnected_positions(self) -> None:
        _, evidence, sources = _complete_evidence(collapsed_pair=("q040", "q050"))

        confirmation = evaluate_confirmation(_plan(), evidence, sources)

        self.assertFalse(confirmation["acceptance"]["objective_decision_ready"])
        self.assertEqual(
            confirmation["selected_subset"]["candidate_ids"],
            ["q050", "q060", "q070", "q075", "q080", "q085"],
        )
        mappings = {mapping["step_id"]: mapping for mapping in confirmation["provisional_mappings"]}
        self.assertEqual(mappings["space_saver"]["status"], "unsupported")
        self.assertEqual(mappings["compact"]["candidate_id"], "q050")

    def test_repeatability_failure_removes_candidate(self) -> None:
        plan, evidence, sources = _complete_evidence(repeat_spread=True)

        confirmation = evaluate_confirmation(plan, evidence, sources)
        q060 = next(summary for summary in confirmation["candidate_summaries"] if summary["candidate_id"] == "q060")

        self.assertFalse(q060["technically_eligible"])
        self.assertIn("repeat_ssim_spread", q060["failure_reasons"])
        self.assertFalse(confirmation["acceptance"]["all_seven_candidates_selected"])

    def test_exploratory_warning_flag_does_not_add_an_unregistered_gate(self) -> None:
        plan, evidence, sources = _complete_evidence()
        evidence["acceptance"]["passed"] = False

        confirmation = evaluate_confirmation(plan, evidence, sources)

        self.assertTrue(confirmation["acceptance"]["objective_decision_ready"])

    def test_finalization_is_canonical_read_only_and_idempotent(self) -> None:
        plan, evidence, sources = _complete_evidence()
        confirmation = evaluate_confirmation(plan, evidence, sources)
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "receipt.json"
            output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            _finalize_receipt(output, evidence, confirmation)

            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o444)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["confirmation"], confirmation)
            output.chmod(0o644)
            _finalize_receipt(output, evidence, confirmation)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o444)

    def test_complete_negative_finalizes_read_only_and_exits_one(self) -> None:
        plan, evidence, sources = _complete_evidence(collapsed_pair=("q040", "q050"))
        confirmation = evaluate_confirmation(plan, evidence, sources)
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "receipt.json"
            output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            _finalize_receipt(output, evidence, confirmation)

            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o444)
            self.assertEqual(exit_code_for_confirmation(evidence), 1)

    def test_finalization_rejects_symlink_output(self) -> None:
        plan, evidence, sources = _complete_evidence()
        confirmation = evaluate_confirmation(plan, evidence, sources)
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            target = root / "target.json"
            target.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            output = root / "receipt.json"
            output.symlink_to(target)

            with self.assertRaisesRegex(QualificationFailure, "symlinks"):
                _finalize_receipt(output, evidence, confirmation)

    def test_incomplete_receipt_exits_three(self) -> None:
        self.assertEqual(exit_code_for_confirmation({"acceptance": {"complete": False}}), 3)

    def test_keyboard_interrupt_exits_three(self) -> None:
        arguments = [
            "--coarse-receipt",
            "/tmp/coarse.json",
            "--upper-receipt",
            "/tmp/upper.json",
            "--anchor-receipt",
            "/tmp/anchor.json",
            "--output",
            "/tmp/output.json",
            "--work-directory",
            "/tmp/work",
        ]
        with patch(
            "scripts.qualify_direct_mv_hevc_mapping_confirmation.run_confirmation",
            side_effect=KeyboardInterrupt,
        ):
            self.assertEqual(main(arguments), 3)


if __name__ == "__main__":
    unittest.main()
