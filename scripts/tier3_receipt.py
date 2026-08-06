from __future__ import annotations

import argparse
import hashlib
import json
import re

from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

from scripts.release_receipt import ReleaseReceiptError, validate_receipt as validate_release_receipt


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY_PATH = REPO_ROOT / "docs" / "qualification" / "release-qualification-policy-v1.json"
RECEIPT_TYPE = "bd-to-avp-tier3-qualification"
SCHEMA_VERSION = 1
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SAFE_IDENTITY_VALUE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
MACOS_VERSION_PATTERN = re.compile(r"^(?P<major>[0-9]{1,2})(?:\.[0-9]{1,2}){1,2}$")
MACOS_BUILD_PATTERN = re.compile(r"^[0-9]{2}[A-Z][0-9A-Za-z]{1,12}$")
RESULT_STATUSES = {"passed", "failed", "skipped", "not_applicable"}
ASSERTION_STATUSES = {"passed", "failed", "not_run"}
FORBIDDEN_IDENTITY_FIELDS = {
    "disc",
    "hostname",
    "media",
    "path",
    "serial",
    "title",
    "token",
    "username",
    "volume",
}


class Tier3ReceiptError(RuntimeError):
    pass


ReferenceContent = Callable[[str], bytes]


def _mapping(value: object, description: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise Tier3ReceiptError(f"{description} must be a JSON object.")
    return cast(Mapping[str, Any], value)


def _sequence(value: object, description: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise Tier3ReceiptError(f"{description} must be a JSON array.")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], description: str) -> None:
    actual = set(value)
    if actual != expected:
        raise Tier3ReceiptError(
            f"{description} keys changed; missing={sorted(expected - actual)}, extra={sorted(actual - expected)}."
        )


def _string(value: object, description: str) -> str:
    if not isinstance(value, str) or not value:
        raise Tier3ReceiptError(f"{description} must be a non-empty string.")
    return value


def _strings(value: object, description: str) -> tuple[str, ...]:
    items = _sequence(value, description)
    if not all(isinstance(item, str) and item for item in items):
        raise Tier3ReceiptError(f"{description} must contain non-empty strings.")
    return tuple(cast(str, item) for item in items)


def _boolean(value: object, description: str) -> bool:
    if not isinstance(value, bool):
        raise Tier3ReceiptError(f"{description} must be a boolean.")
    return value


def _positive_integer(value: object, description: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise Tier3ReceiptError(f"{description} must be a positive integer.")
    return value


def _sha256(value: object, description: str) -> str:
    digest = _string(value, description)
    if SHA256_PATTERN.fullmatch(digest) is None:
        raise Tier3ReceiptError(f"{description} must be a lowercase SHA-256 digest.")
    return digest


def _optional_sha256(value: object, description: str) -> str | None:
    if value is None:
        return None
    return _sha256(value, description)


def _relative_reference(value: object, description: str) -> str:
    reference = _string(value, description)
    path = Path(reference)
    if path.is_absolute() or ".." in path.parts or reference.startswith("./"):
        raise Tier3ReceiptError(f"{description} must be a repository-relative path.")
    return reference


def _timestamp(value: object, description: str) -> datetime:
    text = _string(value, description)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise Tier3ReceiptError(f"{description} must be an ISO-8601 timestamp.") from error
    if parsed.tzinfo is None:
        raise Tier3ReceiptError(f"{description} must include a timezone.")
    return parsed.astimezone(timezone.utc)


def _date(value: object, description: str) -> date:
    text = _string(value, description)
    try:
        return date.fromisoformat(text)
    except ValueError as error:
        raise Tier3ReceiptError(f"{description} must be an ISO-8601 date.") from error


def canonical_payload_bytes(receipt: Mapping[str, Any]) -> bytes:
    payload = dict(receipt)
    payload.pop("receipt_sha256", None)
    return (json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


def receipt_sha256(receipt: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_payload_bytes(receipt)).hexdigest()


def _artifact_digests(release_receipt: Mapping[str, Any]) -> dict[str, str]:
    artifacts: dict[str, str] = {}
    for raw_artifact in _sequence(release_receipt.get("artifacts"), "release receipt artifacts"):
        artifact = _mapping(raw_artifact, "release receipt artifact")
        kind = _string(artifact.get("kind"), "release receipt artifact kind")
        artifacts[kind] = _sha256(artifact.get("sha256"), f"release receipt {kind} sha256")
    if set(artifacts) != {"appcast", "checksum", "dmg"}:
        raise Tier3ReceiptError("Release receipt must contain appcast, checksum, and DMG artifacts.")
    return dict(sorted(artifacts.items()))


def _release_identity(
    release_receipt: Mapping[str, Any],
    *,
    reference: str,
    file_sha256: str,
) -> dict[str, Any]:
    try:
        validate_release_receipt(release_receipt)
    except ReleaseReceiptError as error:
        raise Tier3ReceiptError(f"Referenced release receipt is invalid: {error}") from error
    release = _mapping(release_receipt.get("release"), "release receipt release")
    return {
        "artifact_sha256": _artifact_digests(release_receipt),
        "release_receipt": {
            "file_sha256": _sha256(file_sha256, "release receipt file_sha256"),
            "receipt_sha256": _sha256(release_receipt.get("receipt_sha256"), "release receipt receipt_sha256"),
            "reference": _relative_reference(reference, "release receipt reference"),
        },
        "release_route": _string(release_receipt.get("release_route"), "release receipt release_route"),
        "release_tag": _string(release.get("tag"), "release receipt release tag"),
        "signed_app_tree_sha256": _sha256(
            release_receipt.get("signed_app_tree_sha256"),
            "release receipt signed_app_tree_sha256",
        ),
        "source_sha": _string(release_receipt.get("source_sha"), "release receipt source_sha"),
        "versions": dict(_mapping(release_receipt.get("versions"), "release receipt versions")),
    }


def _safe_identity(value: object, description: str) -> str:
    text = _string(value, description)
    if SAFE_IDENTITY_VALUE_PATTERN.fullmatch(text) is None:
        raise Tier3ReceiptError(
            f"{description} must be a bounded public-safe identifier without spaces, paths, or free-form text."
        )
    return text


def _case_metadata(case: Mapping[str, Any]) -> tuple[str, str, Mapping[str, Any], Mapping[str, Any]]:
    case_id = _string(case.get("id"), "Tier 3 case id")
    if case.get("tier") != 3:
        raise Tier3ReceiptError(f"Case {case_id!r} is not Tier 3.")
    lane = _string(case.get("automation_lane"), f"case {case_id!r} automation_lane")
    execution_mode = _string(case.get("execution_mode"), f"case {case_id!r} execution_mode")
    if execution_mode not in {"automated", "operator_assisted"}:
        raise Tier3ReceiptError(f"Case {case_id!r} execution_mode is unsupported.")
    environment = _mapping(case.get("environment"), f"case {case_id!r} environment")
    hardware = _mapping(case.get("hardware"), f"case {case_id!r} hardware")
    return lane, execution_mode, environment, hardware


def build_receipt(
    facts: Mapping[str, Any],
    *,
    policy_id: str,
    case: Mapping[str, Any],
    release_receipt: Mapping[str, Any],
) -> dict[str, Any]:
    lane, execution_mode, _, _ = _case_metadata(case)
    completed_at = _timestamp(facts.get("completed_at"), "completed_at")
    expiry = _mapping(case.get("evidence_expiry"), f"case {case['id']!r} evidence_expiry")
    max_age_days = _positive_integer(expiry.get("max_age_days"), "evidence_expiry.max_age_days")
    receipt: dict[str, Any] = {
        "assertions": [
            {"id": assertion_id, "status": assertion_status}
            for assertion_id, assertion_status in sorted(
                _mapping(facts.get("assertions"), "assertions").items(),
                key=lambda item: str(item[0]),
            )
        ],
        "automation_lane": lane,
        "cadence": {
            "expires_on": (completed_at.date() + timedelta(days=max_age_days)).isoformat(),
            "expiry_basis": _string(expiry.get("basis"), "evidence_expiry.basis"),
            "first_rc": _boolean(_mapping(case["cadence"], "case cadence").get("first_rc"), "cadence.first_rc"),
            "max_age_days": max_age_days,
            "stable_candidate": _boolean(
                _mapping(case["cadence"], "case cadence").get("stable_candidate"),
                "cadence.stable_candidate",
            ),
        },
        "case_id": _string(case.get("id"), "case id"),
        "cleanup": dict(_mapping(facts.get("cleanup"), "cleanup")),
        "environment": dict(_mapping(facts.get("environment"), "environment")),
        "evidence": [dict(_mapping(item, "evidence item")) for item in _sequence(facts.get("evidence"), "evidence")],
        "evidence_source": _string(facts.get("evidence_source"), "evidence_source"),
        "execution_mode": execution_mode,
        "hardware": dict(_mapping(facts.get("hardware"), "hardware")),
        "policy_id": _string(policy_id, "policy_id"),
        "receipt_type": RECEIPT_TYPE,
        "release_identity": _release_identity(
            release_receipt,
            reference=_relative_reference(facts.get("release_receipt_reference"), "release_receipt_reference"),
            file_sha256=_sha256(facts.get("release_receipt_file_sha256"), "release_receipt_file_sha256"),
        ),
        "result": dict(_mapping(facts.get("result"), "result")),
        "schema_version": SCHEMA_VERSION,
        "timestamps": {
            "completed_at": _string(facts.get("completed_at"), "completed_at"),
            "started_at": _string(facts.get("started_at"), "started_at"),
        },
    }
    receipt["receipt_sha256"] = receipt_sha256(receipt)
    return receipt


def _validate_environment(environment: Mapping[str, Any], case_environment: Mapping[str, Any]) -> None:
    required_fields = set(_strings(case_environment.get("identity_fields"), "environment identity_fields"))
    _exact_keys(environment, required_fields, "Tier 3 environment")
    environment_class = _safe_identity(environment.get("environment_class"), "environment.environment_class")
    if environment_class not in set(_strings(case_environment.get("classes"), "environment classes")):
        raise Tier3ReceiptError("Tier 3 environment class is not allowed by the case.")
    architecture = _safe_identity(environment.get("architecture"), "environment.architecture")
    if architecture not in set(_strings(case_environment.get("architectures"), "environment architectures")):
        raise Tier3ReceiptError("Tier 3 environment architecture is not allowed by the case.")
    macos_version = _safe_identity(environment.get("macos_version"), "environment.macos_version")
    match = MACOS_VERSION_PATTERN.fullmatch(macos_version)
    if match is None:
        raise Tier3ReceiptError("environment.macos_version must be a public major.minor version.")
    allowed_majors = set(_sequence(case_environment.get("macos_major_versions"), "environment macOS major versions"))
    if int(match.group("major")) not in allowed_majors:
        raise Tier3ReceiptError("Tier 3 environment macOS major version is not allowed by the case.")
    macos_build = _safe_identity(environment.get("macos_build"), "environment.macos_build")
    if MACOS_BUILD_PATTERN.fullmatch(macos_build) is None:
        raise Tier3ReceiptError("environment.macos_build must be a public Apple build identifier.")


def _validate_hardware(hardware: Mapping[str, Any], case_hardware: Mapping[str, Any]) -> None:
    _exact_keys(hardware, {"class", "identity"}, "Tier 3 hardware")
    required = _boolean(case_hardware.get("required"), "case hardware.required")
    hardware_class = _safe_identity(hardware.get("class"), "hardware.class")
    identity = _mapping(hardware.get("identity"), "hardware.identity")
    if not required:
        if hardware_class != "none" or identity:
            raise Tier3ReceiptError("A case without required hardware must record class 'none' and empty identity.")
        return
    if hardware_class not in set(_strings(case_hardware.get("classes"), "case hardware classes")):
        raise Tier3ReceiptError("Tier 3 hardware class is not allowed by the case.")
    required_fields = set(_strings(case_hardware.get("identity_fields"), "case hardware identity_fields"))
    _exact_keys(identity, required_fields, "Tier 3 hardware identity")
    for field, raw_value in identity.items():
        lowered = str(field).lower()
        if any(forbidden in lowered for forbidden in FORBIDDEN_IDENTITY_FIELDS):
            raise Tier3ReceiptError(f"Tier 3 hardware identity field {field!r} is not public-safe.")
        _safe_identity(raw_value, f"hardware.identity.{field}")


def _validate_assertions(
    assertions: Sequence[Any],
    case: Mapping[str, Any],
    result_status: str,
) -> None:
    required_ids = set(_strings(case.get("required_assertions"), "case required_assertions"))
    observed: dict[str, str] = {}
    for index, raw_assertion in enumerate(assertions):
        assertion = _mapping(raw_assertion, f"assertion {index}")
        _exact_keys(assertion, {"id", "status"}, f"assertion {index}")
        assertion_id = _safe_identity(assertion.get("id"), f"assertion {index} id")
        status = _string(assertion.get("status"), f"assertion {index} status")
        if status not in ASSERTION_STATUSES:
            raise Tier3ReceiptError(f"Tier 3 assertion {assertion_id!r} has unsupported status {status!r}.")
        if assertion_id in observed:
            raise Tier3ReceiptError(f"Tier 3 assertion ID {assertion_id!r} is duplicated.")
        observed[assertion_id] = status
    if set(observed) != required_ids:
        raise Tier3ReceiptError("Tier 3 receipt assertions do not match the case's required assertions.")
    if result_status == "passed" and set(observed.values()) != {"passed"}:
        raise Tier3ReceiptError("A passed Tier 3 receipt requires every assertion to pass.")
    if result_status == "failed" and "failed" not in observed.values():
        raise Tier3ReceiptError("A failed Tier 3 receipt requires at least one failed assertion.")
    if result_status in {"skipped", "not_applicable"} and set(observed.values()) != {"not_run"}:
        raise Tier3ReceiptError("Skipped or not-applicable Tier 3 receipts must mark every assertion not_run.")


def validate_receipt(
    receipt: Mapping[str, Any],
    *,
    policy_id: str,
    case: Mapping[str, Any],
    reference_content: ReferenceContent,
) -> None:
    _exact_keys(
        receipt,
        {
            "assertions",
            "automation_lane",
            "cadence",
            "case_id",
            "cleanup",
            "environment",
            "evidence",
            "evidence_source",
            "execution_mode",
            "hardware",
            "policy_id",
            "receipt_sha256",
            "receipt_type",
            "release_identity",
            "result",
            "schema_version",
            "timestamps",
        },
        "Tier 3 receipt",
    )
    if receipt.get("schema_version") != SCHEMA_VERSION or receipt.get("receipt_type") != RECEIPT_TYPE:
        raise Tier3ReceiptError("Unsupported Tier 3 receipt schema or type.")
    if receipt.get("policy_id") != policy_id:
        raise Tier3ReceiptError("Tier 3 receipt policy_id does not match the active policy.")
    lane, execution_mode, case_environment, case_hardware = _case_metadata(case)
    if receipt.get("case_id") != case.get("id"):
        raise Tier3ReceiptError("Tier 3 receipt case_id does not match the selected case.")
    if receipt.get("automation_lane") != lane or receipt.get("execution_mode") != execution_mode:
        raise Tier3ReceiptError("Tier 3 receipt lane or execution mode does not match policy.")
    allowed_sources = _strings(case.get("allowed_evidence_sources"), "case allowed_evidence_sources")
    if receipt.get("evidence_source") not in allowed_sources:
        raise Tier3ReceiptError("Tier 3 receipt evidence source does not match policy.")

    release_identity = _mapping(receipt.get("release_identity"), "release_identity")
    _exact_keys(
        release_identity,
        {
            "artifact_sha256",
            "release_receipt",
            "release_route",
            "release_tag",
            "signed_app_tree_sha256",
            "source_sha",
            "versions",
        },
        "release_identity",
    )
    release_reference = _mapping(release_identity.get("release_receipt"), "release_identity.release_receipt")
    _exact_keys(
        release_reference,
        {"file_sha256", "receipt_sha256", "reference"},
        "release_identity.release_receipt",
    )
    reference = _relative_reference(release_reference.get("reference"), "release receipt reference")
    try:
        release_bytes = reference_content(reference)
    except OSError as error:
        raise Tier3ReceiptError(f"Unable to read referenced release receipt {reference!r}: {error}") from error
    release_file_sha256 = hashlib.sha256(release_bytes).hexdigest()
    if release_file_sha256 != _sha256(
        release_reference.get("file_sha256"),
        "release receipt file_sha256",
    ):
        raise Tier3ReceiptError("Referenced release receipt file digest does not match Tier 3 evidence.")
    try:
        release_receipt = _mapping(json.loads(release_bytes), "referenced release receipt")
    except json.JSONDecodeError as error:
        raise Tier3ReceiptError("Referenced release receipt is not valid JSON.") from error
    expected_release_identity = _release_identity(
        release_receipt,
        reference=reference,
        file_sha256=release_file_sha256,
    )
    if dict(release_identity) != expected_release_identity:
        raise Tier3ReceiptError("Tier 3 release or artifact identity does not match the referenced release receipt.")

    _validate_environment(_mapping(receipt.get("environment"), "environment"), case_environment)
    _validate_hardware(_mapping(receipt.get("hardware"), "hardware"), case_hardware)

    timestamps = _mapping(receipt.get("timestamps"), "timestamps")
    _exact_keys(timestamps, {"completed_at", "started_at"}, "timestamps")
    started_at = _timestamp(timestamps.get("started_at"), "timestamps.started_at")
    completed_at = _timestamp(timestamps.get("completed_at"), "timestamps.completed_at")
    if completed_at < started_at:
        raise Tier3ReceiptError("Tier 3 completed_at cannot precede started_at.")

    cadence = _mapping(receipt.get("cadence"), "cadence")
    _exact_keys(cadence, {"expires_on", "expiry_basis", "first_rc", "max_age_days", "stable_candidate"}, "cadence")
    case_cadence = _mapping(case.get("cadence"), "case cadence")
    case_expiry = _mapping(case.get("evidence_expiry"), "case evidence_expiry")
    max_age_days = _positive_integer(case_expiry.get("max_age_days"), "case evidence_expiry.max_age_days")
    if cadence.get("first_rc") != case_cadence.get("first_rc") or cadence.get("stable_candidate") != case_cadence.get(
        "stable_candidate"
    ):
        raise Tier3ReceiptError("Tier 3 receipt milestone cadence does not match policy.")
    if cadence.get("expiry_basis") != case_expiry.get("basis") or cadence.get("max_age_days") != max_age_days:
        raise Tier3ReceiptError("Tier 3 receipt expiry metadata does not match policy.")
    expected_expiry = completed_at.date() + timedelta(days=max_age_days)
    if _date(cadence.get("expires_on"), "cadence.expires_on") != expected_expiry:
        raise Tier3ReceiptError("Tier 3 receipt expires_on does not match completed_at and policy max age.")

    result = _mapping(receipt.get("result"), "result")
    _exact_keys(result, {"reason_code", "status"}, "result")
    result_status = _string(result.get("status"), "result.status")
    if result_status not in RESULT_STATUSES:
        raise Tier3ReceiptError("Tier 3 result.status is unsupported.")
    reason_code = _safe_identity(result.get("reason_code"), "result.reason_code")
    if result_status == "passed" and reason_code != "all_assertions_passed":
        raise Tier3ReceiptError("A passed Tier 3 receipt must use all_assertions_passed.")
    _validate_assertions(_sequence(receipt.get("assertions"), "assertions"), case, result_status)

    allowed_evidence_kinds = set(_strings(case.get("allowed_evidence_kinds"), "case allowed_evidence_kinds"))
    evidence = _sequence(receipt.get("evidence"), "evidence")
    if len(evidence) > 8:
        raise Tier3ReceiptError("Tier 3 receipt evidence is limited to eight bounded digests.")
    seen_evidence: set[str] = set()
    for index, raw_item in enumerate(evidence):
        item = _mapping(raw_item, f"evidence {index}")
        _exact_keys(item, {"kind", "sha256"}, f"evidence {index}")
        kind = _safe_identity(item.get("kind"), f"evidence {index} kind")
        _sha256(item.get("sha256"), f"evidence {index} sha256")
        if kind not in allowed_evidence_kinds or kind in seen_evidence:
            raise Tier3ReceiptError("Tier 3 evidence kind is unsupported or duplicated.")
        seen_evidence.add(kind)
    if result_status in {"passed", "failed"} and not evidence:
        raise Tier3ReceiptError("Passed or failed Tier 3 receipts require bounded evidence digests.")

    cleanup = _mapping(receipt.get("cleanup"), "cleanup")
    _exact_keys(cleanup, {"evidence_sha256", "status"}, "cleanup")
    cleanup_status = _safe_identity(cleanup.get("status"), "cleanup.status")
    allowed_cleanup = set(_strings(case.get("allowed_cleanup_results"), "case allowed_cleanup_results"))
    if cleanup_status not in allowed_cleanup:
        raise Tier3ReceiptError("Tier 3 cleanup status is not allowed by the case.")
    _optional_sha256(cleanup.get("evidence_sha256"), "cleanup.evidence_sha256")

    expected_digest = receipt_sha256(receipt)
    if _sha256(receipt.get("receipt_sha256"), "receipt_sha256") != expected_digest:
        raise Tier3ReceiptError("Tier 3 receipt_sha256 does not match the canonical payload.")


def load_validated_receipt_bytes(
    content: bytes,
    *,
    policy_id: str,
    case: Mapping[str, Any],
    reference_content: ReferenceContent,
) -> Mapping[str, Any]:
    try:
        receipt = _mapping(json.loads(content), "Tier 3 receipt")
    except json.JSONDecodeError as error:
        raise Tier3ReceiptError("Tier 3 receipt is not valid JSON.") from error
    validate_receipt(receipt, policy_id=policy_id, case=case, reference_content=reference_content)
    return receipt


def _load_policy(path: Path) -> Mapping[str, Any]:
    try:
        return _mapping(json.loads(path.read_text(encoding="utf-8")), "release qualification policy")
    except OSError as error:
        raise Tier3ReceiptError(f"Unable to read release qualification policy at {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise Tier3ReceiptError(f"Release qualification policy at {path} is not valid JSON.") from error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate a checked Tier 3 qualification receipt.")
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY_PATH)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--repo", type=Path, default=REPO_ROOT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        policy = _load_policy(args.policy)
        cases = {
            _string(case.get("id"), "case id"): case
            for case in (_mapping(item, "qualification case") for item in _sequence(policy.get("cases"), "cases"))
        }
        case = cases.get(args.case_id)
        if case is None:
            raise Tier3ReceiptError(f"Unknown Tier 3 case: {args.case_id}")
        content = args.receipt.read_bytes()
        receipt = load_validated_receipt_bytes(
            content,
            policy_id=_string(policy.get("policy_id"), "policy_id"),
            case=case,
            reference_content=lambda reference: (args.repo / reference).read_bytes(),
        )
        print(
            json.dumps(
                {
                    "case_id": receipt["case_id"],
                    "receipt_sha256": receipt["receipt_sha256"],
                    "result": receipt["result"],
                    "valid": True,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    except (OSError, Tier3ReceiptError) as error:
        print(f"tier3 receipt error: {error}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
