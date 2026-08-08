import hashlib
import io
import json
import tempfile
import unittest

from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path

from scripts.release_receipt import build_receipt
from scripts.signed_artifact_receipt import (
    MAX_RECEIPT_BYTES,
    PROFILE_CASE_ID,
    SignedArtifactReceiptError,
    SignedArtifactReceiptExpectation,
    build_receipt as build_signed_receipt,
    load_validated_receipt,
    main,
    release_expectation_from_receipt,
    validate_policy_case,
    validate_receipt_files,
)
from scripts.qualify_release_scope import DEFAULT_POLICY_PATH


CANDIDATE_SHA = "b" * 40
DMG_SHA256 = "c" * 64
CHECKSUM_SHA256 = "d" * 64
APPCAST_SHA256 = "e" * 64
APP_TREE_SHA256 = "f" * 64


def release_facts() -> dict[str, object]:
    return {
        "release_route": "prerelease",
        "source_sha": CANDIDATE_SHA,
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
            {"kind": "checksum", "name": "SHA256SUMS", "sha256": CHECKSUM_SHA256, "size_bytes": 100, "asset_id": 2},
            {"kind": "appcast", "name": "appcast.xml", "sha256": APPCAST_SHA256, "size_bytes": 500, "asset_id": 3},
        ],
    }


def evidence() -> dict[str, str]:
    return {
        "accessibility-tree": "1" * 64,
        "screenshot-dark": "2" * 64,
        "screenshot-light": "3" * 64,
        "ui-result": "4" * 64,
    }


class SignedArtifactReceiptTests(unittest.TestCase):
    def expectation(self) -> SignedArtifactReceiptExpectation:
        release_receipt = build_receipt(release_facts())
        return release_expectation_from_receipt(
            release_receipt,
            policy_id=validate_policy_case(json.loads(DEFAULT_POLICY_PATH.read_text(encoding="utf-8"))),
            case_id=PROFILE_CASE_ID,
            workflow_run_id=12345,
            workflow_run_attempt=2,
            release_receipt_asset_id=4,
            release_receipt_file_sha256="a" * 64,
        )

    def test_receipt_is_bound_to_exact_release_artifact_and_ui_case(self) -> None:
        expectation = self.expectation()
        receipt = build_signed_receipt(expectation=expectation, evidence=evidence())

        self.assertEqual(receipt["case_id"], PROFILE_CASE_ID)
        self.assertEqual(receipt["candidate_sha"], CANDIDATE_SHA)
        self.assertEqual(receipt["workflow"], {"job": "signed-artifact-ui", "run_attempt": 2, "run_id": 12345})
        self.assertEqual(receipt["release_receipt"]["asset_id"], 4)
        self.assertEqual(receipt["dmg"]["size_bytes"], 1000)
        self.assertEqual(receipt["signed_app_tree_sha256"], APP_TREE_SHA256)

    def test_validation_rejects_replay_or_artifact_mismatch(self) -> None:
        expectation = self.expectation()
        receipt = build_signed_receipt(expectation=expectation, evidence=evidence())
        mismatches = (
            replace(expectation, workflow_run_attempt=3),
            replace(expectation, release_id=999),
            replace(expectation, release_receipt_asset_id=999),
            replace(expectation, release_receipt_file_sha256="9" * 64),
            replace(expectation, release_receipt_sha256="8" * 64),
            replace(expectation, dmg_asset_id=999),
            replace(expectation, dmg_size=1001),
            replace(expectation, signed_app_tree_sha256="7" * 64),
        )
        for mismatch in mismatches:
            with self.subTest(mismatch=mismatch), tempfile.TemporaryDirectory() as temporary_directory:
                path = Path(temporary_directory) / "signed-artifact-ui-receipt.json"
                with self.assertRaises(SignedArtifactReceiptError):
                    path.write_text(json.dumps(receipt), encoding="utf-8")
                    load_validated_receipt(
                        path,
                        mismatch,
                        expected_file_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                    )

    def test_validation_rejects_external_file_digest_mismatch_and_oversized_receipt(self) -> None:
        expectation = self.expectation()
        receipt = build_signed_receipt(expectation=expectation, evidence=evidence())
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "signed-artifact-ui-receipt.json"
            path.write_text(json.dumps(receipt), encoding="utf-8")
            with self.assertRaisesRegex(SignedArtifactReceiptError, "workflow output"):
                load_validated_receipt(path, expectation, expected_file_sha256="9" * 64)

            path.write_bytes(b" " * (MAX_RECEIPT_BYTES + 1))
            with self.assertRaisesRegex(SignedArtifactReceiptError, "byte limit"):
                load_validated_receipt(
                    path,
                    expectation,
                    expected_file_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                )

    def test_release_expectation_rejects_wrong_workflow_attempt(self) -> None:
        release_receipt = build_receipt(release_facts())

        with self.assertRaisesRegex(SignedArtifactReceiptError, "run attempt"):
            release_expectation_from_receipt(
                release_receipt,
                policy_id="release-qualification-policy-v1",
                case_id=PROFILE_CASE_ID,
                workflow_run_id=12345,
                workflow_run_attempt=3,
                release_receipt_asset_id=4,
                release_receipt_file_sha256="a" * 64,
            )

    def test_policy_case_must_remain_artifact_owned_tier2(self) -> None:
        policy = json.loads(DEFAULT_POLICY_PATH.read_text(encoding="utf-8"))
        policy_id = validate_policy_case(policy)
        self.assertEqual(policy_id, "release-qualification-policy-v1")

        case = next(item for item in policy["cases"] if item["id"] == PROFILE_CASE_ID)
        case["artifact_owned"] = False
        with self.assertRaises(SignedArtifactReceiptError):
            validate_policy_case(policy)

    def test_cli_validates_receipt_against_exact_checked_release_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            release_receipt = build_receipt(release_facts())
            release_receipt_path = root / "release-receipt.json"
            release_receipt_path.write_text(
                json.dumps(release_receipt, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            release_receipt_file_sha256 = hashlib.sha256(release_receipt_path.read_bytes()).hexdigest()
            expectation = release_expectation_from_receipt(
                release_receipt,
                policy_id=validate_policy_case(json.loads(DEFAULT_POLICY_PATH.read_text(encoding="utf-8"))),
                case_id=PROFILE_CASE_ID,
                workflow_run_id=12345,
                workflow_run_attempt=2,
                release_receipt_asset_id=4,
                release_receipt_file_sha256=release_receipt_file_sha256,
            )
            signed_receipt = build_signed_receipt(expectation=expectation, evidence=evidence())
            signed_receipt_path = root / "signed-artifact-ui-receipt.json"
            signed_receipt_path.write_text(
                json.dumps(signed_receipt, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            signed_receipt_file_sha256 = hashlib.sha256(signed_receipt_path.read_bytes()).hexdigest()

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                result = main(
                    [
                        "validate",
                        "--receipt",
                        str(signed_receipt_path),
                        "--receipt-file-sha256",
                        signed_receipt_file_sha256,
                        "--release-receipt",
                        str(release_receipt_path),
                        "--release-receipt-asset-id",
                        "4",
                        "--release-receipt-file-sha256",
                        release_receipt_file_sha256,
                        "--workflow-run-id",
                        "12345",
                        "--workflow-run-attempt",
                        "2",
                    ]
                )

            self.assertEqual(result, 0)
            self.assertEqual(
                json.loads(stdout.getvalue()),
                {
                    "candidate_sha": CANDIDATE_SHA,
                    "case_id": PROFILE_CASE_ID,
                    "receipt_sha256": signed_receipt["receipt_sha256"],
                    "valid": True,
                },
            )

            with self.assertRaisesRegex(SignedArtifactReceiptError, "immutable asset"):
                validate_receipt_files(
                    receipt_path=signed_receipt_path,
                    release_receipt_path=release_receipt_path,
                    policy_path=DEFAULT_POLICY_PATH,
                    case_id=PROFILE_CASE_ID,
                    workflow_run_id=12345,
                    workflow_run_attempt=2,
                    release_receipt_asset_id=4,
                    release_receipt_file_sha256="9" * 64,
                    receipt_file_sha256=signed_receipt_file_sha256,
                )


if __name__ == "__main__":
    unittest.main()
