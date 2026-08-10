import hashlib
import json
import subprocess
import tempfile
import unittest
import zipfile

from dataclasses import replace
from pathlib import Path

from scripts.release_milestone_context import (
    EVIDENCE_INDEX_PATH,
    ReleaseMilestoneContextError,
    discover_milestone_manifest,
    discover_milestone_receipt,
    require_manifest_evidence_baseline,
    require_manifest_runner_sha,
    resolve_milestone_manifest_context,
    resolve_milestone_context,
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
        config_path = root / ".github/github.json"
        controller_path = root / ".github/workflows/milestone-qualification.yml"
        for path in (
            policy_path,
            evidence_path,
            qualification_path,
            route_table_path,
            receipt_path,
            publication_path,
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
                        }
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

    def test_resolves_checked_stable_milestone_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            receipt_path = self.build_repository(root)
            context = resolve_milestone_context(root, receipt_path)

        self.assertEqual(context.release_tag, "v0.3.0")
        self.assertEqual(context.release_stage, "stable")
        self.assertFalse(context.first_candidate_of_cycle)
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
        require_manifest_runner_sha(context, runner_sha)
        with self.assertRaisesRegex(ReleaseMilestoneContextError, "runner SHA"):
            require_manifest_runner_sha(context, "8" * 40)

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
