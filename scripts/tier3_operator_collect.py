from __future__ import annotations

import argparse
import json
import platform
import plistlib
import re
import subprocess
import sys

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from bd_to_avp.modules.config import resolve_makemkvcon_path
from bd_to_avp.worker.protocol import PROTOCOL_VERSION, WorkerEventType, ZERO_JOB_ID
from scripts.tier3_operator_receipt import (
    CASE_CONTRACTS,
    OperatorReceiptError,
    validate_operator_answers,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
MAX_PROBE_BYTES = 4 * 1024 * 1024
MAX_WORKER_EVENTS = 2_000
PUBLIC_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._+-]{0,63}$")
USB_ID_PATTERN = re.compile(r"^(?:0x)?([0-9a-fA-F]{4})(?:\b|$)")
VERSION_PATTERN = re.compile(r"\b(?:MakeMKV\s+v?)?(\d+(?:\.\d+){1,3})\b", re.IGNORECASE)
MACOS_VERSION_PATTERN = re.compile(r"^[0-9]{1,2}(?:\.[0-9]{1,2}){1,2}$")
MACOS_BUILD_PATTERN = re.compile(r"^[0-9]{2}[A-Z][0-9A-Za-z]{1,12}$")
OPTICAL_MARKERS = ("blu-ray", "bluray", "bd-rom", "bd-re", "bd-r", "dvd", "optical")
SKIP_REASONS = ("environment-unavailable", "hardware-unavailable", "operator-cancelled")


class OperatorCollectionError(RuntimeError):
    pass


class PromptCancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class PublicUSBDevice:
    vendor_id: str
    product_id: str
    transport: str = "usb"


@dataclass(frozen=True)
class PromptSpec:
    identifier: str
    category: str
    message: str
    choices: tuple[str, ...]


PROMPT_SPECS = {
    "usb-cancellation": PromptSpec(
        identifier="usb-cancellation",
        category="physical-action",
        message="Cancel the active physical-drive operation, then record whether the app recovered cleanly.",
        choices=("recovered", "failed", "cancel"),
    ),
    "usb-ejection": PromptSpec(
        identifier="usb-ejection",
        category="physical-action",
        message="Eject or disconnect the tested drive, then continue so removal can be detected.",
        choices=("ready", "failed", "cancel"),
    ),
    "protected-run": PromptSpec(
        identifier="protected-run",
        category="physical-action",
        message="Complete or cancel the protected-media run, then continue so its worker events can be inspected.",
        choices=("ready", "cancel"),
    ),
    "protected-cleanup": PromptSpec(
        identifier="protected-cleanup",
        category="physical-action",
        message="Remove only the owned temporary conversion files, then continue so cleanup can be verified.",
        choices=("ready", "cancel"),
    ),
    "vision-transfer": PromptSpec(
        identifier="vision-transfer",
        category="physical-action",
        message="Transfer the candidate to Vision Pro, then record the bounded outcome.",
        choices=("completed", "failed", "cancel"),
    ),
    "vision-stereo": PromptSpec(
        identifier="vision-stereo",
        category="subjective-judgment",
        message="Start playback and record whether distinct left/right stereo presentation is visible.",
        choices=("started", "failed", "cancel"),
    ),
    "vision-spatial": PromptSpec(
        identifier="vision-spatial",
        category="subjective-judgment",
        message="Record whether the presentation appears spatial rather than flat or duplicated.",
        choices=("verified", "failed", "cancel"),
    ),
    "vision-playback": PromptSpec(
        identifier="vision-playback",
        category="subjective-judgment",
        message="Finish the playback sample and record the bounded outcome.",
        choices=("completed", "interrupted", "failed", "cancel"),
    ),
    "confirm-write": PromptSpec(
        identifier="confirm-write",
        category="physical-action",
        message="Write the validated public-safe answers shown in the preview?",
        choices=("write", "cancel"),
    ),
}


class Prompter(Protocol):
    def choose(self, spec: PromptSpec) -> str: ...


class MachineOperations(Protocol):
    def environment(self, environment_class: str) -> Mapping[str, str]: ...

    def usb_devices(self) -> tuple[PublicUSBDevice, ...]: ...

    def makemkv_version(self) -> str | None: ...


class ConsolePrompter:
    def choose(self, spec: PromptSpec) -> str:
        print(f"\n{spec.message}", file=sys.stderr)
        for index, choice in enumerate(spec.choices, start=1):
            print(f"  {index}. {choice}", file=sys.stderr)
        while True:
            response = input("Choice: ").strip().lower()
            if response.isdigit() and 1 <= int(response) <= len(spec.choices):
                return spec.choices[int(response) - 1]
            if response in spec.choices:
                return response
            print("Choose one of the listed bounded outcomes.", file=sys.stderr)


def _safe_identity(value: str, description: str) -> str:
    normalized = value.strip().lower()
    if PUBLIC_ID_PATTERN.fullmatch(normalized) is None:
        raise OperatorCollectionError(f"{description} is not a bounded public identifier.")
    return normalized


def _usb_id(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    match = USB_ID_PATTERN.match(value.strip())
    return match.group(1).lower() if match else None


def detect_public_usb_devices(payload: object) -> tuple[PublicUSBDevice, ...]:
    devices: list[PublicUSBDevice] = []

    def visit(value: object) -> None:
        if isinstance(value, Mapping):
            vendor_id = _usb_id(value.get("vendor_id"))
            product_id = _usb_id(value.get("product_id"))
            label_values = [
                item
                for key, item in value.items()
                if isinstance(item, str) and str(key).lower() in {"_name", "device_type", "media_type", "product_name"}
            ]
            label = " ".join(label_values).lower()
            has_media = any(str(key).lower() == "media" for key in value)
            if vendor_id and product_id and (has_media or any(marker in label for marker in OPTICAL_MARKERS)):
                devices.append(PublicUSBDevice(vendor_id=vendor_id, product_id=product_id))
            for item in value.values():
                visit(item)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            for item in value:
                visit(item)

    visit(payload)
    return tuple(sorted(devices, key=lambda item: (item.vendor_id, item.product_id)))


def _run_stdout(command: Sequence[str], timeout: int) -> bytes:
    try:
        result = subprocess.run(command, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise OperatorCollectionError("A local machine probe could not be completed.") from error
    if result.returncode != 0 or len(result.stdout) > MAX_PROBE_BYTES:
        raise OperatorCollectionError("A local machine probe returned no bounded result.")
    return result.stdout


class MacOSOperations:
    def __init__(
        self,
        *,
        runner: Callable[[Sequence[str], int], bytes] = _run_stdout,
        architecture: Callable[[], str] = platform.machine,
        makemkv_path: Callable[[], Path] = resolve_makemkvcon_path,
    ) -> None:
        self._runner = runner
        self._architecture = architecture
        self._makemkv_path = makemkv_path

    def environment(self, environment_class: str) -> Mapping[str, str]:
        architecture = self._architecture().strip().lower()
        if architecture != "arm64":
            raise OperatorCollectionError("Operator qualification requires an arm64 Mac.")
        version = self._runner(("/usr/bin/sw_vers", "-productVersion"), 15).decode().strip()
        build = self._runner(("/usr/bin/sw_vers", "-buildVersion"), 15).decode().strip()
        return {
            "architecture": architecture,
            "environment_class": _safe_identity(environment_class, "environment class"),
            "macos_build": build,
            "macos_version": version,
        }

    def usb_devices(self) -> tuple[PublicUSBDevice, ...]:
        raw = self._runner(("/usr/sbin/system_profiler", "SPUSBDataType", "-json", "-detailLevel", "mini"), 60)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as error:
            raise OperatorCollectionError("USB discovery returned invalid structured data.") from error
        return detect_public_usb_devices(payload)

    def makemkv_version(self) -> str | None:
        executable = self._makemkv_path()
        if not executable.is_file():
            return None
        bundle = next((parent for parent in executable.parents if parent.suffix == ".app"), None)
        if bundle is not None:
            try:
                info = plistlib.loads((bundle / "Contents" / "Info.plist").read_bytes())
            except (OSError, plistlib.InvalidFileException):
                info = {}
            version = info.get("CFBundleShortVersionString")
            if isinstance(version, str) and VERSION_PATTERN.fullmatch(version.strip()):
                return version.strip().lower()
        try:
            output = self._runner((str(executable), "--version"), 15).decode(errors="replace")
        except OperatorCollectionError:
            return None
        match = VERSION_PATTERN.search(output)
        return match.group(1).lower() if match else None


def _read_worker_events(path: Path) -> tuple[Mapping[str, Any], ...]:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise OperatorCollectionError("The native-worker event stream could not be read.") from error
    if not raw or len(raw) > MAX_PROBE_BYTES:
        raise OperatorCollectionError("The native-worker event stream is empty or exceeds the bounded size.")
    events: list[Mapping[str, Any]] = []
    job_ids: set[str] = set()
    for line in raw.splitlines():
        if not line.strip():
            continue
        if len(events) >= MAX_WORKER_EVENTS:
            raise OperatorCollectionError("The native-worker event stream contains too many events.")
        try:
            event = json.loads(line)
        except json.JSONDecodeError as error:
            raise OperatorCollectionError("The native-worker event stream contains invalid JSON.") from error
        if not isinstance(event, Mapping) or event.get("protocol_version") != PROTOCOL_VERSION:
            raise OperatorCollectionError("The native-worker event stream does not match the current protocol.")
        job_id = event.get("job_id")
        if isinstance(job_id, str) and job_id != ZERO_JOB_ID:
            job_ids.add(job_id)
        events.append(event)
    if not events or len(job_ids) != 1:
        raise OperatorCollectionError("The native-worker event stream must describe exactly one job.")
    return tuple(events)


def _terminal_event(events: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    terminal_types = {event_type.value for event_type in WorkerEventType if event_type.is_terminal}
    terminals = [event for event in events if event.get("type") in terminal_types]
    if len(terminals) != 1 or terminals[0] is not events[-1]:
        raise OperatorCollectionError("The native-worker event stream must end in one terminal event.")
    return terminals[0]


def _paths_absent(paths: Sequence[Path]) -> bool:
    return bool(paths) and all(not path.exists() for path in paths)


def derive_protected_conversion_observations(
    worker_events: Path,
    cleanup_paths: Sequence[Path],
) -> Mapping[str, str]:
    events = _read_worker_events(worker_events)
    terminal = _terminal_event(events)
    started = any(
        event.get("type") == WorkerEventType.JOB_STARTED.value
        and isinstance(event.get("payload"), Mapping)
        and event["payload"].get("operation") == "convert_source"
        for event in events
    )
    terminal_type = terminal.get("type")
    if terminal_type == WorkerEventType.JOB_COMPLETED.value:
        conversion = "completed"
    elif terminal_type == WorkerEventType.JOB_CANCELLED.value:
        conversion = "cancelled"
    elif terminal_type == WorkerEventType.JOB_DECISION_REQUIRED.value:
        raise OperatorCollectionError("The native-worker job requires a decision before qualification can continue.")
    else:
        conversion = "failed"

    output = "not-produced" if conversion == "cancelled" else "failed"
    if conversion == "completed":
        payload = terminal.get("payload")
        result = payload.get("conversion_result") if isinstance(payload, Mapping) else None
        output_path = result.get("output_path") if isinstance(result, Mapping) else None
        size_bytes = result.get("size_bytes") if isinstance(result, Mapping) else None
        if isinstance(output_path, str) and isinstance(size_bytes, int) and size_bytes > 0:
            try:
                if Path(output_path).is_file() and Path(output_path).stat().st_size == size_bytes:
                    output = "verified"
            except OSError:
                output = "failed"

    cleanup_absent = _paths_absent(cleanup_paths)
    recovery = "clean" if conversion == "completed" and cleanup_absent else "recovered" if cleanup_absent else "failed"
    return {
        "conversion": conversion,
        "output": output,
        "protected_source": "opened" if started else "failed",
        "recovery": recovery,
    }


def _prompt(prompter: Prompter, identifier: str) -> str:
    spec = PROMPT_SPECS[identifier]
    response = prompter.choose(spec)
    if response not in spec.choices:
        raise OperatorCollectionError(f"Prompt {identifier} returned an unsupported outcome.")
    if response == "cancel":
        raise PromptCancelled
    return response


def _usb_hardware(
    operations: MachineOperations,
    *,
    vendor_id: str | None,
    product_id: str | None,
    makemkv_version: str | None,
) -> tuple[Mapping[str, Any], PublicUSBDevice]:
    if vendor_id is not None or product_id is not None:
        if vendor_id is None or product_id is None:
            raise OperatorCollectionError("USB vendor and product overrides must be supplied together.")
        normalized_vendor = _usb_id(vendor_id)
        normalized_product = _usb_id(product_id)
        if normalized_vendor is None or normalized_product is None:
            raise OperatorCollectionError("USB vendor and product IDs must be four hexadecimal digits.")
        device = PublicUSBDevice(normalized_vendor, normalized_product)
    else:
        devices = operations.usb_devices()
        if len(devices) == 1:
            device = devices[0]
        elif not devices:
            raise OperatorCollectionError("No public USB optical-drive identity was detected.")
        else:
            raise OperatorCollectionError(
                "Multiple USB optical drives were detected; leave only the tested drive connected."
            )
    version = makemkv_version or operations.makemkv_version()
    if version is None:
        raise OperatorCollectionError("A bounded MakeMKV version could not be detected.")
    normalized_version = _safe_identity(version, "MakeMKV version")
    return (
        {
            "class": "usb-bluray-drive",
            "identity": {
                "makemkv_version": normalized_version,
                "product_id": device.product_id,
                "transport": device.transport,
                "vendor_id": device.vendor_id,
            },
        },
        device,
    )


def _vision_hardware(
    model_family: str | None, chip_family: str | None, visionos_major: str | None
) -> Mapping[str, Any]:
    if model_family is None or chip_family is None or visionos_major is None:
        raise OperatorCollectionError("Vision Pro public model, chip, and visionOS major identities are required.")
    return {
        "class": "vision-pro",
        "identity": {
            "chip_family": _safe_identity(chip_family, "Vision Pro chip family"),
            "model_family": _safe_identity(model_family, "Vision Pro model family"),
            "visionos_major": _safe_identity(visionos_major, "visionOS major"),
        },
    }


def _environment(
    operations: MachineOperations,
    environment_class: str,
    *,
    architecture: str | None,
    macos_version: str | None,
    macos_build: str | None,
) -> Mapping[str, str]:
    overrides = (architecture, macos_version, macos_build)
    if any(value is not None for value in overrides):
        if not all(value is not None for value in overrides):
            raise OperatorCollectionError(
                "Architecture, macOS version, and macOS build overrides must be supplied together."
            )
        normalized_architecture = (architecture or "").strip().lower()
        normalized_version = (macos_version or "").strip()
        normalized_build = (macos_build or "").strip()
        if normalized_architecture != "arm64":
            raise OperatorCollectionError("Operator qualification requires an arm64 Mac identity.")
        if MACOS_VERSION_PATTERN.fullmatch(normalized_version) is None:
            raise OperatorCollectionError("The macOS version override is not a bounded public version.")
        if MACOS_BUILD_PATTERN.fullmatch(normalized_build) is None:
            raise OperatorCollectionError("The macOS build override is not a bounded public build.")
        return {
            "architecture": normalized_architecture,
            "environment_class": _safe_identity(environment_class, "environment class"),
            "macos_build": normalized_build,
            "macos_version": normalized_version,
        }
    return operations.environment(environment_class)


def _timestamp(clock: Callable[[], datetime]) -> str:
    value = clock()
    if value.tzinfo is None:
        raise OperatorCollectionError("The collection clock must be timezone-aware.")
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _completed_reason(case_id: str, observations: Mapping[str, str]) -> str:
    contract = CASE_CONTRACTS[case_id]
    passed = all(observations[field] == expected for field, expected in contract["assertions"].values())
    return "all-assertions-passed" if passed else "operator-assertion-failed"


def collect_operator_answers(
    *,
    case_id: str,
    environment_class: str,
    operations: MachineOperations,
    prompter: Prompter,
    clock: Callable[[], datetime],
    worker_events: Path | None = None,
    cleanup_paths: Sequence[Path] = (),
    skip_reason: str | None = None,
    usb_vendor_id: str | None = None,
    usb_product_id: str | None = None,
    makemkv_version: str | None = None,
    vision_model_family: str | None = None,
    vision_chip_family: str | None = None,
    visionos_major: str | None = None,
    architecture: str | None = None,
    macos_version: str | None = None,
    macos_build: str | None = None,
) -> Mapping[str, Any]:
    if case_id not in CASE_CONTRACTS:
        raise OperatorCollectionError(f"Unsupported operator qualification case: {case_id}")
    started_at = _timestamp(clock)
    environment = _environment(
        operations,
        environment_class,
        architecture=architecture,
        macos_version=macos_version,
        macos_build=macos_build,
    )
    tested_device: PublicUSBDevice | None = None
    if case_id == "vision-pro-physical-playback":
        hardware = _vision_hardware(vision_model_family, vision_chip_family, visionos_major)
    else:
        hardware, tested_device = _usb_hardware(
            operations,
            vendor_id=usb_vendor_id,
            product_id=usb_product_id,
            makemkv_version=makemkv_version,
        )

    if skip_reason is not None:
        if skip_reason not in SKIP_REASONS:
            raise OperatorCollectionError("Skip reason is not supported.")
        disposition = "skipped"
        reason_code = skip_reason
        observations: Mapping[str, str] = {}
        cleanup_status = "not-applicable" if case_id != "protected-real-media-conversion" else "restored"
    else:
        try:
            if case_id == "usb-bluray-makemkv":
                if worker_events is None:
                    cancellation = _prompt(prompter, "usb-cancellation")
                else:
                    cancellation_terminal = _terminal_event(_read_worker_events(worker_events)).get("type")
                    cancellation = (
                        "recovered"
                        if cancellation_terminal == WorkerEventType.JOB_CANCELLED.value and _paths_absent(cleanup_paths)
                        else "failed"
                    )
                    if not _paths_absent(cleanup_paths):
                        _prompt(prompter, "protected-cleanup")
                        if not _paths_absent(cleanup_paths):
                            raise OperatorCollectionError("Owned cancellation cleanup could not be verified.")
                ejection_action = _prompt(prompter, "usb-ejection")
                ejection = "failed"
                if ejection_action == "ready" and tested_device not in operations.usb_devices():
                    ejection = "ejected"
                observations = {
                    "cancellation": cancellation,
                    "drive_discovery": "detected",
                    "ejection": ejection,
                    "makemkv": "installed",
                }
                cleanup_status = "recovered" if cancellation == "recovered" else "not-applicable"
            elif case_id == "protected-real-media-conversion":
                if worker_events is None or not cleanup_paths:
                    raise OperatorCollectionError(
                        "Protected conversion collection requires native-worker events and owned cleanup paths."
                    )
                _prompt(prompter, "protected-run")
                observations = derive_protected_conversion_observations(worker_events, cleanup_paths)
                cleanup_status = "restored" if observations["recovery"] == "clean" else "recovered"
                if observations["recovery"] == "failed":
                    _prompt(prompter, "protected-cleanup")
                    if not _paths_absent(cleanup_paths):
                        raise OperatorCollectionError("Owned conversion cleanup could not be verified.")
            else:
                transfer = _prompt(prompter, "vision-transfer")
                stereo_playback = _prompt(prompter, "vision-stereo")
                spatial_presentation = _prompt(prompter, "vision-spatial")
                playback = _prompt(prompter, "vision-playback")
                observations = {
                    "playback": playback,
                    "spatial_presentation": spatial_presentation,
                    "stereo_playback": stereo_playback,
                    "transfer": transfer,
                }
                cleanup_status = "not-applicable"
            disposition = "completed"
            reason_code = _completed_reason(case_id, observations)
        except PromptCancelled:
            disposition = "skipped"
            reason_code = "operator-cancelled"
            observations = {}
            cleanup_status = "not-applicable" if case_id != "protected-real-media-conversion" else "restored"

    return {
        "case_id": case_id,
        "cleanup_status": cleanup_status,
        "completed_at": _timestamp(clock),
        "disposition": disposition,
        "environment": dict(environment),
        "hardware": dict(hardware),
        "observations": dict(observations),
        "reason_code": reason_code,
        "schema_version": 1,
        "started_at": started_at,
    }


def write_validated_answers(
    answers: Mapping[str, Any],
    *,
    repo: Path,
    release_receipt_path: Path,
    output_path: Path,
    prompter: Prompter,
) -> Mapping[str, Any] | None:
    try:
        preview = validate_operator_answers(answers, repo=repo, release_receipt_path=release_receipt_path)
    except OperatorReceiptError as error:
        raise OperatorCollectionError(f"Collected answers are invalid: {error}") from error
    public_preview = {"answers": answers, **preview}
    print(json.dumps(public_preview, indent=2, sort_keys=True))
    try:
        _prompt(prompter, "confirm-write")
    except PromptCancelled:
        return None
    content = json.dumps(answers, indent=2, sort_keys=True) + "\n"
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("x", encoding="utf-8") as output:
            output.write(content)
    except FileExistsError as error:
        raise OperatorCollectionError("The answers output already exists.") from error
    except OSError as error:
        raise OperatorCollectionError("The answers output could not be written.") from error
    return preview


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect bounded privacy-safe Tier 3 operator answers.")
    parser.add_argument("--case-id", choices=tuple(CASE_CONTRACTS), required=True)
    parser.add_argument("--release-receipt", type=Path, required=True)
    parser.add_argument("--output-answers", type=Path, required=True)
    parser.add_argument(
        "--environment-class",
        choices=("dedicated-hardware", "restorable-location"),
        required=True,
    )
    parser.add_argument("--worker-events", type=Path)
    parser.add_argument("--cleanup-path", type=Path, action="append", default=[])
    parser.add_argument("--skip-reason", choices=SKIP_REASONS)
    parser.add_argument("--usb-vendor-id")
    parser.add_argument("--usb-product-id")
    parser.add_argument("--makemkv-version")
    parser.add_argument("--vision-model-family")
    parser.add_argument("--vision-chip-family")
    parser.add_argument("--visionos-major")
    parser.add_argument("--architecture")
    parser.add_argument("--macos-version")
    parser.add_argument("--macos-build")
    parser.add_argument("--repo", type=Path, default=REPO_ROOT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    prompter = ConsolePrompter()
    try:
        answers = collect_operator_answers(
            case_id=args.case_id,
            environment_class=args.environment_class,
            operations=MacOSOperations(),
            prompter=prompter,
            clock=lambda: datetime.now(UTC),
            worker_events=args.worker_events,
            cleanup_paths=args.cleanup_path,
            skip_reason=args.skip_reason,
            usb_vendor_id=args.usb_vendor_id,
            usb_product_id=args.usb_product_id,
            makemkv_version=args.makemkv_version,
            vision_model_family=args.vision_model_family,
            vision_chip_family=args.vision_chip_family,
            visionos_major=args.visionos_major,
            architecture=args.architecture,
            macos_version=args.macos_version,
            macos_build=args.macos_build,
        )
        preview = write_validated_answers(
            answers,
            repo=args.repo.resolve(),
            release_receipt_path=args.release_receipt,
            output_path=args.output_answers,
            prompter=prompter,
        )
        if preview is None:
            print("Operator collection cancelled; no public state was written.", file=sys.stderr)
            return 3
        return 0
    except (EOFError, KeyboardInterrupt):
        print("Operator collection cancelled; no public state was written.", file=sys.stderr)
        return 3
    except OperatorCollectionError as error:
        print(f"tier3 operator collection error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
