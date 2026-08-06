from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from scripts.qualify_release_scope import DEFAULT_POLICY_PATH, load_policy
from scripts.release_receipt import ReleaseReceiptError, validate_receipt as validate_release_receipt
from scripts.tier3_receipt import Tier3ReceiptError, build_receipt, validate_receipt


REPO_ROOT = Path(__file__).resolve().parents[1]
CASE_IDS = {
    "usb-bluray-makemkv",
    "protected-real-media-conversion",
    "vision-pro-physical-playback",
}
SKIP_REASONS = {"environment-unavailable", "hardware-unavailable", "operator-cancelled"}
CASE_CONTRACTS: dict[str, dict[str, Any]] = {
    "usb-bluray-makemkv": {
        "observations": {
            "drive_discovery": {"detected", "not-detected"},
            "makemkv": {"installed", "missing"},
            "cancellation": {"recovered", "failed"},
            "ejection": {"ejected", "failed"},
        },
        "assertions": {
            "drive-discovered": ("drive_discovery", "detected"),
            "makemkv-path-verified": ("makemkv", "installed"),
            "cancellation-recovered": ("cancellation", "recovered"),
            "drive-ejected": ("ejection", "ejected"),
        },
        "evidence": {
            "device-probe": ("drive_discovery",),
            "makemkv-probe": ("makemkv",),
            "ejection-result": ("cancellation", "ejection"),
            "cleanup": (),
        },
    },
    "protected-real-media-conversion": {
        "observations": {
            "protected_source": {"opened", "failed"},
            "conversion": {"completed", "failed", "cancelled"},
            "output": {"verified", "failed", "not-produced"},
            "recovery": {"clean", "recovered", "failed"},
        },
        "assertions": {
            "protected-source-opened": ("protected_source", "opened"),
            "conversion-completed": ("conversion", "completed"),
            "output-verified": ("output", "verified"),
            "recovery-clean": ("recovery", "clean"),
        },
        "evidence": {
            "conversion-result": ("protected_source", "conversion", "output"),
            "diagnostic-summary": ("recovery",),
            "cleanup": (),
        },
    },
    "vision-pro-physical-playback": {
        "observations": {
            "transfer": {"completed", "failed"},
            "stereo_playback": {"started", "failed"},
            "spatial_presentation": {"verified", "failed"},
            "playback": {"completed", "interrupted", "failed"},
        },
        "assertions": {
            "artifact-transferred": ("transfer", "completed"),
            "stereo-playback-started": ("stereo_playback", "started"),
            "spatial-presentation-verified": ("spatial_presentation", "verified"),
            "playback-completed": ("playback", "completed"),
        },
        "evidence": {
            "device-probe": (),
            "playback-result": ("transfer", "stereo_playback", "spatial_presentation", "playback"),
        },
    },
}


class OperatorReceiptError(RuntimeError):
    pass


def _mapping(value: object, description: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise OperatorReceiptError(f"{description} must be a JSON object.")
    return value


def _string(value: object, description: str) -> str:
    if not isinstance(value, str) or not value:
        raise OperatorReceiptError(f"{description} must be a non-empty string.")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], description: str) -> None:
    if set(value) != expected:
        raise OperatorReceiptError(f"{description} must contain exactly: {', '.join(sorted(expected))}.")


def _write_json(path: Path, value: Mapping[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(value, indent=2, sort_keys=True) + "\n"
    path.write_text(content, encoding="utf-8")
    return hashlib.sha256(content.encode()).hexdigest()


def _checked_release_receipt(repo: Path, path: Path) -> tuple[Mapping[str, Any], str, str]:
    repo = repo.resolve()
    resolved = path.resolve()
    try:
        reference = resolved.relative_to(repo).as_posix()
    except ValueError as error:
        raise OperatorReceiptError("Release receipt must be inside the repository.") from error
    result = subprocess.run(["git", "show", f"HEAD:{reference}"], cwd=repo, capture_output=True, timeout=60)
    if result.returncode != 0 or result.stdout != resolved.read_bytes():
        raise OperatorReceiptError("Release receipt must match the checked repository HEAD.")
    try:
        receipt = _mapping(json.loads(result.stdout), "release receipt")
        validate_release_receipt(receipt)
    except (json.JSONDecodeError, ReleaseReceiptError) as error:
        raise OperatorReceiptError(f"Release receipt is invalid: {error}") from error
    return receipt, reference, hashlib.sha256(result.stdout).hexdigest()


def _case(policy: Mapping[str, Any], case_id: str) -> Mapping[str, Any]:
    cases = [_mapping(item, "qualification case") for item in policy.get("cases", [])]
    matches = [case for case in cases if case.get("id") == case_id]
    if len(matches) != 1 or case_id not in CASE_IDS:
        raise OperatorReceiptError(f"Unsupported operator-assisted Tier 3 case: {case_id}")
    return matches[0]


def build_operator_receipt(
    answers: Mapping[str, Any],
    *,
    repo: Path,
    release_receipt_path: Path,
    evidence_directory: Path,
) -> Mapping[str, Any]:
    _exact_keys(
        answers,
        {
            "case_id",
            "cleanup_status",
            "completed_at",
            "disposition",
            "environment",
            "hardware",
            "observations",
            "reason_code",
            "schema_version",
            "started_at",
        },
        "operator answers",
    )
    if answers.get("schema_version") != 1:
        raise OperatorReceiptError("Operator answers use an unsupported schema version.")
    case_id = _string(answers.get("case_id"), "case_id")
    policy = load_policy(repo / DEFAULT_POLICY_PATH.relative_to(REPO_ROOT))
    case = _case(policy, case_id)
    contract = CASE_CONTRACTS[case_id]
    disposition = _string(answers.get("disposition"), "disposition")
    if disposition not in {"completed", "skipped"}:
        raise OperatorReceiptError("disposition must be completed or skipped.")
    reason_code = _string(answers.get("reason_code"), "reason_code")
    observations = _mapping(answers.get("observations"), "observations")

    evidence: list[Mapping[str, str]] = []
    if disposition == "skipped":
        if observations or reason_code not in SKIP_REASONS:
            raise OperatorReceiptError("Skipped answers require empty observations and a bounded skip reason.")
        assertions = {assertion_id: "not_run" for assertion_id in contract["assertions"]}
        result = {"status": "skipped", "reason_code": reason_code}
    else:
        expected_observations = contract["observations"]
        _exact_keys(observations, set(expected_observations), "observations")
        for field, allowed in expected_observations.items():
            if observations.get(field) not in allowed:
                raise OperatorReceiptError(f"observations.{field} is not a supported outcome.")
        assertions = {
            assertion_id: "passed" if observations[field] == expected else "failed"
            for assertion_id, (field, expected) in contract["assertions"].items()
        }
        passed = set(assertions.values()) == {"passed"}
        expected_reason = "all-assertions-passed" if passed else "operator-assertion-failed"
        if reason_code != expected_reason:
            raise OperatorReceiptError(f"Completed answers must use reason_code {expected_reason}.")
        result = {
            "status": "passed" if passed else "failed",
            "reason_code": "all_assertions_passed" if passed else "operator_assertion_failed",
        }
        if evidence_directory.exists():
            raise OperatorReceiptError("Evidence directory must not already exist.")
        evidence_directory.mkdir(parents=True)
        hardware = _mapping(answers.get("hardware"), "hardware")
        cleanup_status = _string(answers.get("cleanup_status"), "cleanup_status")
        for kind, fields in contract["evidence"].items():
            payload: dict[str, Any] = {field: observations[field] for field in fields}
            if kind == "device-probe":
                payload["hardware"] = hardware
            if kind == "cleanup":
                payload["status"] = cleanup_status
            payload["schema_version"] = 1
            evidence.append({"kind": kind, "sha256": _write_json(evidence_directory / f"{kind}.json", payload)})

    release_receipt, release_reference, release_file_sha256 = _checked_release_receipt(repo, release_receipt_path)
    cleanup_status = _string(answers.get("cleanup_status"), "cleanup_status")
    cleanup_digest = next((item["sha256"] for item in evidence if item["kind"] == "cleanup"), None)
    receipt = build_receipt(
        {
            "assertions": assertions,
            "cleanup": {"status": cleanup_status, "evidence_sha256": cleanup_digest},
            "completed_at": _string(answers.get("completed_at"), "completed_at"),
            "environment": dict(_mapping(answers.get("environment"), "environment")),
            "evidence": evidence,
            "evidence_source": "tier3_operator_receipt",
            "hardware": dict(_mapping(answers.get("hardware"), "hardware")),
            "release_receipt_file_sha256": release_file_sha256,
            "release_receipt_reference": release_reference,
            "result": result,
            "started_at": _string(answers.get("started_at"), "started_at"),
        },
        policy_id=_string(policy.get("policy_id"), "policy_id"),
        case=case,
        release_receipt=release_receipt,
    )
    try:
        validate_receipt(
            receipt,
            policy_id=_string(policy.get("policy_id"), "policy_id"),
            case=case,
            reference_content=lambda reference: (repo / reference).read_bytes(),
        )
    except Tier3ReceiptError as error:
        raise OperatorReceiptError(f"Generated operator receipt is invalid: {error}") from error
    return receipt


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a bounded operator-assisted Tier 3 hardware receipt.")
    parser.add_argument("--answers", type=Path, required=True)
    parser.add_argument("--release-receipt", type=Path, required=True)
    parser.add_argument("--output-receipt", type=Path, required=True)
    parser.add_argument("--evidence-directory", type=Path, required=True)
    parser.add_argument("--repo", type=Path, default=REPO_ROOT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        answers = _mapping(json.loads(args.answers.read_text(encoding="utf-8")), "operator answers")
        if args.output_receipt.exists():
            raise OperatorReceiptError("Output receipt must not already exist.")
        receipt = build_operator_receipt(
            answers,
            repo=args.repo.resolve(),
            release_receipt_path=args.release_receipt,
            evidence_directory=args.evidence_directory,
        )
        _write_json(args.output_receipt, receipt)
        print(json.dumps({"case_id": receipt["case_id"], "result": receipt["result"]}, sort_keys=True))
        return 0
    except (OSError, json.JSONDecodeError, OperatorReceiptError) as error:
        if args.output_receipt.exists():
            args.output_receipt.unlink()
        if args.evidence_directory.exists():
            for path in args.evidence_directory.iterdir():
                path.unlink()
            args.evidence_directory.rmdir()
        print(f"tier3 operator receipt error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
