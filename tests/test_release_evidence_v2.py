import hashlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
import zipfile

from pathlib import Path
from typing import Any

from scripts.release_evidence_v2 import (
    CAPTURE_NAME,
    DISPOSITION_NAME,
    QUALIFICATION_NAME,
    ReleaseEvidenceV2Error,
    canonical_record_bytes,
    validate_v2_bundle,
    verify_all_tags,
    verify_tag,
    verify_write_once_history,
    qualification_template_path,
    with_self_digest,
    write_record,
)
from scripts.release_receipt import build_receipt, write_receipt
from scripts.signed_artifact_receipt import (
    PROFILE_CASE_ID,
    build_receipt as build_signed_artifact_receipt,
    release_expectation_from_receipt,
    validate_policy_case,
    write_receipt as write_signed_artifact_receipt,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
TAG = "v0.3.0-rc.3"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git(root: Path, *arguments: str) -> str:
    return subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


class ReleaseEvidenceV2Tests(unittest.TestCase):
    def build_repository(self, root: Path) -> tuple[str, Path]:
        source_paths = {
            "policy": "docs/qualification/release-qualification-policy-v1.json",
            "qualification_template": f"docs/qualification/{TAG}-signed-qualification-v1.json",
            "route_table": "docs/qualification/video-quality-route-table-v2.json",
            "runner": ".github/workflows/milestone-qualification.yml",
        }
        for name, relative_path in source_paths.items():
            destination = root / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            source = REPO_ROOT / relative_path
            if name == "qualification_template":
                source = REPO_ROOT / "docs/qualification/v0.3.2-beta.7-signed-qualification-v1.json"
            shutil.copy2(source, destination)
        git(root, "init", "-q")
        git(root, "config", "user.email", "test@example.com")
        git(root, "config", "user.name", "Test")
        git(root, "add", ".")
        git(root, "commit", "-qm", "source inputs")
        source_sha = git(root, "rev-parse", "HEAD")
        bundle_root = root / "docs" / "release-evidence" / TAG
        bundle_root.mkdir(parents=True)
        release_receipt = build_receipt(
            {
                "release_route": "prerelease",
                "source_sha": source_sha,
                "workflow_actor": "shiny-code-bot",
                "workflow_run_id": 12345,
                "workflow_run_attempt": 2,
                "package_version": "0.3.0rc3",
                "public_version": "0.3.0-rc.3",
                "build_version": "160",
                "release_tag": TAG,
                "release_name": TAG,
                "release_id": 67890,
                "release_created_at": "2026-08-05T12:00:00Z",
                "prerelease": True,
                "make_latest": False,
                "signed_app_tree_sha256": "f" * 64,
                "artifacts": [
                    {
                        "kind": "dmg",
                        "name": "3D-Blu-ray-to-Vision-Pro-0.3.0-rc.3.dmg",
                        "sha256": "a" * 64,
                        "size_bytes": 1000,
                        "asset_id": 1,
                    },
                    {"kind": "checksum", "name": "SHA256SUMS", "sha256": "b" * 64, "size_bytes": 100, "asset_id": 2},
                    {"kind": "appcast", "name": "appcast.xml", "sha256": "c" * 64, "size_bytes": 500, "asset_id": 3},
                ],
            },
            policy_path=root / source_paths["policy"],
        )
        release_receipt_path = bundle_root / "release-receipt.json"
        write_receipt(release_receipt, release_receipt_path)
        policy = json.loads((root / source_paths["policy"]).read_text(encoding="utf-8"))
        expectation = release_expectation_from_receipt(
            release_receipt,
            policy_id=validate_policy_case(policy, PROFILE_CASE_ID),
            case_id=PROFILE_CASE_ID,
            workflow_run_id=12345,
            workflow_run_attempt=2,
            release_receipt_asset_id=99,
            release_receipt_file_sha256=digest(release_receipt_path),
        )
        signed_receipt = build_signed_artifact_receipt(
            expectation=expectation,
            evidence={
                "accessibility-tree": "1" * 64,
                "screenshot-dark": "2" * 64,
                "screenshot-light": "3" * 64,
                "ui-result": "4" * 64,
            },
        )
        signed_receipt_path = bundle_root / "signed-artifact-ui-receipt.json"
        write_signed_artifact_receipt(signed_receipt, signed_receipt_path)
        archive_path = bundle_root / "signed-artifact-ui.zip"
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("signed-artifact-ui-receipt.json", signed_receipt_path.read_bytes())
        qualification = json.loads(
            (REPO_ROOT / "docs/release-evidence/v0.3.2-beta.7/qualification-record.json").read_text(encoding="utf-8")
        )
        qualification["qualification_id"] = f"{TAG}-signed-qualification-v1"
        candidate = qualification["candidate"]
        candidate.update(
            {
                "appcast_sha256": "c" * 64,
                "build_version": "160",
                "dmg_name": "3D-Blu-ray-to-Vision-Pro-0.3.0-rc.3.dmg",
                "dmg_sha256": "a" * 64,
                "package_version": "0.3.0rc3",
                "public_version": "0.3.0-rc.3",
                "release_id": 67890,
                "release_run_id": 12345,
                "release_tag": TAG,
                "signed_app_tree_sha256": "f" * 64,
                "source_git_sha": source_sha,
                "workflow": "Prerelease",
            }
        )
        (bundle_root / "qualification-record.json").write_text(
            json.dumps(qualification, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (bundle_root / "updater-route-receipt.json").write_text('{"result":"passed"}\n', encoding="utf-8")
        (bundle_root / "clean-machine-signed-update-receipt.json").write_text('{"result":"passed"}\n', encoding="utf-8")
        (bundle_root / "installed-ui-accessibility-receipt.json").write_text('{"result":"passed"}\n', encoding="utf-8")
        return source_sha, bundle_root

    def capture_record(self, root: Path, source_sha: str) -> dict[str, Any]:
        bundle = root / "docs" / "release-evidence" / TAG
        receipt_path = bundle / "release-receipt.json"
        signed_receipt_path = bundle / "signed-artifact-ui-receipt.json"
        source_inputs = {
            "policy": "docs/qualification/release-qualification-policy-v1.json",
            "qualification_template": f"docs/qualification/{TAG}-signed-qualification-v1.json",
            "route_table": "docs/qualification/video-quality-route-table-v2.json",
            "runner": ".github/workflows/milestone-qualification.yml",
        }
        release_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        return with_self_digest(
            {
                "capture_workflow": {
                    "actor": "shiny-code-bot",
                    "path": ".github/workflows/release-evidence.yml",
                    "run_attempt": 1,
                    "run_id": 22222,
                },
                "captured_at": "2026-08-05T12:01:00Z",
                "live_appcast": {"sha256": "c" * 64, "verified_at": "2026-08-05T12:00:30Z"},
                "qualification_record": {
                    "path": f"docs/release-evidence/{TAG}/qualification-record.json",
                    "sha256": digest(bundle / "qualification-record.json"),
                },
                "receipt": {
                    "asset_id": 99,
                    "file_sha256": digest(receipt_path),
                    "path": f"docs/release-evidence/{TAG}/release-receipt.json",
                    "receipt_sha256": release_receipt["receipt_sha256"],
                },
                "record_type": "capture",
                "release_tag": TAG,
                "release_workflow": {
                    "actor": "shiny-code-bot",
                    "path": ".github/workflows/prerelease.yml",
                    "run_attempt": 2,
                    "run_id": 12345,
                },
                "repository": "cbusillo/BD_to_AVP",
                "schema_version": 2,
                "signed_ui": {
                    "archive": {
                        "path": f"docs/release-evidence/{TAG}/signed-artifact-ui.zip",
                        "sha256": digest(bundle / "signed-artifact-ui.zip"),
                    },
                    "artifact_id": 777,
                    "receipt": {
                        "file_sha256": digest(signed_receipt_path),
                        "path": f"docs/release-evidence/{TAG}/signed-artifact-ui-receipt.json",
                        "receipt_sha256": json.loads(signed_receipt_path.read_text(encoding="utf-8"))["receipt_sha256"],
                    },
                },
                "source_inputs": {
                    name: {"path": path, "sha256": digest(root / path)} for name, path in source_inputs.items()
                },
                "source_sha": source_sha,
                "state": "CAPTURED",
            }
        )

    def qualification_record(self, root: Path, capture: dict[str, Any]) -> dict[str, Any]:
        bundle = root / "docs" / "release-evidence" / TAG
        profile_receipt_path = bundle / "signed-artifact-ui-receipt.json"
        updater_receipt_path = bundle / "updater-route-receipt.json"
        return with_self_digest(
            {
                "accepted_case_receipts": {
                    "profile-save-action-accessibility": {
                        "accepted_at": "2026-08-05T12:04:00Z",
                        "path": f"docs/release-evidence/{TAG}/signed-artifact-ui-receipt.json",
                        "sha256": digest(profile_receipt_path),
                        "source": "signed_artifact_receipt",
                    },
                    "sparkle-update-route": {
                        "accepted_at": "2026-08-05T12:04:00Z",
                        "path": f"docs/release-evidence/{TAG}/updater-route-receipt.json",
                        "sha256": digest(updater_receipt_path),
                        "source": "signed_artifact_receipt",
                    },
                    "clean-machine-signed-update": {
                        "accepted_at": "2026-08-05T12:04:00Z",
                        "path": f"docs/release-evidence/{TAG}/clean-machine-signed-update-receipt.json",
                        "sha256": digest(bundle / "clean-machine-signed-update-receipt.json"),
                        "source": "tier3_automation_receipt",
                    },
                    "installed-ui-accessibility": {
                        "accepted_at": "2026-08-05T12:04:00Z",
                        "path": f"docs/release-evidence/{TAG}/installed-ui-accessibility-receipt.json",
                        "sha256": digest(bundle / "installed-ui-accessibility-receipt.json"),
                        "source": "tier3_automation_receipt",
                    },
                },
                "artifact": {"artifact_id": 888, "run_attempt": 3, "run_id": 33333, "sha256": "d" * 64},
                "capture": {
                    "path": f"docs/release-evidence/{TAG}/capture-v2.json",
                    "sha256": capture["capture_sha256"],
                },
                "profile_preservation": {
                    "case_id": "profile-save-action-accessibility",
                    "preserved": True,
                    "receipt_sha256": digest(profile_receipt_path),
                },
                "qualification_record": capture["qualification_record"],
                "qualified_at": "2026-08-05T12:05:00Z",
                "record_type": "qualification",
                "release_tag": TAG,
                "schema_version": 2,
                "source_sha": capture["source_sha"],
                "state": "QUALIFIED",
                "successful_milestone": {
                    "actor": "cbusillo",
                    "path": ".github/workflows/milestone-qualification.yml",
                    "result": "success",
                    "run_attempt": 3,
                    "run_id": 33333,
                },
                "updater_route": {
                    "case_id": "sparkle-update-route",
                    "receipt_sha256": digest(updater_receipt_path),
                    "result": "passed",
                },
            }
        )

    @staticmethod
    def disposition_record(capture: dict[str, Any]) -> dict[str, Any]:
        return with_self_digest(
            {
                "capture": {
                    "path": f"docs/release-evidence/{TAG}/capture-v2.json",
                    "sha256": capture["capture_sha256"],
                },
                "failed_at": "2026-08-05T12:05:00Z",
                "failure": {
                    "code": "profile_preservation_failed",
                    "expected": "profile preserved",
                    "observed": "profile missing",
                    "subject": "profile-save-action-accessibility",
                },
                "failure_workflow": {
                    "actor": "cbusillo",
                    "path": ".github/workflows/milestone-qualification.yml",
                    "run_attempt": 3,
                    "run_id": 33333,
                },
                "preservation": {
                    "release_identity_preserved": True,
                    "signed_artifact_preserved": True,
                    "source_identity_preserved": True,
                },
                "record_type": "disposition",
                "release_tag": TAG,
                "schema_version": 2,
                "source_sha": capture["source_sha"],
                "state": "FAILED",
            }
        )

    def write_capture(self, root: Path, source_sha: str) -> dict[str, Any]:
        capture = self.capture_record(root, source_sha)
        write_record(root / "docs" / "release-evidence" / TAG / CAPTURE_NAME, capture)
        return capture

    def test_capture_binds_release_signed_ui_appcast_workflows_and_historical_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_sha, _bundle = self.build_repository(root)
            capture = self.write_capture(root, source_sha)
            result = validate_v2_bundle(root, TAG, worktree=True)
            self.assertEqual(result["class"], "v2-captured")

            capture["live_appcast"]["sha256"] = "0" * 64
            capture = with_self_digest(capture)
            path = root / "docs" / "release-evidence" / TAG / CAPTURE_NAME
            path.unlink()
            write_record(path, capture)
            with self.assertRaisesRegex(ReleaseEvidenceV2Error, "live appcast digest"):
                validate_v2_bundle(root, TAG, worktree=True)

    def test_signed_ui_archive_tampering_and_receipt_byte_identity_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_sha, bundle = self.build_repository(root)
            capture = self.write_capture(root, source_sha)
            receipt = (bundle / "signed-artifact-ui-receipt.json").read_bytes()
            tampered = io.BytesIO()
            with zipfile.ZipFile(tampered, "w") as archive:
                archive.writestr("signed-artifact-ui-receipt.json", receipt + b"\n")
            (bundle / "signed-artifact-ui.zip").write_bytes(tampered.getvalue())
            capture["signed_ui"]["archive"]["sha256"] = digest(bundle / "signed-artifact-ui.zip")
            capture = with_self_digest(capture)
            path = bundle / CAPTURE_NAME
            path.unlink()
            write_record(path, capture)
            with self.assertRaisesRegex(ReleaseEvidenceV2Error, "byte-identical"):
                validate_v2_bundle(root, TAG, worktree=True)

    def test_capture_rejects_exact_receipt_workflow_and_historical_source_bindings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_sha, bundle = self.build_repository(root)
            capture = self.write_capture(root, source_sha)
            (bundle / "release-receipt.json").write_bytes((bundle / "release-receipt.json").read_bytes() + b" ")
            with self.assertRaisesRegex(ReleaseEvidenceV2Error, "Release receipt digest"):
                validate_v2_bundle(root, TAG, worktree=True)

            source_sha, bundle = self.build_repository(root / "workflow")
            capture = self.write_capture(root / "workflow", source_sha)
            capture["release_workflow"]["run_attempt"] = 3
            capture = with_self_digest(capture)
            path = bundle / CAPTURE_NAME
            path.unlink()
            write_record(path, capture)
            with self.assertRaisesRegex(ReleaseEvidenceV2Error, "release workflow conflicts"):
                validate_v2_bundle(root / "workflow", TAG, worktree=True)

            source_sha, bundle = self.build_repository(root / "source")
            capture = self.write_capture(root / "source", source_sha)
            capture["source_inputs"]["route_table"]["sha256"] = "0" * 64
            capture = with_self_digest(capture)
            path = bundle / CAPTURE_NAME
            path.unlink()
            write_record(path, capture)
            with self.assertRaisesRegex(ReleaseEvidenceV2Error, "source input route_table digest"):
                validate_v2_bundle(root / "source", TAG, worktree=True)

    def test_qualification_requires_exact_case_set_profile_preservation_and_artifact_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_sha, bundle = self.build_repository(root)
            capture = self.write_capture(root, source_sha)
            qualification = self.qualification_record(root, capture)
            write_record(bundle / QUALIFICATION_NAME, qualification)
            self.assertEqual(validate_v2_bundle(root, TAG, worktree=True)["class"], "v2-qualified")

            qualification["accepted_case_receipts"].pop("sparkle-update-route")
            qualification = with_self_digest(qualification)
            (bundle / QUALIFICATION_NAME).unlink()
            write_record(bundle / QUALIFICATION_NAME, qualification)
            with self.assertRaisesRegex(ReleaseEvidenceV2Error, "accepted case receipts keys changed"):
                validate_v2_bundle(root, TAG, worktree=True)

            qualification = self.qualification_record(root, capture)
            qualification["profile_preservation"]["preserved"] = False
            qualification = with_self_digest(qualification)
            (bundle / QUALIFICATION_NAME).unlink()
            write_record(bundle / QUALIFICATION_NAME, qualification)
            with self.assertRaisesRegex(ReleaseEvidenceV2Error, "explicit preserved profile"):
                validate_v2_bundle(root, TAG, worktree=True)

            qualification = self.qualification_record(root, capture)
            qualification["artifact"]["artifact_id"] = capture["signed_ui"]["artifact_id"]
            qualification = with_self_digest(qualification)
            (bundle / QUALIFICATION_NAME).unlink()
            write_record(bundle / QUALIFICATION_NAME, qualification)
            with self.assertRaisesRegex(ReleaseEvidenceV2Error, "independent from the signed UI artifact"):
                validate_v2_bundle(root, TAG, worktree=True)

    def test_disposition_binds_structured_failed_run_and_terminal_preservation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_sha, bundle = self.build_repository(root)
            capture = self.write_capture(root, source_sha)
            disposition = self.disposition_record(capture)
            write_record(bundle / DISPOSITION_NAME, disposition)
            self.assertEqual(validate_v2_bundle(root, TAG, worktree=True)["class"], "v2-failed")

            disposition["preservation"]["signed_artifact_preserved"] = False
            disposition = with_self_digest(disposition)
            (bundle / DISPOSITION_NAME).unlink()
            write_record(bundle / DISPOSITION_NAME, disposition)
            with self.assertRaisesRegex(ReleaseEvidenceV2Error, "must make failure terminal"):
                validate_v2_bundle(root, TAG, worktree=True)

            disposition = self.disposition_record(capture)
            disposition["failure"]["extra"] = "not allowed"
            disposition = with_self_digest(disposition)
            (bundle / DISPOSITION_NAME).unlink()
            write_record(bundle / DISPOSITION_NAME, disposition)
            with self.assertRaisesRegex(ReleaseEvidenceV2Error, "failure keys changed"):
                validate_v2_bundle(root, TAG, worktree=True)

    def test_rejects_unknown_nested_keys_and_bool_for_integer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_sha, bundle = self.build_repository(root)
            capture = self.write_capture(root, source_sha)
            capture["release_workflow"]["run_id"] = True
            capture = with_self_digest(capture)
            (bundle / CAPTURE_NAME).unlink()
            write_record(bundle / CAPTURE_NAME, capture)
            with self.assertRaisesRegex(ReleaseEvidenceV2Error, "positive integer"):
                validate_v2_bundle(root, TAG, worktree=True)

            capture = self.capture_record(root, source_sha)
            capture["signed_ui"]["archive"]["allowed_token_name"] = "public-looking"
            capture = with_self_digest(capture)
            (bundle / CAPTURE_NAME).unlink()
            write_record(bundle / CAPTURE_NAME, capture)
            with self.assertRaisesRegex(ReleaseEvidenceV2Error, "signed UI archive keys changed"):
                validate_v2_bundle(root, TAG, worktree=True)

    def test_revision_verification_uses_immutable_bundle_and_source_after_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_sha, _bundle = self.build_repository(root)
            self.write_capture(root, source_sha)
            git(root, "add", "docs/release-evidence")
            git(root, "commit", "-qm", "capture evidence")
            evidence_revision = git(root, "rev-parse", "HEAD")
            (root / "docs" / "qualification" / "release-qualification-policy-v1.json").write_text(
                "{}\n", encoding="utf-8"
            )
            git(root, "add", ".")
            git(root, "commit", "-qm", "main drift")
            self.assertEqual(
                validate_v2_bundle(root, TAG, verification_revision=evidence_revision)["class"], "v2-captured"
            )
            self.assertEqual(verify_tag(root, TAG, verification_revision=evidence_revision)["class"], "v2-captured")
            self.assertEqual(verify_all_tags(root, verification_revision=evidence_revision)[0]["class"], "v2-captured")

    def test_write_once_history_rejects_changed_and_deleted_records_at_later_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_sha, bundle = self.build_repository(root)
            capture = self.write_capture(root, source_sha)
            git(root, "add", "docs/release-evidence")
            git(root, "commit", "-qm", "capture evidence")
            baseline = git(root, "rev-parse", "HEAD")
            capture["captured_at"] = "2026-08-05T12:03:00Z"
            capture = with_self_digest(capture)
            (bundle / CAPTURE_NAME).unlink()
            write_record(bundle / CAPTURE_NAME, capture)
            git(root, "add", ".")
            git(root, "commit", "-qm", "changed evidence")
            changed = git(root, "rev-parse", "HEAD")
            with self.assertRaisesRegex(ReleaseEvidenceV2Error, "changed after base"):
                verify_write_once_history(root, baseline, verification_revision=changed)

            git(root, "rm", "-q", f"docs/release-evidence/{TAG}/{CAPTURE_NAME}")
            git(root, "commit", "-qm", "deleted evidence")
            deleted = git(root, "rev-parse", "HEAD")
            with self.assertRaisesRegex(ReleaseEvidenceV2Error, "was removed"):
                verify_write_once_history(root, baseline, verification_revision=deleted)

    def test_split_brain_chronology_and_canonical_serialization_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_sha, bundle = self.build_repository(root)
            capture = self.write_capture(root, source_sha)
            qualification = self.qualification_record(root, capture)
            disposition = self.disposition_record(capture)
            write_record(bundle / QUALIFICATION_NAME, qualification)
            write_record(bundle / DISPOSITION_NAME, disposition)
            with self.assertRaisesRegex(ReleaseEvidenceV2Error, "split-brain"):
                validate_v2_bundle(root, TAG, worktree=True)
            (bundle / DISPOSITION_NAME).unlink()
            qualification["qualified_at"] = "2026-08-05T12:00:00Z"
            qualification = with_self_digest(qualification)
            (bundle / QUALIFICATION_NAME).unlink()
            write_record(bundle / QUALIFICATION_NAME, qualification)
            with self.assertRaisesRegex(ReleaseEvidenceV2Error, "precedes capture"):
                validate_v2_bundle(root, TAG, worktree=True)
            (bundle / QUALIFICATION_NAME).write_bytes(
                canonical_record_bytes(self.qualification_record(root, capture)) + b" "
            )
            with self.assertRaisesRegex(ReleaseEvidenceV2Error, "canonical JSON"):
                validate_v2_bundle(root, TAG, worktree=True)

    def test_representative_legacy_classes_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            for tag, expected in (
                ("v0.3.0", "legacy-publication-v1"),
                ("v0.3.2-beta.5", "legacy-failed-post-publication-v1"),
            ):
                shutil.copytree(REPO_ROOT / "docs" / "release-evidence" / tag, root / "docs" / "release-evidence" / tag)
                self.assertEqual(verify_tag(root, tag, worktree=True)["class"], expected)

    def test_all_four_case_receipts_validate_digest_source_and_time(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_sha, bundle = self.build_repository(root)
            capture = self.write_capture(root, source_sha)
            qualification = self.qualification_record(root, capture)
            write_record(bundle / QUALIFICATION_NAME, qualification)
            self.assertEqual(validate_v2_bundle(root, TAG, worktree=True)["class"], "v2-qualified")

            for case_id in sorted(
                {
                    "sparkle-update-route",
                    "clean-machine-signed-update",
                    "installed-ui-accessibility",
                    "profile-save-action-accessibility",
                }
            ):
                mutated = self.qualification_record(root, capture)
                mutated["accepted_case_receipts"][case_id]["source"] = "wrong_source"
                mutated = with_self_digest(mutated)
                (bundle / QUALIFICATION_NAME).unlink()
                write_record(bundle / QUALIFICATION_NAME, mutated)
                with self.assertRaisesRegex(ReleaseEvidenceV2Error, f"receipt {case_id} source"):
                    validate_v2_bundle(root, TAG, worktree=True)

    def test_signed_ui_receipt_binding_has_no_asset_id_and_zip_rejects_extra_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_sha, bundle = self.build_repository(root)
            capture = self.write_capture(root, source_sha)
            self.assertNotIn("asset_id", capture["signed_ui"]["receipt"])

            capture["signed_ui"]["receipt"]["asset_id"] = 123
            capture = with_self_digest(capture)
            (bundle / CAPTURE_NAME).unlink()
            write_record(bundle / CAPTURE_NAME, capture)
            with self.assertRaisesRegex(ReleaseEvidenceV2Error, "signed UI receipt keys changed"):
                validate_v2_bundle(root, TAG, worktree=True)

            capture = self.capture_record(root, source_sha)
            (bundle / CAPTURE_NAME).unlink()
            write_record(bundle / CAPTURE_NAME, capture)

            receipt = (bundle / "signed-artifact-ui-receipt.json").read_bytes()
            archive_bytes = io.BytesIO()
            with zipfile.ZipFile(archive_bytes, "w") as archive:
                archive.writestr("signed-artifact-ui-receipt.json", receipt)
                archive.writestr("unexpected.txt", b"extra")
            (bundle / "signed-artifact-ui.zip").write_bytes(archive_bytes.getvalue())
            capture["signed_ui"]["archive"]["sha256"] = digest(bundle / "signed-artifact-ui.zip")
            capture = with_self_digest(capture)
            (bundle / CAPTURE_NAME).unlink()
            write_record(bundle / CAPTURE_NAME, capture)
            with self.assertRaisesRegex(ReleaseEvidenceV2Error, "exactly one non-directory"):
                validate_v2_bundle(root, TAG, worktree=True)

    def test_dynamic_qualification_template_and_capture_snapshot_are_bound(self) -> None:
        self.assertEqual(
            qualification_template_path("v0.3.2", "stable"),
            "docs/qualification/v0.3.2-stable-signed-qualification-v1.json",
        )
        self.assertEqual(
            qualification_template_path(TAG, "prerelease"),
            f"docs/qualification/{TAG}-signed-qualification-v1.json",
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_sha, bundle = self.build_repository(root)
            capture = self.write_capture(root, source_sha)
            self.assertEqual(
                capture["source_inputs"]["qualification_template"]["path"],
                f"docs/qualification/{TAG}-signed-qualification-v1.json",
            )
            capture["source_inputs"]["qualification_template"]["path"] = "docs/qualification/release-evidence-v1.json"
            capture = with_self_digest(capture)
            (bundle / CAPTURE_NAME).unlink()
            write_record(bundle / CAPTURE_NAME, capture)
            with self.assertRaisesRegex(ReleaseEvidenceV2Error, "source input qualification_template path"):
                validate_v2_bundle(root, TAG, worktree=True)

            capture = self.capture_record(root, source_sha)
            (bundle / CAPTURE_NAME).unlink()
            write_record(bundle / CAPTURE_NAME, capture)
            (bundle / "qualification-record.json").write_bytes(b"{}\n")
            with self.assertRaisesRegex(ReleaseEvidenceV2Error, "qualification record digest"):
                validate_v2_bundle(root, TAG, worktree=True)

    def test_chronology_requires_publication_before_live_verification_and_terminal_after_capture(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_sha, bundle = self.build_repository(root)
            capture = self.write_capture(root, source_sha)
            capture["live_appcast"]["verified_at"] = "2026-08-05T11:59:59Z"
            capture = with_self_digest(capture)
            (bundle / CAPTURE_NAME).unlink()
            write_record(bundle / CAPTURE_NAME, capture)
            with self.assertRaisesRegex(ReleaseEvidenceV2Error, "precedes release publication"):
                validate_v2_bundle(root, TAG, worktree=True)

            capture = self.capture_record(root, source_sha)
            (bundle / CAPTURE_NAME).unlink()
            write_record(bundle / CAPTURE_NAME, capture)
            qualification = self.qualification_record(root, capture)
            qualification["qualified_at"] = "2026-08-05T12:00:59Z"
            qualification = with_self_digest(qualification)
            write_record(bundle / QUALIFICATION_NAME, qualification)
            with self.assertRaisesRegex(ReleaseEvidenceV2Error, "precedes capture"):
                validate_v2_bundle(root, TAG, worktree=True)

    def test_legacy_tampering_is_deeply_validated_and_historical_legacy_mode_is_explicitly_unsupported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            tag = "v0.3.2-beta.5"
            shutil.copytree(REPO_ROOT / "docs" / "release-evidence" / tag, root / "docs" / "release-evidence" / tag)
            self.assertEqual(verify_tag(root, tag, worktree=True)["class"], "legacy-failed-post-publication-v1")
            failed_record = root / "docs" / "release-evidence" / tag / "failed-post-publication-qualification-v1.json"
            tampered = json.loads(failed_record.read_text(encoding="utf-8"))
            tampered["release"]["release_id"] += 1
            failed_record.write_text(json.dumps(tampered, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ReleaseEvidenceV2Error, "Legacy failed post-publication record is invalid"):
                verify_tag(root, tag, worktree=True)

            repo = root / "historical"
            shutil.copytree(REPO_ROOT, repo, dirs_exist_ok=True)
            with self.assertRaisesRegex(ReleaseEvidenceV2Error, "arbitrary historical revision"):
                verify_tag(repo, "v0.3.0", verification_revision=git(repo, "rev-parse", "HEAD"))


if __name__ == "__main__":
    unittest.main()
