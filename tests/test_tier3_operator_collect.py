import io
import json
import plistlib
import tempfile
import unittest

from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from bd_to_avp.worker.protocol import PROTOCOL_VERSION, ZERO_JOB_ID
from scripts.tier3_operator_collect import (
    PROMPT_SPECS,
    MacOSOperations,
    OperatorCollectionError,
    PublicUSBDevice,
    collect_operator_answers,
    derive_protected_conversion_observations,
    detect_public_usb_devices,
    write_validated_answers,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
RELEASE_RECEIPT = REPO_ROOT / "docs/release-evidence/v0.3.0-rc.3/release-receipt.json"


class FakePrompter:
    def __init__(self, responses: dict[str, str]) -> None:
        self.responses = responses
        self.calls: list[Any] = []

    def choose(self, spec: Any) -> str:
        self.calls.append(spec)
        return self.responses[spec.identifier]


class FakeOperations:
    def __init__(self, usb_responses: list[tuple[PublicUSBDevice, ...]] | None = None) -> None:
        self.usb_responses = usb_responses or [(PublicUSBDevice("1234", "5678"),)]
        self.usb_index = 0

    def environment(self, environment_class: str) -> dict[str, str]:
        return {
            "architecture": "arm64",
            "environment_class": environment_class,
            "macos_build": "25A123",
            "macos_version": "26.0",
        }

    def usb_devices(self) -> tuple[PublicUSBDevice, ...]:
        index = min(self.usb_index, len(self.usb_responses) - 1)
        self.usb_index += 1
        return self.usb_responses[index]

    @staticmethod
    def makemkv_version() -> str:
        return "1.18.1"


def clock() -> Any:
    current = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)

    def tick() -> datetime:
        nonlocal current
        value = current
        current += timedelta(minutes=1)
        return value

    return tick


class Tier3OperatorCollectTests(unittest.TestCase):
    def test_detects_only_public_optical_usb_identity(self) -> None:
        payload = {
            "SPUSBDataType": [
                {
                    "_name": "Private Person's Blu-ray Drive",
                    "vendor_id": "0x1234  (Vendor Name)",
                    "product_id": "0x5678",
                    "serial_num": "PRIVATE-SERIAL-123",
                    "mount_point": "/Volumes/Private Movie",
                },
                {"_name": "USB Hub", "vendor_id": "0xabcd", "product_id": "0xef01"},
            ]
        }
        self.assertEqual(detect_public_usb_devices(payload), (PublicUSBDevice("1234", "5678"),))
        self.assertNotIn("PRIVATE", repr(detect_public_usb_devices(payload)))
        self.assertNotIn("/Volumes", repr(detect_public_usb_devices(payload)))

    def test_identical_usb_drives_remain_ambiguous(self) -> None:
        device = {"_name": "BD-ROM", "vendor_id": "0x1234", "product_id": "0x5678"}
        self.assertEqual(
            detect_public_usb_devices({"SPUSBDataType": [device, dict(device)]}),
            (PublicUSBDevice("1234", "5678"), PublicUSBDevice("1234", "5678")),
        )
        with self.assertRaisesRegex(OperatorCollectionError, "Multiple USB optical drives"):
            collect_operator_answers(
                case_id="usb-bluray-makemkv",
                environment_class="dedicated-hardware",
                operations=FakeOperations([(PublicUSBDevice("1234", "5678"), PublicUSBDevice("1234", "5678"))]),
                prompter=FakePrompter({}),
                clock=clock(),
            )

    def test_macos_operations_derives_environment_usb_and_bundle_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            app = Path(temporary_directory) / "MakeMKV.app"
            executable = app / "Contents/MacOS/makemkvcon"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"binary")
            with (app / "Contents/Info.plist").open("wb") as handle:
                plistlib.dump({"CFBundleShortVersionString": "1.18.1"}, handle)

            def runner(command: Any, timeout: int) -> bytes:
                self.assertGreater(timeout, 0)
                if "-productVersion" in command:
                    return b"26.0\n"
                if "-buildVersion" in command:
                    return b"25A123\n"
                return json.dumps(
                    {"SPUSBDataType": [{"_name": "BD-ROM", "vendor_id": "0x1234", "product_id": "0x5678"}]}
                ).encode()

            operations = MacOSOperations(
                runner=runner,
                architecture=lambda: "arm64",
                makemkv_path=lambda: executable,
            )
            self.assertEqual(
                operations.environment("dedicated-hardware"),
                {
                    "architecture": "arm64",
                    "environment_class": "dedicated-hardware",
                    "macos_build": "25A123",
                    "macos_version": "26.0",
                },
            )
            self.assertEqual(operations.usb_devices(), (PublicUSBDevice("1234", "5678"),))
            self.assertEqual(operations.makemkv_version(), "1.18.1")

    def test_usb_collection_derives_identity_and_detects_ejection(self) -> None:
        prompter = FakePrompter({"usb-cancellation": "recovered", "usb-ejection": "ready"})
        answers = collect_operator_answers(
            case_id="usb-bluray-makemkv",
            environment_class="dedicated-hardware",
            operations=FakeOperations([(PublicUSBDevice("1234", "5678"),), ()]),
            prompter=prompter,
            clock=clock(),
        )
        self.assertEqual(
            answers["hardware"]["identity"],
            {"makemkv_version": "1.18.1", "product_id": "5678", "transport": "usb", "vendor_id": "1234"},
        )
        self.assertEqual(answers["observations"]["ejection"], "ejected")
        self.assertEqual(answers["reason_code"], "all-assertions-passed")
        self.assertEqual([spec.identifier for spec in prompter.calls], ["usb-cancellation", "usb-ejection"])

    def test_protected_conversion_derives_terminal_output_and_cleanup_without_publishing_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            output = root / "Private Movie_AVP.mov"
            output.write_bytes(b"output")
            cleanup_path = root / "private-owned-workspace"
            worker_events = root / "private-worker-events.ndjson"
            job_id = "11111111-1111-4111-8111-111111111111"
            events = [
                {"protocol_version": PROTOCOL_VERSION, "type": "worker.ready", "job_id": ZERO_JOB_ID},
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "type": "job.started",
                    "job_id": job_id,
                    "payload": {"operation": "convert_source", "source": "/Volumes/Private Disc"},
                },
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "type": "job.completed",
                    "job_id": job_id,
                    "payload": {
                        "conversion_result": {
                            "output_path": str(output),
                            "size_bytes": output.stat().st_size,
                            "name": "Private Movie",
                        }
                    },
                },
            ]
            worker_events.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
            observations = derive_protected_conversion_observations(worker_events, [cleanup_path])
            self.assertEqual(
                observations,
                {"conversion": "completed", "output": "verified", "protected_source": "opened", "recovery": "clean"},
            )
            self.assertNotIn(str(root), json.dumps(observations))
            self.assertNotIn("Private", json.dumps(observations))

    def test_operator_cancellation_becomes_explicit_nonblocking_skip(self) -> None:
        answers = collect_operator_answers(
            case_id="vision-pro-physical-playback",
            environment_class="restorable-location",
            operations=FakeOperations(),
            prompter=FakePrompter({"vision-transfer": "cancel"}),
            clock=clock(),
            vision_model_family="apple-vision-pro",
            vision_chip_family="m2",
            visionos_major="26",
        )
        self.assertEqual(answers["disposition"], "skipped")
        self.assertEqual(answers["reason_code"], "operator-cancelled")
        self.assertEqual(answers["observations"], {})

    def test_explicit_skip_can_use_bounded_identity_overrides_without_probes(self) -> None:
        class UnavailableOperations:
            @staticmethod
            def environment(environment_class: str) -> dict[str, str]:
                raise AssertionError(environment_class)

            @staticmethod
            def usb_devices() -> tuple[PublicUSBDevice, ...]:
                raise AssertionError("USB probe must not run")

            @staticmethod
            def makemkv_version() -> str:
                raise AssertionError("MakeMKV probe must not run")

        answers = collect_operator_answers(
            case_id="usb-bluray-makemkv",
            environment_class="dedicated-hardware",
            operations=UnavailableOperations(),
            prompter=FakePrompter({}),
            clock=clock(),
            skip_reason="hardware-unavailable",
            usb_vendor_id="1234",
            usb_product_id="5678",
            makemkv_version="1.18.1",
            architecture="arm64",
            macos_version="26.0",
            macos_build="25A123",
        )
        self.assertEqual(answers["disposition"], "skipped")
        self.assertEqual(answers["reason_code"], "hardware-unavailable")

    def test_decision_required_worker_event_is_not_flattened_to_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            worker_events = root / "events.ndjson"
            job_id = "11111111-1111-4111-8111-111111111111"
            events = [
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "type": "job.started",
                    "job_id": job_id,
                    "payload": {"operation": "convert_source"},
                },
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "type": "job.decision_required",
                    "job_id": job_id,
                    "payload": {"decision": {"identifier": "private-diagnostic-id"}},
                },
            ]
            worker_events.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
            with self.assertRaisesRegex(OperatorCollectionError, "requires a decision"):
                derive_protected_conversion_observations(worker_events, [root / "cleanup"])

    def test_prompts_are_closed_physical_actions_or_subjective_judgments(self) -> None:
        for spec in PROMPT_SPECS.values():
            with self.subTest(prompt=spec.identifier):
                self.assertIn(spec.category, {"physical-action", "subjective-judgment"})
                self.assertNotIn("other", spec.choices)
                self.assertNotIn("text", spec.choices)
                self.assertLessEqual(len(spec.choices), 4)

    def test_private_hardware_identity_is_rejected_before_preview(self) -> None:
        with self.assertRaisesRegex(OperatorCollectionError, "bounded public identifier"):
            collect_operator_answers(
                case_id="vision-pro-physical-playback",
                environment_class="dedicated-hardware",
                operations=FakeOperations(),
                prompter=FakePrompter({}),
                clock=clock(),
                vision_model_family="/Users/alice/Private Vision Pro",
                vision_chip_family="m2",
                visionos_major="26",
            )

    def test_final_confirmation_cancellation_writes_nothing(self) -> None:
        answers = collect_operator_answers(
            case_id="vision-pro-physical-playback",
            environment_class="dedicated-hardware",
            operations=FakeOperations(),
            prompter=FakePrompter(
                {
                    "vision-transfer": "completed",
                    "vision-stereo": "started",
                    "vision-spatial": "verified",
                    "vision-playback": "completed",
                }
            ),
            clock=clock(),
            vision_model_family="apple-vision-pro",
            vision_chip_family="m2",
            visionos_major="26",
        )
        with tempfile.TemporaryDirectory() as temporary_directory, redirect_stdout(io.StringIO()) as stdout:
            output = Path(temporary_directory) / "answers.json"
            result = write_validated_answers(
                answers,
                repo=REPO_ROOT,
                release_receipt_path=RELEASE_RECEIPT,
                output_path=output,
                prompter=FakePrompter({"confirm-write": "cancel"}),
            )
            self.assertIsNone(result)
            self.assertFalse(output.exists())
            preview = stdout.getvalue()
            self.assertIn('"release_identity"', preview)
            self.assertNotIn("/Users/", preview)

    def test_validated_write_uses_exclusive_output(self) -> None:
        answers = collect_operator_answers(
            case_id="usb-bluray-makemkv",
            environment_class="dedicated-hardware",
            operations=FakeOperations([(PublicUSBDevice("1234", "5678"),), ()]),
            prompter=FakePrompter({"usb-cancellation": "failed", "usb-ejection": "ready"}),
            clock=clock(),
        )
        with tempfile.TemporaryDirectory() as temporary_directory, redirect_stdout(io.StringIO()):
            output = Path(temporary_directory) / "answers.json"
            preview = write_validated_answers(
                answers,
                repo=REPO_ROOT,
                release_receipt_path=RELEASE_RECEIPT,
                output_path=output,
                prompter=FakePrompter({"confirm-write": "write"}),
            )
            self.assertIsNotNone(preview)
            assert preview is not None
            self.assertEqual(preview["result"]["status"], "failed")
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), answers)
            with self.assertRaisesRegex(OperatorCollectionError, "already exists"):
                write_validated_answers(
                    answers,
                    repo=REPO_ROOT,
                    release_receipt_path=RELEASE_RECEIPT,
                    output_path=output,
                    prompter=FakePrompter({"confirm-write": "write"}),
                )


if __name__ == "__main__":
    unittest.main()
