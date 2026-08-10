import hashlib
import io
import json
import subprocess
import tempfile
import unittest
import zipfile

from pathlib import Path

from scripts.release_qualification_artifact import (
    MAX_ARCHIVE_BYTES,
    QualificationArtifactSafetyError,
    download_and_plan_reconciliation,
    plan_reconciliation,
)
from scripts.release_qualification_resume import ResumeIdentity
from scripts.tier3_receipt import receipt_sha256


REPO_ROOT = Path(__file__).resolve().parents[1]
RELEASE_TAG = "v0.3.1"
RUN_ID = 31330296987
ARTIFACT_ID = 9001


class FakeArtifactAPI:
    def __init__(self, metadata: dict[str, object], archive: bytes) -> None:
        self.metadata = metadata
        self.archive = archive
        self.gets: list[tuple[str, bool]] = []
        self.byte_gets: list[tuple[str, bool, int, float]] = []

    def get_json(self, endpoint: str, *, active_auth: bool = False) -> object:
        self.gets.append((endpoint, active_auth))
        return self.metadata

    def get_bytes(
        self,
        endpoint: str,
        *,
        active_auth: bool = False,
        max_bytes: int,
        timeout_seconds: float,
    ) -> bytes:
        self.byte_gets.append((endpoint, active_auth, max_bytes, timeout_seconds))
        return self.archive


class ReleaseQualificationArtifactTests(unittest.TestCase):
    def fixture(
        self,
        root: Path,
    ) -> tuple[ResumeIdentity, dict[str, object], dict[str, object], dict[str, object], dict[str, bytes], bytes]:
        policy_path = root / "docs/qualification/release-qualification-policy-v1.json"
        index_path = root / "docs/qualification/release-evidence-v1.json"
        release_receipt_path = root / f"docs/release-evidence/{RELEASE_TAG}/release-receipt.json"
        signed_ui_path = root / f"docs/release-evidence/{RELEASE_TAG}/signed-artifact-ui-receipt.json"
        policy_path.parent.mkdir(parents=True)
        release_receipt_path.parent.mkdir(parents=True)
        policy_path.write_bytes((REPO_ROOT / "docs/qualification/release-qualification-policy-v1.json").read_bytes())
        release_receipt_content = (REPO_ROOT / f"docs/release-evidence/{RELEASE_TAG}/release-receipt.json").read_bytes()
        release_receipt_path.write_bytes(release_receipt_content)
        signed_ui_content = (
            REPO_ROOT / f"docs/qualification/{RELEASE_TAG}-profile-save-action-accessibility-v1.json"
        ).read_bytes()
        signed_ui_path.write_bytes(signed_ui_content)
        index_path.write_text('{"receipts":[],"schema_version":1}\n', encoding="utf-8")

        evidence_contents = {
            "accessibility-tree": b'{"kind":"accessibility-tree"}\n',
            "cleanup": b'{"kind":"cleanup"}\n',
            "install-log": b'{"kind":"install-log"}\n',
            "package-smoke": b'{"kind":"package-smoke"}\n',
            "profile-snapshot": b'{"kind":"profile-snapshot"}\n',
            "screenshot-dark": b"\x89PNG\r\n\x1a\ndark",
            "screenshot-light": b"\x89PNG\r\n\x1a\nlight",
            "sparkle-update": b'{"kind":"sparkle-update"}\n',
            "ui-result": b'{"kind":"ui-result"}\n',
        }
        receipt_documents: dict[str, dict[str, object]] = {}
        receipt_contents: dict[str, bytes] = {}
        for case_id in ("clean-machine-signed-update", "installed-ui-accessibility"):
            document = json.loads((REPO_ROOT / f"docs/qualification/{RELEASE_TAG}-{case_id}-v1.json").read_bytes())
            for evidence in document["evidence"]:
                evidence["sha256"] = hashlib.sha256(evidence_contents[evidence["kind"]]).hexdigest()
            if case_id == "clean-machine-signed-update":
                document["cleanup"]["evidence_sha256"] = hashlib.sha256(evidence_contents["cleanup"]).hexdigest()
            document["receipt_sha256"] = receipt_sha256(document)
            content = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()
            receipt_documents[case_id] = document
            receipt_contents[case_id] = content

        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "fixture"], cwd=root, check=True)
        runner_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        release_receipt = json.loads(release_receipt_content)
        signed_ui_receipt = json.loads(signed_ui_content)
        identity = ResumeIdentity(
            release_tag=RELEASE_TAG,
            candidate_sha=release_receipt["source_sha"],
            release_id=release_receipt["release"]["id"],
            manifest_sha256="d" * 64,
            runner_sha=runner_sha,
            main_sha=runner_sha,
            evidence_ref=f"automation/release-evidence-{RELEASE_TAG}",
            evidence_sha=runner_sha,
            evidence_base_sha=runner_sha,
            evidence_pr_number=42,
            release_receipt_file_sha256=hashlib.sha256(release_receipt_content).hexdigest(),
            signed_ui_artifact_id=456,
            signed_ui_artifact_sha256="6" * 64,
            policy_sha256="1" * 64,
            policy_checkpoint_sha256="2" * 64,
            route_table_sha256="3" * 64,
            controller_runner_sha256="4" * 64,
        )
        manifest: dict[str, object] = {
            "paths": {"policy": "docs/qualification/release-qualification-policy-v1.json"},
            "prior": {"release_tag": "v0.3.0"},
            "release": {"sparkle_route": "stable"},
            "signed_ui_artifact": {
                "artifact_id": identity.signed_ui_artifact_id,
                "artifact_sha256": identity.signed_ui_artifact_sha256,
                "receipt_file_sha256": hashlib.sha256(signed_ui_content).hexdigest(),
                "receipt_path": f"docs/release-evidence/{RELEASE_TAG}/signed-artifact-ui-receipt.json",
                "receipt_sha256": signed_ui_receipt["receipt_sha256"],
            },
        }
        run: dict[str, object] = {
            "id": RUN_ID,
            "run_attempt": 1,
            "updated_at": "2026-08-09T19:05:28Z",
        }
        qualification_run = {
            "candidate_tag": RELEASE_TAG,
            "evidence_ref": identity.evidence_ref,
            "evidence_sha": identity.evidence_sha,
            "manifest_sha256": identity.manifest_sha256,
            "prior_tag": "v0.3.0",
            "route": "stable",
            "signed_ui_artifact": {
                "digest": f"sha256:{identity.signed_ui_artifact_sha256}",
                "id": identity.signed_ui_artifact_id,
                "receipt_sha256": signed_ui_receipt["receipt_sha256"],
            },
            "tier3_receipts": [
                {
                    "case_id": case_id,
                    "receipt_sha256": receipt_documents[case_id]["receipt_sha256"],
                }
                for case_id in ("clean-machine-signed-update", "installed-ui-accessibility")
            ],
        }
        entries = {
            "clean-machine-signed-update.json": receipt_contents["clean-machine-signed-update"],
            "installed-ui-accessibility.json": receipt_contents["installed-ui-accessibility"],
            "qualification-run.json": (json.dumps(qualification_run, indent=2) + "\n").encode(),
            "signed-artifact-ui-receipt.json": signed_ui_content,
        }
        evidence_filenames = {
            "accessibility-tree": "accessibility-tree.json",
            "cleanup": "cleanup.json",
            "install-log": "install-log.json",
            "package-smoke": "package-smoke.json",
            "profile-snapshot": "profile-snapshot.json",
            "screenshot-dark": "screenshot-dark.png",
            "screenshot-light": "screenshot-light.png",
            "sparkle-update": "sparkle-update.json",
            "ui-result": "ui-result.json",
        }
        entries.update(
            {
                f"clean-machine-signed-update-evidence/{filename}": evidence_contents[kind]
                for kind, filename in evidence_filenames.items()
            }
        )
        archive = self.archive(entries)
        artifact: dict[str, object] = {
            "digest": "sha256:" + hashlib.sha256(archive).hexdigest(),
            "id": ARTIFACT_ID,
            "name": f"milestone-qualification-{RELEASE_TAG}-1",
            "run_id": RUN_ID,
            "state": "available",
        }
        return identity, manifest, run, artifact, entries, archive

    @staticmethod
    def archive(entries: dict[str, bytes]) -> bytes:
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, content in sorted(entries.items()):
                archive.writestr(name, content)
        return output.getvalue()

    def test_valid_archive_produces_deterministic_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            identity, manifest, run, artifact, _entries, archive = self.fixture(root)

            first = plan_reconciliation(
                repo_root=root,
                identity=identity,
                run=run,
                artifact=artifact,
                manifest=manifest,
                archive_bytes=archive,
            )
            second = plan_reconciliation(
                repo_root=root,
                identity=identity,
                run=run,
                artifact=artifact,
                manifest=manifest,
                archive_bytes=archive,
            )

        self.assertEqual(first, second)
        self.assertTrue(first["requires_changes"])
        self.assertEqual(len(first["plan_sha256"]), 64)
        self.assertEqual(
            [operation["state"] for operation in first["operations"]],
            ["append", "append", "identical", "create", "create"],
        )
        self.assertNotIn("/Users/", json.dumps(first))

    def test_existing_identical_evidence_is_current(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            identity, manifest, run, artifact, entries, archive = self.fixture(root)
            plan = plan_reconciliation(
                repo_root=root,
                identity=identity,
                run=run,
                artifact=artifact,
                manifest=manifest,
                archive_bytes=archive,
            )
            index_path = root / "docs/qualification/release-evidence-v1.json"
            index = json.loads(index_path.read_bytes())
            for operation in plan["operations"]:
                if operation["kind"] == "write_file":
                    path = root / operation["path"]
                    path.parent.mkdir(parents=True, exist_ok=True)
                    case_id = next(
                        case_id
                        for case_id in ("clean-machine-signed-update", "installed-ui-accessibility")
                        if path.name == f"{RELEASE_TAG}-{case_id}-v1.json"
                    )
                    path.write_bytes(entries[f"{case_id}.json"])
                if operation["kind"] == "append_evidence_record":
                    index["receipts"].append(operation["record"])
            index_path.write_text(json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "reconciled"], cwd=root, check=True)

            current = plan_reconciliation(
                repo_root=root,
                identity=identity,
                run=run,
                artifact=artifact,
                manifest=manifest,
                archive_bytes=archive,
            )

        self.assertFalse(current["requires_changes"])
        self.assertEqual(
            [operation["state"] for operation in current["operations"]],
            ["present", "present", "identical", "identical", "identical"],
        )

    def test_unsafe_archive_path_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            identity, manifest, run, artifact, entries, _archive = self.fixture(root)
            entries["../escape.json"] = b"{}\n"

            with self.assertRaisesRegex(QualificationArtifactSafetyError, "unsafe ZIP path"):
                plan_reconciliation(
                    repo_root=root,
                    identity=identity,
                    run=run,
                    artifact=artifact,
                    manifest=manifest,
                    archive_bytes=self.archive(entries),
                )

    def test_qualification_run_identity_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            identity, manifest, run, artifact, entries, _archive = self.fixture(root)
            qualification_run = json.loads(entries["qualification-run.json"])
            qualification_run["manifest_sha256"] = "0" * 64
            entries["qualification-run.json"] = (json.dumps(qualification_run, indent=2) + "\n").encode()

            with self.assertRaisesRegex(QualificationArtifactSafetyError, "manifest digest conflicts"):
                plan_reconciliation(
                    repo_root=root,
                    identity=identity,
                    run=run,
                    artifact=artifact,
                    manifest=manifest,
                    archive_bytes=self.archive(entries),
                )

    def test_evidence_digest_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            identity, manifest, run, artifact, entries, _archive = self.fixture(root)
            entries["clean-machine-signed-update-evidence/cleanup.json"] += b"changed"

            with self.assertRaisesRegex(QualificationArtifactSafetyError, "evidence digest conflicts"):
                plan_reconciliation(
                    repo_root=root,
                    identity=identity,
                    run=run,
                    artifact=artifact,
                    manifest=manifest,
                    archive_bytes=self.archive(entries),
                )

    def test_missing_or_unexpected_archive_file_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            identity, manifest, run, artifact, entries, _archive = self.fixture(root)
            scenarios = {
                "missing": {name: content for name, content in entries.items() if name != "qualification-run.json"},
                "unexpected": {**entries, "unexpected.json": b"{}\n"},
            }

            for scenario, archive_entries in scenarios.items():
                with self.subTest(scenario=scenario):
                    with self.assertRaises(QualificationArtifactSafetyError):
                        plan_reconciliation(
                            repo_root=root,
                            identity=identity,
                            run=run,
                            artifact=artifact,
                            manifest=manifest,
                            archive_bytes=self.archive(archive_entries),
                        )

    def test_unexpected_empty_directory_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            identity, manifest, run, artifact, entries, _archive = self.fixture(root)
            output = io.BytesIO()
            with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
                for name, content in sorted(entries.items()):
                    archive.writestr(name, content)
                archive.writestr("unexpected/", b"")

            with self.assertRaisesRegex(QualificationArtifactSafetyError, "unexpected directory"):
                plan_reconciliation(
                    repo_root=root,
                    identity=identity,
                    run=run,
                    artifact=artifact,
                    manifest=manifest,
                    archive_bytes=output.getvalue(),
                )

    def test_noncanonical_receipt_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            identity, manifest, run, artifact, entries, _archive = self.fixture(root)
            receipt = json.loads(entries["clean-machine-signed-update.json"])
            entries["clean-machine-signed-update.json"] = json.dumps(receipt).encode()

            with self.assertRaisesRegex(QualificationArtifactSafetyError, "not canonical JSON"):
                plan_reconciliation(
                    repo_root=root,
                    identity=identity,
                    run=run,
                    artifact=artifact,
                    manifest=manifest,
                    archive_bytes=self.archive(entries),
                )

    def test_conflicting_checked_receipt_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            identity, manifest, run, artifact, _entries, archive = self.fixture(root)
            conflict = root / f"docs/qualification/{RELEASE_TAG}-clean-machine-signed-update-v1.json"
            conflict.write_text("{}\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "conflict"], cwd=root, check=True)

            with self.assertRaisesRegex(QualificationArtifactSafetyError, "conflicts"):
                plan_reconciliation(
                    repo_root=root,
                    identity=identity,
                    run=run,
                    artifact=artifact,
                    manifest=manifest,
                    archive_bytes=archive,
                )

    def test_download_revalidates_metadata_and_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            identity, manifest, run, artifact, _entries, archive = self.fixture(root)
            metadata = {
                "digest": artifact["digest"],
                "expired": False,
                "id": ARTIFACT_ID,
                "name": artifact["name"],
                "size_in_bytes": len(archive),
                "workflow_run": {"head_branch": "main", "head_sha": identity.runner_sha, "id": RUN_ID},
            }
            client = FakeArtifactAPI(metadata, archive)

            plan = download_and_plan_reconciliation(
                client=client,
                repo_root=root,
                identity=identity,
                run=run,
                artifact=artifact,
                manifest=manifest,
            )

        self.assertTrue(plan["requires_changes"])
        self.assertEqual(
            client.byte_gets,
            [
                (
                    f"repos/cbusillo/BD_to_AVP/actions/artifacts/{ARTIFACT_ID}/zip",
                    True,
                    MAX_ARCHIVE_BYTES,
                    300.0,
                )
            ],
        )

    def test_expired_metadata_race_fails_before_download(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            identity, manifest, run, artifact, _entries, archive = self.fixture(root)
            metadata = {
                "digest": artifact["digest"],
                "expired": True,
                "id": ARTIFACT_ID,
                "name": artifact["name"],
                "size_in_bytes": len(archive),
                "workflow_run": {"head_branch": "main", "head_sha": identity.runner_sha, "id": RUN_ID},
            }
            client = FakeArtifactAPI(metadata, archive)

            with self.assertRaisesRegex(QualificationArtifactSafetyError, "metadata changed"):
                download_and_plan_reconciliation(
                    client=client,
                    repo_root=root,
                    identity=identity,
                    run=run,
                    artifact=artifact,
                    manifest=manifest,
                )

        self.assertEqual(client.byte_gets, [])

    def test_download_digest_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            identity, manifest, run, artifact, _entries, archive = self.fixture(root)
            metadata = {
                "digest": artifact["digest"],
                "expired": False,
                "id": ARTIFACT_ID,
                "name": artifact["name"],
                "size_in_bytes": len(archive),
                "workflow_run": {"head_branch": "main", "head_sha": identity.runner_sha, "id": RUN_ID},
            }
            client = FakeArtifactAPI(metadata, archive + b"changed")

            with self.assertRaisesRegex(QualificationArtifactSafetyError, "archive digest conflicts"):
                download_and_plan_reconciliation(
                    client=client,
                    repo_root=root,
                    identity=identity,
                    run=run,
                    artifact=artifact,
                    manifest=manifest,
                )


if __name__ == "__main__":
    unittest.main()
