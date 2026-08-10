import hashlib
import json
import subprocess
import tempfile
import unittest
import zipfile

from pathlib import Path

from scripts.release_qualification_manifest import (
    MANIFEST_NAME,
    SIGNAL_ARCHIVE_NAME,
    SIGNAL_RECEIPT_NAME,
    ReleaseQualificationManifestError,
    SignedUiArtifactBinding,
    build_manifest,
    build_manifest_for_reconciled_release,
    load_validated_manifest,
    manifest_sha256,
    signed_ui_binding_from_files,
    validate_manifest_evidence_history,
    write_manifest,
)
from scripts.release_receipt import build_receipt, write_receipt
from scripts.signed_artifact_receipt import (
    PROFILE_CASE_ID,
    build_receipt as build_signed_artifact_receipt,
    release_expectation_from_receipt,
    validate_policy_case,
    write_receipt as write_signed_artifact_receipt,
)


SOURCE_SHA = "a" * 40
PRIOR_SHA = "b" * 40
DMG_SHA256 = "c" * 64
CHECKSUM_SHA256 = "d" * 64
APPCAST_SHA256 = "e" * 64
APP_TREE_SHA256 = "f" * 64
PRIOR_DMG_SHA256 = "1" * 64
PRIOR_CHECKSUM_SHA256 = "2" * 64
PRIOR_APPCAST_SHA256 = "3" * 64
REPO_ROOT = Path(__file__).resolve().parents[1]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git_head(root: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def write_signed_ui_archive(receipt_path: Path) -> Path:
    archive_path = receipt_path.parent / SIGNAL_ARCHIVE_NAME
    archive_info = zipfile.ZipInfo(SIGNAL_RECEIPT_NAME, date_time=(2026, 1, 1, 0, 0, 0))
    archive_info.compress_type = zipfile.ZIP_DEFLATED
    archive_info.external_attr = 0o100644 << 16
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(archive_info, receipt_path.read_bytes())
    return archive_path


def release_facts(*, tag: str, source_sha: str, stable: bool) -> dict[str, object]:
    public = tag.removeprefix("v")
    package = public.replace("-rc.", "rc")
    return {
        "release_route": "stable" if stable else "prerelease",
        "source_sha": source_sha,
        "workflow_actor": "shiny-code-bot",
        "workflow_run_id": 12345 if stable else 12346,
        "workflow_run_attempt": 1,
        "package_version": package,
        "public_version": public,
        "build_version": "161" if stable else "160",
        "release_tag": tag,
        "release_name": tag,
        "release_id": 67890 if stable else 67891,
        "release_created_at": "2026-08-06T12:00:00Z",
        "prerelease": not stable,
        "make_latest": stable,
        "signed_app_tree_sha256": APP_TREE_SHA256,
        "artifacts": [
            {
                "kind": "dmg",
                "name": f"3D-Blu-ray-to-Vision-Pro-{public}.dmg",
                "sha256": DMG_SHA256 if stable else PRIOR_DMG_SHA256,
                "size_bytes": 1000,
                "asset_id": 1 if stable else 11,
            },
            {
                "kind": "checksum",
                "name": "SHA256SUMS",
                "sha256": CHECKSUM_SHA256 if stable else PRIOR_CHECKSUM_SHA256,
                "size_bytes": 100,
                "asset_id": 2 if stable else 12,
            },
            {
                "kind": "appcast",
                "name": "appcast.xml",
                "sha256": APPCAST_SHA256 if stable else PRIOR_APPCAST_SHA256,
                "size_bytes": 500,
                "asset_id": 3 if stable else 13,
            },
        ],
    }


class ReleaseQualificationManifestTests(unittest.TestCase):
    @staticmethod
    def build_repository(root: Path) -> tuple[Path, Path, Path, Path]:
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
        policy_path = root / "docs/qualification/release-qualification-policy-v1.json"
        evidence_path = root / "docs/qualification/release-evidence-v1.json"
        qualification_path = root / "docs/qualification/stable-signed-qualification-v1.json"
        route_table_path = root / "docs/qualification/video-quality-route-table-v2.json"
        controller_path = root / ".github/workflows/milestone-qualification.yml"
        for path in (policy_path, evidence_path, qualification_path, route_table_path, controller_path):
            path.parent.mkdir(parents=True, exist_ok=True)
        policy = json.loads(
            (REPO_ROOT / "docs/qualification/release-qualification-policy-v1.json").read_text(encoding="utf-8")
        )
        policy_path.write_text(json.dumps(policy, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        evidence_path.write_text('{"schema_version": 1, "receipts": []}\n', encoding="utf-8")
        route_table_path.write_bytes((REPO_ROOT / "docs/qualification/video-quality-route-table-v2.json").read_bytes())
        controller_path.write_text("name: Milestone Qualification\n", encoding="utf-8")

        candidate_receipt_path = root / "docs/release-evidence/v0.3.0/release-receipt.json"
        prior_receipt_path = root / "docs/release-evidence/v0.3.0-rc.3/release-receipt.json"
        for path in (candidate_receipt_path, prior_receipt_path):
            path.parent.mkdir(parents=True, exist_ok=True)
        candidate_receipt = build_receipt(release_facts(tag="v0.3.0", source_sha=SOURCE_SHA, stable=True))
        prior_receipt = build_receipt(release_facts(tag="v0.3.0-rc.3", source_sha=PRIOR_SHA, stable=False))
        write_receipt(candidate_receipt, candidate_receipt_path)
        write_receipt(prior_receipt, prior_receipt_path)
        candidate_release = candidate_receipt["release"]
        candidate_versions = candidate_receipt["versions"]
        candidate_workflow = candidate_receipt["workflow"]
        candidate_artifacts = {artifact["kind"]: artifact for artifact in candidate_receipt["artifacts"]}
        qualification = json.loads(
            (REPO_ROOT / "docs/qualification/stable-signed-qualification-v1.json").read_text(encoding="utf-8")
        )
        qualification["candidate"].update(
            {
                "appcast_sha256": candidate_artifacts["appcast"]["sha256"],
                "build_version": candidate_versions["build"],
                "dmg_name": candidate_artifacts["dmg"]["name"],
                "dmg_sha256": candidate_artifacts["dmg"]["sha256"],
                "package_version": candidate_versions["package"],
                "public_version": candidate_versions["public"],
                "release_id": candidate_release["id"],
                "release_run_id": candidate_workflow["run_id"],
                "release_tag": candidate_release["tag"],
                "signed_app_tree_sha256": candidate_receipt["signed_app_tree_sha256"],
                "source_git_sha": candidate_receipt["source_sha"],
                "workflow": candidate_workflow["name"],
            }
        )
        qualification_content = json.dumps(qualification, indent=2, sort_keys=True) + "\n"
        qualification_path.write_text(qualification_content, encoding="utf-8")
        qualification_record_path = root / "docs/release-evidence/v0.3.0/qualification-record.json"
        qualification_record_path.write_text(qualification_content, encoding="utf-8")
        publication_path = root / "docs/release-evidence/v0.3.0/publication-record.json"
        publication_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "release_tag": "v0.3.0",
                    "release_id": 67890,
                    "source_sha": SOURCE_SHA,
                    "workflow_run_id": 12345,
                    "workflow_conclusion": "success",
                    "published_at": "2026-08-06T13:00:00Z",
                    "receipt_asset_id": 4,
                    "receipt_file_sha256": sha256(candidate_receipt_path),
                    "live_pages": {
                        "state": "verified",
                        "sha256": APPCAST_SHA256,
                        "url": "https://cbusillo.github.io/BD_to_AVP/appcast.xml",
                    },
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        policy_id = validate_policy_case(policy)
        expectation = release_expectation_from_receipt(
            candidate_receipt,
            policy_id=policy_id,
            case_id=PROFILE_CASE_ID,
            workflow_run_id=12345,
            workflow_run_attempt=1,
            release_receipt_asset_id=4,
            release_receipt_file_sha256=sha256(candidate_receipt_path),
        )
        signed_receipt = build_signed_artifact_receipt(
            expectation=expectation,
            evidence={
                "accessibility-tree": "5" * 64,
                "screenshot-dark": "6" * 64,
                "screenshot-light": "7" * 64,
                "ui-result": "8" * 64,
            },
        )
        signed_receipt_path = root / "docs/release-evidence/v0.3.0" / SIGNAL_RECEIPT_NAME
        write_signed_artifact_receipt(signed_receipt, signed_receipt_path)
        signed_archive_path = write_signed_ui_archive(signed_receipt_path)
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "fixtures"], cwd=root, check=True)
        return candidate_receipt_path, prior_receipt_path, signed_receipt_path, signed_archive_path

    def test_builds_exact_key_self_digest_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            candidate_path, prior_path, signed_receipt_path, signed_archive_path = self.build_repository(root)
            signed_ui = signed_ui_binding_from_files(
                repo_root=root,
                release_receipt_path=candidate_path,
                receipt=json.loads(candidate_path.read_text(encoding="utf-8")),
                release_receipt_asset_id=4,
                release_receipt_file_sha256=sha256(candidate_path),
                signed_ui_artifact_id=999,
                signed_ui_artifact_digest=sha256(signed_archive_path),
                signed_ui_archive_path=signed_archive_path,
                signed_ui_receipt_path=signed_receipt_path,
                signed_ui_receipt_file_sha256=sha256(signed_receipt_path),
            )
            manifest = build_manifest(
                repo_root=root,
                release_receipt_path=candidate_path,
                prior_release_receipt_path=prior_path,
                signed_ui=signed_ui,
                evidence_ref="automation/release-evidence-v0.3.0",
                evidence_base_sha=git_head(root),
                runner_sha=git_head(root),
                sparkle_route="rc",
            )
            manifest_path = root / "docs/release-evidence/v0.3.0" / MANIFEST_NAME
            write_manifest(manifest, manifest_path)

            self.assertEqual(manifest["manifest_sha256"], manifest_sha256(manifest))
            self.assertEqual(load_validated_manifest(manifest_path, repo_root=root), manifest)
            self.assertEqual(manifest["signed_ui_artifact"]["artifact_id"], 999)
            self.assertEqual(manifest["release"]["sparkle_route"], "rc")
            self.assertEqual(manifest["prior"]["release_tag"], "v0.3.0-rc.3")
            self.assertEqual(manifest["runner_sha"], git_head(root))
            self.assertNotEqual(manifest["runner_sha"], manifest["candidate"]["source_sha"])

            (root / "docs/qualification/release-evidence-v1.json").write_text(
                '{"schema_version": 1, "receipts": [{"status": "accepted"}]}\n',
                encoding="utf-8",
            )
            self.assertEqual(load_validated_manifest(manifest_path, repo_root=root), manifest)
            self.assertEqual(
                build_manifest_for_reconciled_release(
                    repo_root=root,
                    release_tag="v0.3.0",
                    prior_tag="v0.3.0-rc.3",
                    signed_ui_artifact_id=999,
                    signed_ui_artifact_digest=sha256(signed_archive_path),
                    signed_ui_archive_source_path=signed_archive_path,
                    signed_ui_receipt_source_path=signed_receipt_path,
                    signed_ui_receipt_file_sha256=sha256(signed_receipt_path),
                    evidence_ref="automation/release-evidence-v0.3.0",
                    evidence_base_sha=git_head(root),
                    runner_sha=git_head(root),
                    sparkle_route="rc",
                    qualification_path=Path("docs/release-evidence/v0.3.0/qualification-record.json"),
                ),
                manifest,
            )

    def test_validation_rejects_private_fields_absolute_paths_and_digest_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            candidate_path, prior_path, signed_receipt_path, signed_archive_path = self.build_repository(root)
            signed_ui = SignedUiArtifactBinding(
                artifact_id=999,
                artifact_digest=sha256(signed_archive_path),
                archive_path="docs/release-evidence/v0.3.0/signed-artifact-ui.zip",
                receipt_path="docs/release-evidence/v0.3.0/signed-artifact-ui-receipt.json",
                receipt_file_sha256=sha256(signed_receipt_path),
                receipt_sha256=json.loads(signed_receipt_path.read_text(encoding="utf-8"))["receipt_sha256"],
            )
            with self.assertRaisesRegex(ReleaseQualificationManifestError, "Sparkle route"):
                build_manifest(
                    repo_root=root,
                    release_receipt_path=candidate_path,
                    prior_release_receipt_path=prior_path,
                    signed_ui=signed_ui,
                    evidence_ref="automation/release-evidence-v0.3.0",
                    evidence_base_sha=git_head(root),
                    runner_sha=git_head(root),
                    sparkle_route="stable",
                )
            manifest = build_manifest(
                repo_root=root,
                release_receipt_path=candidate_path,
                prior_release_receipt_path=prior_path,
                signed_ui=signed_ui,
                evidence_ref="automation/release-evidence-v0.3.0",
                evidence_base_sha=git_head(root),
                runner_sha=git_head(root),
                sparkle_route="rc",
            )
            qualification_path = root / "docs/release-evidence/v0.3.0/qualification-record.json"
            qualification = json.loads(qualification_path.read_text(encoding="utf-8"))
            qualification["matrix"][0]["operator_email"] = "operator@example.com"
            qualification_path.write_text(
                json.dumps(qualification, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ReleaseQualificationManifestError, "public-safe"):
                build_manifest(
                    repo_root=root,
                    release_receipt_path=candidate_path,
                    prior_release_receipt_path=prior_path,
                    signed_ui=signed_ui,
                    evidence_ref="automation/release-evidence-v0.3.0",
                    evidence_base_sha=git_head(root),
                    runner_sha=git_head(root),
                    sparkle_route="rc",
                )
            qualification["matrix"][0].pop("operator_email")
            qualification["matrix"][0]["operator_note"] = "not part of the canonical schema"
            qualification_path.write_text(
                json.dumps(qualification, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ReleaseQualificationManifestError, "keys changed"):
                build_manifest(
                    repo_root=root,
                    release_receipt_path=candidate_path,
                    prior_release_receipt_path=prior_path,
                    signed_ui=signed_ui,
                    evidence_ref="automation/release-evidence-v0.3.0",
                    evidence_base_sha=git_head(root),
                    runner_sha=git_head(root),
                    sparkle_route="rc",
                )
            qualification["matrix"][0].pop("operator_note")
            qualification_path.write_text(
                json.dumps(qualification, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            path = root / "docs/release-evidence/v0.3.0" / MANIFEST_NAME
            write_manifest(manifest, path)

            archive_bytes = signed_archive_path.read_bytes()
            signed_archive_path.write_bytes(b"not-the-actions-artifact")
            with self.assertRaisesRegex(ReleaseQualificationManifestError, "archive digest"):
                load_validated_manifest(path, repo_root=root)
            signed_archive_path.write_bytes(archive_bytes)

            mutated = dict(manifest)
            mutated["private_path"] = "/Users/example/secret"
            mutated["manifest_sha256"] = manifest_sha256(mutated)
            path.write_text(json.dumps(mutated, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ReleaseQualificationManifestError, "public-safe"):
                load_validated_manifest(path, repo_root=root)

            path.unlink()
            write_manifest(manifest, path)
            with self.assertRaisesRegex(ReleaseQualificationManifestError, "workflow input"):
                load_validated_manifest(path, repo_root=root, expected_sha256="0" * 64)

            mutated = json.loads(path.read_text(encoding="utf-8"))
            mutated["signed_ui_artifact"]["receipt_sha256"] = "0" * 64
            mutated["manifest_sha256"] = manifest_sha256(mutated)
            path.write_text(json.dumps(mutated, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ReleaseQualificationManifestError, "signed UI binding"):
                load_validated_manifest(path, repo_root=root)

            mutated = json.loads(json.dumps(manifest))
            mutated["paths"]["policy"] = "docs/qualification/video-quality-route-table-v2.json"
            mutated["manifest_sha256"] = manifest_sha256(mutated)
            path.write_text(json.dumps(mutated, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ReleaseQualificationManifestError, "canonical qualification inputs"):
                load_validated_manifest(path, repo_root=root)

            mutated = json.loads(json.dumps(manifest))
            mutated["paths"]["qualification"] = "docs/qualification/stable-signed-qualification-v1.json"
            mutated["manifest_sha256"] = manifest_sha256(mutated)
            path.write_text(json.dumps(mutated, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ReleaseQualificationManifestError, "canonical qualification inputs"):
                load_validated_manifest(path, repo_root=root)

            mutated = json.loads(json.dumps(manifest))
            mutated["prior"]["source_sha"] = "0" * 40
            mutated["manifest_sha256"] = manifest_sha256(mutated)
            path.write_text(json.dumps(mutated, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ReleaseQualificationManifestError, "prior identity"):
                load_validated_manifest(path, repo_root=root)

            path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            broken = json.loads(path.read_text(encoding="utf-8"))
            broken["manifest_sha256"] = "0" * 64
            path.write_text(json.dumps(broken, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ReleaseQualificationManifestError, "self-digest mismatch"):
                load_validated_manifest(path, repo_root=root)

    def test_accepts_stable_to_stable_sparkle_route(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            candidate_path, _prior_path, signed_receipt_path, signed_archive_path = self.build_repository(root)
            stable_prior_path = root / "docs/release-evidence/v0.2.143/release-receipt.json"
            stable_prior_path.parent.mkdir(parents=True, exist_ok=True)
            write_receipt(
                build_receipt(release_facts(tag="v0.2.143", source_sha=PRIOR_SHA, stable=True)),
                stable_prior_path,
            )
            signed_ui = signed_ui_binding_from_files(
                repo_root=root,
                release_receipt_path=candidate_path,
                receipt=json.loads(candidate_path.read_text(encoding="utf-8")),
                release_receipt_asset_id=4,
                release_receipt_file_sha256=sha256(candidate_path),
                signed_ui_artifact_id=999,
                signed_ui_artifact_digest=sha256(signed_archive_path),
                signed_ui_archive_path=signed_archive_path,
                signed_ui_receipt_path=signed_receipt_path,
                signed_ui_receipt_file_sha256=sha256(signed_receipt_path),
            )

            manifest = build_manifest(
                repo_root=root,
                release_receipt_path=candidate_path,
                prior_release_receipt_path=stable_prior_path,
                signed_ui=signed_ui,
                evidence_ref="automation/release-evidence-v0.3.0",
                evidence_base_sha=git_head(root),
                runner_sha=git_head(root),
                sparkle_route="stable",
            )

            self.assertEqual(manifest["release"]["sparkle_route"], "stable")
            self.assertEqual(manifest["prior"]["release_tag"], "v0.2.143")

    def test_refreshes_mutable_checkpoint_fields_without_changing_release_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            candidate_path, _prior_path, signed_receipt_path, signed_archive_path = self.build_repository(root)
            first_base = git_head(root)
            first = build_manifest_for_reconciled_release(
                repo_root=root,
                release_tag="v0.3.0",
                prior_tag="v0.3.0-rc.3",
                signed_ui_artifact_id=999,
                signed_ui_artifact_digest=sha256(signed_archive_path),
                signed_ui_archive_source_path=signed_archive_path,
                signed_ui_receipt_source_path=signed_receipt_path,
                signed_ui_receipt_file_sha256=sha256(signed_receipt_path),
                evidence_ref="automation/release-evidence-v0.3.0",
                evidence_base_sha=first_base,
                runner_sha=first_base,
                sparkle_route="rc",
                qualification_path=Path("docs/release-evidence/v0.3.0/qualification-record.json"),
            )
            controller = root / ".github/workflows/milestone-qualification.yml"
            controller.write_text("name: Refreshed Milestone Qualification\n", encoding="utf-8")
            subprocess.run(["git", "add", controller], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "refresh reviewed main"], cwd=root, check=True)
            second_base = git_head(root)

            refreshed = build_manifest_for_reconciled_release(
                repo_root=root,
                release_tag="v0.3.0",
                prior_tag="v0.3.0-rc.3",
                signed_ui_artifact_id=999,
                signed_ui_artifact_digest=sha256(signed_archive_path),
                signed_ui_archive_source_path=signed_archive_path,
                signed_ui_receipt_source_path=signed_receipt_path,
                signed_ui_receipt_file_sha256=sha256(signed_receipt_path),
                evidence_ref="automation/release-evidence-v0.3.0",
                evidence_base_sha=second_base,
                runner_sha=second_base,
                sparkle_route="rc",
                qualification_path=Path("docs/release-evidence/v0.3.0/qualification-record.json"),
            )

            self.assertNotEqual(first["manifest_sha256"], refreshed["manifest_sha256"])
            self.assertEqual(refreshed["canonical_evidence"]["base_sha"], second_base)
            self.assertEqual(refreshed["runner_sha"], second_base)
            self.assertEqual(refreshed["candidate"], first["candidate"])
            self.assertEqual(load_validated_manifest(candidate_path.parent / MANIFEST_NAME, repo_root=root), refreshed)

            with self.assertRaisesRegex(ReleaseQualificationManifestError, "immutable release state"):
                build_manifest_for_reconciled_release(
                    repo_root=root,
                    release_tag="v0.3.0",
                    prior_tag="v0.3.0-rc.3",
                    signed_ui_artifact_id=1000,
                    signed_ui_artifact_digest=sha256(signed_archive_path),
                    signed_ui_archive_source_path=signed_archive_path,
                    signed_ui_receipt_source_path=signed_receipt_path,
                    signed_ui_receipt_file_sha256=sha256(signed_receipt_path),
                    evidence_ref="automation/release-evidence-v0.3.0",
                    evidence_base_sha=second_base,
                    runner_sha=second_base,
                    sparkle_route="rc",
                    qualification_path=Path("docs/release-evidence/v0.3.0/qualification-record.json"),
                )

    def test_historical_manifest_survives_later_runner_and_rolling_input_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            candidate_path, prior_path, signed_receipt_path, signed_archive_path = self.build_repository(root)
            runner_sha = git_head(root)
            signed_ui = signed_ui_binding_from_files(
                repo_root=root,
                release_receipt_path=candidate_path,
                receipt=json.loads(candidate_path.read_text(encoding="utf-8")),
                release_receipt_asset_id=4,
                release_receipt_file_sha256=sha256(candidate_path),
                signed_ui_artifact_id=999,
                signed_ui_artifact_digest=sha256(signed_archive_path),
                signed_ui_archive_path=signed_archive_path,
                signed_ui_receipt_path=signed_receipt_path,
                signed_ui_receipt_file_sha256=sha256(signed_receipt_path),
            )
            manifest = build_manifest(
                repo_root=root,
                release_receipt_path=candidate_path,
                prior_release_receipt_path=prior_path,
                signed_ui=signed_ui,
                evidence_ref="automation/release-evidence-v0.3.0",
                evidence_base_sha=runner_sha,
                runner_sha=runner_sha,
                sparkle_route="rc",
            )
            manifest_path = candidate_path.parent / MANIFEST_NAME
            write_manifest(manifest, manifest_path)

            policy_path = root / "docs/qualification/release-qualification-policy-v1.json"
            policy = json.loads(policy_path.read_text(encoding="utf-8"))
            policy["cases"][0]["blocking_phase"] = "milestone"
            policy_path.write_text(json.dumps(policy, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            (root / ".github/workflows/milestone-qualification.yml").write_text(
                "name: Later Milestone Qualification\n",
                encoding="utf-8",
            )
            (root / "docs/qualification/video-quality-route-table-v2.json").write_text(
                '{"routes": [{"id": "later"}], "schema_version": 1}\n',
                encoding="utf-8",
            )
            rolling_qualification = root / "docs/qualification/stable-signed-qualification-v1.json"
            rolling_qualification.write_text(
                '{"candidate": {"release_tag": "v0.3.1"}, "schema_version": 1}\n',
                encoding="utf-8",
            )
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "later main evolution"], cwd=root, check=True)

            self.assertNotEqual(git_head(root), runner_sha)
            self.assertEqual(load_validated_manifest(manifest_path, repo_root=root), manifest)
            validate_manifest_evidence_history(manifest, repo_root=root, base_revision=runner_sha)

    def test_manifest_rejects_snapshot_with_rebound_release_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            candidate_path, prior_path, signed_receipt_path, signed_archive_path = self.build_repository(root)
            signed_ui = signed_ui_binding_from_files(
                repo_root=root,
                release_receipt_path=candidate_path,
                receipt=json.loads(candidate_path.read_text(encoding="utf-8")),
                release_receipt_asset_id=4,
                release_receipt_file_sha256=sha256(candidate_path),
                signed_ui_artifact_id=999,
                signed_ui_artifact_digest=sha256(signed_archive_path),
                signed_ui_archive_path=signed_archive_path,
                signed_ui_receipt_path=signed_receipt_path,
                signed_ui_receipt_file_sha256=sha256(signed_receipt_path),
            )
            manifest = build_manifest(
                repo_root=root,
                release_receipt_path=candidate_path,
                prior_release_receipt_path=prior_path,
                signed_ui=signed_ui,
                evidence_ref="automation/release-evidence-v0.3.0",
                evidence_base_sha=git_head(root),
                runner_sha=git_head(root),
                sparkle_route="rc",
            )
            snapshot_path = root / manifest["paths"]["qualification"]
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            snapshot["candidate"]["source_git_sha"] = "0" * 40
            snapshot_path.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            manifest["input_digests"]["qualification_record"] = sha256(snapshot_path)
            manifest["manifest_sha256"] = manifest_sha256(manifest)
            manifest_path = candidate_path.parent / MANIFEST_NAME
            write_manifest(manifest, manifest_path)

            with self.assertRaisesRegex(ReleaseQualificationManifestError, "exact release receipt identity"):
                load_validated_manifest(manifest_path, repo_root=root)

    def test_validates_append_only_evidence_from_manifest_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            candidate_path, prior_path, signed_receipt_path, signed_archive_path = self.build_repository(root)
            evidence_path = root / "docs/qualification/release-evidence-v1.json"
            accepted = {"receipt_id": "accepted"}
            evidence_path.write_text(
                json.dumps({"schema_version": 1, "receipts": [accepted]}, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "add", evidence_path], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "accepted evidence"], cwd=root, check=True)
            base_sha = git_head(root)
            signed_ui = signed_ui_binding_from_files(
                repo_root=root,
                release_receipt_path=candidate_path,
                receipt=json.loads(candidate_path.read_text(encoding="utf-8")),
                release_receipt_asset_id=4,
                release_receipt_file_sha256=sha256(candidate_path),
                signed_ui_artifact_id=999,
                signed_ui_artifact_digest=sha256(signed_archive_path),
                signed_ui_archive_path=signed_archive_path,
                signed_ui_receipt_path=signed_receipt_path,
                signed_ui_receipt_file_sha256=sha256(signed_receipt_path),
            )
            manifest = build_manifest(
                repo_root=root,
                release_receipt_path=candidate_path,
                prior_release_receipt_path=prior_path,
                signed_ui=signed_ui,
                evidence_ref="automation/release-evidence-v0.3.0",
                evidence_base_sha=base_sha,
                runner_sha=git_head(root),
                sparkle_route="rc",
            )
            evidence_path.write_text(
                json.dumps(
                    {"schema_version": 1, "receipts": [accepted, {"receipt_id": "appended"}]},
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            validate_manifest_evidence_history(manifest, repo_root=root, base_revision=base_sha)

            evidence_path.write_text(
                json.dumps(
                    {"schema_version": 1, "receipts": [{"receipt_id": "rewritten"}]},
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ReleaseQualificationManifestError, "only append receipts"):
                validate_manifest_evidence_history(manifest, repo_root=root, base_revision=base_sha)


if __name__ == "__main__":
    unittest.main()
