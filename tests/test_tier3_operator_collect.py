import io
import json
import plistlib
import tempfile
import unittest

from argparse import Namespace
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from bd_to_avp.worker.protocol import PROTOCOL_VERSION
from scripts.tier3_operator_collect import (
    PROMPT_SPECS,
    MacOSOperations,
    OperatorCollectionError,
    PublicUSBDevice,
    build_parser,
    collect_operator_answers,
    derive_protected_conversion_observations,
    detect_public_usb_devices,
    run_collection,
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
    def __init__(
        self,
        usb_responses: list[tuple[PublicUSBDevice, ...]] | None = None,
        makemkv_version: str | None = "1.18.1",
    ) -> None:
        self.usb_responses = usb_responses or [(PublicUSBDevice("1234", "5678"),)]
        self.usb_index = 0
        self.version = makemkv_version

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

    def makemkv_version(self) -> str | None:
        return self.version


def clock() -> Any:
    current = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)

    def tick() -> datetime:
        nonlocal current
        value = current
        current += timedelta(minutes=1)
        return value

    return tick


class Tier3OperatorCollectTests(unittest.TestCase):
    def test_standalone_parser_retains_release_binding_arguments(self) -> None:
        args = build_parser().parse_args(
            [
                "--case-id",
                "vision-pro-physical-playback",
                "--environment-class",
                "dedicated-hardware",
                "--output-answers",
                "answers.json",
                "--release-receipt",
                str(RELEASE_RECEIPT),
            ]
        )

        self.assertEqual(args.release_receipt, RELEASE_RECEIPT)
        self.assertEqual(args.repo, REPO_ROOT)

    def test_run_collection_preserves_injected_collection_flow(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory, redirect_stdout(io.StringIO()):
            output = Path(temporary_directory) / "answers.json"
            args = Namespace(
                architecture=None,
                case_id="vision-pro-physical-playback",
                cleanup_path=[],
                environment_class="dedicated-hardware",
                macos_build=None,
                macos_version=None,
                makemkv_version=None,
                output_answers=output,
                release_receipt=RELEASE_RECEIPT,
                repo=REPO_ROOT,
                skip_reason=None,
                usb_product_id=None,
                usb_vendor_id=None,
                vision_chip_family="m2",
                vision_model_family="apple-vision-pro",
                visionos_major="26",
                worker_events=None,
            )
            exit_code = run_collection(
                args,
                operations=FakeOperations(),
                prompter=FakePrompter(
                    {
                        "vision-transfer": "completed",
                        "vision-stereo": "started",
                        "vision-spatial": "verified",
                        "vision-playback": "completed",
                        "confirm-write": "write",
                    }
                ),
                clock=clock(),
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["case_id"], args.case_id)

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
                {
                    "_name": "External Storage",
                    "vendor_id": "0xabcd",
                    "product_id": "0xef02",
                    "Media": [{"size": "1 TB"}],
                },
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

    def test_makemkv_version_falls_back_to_bounded_command_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            executable = Path(temporary_directory) / "makemkvcon"
            executable.write_bytes(b"binary")
            operations = MacOSOperations(
                runner=lambda command, timeout: b"MakeMKV v1.18.2 linux(x64-release) started\n",
                makemkv_path=lambda: executable,
            )
            self.assertEqual(operations.makemkv_version(), "1.18.2")

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
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "type": "worker.ready",
                    "job_id": job_id,
                    "sequence": 0,
                    "payload": {},
                },
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "type": "job.started",
                    "job_id": job_id,
                    "sequence": 1,
                    "payload": {"operation": "convert_source", "source": "/Volumes/Private Disc"},
                },
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "type": "job.completed",
                    "job_id": job_id,
                    "sequence": 2,
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
            answers = collect_operator_answers(
                case_id="protected-real-media-conversion",
                environment_class="dedicated-hardware",
                operations=FakeOperations(),
                prompter=FakePrompter({"protected-run": "ready"}),
                clock=clock(),
                worker_events=worker_events,
                cleanup_paths=[cleanup_path],
            )
            self.assertEqual(answers["observations"], observations)
            self.assertEqual(answers["reason_code"], "all-assertions-passed")

    def test_long_worker_stream_is_filtered_without_losing_terminal_facts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            output = root / "output.mov"
            output.write_bytes(b"output")
            worker_events = root / "events.ndjson"
            job_id = "11111111-1111-4111-8111-111111111111"
            events = [
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "type": "worker.ready",
                    "job_id": job_id,
                    "sequence": 0,
                    "payload": {},
                },
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "type": "job.started",
                    "job_id": job_id,
                    "sequence": 1,
                    "payload": {"operation": "convert_source"},
                },
            ]
            events.extend(
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "type": "heartbeat",
                    "job_id": job_id,
                    "sequence": sequence,
                    "payload": {"elapsed_seconds": sequence, "private_path": str(root)},
                }
                for sequence in range(2, 2_502)
            )
            events.append(
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "type": "job.completed",
                    "job_id": job_id,
                    "sequence": 2_502,
                    "payload": {"conversion_result": {"output_path": str(output), "size_bytes": 6}},
                }
            )
            worker_events.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
            observations = derive_protected_conversion_observations(worker_events, [root / "cleanup"])
            self.assertEqual(observations["conversion"], "completed")
            self.assertEqual(observations["output"], "verified")

    def test_completed_environment_overrides_must_match_live_probe(self) -> None:
        with self.assertRaisesRegex(OperatorCollectionError, "environment identity does not match"):
            collect_operator_answers(
                case_id="vision-pro-physical-playback",
                environment_class="dedicated-hardware",
                operations=FakeOperations(),
                prompter=FakePrompter({}),
                clock=clock(),
                vision_model_family="apple-vision-pro",
                vision_chip_family="m2",
                visionos_major="26",
                architecture="arm64",
                macos_version="26.0",
                macos_build="25A999",
            )

    def test_usb_overrides_must_match_live_probe(self) -> None:
        with self.assertRaisesRegex(OperatorCollectionError, "USB identity does not match"):
            collect_operator_answers(
                case_id="usb-bluray-makemkv",
                environment_class="dedicated-hardware",
                operations=FakeOperations([(PublicUSBDevice("abcd", "ef01"),)]),
                prompter=FakePrompter({}),
                clock=clock(),
                usb_vendor_id="1234",
                usb_product_id="5678",
                makemkv_version="1.18.1",
            )

    def test_cleanup_cancellation_with_known_leftovers_writes_no_answers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            output = root / "output.mov"
            output.write_bytes(b"output")
            cleanup_path = root / "owned-workspace"
            cleanup_path.mkdir()
            worker_events = root / "events.ndjson"
            job_id = "11111111-1111-4111-8111-111111111111"
            events = [
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "type": "job.started",
                    "job_id": job_id,
                    "sequence": 0,
                    "payload": {"operation": "convert_source"},
                },
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "type": "job.completed",
                    "job_id": job_id,
                    "sequence": 1,
                    "payload": {"conversion_result": {"output_path": str(output), "size_bytes": 6}},
                },
            ]
            worker_events.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
            with self.assertRaisesRegex(OperatorCollectionError, "cleanup remains; no public state was written"):
                collect_operator_answers(
                    case_id="protected-real-media-conversion",
                    environment_class="dedicated-hardware",
                    operations=FakeOperations(),
                    prompter=FakePrompter({"protected-run": "ready", "protected-cleanup": "cancel"}),
                    clock=clock(),
                    worker_events=worker_events,
                    cleanup_paths=[cleanup_path],
                )

    def test_protected_run_cancellation_requires_verified_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            cleanup_path = root / "owned-workspace"
            cleanup_path.mkdir()
            worker_events = root / "events.ndjson"
            worker_events.write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(OperatorCollectionError, "cleanup could not be verified"):
                collect_operator_answers(
                    case_id="protected-real-media-conversion",
                    environment_class="dedicated-hardware",
                    operations=FakeOperations(),
                    prompter=FakePrompter({"protected-run": "cancel"}),
                    clock=clock(),
                    worker_events=worker_events,
                    cleanup_paths=[cleanup_path],
                )

    def test_preidentity_worker_failure_has_bounded_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            worker_events = root / "events.ndjson"
            event = {
                "protocol_version": PROTOCOL_VERSION,
                "type": "job.failed",
                "job_id": "00000000-0000-0000-0000-000000000000",
                "sequence": 0,
                "payload": {"error": {"details": "/Users/private/source.mkv"}},
            }
            worker_events.write_text(json.dumps(event) + "\n", encoding="utf-8")
            with self.assertRaises(OperatorCollectionError) as raised:
                derive_protected_conversion_observations(worker_events, [root / "cleanup"])
            self.assertIn("before a qualification job identity", str(raised.exception))
            self.assertNotIn("/Users/", str(raised.exception))

    def test_out_of_sequence_worker_stream_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            worker_events = root / "events.ndjson"
            job_id = "11111111-1111-4111-8111-111111111111"
            events = [
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "type": "job.started",
                    "job_id": job_id,
                    "sequence": 0,
                    "payload": {"operation": "convert_source"},
                },
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "type": "job.failed",
                    "job_id": job_id,
                    "sequence": 2,
                    "payload": {},
                },
            ]
            worker_events.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
            with self.assertRaisesRegex(OperatorCollectionError, "ambiguous or out of sequence"):
                derive_protected_conversion_observations(worker_events, [root / "cleanup"])

    def test_usb_missing_machine_facts_produce_explicit_failed_answers_with_overrides(self) -> None:
        answers = collect_operator_answers(
            case_id="usb-bluray-makemkv",
            environment_class="dedicated-hardware",
            operations=FakeOperations([()], makemkv_version=None),
            prompter=FakePrompter({}),
            clock=clock(),
            usb_vendor_id="1234",
            usb_product_id="5678",
            makemkv_version="1.18.1",
        )
        self.assertEqual(
            answers["observations"],
            {
                "cancellation": "failed",
                "drive_discovery": "not-detected",
                "ejection": "failed",
                "makemkv": "missing",
            },
        )
        self.assertEqual(answers["reason_code"], "operator-assertion-failed")

    def test_usb_worker_events_require_cleanup_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            worker_events = Path(temporary_directory) / "events.ndjson"
            job_id = "11111111-1111-4111-8111-111111111111"
            events = [
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "type": "job.started",
                    "job_id": job_id,
                    "sequence": 0,
                    "payload": {"operation": "convert_source"},
                },
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "type": "job.cancelled",
                    "job_id": job_id,
                    "sequence": 1,
                    "payload": {},
                },
            ]
            worker_events.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
            with self.assertRaisesRegex(OperatorCollectionError, "requires at least one owned cleanup path"):
                collect_operator_answers(
                    case_id="usb-bluray-makemkv",
                    environment_class="dedicated-hardware",
                    operations=FakeOperations(),
                    prompter=FakePrompter({}),
                    clock=clock(),
                    worker_events=worker_events,
                )

    def test_usb_worker_events_derive_recovery_and_verify_ejection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            worker_events = root / "events.ndjson"
            job_id = "11111111-1111-4111-8111-111111111111"
            events = [
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "type": "job.started",
                    "job_id": job_id,
                    "sequence": 0,
                    "payload": {"operation": "convert_source"},
                },
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "type": "job.cancelled",
                    "job_id": job_id,
                    "sequence": 1,
                    "payload": {},
                },
            ]
            worker_events.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
            answers = collect_operator_answers(
                case_id="usb-bluray-makemkv",
                environment_class="dedicated-hardware",
                operations=FakeOperations([(PublicUSBDevice("1234", "5678"),), ()]),
                prompter=FakePrompter({"usb-ejection": "ready"}),
                clock=clock(),
                worker_events=worker_events,
                cleanup_paths=[root / "cleanup"],
            )
            self.assertEqual(answers["observations"]["cancellation"], "recovered")
            self.assertEqual(answers["observations"]["ejection"], "ejected")
            self.assertEqual(answers["reason_code"], "all-assertions-passed")

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
                    "sequence": 0,
                    "payload": {"operation": "convert_source"},
                },
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "type": "job.decision_required",
                    "job_id": job_id,
                    "sequence": 1,
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

    def test_invalid_release_receipt_is_reported_without_path_details(self) -> None:
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
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with self.assertRaises(OperatorCollectionError) as raised:
                write_validated_answers(
                    answers,
                    repo=REPO_ROOT,
                    release_receipt_path=REPO_ROOT / "docs/private-missing-receipt.json",
                    output_path=root / "answers.json",
                    prompter=FakePrompter({"confirm-write": "write"}),
                )
            self.assertNotIn("/Users/", str(raised.exception))

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
