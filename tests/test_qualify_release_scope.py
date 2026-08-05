import hashlib
import json
import subprocess
import tempfile
import unittest

from contextlib import redirect_stdout
from datetime import date
from io import StringIO
from pathlib import Path
from typing import Any

from scripts.qualify_release_scope import (
    DEFAULT_POLICY_PATH,
    QualificationScopeError,
    classify_release_scope,
    load_policy,
    load_qualification_overrides,
    main,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
RC3_QUALIFICATION_PATH = REPO_ROOT / "docs" / "qualification" / "rc3-signed-qualification-v1.json"
CANDIDATE_SHA = "b" * 40
PRIOR_SHA = "a" * 40


class ReleaseQualificationScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = load_policy(DEFAULT_POLICY_PATH)

    def evidence(
        self,
        root: Path,
        case_id: str,
        *,
        source_sha: str = PRIOR_SHA,
        source: str = "signed_artifact_receipt",
        accepted_at: str = "2026-08-01T00:00:00Z",
        tier1: bool = False,
    ) -> dict[str, Any]:
        reference = Path("docs") / "qualification" / f"{case_id}-receipt.json"
        receipt_path = root / reference
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(json.dumps({"case_id": case_id, "passed": True}), encoding="utf-8")
        receipt: dict[str, Any] = {
            "case_id": case_id,
            "receipt_id": f"{case_id}-receipt-v1",
            "source": source,
            "status": "accepted",
            "source_sha": source_sha,
            "accepted_at": accepted_at,
            "reference": reference.as_posix(),
            "sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
        }
        if tier1:
            receipt["workflow_conclusion"] = "success"
            receipt["release_run_id"] = 12345
        return {"schema_version": 1, "receipts": [receipt]}

    def classify(
        self,
        evidence: dict[str, Any],
        root: Path,
        *,
        changed: set[str] | None = None,
        fresh: dict[str, bool] | None = None,
    ) -> dict[str, Any]:
        return classify_release_scope(
            self.policy,
            evidence,
            candidate_sha=CANDIDATE_SHA,
            changed_paths=lambda _base, _candidate: changed or set(),
            repo=root,
            reference_content=lambda reference: (root / reference).read_bytes(),
            fresh_retest=fresh,
            as_of=date(2026, 8, 5),
        )

    def result_for(self, report: dict[str, Any], case_id: str) -> dict[str, Any]:
        return next(case for case in report["cases"] if case["case_id"] == case_id)

    def test_policy_and_rc3_migration_cover_every_preregistered_case(self) -> None:
        qualification_id, overrides = load_qualification_overrides(
            RC3_QUALIFICATION_PATH,
            self.policy,
        )
        qualification = json.loads(RC3_QUALIFICATION_PATH.read_text(encoding="utf-8"))
        policy_case_ids = {case["id"] for case in self.policy["cases"]}
        mapped_case_ids = {case["policy_case_id"] for case in qualification["matrix"]}

        self.assertEqual(qualification_id, "rc3-signed-qualification-v1")
        self.assertEqual(mapped_case_ids, policy_case_ids)
        self.assertTrue(overrides["sparkle-update-route"])
        self.assertTrue(overrides["profile-save-action-accessibility"])
        self.assertFalse(overrides["gui-preview-cancel-cleanup"])
        self.assertNotIn(
            "public-diagnostics-and-field-closure",
            qualification["acceptance"]["required_case_ids"],
        )

    def test_contract_change_forces_retest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            evidence = self.evidence(root, "gui-preview-cancel-cleanup")
            report = self.classify(
                evidence,
                root,
                changed={"bd_to_avp/modules/preview.py"},
            )

        result = self.result_for(report, "gui-preview-cancel-cleanup")
        self.assertEqual(result["status"], "retest")
        self.assertEqual(result["invalidating_paths"], ["bd_to_avp/modules/preview.py"])

    def test_direct_path_pattern_forces_retest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            evidence = self.evidence(root, "network-generated-final-output")
            report = self.classify(
                evidence,
                root,
                changed={"bd_to_avp/modules/container.py"},
            )

        result = self.result_for(report, "network-generated-final-output")
        self.assertEqual(result["status"], "retest")
        self.assertEqual(result["invalidating_paths"], ["bd_to_avp/modules/container.py"])

    def test_unchanged_case_carries_named_prior_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            evidence = self.evidence(root, "overwrite-and-conversion-cancel")
            report = self.classify(evidence, root)

        result = self.result_for(report, "overwrite-and-conversion-cancel")
        self.assertEqual(result["status"], "carry")
        self.assertEqual(result["evidence"]["receipt_id"], "overwrite-and-conversion-cancel-receipt-v1")

    def test_missing_evidence_is_a_blocking_retest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            report = self.classify({"schema_version": 1, "receipts": []}, Path(temporary_directory))

        self.assertIn("gui-preview-failure-cleanup", report["blocking_retests"])
        self.assertFalse(report["passed"])

    def test_fresh_rc3_override_prevents_carry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            evidence = self.evidence(root, "malformed-pgs-parser-recovery")
            report = self.classify(
                evidence,
                root,
                fresh={"malformed-pgs-parser-recovery": True},
            )

        result = self.result_for(report, "malformed-pgs-parser-recovery")
        self.assertEqual(result["status"], "retest")
        self.assertIn("fresh targeted evidence", result["reason"])
        self.assertIsNone(result["evidence"]["receipt_id"])

    def test_tier1_requires_exact_successful_release_run_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            prior = self.evidence(
                root,
                "release-workflow-identity",
                source="release_run_receipt",
                tier1=True,
            )
            prior_report = self.classify(prior, root)
            exact = self.evidence(
                root,
                "release-workflow-identity",
                source_sha=CANDIDATE_SHA,
                source="release_run_receipt",
                tier1=True,
            )
            exact_report = self.classify(exact, root)

        self.assertEqual(self.result_for(prior_report, "release-workflow-identity")["status"], "retest")
        self.assertEqual(self.result_for(exact_report, "release-workflow-identity")["status"], "covered")

    def test_tier1_rejects_unsuccessful_or_unbound_release_run_receipt(self) -> None:
        for field, value in (("workflow_conclusion", "failure"), ("release_run_id", 0)):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary_directory:
                root = Path(temporary_directory)
                evidence = self.evidence(
                    root,
                    "release-workflow-identity",
                    source_sha=CANDIDATE_SHA,
                    source="release_run_receipt",
                    tier1=True,
                )
                evidence["receipts"][0][field] = value

                with self.assertRaises(QualificationScopeError):
                    self.classify(evidence, root)

    def test_periodic_evidence_expires_and_first_rc_forces_retest(self) -> None:
        policy = json.loads(json.dumps(self.policy))
        policy["cases"].append(
            {
                "id": "real-drive-qualification",
                "tier": 3,
                "blocking_phase": "milestone",
                "invalidates_on": {"paths": ["bd_to_avp/modules/disc.py"], "contracts": []},
                "allowed_evidence_sources": ["hardware_qualification_receipt"],
                "carry_forward": {
                    "allowed": True,
                    "requires_prior_accepted_receipt": True,
                    "requires_clean_invalidation": True,
                },
                "cadence": {
                    "first_rc_or_stable_candidate": True,
                    "max_age_days": 30,
                    "hardware": ["usb_bluray_drive"],
                },
            }
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            evidence = self.evidence(
                root,
                "real-drive-qualification",
                source="hardware_qualification_receipt",
                accepted_at="2026-06-01T00:00:00Z",
            )
            evidence["receipts"][0]["hardware"] = ["usb_bluray_drive"]
            report = classify_release_scope(
                policy,
                evidence,
                candidate_sha=CANDIDATE_SHA,
                changed_paths=lambda _base, _candidate: set(),
                repo=root,
                reference_content=lambda reference: (root / reference).read_bytes(),
                release_stage="rc",
                first_candidate_of_cycle=True,
                as_of=date(2026, 8, 5),
            )

        result = self.result_for(report, "real-drive-qualification")
        self.assertEqual(result["status"], "retest")
        self.assertIn("release milestone", result["reason"])

    def test_periodic_evidence_expires_outside_milestone(self) -> None:
        policy = json.loads(json.dumps(self.policy))
        policy["cases"].append(
            {
                "id": "clean-machine-installation",
                "tier": 3,
                "blocking_phase": "milestone",
                "invalidates_on": {"paths": ["scripts/macos_release.py"], "contracts": []},
                "allowed_evidence_sources": ["hardware_qualification_receipt"],
                "carry_forward": {
                    "allowed": True,
                    "requires_prior_accepted_receipt": True,
                    "requires_clean_invalidation": True,
                },
                "cadence": {
                    "first_rc_or_stable_candidate": True,
                    "max_age_days": 30,
                    "hardware": ["disposable_macos_vm"],
                },
            }
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            evidence = self.evidence(
                root,
                "clean-machine-installation",
                source="hardware_qualification_receipt",
                accepted_at="2026-06-01T00:00:00Z",
            )
            evidence["receipts"][0]["hardware"] = ["disposable_macos_vm"]
            report = classify_release_scope(
                policy,
                evidence,
                candidate_sha=CANDIDATE_SHA,
                changed_paths=lambda _base, _candidate: set(),
                repo=root,
                reference_content=lambda reference: (root / reference).read_bytes(),
                release_stage="beta",
                first_candidate_of_cycle=False,
                as_of=date(2026, 8, 5),
            )

        result = self.result_for(report, "clean-machine-installation")
        self.assertEqual(result["status"], "retest")
        self.assertIn("maximum age", result["reason"])

    def test_periodic_evidence_carries_with_matching_hardware_inside_cadence(self) -> None:
        policy = json.loads(json.dumps(self.policy))
        policy["cases"].append(
            {
                "id": "clean-machine-installation",
                "tier": 3,
                "blocking_phase": "milestone",
                "invalidates_on": {"paths": ["scripts/macos_release.py"], "contracts": []},
                "allowed_evidence_sources": ["hardware_qualification_receipt"],
                "carry_forward": {
                    "allowed": True,
                    "requires_prior_accepted_receipt": True,
                    "requires_clean_invalidation": True,
                },
                "cadence": {
                    "first_rc_or_stable_candidate": True,
                    "max_age_days": 30,
                    "hardware": ["disposable_macos_vm"],
                },
            }
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            evidence = self.evidence(
                root,
                "clean-machine-installation",
                source="hardware_qualification_receipt",
                accepted_at="2026-08-01T00:00:00Z",
            )
            evidence["receipts"][0]["hardware"] = ["disposable_macos_vm"]
            report = classify_release_scope(
                policy,
                evidence,
                candidate_sha=CANDIDATE_SHA,
                changed_paths=lambda _base, _candidate: set(),
                repo=root,
                reference_content=lambda reference: (root / reference).read_bytes(),
                release_stage="beta",
                first_candidate_of_cycle=False,
                as_of=date(2026, 8, 5),
            )

        self.assertEqual(self.result_for(report, "clean-machine-installation")["status"], "carry")

    def test_periodic_evidence_requires_matching_hardware(self) -> None:
        policy = json.loads(json.dumps(self.policy))
        policy["cases"].append(
            {
                "id": "real-drive-qualification",
                "tier": 3,
                "blocking_phase": "milestone",
                "invalidates_on": {"paths": ["bd_to_avp/modules/disc.py"], "contracts": []},
                "allowed_evidence_sources": ["hardware_qualification_receipt"],
                "carry_forward": {
                    "allowed": True,
                    "requires_prior_accepted_receipt": True,
                    "requires_clean_invalidation": True,
                },
                "cadence": {
                    "first_rc_or_stable_candidate": True,
                    "max_age_days": 30,
                    "hardware": ["usb_bluray_drive"],
                },
            }
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            evidence = self.evidence(
                root,
                "real-drive-qualification",
                source="hardware_qualification_receipt",
            )
            evidence["receipts"][0]["hardware"] = ["disposable_macos_vm"]

            with self.assertRaises(QualificationScopeError):
                classify_release_scope(
                    policy,
                    evidence,
                    candidate_sha=CANDIDATE_SHA,
                    changed_paths=lambda _base, _candidate: set(),
                    repo=root,
                    reference_content=lambda reference: (root / reference).read_bytes(),
                    as_of=date(2026, 8, 5),
                )

    def test_periodic_policy_rejects_noncanonical_cadence_types(self) -> None:
        for field, value in (("max_age_days", True), ("first_rc_or_stable_candidate", "false")):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary_directory:
                policy = json.loads(json.dumps(self.policy))
                cadence = {
                    "first_rc_or_stable_candidate": True,
                    "max_age_days": 30,
                    "hardware": ["usb_bluray_drive"],
                }
                cadence[field] = value
                policy["cases"].append(
                    {
                        "id": "real-drive-qualification",
                        "tier": 3,
                        "blocking_phase": "milestone",
                        "invalidates_on": {"paths": ["bd_to_avp/modules/disc.py"], "contracts": []},
                        "allowed_evidence_sources": ["hardware_qualification_receipt"],
                        "carry_forward": {
                            "allowed": True,
                            "requires_prior_accepted_receipt": True,
                            "requires_clean_invalidation": True,
                        },
                        "cadence": cadence,
                    }
                )
                policy_path = Path(temporary_directory) / "policy.json"
                policy_path.write_text(json.dumps(policy), encoding="utf-8")

                with self.assertRaises(QualificationScopeError):
                    load_policy(policy_path)

    def test_future_dated_receipt_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            evidence = self.evidence(
                root,
                "overwrite-and-conversion-cancel",
                accepted_at="2026-08-06T00:00:00Z",
            )

            with self.assertRaises(QualificationScopeError):
                self.classify(evidence, root)

    def test_untracked_evidence_reference_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            (root / "tracked.txt").write_text("tracked\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "initial"], cwd=root, check=True)
            evidence = self.evidence(
                root,
                "release-workflow-identity",
                source_sha=CANDIDATE_SHA,
                source="release_run_receipt",
                tier1=True,
            )

            with self.assertRaises(QualificationScopeError):
                classify_release_scope(
                    self.policy,
                    evidence,
                    candidate_sha=CANDIDATE_SHA,
                    changed_paths=lambda _base, _candidate: set(),
                    repo=root,
                    as_of=date(2026, 8, 5),
                )

    def test_tier4_is_external_and_nonblocking(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            report = self.classify({"schema_version": 1, "receipts": []}, Path(temporary_directory))

        result = self.result_for(report, "public-diagnostics-and-field-closure")
        self.assertEqual(result["status"], "external")
        self.assertFalse(result["blocking"])
        self.assertNotIn("public-diagnostics-and-field-closure", report["blocking_retests"])

    def test_require_evidence_returns_nonzero_for_blocking_retests(self) -> None:
        with redirect_stdout(StringIO()):
            exit_code = main(
                [
                    "--candidate-sha",
                    CANDIDATE_SHA,
                    "--require-evidence",
                    "--repo",
                    str(REPO_ROOT),
                ]
            )

        self.assertEqual(exit_code, 2)


if __name__ == "__main__":
    unittest.main()
