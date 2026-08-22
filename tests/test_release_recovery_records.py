import json
import shutil
import tempfile
import unittest

from pathlib import Path

from scripts.release_milestone_context import (
    ReleaseMilestoneContextError,
    validate_failed_attempt_record,
    validate_release_recovery_records,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


class ReleaseRecoveryRecordTests(unittest.TestCase):
    @staticmethod
    def copy_recovery_fixtures(root: Path) -> None:
        shutil.copytree(REPO_ROOT / "docs" / "release-attempts", root / "docs" / "release-attempts")
        shutil.copytree(REPO_ROOT / "docs" / "qualification", root / "docs" / "qualification")
        config_path = root / ".github" / "github.json"
        config_path.parent.mkdir(parents=True)
        shutil.copy2(REPO_ROOT / ".github" / "github.json", config_path)

    def test_repository_recovery_records_validate_directly_from_disk(self) -> None:
        records = validate_release_recovery_records(REPO_ROOT)

        self.assertEqual(records["v0.3.2-beta.3"]["attempt"]["build_version"], "165")
        self.assertEqual(records["v0.3.2-beta.4"]["attempt"]["build_version"], "166")
        self.assertEqual(records["v0.3.2-beta.6"]["attempt"]["build_version"], "168")
        self.assertEqual(
            records["v0.3.2-beta.6"]["disposition"]["status"],
            "deleted_abandoned_unpublished_draft",
        )

    def test_failed_attempt_records_validate_directly_from_disk(self) -> None:
        for release_tag, build_version in (("v0.3.2-beta.3", "165"), ("v0.3.2-beta.4", "166")):
            with self.subTest(release_tag=release_tag):
                record = validate_failed_attempt_record(REPO_ROOT, release_tag)

                self.assertEqual(record["build_version"], build_version)
                self.assertTrue(record["draft_deleted"])
                self.assertTrue(record["must_not_rebuild"])

    def test_rejects_noncanonical_attempt_serialization(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.copy_recovery_fixtures(root)
            record_path = root / "docs" / "release-attempts" / "v0.3.2-beta.3" / "failed-attempt-v1.json"
            record = json.loads(record_path.read_text(encoding="utf-8"))
            record_path.write_text(json.dumps(record) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(ReleaseMilestoneContextError, "canonical JSON serialization"):
                validate_failed_attempt_record(root, "v0.3.2-beta.3")

    def test_rejects_missing_historical_burns_for_each_failed_attempt(self) -> None:
        cases = (
            ("v0.3.2-beta.3", 165, "v0.3.2-beta.4-signed-qualification-v1.json"),
            ("v0.3.2-beta.4", 166, "v0.3.2-beta.5-signed-qualification-v1.json"),
        )
        for release_tag, build, qualification_name in cases:
            with self.subTest(release_tag=release_tag), tempfile.TemporaryDirectory() as temporary_directory:
                root = Path(temporary_directory)
                self.copy_recovery_fixtures(root)
                qualification_path = root / "docs" / "qualification" / qualification_name
                qualification = json.loads(qualification_path.read_text(encoding="utf-8"))
                qualification["immutable_history"]["burned_builds"].remove(build)
                qualification_path.write_text(
                    json.dumps(qualification, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(
                    ReleaseMilestoneContextError,
                    rf"permanently burn build {build} from {release_tag}",
                ):
                    validate_release_recovery_records(root)

    def test_rejects_cancelled_attempt_without_deletion_disposition(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.copy_recovery_fixtures(root)
            disposition_path = root / "docs" / "release-attempts" / "v0.3.2-beta.6" / "draft-deletion-v1.json"
            disposition_path.unlink()

            with self.assertRaisesRegex(ReleaseMilestoneContextError, "requires an authorized draft-deletion"):
                validate_release_recovery_records(root)


if __name__ == "__main__":
    unittest.main()
