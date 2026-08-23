import json
import shutil
import tempfile
import unittest

from pathlib import Path

from scripts.release_milestone_context import (
    ReleaseMilestoneContextError,
    failed_post_publication_disposition_sha256,
    validate_failed_post_publication_qualification_record,
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

    @staticmethod
    def copy_beta5_disposition(root: Path) -> Path:
        source = REPO_ROOT / "docs" / "release-evidence" / "v0.3.2-beta.5"
        destination = root / "docs" / "release-evidence" / "v0.3.2-beta.5"
        shutil.copytree(source, destination)
        return destination / "failed-post-publication-qualification-v1.json"

    @staticmethod
    def write_disposition(path: Path, record: dict[str, object]) -> None:
        record["disposition_sha256"] = failed_post_publication_disposition_sha256(record)
        path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def test_repository_recovery_records_validate_directly_from_disk(self) -> None:
        records = validate_release_recovery_records(REPO_ROOT)

        self.assertEqual(records["v0.3.2-beta.3"]["attempt"]["build_version"], "165")
        self.assertEqual(records["v0.3.2-beta.4"]["attempt"]["build_version"], "166")
        self.assertEqual(records["v0.3.2-beta.6"]["attempt"]["build_version"], "168")
        for release_tag in ("v0.3.2-beta.3", "v0.3.2-beta.4", "v0.3.2-beta.6"):
            with self.subTest(release_tag=release_tag):
                self.assertTrue(records[release_tag]["attempt"]["must_not_rebuild"])
        self.assertEqual(
            records["v0.3.2-beta.6"]["disposition"]["status"],
            "deleted_abandoned_unpublished_draft",
        )
        beta5 = records["v0.3.2-beta.5"]["qualification_disposition"]
        self.assertEqual(beta5["failed_qualification"]["observed"], "0.0.0")
        self.assertFalse(beta5["preservation"]["qualification_passed"])

    def test_beta5_failed_post_publication_disposition_validates_directly_from_disk(self) -> None:
        record = validate_failed_post_publication_qualification_record(REPO_ROOT, "v0.3.2-beta.5")

        self.assertEqual(record["release"]["build_version"], "167")
        self.assertEqual(record["release_workflow"]["run_id"], 32488665999)
        self.assertEqual(record["failed_qualification"]["run_id"], 32491156253)
        self.assertEqual(record["failed_qualification"]["failed_job_id"], 96798878506)
        self.assertEqual(record["failed_qualification"]["observed"], "0.0.0")
        self.assertEqual(record["remediation"]["pull_request_number"], 623)
        self.assertEqual(
            record["disposition_sha256"],
            "a139a72172c9f40f655e4ccbc3416e400317058f26486881e3e0043963eb87b6",
        )

    def test_rejects_noncanonical_failed_post_publication_serialization(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            record_path = self.copy_beta5_disposition(root)
            record = json.loads(record_path.read_text(encoding="utf-8"))
            record_path.write_text(json.dumps(record) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(ReleaseMilestoneContextError, "canonical JSON serialization"):
                validate_failed_post_publication_qualification_record(root, "v0.3.2-beta.5")

    def test_rejects_failed_post_publication_disposition_tampering(self) -> None:
        cases = (
            (
                "schema",
                lambda record: record.__setitem__("unexpected", True),
                "keys do not match",
            ),
            (
                "release source",
                lambda record: record["release"].__setitem__("source_sha", "0" * 40),
                "release identity",
            ),
            ("receipt digest", lambda record: record["receipt"].__setitem__("file_sha256", "0" * 64), "file digest"),
            ("asset digest", lambda record: record["assets"][0].__setitem__("sha256", "0" * 64), "asset appcast"),
            (
                "qualification source",
                lambda record: record["failed_qualification"].__setitem__("source_sha", "0" * 40),
                "exact published release identity",
            ),
            (
                "observed value",
                lambda record: record["failed_qualification"].__setitem__("observed", "0.3.2b5"),
                "expected and observed",
            ),
            (
                "remediation scope",
                lambda record: record["remediation"].__setitem__("successor_only", False),
                "successor-only",
            ),
            (
                "released artifact",
                lambda record: record["remediation"].__setitem__("released_artifact_unchanged", False),
                "successor-only",
            ),
            (
                "qualification claim",
                lambda record: record["preservation"].__setitem__("qualification_passed", True),
                "preservation requirements",
            ),
            (
                "timestamp order",
                lambda record: record.__setitem__("recorded_at", "2026-08-21T14:16:00Z"),
                "timestamps are out of order",
            ),
            (
                "qualification timestamp order",
                lambda record: record["failed_qualification"].__setitem__("started_at", "2026-08-21T14:10:30Z"),
                "timestamps are out of order",
            ),
        )
        for description, mutate, expected_error in cases:
            with self.subTest(description=description), tempfile.TemporaryDirectory() as temporary_directory:
                root = Path(temporary_directory)
                record_path = self.copy_beta5_disposition(root)
                record = json.loads(record_path.read_text(encoding="utf-8"))
                mutate(record)
                self.write_disposition(record_path, record)

                with self.assertRaisesRegex(ReleaseMilestoneContextError, expected_error):
                    validate_failed_post_publication_qualification_record(root, "v0.3.2-beta.5")

    def test_rejects_failed_post_publication_disposition_self_digest_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            record_path = self.copy_beta5_disposition(root)
            record = json.loads(record_path.read_text(encoding="utf-8"))
            record["disposition_sha256"] = "0" * 64
            record_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(ReleaseMilestoneContextError, "self-digest mismatch"):
                validate_failed_post_publication_qualification_record(root, "v0.3.2-beta.5")

    def test_rejects_failed_post_publication_receipt_file_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.copy_beta5_disposition(root)
            receipt_path = root / "docs" / "release-evidence" / "v0.3.2-beta.5" / "release-receipt.json"
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt_path.write_text(json.dumps(receipt, indent=4, sort_keys=True) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(ReleaseMilestoneContextError, "receipt file digest"):
                validate_failed_post_publication_qualification_record(root, "v0.3.2-beta.5")

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
