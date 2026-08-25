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
    INDEX_NAME,
    QUALIFICATION_NAME,
    ReleaseEvidenceV2Error,
    build_index_v2,
    canonical_index_bytes,
    canonical_record_bytes,
    check_index_v2,
    evidence_ref_for_tag,
    sanitize_evidence_ref,
    sanitize_release_tag,
    validate_v2_bundle,
    verify_all_tags,
    verify_tag,
    verify_write_once_history,
    write_or_validate_capture_v2,
    qualification_template_path,
    with_self_digest,
    write_record,
)
from scripts.release_qualification_manifest import manifest_sha256
from scripts.release_receipt import build_receipt, write_receipt
from scripts.signed_artifact_receipt import (
    PROFILE_CASE_ID,
    build_receipt as build_signed_artifact_receipt,
    release_expectation_from_receipt,
    validate_policy_case,
    write_receipt as write_signed_artifact_receipt,
)
from scripts.tier3_receipt import build_receipt as build_tier3_receipt, receipt_sha256 as tier3_receipt_sha256


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
        cases = {case["id"]: case for case in policy["cases"]}
        for case_id, sample_name in (
            ("clean-machine-signed-update", "v0.3.2-clean-machine-signed-update-v1.json"),
            ("installed-ui-accessibility", "v0.3.2-installed-ui-accessibility-v1.json"),
        ):
            sample = json.loads((REPO_ROOT / "docs" / "qualification" / sample_name).read_text(encoding="utf-8"))
            facts = {
                "assertions": {item["id"]: item["status"] for item in sample["assertions"]},
                "cleanup": sample["cleanup"],
                "completed_at": "2026-08-05T12:03:30Z",
                "environment": sample["environment"],
                "evidence": sample["evidence"],
                "evidence_source": sample["evidence_source"],
                "hardware": sample["hardware"],
                "release_receipt_file_sha256": digest(release_receipt_path),
                "release_receipt_reference": f"docs/release-evidence/{TAG}/release-receipt.json",
                "result": sample["result"],
                "started_at": "2026-08-05T12:02:00Z",
            }
            tier3_receipt = build_tier3_receipt(
                facts,
                policy_id=policy["policy_id"],
                case=cases[case_id],
                release_receipt=release_receipt,
            )
            (bundle_root / f"{case_id}-receipt.json").write_text(
                json.dumps(tier3_receipt, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        qualification_manifest: dict[str, Any] = {
            "manifest_type": "release-qualification-v1",
            "schema_version": 1,
        }
        qualification_manifest["manifest_sha256"] = manifest_sha256(qualification_manifest)
        (bundle_root / "qualification-manifest.json").write_text(
            json.dumps(qualification_manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
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
        clean_receipt_path = bundle / "clean-machine-signed-update-receipt.json"
        live_qualification_path = bundle / "live-qualification-v1.json"
        qualification_manifest_path = bundle / "qualification-manifest.json"
        release_receipt = json.loads((bundle / "release-receipt.json").read_text(encoding="utf-8"))
        release = release_receipt["release"]
        versions = release_receipt["versions"]
        workflow = release_receipt["workflow"]
        manifest = json.loads(qualification_manifest_path.read_text(encoding="utf-8"))
        live_qualification = {
            "candidate": {
                "appcast_sha256": "c" * 64,
                "build": versions["build"],
                "dmg_sha256": "a" * 64,
                "package_version": versions["package"],
                "public_version": versions["public"],
                "release_id": release["id"],
                "release_run_id": workflow["run_id"],
                "release_tag": TAG,
                "signed_app_tree_sha256": release_receipt["signed_app_tree_sha256"],
                "source_sha": capture["source_sha"],
            },
            "cases": [
                {
                    "id": "sparkle-update-route",
                    "observations": {
                        "candidate_offered_on_rc_route": True,
                        "install_action": "Install and Relaunch",
                        "post_update_build": versions["build"],
                        "post_update_package_version": versions["package"],
                        "post_update_signed_app_tree_sha256": release_receipt["signed_app_tree_sha256"],
                        "prior_release_tag": "v0.3.0-rc.2",
                        "profile_preserved": True,
                        "qualification_artifact_digest": f"sha256:{'d' * 64}",
                        "qualification_artifact_id": 888,
                        "qualification_evidence_sha": "e" * 40,
                        "qualification_manifest_sha256": manifest["manifest_sha256"],
                        "qualification_receipt_reference": (
                            f"docs/release-evidence/{TAG}/clean-machine-signed-update-receipt.json"
                        ),
                        "qualification_receipt_sha256": digest(clean_receipt_path),
                        "qualification_workflow_run_id": 33333,
                        "route": "rc",
                    },
                    "result": "passed",
                }
            ],
            "qualification_id": "rc-live-qualification-v1",
            "qualified_at": "2026-08-05T12:04:30Z",
            "schema_version": 1,
        }
        live_qualification_path.write_text(
            json.dumps(live_qualification, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
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
                        "path": f"docs/release-evidence/{TAG}/live-qualification-v1.json",
                        "sha256": digest(live_qualification_path),
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
                "qualification_manifest": {
                    "path": f"docs/release-evidence/{TAG}/qualification-manifest.json",
                    "sha256": digest(qualification_manifest_path),
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
                    "receipt_sha256": digest(live_qualification_path),
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
            self.write_capture(root, source_sha)
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

    def test_capture_rejects_repository_actor_and_non_ancestor_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_sha, bundle = self.build_repository(root)
            capture = self.write_capture(root, source_sha)
            capture["repository"] = "other/repository"
            capture = with_self_digest(capture)
            (bundle / CAPTURE_NAME).unlink()
            write_record(bundle / CAPTURE_NAME, capture)
            with self.assertRaisesRegex(ReleaseEvidenceV2Error, "repository is not canonical"):
                validate_v2_bundle(root, TAG, worktree=True)

            source_sha, bundle = self.build_repository(root / "actor")
            capture = self.write_capture(root / "actor", source_sha)
            capture["capture_workflow"]["actor"] = "someone-else"
            capture = with_self_digest(capture)
            (bundle / CAPTURE_NAME).unlink()
            write_record(bundle / CAPTURE_NAME, capture)
            with self.assertRaisesRegex(ReleaseEvidenceV2Error, "not the approved release actor"):
                validate_v2_bundle(root / "actor", TAG, worktree=True)

            source_sha, bundle = self.build_repository(root / "ancestry")
            capture = self.write_capture(root / "ancestry", source_sha)
            tree = git(root / "ancestry", "write-tree")
            unrelated = subprocess.run(
                ["git", "commit-tree", tree],
                cwd=root / "ancestry",
                check=True,
                capture_output=True,
                input="unrelated\n",
                text=True,
            ).stdout.strip()
            capture["source_sha"] = unrelated
            capture = with_self_digest(capture)
            (bundle / CAPTURE_NAME).unlink()
            write_record(bundle / CAPTURE_NAME, capture)
            with self.assertRaisesRegex(ReleaseEvidenceV2Error, "not an ancestor"):
                validate_v2_bundle(root / "ancestry", TAG, worktree=True)

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

            qualification = self.qualification_record(root, capture)
            qualification["successful_milestone"]["path"] = ".github/workflows/other.yml"
            qualification = with_self_digest(qualification)
            (bundle / QUALIFICATION_NAME).unlink()
            write_record(bundle / QUALIFICATION_NAME, qualification)
            with self.assertRaisesRegex(ReleaseEvidenceV2Error, "path is not canonical"):
                validate_v2_bundle(root, TAG, worktree=True)

            qualification = self.qualification_record(root, capture)
            qualification["successful_milestone"]["actor"] = "someone-else"
            qualification = with_self_digest(qualification)
            (bundle / QUALIFICATION_NAME).unlink()
            write_record(bundle / QUALIFICATION_NAME, qualification)
            with self.assertRaisesRegex(ReleaseEvidenceV2Error, "actor is not the repository owner"):
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

            disposition = self.disposition_record(capture)
            disposition["failure_workflow"]["path"] = ".github/workflows/other.yml"
            disposition = with_self_digest(disposition)
            (bundle / DISPOSITION_NAME).unlink()
            write_record(bundle / DISPOSITION_NAME, disposition)
            with self.assertRaisesRegex(ReleaseEvidenceV2Error, "path is not canonical"):
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
        for tag, expected in (
            ("v0.3.0", "legacy-publication-v1"),
            ("v0.3.2", "legacy-qualification-manifest-v1"),
            ("v0.3.2-beta.5", "legacy-failed-post-publication-v1"),
        ):
            self.assertEqual(verify_tag(REPO_ROOT, tag, worktree=True)["class"], expected)
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            receipt_root = root / "docs" / "release-evidence" / "v0.3.0"
            receipt_root.mkdir(parents=True)
            shutil.copy2(
                REPO_ROOT / "docs" / "release-evidence" / "v0.3.0" / "release-receipt.json",
                receipt_root / "release-receipt.json",
            )
            self.assertEqual(verify_tag(root, "v0.3.0", worktree=True)["class"], "legacy-receipt-v1")

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

    def test_case_receipt_contents_are_deeply_validated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_sha, bundle = self.build_repository(root)
            capture = self.write_capture(root, source_sha)

            qualification = self.qualification_record(root, capture)
            clean_path = bundle / "clean-machine-signed-update-receipt.json"
            clean_path.write_text(json.dumps({"result": "passed"}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            qualification["accepted_case_receipts"]["clean-machine-signed-update"]["sha256"] = digest(clean_path)
            qualification = with_self_digest(qualification)
            write_record(bundle / QUALIFICATION_NAME, qualification)
            with self.assertRaisesRegex(ReleaseEvidenceV2Error, "clean-machine-signed-update is invalid"):
                validate_v2_bundle(root, TAG, worktree=True)

            (bundle / QUALIFICATION_NAME).unlink()
            source_sha, bundle = self.build_repository(root / "installed")
            capture = self.write_capture(root / "installed", source_sha)
            qualification = self.qualification_record(root / "installed", capture)
            installed_path = bundle / "installed-ui-accessibility-receipt.json"
            installed = json.loads(installed_path.read_text(encoding="utf-8"))
            installed["assertions"][0]["status"] = "failed"
            installed["result"] = {"reason_code": "assertion_failed", "status": "failed"}
            installed["receipt_sha256"] = tier3_receipt_sha256(installed)
            installed_path.write_text(json.dumps(installed, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            qualification["accepted_case_receipts"]["installed-ui-accessibility"]["sha256"] = digest(installed_path)
            qualification = with_self_digest(qualification)
            write_record(bundle / QUALIFICATION_NAME, qualification)
            with self.assertRaisesRegex(ReleaseEvidenceV2Error, "installed-ui-accessibility did not pass"):
                validate_v2_bundle(root / "installed", TAG, worktree=True)

            source_sha, bundle = self.build_repository(root / "live")
            capture = self.write_capture(root / "live", source_sha)
            qualification = self.qualification_record(root / "live", capture)
            live_path = bundle / "live-qualification-v1.json"
            live = json.loads(live_path.read_text(encoding="utf-8"))
            live["candidate"]["release_id"] += 1
            live_path.write_text(json.dumps(live, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            qualification["accepted_case_receipts"]["sparkle-update-route"]["sha256"] = digest(live_path)
            qualification["updater_route"]["receipt_sha256"] = digest(live_path)
            qualification = with_self_digest(qualification)
            write_record(bundle / QUALIFICATION_NAME, qualification)
            with self.assertRaisesRegex(ReleaseEvidenceV2Error, "candidate conflicts"):
                validate_v2_bundle(root / "live", TAG, worktree=True)

            source_sha, bundle = self.build_repository(root / "profile")
            capture = self.write_capture(root / "profile", source_sha)
            qualification = self.qualification_record(root / "profile", capture)
            qualification["accepted_case_receipts"][PROFILE_CASE_ID]["sha256"] = "0" * 64
            qualification["profile_preservation"]["receipt_sha256"] = "0" * 64
            qualification = with_self_digest(qualification)
            write_record(bundle / QUALIFICATION_NAME, qualification)
            with self.assertRaisesRegex(ReleaseEvidenceV2Error, "profile receipt conflicts"):
                validate_v2_bundle(root / "profile", TAG, worktree=True)

            foreign_root = root / "foreign"
            source_sha, bundle = self.build_repository(foreign_root)
            capture = self.write_capture(foreign_root, source_sha)
            qualification = self.qualification_record(foreign_root, capture)
            policy = json.loads(
                (foreign_root / "docs" / "qualification" / "release-qualification-policy-v1.json").read_text(
                    encoding="utf-8"
                )
            )
            foreign_receipt = build_receipt(
                {
                    "release_route": "prerelease",
                    "source_sha": "1" * 40,
                    "workflow_actor": "shiny-code-bot",
                    "workflow_run_id": 12344,
                    "workflow_run_attempt": 1,
                    "package_version": "0.3.0rc2",
                    "public_version": "0.3.0-rc.2",
                    "build_version": "159",
                    "release_tag": "v0.3.0-rc.2",
                    "release_name": "v0.3.0-rc.2",
                    "release_id": 67889,
                    "release_created_at": "2026-08-04T12:00:00Z",
                    "prerelease": True,
                    "make_latest": False,
                    "signed_app_tree_sha256": "8" * 64,
                    "artifacts": [
                        {
                            "kind": "dmg",
                            "name": "3D-Blu-ray-to-Vision-Pro-0.3.0-rc.2.dmg",
                            "sha256": "7" * 64,
                            "size_bytes": 900,
                            "asset_id": 11,
                        },
                        {
                            "kind": "checksum",
                            "name": "SHA256SUMS",
                            "sha256": "6" * 64,
                            "size_bytes": 90,
                            "asset_id": 12,
                        },
                        {
                            "kind": "appcast",
                            "name": "appcast.xml",
                            "sha256": "5" * 64,
                            "size_bytes": 450,
                            "asset_id": 13,
                        },
                    ],
                },
                policy_path=foreign_root / "docs" / "qualification" / "release-qualification-policy-v1.json",
            )
            foreign_receipt_path = foreign_root / "docs" / "release-evidence" / "v0.3.0-rc.2" / "release-receipt.json"
            foreign_receipt_path.parent.mkdir(parents=True)
            write_receipt(foreign_receipt, foreign_receipt_path)
            sample = json.loads(
                (REPO_ROOT / "docs" / "qualification" / "v0.3.2-clean-machine-signed-update-v1.json").read_text(
                    encoding="utf-8"
                )
            )
            foreign_tier3 = build_tier3_receipt(
                {
                    "assertions": {item["id"]: item["status"] for item in sample["assertions"]},
                    "cleanup": sample["cleanup"],
                    "completed_at": "2026-08-05T12:03:30Z",
                    "environment": sample["environment"],
                    "evidence": sample["evidence"],
                    "evidence_source": sample["evidence_source"],
                    "hardware": sample["hardware"],
                    "release_receipt_file_sha256": digest(foreign_receipt_path),
                    "release_receipt_reference": "docs/release-evidence/v0.3.0-rc.2/release-receipt.json",
                    "result": sample["result"],
                    "started_at": "2026-08-05T12:02:00Z",
                },
                policy_id=policy["policy_id"],
                case={case["id"]: case for case in policy["cases"]}["clean-machine-signed-update"],
                release_receipt=foreign_receipt,
            )
            clean_path = bundle / "clean-machine-signed-update-receipt.json"
            clean_path.write_text(json.dumps(foreign_tier3, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            qualification["accepted_case_receipts"]["clean-machine-signed-update"]["sha256"] = digest(clean_path)
            qualification = with_self_digest(qualification)
            write_record(bundle / QUALIFICATION_NAME, qualification)
            with self.assertRaisesRegex(ReleaseEvidenceV2Error, "not bound to the captured release receipt"):
                validate_v2_bundle(foreign_root, TAG, worktree=True)

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

    def test_legacy_tampering_is_deeply_validated_and_historical_revision_is_supported(self) -> None:
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
            self.assertEqual(
                verify_tag(repo, "v0.3.0", verification_revision=git(repo, "rev-parse", "HEAD"))["class"],
                "legacy-publication-v1",
            )

    def test_legacy_manifest_signed_ui_binding_and_publication_key_variants_are_strict(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            clone = Path(temporary_directory) / "clone"
            subprocess.run(
                ["git", "clone", "--quiet", "--shared", REPO_ROOT.as_posix(), clone.as_posix()],
                check=True,
                capture_output=True,
                text=True,
            )
            manifest_path = clone / "docs" / "release-evidence" / "v0.3.2" / "qualification-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["signed_ui_artifact"]["receipt_sha256"] = "9" * 64
            manifest["manifest_sha256"] = manifest_sha256(manifest)
            manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ReleaseEvidenceV2Error, "signed UI binding conflicts"):
                verify_tag(clone, "v0.3.2", worktree=True)

            publication_root = Path(temporary_directory) / "publication"
            shutil.copytree(
                REPO_ROOT / "docs" / "release-evidence" / "v0.3.0",
                publication_root / "docs" / "release-evidence" / "v0.3.0",
            )
            publication_path = publication_root / "docs" / "release-evidence" / "v0.3.0" / "publication-record.json"
            publication = json.loads(publication_path.read_text(encoding="utf-8"))
            publication["recovery_workflow_run"] = {"unexpected": True}
            publication["extra"] = True
            publication_path.write_text(json.dumps(publication, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ReleaseEvidenceV2Error, "nearest recovery variant"):
                verify_tag(publication_root, "v0.3.0", worktree=True)

    def test_index_v2_is_deterministic_sorted_and_preserves_legacy_index_bytes(self) -> None:
        revision = git(REPO_ROOT, "rev-parse", "HEAD")
        legacy_before = (REPO_ROOT / "docs" / "release-evidence" / "index-v1.json").read_bytes()
        first = build_index_v2(REPO_ROOT, revision=revision)
        second = build_index_v2(REPO_ROOT, revision=revision)
        self.assertEqual(canonical_index_bytes(first), canonical_index_bytes(second))
        self.assertEqual(first["schema_version"], 2)
        self.assertEqual(first["legacy_index"]["path"], "docs/release-evidence/index-v1.json")
        self.assertEqual(first["legacy_index"]["sha256"], digest(REPO_ROOT / first["legacy_index"]["path"]))
        self.assertEqual(
            [release["release_tag"] for release in first["releases"]],
            sorted(release["release_tag"] for release in first["releases"]),
        )
        for release in first["releases"]:
            self.assertEqual(release["files"], sorted(release["files"], key=lambda item: item["path"]))
            self.assertNotIn(f"docs/release-evidence/{INDEX_NAME}", {item["path"] for item in release["files"]})
        self.assertEqual(legacy_before, (REPO_ROOT / "docs" / "release-evidence" / "index-v1.json").read_bytes())

    def test_index_v2_historical_replay_contains_every_legacy_class(self) -> None:
        index = build_index_v2(REPO_ROOT, revision="119b27f4bf0e72b1b979d5397993d1ae526db187")
        classes = {release["evidence_class"] for release in index["releases"]}
        self.assertIn("legacy-publication-v1", classes)
        self.assertIn("legacy-qualification-manifest-v1", classes)
        self.assertIn("legacy-failed-post-publication-v1", classes)

    def test_tag_and_evidence_ref_sanitization_is_exact(self) -> None:
        self.assertEqual(sanitize_release_tag("v0.3.2-beta.7"), "v0.3.2-beta.7")
        self.assertEqual(evidence_ref_for_tag("v0.3.2-beta.7"), "automation/release-evidence-v0.3.2-beta.7")
        self.assertEqual(
            sanitize_evidence_ref("automation/release-evidence-v0.3.2-beta.7", "v0.3.2-beta.7"),
            "automation/release-evidence-v0.3.2-beta.7",
        )
        for malicious in ("v0.3.2/evil", "v0.3.2..evil", "v0.3.2-", "v0.3.2\n"):
            with self.assertRaises(ReleaseEvidenceV2Error):
                sanitize_release_tag(malicious)
        with self.assertRaises(ReleaseEvidenceV2Error):
            sanitize_evidence_ref("automation/release-evidence-v0.3.2/evil", "v0.3.2")

    def test_existing_capture_bytes_are_reused_and_conflicting_record_write_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_sha, bundle = self.build_repository(root)
            capture = self.write_capture(root, source_sha)
            capture_path = bundle / CAPTURE_NAME
            original = capture_path.read_bytes()
            changed = dict(capture)
            changed["captured_at"] = "2026-08-05T12:02:00Z"
            changed = with_self_digest(changed)
            reused = write_or_validate_capture_v2(root, TAG, changed)
            self.assertEqual(reused["capture_sha256"], capture["capture_sha256"])
            self.assertEqual(capture_path.read_bytes(), original)
            capture_path.unlink()
            write_record(capture_path, capture)
            with self.assertRaisesRegex(ReleaseEvidenceV2Error, "different bytes"):
                write_record(capture_path, changed)

    def test_workflow_contract_uses_unconditional_shadow_capture_and_index(self) -> None:
        workflow = (REPO_ROOT / ".github" / "workflows" / "release-evidence.yml").read_text(encoding="utf-8")
        self.assertIn("name: Sanitize release evidence tag and ref", workflow)
        self.assertIn("name: Generate or validate shadow capture-v2", workflow)
        self.assertIn("name: Generate and check shadow index-v2", workflow)
        self.assertIn("--captured-at", workflow)
        self.assertIn("--capture-workflow-run-id", workflow)
        self.assertIn("branch: ${{ needs.validate-and-prepare.outputs.evidence_ref }}", workflow)
        self.assertNotIn("if: vars.RELEASE_EVIDENCE_V2", workflow)

    def test_index_check_rejects_drift_without_changing_legacy_index(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            index = build_index_v2(REPO_ROOT, worktree=True)
            index_path = Path(temporary_directory) / INDEX_NAME
            index_path.write_bytes(canonical_index_bytes(index))
            check_index_v2(REPO_ROOT, worktree=True, output_path=index_path)
            index_path.write_bytes(index_path.read_bytes().replace(b"schema_version", b"schema-version", 1))
            with self.assertRaisesRegex(ReleaseEvidenceV2Error, "not deterministic"):
                check_index_v2(REPO_ROOT, worktree=True, output_path=index_path)


if __name__ == "__main__":
    unittest.main()
