import hashlib
import json
import subprocess
import tempfile
import unittest

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
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
from scripts.release_receipt import (
    ArtifactReceiptExpectation,
    build_receipt,
    file_sha256,
    receipt_sha256,
    write_receipt,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
RC3_QUALIFICATION_PATH = REPO_ROOT / "docs" / "qualification" / "rc3-signed-qualification-v1.json"
CANDIDATE_SHA = "b" * 40
PRIOR_SHA = "a" * 40
DMG_SHA256 = "c" * 64
CHECKSUM_SHA256 = "d" * 64
APPCAST_SHA256 = "e" * 64
APP_TREE_SHA256 = "f" * 64


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
        workflow_phase: str = "preparation",
        artifact_receipt_path: Path | None = None,
        artifact_receipt_expectation: ArtifactReceiptExpectation | None = None,
    ) -> dict[str, Any]:
        return classify_release_scope(
            self.policy,
            evidence,
            candidate_sha=CANDIDATE_SHA,
            changed_paths=lambda _base, _candidate: changed or set(),
            repo=root,
            reference_content=lambda reference: (root / reference).read_bytes(),
            fresh_retest=fresh,
            workflow_phase=workflow_phase,
            artifact_receipt_path=artifact_receipt_path,
            artifact_receipt_expectation=artifact_receipt_expectation,
            as_of=date(2026, 8, 5),
        )

    def artifact_receipt(
        self,
        root: Path,
    ) -> tuple[Path, ArtifactReceiptExpectation]:
        facts: dict[str, object] = {
            "release_route": "prerelease",
            "source_sha": CANDIDATE_SHA,
            "workflow_actor": "shiny-code-bot",
            "workflow_run_id": 12345,
            "workflow_run_attempt": 2,
            "package_version": "0.3.0rc3",
            "public_version": "0.3.0-rc.3",
            "build_version": "160",
            "release_tag": "v0.3.0-rc.3",
            "release_name": "v0.3.0-rc.3",
            "release_id": 67890,
            "release_created_at": "2026-08-05T12:00:00Z",
            "prerelease": True,
            "make_latest": False,
            "signed_app_tree_sha256": APP_TREE_SHA256,
            "artifacts": [
                {
                    "kind": "dmg",
                    "name": "3D-Blu-ray-to-Vision-Pro-0.3.0-rc.3.dmg",
                    "sha256": DMG_SHA256,
                    "size_bytes": 1000,
                    "asset_id": 1,
                },
                {
                    "kind": "checksum",
                    "name": "SHA256SUMS",
                    "sha256": CHECKSUM_SHA256,
                    "size_bytes": 100,
                    "asset_id": 2,
                },
                {
                    "kind": "appcast",
                    "name": "appcast.xml",
                    "sha256": APPCAST_SHA256,
                    "size_bytes": 500,
                    "asset_id": 3,
                },
            ],
        }
        receipt_path = root / "release-receipt.json"
        write_receipt(build_receipt(facts), receipt_path)
        expectation = ArtifactReceiptExpectation(
            candidate_sha=CANDIDATE_SHA,
            release_route="prerelease",
            workflow_run_id=12345,
            workflow_run_attempt=2,
            release_id=67890,
            package_version="0.3.0rc3",
            public_version="0.3.0-rc.3",
            build_version="160",
            release_tag="v0.3.0-rc.3",
            dmg_name="3D-Blu-ray-to-Vision-Pro-0.3.0-rc.3.dmg",
            receipt_file_sha256=file_sha256(receipt_path),
            signed_app_tree_sha256=APP_TREE_SHA256,
            dmg_asset_id=1,
            dmg_sha256=DMG_SHA256,
            checksum_asset_id=2,
            checksum_sha256=CHECKSUM_SHA256,
            appcast_asset_id=3,
            appcast_sha256=APPCAST_SHA256,
        )
        return receipt_path, expectation

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

    def test_preparation_defers_visible_tier1_retests(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            report = self.classify({"schema_version": 1, "receipts": []}, Path(temporary_directory))

        result = self.result_for(report, "release-workflow-identity")
        self.assertEqual(report["workflow_phase"], "preparation")
        self.assertEqual(result["status"], "retest")
        self.assertFalse(result["applicable"])
        self.assertTrue(result["deferred"])
        self.assertTrue(result["evidence_required"])
        self.assertEqual(result["evidence_requirement"]["binding"], "exact_same_run_release_receipt")
        self.assertNotIn("release-workflow-identity", report["blocking_retests"])
        self.assertIn("release-workflow-identity", report["deferred_retests"])

    def test_artifact_phase_uses_exact_receipt_to_cover_tier1(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            receipt_path, expectation = self.artifact_receipt(root)
            report = self.classify(
                {"schema_version": 1, "receipts": []},
                root,
                workflow_phase="artifact",
                artifact_receipt_path=receipt_path,
                artifact_receipt_expectation=expectation,
            )

        result = self.result_for(report, "release-workflow-identity")
        self.assertTrue(result["applicable"])
        self.assertFalse(result["deferred"])
        self.assertEqual(result["status"], "covered")
        self.assertEqual(result["evidence"]["reference"], "release-receipt.json")
        self.assertNotIn("release-workflow-identity", report["blocking_retests"])
        self.assertEqual(report["deferred_retests"], [])

    def test_artifact_phase_fails_closed_without_receipt_and_requires_exact_policy_tier1_references(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with self.assertRaisesRegex(QualificationScopeError, "exact same-run release receipt"):
                classify_release_scope(
                    self.policy,
                    {"schema_version": 1, "receipts": []},
                    candidate_sha=CANDIDATE_SHA,
                    changed_paths=lambda _base, _candidate: set(),
                    repo=root,
                    reference_content=lambda reference: (root / reference).read_bytes(),
                    workflow_phase="artifact",
                )

            receipt_path, expectation = self.artifact_receipt(root)
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["tier1_case_references"].pop()
            receipt["receipt_sha256"] = receipt_sha256(receipt)
            write_receipt(receipt, receipt_path)
            expectation = replace(expectation, receipt_file_sha256=file_sha256(receipt_path))
            with self.assertRaisesRegex(QualificationScopeError, "explicit receipt verification mappings"):
                self.classify(
                    {"schema_version": 1, "receipts": []},
                    root,
                    workflow_phase="artifact",
                    artifact_receipt_path=receipt_path,
                    artifact_receipt_expectation=expectation,
                )

    def test_exact_checked_tier1_evidence_cannot_replace_same_run_artifact_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            evidence = self.evidence(
                root,
                "release-workflow-identity",
                source_sha=CANDIDATE_SHA,
                source="release_run_receipt",
                tier1=True,
            )

            with self.assertRaisesRegex(QualificationScopeError, "exact same-run release receipt"):
                self.classify(evidence, root, workflow_phase="artifact")

    def test_artifact_receipt_does_not_bypass_checked_evidence_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            evidence = self.evidence(root, "gui-preview-cancel-cleanup")
            evidence["receipts"][0]["sha256"] = "0" * 64
            receipt_path, expectation = self.artifact_receipt(root)

            with self.assertRaisesRegex(QualificationScopeError, "recorded SHA-256"):
                self.classify(
                    evidence,
                    root,
                    workflow_phase="artifact",
                    artifact_receipt_path=receipt_path,
                    artifact_receipt_expectation=expectation,
                )

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
                workflow_phase="preparation",
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
                workflow_phase="preparation",
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
                workflow_phase="preparation",
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
                    workflow_phase="preparation",
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
                    workflow_phase="preparation",
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
        stderr = StringIO()
        with redirect_stdout(StringIO()), redirect_stderr(stderr):
            exit_code = main(
                [
                    "--candidate-sha",
                    CANDIDATE_SHA,
                    "--workflow-phase",
                    "preparation",
                    "--require-evidence",
                    "--repo",
                    str(REPO_ROOT),
                ]
            )

        self.assertEqual(exit_code, 2)
        self.assertIn("gui-preview-failure-cleanup", stderr.getvalue())
        self.assertIn("signed_artifact_receipt", stderr.getvalue())
        self.assertIn("before this phase can pass", stderr.getvalue())

    def test_artifact_cli_accepts_exact_receipt_identity_flags(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            receipt_path, expectation = self.artifact_receipt(root)
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "--candidate-sha",
                        CANDIDATE_SHA,
                        "--workflow-phase",
                        "artifact",
                        "--release-receipt",
                        str(receipt_path),
                        "--release-route",
                        expectation.release_route,
                        "--workflow-run-id",
                        str(expectation.workflow_run_id),
                        "--workflow-run-attempt",
                        str(expectation.workflow_run_attempt),
                        "--release-id",
                        str(expectation.release_id),
                        "--package-version",
                        expectation.package_version,
                        "--public-version",
                        expectation.public_version,
                        "--build-version",
                        expectation.build_version,
                        "--release-tag",
                        expectation.release_tag,
                        "--dmg-name",
                        expectation.dmg_name,
                        "--release-receipt-sha256",
                        expectation.receipt_file_sha256,
                        "--signed-app-tree-sha256",
                        expectation.signed_app_tree_sha256,
                        "--dmg-asset-id",
                        str(expectation.dmg_asset_id),
                        "--dmg-sha256",
                        expectation.dmg_sha256,
                        "--checksum-asset-id",
                        str(expectation.checksum_asset_id),
                        "--checksum-sha256",
                        expectation.checksum_sha256,
                        "--appcast-asset-id",
                        str(expectation.appcast_asset_id),
                        "--appcast-sha256",
                        expectation.appcast_sha256,
                        "--repo",
                        str(REPO_ROOT),
                    ]
                )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["workflow_phase"], "artifact")
        self.assertEqual(self.result_for(payload, "release-workflow-identity")["status"], "covered")

    def test_artifact_cli_retains_structured_report_when_receipt_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "report.json"
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                exit_code = main(
                    [
                        "--candidate-sha",
                        CANDIDATE_SHA,
                        "--workflow-phase",
                        "artifact",
                        "--output",
                        str(output),
                    ]
                )

            report = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 1)
        self.assertFalse(report["passed"])
        self.assertEqual(report["workflow_phase"], "artifact")
        self.assertIn("exact same-run release receipt", report["error"])


if __name__ == "__main__":
    unittest.main()
