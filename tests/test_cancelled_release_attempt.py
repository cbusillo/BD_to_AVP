import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from scripts.release_milestone_context import (
    ReleaseMilestoneContextError,
    _validate_failed_unpublished_candidate,
    build_draft_deletion_authorization_fingerprint,
    main,
    validate_cancelled_attempt_record,
    validate_draft_deletion_record,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
RELEASE_TAG = "v0.3.2-beta.6"


class CancelledReleaseAttemptTests(unittest.TestCase):
    @staticmethod
    def copy_attempt(root: Path) -> Path:
        source = REPO_ROOT / "docs" / "release-attempts" / RELEASE_TAG
        destination = root / "docs" / "release-attempts" / RELEASE_TAG
        shutil.copytree(source, destination)
        config_destination = root / ".github" / "github.json"
        config_destination.parent.mkdir(parents=True)
        shutil.copy2(REPO_ROOT / ".github" / "github.json", config_destination)
        return destination

    @staticmethod
    def write_deletion_record(root: Path, *, fingerprint: str | None = None) -> Path:
        attempt_root = root / "docs" / "release-attempts" / RELEASE_TAG
        attempt_path = attempt_root / "cancelled-attempt-v1.json"
        attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
        authorization_actor = "cbusillo"
        record = {
            "action": "delete_abandoned_unpublished_draft",
            "attempt_record_file_sha256": hashlib.sha256(attempt_path.read_bytes()).hexdigest(),
            "attempt_record_path": f"docs/release-attempts/{RELEASE_TAG}/cancelled-attempt-v1.json",
            "authorization_actor": authorization_actor,
            "authorization_fingerprint": fingerprint
            or build_draft_deletion_authorization_fingerprint(
                release_tag=RELEASE_TAG,
                draft_release_id=attempt["draft_release_id"],
                source_sha=attempt["source_sha"],
                authorization_actor=authorization_actor,
            ),
            "deleted_at": "2026-08-21T19:34:00Z",
            "deleted_by": "shiny-code-bot",
            "deletion_reason": "explicitly_authorized_abandoned_draft_cleanup",
            "draft_deleted": True,
            "draft_exists": False,
            "draft_release_id": attempt["draft_release_id"],
            "published_at": None,
            "receipt_file_sha256": attempt["release_receipt_file_sha256"],
            "recorded_at": "2026-08-21T19:35:00Z",
            "release_tag": RELEASE_TAG,
            "schema_version": 1,
            "source_sha": attempt["source_sha"],
            "status": "deleted_abandoned_unpublished_draft",
            "tag_exists": False,
            "verified_absent_at": "2026-08-21T19:34:30Z",
        }
        path = attempt_root / "draft-deletion-v1.json"
        path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path

    def test_repository_record_binds_cancelled_signed_attempt(self) -> None:
        record = validate_cancelled_attempt_record(REPO_ROOT, RELEASE_TAG)

        self.assertEqual(record["build_version"], "168")
        self.assertEqual(record["draft_release_id"], 374538590)
        self.assertFalse(record["draft_deleted"])
        self.assertTrue(record["must_not_rebuild"])

    def test_repository_draft_deletion_disposition_validates(self) -> None:
        attempt = validate_cancelled_attempt_record(REPO_ROOT, RELEASE_TAG)
        disposition = validate_draft_deletion_record(REPO_ROOT, RELEASE_TAG, attempt)

        self.assertEqual(disposition["status"], "deleted_abandoned_unpublished_draft")
        self.assertEqual(disposition["draft_release_id"], 374538590)
        self.assertEqual(
            disposition["authorization_fingerprint"],
            "0ac759e7d6332cc2f3555083fd7ec74c0ec938afbb3f0be1005e5e0cbe4ce193",
        )

    def assert_mutation_rejected(self, field: str, value: object, message: str) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            destination = self.copy_attempt(root)
            record_path = destination / "cancelled-attempt-v1.json"
            record = json.loads(record_path.read_text(encoding="utf-8"))
            record[field] = value
            record_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(ReleaseMilestoneContextError, message):
                validate_cancelled_attempt_record(root, RELEASE_TAG)

    def test_rejects_deleted_draft_claim(self) -> None:
        self.assert_mutation_rejected("draft_deleted", True, "draft-present")

    def test_rejects_non_cancelled_workflow_conclusion(self) -> None:
        self.assert_mutation_rejected("workflow_conclusion", "failure", "draft-present")

    def test_rejects_receipt_file_digest_mismatch(self) -> None:
        self.assert_mutation_rejected("release_receipt_file_sha256", "0" * 64, "file digest")

    def test_rejects_receipt_asset_identity_mismatch(self) -> None:
        self.assert_mutation_rejected("release_receipt_asset_id", 1, "receipt asset identity")

    def test_rejects_invalid_signing_approval_fingerprint(self) -> None:
        self.assert_mutation_rejected("signing_approval_fingerprint", "invalid", "SHA-256")

    def test_rejects_draft_asset_digest_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            destination = self.copy_attempt(root)
            record_path = destination / "cancelled-attempt-v1.json"
            record = json.loads(record_path.read_text(encoding="utf-8"))
            record["draft_assets"][0]["sha256"] = "0" * 64
            record_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(ReleaseMilestoneContextError, "differs from the checked release receipt"):
                validate_cancelled_attempt_record(root, RELEASE_TAG)

    def test_authorized_draft_deletion_unlocks_successor_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.copy_attempt(root)
            self.write_deletion_record(root)
            attempt = validate_cancelled_attempt_record(root, RELEASE_TAG)

            disposition = validate_draft_deletion_record(root, RELEASE_TAG, attempt)
            build = _validate_failed_unpublished_candidate(
                root,
                base_sha="0" * 40,
                base_qualification={"status": "preregistered_pending_exact_candidate"},
                base_candidate={
                    "package_version": "0.3.2b6",
                    "public_version": "0.3.2-beta.6",
                    "build_version": "168",
                    "release_tag": RELEASE_TAG,
                    "dmg_name": "3D-Blu-ray-to-Vision-Pro-0.3.2-beta.6.dmg",
                    "workflow": "Prerelease",
                },
            )

        self.assertEqual(disposition["status"], "deleted_abandoned_unpublished_draft")
        self.assertEqual(build, 168)

    def test_rejects_invalid_draft_deletion_authorization_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.copy_attempt(root)
            self.write_deletion_record(root, fingerprint="0" * 64)
            attempt = validate_cancelled_attempt_record(root, RELEASE_TAG)

            with self.assertRaisesRegex(ReleaseMilestoneContextError, "authorization fingerprint"):
                validate_draft_deletion_record(root, RELEASE_TAG, attempt)

    def test_rejects_mutation_of_committed_draft_deletion_disposition(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            attempt_root = self.copy_attempt(root)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "record deletion"], cwd=root, check=True)
            base_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                capture_output=True,
                check=True,
                text=True,
            ).stdout.strip()
            deletion_path = attempt_root / "draft-deletion-v1.json"
            deletion = json.loads(deletion_path.read_text(encoding="utf-8"))
            deletion["deletion_reason"] = "mutated_after_commit"
            deletion_path.write_text(json.dumps(deletion, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(ReleaseMilestoneContextError, "must remain immutable"):
                _validate_failed_unpublished_candidate(
                    root,
                    base_sha=base_sha,
                    base_qualification={"status": "preregistered_pending_exact_candidate"},
                    base_candidate={
                        "package_version": "0.3.2b6",
                        "public_version": "0.3.2-beta.6",
                        "build_version": "168",
                        "release_tag": RELEASE_TAG,
                        "dmg_name": "3D-Blu-ray-to-Vision-Pro-0.3.2-beta.6.dmg",
                        "workflow": "Prerelease",
                    },
                )

    def test_draft_deletion_cli_writes_validated_github_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.copy_attempt(root)
            self.write_deletion_record(root)
            output_path = root / "github-output.txt"
            stdout = StringIO()

            with redirect_stdout(stdout):
                result = main(
                    [
                        "--draft-deletion-tag",
                        RELEASE_TAG,
                        "--repo-root",
                        str(root),
                        "--github-output",
                        str(output_path),
                    ]
                )

            outputs = dict(line.split("=", 1) for line in output_path.read_text(encoding="utf-8").splitlines())

        self.assertEqual(result, 0)
        self.assertEqual(outputs["status"], "validated_draft_deletion")
        self.assertEqual(json.loads(stdout.getvalue())["status"], "validated_draft_deletion")

    def test_cli_writes_validated_github_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_path = Path(temporary_directory) / "github-output.txt"
            stdout = StringIO()

            with redirect_stdout(stdout):
                result = main(
                    [
                        "--cancelled-attempt-tag",
                        RELEASE_TAG,
                        "--repo-root",
                        str(REPO_ROOT),
                        "--github-output",
                        str(output_path),
                    ]
                )

            outputs = dict(line.split("=", 1) for line in output_path.read_text(encoding="utf-8").splitlines())

        self.assertEqual(result, 0)
        self.assertEqual(outputs["release_tag"], RELEASE_TAG)
        self.assertEqual(outputs["build_version"], "168")
        self.assertEqual(outputs["status"], "validated_cancelled_attempt")
        self.assertEqual(json.loads(stdout.getvalue())["status"], "validated_cancelled_attempt")


if __name__ == "__main__":
    unittest.main()
