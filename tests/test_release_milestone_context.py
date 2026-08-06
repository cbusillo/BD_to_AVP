import hashlib
import json
import subprocess
import tempfile
import unittest

from pathlib import Path

from scripts.release_milestone_context import (
    ReleaseMilestoneContextError,
    discover_milestone_receipt,
    resolve_milestone_context,
)
from scripts.release_receipt import build_receipt, write_receipt


DMG_SHA256 = "a" * 64
CHECKSUM_SHA256 = "b" * 64
APPCAST_SHA256 = "c" * 64
APP_TREE_SHA256 = "d" * 64


class ReleaseMilestoneContextTests(unittest.TestCase):
    @staticmethod
    def build_repository(root: Path) -> Path:
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
        policy_path = root / "docs/qualification/release-qualification-policy-v1.json"
        evidence_path = root / "docs/qualification/release-evidence-v1.json"
        qualification_path = root / "docs/qualification/stable-signed-qualification-v1.json"
        receipt_path = root / "docs/release-evidence/v0.3.0/release-receipt.json"
        publication_path = root / "docs/release-evidence/v0.3.0/publication-record.json"
        config_path = root / ".github/github.json"
        for path in (policy_path, evidence_path, qualification_path, receipt_path, publication_path, config_path):
            path.parent.mkdir(parents=True, exist_ok=True)
        policy_path.write_text("{}\n", encoding="utf-8")
        evidence_path.write_text('{"schema_version": 1, "receipts": []}\n', encoding="utf-8")
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

        qualification_path.write_text(
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
                }
            )
            + "\n",
            encoding="utf-8",
        )
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
