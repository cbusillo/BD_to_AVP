import json
import tempfile
import unittest

from pathlib import Path
from typing import Any

from scripts.qualify_release_scope import DEFAULT_POLICY_PATH, load_policy
from scripts.release_receipt import build_receipt as build_release_receipt
from scripts.release_receipt import file_sha256, write_receipt
from scripts.tier3_receipt import (
    Tier3ReceiptError,
    build_receipt as build_tier3_receipt,
    load_validated_receipt_bytes,
    receipt_sha256,
    validate_receipt,
)


SOURCE_SHA = "a" * 40
APP_TREE_SHA256 = "b" * 64
DMG_SHA256 = "c" * 64
CHECKSUM_SHA256 = "d" * 64
APPCAST_SHA256 = "e" * 64


def release_facts() -> dict[str, object]:
    return {
        "release_route": "prerelease",
        "source_sha": SOURCE_SHA,
        "workflow_actor": "shiny-code-bot",
        "workflow_run_id": 12345,
        "workflow_run_attempt": 1,
        "package_version": "0.3.0rc3",
        "public_version": "0.3.0-rc.3",
        "build_version": "160",
        "release_tag": "v0.3.0-rc.3",
        "release_name": "v0.3.0-rc.3",
        "release_id": 67890,
        "release_created_at": "2026-08-01T09:00:00Z",
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


class Tier3ReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = load_policy(DEFAULT_POLICY_PATH)
        self.cases = {case["id"]: case for case in self.policy["cases"]}

    def write_release_receipt(self, root: Path) -> tuple[Path, dict[str, Any]]:
        path = root / "docs" / "release-evidence" / "test" / "release-receipt.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        receipt = build_release_receipt(release_facts())
        write_receipt(receipt, path)
        return path, receipt

    def hardware(self, case: dict[str, Any]) -> dict[str, Any]:
        hardware = case["hardware"]
        if not hardware["required"]:
            return {"class": "none", "identity": {}}
        values = {
            "vendor_id": "05e3",
            "product_id": "0749",
            "transport": "usb",
            "makemkv_version": "1.18.1",
            "model_family": "vision-pro",
            "chip_family": "m2",
            "visionos_major": "26",
        }
        return {
            "class": hardware["classes"][0],
            "identity": {field: values[field] for field in hardware["identity_fields"]},
        }

    def build(
        self,
        root: Path,
        case_id: str = "clean-machine-signed-update",
        *,
        result_status: str = "passed",
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        release_path, release_receipt = self.write_release_receipt(root)
        case = self.cases[case_id]
        assertion_status = {
            "failed": "failed",
            "not_applicable": "not_run",
            "passed": "passed",
            "skipped": "not_run",
        }[result_status]
        evidence = (
            [{"kind": case["allowed_evidence_kinds"][0], "sha256": "f" * 64}]
            if result_status in {"passed", "failed"}
            else []
        )
        cleanup_status = case["allowed_cleanup_results"][0]
        if result_status in {"skipped", "not_applicable"} and "not-applicable" in case["allowed_cleanup_results"]:
            cleanup_status = "not-applicable"
        facts = {
            "assertions": {assertion_id: assertion_status for assertion_id in case["required_assertions"]},
            "cleanup": {"status": cleanup_status, "evidence_sha256": "1" * 64},
            "completed_at": "2026-08-01T10:10:00Z",
            "environment": {
                "environment_class": case["environment"]["classes"][0],
                "architecture": "arm64",
                "macos_version": "26.0",
                "macos_build": "25A123",
            },
            "evidence": evidence,
            "evidence_source": case["allowed_evidence_sources"][0],
            "hardware": self.hardware(case),
            "release_receipt_file_sha256": file_sha256(release_path),
            "release_receipt_reference": release_path.relative_to(root).as_posix(),
            "result": {
                "status": result_status,
                "reason_code": "all_assertions_passed" if result_status == "passed" else "operator-deferred",
            },
            "started_at": "2026-08-01T10:00:00Z",
        }
        receipt = build_tier3_receipt(
            facts,
            policy_id=self.policy["policy_id"],
            case=case,
            release_receipt=release_receipt,
        )
        return receipt, case

    def validate(self, receipt: dict[str, Any], case: dict[str, Any], root: Path) -> None:
        validate_receipt(
            receipt,
            policy_id=self.policy["policy_id"],
            case=case,
            reference_content=lambda reference: (root / reference).read_bytes(),
        )

    def test_build_is_deterministic_and_binds_exact_release_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            first, case = self.build(root)
            second, _ = self.build(root)
            self.validate(first, case, root)

        self.assertEqual(first, second)
        self.assertEqual(first["release_identity"]["source_sha"], SOURCE_SHA)
        self.assertEqual(first["release_identity"]["artifact_sha256"]["dmg"], DMG_SHA256)
        self.assertEqual(first["cadence"]["expires_on"], "2026-08-31")
        self.assertEqual(first["receipt_sha256"], receipt_sha256(first))

    def test_validation_rejects_private_or_mismatched_environment_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            receipt, case = self.build(root)
            receipt["environment"]["hostname"] = "private-mac"
            receipt["receipt_sha256"] = receipt_sha256(receipt)
            with self.assertRaisesRegex(Tier3ReceiptError, "keys changed"):
                self.validate(receipt, case, root)

            receipt, case = self.build(root)
            receipt["environment"]["architecture"] = "x86_64"
            receipt["receipt_sha256"] = receipt_sha256(receipt)
            with self.assertRaisesRegex(Tier3ReceiptError, "architecture"):
                self.validate(receipt, case, root)

    def test_validation_rejects_release_or_artifact_identity_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            receipt, case = self.build(root)
            receipt["release_identity"]["artifact_sha256"]["dmg"] = "0" * 64
            receipt["receipt_sha256"] = receipt_sha256(receipt)
            with self.assertRaisesRegex(Tier3ReceiptError, "release or artifact identity"):
                self.validate(receipt, case, root)

    def test_operator_receipt_records_explicit_skipped_reason(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            receipt, case = self.build(root, "usb-bluray-makemkv", result_status="skipped")
            self.validate(receipt, case, root)

        self.assertEqual(receipt["result"], {"status": "skipped", "reason_code": "operator-deferred"})
        self.assertTrue(all(assertion["status"] == "not_run" for assertion in receipt["assertions"]))

    def test_loader_rejects_mutated_receipt_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            receipt, case = self.build(root)
            receipt["cleanup"]["status"] = "restored"
            content = (json.dumps(receipt, sort_keys=True) + "\n").encode()
            with self.assertRaisesRegex(Tier3ReceiptError, "receipt_sha256"):
                load_validated_receipt_bytes(
                    content,
                    policy_id=self.policy["policy_id"],
                    case=case,
                    reference_content=lambda reference: (root / reference).read_bytes(),
                )


if __name__ == "__main__":
    unittest.main()
