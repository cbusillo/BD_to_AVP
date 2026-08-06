import json
import tempfile
import unittest

from pathlib import Path
from typing import Any

from scripts.tier3_operator_receipt import OperatorReceiptError, build_operator_receipt


REPO_ROOT = Path(__file__).resolve().parents[1]
RELEASE_RECEIPT = REPO_ROOT / "docs/release-evidence/v0.3.0-rc.3/release-receipt.json"


class Tier3OperatorReceiptTests(unittest.TestCase):
    @staticmethod
    def answers(case_id: str) -> dict[str, Any]:
        hardware: dict[str, Any]
        cleanup_status: str
        if case_id == "vision-pro-physical-playback":
            hardware = {
                "class": "vision-pro",
                "identity": {"chip_family": "m2", "model_family": "apple-vision-pro", "visionos_major": "26"},
            }
            cleanup_status = "not-applicable"
            observations = {
                "playback": "completed",
                "spatial_presentation": "verified",
                "stereo_playback": "started",
                "transfer": "completed",
            }
        else:
            hardware = {
                "class": "usb-bluray-drive",
                "identity": {
                    "makemkv_version": "1.18.1",
                    "product_id": "5678",
                    "transport": "usb",
                    "vendor_id": "1234",
                },
            }
            cleanup_status = "recovered" if case_id == "usb-bluray-makemkv" else "restored"
            observations = (
                {
                    "cancellation": "recovered",
                    "drive_discovery": "detected",
                    "ejection": "ejected",
                    "makemkv": "installed",
                }
                if case_id == "usb-bluray-makemkv"
                else {
                    "conversion": "completed",
                    "output": "verified",
                    "protected_source": "opened",
                    "recovery": "clean",
                }
            )
        return {
            "case_id": case_id,
            "cleanup_status": cleanup_status,
            "completed_at": "2026-08-06T12:10:00Z",
            "disposition": "completed",
            "environment": {
                "architecture": "arm64",
                "environment_class": "dedicated-hardware",
                "macos_build": "25A123",
                "macos_version": "26.0",
            },
            "hardware": hardware,
            "observations": observations,
            "reason_code": "all-assertions-passed",
            "schema_version": 1,
            "started_at": "2026-08-06T12:00:00Z",
        }

    @staticmethod
    def build(answers: dict[str, Any], root: Path) -> dict[str, Any]:
        return dict(
            build_operator_receipt(
                answers,
                repo=REPO_ROOT,
                release_receipt_path=RELEASE_RECEIPT,
                evidence_directory=root / "evidence",
            )
        )

    def test_builds_passed_receipts_for_every_physical_case(self) -> None:
        for case_id in (
            "usb-bluray-makemkv",
            "protected-real-media-conversion",
            "vision-pro-physical-playback",
        ):
            with self.subTest(case_id=case_id), tempfile.TemporaryDirectory() as temporary_directory:
                receipt = self.build(self.answers(case_id), Path(temporary_directory))
                self.assertEqual(receipt["case_id"], case_id)
                self.assertEqual(receipt["result"], {"reason_code": "all_assertions_passed", "status": "passed"})
                self.assertNotIn("/Users/", json.dumps(receipt))

    def test_failed_outcome_derives_failed_assertion_and_reason(self) -> None:
        answers = self.answers("usb-bluray-makemkv")
        observations = dict(answers["observations"])
        observations["ejection"] = "failed"
        answers["observations"] = observations
        answers["reason_code"] = "operator-assertion-failed"
        with tempfile.TemporaryDirectory() as temporary_directory:
            receipt = self.build(answers, Path(temporary_directory))
        self.assertEqual(receipt["result"], {"reason_code": "operator_assertion_failed", "status": "failed"})
        assertions = {item["id"]: item["status"] for item in receipt["assertions"]}
        self.assertEqual(assertions["drive-ejected"], "failed")

    def test_skipped_outcome_requires_empty_observations_and_bounded_reason(self) -> None:
        answers = self.answers("vision-pro-physical-playback")
        answers["disposition"] = "skipped"
        answers["observations"] = {}
        answers["reason_code"] = "hardware-unavailable"
        with tempfile.TemporaryDirectory() as temporary_directory:
            receipt = self.build(answers, Path(temporary_directory))
        self.assertEqual(receipt["result"], {"reason_code": "hardware-unavailable", "status": "skipped"})
        self.assertEqual({item["status"] for item in receipt["assertions"]}, {"not_run"})
        self.assertEqual(receipt["evidence"], [])

    def test_rejects_unbounded_or_private_observation_fields(self) -> None:
        answers = self.answers("protected-real-media-conversion")
        observations = dict(answers["observations"])
        observations["disc_title"] = "/Volumes/Private Disc"
        answers["observations"] = observations
        with (
            tempfile.TemporaryDirectory() as temporary_directory,
            self.assertRaisesRegex(OperatorReceiptError, "observations must contain exactly"),
        ):
            self.build(answers, Path(temporary_directory))

    def test_rejects_missing_hardware_identity(self) -> None:
        answers = self.answers("usb-bluray-makemkv")
        hardware = dict(answers["hardware"])
        hardware["identity"] = {"transport": "usb"}
        answers["hardware"] = hardware
        with (
            tempfile.TemporaryDirectory() as temporary_directory,
            self.assertRaisesRegex(OperatorReceiptError, "Generated operator receipt is invalid"),
        ):
            self.build(answers, Path(temporary_directory))


if __name__ == "__main__":
    unittest.main()
