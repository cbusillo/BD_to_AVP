import json
import tempfile
import unittest

from pathlib import Path

from scripts.release_evidence import ReleaseEvidenceError, reconcile
from scripts.release_receipt import (
    ReleaseReceiptError,
    build_receipt,
    file_sha256,
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
        with self.assertRaisesRegex(ReleaseReceiptError, "self-digest mismatch"):
            validate_receipt(receipt)

    def test_reconcile_writes_idempotent_checked_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            qualification_directory = root / "docs" / "qualification"
            qualification_directory.mkdir(parents=True)
            qualification = {
                "schema_version": 1,
                "candidate": {
                    "release_tag": "v0.3.0-rc.3",
                    "source_git_sha": None,
                    "dmg_sha256": None,
                    "signed_app_tree_sha256": None,
                    "release_run_id": None,
                    "release_id": None,
                    "appcast_sha256": None,
                },
            }
            qualification_path = qualification_directory / "rc3-signed-qualification-v1.json"
            qualification_path.write_text(json.dumps(qualification), encoding="utf-8")
            cut_packet = root / "docs" / "0.3.0-rc.3-cut-packet.md"
            cut_packet.write_text("# RC3\n\n> **Prepared metadata; publication pending.**\n", encoding="utf-8")

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
            self.assertEqual(len(evidence["receipts"]), 2)
            self.assertTrue(all(item["workflow_conclusion"] == "success" for item in evidence["receipts"]))
            updated_qualification = json.loads(qualification_path.read_text(encoding="utf-8"))
            self.assertEqual(updated_qualification["candidate"]["source_git_sha"], SOURCE_SHA)
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


if __name__ == "__main__":
    unittest.main()
