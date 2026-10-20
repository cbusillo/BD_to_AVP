import json
import unittest

from pathlib import Path

from scripts.release_receipt import (
    ReleaseReceiptError,
    build_receipt,
    validate_receipt,
)
from scripts.release_workflow_policy import REQUIRED_ACTOR


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
        "workflow_actor": REQUIRED_ACTOR,
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


class ReleaseReceiptTests(unittest.TestCase):
    def test_archived_release_receipts_keep_their_recorded_actors(self) -> None:
        for path in (REPO_ROOT / "docs" / "release-evidence").glob("*/release-receipt.json"):
            with self.subTest(receipt=path.parent.name):
                validate_receipt(json.loads(path.read_bytes()))

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


if __name__ == "__main__":
    unittest.main()
