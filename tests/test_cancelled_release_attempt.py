import json
import shutil
import tempfile
import unittest

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from scripts.release_milestone_context import (
    ReleaseMilestoneContextError,
    main,
    validate_cancelled_attempt_record,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
RELEASE_TAG = "v0.3.2-beta.6"


class CancelledReleaseAttemptTests(unittest.TestCase):
    def test_repository_record_binds_cancelled_signed_attempt(self) -> None:
        record = validate_cancelled_attempt_record(REPO_ROOT, RELEASE_TAG)

        self.assertEqual(record["build_version"], "168")
        self.assertEqual(record["draft_release_id"], 374538590)
        self.assertFalse(record["draft_deleted"])
        self.assertTrue(record["must_not_rebuild"])

    def assert_mutation_rejected(self, field: str, value: object, message: str) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = REPO_ROOT / "docs" / "release-attempts" / RELEASE_TAG
            destination = root / "docs" / "release-attempts" / RELEASE_TAG
            shutil.copytree(source, destination)
            config_destination = root / ".github" / "github.json"
            config_destination.parent.mkdir(parents=True)
            shutil.copy2(REPO_ROOT / ".github" / "github.json", config_destination)
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
            source = REPO_ROOT / "docs" / "release-attempts" / RELEASE_TAG
            destination = root / "docs" / "release-attempts" / RELEASE_TAG
            shutil.copytree(source, destination)
            config_destination = root / ".github" / "github.json"
            config_destination.parent.mkdir(parents=True)
            shutil.copy2(REPO_ROOT / ".github" / "github.json", config_destination)
            record_path = destination / "cancelled-attempt-v1.json"
            record = json.loads(record_path.read_text(encoding="utf-8"))
            record["draft_assets"][0]["sha256"] = "0" * 64
            record_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(ReleaseMilestoneContextError, "differs from the checked release receipt"):
                validate_cancelled_attempt_record(root, RELEASE_TAG)

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
