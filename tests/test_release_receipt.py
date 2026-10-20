import json
import tempfile
import unittest

from dataclasses import replace
from pathlib import Path

from scripts.release_receipt import (
    ArtifactReceiptExpectation,
    DEFAULT_POLICY_PATH,
    ReleaseReceiptError,
    build_receipt,
    file_sha256,
    load_validated_artifact_receipt,
    validate_receipt,
    write_receipt,
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


if __name__ == "__main__":
    unittest.main()
