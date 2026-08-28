import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from scripts.release_milestone_context import (
    EVIDENCE_INDEX_PATH,
    RELEASE_LEDGER_PATH,
    ReleaseMilestoneContext,
    ReleaseMilestoneContextError,
    _validate_failed_unpublished_candidate,
    _validate_release_evidence_tag_scope,
    _requires_unpublished_candidate_recovery,
    discover_milestone_manifest,
    discover_milestone_receipt,
    discover_terminal_v2_qualification,
    require_manifest_evidence_baseline,
    require_manifest_runner_sha,
    resolve_milestone_manifest_context,
    resolve_milestone_context,
    verify_qualified_v2_bundle,
    verify_published_release_receipt,
)
from scripts.release_qualification_manifest import (
    SIGNAL_ARCHIVE_NAME,
    SIGNAL_RECEIPT_NAME,
    SignedUiArtifactBinding,
    build_manifest,
    write_manifest,
)
from scripts.release_receipt import build_receipt, receipt_sha256, write_receipt


DMG_SHA256 = "a" * 64
CHECKSUM_SHA256 = "b" * 64
APPCAST_SHA256 = "c" * 64
APP_TREE_SHA256 = "d" * 64
REPO_ROOT = Path(__file__).resolve().parents[1]


class ReleaseMilestoneContextTests(unittest.TestCase):
    @staticmethod
    def build_repository(root: Path) -> Path:
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
        policy_path = root / "docs/qualification/release-qualification-policy-v1.json"
        evidence_path = root / "docs/qualification/release-evidence-v1.json"
        qualification_path = root / "docs/qualification/stable-signed-qualification-v1.json"
        route_table_path = root / "docs/qualification/video-quality-route-table-v2.json"
        receipt_path = root / "docs/release-evidence/v0.3.0/release-receipt.json"
        publication_path = root / "docs/release-evidence/v0.3.0/publication-record.json"
        release_ledger_path = root / RELEASE_LEDGER_PATH
        config_path = root / ".github/github.json"
        controller_path = root / ".github/workflows/milestone-qualification.yml"
        for path in (
            policy_path,
            evidence_path,
            qualification_path,
            route_table_path,
            receipt_path,
            publication_path,
            release_ledger_path,
            config_path,
            controller_path,
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
        policy_path.write_text(
            json.dumps(
                {
                    "cases": [
                        {
                            "allowed_evidence_sources": ["signed_artifact_receipt"],
                            "artifact_owned": True,
                            "blocking_phase": "publication",
                            "id": "profile-save-action-accessibility",
                            "invalidates_on": {"contracts": ["profile-storage"], "paths": []},
                            "tier": 2,
                        },
                        {
                            "allowed_evidence_sources": ["signed_artifact_receipt"],
                            "blocking_phase": "release_candidate",
                            "id": "beta-change-scoped-case",
                            "invalidates_on": {"contracts": [], "paths": []},
                            "tier": 2,
                        },
                    ],
                    "policy_id": "release-qualification-policy-v1",
                    "result_states": ["covered", "carry", "retest", "external"],
                    "schema_version": 1,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        evidence_path.write_text('{"schema_version": 1, "receipts": []}\n', encoding="utf-8")
        release_ledger_path.write_text('{"schema_version": 1, "releases": []}\n', encoding="utf-8")
        route_table_path.write_text('{"schema_version": 1, "routes": []}\n', encoding="utf-8")
        controller_path.write_text("name: Milestone Qualification\n", encoding="utf-8")
        config_path.write_text(
            json.dumps(
                {
                    "releaseOperations": {
                        "qualificationPolicyPath": "docs/qualification/release-qualification-policy-v1.json",
                        "qualificationEvidencePath": "docs/qualification/release-evidence-v1.json",
                        "qualificationRecordPath": "docs/qualification/stable-signed-qualification-v1.json",
                    }
                }
            )
            + "\n",
            encoding="utf-8",
        )
        qualification_path.write_text(
            json.dumps(
                {
                    "candidate": {
                        "package_version": "0.3.0",
                        "public_version": "0.3.0",
                        "build_version": "161",
                        "release_tag": "v0.3.0",
                        "dmg_name": "3D-Blu-ray-to-Vision-Pro-0.3.0.dmg",
                        "source_git_sha": None,
                        "workflow": "Stable",
                        "release_run_id": None,
                        "release_id": None,
                        "dmg_sha256": None,
                        "appcast_sha256": None,
                        "signed_app_tree_sha256": None,
                    }
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (root / "baseline.txt").write_text("baseline\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "baseline"], cwd=root, check=True)
        source_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        qualification_content = (
            json.dumps(
                {
                    "candidate": {
                        "package_version": "0.3.0",
                        "public_version": "0.3.0",
                        "build_version": "161",
                        "release_tag": "v0.3.0",
                        "dmg_name": "3D-Blu-ray-to-Vision-Pro-0.3.0.dmg",
                        "source_git_sha": source_sha,
                        "workflow": "Stable",
                        "release_run_id": 12345,
                        "release_id": 67890,
                        "dmg_sha256": DMG_SHA256,
                        "appcast_sha256": APPCAST_SHA256,
                        "signed_app_tree_sha256": APP_TREE_SHA256,
                    }
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        qualification_path.write_text(qualification_content, encoding="utf-8")
        qualification_record_path = root / "docs/release-evidence/v0.3.0/qualification-record.json"
        qualification_record_path.parent.mkdir(parents=True, exist_ok=True)
        qualification_record_path.write_text(qualification_content, encoding="utf-8")
        receipt = build_receipt(
            {
                "release_route": "stable",
                "source_sha": source_sha,
                "workflow_actor": "shiny-code-bot",
                "workflow_run_id": 12345,
                "workflow_run_attempt": 1,
                "package_version": "0.3.0",
                "public_version": "0.3.0",
                "build_version": "161",
                "release_tag": "v0.3.0",
                "release_name": "v0.3.0",
                "release_id": 67890,
                "release_created_at": "2026-08-06T12:00:00Z",
                "prerelease": False,
                "make_latest": True,
                "signed_app_tree_sha256": APP_TREE_SHA256,
                "artifacts": [
                    {
                        "kind": "dmg",
                        "name": "3D-Blu-ray-to-Vision-Pro-0.3.0.dmg",
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
        )
        write_receipt(receipt, receipt_path)
        publication_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "release_tag": "v0.3.0",
                    "release_id": 67890,
                    "source_sha": source_sha,
                    "workflow_run_id": 12345,
                    "workflow_conclusion": "success",
                    "published_at": "2026-08-06T13:00:00Z",
                    "receipt_asset_id": 4,
                    "receipt_file_sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
                    "live_pages": {
                        "state": "verified",
                        "sha256": APPCAST_SHA256,
                        "url": "https://cbusillo.github.io/BD_to_AVP/appcast.xml",
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "evidence"], cwd=root, check=True)
        return receipt_path

    @staticmethod
    def release_ledger_record(root: Path) -> dict[str, object]:
        receipt_path = root / "docs/release-evidence/v0.3.0/release-receipt.json"
        publication_path = root / "docs/release-evidence/v0.3.0/publication-record.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        publication = json.loads(publication_path.read_text(encoding="utf-8"))
        record = {
            "publication_record": publication_path.relative_to(root).as_posix(),
            "published_at": publication["published_at"],
            "receipt": receipt_path.relative_to(root).as_posix(),
            "receipt_file_sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
            "release_id": receipt["release"]["id"],
            "source_sha": receipt["source_sha"],
            "tag": receipt["release"]["tag"],
            "workflow_run_id": receipt["workflow"]["run_id"],
        }
        if "recovery_workflow_run" in publication:
            record["recovery_workflow_run"] = publication["recovery_workflow_run"]
        return record

    @staticmethod
    def prepare_failed_candidate_transition(
        root: Path,
        *,
        include_burned_build: bool = True,
        receipt_digest_override: str | None = None,
    ) -> tuple[str, str]:
        base_qualification_path = root / "docs/qualification/stable-signed-qualification-v1.json"
        base_qualification = json.loads(base_qualification_path.read_text(encoding="utf-8"))
        base_qualification["status"] = "preregistered_pending_exact_candidate"
        base_qualification["candidate"].update(
            {
                "package_version": "0.3.1b1",
                "public_version": "0.3.1-beta.1",
                "build_version": "162",
                "release_tag": "v0.3.1-beta.1",
                "dmg_name": "3D-Blu-ray-to-Vision-Pro-0.3.1-beta.1.dmg",
                "workflow": "Prerelease",
            }
        )
        for field in (
            "source_git_sha",
            "release_run_id",
            "release_id",
            "dmg_sha256",
            "appcast_sha256",
            "signed_app_tree_sha256",
        ):
            base_qualification["candidate"][field] = None
        base_qualification_path.write_text(json.dumps(base_qualification) + "\n", encoding="utf-8")
        subprocess.run(["git", "add", base_qualification_path], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "prepare failed beta"], cwd=root, check=True)
        base_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
        ).stdout.strip()

        release_tag = "v0.3.1-beta.1"
        attempt_root = root / "docs/release-attempts" / release_tag
        attempt_root.mkdir(parents=True)
        receipt_path = attempt_root / "release-receipt.json"
        receipt = build_receipt(
            {
                "release_route": "prerelease",
                "source_sha": base_sha,
                "workflow_actor": "shiny-code-bot",
                "workflow_run_id": 22222,
                "workflow_run_attempt": 1,
                "package_version": "0.3.1b1",
                "public_version": "0.3.1-beta.1",
                "build_version": "162",
                "release_tag": release_tag,
                "release_name": release_tag,
                "release_id": 33333,
                "release_created_at": "2026-08-21T01:00:00Z",
                "prerelease": True,
                "make_latest": False,
                "signed_app_tree_sha256": APP_TREE_SHA256,
                "artifacts": [
                    {
                        "kind": "dmg",
                        "name": "3D-Blu-ray-to-Vision-Pro-0.3.1-beta.1.dmg",
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
        )
        write_receipt(receipt, receipt_path)
        record = {
            "build_version": "162",
            "draft_deleted": True,
            "draft_release_id": 33333,
            "failed_job": "Prove signed artifact UI accessibility",
            "failed_job_id": 44444,
            "failed_step": "Run installed candidate UI accessibility lane",
            "must_not_rebuild": True,
            "package_version": "0.3.1b1",
            "public_version": "0.3.1-beta.1",
            "published_at": None,
            "recorded_at": "2026-08-21T02:00:00Z",
            "release_receipt_file_sha256": receipt_digest_override
            or hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
            "release_receipt_path": receipt_path.relative_to(root).as_posix(),
            "release_receipt_sha256": receipt["receipt_sha256"],
            "release_tag": release_tag,
            "schema_version": 1,
            "source_sha": base_sha,
            "status": "failed_unpublished_signed_candidate",
            "workflow_conclusion": "failure",
            "workflow_run_attempt": 1,
            "workflow_run_id": 22222,
        }
        (attempt_root / "failed-attempt-v1.json").write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        next_qualification = json.loads(json.dumps(base_qualification))
        next_qualification["candidate"].update(
            {
                "package_version": "0.3.1b2",
                "public_version": "0.3.1-beta.2",
                "build_version": "163",
                "release_tag": "v0.3.1-beta.2",
                "dmg_name": "3D-Blu-ray-to-Vision-Pro-0.3.1-beta.2.dmg",
            }
        )
        for field in (
            "source_git_sha",
            "release_run_id",
            "release_id",
            "dmg_sha256",
            "appcast_sha256",
            "signed_app_tree_sha256",
        ):
            next_qualification["candidate"][field] = None
        next_qualification["immutable_history"] = {"burned_builds": [162] if include_burned_build else []}
        next_path = root / "docs/qualification/v0.3.1-beta.2-signed-qualification-v1.json"
        next_path.write_text(json.dumps(next_qualification) + "\n", encoding="utf-8")
        config_path = root / ".github/github.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["releaseOperations"]["qualificationRecordPath"] = next_path.relative_to(root).as_posix()
        config_path.write_text(json.dumps(config) + "\n", encoding="utf-8")
        subprocess.run(["git", "add", attempt_root, next_path, config_path], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "prepare successor beta"], cwd=root, check=True)
        head_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
        ).stdout.strip()
        return base_sha, head_sha

    @staticmethod
    def prepare_published_prior_receipt_transition(
        root: Path,
        *,
        bind_base_candidate: bool = False,
        previous_beta_override: tuple[str, object] | None = None,
        include_extra_evidence_file: bool = False,
        include_generated_v2_index: bool = False,
        tamper_config: bool = False,
    ) -> tuple[str, str]:
        base_qualification_path = root / "docs/qualification/stable-signed-qualification-v1.json"
        base_qualification = json.loads(base_qualification_path.read_text(encoding="utf-8"))
        base_qualification["status"] = "preregistered_pending_exact_candidate"
        base_qualification["candidate"].update(
            {
                "package_version": "0.3.1b1",
                "public_version": "0.3.1-beta.1",
                "build_version": "162",
                "release_tag": "v0.3.1-beta.1",
                "dmg_name": "3D-Blu-ray-to-Vision-Pro-0.3.1-beta.1.dmg",
                "workflow": "Prerelease",
            }
        )
        for field in (
            "source_git_sha",
            "release_run_id",
            "release_id",
            "dmg_sha256",
            "appcast_sha256",
            "signed_app_tree_sha256",
        ):
            base_qualification["candidate"][field] = None
        if bind_base_candidate:
            base_qualification["candidate"]["release_id"] = 33333
        base_qualification_path.write_text(json.dumps(base_qualification) + "\n", encoding="utf-8")
        subprocess.run(["git", "add", base_qualification_path], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "prepare published beta"], cwd=root, check=True)
        base_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
        ).stdout.strip()
        release_tag = "v0.3.1-beta.1"
        subprocess.run(["git", "tag", release_tag, base_sha], cwd=root, check=True)

        receipt_path = root / f"docs/release-evidence/{release_tag}/release-receipt.json"
        receipt_path.parent.mkdir(parents=True)
        receipt = build_receipt(
            {
                "release_route": "prerelease",
                "source_sha": base_sha,
                "workflow_actor": "shiny-code-bot",
                "workflow_run_id": 22222,
                "workflow_run_attempt": 1,
                "package_version": "0.3.1b1",
                "public_version": "0.3.1-beta.1",
                "build_version": "162",
                "release_tag": release_tag,
                "release_name": release_tag,
                "release_id": 33333,
                "release_created_at": "2026-08-21T01:00:00Z",
                "prerelease": True,
                "make_latest": False,
                "signed_app_tree_sha256": APP_TREE_SHA256,
                "artifacts": [
                    {
                        "kind": "dmg",
                        "name": "3D-Blu-ray-to-Vision-Pro-0.3.1-beta.1.dmg",
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
        )
        write_receipt(receipt, receipt_path)
        next_qualification = json.loads(json.dumps(base_qualification))
        next_qualification["candidate"].update(
            {
                "package_version": "0.3.1b2",
                "public_version": "0.3.1-beta.2",
                "build_version": "163",
                "release_tag": "v0.3.1-beta.2",
                "dmg_name": "3D-Blu-ray-to-Vision-Pro-0.3.1-beta.2.dmg",
            }
        )
        for field in (
            "source_git_sha",
            "release_run_id",
            "release_id",
            "dmg_sha256",
            "appcast_sha256",
            "signed_app_tree_sha256",
        ):
            next_qualification["candidate"][field] = None
        next_qualification["immutable_history"] = {
            "burned_builds": [],
            "previous_beta": {
                "appcast_sha256": APPCAST_SHA256,
                "build_version": "162",
                "dmg_sha256": DMG_SHA256,
                "must_not_rebuild": True,
                "package_version": "0.3.1b1",
                "published_at": "2026-08-21T02:00:00Z",
                "release_id": 33333,
                "release_run_id": 22222,
                "release_tag": release_tag,
                "signed_app_tree_sha256": APP_TREE_SHA256,
                "source_git_sha": base_sha,
            },
        }
        if previous_beta_override is not None:
            field, value = previous_beta_override
            next_qualification["immutable_history"]["previous_beta"][field] = value
        next_path = root / "docs/qualification/v0.3.1-beta.2-signed-qualification-v1.json"
        next_path.write_text(json.dumps(next_qualification) + "\n", encoding="utf-8")
        config_path = root / ".github/github.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["releaseOperations"]["qualificationRecordPath"] = next_path.relative_to(root).as_posix()
        if tamper_config:
            config["releaseOperations"]["qualificationPolicyPath"] = "docs/qualification/weakened-policy.json"
        config_path.write_text(json.dumps(config) + "\n", encoding="utf-8")
        paths = [receipt_path, next_path, config_path]
        if include_extra_evidence_file:
            publication_path = receipt_path.with_name("publication-record.json")
            publication_path.write_text('{"schema_version": 1}\n', encoding="utf-8")
            paths.append(publication_path)
        if include_generated_v2_index:
            index_path = root / "docs/release-evidence/index-v2.json"
            index_path.write_text('{"legacy_index":{},"releases":[],"schema_version":2}\n', encoding="utf-8")
            paths.append(index_path)
        subprocess.run(["git", "add", *paths], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "prepare successor after published beta"], cwd=root, check=True)
        head_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
        ).stdout.strip()
        return base_sha, head_sha

    @staticmethod
    def append_beta_change_scoped_evidence(
        root: Path,
        *,
        case_id: str = "beta-change-scoped-case",
        source_sha: str | None = None,
        receipt_digest: str | None = None,
    ) -> tuple[str, str]:
        base_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        release_tag = "v0.3.1-beta.1"
        evidence_relative = f"docs/qualification/{release_tag}-change-scoped-evidence-v1.json"
        evidence_path = root / evidence_relative
        evidence = {
            "schema_version": 1,
            "qualification_id": "beta-change-scoped-v1",
            "result": "accepted_public_safe_summary",
            "source": {
                "package_version": "0.3.1b1",
                "public_version": "0.3.1-beta.1",
                "build_version": "162",
                "release_tag": release_tag,
                "source_sha": source_sha or base_sha,
            },
            "failed_preparation_run": {
                "signing_started": False,
                "release_identity_created": False,
            },
            "evidence_source_semantics": {
                "developer_id_or_notarization_claimed": False,
                "index_source": "signed_artifact_receipt",
            },
            "privacy": {
                "private_paths_recorded": False,
                "private_hostnames_recorded": False,
                "private_media_identifiers_recorded": False,
                "diagnostic_tokens_recorded": False,
            },
            "acceptance": {
                "passed": True,
                "passed_case_count": 1,
                "failed_case_count": 0,
                "blocking_case_ids": [],
            },
            "cases": [{"case_id": case_id, "status": "passed"}],
        }
        evidence_path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        digest = receipt_digest or hashlib.sha256(evidence_path.read_bytes()).hexdigest()
        evidence_index_path = root / EVIDENCE_INDEX_PATH
        evidence_index = json.loads(evidence_index_path.read_text(encoding="utf-8"))
        evidence_index["receipts"].append(
            {
                "case_id": case_id,
                "receipt_id": f"{release_tag}:{case_id}:{base_sha[:7]}",
                "source": "signed_artifact_receipt",
                "status": "accepted",
                "source_sha": source_sha or base_sha,
                "accepted_at": "2026-08-18T20:00:00Z",
                "reference": evidence_relative,
                "sha256": digest,
            }
        )
        evidence_index_path.write_text(json.dumps(evidence_index, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        subprocess.run(["git", "add", evidence_path, evidence_index_path], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "append beta evidence"], cwd=root, check=True)
        head_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        return base_sha, head_sha

    def test_resolves_checked_stable_milestone_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            receipt_path = self.build_repository(root)
            context = resolve_milestone_context(root, receipt_path)

        self.assertEqual(context.release_tag, "v0.3.0")
        self.assertEqual(context.release_stage, "stable")
        self.assertFalse(context.first_candidate_of_cycle)
        self.assertEqual(context.qualification_path, "docs/qualification/stable-signed-qualification-v1.json")

    def test_resolves_legacy_release_record_by_tag_when_current_config_advances(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            receipt_path = self.build_repository(root)
            current_path = root / "docs/qualification/current-signed-qualification-v1.json"
            current = json.loads(
                (root / "docs/qualification/stable-signed-qualification-v1.json").read_text(encoding="utf-8")
            )
            current["candidate"]["release_tag"] = "v1.0.1-beta.1"
            current_path.write_text(json.dumps(current) + "\n", encoding="utf-8")
            config_path = root / ".github/github.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["releaseOperations"]["qualificationRecordPath"] = current_path.relative_to(root).as_posix()
            config_path.write_text(json.dumps(config) + "\n", encoding="utf-8")

            context = resolve_milestone_context(root, receipt_path)

        self.assertEqual(context.release_tag, "v0.3.0")
        self.assertEqual(context.qualification_path, "docs/qualification/stable-signed-qualification-v1.json")

    def test_resolves_manifest_milestone_context_without_migrating_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            receipt_path = self.build_repository(root)
            policy_path = root / "docs/qualification/release-qualification-policy-v1.json"
            policy_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "policy_id": "release-qualification-policy-v1",
                        "result_states": ["covered", "carry", "retest", "external"],
                        "tiers": {},
                        "contracts": {"profile-storage": [], "release-engine": []},
                        "cases": [
                            {
                                "id": "release-workflow-identity",
                                "tier": 1,
                                "blocking_phase": "publication",
                                "invalidates_on": {"paths": [], "contracts": ["release-engine"]},
                                "allowed_evidence_sources": ["release_run_receipt"],
                            },
                            {
                                "id": "profile-save-action-accessibility",
                                "tier": 2,
                                "blocking_phase": "publication",
                                "artifact_owned": True,
                                "invalidates_on": {"paths": [], "contracts": ["profile-storage"]},
                                "allowed_evidence_sources": ["signed_artifact_receipt"],
                            },
                        ],
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            policy_path.write_bytes(
                (REPO_ROOT / "docs/qualification/release-qualification-policy-v1.json").read_bytes()
            )
            route_table_path = root / "docs/qualification/video-quality-route-table-v2.json"
            route_table_path.write_bytes(
                (REPO_ROOT / "docs/qualification/video-quality-route-table-v2.json").read_bytes()
            )
            receipt_document = json.loads(receipt_path.read_text(encoding="utf-8"))
            artifacts = {item["kind"]: item for item in receipt_document["artifacts"]}
            qualification = json.loads(
                (REPO_ROOT / "docs/qualification/stable-signed-qualification-v1.json").read_text(encoding="utf-8")
            )
            qualification["candidate"].update(
                {
                    "appcast_sha256": artifacts["appcast"]["sha256"],
                    "build_version": receipt_document["versions"]["build"],
                    "dmg_name": artifacts["dmg"]["name"],
                    "dmg_sha256": artifacts["dmg"]["sha256"],
                    "package_version": receipt_document["versions"]["package"],
                    "public_version": receipt_document["versions"]["public"],
                    "release_id": receipt_document["release"]["id"],
                    "release_run_id": receipt_document["workflow"]["run_id"],
                    "release_tag": receipt_document["release"]["tag"],
                    "signed_app_tree_sha256": receipt_document["signed_app_tree_sha256"],
                    "source_git_sha": receipt_document["source_sha"],
                    "workflow": receipt_document["workflow"]["name"],
                }
            )
            qualification_content = json.dumps(qualification, indent=2, sort_keys=True) + "\n"
            qualification_path = root / "docs/qualification/stable-signed-qualification-v1.json"
            qualification_record_path = root / "docs/release-evidence/v0.3.0/qualification-record.json"
            qualification_path.write_text(qualification_content, encoding="utf-8")
            qualification_record_path.write_text(qualification_content, encoding="utf-8")
            subprocess.run(
                ["git", "add", policy_path, route_table_path, qualification_path, qualification_record_path],
                cwd=root,
                check=True,
            )
            subprocess.run(["git", "commit", "-qm", "manifest inputs"], cwd=root, check=True)
            prior_path = root / "docs/release-evidence/v0.2.143/release-receipt.json"
            prior_path.parent.mkdir(parents=True)
            prior = build_receipt(
                {
                    "release_route": "stable",
                    "source_sha": "e" * 40,
                    "workflow_actor": "shiny-code-bot",
                    "workflow_run_id": 111,
                    "workflow_run_attempt": 1,
                    "package_version": "0.2.143",
                    "public_version": "0.2.143",
                    "build_version": "143",
                    "release_tag": "v0.2.143",
                    "release_name": "v0.2.143",
                    "release_id": 222,
                    "release_created_at": "2026-08-01T12:00:00Z",
                    "prerelease": False,
                    "make_latest": True,
                    "signed_app_tree_sha256": APP_TREE_SHA256,
                    "artifacts": [
                        {
                            "kind": "dmg",
                            "name": "3D-Blu-ray-to-Vision-Pro-0.2.143.dmg",
                            "sha256": "1" * 64,
                            "size_bytes": 1000,
                            "asset_id": 11,
                        },
                        {
                            "kind": "checksum",
                            "name": "SHA256SUMS",
                            "sha256": "2" * 64,
                            "size_bytes": 100,
                            "asset_id": 12,
                        },
                        {
                            "kind": "appcast",
                            "name": "appcast.xml",
                            "sha256": "3" * 64,
                            "size_bytes": 500,
                            "asset_id": 13,
                        },
                    ],
                }
            )
            write_receipt(prior, prior_path)
            subprocess.run(["git", "add", prior_path], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "record prior release evidence"], cwd=root, check=True)
            signed_ui_path = root / "docs/release-evidence/v0.3.0/signed-artifact-ui-receipt.json"
            signed_ui = {
                "case_id": "profile-save-action-accessibility",
                "candidate_sha": json.loads(receipt_path.read_text(encoding="utf-8"))["source_sha"],
                "dmg": {
                    "asset_id": 1,
                    "name": "3D-Blu-ray-to-Vision-Pro-0.3.0.dmg",
                    "sha256": DMG_SHA256,
                    "size_bytes": 1000,
                },
                "evidence": [
                    {"kind": "accessibility-tree", "sha256": "4" * 64},
                    {"kind": "screenshot-dark", "sha256": "5" * 64},
                    {"kind": "screenshot-light", "sha256": "6" * 64},
                    {"kind": "ui-result", "sha256": "7" * 64},
                ],
                "policy_id": "release-qualification-policy-v1",
                "receipt_type": "bd-to-avp-signed-artifact-ui",
                "release": {"id": 67890, "tag": "v0.3.0"},
                "release_receipt": {
                    "asset_id": 4,
                    "file_sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
                    "receipt_sha256": json.loads(receipt_path.read_text(encoding="utf-8"))["receipt_sha256"],
                },
                "schema_version": 1,
                "signed_app_tree_sha256": APP_TREE_SHA256,
                "source": "signed_artifact_receipt",
                "status": "passed",
                "test_selector": (
                    "BluRayToVisionProUITests/InstalledUIAcceptanceTests/testCandidateMainWindowProfileAndSettings"
                ),
                "versions": {"build": "161", "package": "0.3.0", "public": "0.3.0"},
                "workflow": {"job": "signed-artifact-ui", "run_attempt": 1, "run_id": 12345},
            }
            signed_ui["receipt_sha256"] = receipt_sha256(signed_ui)
            signed_ui_path.write_text(
                json.dumps(signed_ui, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            signed_archive_path = signed_ui_path.parent / SIGNAL_ARCHIVE_NAME
            archive_info = zipfile.ZipInfo(SIGNAL_RECEIPT_NAME, date_time=(2026, 1, 1, 0, 0, 0))
            archive_info.compress_type = zipfile.ZIP_DEFLATED
            archive_info.external_attr = 0o100644 << 16
            with zipfile.ZipFile(signed_archive_path, "w") as archive:
                archive.writestr(archive_info, signed_ui_path.read_bytes())
            runner_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            manifest = build_manifest(
                repo_root=root,
                release_receipt_path=receipt_path,
                prior_release_receipt_path=prior_path,
                signed_ui=SignedUiArtifactBinding(
                    artifact_id=999,
                    artifact_digest=hashlib.sha256(signed_archive_path.read_bytes()).hexdigest(),
                    archive_path="docs/release-evidence/v0.3.0/signed-artifact-ui.zip",
                    receipt_path="docs/release-evidence/v0.3.0/signed-artifact-ui-receipt.json",
                    receipt_file_sha256=hashlib.sha256(signed_ui_path.read_bytes()).hexdigest(),
                    receipt_sha256=signed_ui["receipt_sha256"],
                ),
                evidence_ref="automation/release-evidence-v0.3.0",
                evidence_base_sha=subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=root,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip(),
                runner_sha=runner_sha,
                sparkle_route="stable",
            )
            manifest_path = root / "docs/release-evidence/v0.3.0/qualification-manifest.json"
            write_manifest(manifest, manifest_path)
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "manifest"], cwd=root, check=True)
            commits = subprocess.run(
                ["git", "rev-list", "--reverse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.splitlines()

            discovered = discover_milestone_manifest(
                root,
                base_sha=commits[-2],
                head_sha=commits[-1],
                head_branch="automation/release-evidence-v0.3.0",
                base_repo="cbusillo/BD_to_AVP",
                head_repo="cbusillo/BD_to_AVP",
                base_branch="main",
            )
            context = resolve_milestone_manifest_context(root, manifest_path)
            require_manifest_evidence_baseline(context, root, commits[-2])
            with self.assertRaisesRegex(ReleaseMilestoneContextError, "evidence baseline"):
                require_manifest_evidence_baseline(
                    replace(context, evidence_index_base_sha256="0" * 64),
                    root,
                    commits[-2],
                )

        self.assertEqual(discovered, manifest_path)
        self.assertEqual(context.manifest_path, "docs/release-evidence/v0.3.0/qualification-manifest.json")
        self.assertEqual(context.manifest_sha256, manifest["manifest_sha256"])
        self.assertEqual(context.runner_sha, runner_sha)
        require_manifest_runner_sha(context, REPO_ROOT, runner_sha)

    def test_manifest_runner_sha_accepts_validation_only_descendant_base(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.build_repository(root)
            release_test = root / "tests/test_release.py"
            milestone_context = root / "scripts/release_milestone_context.py"
            release_test.parent.mkdir(parents=True, exist_ok=True)
            milestone_context.parent.mkdir(parents=True, exist_ok=True)
            release_test.write_text("prepared = True\n", encoding="utf-8")
            milestone_context.write_text("strict = True\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "runner"], cwd=root, check=True)
            runner_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            release_test.write_text("published = True\n", encoding="utf-8")
            milestone_context.write_text("compatible = True\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "validation compatibility"], cwd=root, check=True)
            base_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            context = ReleaseMilestoneContext(
                candidate_sha="a" * 40,
                evidence_path=EVIDENCE_INDEX_PATH,
                first_candidate_of_cycle=True,
                policy_path="docs/qualification/release-qualification-policy-v1.json",
                qualification_path="docs/qualification/stable-signed-qualification-v1.json",
                release_receipt_path="docs/release-evidence/v0.3.0/release-receipt.json",
                release_stage="stable",
                release_tag="v0.3.0",
                runner_sha=runner_sha,
            )

            require_manifest_runner_sha(context, root, base_sha)

    def test_manifest_runner_sha_rejects_non_validation_base_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.build_repository(root)
            runner_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            application_path = root / "bd_to_avp/app.py"
            application_path.parent.mkdir(parents=True, exist_ok=True)
            application_path.write_text("changed = True\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "application change"], cwd=root, check=True)
            base_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            context = ReleaseMilestoneContext(
                candidate_sha="a" * 40,
                evidence_path=EVIDENCE_INDEX_PATH,
                first_candidate_of_cycle=True,
                policy_path="docs/qualification/release-qualification-policy-v1.json",
                qualification_path="docs/qualification/stable-signed-qualification-v1.json",
                release_receipt_path="docs/release-evidence/v0.3.0/release-receipt.json",
                release_stage="stable",
                release_tag="v0.3.0",
                runner_sha=runner_sha,
            )

            with self.assertRaisesRegex(ReleaseMilestoneContextError, "validation-only"):
                require_manifest_runner_sha(context, root, base_sha)

    def test_manifest_runner_sha_rejects_non_descendant_base(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.build_repository(root)
            baseline_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            runner_path = root / "tests/runner.py"
            runner_path.parent.mkdir(parents=True, exist_ok=True)
            runner_path.write_text("runner = True\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "runner"], cwd=root, check=True)
            runner_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            subprocess.run(["git", "switch", "-q", "--detach", baseline_sha], cwd=root, check=True)
            base_path = root / "tests/base.py"
            base_path.parent.mkdir(parents=True, exist_ok=True)
            base_path.write_text("base = True\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "base"], cwd=root, check=True)
            base_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            context = ReleaseMilestoneContext(
                candidate_sha="a" * 40,
                evidence_path=EVIDENCE_INDEX_PATH,
                first_candidate_of_cycle=True,
                policy_path="docs/qualification/release-qualification-policy-v1.json",
                qualification_path="docs/qualification/stable-signed-qualification-v1.json",
                release_receipt_path="docs/release-evidence/v0.3.0/release-receipt.json",
                release_stage="stable",
                release_tag="v0.3.0",
                runner_sha=runner_sha,
            )

            with self.assertRaisesRegex(ReleaseMilestoneContextError, "ancestor"):
                require_manifest_runner_sha(context, root, base_sha)

    def test_manifest_discovery_rejects_rewritten_evidence_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.build_repository(root)
            evidence_path = root / EVIDENCE_INDEX_PATH
            evidence_path.write_text(
                json.dumps({"schema_version": 1, "receipts": [{"receipt_id": "accepted"}]}) + "\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "add", evidence_path], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "accept evidence"], cwd=root, check=True)
            base_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            manifest_path = root / "docs/release-evidence/v0.3.0/qualification-manifest.json"
            manifest_path.write_text("{}\n", encoding="utf-8")
            evidence_path.write_text(
                json.dumps({"schema_version": 1, "receipts": [{"receipt_id": "replacement"}]}) + "\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "add", manifest_path, evidence_path], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "rewrite evidence with manifest"], cwd=root, check=True)
            head_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            with self.assertRaisesRegex(ReleaseMilestoneContextError, "only append receipts"):
                discover_milestone_manifest(
                    root,
                    base_sha=base_sha,
                    head_sha=head_sha,
                    head_branch="automation/release-evidence-v0.3.0",
                    base_repo="cbusillo/BD_to_AVP",
                    head_repo="cbusillo/BD_to_AVP",
                    base_branch="main",
                )

    def test_manifest_discovery_rejects_runner_bound_policy_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.build_repository(root)
            base_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            policy_path = root / "docs/qualification/release-qualification-policy-v1.json"
            policy = json.loads(policy_path.read_text(encoding="utf-8"))
            policy["policy_id"] = "changed-on-evidence-branch"
            policy_path.write_text(json.dumps(policy, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            manifest_path = root / "docs/release-evidence/v0.3.0/qualification-manifest.json"
            manifest_path.write_text("{}\n", encoding="utf-8")
            subprocess.run(["git", "add", policy_path, manifest_path], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "mutate runner inputs"], cwd=root, check=True)
            head_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            with self.assertRaisesRegex(ReleaseMilestoneContextError, "runner-bound policy or route inputs"):
                discover_milestone_manifest(
                    root,
                    base_sha=base_sha,
                    head_sha=head_sha,
                    head_branch="automation/release-evidence-v0.3.0",
                    base_repo="cbusillo/BD_to_AVP",
                    head_repo="cbusillo/BD_to_AVP",
                    base_branch="main",
                )

    def test_accepts_explicit_successful_recovery_for_failed_release_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            receipt_path = self.build_repository(root)
            publication_path = root / "docs/release-evidence/v0.3.0/publication-record.json"
            publication = json.loads(publication_path.read_text(encoding="utf-8"))
            publication["workflow_conclusion"] = "failure"
            publication["recovery_workflow_run"] = {
                "operation": "pypi_recovery",
                "workflow_conclusion": "success",
                "workflow_run_id": 54321,
            }
            publication_path.write_text(json.dumps(publication) + "\n", encoding="utf-8")

            context = resolve_milestone_context(root, receipt_path)

        self.assertEqual(context.release_tag, "v0.3.0")

    def test_rejects_mismatched_qualification_identity(self) -> None:
        for field, value in (
            ("build_version", "162"),
            ("release_run_id", 12346),
            ("release_id", 67891),
            ("dmg_sha256", "0" * 64),
            ("appcast_sha256", "0" * 64),
            ("signed_app_tree_sha256", "0" * 64),
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary_directory:
                root = Path(temporary_directory)
                receipt_path = self.build_repository(root)
                qualification_path = root / "docs/qualification/stable-signed-qualification-v1.json"
                qualification = json.loads(qualification_path.read_text(encoding="utf-8"))
                qualification["candidate"][field] = value
                qualification_path.write_text(json.dumps(qualification) + "\n", encoding="utf-8")

                with self.assertRaisesRegex(ReleaseMilestoneContextError, f"candidate.{field}"):
                    resolve_milestone_context(root, receipt_path)

    def test_rejects_mismatched_publication_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            receipt_path = self.build_repository(root)
            publication_path = root / "docs/release-evidence/v0.3.0/publication-record.json"
            publication = json.loads(publication_path.read_text(encoding="utf-8"))
            publication["receipt_file_sha256"] = "0" * 64
            publication_path.write_text(json.dumps(publication) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(ReleaseMilestoneContextError, "receipt_file_sha256"):
                resolve_milestone_context(root, receipt_path)

    def test_discovers_only_canonical_release_evidence_branch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            receipt_path = self.build_repository(root)
            commits = subprocess.run(
                ["git", "rev-list", "--reverse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.splitlines()
            discovered = discover_milestone_receipt(
                root,
                base_sha=commits[0],
                head_sha=commits[-1],
                head_branch="automation/release-evidence-v0.3.0",
                base_repo="cbusillo/BD_to_AVP",
                head_repo="cbusillo/BD_to_AVP",
                base_branch="main",
            )
            with self.assertRaisesRegex(ReleaseMilestoneContextError, "idempotent branch"):
                discover_milestone_receipt(
                    root,
                    base_sha=commits[0],
                    head_sha=commits[-1],
                    head_branch="copy/evidence-v0.3.0",
                    base_repo="cbusillo/BD_to_AVP",
                    head_repo="cbusillo/BD_to_AVP",
                    base_branch="main",
                )

            with self.assertRaisesRegex(ReleaseMilestoneContextError, "same-repository"):
                discover_milestone_receipt(
                    root,
                    base_sha=commits[0],
                    head_sha=commits[-1],
                    head_branch="automation/release-evidence-v0.3.0",
                    base_repo="cbusillo/BD_to_AVP",
                    head_repo="external/BD_to_AVP",
                    base_branch="main",
                )

        self.assertEqual(discovered, receipt_path)

    def test_rejects_untracked_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            receipt_path = self.build_repository(root)
            subprocess.run(["git", "rm", "--cached", receipt_path.relative_to(root)], cwd=root, check=True)

            with self.assertRaisesRegex(ReleaseMilestoneContextError, "must be tracked"):
                resolve_milestone_context(root, receipt_path)

    def test_rejects_unbound_published_qualification_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.build_repository(root)
            base_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            qualification_path = root / "docs/qualification/stable-signed-qualification-v1.json"
            qualification = json.loads(qualification_path.read_text(encoding="utf-8"))
            qualification["status"] = "published_immutable_qualified"
            qualification_path.write_text(json.dumps(qualification) + "\n", encoding="utf-8")
            subprocess.run(["git", "add", qualification_path.relative_to(root)], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "mutate qualification"], cwd=root, check=True)
            head_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            with self.assertRaisesRegex(ReleaseMilestoneContextError, "Published qualification"):
                discover_milestone_receipt(
                    root,
                    base_sha=base_sha,
                    head_sha=head_sha,
                    head_branch="fix/qualification",
                    base_repo="cbusillo/BD_to_AVP",
                    head_repo="cbusillo/BD_to_AVP",
                    base_branch="main",
                )

    def test_rejects_prior_receipt_carry_forward_outside_exact_prepare_branch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.build_repository(root)
            base_sha, head_sha = self.prepare_published_prior_receipt_transition(root)

            with self.assertRaisesRegex(ReleaseMilestoneContextError, "preparation branch"):
                discover_milestone_receipt(
                    root,
                    base_sha=base_sha,
                    head_sha=head_sha,
                    head_branch="feature/carry-receipt",
                    base_repo="cbusillo/BD_to_AVP",
                    head_repo="cbusillo/BD_to_AVP",
                    base_branch="main",
                    published_receipt_verifier=lambda _path, _receipt, _digest: None,
                )

    def test_rejects_prior_receipt_when_base_candidate_is_partially_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.build_repository(root)
            base_sha, head_sha = self.prepare_published_prior_receipt_transition(root, bind_base_candidate=True)

            with self.assertRaisesRegex(ReleaseMilestoneContextError, "every base immutable candidate field"):
                discover_milestone_receipt(
                    root,
                    base_sha=base_sha,
                    head_sha=head_sha,
                    head_branch="prepare/v0.3.1-beta.2",
                    base_repo="cbusillo/BD_to_AVP",
                    head_repo="cbusillo/BD_to_AVP",
                    base_branch="main",
                    published_receipt_verifier=lambda _path, _receipt, _digest: None,
                )

    def test_rejects_prior_receipt_with_unrelated_github_config_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.build_repository(root)
            base_sha, head_sha = self.prepare_published_prior_receipt_transition(root, tamper_config=True)

            with self.assertRaisesRegex(ReleaseMilestoneContextError, "only by advancing qualificationRecordPath"):
                discover_milestone_receipt(
                    root,
                    base_sha=base_sha,
                    head_sha=head_sha,
                    head_branch="prepare/v0.3.1-beta.2",
                    base_repo="cbusillo/BD_to_AVP",
                    head_repo="cbusillo/BD_to_AVP",
                    base_branch="main",
                    published_receipt_verifier=lambda _path, _receipt, _digest: None,
                )

    def test_verifies_carried_receipt_against_immutable_release_and_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            receipt_path = self.build_repository(root)
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt_digest = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
            release = receipt["release"]
            workflow = receipt["workflow"]
            release_response = {
                "id": release["id"],
                "tag_name": release["tag"],
                "target_commitish": receipt["source_sha"],
                "draft": False,
                "prerelease": release["prerelease"],
                "immutable": True,
                "assets": [
                    {
                        "name": "release-receipt.json",
                        "size": receipt_path.stat().st_size,
                        "digest": f"sha256:{receipt_digest}",
                    }
                ],
            }
            run_response = {
                "id": workflow["run_id"],
                "name": workflow["name"],
                "path": workflow["path"],
                "head_sha": receipt["source_sha"],
                "status": "completed",
                "conclusion": "success",
                "run_attempt": workflow["run_attempt"],
                "event": "workflow_dispatch",
                "head_branch": "main",
                "actor": {"login": workflow["actor"]},
            }
            responses = [
                subprocess.CompletedProcess([], 0, stdout=json.dumps(release_response), stderr=""),
                subprocess.CompletedProcess([], 0, stdout=json.dumps(run_response), stderr=""),
            ]

            with patch("scripts.release_milestone_context.subprocess.run", side_effect=responses):
                verify_published_release_receipt(receipt_path, receipt, receipt_digest)

    def test_rejects_carried_receipt_that_differs_from_published_asset(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            receipt_path = self.build_repository(root)
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt_digest = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
            release = receipt["release"]
            release_response = {
                "id": release["id"],
                "tag_name": release["tag"],
                "target_commitish": receipt["source_sha"],
                "draft": False,
                "prerelease": release["prerelease"],
                "immutable": True,
                "assets": [
                    {
                        "name": "release-receipt.json",
                        "size": receipt_path.stat().st_size,
                        "digest": f"sha256:{'0' * 64}",
                    }
                ],
            }

            with (
                patch(
                    "scripts.release_milestone_context.subprocess.run",
                    return_value=subprocess.CompletedProcess([], 0, stdout=json.dumps(release_response), stderr=""),
                ),
                self.assertRaisesRegex(ReleaseMilestoneContextError, "asset field digest"),
            ):
                verify_published_release_receipt(receipt_path, receipt, receipt_digest)

    def test_allows_prepublication_qualification_evidence_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.build_repository(root)
            base_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            qualification_path = root / "docs/qualification/stable-signed-qualification-v1.json"
            qualification = json.loads(qualification_path.read_text(encoding="utf-8"))
            for field in (
                "source_git_sha",
                "release_run_id",
                "release_id",
                "dmg_sha256",
                "appcast_sha256",
                "signed_app_tree_sha256",
            ):
                qualification["candidate"][field] = None
            qualification["candidate"].update(
                {
                    "package_version": "0.3.1",
                    "public_version": "0.3.1",
                    "build_version": "162",
                    "release_tag": "v0.3.1",
                    "dmg_name": "3D-Blu-ray-to-Vision-Pro-0.3.1.dmg",
                }
            )
            qualification_path.write_text(json.dumps(qualification) + "\n", encoding="utf-8")
            evidence_path = root / "docs/qualification/release-evidence-v1.json"
            evidence_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "receipts": [
                            {
                                "case_id": "network-generated-final-output",
                                "receipt_id": "v0.3.1-preparation",
                            }
                        ],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "add", qualification_path, evidence_path], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "prepare release evidence"], cwd=root, check=True)
            head_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            discovered = discover_milestone_receipt(
                root,
                base_sha=base_sha,
                head_sha=head_sha,
                head_branch="release/v0.3.1",
                base_repo="cbusillo/BD_to_AVP",
                head_repo="cbusillo/BD_to_AVP",
                base_branch="main",
            )

        self.assertIsNone(discovered)

    def test_allows_configured_prepublication_prerelease_record_to_advance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.build_repository(root)
            base_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            base_qualification_path = root / "docs/qualification/stable-signed-qualification-v1.json"
            qualification = json.loads(base_qualification_path.read_text(encoding="utf-8"))
            for field in (
                "source_git_sha",
                "release_run_id",
                "release_id",
                "dmg_sha256",
                "appcast_sha256",
                "signed_app_tree_sha256",
            ):
                qualification["candidate"][field] = None
            qualification["candidate"].update(
                {
                    "package_version": "0.3.1b1",
                    "public_version": "0.3.1-beta.1",
                    "build_version": "162",
                    "release_tag": "v0.3.1-beta.1",
                    "dmg_name": "3D-Blu-ray-to-Vision-Pro-0.3.1-beta.1.dmg",
                    "workflow": "Prerelease",
                }
            )
            next_qualification_path = root / "docs/qualification/v0.3.1-beta.1-signed-qualification-v1.json"
            next_qualification_path.write_text(json.dumps(qualification) + "\n", encoding="utf-8")
            config_path = root / ".github/github.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["releaseOperations"]["qualificationRecordPath"] = next_qualification_path.relative_to(
                root
            ).as_posix()
            config_path.write_text(json.dumps(config) + "\n", encoding="utf-8")
            subprocess.run(["git", "add", next_qualification_path, config_path], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "prepare prerelease qualification"], cwd=root, check=True)
            head_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            discovered = discover_milestone_receipt(
                root,
                base_sha=base_sha,
                head_sha=head_sha,
                head_branch="release/v0.3.1-beta.1",
                base_repo="cbusillo/BD_to_AVP",
                head_repo="cbusillo/BD_to_AVP",
                base_branch="main",
            )

        self.assertIsNone(discovered)

    def test_allows_failed_unpublished_signed_candidate_to_advance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.build_repository(root)
            base_sha, head_sha = self.prepare_failed_candidate_transition(root)

            discovered = discover_milestone_receipt(
                root,
                base_sha=base_sha,
                head_sha=head_sha,
                head_branch="prepare/v0.3.1-beta.2",
                base_repo="cbusillo/BD_to_AVP",
                head_repo="cbusillo/BD_to_AVP",
                base_branch="main",
                published_receipt_verifier=lambda _path, _receipt, _digest: None,
            )

        self.assertIsNone(discovered)

    def test_allows_exact_published_prior_receipt_to_advance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.build_repository(root)
            base_sha, head_sha = self.prepare_published_prior_receipt_transition(root)

            discovered = discover_milestone_receipt(
                root,
                base_sha=base_sha,
                head_sha=head_sha,
                head_branch="prepare/v0.3.1-beta.2",
                base_repo="cbusillo/BD_to_AVP",
                head_repo="cbusillo/BD_to_AVP",
                base_branch="main",
                published_receipt_verifier=lambda _path, _receipt, _digest: None,
            )

        self.assertIsNone(discovered)

    def test_allows_generated_v2_index_with_published_prior_receipt_carry_forward(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.build_repository(root)
            base_sha, head_sha = self.prepare_published_prior_receipt_transition(
                root,
                include_generated_v2_index=True,
            )

            discovered = discover_milestone_receipt(
                root,
                base_sha=base_sha,
                head_sha=head_sha,
                head_branch="prepare/v0.3.1-beta.2",
                base_repo="cbusillo/BD_to_AVP",
                head_repo="cbusillo/BD_to_AVP",
                base_branch="main",
                published_receipt_verifier=lambda _path, _receipt, _digest: None,
            )

        self.assertIsNone(discovered)

    def test_rejects_published_prior_receipt_identity_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.build_repository(root)
            base_sha, head_sha = self.prepare_published_prior_receipt_transition(
                root,
                previous_beta_override=("dmg_sha256", "0" * 64),
            )

            with self.assertRaisesRegex(ReleaseMilestoneContextError, "previous_beta.dmg_sha256"):
                discover_milestone_receipt(
                    root,
                    base_sha=base_sha,
                    head_sha=head_sha,
                    head_branch="prepare/v0.3.1-beta.2",
                    base_repo="cbusillo/BD_to_AVP",
                    head_repo="cbusillo/BD_to_AVP",
                    base_branch="main",
                    published_receipt_verifier=lambda _path, _receipt, _digest: None,
                )

    def test_rejects_extra_release_evidence_with_prior_receipt_carry_forward(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.build_repository(root)
            base_sha, head_sha = self.prepare_published_prior_receipt_transition(
                root,
                include_extra_evidence_file=True,
            )

            with self.assertRaisesRegex(ReleaseMilestoneContextError, "may add only the exact checked receipt"):
                discover_milestone_receipt(
                    root,
                    base_sha=base_sha,
                    head_sha=head_sha,
                    head_branch="prepare/v0.3.1-beta.2",
                    base_repo="cbusillo/BD_to_AVP",
                    head_repo="cbusillo/BD_to_AVP",
                    base_branch="main",
                    published_receipt_verifier=lambda _path, _receipt, _digest: None,
                )

    def test_rejects_failed_candidate_with_wrong_receipt_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.build_repository(root)
            base_sha, head_sha = self.prepare_failed_candidate_transition(
                root,
                receipt_digest_override="0" * 64,
            )

            with self.assertRaisesRegex(ReleaseMilestoneContextError, "file digest"):
                discover_milestone_receipt(
                    root,
                    base_sha=base_sha,
                    head_sha=head_sha,
                    head_branch="prepare/v0.3.1-beta.2",
                    base_repo="cbusillo/BD_to_AVP",
                    head_repo="cbusillo/BD_to_AVP",
                    base_branch="main",
                )

    def test_rejects_successor_that_does_not_burn_failed_build(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.build_repository(root)
            base_sha, head_sha = self.prepare_failed_candidate_transition(root, include_burned_build=False)

            with self.assertRaisesRegex(ReleaseMilestoneContextError, "permanently burn"):
                discover_milestone_receipt(
                    root,
                    base_sha=base_sha,
                    head_sha=head_sha,
                    head_branch="prepare/v0.3.1-beta.2",
                    base_repo="cbusillo/BD_to_AVP",
                    head_repo="cbusillo/BD_to_AVP",
                    base_branch="main",
                )

    def test_cancelled_attempt_blocks_successor_without_authorized_draft_deletion(self) -> None:
        base_candidate = {
            "package_version": "0.3.2b6",
            "public_version": "0.3.2-beta.6",
            "build_version": "168",
            "release_tag": "v0.3.2-beta.6",
            "dmg_name": "3D-Blu-ray-to-Vision-Pro-0.3.2-beta.6.dmg",
            "workflow": "Prerelease",
        }

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            attempt_source = REPO_ROOT / "docs/release-attempts/v0.3.2-beta.6"
            attempt_destination = root / "docs/release-attempts/v0.3.2-beta.6"
            shutil.copytree(attempt_source, attempt_destination)
            (attempt_destination / "draft-deletion-v1.json").unlink()
            config_destination = root / ".github/github.json"
            config_destination.parent.mkdir(parents=True)
            shutil.copy2(REPO_ROOT / ".github/github.json", config_destination)

            with self.assertRaisesRegex(ReleaseMilestoneContextError, "authorized draft-deletion"):
                _validate_failed_unpublished_candidate(
                    root,
                    base_sha="0" * 40,
                    base_qualification={"status": "preregistered_pending_exact_candidate"},
                    base_candidate=base_candidate,
                )

    def test_bound_cancelled_attempt_still_requires_recovery(self) -> None:
        base_candidate = {
            "release_tag": "v0.3.2-beta.6",
            **{
                field: "bound"
                for field in (
                    "source_git_sha",
                    "release_run_id",
                    "release_id",
                    "dmg_sha256",
                    "appcast_sha256",
                    "signed_app_tree_sha256",
                )
            },
        }

        self.assertTrue(_requires_unpublished_candidate_recovery(REPO_ROOT, base_candidate))

    def test_allows_beta_change_scoped_evidence_append_without_checked_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.build_repository(root)
            base_sha, head_sha = self.append_beta_change_scoped_evidence(root)

            discovered = discover_milestone_receipt(
                root,
                base_sha=base_sha,
                head_sha=head_sha,
                head_branch="qualify/v0.3.1-beta.1",
                base_repo="cbusillo/BD_to_AVP",
                head_repo="cbusillo/BD_to_AVP",
                base_branch="main",
            )

        self.assertIsNone(discovered)

    def test_rejects_beta_change_scoped_evidence_on_wrong_branch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.build_repository(root)
            base_sha, head_sha = self.append_beta_change_scoped_evidence(root)

            with self.assertRaisesRegex(ReleaseMilestoneContextError, "qualify/v0.3.1-beta.1"):
                discover_milestone_receipt(
                    root,
                    base_sha=base_sha,
                    head_sha=head_sha,
                    head_branch="feature/beta-evidence",
                    base_repo="cbusillo/BD_to_AVP",
                    head_repo="cbusillo/BD_to_AVP",
                    base_branch="main",
                )

    def test_rejects_beta_change_scoped_evidence_digest_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.build_repository(root)
            base_sha, head_sha = self.append_beta_change_scoped_evidence(root, receipt_digest="0" * 64)

            with self.assertRaisesRegex(ReleaseMilestoneContextError, "receipt identity"):
                discover_milestone_receipt(
                    root,
                    base_sha=base_sha,
                    head_sha=head_sha,
                    head_branch="qualify/v0.3.1-beta.1",
                    base_repo="cbusillo/BD_to_AVP",
                    head_repo="cbusillo/BD_to_AVP",
                    base_branch="main",
                )

    def test_rejects_beta_change_scoped_evidence_for_artifact_owned_case(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.build_repository(root)
            base_sha, head_sha = self.append_beta_change_scoped_evidence(
                root,
                case_id="profile-save-action-accessibility",
            )

            with self.assertRaisesRegex(ReleaseMilestoneContextError, "release-candidate Tier 2"):
                discover_milestone_receipt(
                    root,
                    base_sha=base_sha,
                    head_sha=head_sha,
                    head_branch="qualify/v0.3.1-beta.1",
                    base_repo="cbusillo/BD_to_AVP",
                    head_repo="cbusillo/BD_to_AVP",
                    base_branch="main",
                )

    def test_rejects_beta_change_scoped_evidence_for_non_base_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.build_repository(root)
            base_sha, head_sha = self.append_beta_change_scoped_evidence(root, source_sha="f" * 40)

            with self.assertRaisesRegex(ReleaseMilestoneContextError, "base SHA"):
                discover_milestone_receipt(
                    root,
                    base_sha=base_sha,
                    head_sha=head_sha,
                    head_branch="qualify/v0.3.1-beta.1",
                    base_repo="cbusillo/BD_to_AVP",
                    head_repo="cbusillo/BD_to_AVP",
                    base_branch="main",
                )

    def test_rejects_beta_change_scoped_evidence_with_policy_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.build_repository(root)
            base_sha, _head_sha = self.append_beta_change_scoped_evidence(root)
            policy_path = root / "docs/qualification/release-qualification-policy-v1.json"
            policy = json.loads(policy_path.read_text(encoding="utf-8"))
            policy["policy_id"] = "self-authorized-policy"
            policy_path.write_text(json.dumps(policy, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            subprocess.run(["git", "add", policy_path], cwd=root, check=True)
            subprocess.run(["git", "commit", "--amend", "-qm", "append beta evidence and policy"], cwd=root, check=True)
            head_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            with self.assertRaisesRegex(ReleaseMilestoneContextError, "runner-bound policy"):
                discover_milestone_receipt(
                    root,
                    base_sha=base_sha,
                    head_sha=head_sha,
                    head_branch="qualify/v0.3.1-beta.1",
                    base_repo="cbusillo/BD_to_AVP",
                    head_repo="cbusillo/BD_to_AVP",
                    base_branch="main",
                )

    def test_rejects_beta_change_scoped_evidence_with_stable_reset(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.build_repository(root)
            base_sha, _head_sha = self.append_beta_change_scoped_evidence(root)
            qualification_path = root / "docs/qualification/stable-signed-qualification-v1.json"
            qualification = json.loads(qualification_path.read_text(encoding="utf-8"))
            for field in (
                "source_git_sha",
                "release_run_id",
                "release_id",
                "dmg_sha256",
                "appcast_sha256",
                "signed_app_tree_sha256",
            ):
                qualification["candidate"][field] = None
            qualification["candidate"].update(
                {
                    "package_version": "0.3.1",
                    "public_version": "0.3.1",
                    "build_version": "162",
                    "release_tag": "v0.3.1",
                    "dmg_name": "3D-Blu-ray-to-Vision-Pro-0.3.1.dmg",
                }
            )
            qualification_path.write_text(json.dumps(qualification) + "\n", encoding="utf-8")
            subprocess.run(["git", "add", qualification_path], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "--amend", "-qm", "combine beta evidence and stable reset"],
                cwd=root,
                check=True,
            )
            head_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            with self.assertRaisesRegex(ReleaseMilestoneContextError, "may not be combined"):
                discover_milestone_receipt(
                    root,
                    base_sha=base_sha,
                    head_sha=head_sha,
                    head_branch="qualify/v0.3.1-beta.1",
                    base_repo="cbusillo/BD_to_AVP",
                    head_repo="cbusillo/BD_to_AVP",
                    base_branch="main",
                )

    def test_rejects_same_version_prepublication_reset(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.build_repository(root)
            base_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            qualification_path = root / "docs/qualification/stable-signed-qualification-v1.json"
            qualification = json.loads(qualification_path.read_text(encoding="utf-8"))
            for field in (
                "source_git_sha",
                "release_run_id",
                "release_id",
                "dmg_sha256",
                "appcast_sha256",
                "signed_app_tree_sha256",
            ):
                qualification["candidate"][field] = None
            qualification_path.write_text(json.dumps(qualification) + "\n", encoding="utf-8")
            subprocess.run(["git", "add", qualification_path], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "reset same candidate"], cwd=root, check=True)
            head_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            with self.assertRaisesRegex(ReleaseMilestoneContextError, "newer Stable"):
                discover_milestone_receipt(
                    root,
                    base_sha=base_sha,
                    head_sha=head_sha,
                    head_branch="release/v0.3.0",
                    base_repo="cbusillo/BD_to_AVP",
                    head_repo="cbusillo/BD_to_AVP",
                    base_branch="main",
                )

    def test_rejects_missing_prepublication_immutable_field(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.build_repository(root)
            base_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            qualification_path = root / "docs/qualification/stable-signed-qualification-v1.json"
            qualification = json.loads(qualification_path.read_text(encoding="utf-8"))
            for field in (
                "source_git_sha",
                "release_run_id",
                "release_id",
                "dmg_sha256",
                "appcast_sha256",
                "signed_app_tree_sha256",
            ):
                qualification["candidate"][field] = None
            del qualification["candidate"]["source_git_sha"]
            qualification["candidate"].update(
                {
                    "package_version": "0.3.1",
                    "public_version": "0.3.1",
                    "build_version": "162",
                    "release_tag": "v0.3.1",
                    "dmg_name": "3D-Blu-ray-to-Vision-Pro-0.3.1.dmg",
                }
            )
            qualification_path.write_text(json.dumps(qualification) + "\n", encoding="utf-8")
            subprocess.run(["git", "add", qualification_path], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "omit immutable field"], cwd=root, check=True)
            head_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            with self.assertRaisesRegex(ReleaseMilestoneContextError, "explicitly include"):
                discover_milestone_receipt(
                    root,
                    base_sha=base_sha,
                    head_sha=head_sha,
                    head_branch="release/v0.3.1",
                    base_repo="cbusillo/BD_to_AVP",
                    head_repo="cbusillo/BD_to_AVP",
                    base_branch="main",
                )

    def test_rejects_prepublication_evidence_history_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.build_repository(root)
            evidence_path = root / "docs/qualification/release-evidence-v1.json"
            evidence_path.write_text(
                json.dumps({"schema_version": 1, "receipts": [{"receipt_id": "existing"}]}) + "\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "add", evidence_path], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "record existing evidence"], cwd=root, check=True)
            base_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            qualification_path = root / "docs/qualification/stable-signed-qualification-v1.json"
            qualification = json.loads(qualification_path.read_text(encoding="utf-8"))
            for field in (
                "source_git_sha",
                "release_run_id",
                "release_id",
                "dmg_sha256",
                "appcast_sha256",
                "signed_app_tree_sha256",
            ):
                qualification["candidate"][field] = None
            qualification["candidate"].update(
                {
                    "package_version": "0.3.1",
                    "public_version": "0.3.1",
                    "build_version": "162",
                    "release_tag": "v0.3.1",
                    "dmg_name": "3D-Blu-ray-to-Vision-Pro-0.3.1.dmg",
                }
            )
            qualification_path.write_text(json.dumps(qualification) + "\n", encoding="utf-8")
            evidence_path.write_text(
                json.dumps({"schema_version": 1, "receipts": [{"receipt_id": "replacement"}]}) + "\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "add", qualification_path, evidence_path], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "rewrite evidence"], cwd=root, check=True)
            head_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            with self.assertRaisesRegex(ReleaseMilestoneContextError, "only append receipts"):
                discover_milestone_receipt(
                    root,
                    base_sha=base_sha,
                    head_sha=head_sha,
                    head_branch="release/v0.3.1",
                    base_repo="cbusillo/BD_to_AVP",
                    head_repo="cbusillo/BD_to_AVP",
                    base_branch="main",
                )

    def test_recovery_authorization_does_not_impersonate_checked_release_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.build_repository(root)
            base_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            recovery_path = root / "docs/release-evidence/v0.3.0-pypi-recovery.json"
            recovery_path.write_text('{"schema": "bd_to_avp.pypi_recovery"}\n', encoding="utf-8")
            subprocess.run(["git", "add", recovery_path.relative_to(root)], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "add recovery authorization"], cwd=root, check=True)
            head_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            discovered = discover_milestone_receipt(
                root,
                base_sha=base_sha,
                head_sha=head_sha,
                head_branch="fix/stable-pypi-recovery",
                base_repo="cbusillo/BD_to_AVP",
                head_repo="cbusillo/BD_to_AVP",
                base_branch="main",
            )

        self.assertIsNone(discovered)

    def test_lookalike_future_recovery_authorization_requires_checked_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.build_repository(root)
            base_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            recovery_path = root / "docs/release-evidence/v0.3.1-pypi-recovery.json"
            recovery_path.write_text('{"schema": "bd_to_avp.pypi_recovery"}\n', encoding="utf-8")
            subprocess.run(["git", "add", recovery_path.relative_to(root)], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "add lookalike recovery authorization"], cwd=root, check=True)
            head_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            with self.assertRaisesRegex(ReleaseMilestoneContextError, "exactly one checked release receipt"):
                discover_milestone_receipt(
                    root,
                    base_sha=base_sha,
                    head_sha=head_sha,
                    head_branch="fix/stable-pypi-recovery",
                    base_repo="cbusillo/BD_to_AVP",
                    head_repo="cbusillo/BD_to_AVP",
                    base_branch="main",
                )

    def test_manifest_discovery_allows_release_ledger_append(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.build_repository(root)
            base_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            manifest_path = root / "docs/release-evidence/v0.3.0/qualification-manifest.json"
            manifest_path.write_text("{}\n", encoding="utf-8")
            ledger_path = root / RELEASE_LEDGER_PATH
            ledger_path.write_text(
                json.dumps({"schema_version": 1, "releases": [self.release_ledger_record(root)]}) + "\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "add", manifest_path, ledger_path], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "append release ledger"], cwd=root, check=True)
            head_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            discovered = discover_milestone_manifest(
                root,
                base_sha=base_sha,
                head_sha=head_sha,
                head_branch="automation/release-evidence-v0.3.0",
                base_repo="cbusillo/BD_to_AVP",
                head_repo="cbusillo/BD_to_AVP",
                base_branch="main",
            )

        self.assertEqual(discovered, manifest_path)

    def test_manifest_discovery_rejects_release_ledger_history_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.build_repository(root)
            ledger_path = root / RELEASE_LEDGER_PATH
            ledger_path.write_text(
                json.dumps({"schema_version": 1, "releases": [{"tag": "v0.2.9", "release_id": 1}]}) + "\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "add", ledger_path], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "record prior release"], cwd=root, check=True)
            base_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            manifest_path = root / "docs/release-evidence/v0.3.0/qualification-manifest.json"
            manifest_path.write_text("{}\n", encoding="utf-8")
            ledger_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "releases": [
                            {"tag": "v0.2.9", "release_id": 2},
                            self.release_ledger_record(root),
                        ],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "add", manifest_path, ledger_path], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "rewrite release ledger"], cwd=root, check=True)
            head_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            with self.assertRaisesRegex(ReleaseMilestoneContextError, "changing accepted history"):
                discover_milestone_manifest(
                    root,
                    base_sha=base_sha,
                    head_sha=head_sha,
                    head_branch="automation/release-evidence-v0.3.0",
                    base_repo="cbusillo/BD_to_AVP",
                    head_repo="cbusillo/BD_to_AVP",
                    base_branch="main",
                )

    def test_manifest_discovery_rejects_release_ledger_foreign_tag_append(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.build_repository(root)
            base_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            manifest_path = root / "docs/release-evidence/v0.3.0/qualification-manifest.json"
            manifest_path.write_text("{}\n", encoding="utf-8")
            ledger_path = root / RELEASE_LEDGER_PATH
            ledger_path.write_text(
                json.dumps({"schema_version": 1, "releases": [{"tag": "v0.3.1"}]}) + "\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "add", manifest_path, ledger_path], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "append foreign release"], cwd=root, check=True)
            head_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            with self.assertRaisesRegex(ReleaseMilestoneContextError, "pull request release tag"):
                discover_milestone_manifest(
                    root,
                    base_sha=base_sha,
                    head_sha=head_sha,
                    head_branch="automation/release-evidence-v0.3.0",
                    base_repo="cbusillo/BD_to_AVP",
                    head_repo="cbusillo/BD_to_AVP",
                    base_branch="main",
                )

    def test_manifest_discovery_rejects_release_ledger_record_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.build_repository(root)
            base_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            manifest_path = root / "docs/release-evidence/v0.3.0/qualification-manifest.json"
            manifest_path.write_text("{}\n", encoding="utf-8")
            record = self.release_ledger_record(root)
            record["receipt"] = "docs/release-evidence/v0.2.9/release-receipt.json"
            ledger_path = root / RELEASE_LEDGER_PATH
            ledger_path.write_text(
                json.dumps({"schema_version": 1, "releases": [record]}) + "\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "add", manifest_path, ledger_path], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "forge release ledger"], cwd=root, check=True)
            head_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            with self.assertRaisesRegex(ReleaseMilestoneContextError, "checked release receipt"):
                discover_milestone_manifest(
                    root,
                    base_sha=base_sha,
                    head_sha=head_sha,
                    head_branch="automation/release-evidence-v0.3.0",
                    base_repo="cbusillo/BD_to_AVP",
                    head_repo="cbusillo/BD_to_AVP",
                    base_branch="main",
                )

    def test_manifest_discovery_allows_recovered_release_ledger_append(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.build_repository(root)
            base_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            publication_path = root / "docs/release-evidence/v0.3.0/publication-record.json"
            publication = json.loads(publication_path.read_text(encoding="utf-8"))
            publication["workflow_conclusion"] = "failure"
            publication["recovery_workflow_run"] = {
                "operation": "pypi_recovery",
                "workflow_conclusion": "success",
                "workflow_run_id": 12346,
            }
            publication_path.write_text(json.dumps(publication) + "\n", encoding="utf-8")
            manifest_path = root / "docs/release-evidence/v0.3.0/qualification-manifest.json"
            manifest_path.write_text("{}\n", encoding="utf-8")
            ledger_path = root / RELEASE_LEDGER_PATH
            ledger_path.write_text(
                json.dumps({"schema_version": 1, "releases": [self.release_ledger_record(root)]}) + "\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "add", manifest_path, publication_path, ledger_path], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "append recovered release ledger"], cwd=root, check=True)
            head_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            discovered = discover_milestone_manifest(
                root,
                base_sha=base_sha,
                head_sha=head_sha,
                head_branch="automation/release-evidence-v0.3.0",
                base_repo="cbusillo/BD_to_AVP",
                head_repo="cbusillo/BD_to_AVP",
                base_branch="main",
            )

        self.assertEqual(discovered, manifest_path)

    def test_receipt_discovery_validates_release_ledger_append(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            receipt_path = self.build_repository(root)
            base_sha = subprocess.run(
                ["git", "rev-list", "--reverse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.splitlines()[0]
            ledger_path = root / RELEASE_LEDGER_PATH
            ledger_path.write_text(
                json.dumps({"schema_version": 1, "releases": [self.release_ledger_record(root)]}) + "\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "add", ledger_path], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "append release ledger"], cwd=root, check=True)
            head_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            discovered = discover_milestone_receipt(
                root,
                base_sha=base_sha,
                head_sha=head_sha,
                head_branch="automation/release-evidence-v0.3.0",
                base_repo="cbusillo/BD_to_AVP",
                head_repo="cbusillo/BD_to_AVP",
                base_branch="main",
            )

        self.assertEqual(discovered, receipt_path)

    def test_manifest_discovery_rejects_changed_receipt_for_another_release(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.build_repository(root)
            base_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            manifest_path = root / "docs/release-evidence/v0.3.0/qualification-manifest.json"
            manifest_path.write_text("{}\n", encoding="utf-8")
            unrelated_receipt = root / "docs/release-evidence/v0.3.1/release-receipt.json"
            unrelated_receipt.parent.mkdir(parents=True, exist_ok=True)
            unrelated_receipt.write_text("{}\n", encoding="utf-8")
            subprocess.run(["git", "add", manifest_path, unrelated_receipt], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "mix release evidence"], cwd=root, check=True)
            head_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            with self.assertRaisesRegex(ReleaseMilestoneContextError, "same release tag"):
                discover_milestone_manifest(
                    root,
                    base_sha=base_sha,
                    head_sha=head_sha,
                    head_branch="automation/release-evidence-v0.3.0",
                    base_repo="cbusillo/BD_to_AVP",
                    head_repo="cbusillo/BD_to_AVP",
                    base_branch="main",
                )

    def test_manifest_discovery_rejects_other_release_evidence_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.build_repository(root)
            base_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            manifest_path = root / "docs/release-evidence/v0.3.0/qualification-manifest.json"
            manifest_path.write_text("{}\n", encoding="utf-8")
            unrelated_record = root / "docs/release-evidence/v0.3.1/publication-record.json"
            unrelated_record.parent.mkdir(parents=True, exist_ok=True)
            unrelated_record.write_text("{}\n", encoding="utf-8")
            subprocess.run(["git", "add", manifest_path, unrelated_record], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "mix immutable release evidence"], cwd=root, check=True)
            head_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            with self.assertRaisesRegex(ReleaseMilestoneContextError, "only one release tag"):
                discover_milestone_manifest(
                    root,
                    base_sha=base_sha,
                    head_sha=head_sha,
                    head_branch="automation/release-evidence-v0.3.0",
                    base_repo="cbusillo/BD_to_AVP",
                    head_repo="cbusillo/BD_to_AVP",
                    base_branch="main",
                )

    def test_other_release_evidence_still_requires_checked_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.build_repository(root)
            base_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            unbound_path = root / "docs/release-evidence/v0.3.1-unbound.json"
            unbound_path.write_text("{}\n", encoding="utf-8")
            subprocess.run(["git", "add", unbound_path.relative_to(root)], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "add unbound release evidence"], cwd=root, check=True)
            head_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            with self.assertRaisesRegex(ReleaseMilestoneContextError, "exactly one checked release receipt"):
                discover_milestone_receipt(
                    root,
                    base_sha=base_sha,
                    head_sha=head_sha,
                    head_branch="fix/stable-pypi-recovery",
                    base_repo="cbusillo/BD_to_AVP",
                    head_repo="cbusillo/BD_to_AVP",
                    base_branch="main",
                )

    def test_generated_release_evidence_v2_index_does_not_require_checked_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.build_repository(root)
            base_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            index_path = root / "docs/release-evidence/index-v2.json"
            index_path.write_text('{"legacy_index":{},"releases":[],"schema_version":2}\n', encoding="utf-8")
            subprocess.run(["git", "add", index_path.relative_to(root)], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "add generated release evidence v2 index"], cwd=root, check=True)
            head_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            discovered = discover_milestone_receipt(
                root,
                base_sha=base_sha,
                head_sha=head_sha,
                head_branch="code/issue-662-shadow-capture",
                base_repo="cbusillo/BD_to_AVP",
                head_repo="cbusillo/BD_to_AVP",
                base_branch="main",
            )

        self.assertIsNone(discovered)

    def test_generated_release_evidence_v2_index_is_allowed_with_single_tag_evidence(self) -> None:
        _validate_release_evidence_tag_scope(
            (
                "docs/release-evidence/v0.3.0/release-receipt.json",
                "docs/release-evidence/v0.3.0/capture-v2.json",
                "docs/release-evidence/index-v2.json",
            ),
            "v0.3.0",
        )

    def test_terminal_v2_qualification_skips_legacy_milestone_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.build_repository(root)
            base_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            release_tag = "v1.0.0"
            bundle = root / "docs/release-evidence" / release_tag
            bundle.mkdir(parents=True)
            (bundle / "qualification-v2.json").write_text("{}\n", encoding="utf-8")
            (root / "docs/release-evidence/index-v2.json").write_text("{}\n", encoding="utf-8")
            subprocess.run(["git", "add", "docs/release-evidence"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "add terminal v2 evidence"], cwd=root, check=True)
            head_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            verified: list[tuple[Path, str, str]] = []

            def verify(repo_root: Path, tag: str, verified_base_sha: str) -> Mapping[str, object]:
                verified.append((repo_root, tag, verified_base_sha))
                return {"class": "v2-qualified"}

            discovered = discover_terminal_v2_qualification(
                root,
                base_sha=base_sha,
                head_sha=head_sha,
                head_branch=f"automation/release-evidence-{release_tag}",
                base_repo="cbusillo/BD_to_AVP",
                head_repo="cbusillo/BD_to_AVP",
                base_branch="main",
                qualified_v2_verifier=verify,
            )

        self.assertEqual(discovered, release_tag)
        self.assertEqual(verified, [(root, release_tag, base_sha)])

    def test_terminal_v2_qualification_rejects_nonqualified_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.build_repository(root)
            base_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            release_tag = "v1.0.0"
            bundle = root / "docs/release-evidence" / release_tag
            bundle.mkdir(parents=True)
            (bundle / "qualification-v2.json").write_text("{}\n", encoding="utf-8")
            (root / "docs/release-evidence/index-v2.json").write_text("{}\n", encoding="utf-8")
            subprocess.run(["git", "add", "docs/release-evidence"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "add nonterminal v2 evidence"], cwd=root, check=True)
            head_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            with self.assertRaisesRegex(ReleaseMilestoneContextError, "class 'v2-qualified'"):
                discover_terminal_v2_qualification(
                    root,
                    base_sha=base_sha,
                    head_sha=head_sha,
                    head_branch=f"automation/release-evidence-{release_tag}",
                    base_repo="cbusillo/BD_to_AVP",
                    head_repo="cbusillo/BD_to_AVP",
                    base_branch="main",
                    qualified_v2_verifier=lambda _root, _tag, _base: {"class": "v2-captured"},
                )

    def test_qualified_v2_verifier_uses_worktree_and_write_once_base(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout='{"verified":[{"class":"v2-qualified","release_tag":"v1.0.0"}]}\n',
                stderr="",
            )
            with patch("scripts.release_milestone_context.subprocess.run", return_value=completed) as run:
                result = verify_qualified_v2_bundle(root, "v1.0.0", "a" * 40)

        self.assertEqual(result, {"class": "v2-qualified", "release_tag": "v1.0.0"})
        run.assert_called_once_with(
            [
                sys.executable,
                "-m",
                "scripts.release_evidence_v2",
                "--repo-root",
                str(root.resolve()),
                "--tag",
                "v1.0.0",
                "--worktree",
                "--base-revision",
                "a" * 40,
            ],
            cwd=root.resolve(),
            capture_output=True,
            text=True,
            check=False,
        )

    def test_rejects_non_docs_changes_on_evidence_branch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.build_repository(root)
            commits = subprocess.run(
                ["git", "rev-list", "--reverse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.splitlines()
            (root / "unexpected.txt").write_text("unexpected\n", encoding="utf-8")
            subprocess.run(["git", "add", "unexpected.txt"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "out of scope"], cwd=root, check=True)
            head_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            with self.assertRaisesRegex(ReleaseMilestoneContextError, "only docs"):
                discover_milestone_receipt(
                    root,
                    base_sha=commits[0],
                    head_sha=head_sha,
                    head_branch="automation/release-evidence-v0.3.0",
                    base_repo="cbusillo/BD_to_AVP",
                    head_repo="cbusillo/BD_to_AVP",
                    base_branch="main",
                )


if __name__ == "__main__":
    unittest.main()
