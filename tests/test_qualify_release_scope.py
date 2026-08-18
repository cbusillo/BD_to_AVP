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
    _matches_milestone_tier1_receipt,
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
from scripts.signed_artifact_receipt import build_receipt as build_signed_artifact_receipt
from scripts.signed_artifact_receipt import SignedArtifactReceiptExpectation
from scripts.tier3_receipt import build_receipt as build_tier3_receipt
from scripts.tier3_receipt import receipt_sha256 as tier3_receipt_sha256


REPO_ROOT = Path(__file__).resolve().parents[1]
RC3_QUALIFICATION_PATH = REPO_ROOT / "docs" / "qualification" / "rc3-signed-qualification-v1.json"
STABLE_QUALIFICATION_PATH = REPO_ROOT / "docs" / "qualification" / "stable-signed-qualification-v1.json"
CANDIDATE_SHA = "b" * 40
PRIOR_SHA = "a" * 40
DMG_SHA256 = "c" * 64
CHECKSUM_SHA256 = "d" * 64
APPCAST_SHA256 = "e" * 64
APP_TREE_SHA256 = "f" * 64


class ReleaseQualificationScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = load_policy(DEFAULT_POLICY_PATH)

    def checked_evidence_as_of(self, cutoff: date) -> dict[str, Any]:
        evidence = json.loads((REPO_ROOT / "docs/qualification/release-evidence-v1.json").read_text(encoding="utf-8"))
        evidence["receipts"] = [
            receipt for receipt in evidence["receipts"] if date.fromisoformat(receipt["accepted_at"][:10]) <= cutoff
        ]
        return evidence

    def test_milestone_tier1_receipt_accepts_explicit_successful_recovery(self) -> None:
        release_receipt = {"source_sha": CANDIDATE_SHA, "workflow": {"run_id": 12345}}
        evidence_receipt = {
            "source_sha": CANDIDATE_SHA,
            "release_run_id": 12345,
            "workflow_conclusion": "failure",
            "recovery_workflow_run": {
                "operation": "pypi_recovery",
                "workflow_conclusion": "success",
                "workflow_run_id": 54321,
            },
            "reference": "docs/release-evidence/v0.3.0/release-receipt.json",
            "sha256": "a" * 64,
        }

        self.assertTrue(
            _matches_milestone_tier1_receipt(
                evidence_receipt,
                release_receipt,
                receipt_reference="docs/release-evidence/v0.3.0/release-receipt.json",
                receipt_file_sha256="a" * 64,
            )
        )
        evidence_receipt["recovery_workflow_run"]["operation"] = "unexpected"
        self.assertFalse(
            _matches_milestone_tier1_receipt(
                evidence_receipt,
                release_receipt,
                receipt_reference="docs/release-evidence/v0.3.0/release-receipt.json",
                receipt_file_sha256="a" * 64,
            )
        )

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
        signed_artifact_receipt_path: Path | None = None,
        signed_artifact_receipt_file_sha256: str | None = None,
        milestone_receipt_path: Path | None = None,
        release_stage: str = "rc",
        first_candidate_of_cycle: bool = False,
        as_of: date = date(2026, 8, 5),
    ) -> dict[str, Any]:
        return classify_release_scope(
            self.policy,
            evidence,
            candidate_sha=CANDIDATE_SHA,
            changed_paths=lambda _base, _candidate: changed or set(),
            repo=root,
            reference_content=lambda reference: (root / reference).read_bytes(),
            fresh_retest=fresh,
            release_stage=release_stage,
            first_candidate_of_cycle=first_candidate_of_cycle,
            workflow_phase=workflow_phase,
            artifact_receipt_path=artifact_receipt_path,
            artifact_receipt_expectation=artifact_receipt_expectation,
            signed_artifact_receipt_path=signed_artifact_receipt_path,
            signed_artifact_receipt_file_sha256=signed_artifact_receipt_file_sha256,
            milestone_receipt_path=milestone_receipt_path,
            as_of=as_of,
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
            receipt_asset_id=4,
            receipt_file_sha256=file_sha256(receipt_path),
            receipt_sha256=json.loads(receipt_path.read_text(encoding="utf-8"))["receipt_sha256"],
            signed_app_tree_sha256=APP_TREE_SHA256,
            dmg_asset_id=1,
            dmg_size=1000,
            dmg_sha256=DMG_SHA256,
            checksum_asset_id=2,
            checksum_sha256=CHECKSUM_SHA256,
            appcast_asset_id=3,
            appcast_sha256=APPCAST_SHA256,
        )
        return receipt_path, expectation

    def signed_artifact_receipt(self, root: Path, expectation: ArtifactReceiptExpectation) -> Path:
        signed_expectation = SignedArtifactReceiptExpectation(
            policy_id=self.policy["policy_id"],
            case_id="profile-save-action-accessibility",
            candidate_sha=expectation.candidate_sha,
            workflow_run_id=expectation.workflow_run_id,
            workflow_run_attempt=expectation.workflow_run_attempt,
            release_id=expectation.release_id,
            release_receipt_asset_id=expectation.receipt_asset_id,
            release_receipt_file_sha256=expectation.receipt_file_sha256,
            release_receipt_sha256=expectation.receipt_sha256,
            package_version=expectation.package_version,
            public_version=expectation.public_version,
            build_version=expectation.build_version,
            release_tag=expectation.release_tag,
            dmg_asset_id=expectation.dmg_asset_id,
            dmg_name=expectation.dmg_name,
            dmg_size=expectation.dmg_size,
            dmg_sha256=expectation.dmg_sha256,
            signed_app_tree_sha256=expectation.signed_app_tree_sha256,
        )
        receipt = build_signed_artifact_receipt(
            expectation=signed_expectation,
            evidence={
                "accessibility-tree": "1" * 64,
                "screenshot-dark": "2" * 64,
                "screenshot-light": "3" * 64,
                "ui-result": "4" * 64,
            },
        )
        path = root / "signed-artifact-ui-receipt.json"
        path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path

    def milestone_receipt(self, root: Path) -> Path:
        receipt_path = self.artifact_receipt(root)[0]
        self.write_milestone_publication(root, receipt_path)
        return receipt_path

    @staticmethod
    def write_milestone_publication(root: Path, receipt_path: Path) -> None:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        publication_path = root / f"docs/release-evidence/{receipt['release']['tag']}/publication-record.json"
        publication_path.parent.mkdir(parents=True, exist_ok=True)
        publication_path.write_text("{}\n", encoding="utf-8")

    @staticmethod
    def milestone_receipt_from_tier3(root: Path, evidence: dict[str, Any]) -> Path:
        tier3_path = root / evidence["receipts"][0]["reference"]
        tier3_receipt = json.loads(tier3_path.read_text(encoding="utf-8"))
        receipt_path = root / tier3_receipt["release_identity"]["release_receipt"]["reference"]
        ReleaseQualificationScopeTests.write_milestone_publication(root, receipt_path)
        return receipt_path

    def tier3_evidence(
        self,
        root: Path,
        case_id: str,
        *,
        source_sha: str = PRIOR_SHA,
        completed_at: str = "2026-08-01T00:00:00Z",
        accepted_at: str = "2026-08-01T00:10:00Z",
        result_status: str = "passed",
    ) -> dict[str, Any]:
        case = next(case for case in self.policy["cases"] if case["id"] == case_id)
        release_facts: dict[str, object] = {
            "release_route": "prerelease",
            "source_sha": source_sha,
            "workflow_actor": "shiny-code-bot",
            "workflow_run_id": 12345,
            "workflow_run_attempt": 1,
            "package_version": "0.3.0rc3",
            "public_version": "0.3.0-rc.3",
            "build_version": "160",
            "release_tag": "v0.3.0-rc.3",
            "release_name": "v0.3.0-rc.3",
            "release_id": 67890,
            "release_created_at": "2026-06-01T00:00:00Z",
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
        release_reference = Path("docs") / "release-evidence" / case_id / "release-receipt.json"
        release_path = root / release_reference
        release_path.parent.mkdir(parents=True, exist_ok=True)
        release_receipt = build_receipt(release_facts)
        write_receipt(release_receipt, release_path)

        identity_values = {
            "vendor_id": "05e3",
            "product_id": "0749",
            "transport": "usb",
            "makemkv_version": "1.18.1",
            "model_family": "vision-pro",
            "chip_family": "m2",
            "visionos_major": "26",
        }
        hardware = case["hardware"]
        hardware_receipt = (
            {
                "class": hardware["classes"][0],
                "identity": {field: identity_values[field] for field in hardware["identity_fields"]},
            }
            if hardware["required"]
            else {"class": "none", "identity": {}}
        )
        assertion_status = (
            "passed" if result_status == "passed" else "failed" if result_status == "failed" else "not_run"
        )
        tier3_facts = {
            "assertions": {assertion_id: assertion_status for assertion_id in case["required_assertions"]},
            "cleanup": {"status": case["allowed_cleanup_results"][0], "evidence_sha256": "1" * 64},
            "completed_at": completed_at,
            "environment": {
                "environment_class": case["environment"]["classes"][0],
                "architecture": "arm64",
                "macos_version": "26.0",
                "macos_build": "25A123",
            },
            "evidence": (
                [{"kind": case["allowed_evidence_kinds"][0], "sha256": "2" * 64}]
                if result_status in {"passed", "failed"}
                else []
            ),
            "evidence_source": case["allowed_evidence_sources"][0],
            "hardware": hardware_receipt,
            "release_receipt_file_sha256": file_sha256(release_path),
            "release_receipt_reference": release_reference.as_posix(),
            "result": {
                "status": result_status,
                "reason_code": "all_assertions_passed" if result_status == "passed" else "operator-deferred",
            },
            "started_at": completed_at,
        }
        tier3_receipt = build_tier3_receipt(
            tier3_facts,
            policy_id=self.policy["policy_id"],
            case=case,
            release_receipt=release_receipt,
        )
        reference = Path("docs") / "qualification" / f"{case_id}-tier3.json"
        receipt_path = root / reference
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(json.dumps(tier3_receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return {
            "schema_version": 1,
            "receipts": [
                {
                    "case_id": case_id,
                    "receipt_id": f"{case_id}-receipt-v1",
                    "source": case["allowed_evidence_sources"][0],
                    "status": "accepted" if result_status == "passed" else result_status,
                    "source_sha": source_sha,
                    "accepted_at": accepted_at,
                    "reference": reference.as_posix(),
                    "sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
                }
            ],
        }

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
        self.assertTrue(mapped_case_ids.issubset(policy_case_ids))
        self.assertTrue({case["id"] for case in self.policy["cases"] if case["tier"] == 3}.isdisjoint(mapped_case_ids))
        self.assertTrue(overrides["sparkle-update-route"])
        self.assertTrue(overrides["profile-save-action-accessibility"])
        self.assertFalse(overrides["gui-preview-cancel-cleanup"])
        self.assertNotIn(
            "public-diagnostics-and-field-closure",
            qualification["acceptance"]["required_case_ids"],
        )

    def test_stable_migration_separates_release_blockers_from_operational_evidence(self) -> None:
        qualification_id, overrides = load_qualification_overrides(
            STABLE_QUALIFICATION_PATH,
            self.policy,
        )
        qualification = json.loads(STABLE_QUALIFICATION_PATH.read_text(encoding="utf-8"))
        acceptance = qualification["acceptance"]
        optional_cases = {
            "native-sparkle-release-notes",
            "usb-bluray-makemkv",
            "protected-real-media-conversion",
            "vision-pro-physical-playback",
        }

        self.assertEqual(qualification_id, "stable-signed-qualification-v1")
        self.assertTrue(optional_cases.issubset(acceptance["nonblocking_case_ids"]))
        self.assertTrue(optional_cases.isdisjoint(acceptance["required_case_ids"]))
        self.assertTrue(optional_cases.isdisjoint(acceptance["blocking_case_ids"]))
        self.assertEqual(
            set(acceptance["blocking_case_ids"]),
            {"sparkle-update-route", "clean-machine-signed-update", "installed-ui-accessibility"},
        )
        self.assertTrue(overrides["sparkle-update-route"])
        self.assertTrue(overrides["clean-machine-signed-update"])
        self.assertTrue(overrides["installed-ui-accessibility"])
        self.assertTrue(all(not overrides[case_id] for case_id in optional_cases))

    def test_nonblocking_fresh_retest_requires_bounded_reason(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            qualification = json.loads(STABLE_QUALIFICATION_PATH.read_text(encoding="utf-8"))
            physical = next(case for case in qualification["matrix"] if case["policy_case_id"] == "usb-bluray-makemkv")
            physical["migration"] = "fresh_retest"
            path = Path(temporary_directory) / "qualification.json"
            path.write_text(json.dumps(qualification), encoding="utf-8")

            with self.assertRaisesRegex(QualificationScopeError, "fresh_retest entries require"):
                load_qualification_overrides(path, self.policy)

            physical["retest_reason"] = "device_family_changed"
            path.write_text(json.dumps(qualification), encoding="utf-8")
            _, overrides = load_qualification_overrides(path, self.policy)

        self.assertTrue(overrides["usb-bluray-makemkv"])

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

        sparkle = self.result_for(report, "sparkle-update-route")
        clean_machine = self.result_for(report, "clean-machine-signed-update")
        self.assertEqual(sparkle["required_phase"], "milestone")
        self.assertTrue(sparkle["requires_live_publication"])
        self.assertTrue(sparkle["deferred"])
        self.assertFalse(sparkle["applicable"])
        self.assertEqual(clean_machine["required_phase"], "milestone")
        self.assertFalse(clean_machine["requires_live_publication"])

        profile = self.result_for(report, "profile-save-action-accessibility")
        self.assertEqual(profile["tier"], 2)
        self.assertEqual(profile["blocking_phase"], "publication")
        self.assertEqual(profile["required_phase"], "artifact")
        self.assertTrue(profile["deferred"])
        self.assertFalse(profile["applicable"])
        self.assertIn("profile-save-action-accessibility", report["deferred_retests"])

    def test_artifact_phase_uses_exact_receipt_to_cover_tier1(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            receipt_path, expectation = self.artifact_receipt(root)
            signed_receipt_path = self.signed_artifact_receipt(root, expectation)
            report = self.classify(
                {"schema_version": 1, "receipts": []},
                root,
                workflow_phase="artifact",
                artifact_receipt_path=receipt_path,
                artifact_receipt_expectation=expectation,
                signed_artifact_receipt_path=signed_receipt_path,
                signed_artifact_receipt_file_sha256=hashlib.sha256(signed_receipt_path.read_bytes()).hexdigest(),
            )

        result = self.result_for(report, "release-workflow-identity")
        profile = self.result_for(report, "profile-save-action-accessibility")
        self.assertTrue(result["applicable"])
        self.assertFalse(result["deferred"])
        self.assertEqual(result["status"], "covered")
        self.assertEqual(result["evidence"]["reference"], "release-receipt.json")
        self.assertTrue(profile["applicable"])
        self.assertEqual(profile["status"], "covered")
        self.assertEqual(profile["trigger"], "exact_signed_artifact")
        self.assertEqual(profile["evidence"]["reference"], "signed-artifact-ui-receipt.json")
        self.assertNotIn("release-workflow-identity", report["blocking_retests"])
        self.assertNotIn("profile-save-action-accessibility", report["blocking_retests"])
        self.assertIn("sparkle-update-route", report["deferred_retests"])
        self.assertIn("native-sparkle-release-notes", report["deferred_retests"])

    def test_artifact_phase_requires_exact_signed_ui_receipt_for_profile_case(self) -> None:
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

        profile = self.result_for(report, "profile-save-action-accessibility")
        self.assertEqual(profile["status"], "retest")
        self.assertEqual(profile["trigger"], "missing_exact_signed_artifact")
        self.assertTrue(profile["applicable"])
        self.assertIn("profile-save-action-accessibility", report["blocking_retests"])

    def test_artifact_phase_rejects_replayed_signed_ui_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            receipt_path, expectation = self.artifact_receipt(root)
            signed_receipt_path = self.signed_artifact_receipt(root, expectation)
            replayed = replace(expectation, workflow_run_attempt=3)

            with self.assertRaisesRegex(QualificationScopeError, "workflow"):
                self.classify(
                    {"schema_version": 1, "receipts": []},
                    root,
                    workflow_phase="artifact",
                    artifact_receipt_path=receipt_path,
                    artifact_receipt_expectation=replayed,
                    signed_artifact_receipt_path=signed_receipt_path,
                    signed_artifact_receipt_file_sha256=hashlib.sha256(signed_receipt_path.read_bytes()).hexdigest(),
                )

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

    def test_milestone_uses_checked_tier1_evidence_and_rejects_artifact_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            receipt_path, expectation = self.artifact_receipt(root)
            self.write_milestone_publication(root, receipt_path)
            evidence = {
                "schema_version": 1,
                "receipts": [
                    {
                        "case_id": "release-workflow-identity",
                        "receipt_id": "release-workflow-identity-receipt-v1",
                        "source": "release_run_receipt",
                        "status": "accepted",
                        "source_sha": CANDIDATE_SHA,
                        "accepted_at": "2026-08-05T13:00:00Z",
                        "reference": receipt_path.relative_to(root).as_posix(),
                        "sha256": file_sha256(receipt_path),
                        "workflow_conclusion": "success",
                        "release_run_id": expectation.workflow_run_id,
                    }
                ],
            }
            report = self.classify(
                evidence,
                root,
                workflow_phase="milestone",
                release_stage="stable",
                milestone_receipt_path=receipt_path,
            )
            with self.assertRaisesRegex(QualificationScopeError, "Milestone workflow phase"):
                self.classify(
                    evidence,
                    root,
                    workflow_phase="milestone",
                    artifact_receipt_path=receipt_path,
                    artifact_receipt_expectation=expectation,
                    milestone_receipt_path=receipt_path,
                )

        tier1 = self.result_for(report, "release-workflow-identity")
        sparkle = self.result_for(report, "sparkle-update-route")
        clean_machine = self.result_for(report, "clean-machine-signed-update")
        self.assertEqual(tier1["status"], "covered")
        self.assertTrue(tier1["applicable"])
        self.assertFalse(tier1["deferred"])
        self.assertTrue(sparkle["applicable"])
        self.assertIn("sparkle-update-route", report["blocking_retests"])
        self.assertTrue(clean_machine["applicable"])

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

    def test_first_rc_forces_only_declared_tier3_cases_to_retest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            clean_evidence = self.tier3_evidence(root, "clean-machine-signed-update")
            clean_report = self.classify(
                clean_evidence,
                root,
                release_stage="rc",
                first_candidate_of_cycle=True,
                workflow_phase="milestone",
                milestone_receipt_path=self.milestone_receipt(root),
            )
            empty_report = self.classify(
                {"schema_version": 1, "receipts": []},
                root,
                release_stage="rc",
                first_candidate_of_cycle=True,
                workflow_phase="milestone",
                milestone_receipt_path=self.milestone_receipt(root),
            )

        clean_result = self.result_for(clean_report, "clean-machine-signed-update")
        real_media_result = self.result_for(empty_report, "protected-real-media-conversion")
        self.assertEqual(clean_result["status"], "retest")
        self.assertEqual(clean_result["trigger"], "first_rc")
        self.assertTrue(clean_result["applicable"])
        self.assertEqual(real_media_result["trigger"], "not_due")
        self.assertFalse(real_media_result["applicable"])
        self.assertFalse(real_media_result["evidence_required"])

    def test_stable_does_not_force_operator_hardware_retests(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            report = self.classify(
                {"schema_version": 1, "receipts": []},
                root,
                release_stage="stable",
                workflow_phase="milestone",
                milestone_receipt_path=self.milestone_receipt(root),
            )

        for case_id in (
            "usb-bluray-makemkv",
            "protected-real-media-conversion",
            "vision-pro-physical-playback",
        ):
            with self.subTest(case_id=case_id):
                result = self.result_for(report, case_id)
                self.assertEqual(result["trigger"], "not_due")
                self.assertEqual(result["operational_status"], "missing")
                self.assertFalse(result["blocking"])
                self.assertFalse(result["applicable"])
                self.assertFalse(result["evidence_due"])
                self.assertFalse(result["evidence_required"])
                self.assertNotIn(case_id, report["blocking_retests"])

        automated = self.result_for(report, "clean-machine-signed-update")
        self.assertEqual(automated["trigger"], "stable_candidate")
        self.assertTrue(automated["blocking"])
        self.assertTrue(automated["evidence_due"])
        self.assertTrue(automated["evidence_required"])
        self.assertIn("clean-machine-signed-update", report["blocking_retests"])

    def test_explicit_operator_and_presentation_retests_are_visible_but_nonblocking(self) -> None:
        optional_cases = {
            "native-sparkle-release-notes": True,
            "usb-bluray-makemkv": True,
            "protected-real-media-conversion": True,
            "vision-pro-physical-playback": True,
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            report = self.classify(
                {"schema_version": 1, "receipts": []},
                root,
                fresh=optional_cases,
                release_stage="stable",
                workflow_phase="milestone",
                milestone_receipt_path=self.milestone_receipt(root),
            )

        for case_id in optional_cases:
            with self.subTest(case_id=case_id):
                result = self.result_for(report, case_id)
                self.assertEqual(result["status"], "retest")
                self.assertEqual(result["operational_status"], "due")
                self.assertFalse(result["blocking"])
                self.assertTrue(result["applicable"])
                self.assertTrue(result["evidence_due"])
                self.assertFalse(result["evidence_required"])
                self.assertTrue(result["evidence_requirement"]["due_for_phase"])
                self.assertFalse(result["evidence_requirement"]["required_for_phase"])
                self.assertNotIn(case_id, report["blocking_retests"])

    def test_failed_and_skipped_nonblocking_operator_receipts_remain_visible(self) -> None:
        for outcome in ("failed", "skipped"):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as temporary_directory:
                root = Path(temporary_directory)
                evidence = self.tier3_evidence(
                    root,
                    "usb-bluray-makemkv",
                    source_sha=CANDIDATE_SHA,
                    result_status=outcome,
                )
                report = self.classify(
                    evidence,
                    root,
                    fresh={"usb-bluray-makemkv": True},
                    release_stage="stable",
                    workflow_phase="milestone",
                    milestone_receipt_path=self.milestone_receipt(root),
                )

            result = self.result_for(report, "usb-bluray-makemkv")
            self.assertEqual(result["operational_status"], outcome)
            self.assertEqual(result["evidence"]["receipt_id"], "usb-bluray-makemkv-receipt-v1")
            self.assertFalse(result["blocking"])
            self.assertNotIn("usb-bluray-makemkv", report["blocking_retests"])

    def test_recorded_nonblocking_outcome_requires_exact_candidate_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            evidence = self.tier3_evidence(
                root,
                "usb-bluray-makemkv",
                source_sha=PRIOR_SHA,
                result_status="failed",
            )

            with self.assertRaisesRegex(QualificationScopeError, "exact candidate SHA"):
                self.classify(
                    evidence,
                    root,
                    fresh={"usb-bluray-makemkv": True},
                    release_stage="stable",
                    workflow_phase="milestone",
                    milestone_receipt_path=self.milestone_receipt(root),
                )

    def test_failed_native_presentation_is_exact_source_visible_and_nonblocking(self) -> None:
        candidate_sha = "0b06582a83a45bb38d851e62ccf38cd148c7bb95"
        reference = "docs/qualification/rc3-targeted-qualification-v1.json"
        document = json.loads((REPO_ROOT / reference).read_text(encoding="utf-8"))
        case = next(item for item in document["cases"] if item["id"] == "native-sparkle-notes-rc3")
        case["result"] = "failed"
        reference_bytes = (json.dumps(document, indent=2) + "\n").encode()
        evidence = {
            "schema_version": 1,
            "receipts": [
                {
                    "case_id": "native-sparkle-release-notes",
                    "receipt_id": "native-sparkle-release-notes-failed-v1",
                    "source": "signed_artifact_receipt",
                    "status": "failed",
                    "source_sha": candidate_sha,
                    "accepted_at": "2026-08-05T10:30:00Z",
                    "reference": reference,
                    "sha256": hashlib.sha256(reference_bytes).hexdigest(),
                }
            ],
        }
        report = classify_release_scope(
            self.policy,
            evidence,
            candidate_sha=candidate_sha,
            changed_paths=lambda _base, _candidate: set(),
            repo=REPO_ROOT,
            reference_content=lambda path: reference_bytes if path == reference else (REPO_ROOT / path).read_bytes(),
            fresh_retest={"native-sparkle-release-notes": True},
            release_stage="rc",
            workflow_phase="milestone",
            milestone_receipt_path=REPO_ROOT / "docs/release-evidence/v0.3.0-rc.3/release-receipt.json",
            as_of=date(2026, 8, 9),
        )

        result = self.result_for(report, "native-sparkle-release-notes")
        self.assertEqual(result["operational_status"], "failed")
        self.assertFalse(result["blocking"])
        self.assertNotIn("native-sparkle-release-notes", report["blocking_retests"])

    def test_periodic_evidence_expires_outside_milestone(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            evidence = self.tier3_evidence(
                root,
                "clean-machine-signed-update",
                completed_at="2026-06-01T00:00:00Z",
                accepted_at="2026-06-01T00:10:00Z",
            )
            report = self.classify(
                evidence,
                root,
                release_stage="beta",
                workflow_phase="milestone",
                milestone_receipt_path=self.milestone_receipt(root),
                as_of=date(2026, 8, 5),
            )

        result = self.result_for(report, "clean-machine-signed-update")
        self.assertEqual(result["status"], "retest")
        self.assertEqual(result["trigger"], "expired")
        self.assertTrue(result["applicable"])

    def test_exact_candidate_tier3_evidence_still_expires(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            evidence = self.tier3_evidence(
                root,
                "clean-machine-signed-update",
                source_sha=CANDIDATE_SHA,
                completed_at="2026-06-01T00:00:00Z",
                accepted_at="2026-06-01T00:10:00Z",
            )
            report = self.classify(
                evidence,
                root,
                release_stage="beta",
                workflow_phase="milestone",
                milestone_receipt_path=self.milestone_receipt_from_tier3(root, evidence),
                as_of=date(2026, 8, 5),
            )

        result = self.result_for(report, "clean-machine-signed-update")
        self.assertEqual(result["status"], "retest")
        self.assertEqual(result["trigger"], "expired")
        self.assertTrue(result["applicable"])

    def test_exact_candidate_tier3_evidence_must_match_milestone_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            evidence = self.tier3_evidence(
                root,
                "clean-machine-signed-update",
                source_sha=CANDIDATE_SHA,
            )
            report = self.classify(
                evidence,
                root,
                release_stage="beta",
                workflow_phase="milestone",
                milestone_receipt_path=self.milestone_receipt(root),
            )

        result = self.result_for(report, "clean-machine-signed-update")
        self.assertEqual(result["status"], "retest")
        self.assertEqual(result["trigger"], "milestone_receipt_mismatch")

    def test_periodic_evidence_carries_inside_cadence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            evidence = self.tier3_evidence(root, "clean-machine-signed-update")
            report = self.classify(
                evidence,
                root,
                release_stage="beta",
                workflow_phase="milestone",
                milestone_receipt_path=self.milestone_receipt(root),
            )

        result = self.result_for(report, "clean-machine-signed-update")
        self.assertEqual(result["status"], "carry")
        self.assertEqual(result["trigger"], "cadence_valid")
        self.assertFalse(result["applicable"])
        self.assertEqual(result["evidence"]["environment_class"], "resettable-vm")

    def test_tier3_invalidation_triggers_outside_milestone(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            evidence = self.tier3_evidence(root, "clean-machine-signed-update")
            report = self.classify(
                evidence,
                root,
                changed={"scripts/sparkle_bundle.py"},
                release_stage="beta",
                workflow_phase="milestone",
                milestone_receipt_path=self.milestone_receipt(root),
            )

        result = self.result_for(report, "clean-machine-signed-update")
        self.assertEqual(result["status"], "retest")
        self.assertEqual(result["trigger"], "invalidated")
        self.assertTrue(result["applicable"])

    def test_tier3_evidence_rejects_mismatched_hardware_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            evidence = self.tier3_evidence(root, "usb-bluray-makemkv")
            receipt_path = root / evidence["receipts"][0]["reference"]
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["hardware"]["class"] = "vision-pro"
            receipt["receipt_sha256"] = tier3_receipt_sha256(receipt)
            receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            evidence["receipts"][0]["sha256"] = hashlib.sha256(receipt_path.read_bytes()).hexdigest()

            with self.assertRaisesRegex(QualificationScopeError, "hardware class"):
                self.classify(evidence, root, release_stage="beta")

    def test_periodic_policy_rejects_noncanonical_cadence_and_expiry(self) -> None:
        mutations = (
            ("cadence", "first_rc", "yes"),
            ("evidence_expiry", "max_age_days", True),
        )
        for section, field, value in mutations:
            with self.subTest(section=section, field=field), tempfile.TemporaryDirectory() as temporary_directory:
                policy = json.loads(json.dumps(self.policy))
                case = next(case for case in policy["cases"] if case["id"] == "clean-machine-signed-update")
                case[section][field] = value
                policy_path = Path(temporary_directory) / "policy.json"
                policy_path.write_text(json.dumps(policy), encoding="utf-8")

                with self.assertRaises(QualificationScopeError):
                    load_policy(policy_path)

    def test_policy_rejects_nonblocking_automated_tier3_case(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            policy = json.loads(json.dumps(self.policy))
            case = next(case for case in policy["cases"] if case["id"] == "clean-machine-signed-update")
            case["release_blocking"] = False
            policy_path = Path(temporary_directory) / "policy.json"
            policy_path.write_text(json.dumps(policy), encoding="utf-8")

            with self.assertRaisesRegex(QualificationScopeError, "not approved as nonblocking"):
                load_policy(policy_path)

    def test_milestone_owned_tier2_requires_live_publication_declaration(self) -> None:
        mutations = (
            ("sparkle-update-route", "requires_live_publication", False),
            ("sparkle-update-route", "live_evidence_case_ids", ["updater-route-rc2-to-rc3"]),
            ("native-sparkle-release-notes", "evidence_role", "automated"),
        )
        for case_id, field, value in mutations:
            with self.subTest(case_id=case_id, field=field), tempfile.TemporaryDirectory() as temporary_directory:
                policy = json.loads(json.dumps(self.policy))
                case = next(case for case in policy["cases"] if case["id"] == case_id)
                case[field] = value
                policy_path = Path(temporary_directory) / "policy.json"
                policy_path.write_text(json.dumps(policy), encoding="utf-8")

                with self.assertRaises(QualificationScopeError):
                    load_policy(policy_path)

    def test_policy_rejects_nonblocking_tier1_case(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            policy = json.loads(json.dumps(self.policy))
            case = next(case for case in policy["cases"] if case["id"] == "release-workflow-identity")
            case["release_blocking"] = False
            policy_path = Path(temporary_directory) / "policy.json"
            policy_path.write_text(json.dumps(policy), encoding="utf-8")

            with self.assertRaisesRegex(QualificationScopeError, "must remain release blocking"):
                load_policy(policy_path)

    def test_policy_rejects_demoting_unapproved_live_automation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            policy = json.loads(json.dumps(self.policy))
            case = next(case for case in policy["cases"] if case["id"] == "sparkle-update-route")
            case["release_blocking"] = False
            case["evidence_role"] = "operator_presentation"
            policy_path = Path(temporary_directory) / "policy.json"
            policy_path.write_text(json.dumps(policy), encoding="utf-8")

            with self.assertRaisesRegex(QualificationScopeError, "not approved as nonblocking"):
                load_policy(policy_path)

    def test_artifact_owned_tier2_requires_explicit_publication_contract(self) -> None:
        mutations = (
            ("profile-save-action-accessibility", "artifact_owned", False),
            ("profile-save-action-accessibility", "blocking_phase", "release_candidate"),
            ("profile-save-action-accessibility", "allowed_evidence_sources", ["release_run_receipt"]),
        )
        for case_id, field, value in mutations:
            with self.subTest(case_id=case_id, field=field), tempfile.TemporaryDirectory() as temporary_directory:
                policy = json.loads(json.dumps(self.policy))
                case = next(case for case in policy["cases"] if case["id"] == case_id)
                case[field] = value
                policy_path = Path(temporary_directory) / "policy.json"
                policy_path.write_text(json.dumps(policy), encoding="utf-8")

                with self.assertRaises(QualificationScopeError):
                    load_policy(policy_path)

    def test_live_publication_evidence_requires_receipt_binding_and_embedded_post_publication_time(self) -> None:
        evidence_path = REPO_ROOT / "docs/qualification/release-evidence-v1.json"
        reference_path = REPO_ROOT / "docs/qualification/rc3-targeted-qualification-v1.json"
        for section, field, value in (
            (None, "qualified_at", "2026-08-05T08:00:00Z"),
            ("candidate", "release_run_id", 1),
            ("candidate", "release_tag", "v0.3.0-rc.2"),
        ):
            with self.subTest(section=section, field=field):
                evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
                receipt = next(item for item in evidence["receipts"] if item["case_id"] == "sparkle-update-route")
                reference = json.loads(reference_path.read_text(encoding="utf-8"))
                if section is None:
                    reference[field] = value
                else:
                    reference[section][field] = value
                reference_bytes = (json.dumps(reference, indent=2) + "\n").encode()
                receipt["sha256"] = hashlib.sha256(reference_bytes).hexdigest()

                with self.assertRaises(QualificationScopeError):
                    classify_release_scope(
                        self.policy,
                        evidence,
                        candidate_sha="0b06582a83a45bb38d851e62ccf38cd148c7bb95",
                        changed_paths=lambda _base, _candidate: set(),
                        repo=REPO_ROOT,
                        reference_content=lambda path, content=reference_bytes: (
                            content
                            if path == "docs/qualification/rc3-targeted-qualification-v1.json"
                            else (REPO_ROOT / path).read_bytes()
                        ),
                        as_of=date(2026, 8, 6),
                    )

    def test_checked_rc3_live_publication_evidence_matches_milestone_receipt(self) -> None:
        evidence = self.checked_evidence_as_of(date(2026, 8, 9))
        report = classify_release_scope(
            self.policy,
            evidence,
            candidate_sha="0b06582a83a45bb38d851e62ccf38cd148c7bb95",
            changed_paths=lambda _base, _candidate: set(),
            repo=REPO_ROOT,
            workflow_phase="milestone",
            milestone_receipt_path=REPO_ROOT / "docs/release-evidence/v0.3.0-rc.3/release-receipt.json",
            as_of=date(2026, 8, 9),
        )

        self.assertEqual(self.result_for(report, "sparkle-update-route")["status"], "covered")
        self.assertEqual(self.result_for(report, "native-sparkle-release-notes")["status"], "covered")

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
        self.assertIn("before the preparation phase can pass", stderr.getvalue())

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
                        "--release-receipt-asset-id",
                        str(expectation.receipt_asset_id),
                        "--release-receipt-sha256",
                        expectation.receipt_file_sha256,
                        "--release-receipt-self-sha256",
                        expectation.receipt_sha256,
                        "--signed-app-tree-sha256",
                        expectation.signed_app_tree_sha256,
                        "--dmg-asset-id",
                        str(expectation.dmg_asset_id),
                        "--dmg-size",
                        str(expectation.dmg_size),
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
