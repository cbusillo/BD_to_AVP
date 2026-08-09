import json
import tempfile
import unittest

from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from scripts.release_evidence import ReleaseEvidenceError, effective_successful_workflow_run_id, reconcile
from scripts.release_receipt import (
    ArtifactReceiptExpectation,
    DEFAULT_POLICY_PATH,
    ReleaseReceiptError,
    build_receipt,
    file_sha256,
    load_validated_artifact_receipt,
    receipt_sha256,
    validate_receipt,
    write_receipt,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_SHA = "a" * 40
DMG_SHA256 = "b" * 64
CHECKSUM_SHA256 = "c" * 64
APPCAST_SHA256 = "d" * 64
APP_TREE_SHA256 = "e" * 64


def receipt_facts() -> dict[str, object]:
    return {
        "release_route": "prerelease",
        "source_sha": SOURCE_SHA,
        "workflow_actor": "shiny-code-bot",
        "workflow_run_id": 12345,
        "workflow_run_attempt": 2,
        "package_version": "0.3.0rc3",
        "public_version": "0.3.0-rc.3",
        "build_version": "160",
        "release_tag": "v0.3.0-rc.3",
        "release_name": "v0.3.0-rc.3",
        "release_id": 67890,
        "release_created_at": "2026-08-05T12:00:00Z",
        "prerelease": True,
        "make_latest": False,
        "signed_app_tree_sha256": APP_TREE_SHA256,
        "artifacts": [
            {
                "kind": "dmg",
                "name": "3D-Blu-ray-to-Vision-Pro-0.3.0-rc.3.dmg",
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


def workflow_run() -> dict[str, object]:
    return {
        "actor": {"login": "shiny-code-bot"},
        "conclusion": "success",
        "event": "workflow_dispatch",
        "head_branch": "main",
        "head_sha": SOURCE_SHA,
        "id": 12345,
        "name": "Prerelease",
        "path": ".github/workflows/prerelease.yml",
        "repository": {"full_name": "cbusillo/BD_to_AVP"},
        "run_attempt": 2,
        "status": "completed",
        "triggering_actor": {"login": "shiny-code-bot"},
    }


def artifact_expectation(path: Path) -> ArtifactReceiptExpectation:
    return ArtifactReceiptExpectation(
        candidate_sha=SOURCE_SHA,
        release_route="prerelease",
        workflow_run_id=12345,
        workflow_run_attempt=2,
        release_id=67890,
        package_version="0.3.0rc3",
        public_version="0.3.0-rc.3",
        build_version="160",
        release_tag="v0.3.0-rc.3",
        dmg_name="3D-Blu-ray-to-Vision-Pro-0.3.0-rc.3.dmg",
        receipt_asset_id=4,
        receipt_file_sha256=file_sha256(path),
        receipt_sha256=json.loads(path.read_text(encoding="utf-8"))["receipt_sha256"],
        signed_app_tree_sha256=APP_TREE_SHA256,
        dmg_asset_id=1,
        dmg_size=1000,
        dmg_sha256=DMG_SHA256,
        checksum_asset_id=2,
        checksum_sha256=CHECKSUM_SHA256,
        appcast_asset_id=3,
        appcast_sha256=APPCAST_SHA256,
    )


def qualification_for_receipt(receipt: dict[str, object]) -> dict[str, object]:
    release = receipt["release"]
    versions = receipt["versions"]
    workflow = receipt["workflow"]
    artifacts = {artifact["kind"]: artifact for artifact in receipt["artifacts"]}
    return {
        "schema_version": 1,
        "candidate": {
            "appcast_sha256": artifacts["appcast"]["sha256"],
            "build_version": versions["build"],
            "dmg_name": artifacts["dmg"]["name"],
            "dmg_sha256": artifacts["dmg"]["sha256"],
            "package_version": versions["package"],
            "public_version": versions["public"],
            "release_id": release["id"],
            "release_run_id": workflow["run_id"],
            "release_tag": release["tag"],
            "signed_app_tree_sha256": receipt["signed_app_tree_sha256"],
            "source_git_sha": receipt["source_sha"],
            "workflow": workflow["name"],
        },
    }


def minimal_reconcile_receipt() -> dict[str, object]:
    return {
        "appcast": {"live_pages_url": "https://example.com/appcast.xml"},
        "artifacts": [
            {"kind": "appcast", "name": "appcast.xml", "sha256": "a" * 64},
            {"kind": "dmg", "name": "release.dmg", "sha256": "d" * 64},
        ],
        "release": {"id": 20, "tag": "v0.3.0"},
        "signed_app_tree_sha256": "e" * 64,
        "source_sha": SOURCE_SHA,
        "tier1_case_references": [],
        "versions": {"build": "1", "package": "0.3.0", "public": "0.3.0"},
        "workflow": {"name": "Stable", "run_id": 30},
    }


class ReleaseReceiptTests(unittest.TestCase):
    def test_build_is_deterministic_and_public_safe(self) -> None:
        first = build_receipt(receipt_facts())
        second = build_receipt(receipt_facts())

        self.assertEqual(first, second)
        validate_receipt(first)
        serialized = json.dumps(first, sort_keys=True)
        for forbidden in ("token", "certificate", "keychain", "private_path", "approval_comment"):
            self.assertNotIn(forbidden, serialized.lower())
        self.assertEqual(
            first["tier1_case_references"],
            ["release-workflow-identity", "signed-packaged-route-parity"],
        )

    def test_validation_rejects_unknown_or_mutated_fields(self) -> None:
        receipt = build_receipt(receipt_facts())
        receipt["private_path"] = "/tmp/private"
        with self.assertRaisesRegex(ReleaseReceiptError, "keys changed"):
            validate_receipt(receipt)

        receipt = build_receipt(receipt_facts())
        receipt["release"]["name"] = "changed"
        with self.assertRaisesRegex(ReleaseReceiptError, "tag or name"):
            validate_receipt(receipt)

    def test_build_rejects_incoherent_release_identity(self) -> None:
        mutations = (
            ("public_version", "0.3.0-beta.99", "public version"),
            ("release_tag", "v0.3.0-beta.99", "tag or name"),
            ("release_name", "wrong-name", "tag or name"),
            ("release_created_at", "not-a-timestamp", "created_at"),
        )
        for field, value, message in mutations:
            with self.subTest(field=field):
                facts = receipt_facts()
                facts[field] = value
                with self.assertRaisesRegex(ReleaseReceiptError, message):
                    build_receipt(facts)

        facts = receipt_facts()
        facts["artifacts"][0]["name"] = "wrong.dmg"
        with self.assertRaisesRegex(ReleaseReceiptError, "dmg asset name"):
            build_receipt(facts)

    def test_build_requires_explicit_verification_mapping_for_every_tier1_case(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            policy = json.loads(DEFAULT_POLICY_PATH.read_text(encoding="utf-8"))
            policy["cases"].append(
                {
                    "id": "new-tier1-case",
                    "tier": 1,
                    "blocking_phase": "publication",
                    "invalidates_on": {"paths": [], "contracts": ["release-engine"]},
                    "allowed_evidence_sources": ["release_run_receipt"],
                    "carry_forward": {
                        "allowed": False,
                        "requires_prior_accepted_receipt": False,
                        "requires_clean_invalidation": False,
                    },
                }
            )
            policy_path = Path(temporary_directory) / "policy.json"
            policy_path.write_text(json.dumps(policy), encoding="utf-8")

            with self.assertRaisesRegex(ReleaseReceiptError, "explicit receipt verification mappings"):
                build_receipt(receipt_facts(), policy_path)

    def test_artifact_validation_accepts_only_the_exact_same_run_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            receipt_path = Path(temporary_directory) / "release-receipt.json"
            receipt = build_receipt(receipt_facts())
            write_receipt(receipt, receipt_path)

            validated = load_validated_artifact_receipt(receipt_path, artifact_expectation(receipt_path))

        self.assertEqual(validated, receipt)

    def test_artifact_validation_fails_closed_for_mismatched_identity_or_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            receipt_path = Path(temporary_directory) / "release-receipt.json"
            write_receipt(build_receipt(receipt_facts()), receipt_path)
            exact = artifact_expectation(receipt_path)
            mismatches = (
                ("candidate_sha", replace(exact, candidate_sha="f" * 40)),
                ("release_route", replace(exact, release_route="stable")),
                ("workflow_run_id", replace(exact, workflow_run_id=54321)),
                ("workflow_run_attempt", replace(exact, workflow_run_attempt=3)),
                ("release_id", replace(exact, release_id=9876)),
                ("package_version", replace(exact, package_version="0.3.0rc2")),
                ("public_version", replace(exact, public_version="0.3.0-rc.2")),
                ("build_version", replace(exact, build_version="159")),
                ("release_tag", replace(exact, release_tag="v0.3.0-rc.2")),
                ("dmg_name", replace(exact, dmg_name="wrong.dmg")),
                ("receipt_file_sha256", replace(exact, receipt_file_sha256="0" * 64)),
                ("receipt_sha256", replace(exact, receipt_sha256="1" * 64)),
                ("signed_app_tree_sha256", replace(exact, signed_app_tree_sha256="1" * 64)),
                ("dmg_asset_id", replace(exact, dmg_asset_id=11)),
                ("dmg_size", replace(exact, dmg_size=1001)),
                ("dmg_sha256", replace(exact, dmg_sha256="2" * 64)),
                ("checksum_asset_id", replace(exact, checksum_asset_id=12)),
                ("checksum_sha256", replace(exact, checksum_sha256="3" * 64)),
                ("appcast_asset_id", replace(exact, appcast_asset_id=13)),
                ("appcast_sha256", replace(exact, appcast_sha256="4" * 64)),
            )

            for field, mismatched_expectation in mismatches:
                with self.subTest(field=field), self.assertRaises(ReleaseReceiptError):
                    load_validated_artifact_receipt(receipt_path, mismatched_expectation)

    def test_artifact_validation_rejects_missing_or_schema_invalid_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            valid_path = root / "release-receipt.json"
            write_receipt(build_receipt(receipt_facts()), valid_path)
            expectation = artifact_expectation(valid_path)

            with self.assertRaisesRegex(ReleaseReceiptError, "Unable to read"):
                load_validated_artifact_receipt(root / "missing.json", expectation)

            invalid_path = root / "invalid.json"
            invalid_path.write_text('{"schema_version": 1}\n', encoding="utf-8")
            invalid_expectation = replace(expectation, receipt_file_sha256=file_sha256(invalid_path))
            with self.assertRaisesRegex(ReleaseReceiptError, "keys changed"):
                load_validated_artifact_receipt(invalid_path, invalid_expectation)

    def test_reconcile_writes_idempotent_checked_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            qualification_directory = root / "docs" / "qualification"
            qualification_directory.mkdir(parents=True)
            qualification = {
                "schema_version": 1,
                "candidate": {
                    "build_version": "160",
                    "dmg_name": "3D-Blu-ray-to-Vision-Pro-0.3.0-rc.3.dmg",
                    "package_version": "0.3.0rc3",
                    "public_version": "0.3.0-rc.3",
                    "release_tag": "v0.3.0-rc.3",
                    "source_git_sha": None,
                    "dmg_sha256": None,
                    "signed_app_tree_sha256": None,
                    "release_run_id": None,
                    "release_id": None,
                    "appcast_sha256": None,
                    "workflow": "Prerelease",
                },
            }
            qualification_path = qualification_directory / "rc3-signed-qualification-v1.json"
            qualification_path.write_text(json.dumps(qualification), encoding="utf-8")
            cut_packet = root / "docs" / "0.3.0-rc.3-cut-packet.md"
            cut_packet.write_text("# RC3\n\n> **Prepared metadata; publication pending.**\n", encoding="utf-8")
            historical_receipts = [{"receipt_id": "z-history"}, {"receipt_id": "a-history"}]
            evidence_path = qualification_directory / "release-evidence-v1.json"
            evidence_path.write_text(
                json.dumps({"schema_version": 1, "receipts": historical_receipts}) + "\n",
                encoding="utf-8",
            )

            receipt = build_receipt(receipt_facts())
            receipt_path = root / "published-release-receipt.json"
            write_receipt(receipt, receipt_path)
            live_appcast = root / "live-appcast.xml"
            live_appcast.write_bytes(b"appcast")
            appcast = next(artifact for artifact in receipt["artifacts"] if artifact["kind"] == "appcast")
            appcast["sha256"] = file_sha256(live_appcast)
            receipt["appcast"]["live_pages_sha256"] = file_sha256(live_appcast)
            receipt["receipt_sha256"] = receipt_sha256(receipt)
            write_receipt(receipt, receipt_path)

            release = {
                "id": 67890,
                "tag_name": "v0.3.0-rc.3",
                "name": "v0.3.0-rc.3",
                "target_commitish": SOURCE_SHA,
                "draft": False,
                "immutable": True,
                "prerelease": True,
                "published_at": "2026-08-05T13:00:00Z",
                "assets": [
                    {
                        "digest": f"sha256:{artifact['sha256']}",
                        "id": artifact["asset_id"],
                        "name": artifact["name"],
                        "size": artifact["size_bytes"],
                    }
                    for artifact in receipt["artifacts"]
                ]
                + [
                    {
                        "digest": f"sha256:{file_sha256(receipt_path)}",
                        "id": 4,
                        "name": "release-receipt.json",
                        "size": receipt_path.stat().st_size,
                    }
                ],
            }

            first = reconcile(root, workflow_run(), release, receipt, receipt_path, live_appcast)
            second = reconcile(root, workflow_run(), release, receipt, receipt_path, live_appcast)

            self.assertEqual(first, second)
            checked_receipt = root / first["receipt"]
            self.assertEqual(checked_receipt.read_bytes(), receipt_path.read_bytes())
            evidence = json.loads((root / first["evidence_index"]).read_text(encoding="utf-8"))
            self.assertEqual(evidence["receipts"][:2], historical_receipts)
            self.assertEqual(len(evidence["receipts"]), 4)
            self.assertTrue(all(item["workflow_conclusion"] == "success" for item in evidence["receipts"][2:]))
            updated_qualification = json.loads(qualification_path.read_text(encoding="utf-8"))
            self.assertEqual(updated_qualification["candidate"]["source_git_sha"], SOURCE_SHA)
            qualification_record = json.loads((root / first["qualification_record"]).read_text(encoding="utf-8"))
            self.assertEqual(qualification_record, updated_qualification)
            self.assertIn("Published and immutable", cut_packet.read_text(encoding="utf-8"))

    def test_reconcile_fails_closed_for_wrong_actor_or_live_appcast(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            receipt = build_receipt(receipt_facts())
            receipt_path = root / "release-receipt.json"
            write_receipt(receipt, receipt_path)
            live_appcast = root / "appcast.xml"
            live_appcast.write_bytes(b"wrong")
            release = {
                "id": 67890,
                "tag_name": "v0.3.0-rc.3",
                "name": "v0.3.0-rc.3",
                "target_commitish": SOURCE_SHA,
                "draft": False,
                "immutable": True,
                "prerelease": True,
                "published_at": "2026-08-05T13:00:00Z",
                "assets": [
                    {
                        "digest": f"sha256:{artifact['sha256']}",
                        "id": artifact["asset_id"],
                        "name": artifact["name"],
                        "size": artifact["size_bytes"],
                    }
                    for artifact in receipt["artifacts"]
                ]
                + [
                    {
                        "digest": f"sha256:{file_sha256(receipt_path)}",
                        "id": 4,
                        "name": "release-receipt.json",
                        "size": receipt_path.stat().st_size,
                    }
                ],
            }
            bad_run = workflow_run()
            bad_run["actor"] = {"login": "not-approved"}

            with self.assertRaisesRegex(ReleaseEvidenceError, "approved release actor"):
                reconcile(root, bad_run, release, receipt, receipt_path, live_appcast)
            with self.assertRaisesRegex(ReleaseEvidenceError, "Live Pages appcast"):
                reconcile(root, workflow_run(), release, receipt, receipt_path, live_appcast)

    def test_reconcile_attributes_recovered_success_to_the_recovery_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            receipt_path = root / "release-receipt.json"
            receipt_path.write_text("{}\n", encoding="utf-8")
            qualification_path = root / "docs" / "qualification.json"
            cut_packet_path = root / "docs" / "cut-packet.md"
            qualification_path.parent.mkdir(parents=True)
            qualification_path.write_text("{}\n", encoding="utf-8")
            cut_packet_path.write_text("# Cut packet\n", encoding="utf-8")
            receipt = {
                "appcast": {"live_pages_url": "https://example.com/appcast.xml"},
                "release": {"id": 20, "tag": "v0.3.0"},
                "source_sha": SOURCE_SHA,
                "tier1_case_references": ["release-workflow-identity"],
                "workflow": {"run_id": 30},
            }
            publication = {
                "live_appcast_sha256": "a" * 64,
                "original_workflow_conclusion": "failure",
                "published_at": "2026-08-07T21:42:28Z",
                "receipt_asset_id": 40,
                "receipt_file_sha256": file_sha256(receipt_path),
                "recovery_workflow_run_id": 50,
            }
            with (
                patch("scripts.release_evidence.validate_publication", return_value=publication),
                patch("scripts.release_evidence._update_qualification", return_value=qualification_path),
                patch("scripts.release_evidence._snapshot_qualification", return_value=qualification_path),
                patch("scripts.release_evidence._update_cut_packet", return_value=cut_packet_path),
            ):
                outputs = reconcile(root, {}, {}, receipt, receipt_path, root / "appcast.xml")

            publication_record = json.loads((root / outputs["publication_record"]).read_text(encoding="utf-8"))
            self.assertEqual(publication_record["workflow_run_id"], 30)
            self.assertEqual(publication_record["workflow_conclusion"], "failure")
            self.assertEqual(
                publication_record["recovery_workflow_run"],
                {"operation": "pypi_recovery", "workflow_conclusion": "success", "workflow_run_id": 50},
            )
            self.assertEqual(effective_successful_workflow_run_id(publication_record), 50)
            evidence = json.loads((root / outputs["evidence_index"]).read_text(encoding="utf-8"))
            self.assertEqual(evidence["receipts"][0]["release_run_id"], 30)
            self.assertEqual(evidence["receipts"][0]["workflow_conclusion"], "failure")
            self.assertEqual(evidence["receipts"][0]["recovery_workflow_run"]["workflow_run_id"], 50)
            self.assertEqual(
                effective_successful_workflow_run_id(evidence["receipts"][0], run_id_field="release_run_id"),
                50,
            )
            self.assertEqual(outputs["release_run_id"], 30)

    def test_reconcile_rejects_partial_manifest_inputs_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with patch("scripts.release_evidence.validate_publication") as validate_publication:
                with self.assertRaisesRegex(ReleaseEvidenceError, "manifest inputs are partial"):
                    reconcile(
                        root,
                        {},
                        {},
                        {},
                        root / "release-receipt.json",
                        root / "appcast.xml",
                        prior_tag="v0.3.0",
                    )

            validate_publication.assert_not_called()
            self.assertFalse((root / "docs").exists())

    def test_reconcile_rejects_conflicting_immutable_qualification_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            receipt = minimal_reconcile_receipt()
            receipt_path = root / "release-receipt.json"
            receipt_path.write_text("{}\n", encoding="utf-8")
            qualification_path = root / "docs/qualification/stable-signed-qualification-v1.json"
            cut_packet_path = root / "docs/0.3.0-cut-packet.md"
            qualification_path.parent.mkdir(parents=True)
            qualification = qualification_for_receipt(receipt)
            qualification_path.write_text(
                json.dumps(qualification, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            cut_packet_path.write_text("# Cut packet\n", encoding="utf-8")
            publication = {
                "live_appcast_sha256": "a" * 64,
                "original_workflow_conclusion": "success",
                "published_at": "2026-08-07T21:42:28Z",
                "receipt_asset_id": 40,
                "receipt_file_sha256": file_sha256(receipt_path),
                "recovery_workflow_run_id": None,
            }
            with (
                patch("scripts.release_evidence.validate_publication", return_value=publication),
                patch("scripts.release_evidence._update_qualification", return_value=qualification_path),
                patch("scripts.release_evidence._update_cut_packet", return_value=cut_packet_path),
            ):
                reconcile(root, {}, {}, receipt, receipt_path, root / "appcast.xml")
                qualification["status"] = "later-rolling-state"
                qualification_path.write_text(
                    json.dumps(qualification, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ReleaseEvidenceError, "immutable release v0.3.0 conflicts"):
                    reconcile(root, {}, {}, receipt, receipt_path, root / "appcast.xml")

    def test_reconcile_rejects_qualification_snapshot_identity_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            receipt = minimal_reconcile_receipt()
            receipt_path = root / "release-receipt.json"
            receipt_path.write_text("{}\n", encoding="utf-8")
            qualification_path = root / "docs/qualification/stable-signed-qualification-v1.json"
            cut_packet_path = root / "docs/0.3.0-cut-packet.md"
            qualification_path.parent.mkdir(parents=True)
            qualification = qualification_for_receipt(receipt)
            qualification["candidate"]["source_git_sha"] = "0" * 40
            qualification_path.write_text(
                json.dumps(qualification, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            cut_packet_path.write_text("# Cut packet\n", encoding="utf-8")
            publication = {
                "live_appcast_sha256": "a" * 64,
                "original_workflow_conclusion": "success",
                "published_at": "2026-08-07T21:42:28Z",
                "receipt_asset_id": 40,
                "receipt_file_sha256": file_sha256(receipt_path),
                "recovery_workflow_run_id": None,
            }
            with (
                patch("scripts.release_evidence.validate_publication", return_value=publication),
                patch("scripts.release_evidence._update_qualification", return_value=qualification_path),
                patch("scripts.release_evidence._update_cut_packet", return_value=cut_packet_path),
            ):
                with self.assertRaisesRegex(ReleaseEvidenceError, "exact release receipt identity"):
                    reconcile(root, {}, {}, receipt, receipt_path, root / "appcast.xml")

    def test_reconcile_wires_complete_manifest_inputs_and_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            receipt_path = root / "release-receipt.json"
            receipt_path.write_text("{}\n", encoding="utf-8")
            signed_ui_path = root / "signed-artifact-ui-receipt.json"
            signed_ui_path.write_text("{}\n", encoding="utf-8")
            signed_ui_archive_path = root / "signed-artifact-ui.zip"
            signed_ui_archive_path.write_bytes(b"archive")
            qualification_path = root / "docs/qualification/stable-signed-qualification-v1.json"
            cut_packet_path = root / "docs/0.3.0-cut-packet.md"
            qualification_path.parent.mkdir(parents=True)
            cut_packet_path.write_text("# Cut packet\n", encoding="utf-8")
            receipt = minimal_reconcile_receipt()
            qualification_path.write_text(
                json.dumps(qualification_for_receipt(receipt), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            publication = {
                "live_appcast_sha256": "a" * 64,
                "original_workflow_conclusion": "success",
                "published_at": "2026-08-07T21:42:28Z",
                "receipt_asset_id": 40,
                "receipt_file_sha256": file_sha256(receipt_path),
                "recovery_workflow_run_id": None,
            }
            manifest = {
                "manifest_sha256": "b" * 64,
                "signed_ui_artifact": {
                    "artifact_sha256": "d" * 64,
                    "receipt_sha256": "c" * 64,
                },
            }
            with (
                patch("scripts.release_evidence.validate_publication", return_value=publication),
                patch("scripts.release_evidence._update_qualification", return_value=qualification_path),
                patch("scripts.release_evidence._update_cut_packet", return_value=cut_packet_path),
                patch(
                    "scripts.release_qualification_manifest.build_manifest_for_reconciled_release",
                    return_value=manifest,
                ) as build_manifest,
            ):
                outputs = reconcile(
                    root,
                    {},
                    {},
                    receipt,
                    receipt_path,
                    root / "appcast.xml",
                    prior_tag="v0.3.0-rc.3",
                    signed_ui_artifact_id=50,
                    signed_ui_artifact_digest="d" * 64,
                    signed_ui_archive_path=signed_ui_archive_path,
                    signed_ui_receipt_path=signed_ui_path,
                    signed_ui_receipt_file_sha256=file_sha256(signed_ui_path),
                    evidence_ref="automation/release-evidence-v0.3.0",
                    evidence_base_sha="e" * 40,
                    runner_sha="f" * 40,
                    sparkle_route="rc",
                )

            build_manifest.assert_called_once()
            self.assertEqual(
                build_manifest.call_args.kwargs["qualification_path"],
                Path("docs/release-evidence/v0.3.0/qualification-record.json"),
            )
            self.assertEqual(outputs["manifest_sha256"], "b" * 64)
            self.assertEqual(outputs["signed_ui_receipt_sha256"], "c" * 64)
            self.assertEqual(
                outputs["qualification_manifest"],
                "docs/release-evidence/v0.3.0/qualification-manifest.json",
            )
            self.assertEqual(
                outputs["qualification_record"],
                "docs/release-evidence/v0.3.0/qualification-record.json",
            )


if __name__ == "__main__":
    unittest.main()
