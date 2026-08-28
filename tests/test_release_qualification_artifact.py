import hashlib
import io
import json
import subprocess
import tempfile
import unittest
import zipfile

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from scripts.qualify_release_scope import _validate_live_publication_evidence
from scripts.release_qualification_artifact import (
    MAX_ARCHIVE_BYTES,
    QualificationArtifactSafetyError,
    download_and_plan_reconciliation,
    plan_reconciliation,
    plan_reconciliation_bundle,
)
from scripts.release_qualification_resume import ResumeIdentity
from scripts.signed_artifact_receipt import receipt_sha256 as signed_artifact_receipt_sha256
from scripts.tier3_receipt import receipt_sha256


REPO_ROOT = Path(__file__).resolve().parents[1]
RELEASE_TAG = "v0.3.1"
RUN_ID = 31330296987
ARTIFACT_ID = 9001


class FakeArtifactAPI:
    def __init__(self, metadata: dict[str, Any], archive: bytes) -> None:
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
    ) -> tuple[ResumeIdentity, dict[str, Any], dict[str, Any], dict[str, Any], dict[str, bytes], bytes]:
        policy_path = root / "docs/qualification/release-qualification-policy-v1.json"
        index_path = root / "docs/qualification/release-evidence-v1.json"
        release_receipt_path = root / f"docs/release-evidence/{RELEASE_TAG}/release-receipt.json"
        signed_ui_path = root / f"docs/release-evidence/{RELEASE_TAG}/signed-artifact-ui-receipt.json"
        policy_path.parent.mkdir(parents=True)
        release_receipt_path.parent.mkdir(parents=True)
        policy_path.write_bytes((REPO_ROOT / "docs/qualification/release-qualification-policy-v1.json").read_bytes())
        release_receipt_content = (REPO_ROOT / f"docs/release-evidence/{RELEASE_TAG}/release-receipt.json").read_bytes()
        release_receipt_path.write_bytes(release_receipt_content)
        publication_path = root / f"docs/release-evidence/{RELEASE_TAG}/publication-record.json"
        publication_path.write_bytes(
            (REPO_ROOT / f"docs/release-evidence/{RELEASE_TAG}/publication-record.json").read_bytes()
        )
        signed_ui_content = (
            REPO_ROOT / f"docs/qualification/{RELEASE_TAG}-profile-save-action-accessibility-v1.json"
        ).read_bytes()
        signed_ui_path.write_bytes(signed_ui_content)
        index_path.write_text('{"receipts":[],"schema_version":1}\n', encoding="utf-8")

        release_receipt = json.loads(release_receipt_content)
        appcast = next(item for item in release_receipt["artifacts"] if item["kind"] == "appcast")
        evidence_contents = {
            "accessibility-tree": b'{"kind":"accessibility-tree"}\n',
            "cleanup": b'{"kind":"cleanup"}\n',
            "install-log": b'{"kind":"install-log"}\n',
            "package-smoke": b'{"kind":"package-smoke"}\n',
            "profile-snapshot": (
                json.dumps(
                    {
                        "profile_after_semantic_sha256": "a" * 64,
                        "profile_after_sha256": "b" * 64,
                        "profile_before_semantic_sha256": "a" * 64,
                        "profile_before_sha256": "b" * 64,
                        "profile_encoding_options_preserved": True,
                        "profile_identity_preserved": True,
                        "profile_migration_matched": True,
                        "profile_safe_pipeline_defaults_preserved": True,
                        "route_after": "stable",
                        "route_preserved": True,
                        "sentinel_preserved": True,
                        "unsafe_legacy_run_defaults_removed": True,
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            ).encode(),
            "screenshot-dark": b"\x89PNG\r\n\x1a\ndark",
            "screenshot-light": b"\x89PNG\r\n\x1a\nlight",
            "sparkle-update": (
                json.dumps(
                    {
                        "button": "Install and Relaunch",
                        "candidate": {
                            "build_version": release_receipt["versions"]["build"],
                            "package_version": release_receipt["versions"]["package"],
                            "signed_app_tree_sha256": release_receipt["signed_app_tree_sha256"],
                        },
                        "feed_sha256": appcast["sha256"],
                        "outcome": "install-and-relaunch",
                        "route": "stable",
                        "status": "passed",
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            ).encode(),
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
            "release": {"id": release_receipt["release"]["id"], "sparkle_route": "stable"},
            "release_receipt": {
                "asset_id": signed_ui_receipt["release_receipt"]["asset_id"],
                "file_sha256": hashlib.sha256(release_receipt_content).hexdigest(),
                "path": f"docs/release-evidence/{RELEASE_TAG}/release-receipt.json",
                "receipt_sha256": release_receipt["receipt_sha256"],
            },
            "signed_ui_artifact": {
                "artifact_id": identity.signed_ui_artifact_id,
                "artifact_sha256": identity.signed_ui_artifact_sha256,
                "receipt_file_sha256": hashlib.sha256(signed_ui_content).hexdigest(),
                "receipt_path": f"docs/release-evidence/{RELEASE_TAG}/signed-artifact-ui-receipt.json",
                "receipt_sha256": signed_ui_receipt["receipt_sha256"],
            },
            "workflow": {
                "run_attempt": release_receipt["workflow"]["run_attempt"],
                "run_id": release_receipt["workflow"]["run_id"],
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

    def later_run(
        self,
        entries: dict[str, bytes],
        *,
        run_id: int,
    ) -> tuple[dict[str, bytes], dict[str, object], dict[str, object], bytes]:
        updated = dict(entries)
        receipt_digests: dict[str, str] = {}
        for case_id in ("clean-machine-signed-update", "installed-ui-accessibility"):
            receipt = json.loads(updated[f"{case_id}.json"])
            for field in ("started_at", "completed_at"):
                timestamp = datetime.fromisoformat(receipt["timestamps"][field].replace("Z", "+00:00"))
                receipt["timestamps"][field] = (timestamp + timedelta(days=1)).isoformat().replace("+00:00", "Z")
            expires_on = datetime.fromisoformat(receipt["cadence"]["expires_on"])
            receipt["cadence"]["expires_on"] = (expires_on + timedelta(days=1)).date().isoformat()
            receipt["receipt_sha256"] = receipt_sha256(receipt)
            updated[f"{case_id}.json"] = (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode()
            receipt_digests[case_id] = receipt["receipt_sha256"]
        qualification_run = json.loads(updated["qualification-run.json"])
        for summary in qualification_run["tier3_receipts"]:
            summary["receipt_sha256"] = receipt_digests[summary["case_id"]]
        updated["qualification-run.json"] = (json.dumps(qualification_run, indent=2) + "\n").encode()
        archive = self.archive(updated)
        run = {"id": run_id, "run_attempt": 1, "updated_at": "2026-08-10T19:05:28Z"}
        artifact = {
            "digest": "sha256:" + hashlib.sha256(archive).hexdigest(),
            "id": ARTIFACT_ID + 1,
            "name": f"milestone-qualification-{RELEASE_TAG}-1",
            "run_id": run_id,
            "state": "available",
        }
        return updated, run, artifact, archive

    def commit_initial_reconciliation(
        self,
        root: Path,
        bundle,
    ) -> None:
        for file in bundle.files:
            path = root / file.path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(file.content)
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "initial reconciliation"], cwd=root, check=True)

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
            ["append", "append", "append", "append", "identical", "create", "create", "create"],
        )
        records = {
            operation["record"]["case_id"]: operation["record"]
            for operation in first["operations"]
            if operation["kind"] == "append_evidence_record"
        }
        self.assertEqual(
            records["profile-save-action-accessibility"]["reference"],
            f"docs/release-evidence/{RELEASE_TAG}/signed-artifact-ui-receipt.json",
        )
        self.assertEqual(
            records["sparkle-update-route"]["reference"],
            f"docs/qualification/{RELEASE_TAG}-live-qualification-v1.json",
        )
        self.assertNotIn("/Users/", json.dumps(first))

    def test_bundle_binds_exact_materialized_file_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            identity, manifest, run, artifact, entries, archive = self.fixture(root)

            bundle = plan_reconciliation_bundle(
                repo_root=root,
                identity=identity,
                run=run,
                artifact=artifact,
                manifest=manifest,
                archive_bytes=archive,
            )
            policy_content = (root / "docs/qualification/release-qualification-policy-v1.json").read_bytes()
            reference_contents = {
                f"docs/release-evidence/{RELEASE_TAG}/release-receipt.json": (
                    root / f"docs/release-evidence/{RELEASE_TAG}/release-receipt.json"
                ).read_bytes(),
                f"docs/release-evidence/{RELEASE_TAG}/publication-record.json": (
                    root / f"docs/release-evidence/{RELEASE_TAG}/publication-record.json"
                ).read_bytes(),
            }

        files = {file.path: file.content for file in bundle.files}
        self.assertEqual(
            files[f"docs/qualification/{RELEASE_TAG}-clean-machine-signed-update-v1.json"],
            entries["clean-machine-signed-update.json"],
        )
        self.assertEqual(
            files[f"docs/qualification/{RELEASE_TAG}-installed-ui-accessibility-v1.json"],
            entries["installed-ui-accessibility.json"],
        )
        live = json.loads(files[f"docs/qualification/{RELEASE_TAG}-live-qualification-v1.json"])
        self.assertEqual(live["candidate"]["release_id"], manifest["release"]["id"])
        observations = live["cases"][0]["observations"]
        self.assertEqual(observations["qualification_workflow_run_id"], RUN_ID)
        self.assertEqual(observations["qualification_artifact_id"], ARTIFACT_ID)
        self.assertEqual(observations["qualification_manifest_sha256"], identity.manifest_sha256)
        self.assertEqual(observations["qualification_evidence_sha"], identity.evidence_sha)
        self.assertTrue(observations["profile_preserved"])
        policy = json.loads(policy_content)
        sparkle_case = next(item for item in policy["cases"] if item["id"] == "sparkle-update-route")
        binding = _validate_live_publication_evidence(
            files[f"docs/qualification/{RELEASE_TAG}-live-qualification-v1.json"],
            sparkle_case,
            "sparkle-update-route",
            identity.candidate_sha,
            reference_contents.__getitem__,
            datetime.fromisoformat(live["qualified_at"].replace("Z", "+00:00")),
            "passed",
        )
        self.assertEqual(
            binding["release_receipt_reference"],
            f"docs/release-evidence/{RELEASE_TAG}/release-receipt.json",
        )
        self.assertEqual(
            {item["path"] for item in bundle.plan["files"]},
            set(files),
        )
        for summary in bundle.plan["files"]:
            content = files[summary["path"]]
            self.assertEqual(summary["sha256"], hashlib.sha256(content).hexdigest())
            self.assertEqual(summary["size_bytes"], len(content))

    def test_plan_is_stable_when_workflow_updated_at_drifts(self) -> None:
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
            run["updated_at"] = "2026-08-20T12:00:00Z"

            second = plan_reconciliation(
                repo_root=root,
                identity=identity,
                run=run,
                artifact=artifact,
                manifest=manifest,
                archive_bytes=archive,
            )

        self.assertEqual(first, second)

    def test_existing_identical_evidence_is_current(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            identity, manifest, run, artifact, _entries, archive = self.fixture(root)
            bundle = plan_reconciliation_bundle(
                repo_root=root,
                identity=identity,
                run=run,
                artifact=artifact,
                manifest=manifest,
                archive_bytes=archive,
            )
            plan = bundle.plan
            materialized_files = {file.path: file.content for file in bundle.files}
            index_path = root / "docs/qualification/release-evidence-v1.json"
            index = json.loads(index_path.read_bytes())
            for operation in plan["operations"]:
                if operation["kind"] == "write_file":
                    path = root / operation["path"]
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(materialized_files[operation["path"]])
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
            ["present", "present", "present", "present", "identical", "identical", "identical", "identical"],
        )

    def test_later_run_rolls_over_accepted_tier3_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            identity, manifest, run, artifact, entries, archive = self.fixture(root)
            initial = plan_reconciliation_bundle(
                repo_root=root,
                identity=identity,
                run=run,
                artifact=artifact,
                manifest=manifest,
                archive_bytes=archive,
            )
            self.commit_initial_reconciliation(root, initial)
            later_entries, later_run, later_artifact, later_archive = self.later_run(
                entries,
                run_id=RUN_ID + 1,
            )

            rollover = plan_reconciliation_bundle(
                repo_root=root,
                identity=identity,
                run=later_run,
                artifact=later_artifact,
                manifest=manifest,
                archive_bytes=later_archive,
            )

        files = {file.path: file.content for file in rollover.files}
        clean_path = f"docs/qualification/{RELEASE_TAG}-clean-machine-signed-update-run-{RUN_ID + 1}-v1.json"
        ui_path = f"docs/qualification/{RELEASE_TAG}-installed-ui-accessibility-run-{RUN_ID + 1}-v1.json"
        self.assertEqual(files[clean_path], later_entries["clean-machine-signed-update.json"])
        self.assertEqual(files[ui_path], later_entries["installed-ui-accessibility.json"])
        self.assertNotIn(f"docs/qualification/{RELEASE_TAG}-clean-machine-signed-update-v1.json", files)
        live_path = f"docs/qualification/{RELEASE_TAG}-live-qualification-run-{RUN_ID + 1}-v1.json"
        live = json.loads(files[live_path])
        observations = live["cases"][0]["observations"]
        self.assertEqual(observations["qualification_receipt_reference"], clean_path)
        self.assertEqual(
            observations["qualification_receipt_sha256"],
            hashlib.sha256(later_entries["clean-machine-signed-update.json"]).hexdigest(),
        )
        index = json.loads(files["docs/qualification/release-evidence-v1.json"])
        records = {item["receipt_id"]: item for item in index["receipts"]}
        self.assertIn(f"{RELEASE_TAG}:clean-machine-signed-update:{RUN_ID}", records)
        self.assertIn(f"{RELEASE_TAG}:sparkle-update-route:{RUN_ID}", records)
        self.assertEqual(
            records[f"{RELEASE_TAG}:clean-machine-signed-update:{RUN_ID + 1}"]["reference"],
            clean_path,
        )
        self.assertEqual(records[f"{RELEASE_TAG}:sparkle-update-route:{RUN_ID + 1}"]["reference"], live_path)

    def test_later_run_rollover_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            identity, manifest, run, artifact, entries, archive = self.fixture(root)
            initial = plan_reconciliation_bundle(
                repo_root=root,
                identity=identity,
                run=run,
                artifact=artifact,
                manifest=manifest,
                archive_bytes=archive,
            )
            self.commit_initial_reconciliation(root, initial)
            _later_entries, later_run, later_artifact, later_archive = self.later_run(
                entries,
                run_id=RUN_ID + 1,
            )
            rollover = plan_reconciliation_bundle(
                repo_root=root,
                identity=identity,
                run=later_run,
                artifact=later_artifact,
                manifest=manifest,
                archive_bytes=later_archive,
            )
            for file in rollover.files:
                path = root / file.path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(file.content)
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "rollover receipts"], cwd=root, check=True)

            replay = plan_reconciliation(
                repo_root=root,
                identity=identity,
                run=later_run,
                artifact=later_artifact,
                manifest=manifest,
                archive_bytes=later_archive,
            )

        self.assertFalse(replay["requires_changes"])
        self.assertTrue(all(operation["state"] in {"identical", "present"} for operation in replay["operations"]))

    def test_later_run_rollover_rejects_ambiguous_fixed_path_backing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            identity, manifest, run, artifact, entries, archive = self.fixture(root)
            initial = plan_reconciliation_bundle(
                repo_root=root,
                identity=identity,
                run=run,
                artifact=artifact,
                manifest=manifest,
                archive_bytes=archive,
            )
            self.commit_initial_reconciliation(root, initial)
            index_path = root / "docs/qualification/release-evidence-v1.json"
            index = json.loads(index_path.read_bytes())
            fixed_path = f"docs/qualification/{RELEASE_TAG}-clean-machine-signed-update-v1.json"
            backing = next(item for item in index["receipts"] if item["reference"] == fixed_path)
            duplicate = dict(backing)
            duplicate["receipt_id"] = f"{RELEASE_TAG}:clean-machine-signed-update:{RUN_ID + 99}"
            index["receipts"].append(duplicate)
            index_path.write_text(json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "ambiguous fixed receipt"], cwd=root, check=True)
            _later_entries, later_run, later_artifact, later_archive = self.later_run(
                entries,
                run_id=RUN_ID + 1,
            )

            with self.assertRaisesRegex(QualificationArtifactSafetyError, "conflicts"):
                plan_reconciliation(
                    repo_root=root,
                    identity=identity,
                    run=later_run,
                    artifact=later_artifact,
                    manifest=manifest,
                    archive_bytes=later_archive,
                )

    def test_signed_ui_identity_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            identity, manifest, run, artifact, entries, _archive = self.fixture(root)
            receipt = json.loads(entries["signed-artifact-ui-receipt.json"])
            receipt["candidate_sha"] = "0" * 40
            receipt["receipt_sha256"] = signed_artifact_receipt_sha256(receipt)
            content = (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode()
            entries["signed-artifact-ui-receipt.json"] = content
            signed_ui_path = root / f"docs/release-evidence/{RELEASE_TAG}/signed-artifact-ui-receipt.json"
            signed_ui_path.write_bytes(content)
            manifest["signed_ui_artifact"]["receipt_file_sha256"] = hashlib.sha256(content).hexdigest()
            manifest["signed_ui_artifact"]["receipt_sha256"] = receipt["receipt_sha256"]
            qualification_run = json.loads(entries["qualification-run.json"])
            qualification_run["signed_ui_artifact"]["receipt_sha256"] = receipt["receipt_sha256"]
            entries["qualification-run.json"] = (json.dumps(qualification_run, indent=2) + "\n").encode()
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "mutate signed UI receipt"], cwd=root, check=True)

            with self.assertRaisesRegex(QualificationArtifactSafetyError, "signed UI receipt is invalid"):
                plan_reconciliation(
                    repo_root=root,
                    identity=identity,
                    run=run,
                    artifact=artifact,
                    manifest=manifest,
                    archive_bytes=self.archive(entries),
                )

    def test_profile_preservation_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            identity, manifest, run, artifact, entries, _archive = self.fixture(root)
            profile_path = "clean-machine-signed-update-evidence/profile-snapshot.json"
            profile = json.loads(entries[profile_path])
            profile["profile_after_sha256"] = "c" * 64
            entries[profile_path] = (json.dumps(profile, indent=2, sort_keys=True) + "\n").encode()
            clean_receipt = json.loads(entries["clean-machine-signed-update.json"])
            profile_evidence = next(item for item in clean_receipt["evidence"] if item["kind"] == "profile-snapshot")
            profile_evidence["sha256"] = hashlib.sha256(entries[profile_path]).hexdigest()
            clean_receipt["receipt_sha256"] = receipt_sha256(clean_receipt)
            entries["clean-machine-signed-update.json"] = (
                json.dumps(clean_receipt, indent=2, sort_keys=True) + "\n"
            ).encode()
            qualification_run = json.loads(entries["qualification-run.json"])
            clean_summary = next(
                item for item in qualification_run["tier3_receipts"] if item["case_id"] == "clean-machine-signed-update"
            )
            clean_summary["receipt_sha256"] = clean_receipt["receipt_sha256"]
            entries["qualification-run.json"] = (json.dumps(qualification_run, indent=2) + "\n").encode()

            with self.assertRaisesRegex(QualificationArtifactSafetyError, "Profile snapshot"):
                plan_reconciliation(
                    repo_root=root,
                    identity=identity,
                    run=run,
                    artifact=artifact,
                    manifest=manifest,
                    archive_bytes=self.archive(entries),
                )

    def test_live_appcast_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            identity, manifest, run, artifact, _entries, archive = self.fixture(root)
            publication_path = root / f"docs/release-evidence/{RELEASE_TAG}/publication-record.json"
            publication = json.loads(publication_path.read_bytes())
            publication["live_pages"]["sha256"] = "0" * 64
            publication_path.write_text(json.dumps(publication, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "mutate live appcast"], cwd=root, check=True)

            with self.assertRaisesRegex(QualificationArtifactSafetyError, "live appcast"):
                plan_reconciliation(
                    repo_root=root,
                    identity=identity,
                    run=run,
                    artifact=artifact,
                    manifest=manifest,
                    archive_bytes=archive,
                )

    def test_conflicting_profile_index_record_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            identity, manifest, run, artifact, _entries, archive = self.fixture(root)
            index_path = root / "docs/qualification/release-evidence-v1.json"
            index = json.loads(index_path.read_bytes())
            index["receipts"].append(
                {
                    "accepted_at": "2026-08-09T19:05:28Z",
                    "case_id": "profile-save-action-accessibility",
                    "receipt_id": f"{RELEASE_TAG}:profile-save-action-accessibility:{manifest['workflow']['run_id']}",
                    "reference": f"docs/release-evidence/{RELEASE_TAG}/signed-artifact-ui-receipt.json",
                    "sha256": "0" * 64,
                    "source": "signed_artifact_receipt",
                    "source_sha": identity.candidate_sha,
                    "status": "accepted",
                }
            )
            index_path.write_text(json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "conflicting profile record"], cwd=root, check=True)

            with self.assertRaisesRegex(QualificationArtifactSafetyError, "immutable record"):
                plan_reconciliation(
                    repo_root=root,
                    identity=identity,
                    run=run,
                    artifact=artifact,
                    manifest=manifest,
                    archive_bytes=archive,
                )

    def test_conflicting_sparkle_index_record_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            identity, manifest, run, artifact, _entries, archive = self.fixture(root)
            index_path = root / "docs/qualification/release-evidence-v1.json"
            index = json.loads(index_path.read_bytes())
            index["receipts"].append(
                {
                    "accepted_at": "2026-08-09T19:05:28Z",
                    "case_id": "sparkle-update-route",
                    "receipt_id": f"{RELEASE_TAG}:sparkle-update-route:{RUN_ID}",
                    "reference": f"docs/qualification/{RELEASE_TAG}-live-qualification-v1.json",
                    "sha256": "0" * 64,
                    "source": "signed_artifact_receipt",
                    "source_sha": identity.candidate_sha,
                    "status": "accepted",
                }
            )
            index_path.write_text(json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "conflicting sparkle record"], cwd=root, check=True)

            with self.assertRaisesRegex(QualificationArtifactSafetyError, "immutable record"):
                plan_reconciliation(
                    repo_root=root,
                    identity=identity,
                    run=run,
                    artifact=artifact,
                    manifest=manifest,
                    archive_bytes=archive,
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
