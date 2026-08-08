from __future__ import annotations

import argparse
import hashlib
import json
import re

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from scripts.release_receipt import ReleaseReceiptError, validate_receipt as validate_release_receipt


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY_PATH = REPO_ROOT / "docs" / "qualification" / "release-qualification-policy-v1.json"
RECEIPT_TYPE = "bd-to-avp-signed-artifact-ui"
SCHEMA_VERSION = 1
PROFILE_CASE_ID = "profile-save-action-accessibility"
SOURCE = "signed_artifact_receipt"
TEST_SELECTOR = "BluRayToVisionProUITests/InstalledUIAcceptanceTests/testCandidateMainWindowProfileAndSettings"
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
MAX_RECEIPT_BYTES = 16 * 1024
MAX_POLICY_BYTES = 256 * 1024


class SignedArtifactReceiptError(RuntimeError):
    pass


@dataclass(frozen=True)
class SignedArtifactReceiptExpectation:
    policy_id: str
    case_id: str
    candidate_sha: str
    workflow_run_id: int
    workflow_run_attempt: int
    release_id: int
    release_receipt_asset_id: int
    release_receipt_file_sha256: str
    release_receipt_sha256: str
    package_version: str
    public_version: str
    build_version: str
    release_tag: str
    dmg_asset_id: int
    dmg_name: str
    dmg_size: int
    dmg_sha256: str
    signed_app_tree_sha256: str


def _mapping(value: object, description: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SignedArtifactReceiptError(f"{description} must be a JSON object.")
    return cast(Mapping[str, Any], value)


def _sequence(value: object, description: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise SignedArtifactReceiptError(f"{description} must be a JSON array.")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], description: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise SignedArtifactReceiptError(f"{description} keys changed; missing={missing}, extra={extra}.")


def _string(value: object, description: str) -> str:
    if not isinstance(value, str) or not value:
        raise SignedArtifactReceiptError(f"{description} must be a non-empty string.")
    return value


def _integer(value: object, description: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise SignedArtifactReceiptError(f"{description} must be a positive integer.")
    return value


def _sha(value: object, description: str) -> str:
    text = _string(value, description)
    if SHA_PATTERN.fullmatch(text) is None:
        raise SignedArtifactReceiptError(f"{description} must be a full lowercase Git SHA.")
    return text


def _sha256(value: object, description: str) -> str:
    text = _string(value, description)
    if SHA256_PATTERN.fullmatch(text) is None:
        raise SignedArtifactReceiptError(f"{description} must be a lowercase SHA-256 digest.")
    return text


def _load_json(path: Path, description: str, *, max_bytes: int = MAX_RECEIPT_BYTES) -> Mapping[str, Any]:
    try:
        content = path.read_bytes()
    except OSError as error:
        raise SignedArtifactReceiptError(f"Unable to read {description} at {path}: {error}") from error
    if len(content) > max_bytes:
        raise SignedArtifactReceiptError(f"{description.capitalize()} exceeds the {max_bytes}-byte limit.")
    try:
        return _mapping(json.loads(content), description)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SignedArtifactReceiptError(f"Invalid JSON in {description} at {path}: {error}") from error


def canonical_payload_bytes(receipt: Mapping[str, Any]) -> bytes:
    payload = dict(receipt)
    payload.pop("receipt_sha256", None)
    return (json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


def receipt_sha256(receipt: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_payload_bytes(receipt)).hexdigest()


def _case(policy: Mapping[str, Any], case_id: str) -> Mapping[str, Any]:
    for raw_case in _sequence(policy.get("cases"), "release qualification cases"):
        case = _mapping(raw_case, "release qualification case")
        if case.get("id") == case_id:
            return case
    raise SignedArtifactReceiptError(f"Release qualification policy is missing case {case_id!r}.")


def validate_policy_case(policy: Mapping[str, Any], case_id: str = PROFILE_CASE_ID) -> str:
    if policy.get("schema_version") != 1:
        raise SignedArtifactReceiptError("Release qualification policy schema_version must be 1.")
    policy_id = _string(policy.get("policy_id"), "policy_id")
    case = _case(policy, case_id)
    if case.get("tier") != 2:
        raise SignedArtifactReceiptError(f"Signed artifact case {case_id!r} must remain Tier 2.")
    if case.get("blocking_phase") != "publication" or case.get("artifact_owned") is not True:
        raise SignedArtifactReceiptError(f"Signed artifact case {case_id!r} must be artifact-owned.")
    if case.get("requires_live_publication") is True:
        raise SignedArtifactReceiptError(f"Signed artifact case {case_id!r} cannot require live publication.")
    sources = _sequence(case.get("allowed_evidence_sources"), f"case {case_id!r} evidence sources")
    if list(sources) != [SOURCE]:
        raise SignedArtifactReceiptError(f"Signed artifact case {case_id!r} must use {SOURCE!r}.")
    return policy_id


def release_expectation_from_receipt(
    release_receipt: Mapping[str, Any],
    *,
    policy_id: str,
    case_id: str,
    workflow_run_id: int,
    workflow_run_attempt: int,
    release_receipt_asset_id: int,
    release_receipt_file_sha256: str,
) -> SignedArtifactReceiptExpectation:
    try:
        validate_release_receipt(release_receipt)
    except ReleaseReceiptError as error:
        raise SignedArtifactReceiptError(f"Release receipt is invalid: {error}") from error
    release = _mapping(release_receipt.get("release"), "release receipt release")
    versions = _mapping(release_receipt.get("versions"), "release receipt versions")
    artifacts = [
        _mapping(item, "release receipt artifact")
        for item in _sequence(release_receipt.get("artifacts"), "release receipt artifacts")
    ]
    dmgs = [artifact for artifact in artifacts if artifact.get("kind") == "dmg"]
    if len(dmgs) != 1:
        raise SignedArtifactReceiptError("Release receipt must contain exactly one DMG artifact.")
    dmg = dmgs[0]
    workflow = _mapping(release_receipt.get("workflow"), "release receipt workflow")
    if workflow.get("run_id") != workflow_run_id or workflow.get("run_attempt") != workflow_run_attempt:
        raise SignedArtifactReceiptError("Release receipt workflow binding does not match this run attempt.")
    return SignedArtifactReceiptExpectation(
        policy_id=policy_id,
        case_id=case_id,
        candidate_sha=_sha(release_receipt.get("source_sha"), "release receipt source_sha"),
        workflow_run_id=_integer(workflow_run_id, "workflow run ID"),
        workflow_run_attempt=_integer(workflow_run_attempt, "workflow run attempt"),
        release_id=_integer(release.get("id"), "release ID"),
        release_receipt_asset_id=_integer(release_receipt_asset_id, "release receipt asset ID"),
        release_receipt_file_sha256=_sha256(release_receipt_file_sha256, "release receipt file SHA-256"),
        release_receipt_sha256=_sha256(release_receipt.get("receipt_sha256"), "release receipt self SHA-256"),
        package_version=_string(versions.get("package"), "package version"),
        public_version=_string(versions.get("public"), "public version"),
        build_version=_string(versions.get("build"), "build version"),
        release_tag=_string(release.get("tag"), "release tag"),
        dmg_asset_id=_integer(dmg.get("asset_id"), "DMG asset ID"),
        dmg_name=_string(dmg.get("name"), "DMG name"),
        dmg_size=_integer(dmg.get("size_bytes"), "DMG size"),
        dmg_sha256=_sha256(dmg.get("sha256"), "DMG SHA-256"),
        signed_app_tree_sha256=_sha256(
            release_receipt.get("signed_app_tree_sha256"),
            "signed app tree SHA-256",
        ),
    )


def build_receipt(
    *,
    expectation: SignedArtifactReceiptExpectation,
    evidence: Mapping[str, str],
) -> dict[str, Any]:
    evidence_records = [
        {"kind": _string(kind, "evidence kind"), "sha256": _sha256(digest, f"{kind} SHA-256")}
        for kind, digest in sorted(evidence.items())
    ]
    expected_evidence = {"accessibility-tree", "screenshot-dark", "screenshot-light", "ui-result"}
    if {record["kind"] for record in evidence_records} != expected_evidence:
        raise SignedArtifactReceiptError("Signed artifact UI evidence set is incomplete or changed.")
    receipt: dict[str, Any] = {
        "case_id": expectation.case_id,
        "candidate_sha": expectation.candidate_sha,
        "dmg": {
            "asset_id": expectation.dmg_asset_id,
            "name": expectation.dmg_name,
            "sha256": expectation.dmg_sha256,
            "size_bytes": expectation.dmg_size,
        },
        "evidence": evidence_records,
        "policy_id": expectation.policy_id,
        "receipt_type": RECEIPT_TYPE,
        "release": {"id": expectation.release_id, "tag": expectation.release_tag},
        "release_receipt": {
            "asset_id": expectation.release_receipt_asset_id,
            "file_sha256": expectation.release_receipt_file_sha256,
            "receipt_sha256": expectation.release_receipt_sha256,
        },
        "schema_version": SCHEMA_VERSION,
        "signed_app_tree_sha256": expectation.signed_app_tree_sha256,
        "source": SOURCE,
        "status": "passed",
        "test_selector": TEST_SELECTOR,
        "versions": {
            "build": expectation.build_version,
            "package": expectation.package_version,
            "public": expectation.public_version,
        },
        "workflow": {
            "job": "signed-artifact-ui",
            "run_attempt": expectation.workflow_run_attempt,
            "run_id": expectation.workflow_run_id,
        },
    }
    receipt["receipt_sha256"] = receipt_sha256(receipt)
    validate_receipt(receipt, expectation)
    return receipt


def validate_receipt(receipt: Mapping[str, Any], expectation: SignedArtifactReceiptExpectation) -> None:
    expected_workflow_run_id = _integer(expectation.workflow_run_id, "expected workflow run ID")
    expected_workflow_run_attempt = _integer(expectation.workflow_run_attempt, "expected workflow run attempt")
    expected_release_id = _integer(expectation.release_id, "expected release ID")
    expected_release_receipt_asset_id = _integer(
        expectation.release_receipt_asset_id,
        "expected release receipt asset ID",
    )
    expected_dmg_asset_id = _integer(expectation.dmg_asset_id, "expected DMG asset ID")
    expected_dmg_size = _integer(expectation.dmg_size, "expected DMG size")
    expected_values = {
        "case_id": _string(expectation.case_id, "expected case ID"),
        "candidate_sha": _sha(expectation.candidate_sha, "expected candidate SHA"),
        "policy_id": _string(expectation.policy_id, "expected policy ID"),
        "release_receipt_file_sha256": _sha256(
            expectation.release_receipt_file_sha256,
            "expected release receipt file SHA-256",
        ),
        "release_receipt_sha256": _sha256(
            expectation.release_receipt_sha256,
            "expected release receipt self SHA-256",
        ),
        "package_version": _string(expectation.package_version, "expected package version"),
        "public_version": _string(expectation.public_version, "expected public version"),
        "build_version": _string(expectation.build_version, "expected build version"),
        "release_tag": _string(expectation.release_tag, "expected release tag"),
        "dmg_name": _string(expectation.dmg_name, "expected DMG name"),
        "dmg_sha256": _sha256(expectation.dmg_sha256, "expected DMG SHA-256"),
        "signed_app_tree_sha256": _sha256(
            expectation.signed_app_tree_sha256,
            "expected signed app tree SHA-256",
        ),
    }
    _exact_keys(
        receipt,
        {
            "case_id",
            "candidate_sha",
            "dmg",
            "evidence",
            "policy_id",
            "receipt_sha256",
            "receipt_type",
            "release",
            "release_receipt",
            "schema_version",
            "signed_app_tree_sha256",
            "source",
            "status",
            "test_selector",
            "versions",
            "workflow",
        },
        "signed artifact receipt",
    )
    if receipt.get("schema_version") != SCHEMA_VERSION or receipt.get("receipt_type") != RECEIPT_TYPE:
        raise SignedArtifactReceiptError("Unsupported signed artifact receipt schema or type.")
    expected_scalars = {
        "case_id": expected_values["case_id"],
        "candidate_sha": expected_values["candidate_sha"],
        "policy_id": expected_values["policy_id"],
        "signed_app_tree_sha256": expected_values["signed_app_tree_sha256"],
        "source": SOURCE,
        "status": "passed",
        "test_selector": TEST_SELECTOR,
    }
    for field, expected in expected_scalars.items():
        if receipt.get(field) != expected:
            raise SignedArtifactReceiptError(f"Signed artifact receipt {field} does not match.")

    workflow = _mapping(receipt.get("workflow"), "signed artifact workflow")
    _exact_keys(workflow, {"job", "run_attempt", "run_id"}, "signed artifact workflow")
    if workflow != {
        "job": "signed-artifact-ui",
        "run_attempt": expected_workflow_run_attempt,
        "run_id": expected_workflow_run_id,
    }:
        raise SignedArtifactReceiptError("Signed artifact receipt workflow binding does not match.")

    release = _mapping(receipt.get("release"), "signed artifact release")
    _exact_keys(release, {"id", "tag"}, "signed artifact release")
    if release != {"id": expected_release_id, "tag": expected_values["release_tag"]}:
        raise SignedArtifactReceiptError("Signed artifact receipt release binding does not match.")

    release_receipt = _mapping(receipt.get("release_receipt"), "signed artifact release receipt")
    _exact_keys(release_receipt, {"asset_id", "file_sha256", "receipt_sha256"}, "signed artifact release receipt")
    if release_receipt != {
        "asset_id": expected_release_receipt_asset_id,
        "file_sha256": expected_values["release_receipt_file_sha256"],
        "receipt_sha256": expected_values["release_receipt_sha256"],
    }:
        raise SignedArtifactReceiptError("Signed artifact release receipt binding does not match.")

    versions = _mapping(receipt.get("versions"), "signed artifact versions")
    _exact_keys(versions, {"build", "package", "public"}, "signed artifact versions")
    if versions != {
        "build": expected_values["build_version"],
        "package": expected_values["package_version"],
        "public": expected_values["public_version"],
    }:
        raise SignedArtifactReceiptError("Signed artifact receipt version binding does not match.")

    dmg = _mapping(receipt.get("dmg"), "signed artifact DMG")
    _exact_keys(dmg, {"asset_id", "name", "sha256", "size_bytes"}, "signed artifact DMG")
    if dmg != {
        "asset_id": expected_dmg_asset_id,
        "name": expected_values["dmg_name"],
        "sha256": expected_values["dmg_sha256"],
        "size_bytes": expected_dmg_size,
    }:
        raise SignedArtifactReceiptError("Signed artifact receipt DMG binding does not match.")

    evidence = _sequence(receipt.get("evidence"), "signed artifact evidence")
    expected_kinds = ["accessibility-tree", "screenshot-dark", "screenshot-light", "ui-result"]
    observed_kinds: list[str] = []
    for index, raw_record in enumerate(evidence):
        record = _mapping(raw_record, f"signed artifact evidence {index}")
        _exact_keys(record, {"kind", "sha256"}, f"signed artifact evidence {index}")
        observed_kinds.append(_string(record.get("kind"), f"signed artifact evidence {index} kind"))
        _sha256(record.get("sha256"), f"signed artifact evidence {index} SHA-256")
    if observed_kinds != expected_kinds:
        raise SignedArtifactReceiptError("Signed artifact receipt evidence kinds changed.")

    recorded_digest = _sha256(receipt.get("receipt_sha256"), "signed artifact receipt_sha256")
    if recorded_digest != receipt_sha256(receipt):
        raise SignedArtifactReceiptError("Signed artifact receipt self-digest mismatch.")


def load_validated_receipt(
    path: Path,
    expectation: SignedArtifactReceiptExpectation,
    *,
    expected_file_sha256: str,
) -> Mapping[str, Any]:
    try:
        content = path.read_bytes()
    except OSError as error:
        raise SignedArtifactReceiptError(f"Unable to read signed artifact receipt at {path}: {error}") from error
    if len(content) > MAX_RECEIPT_BYTES:
        raise SignedArtifactReceiptError(f"Signed artifact receipt exceeds the {MAX_RECEIPT_BYTES}-byte limit.")
    if hashlib.sha256(content).hexdigest() != _sha256(
        expected_file_sha256,
        "expected signed artifact receipt file SHA-256",
    ):
        raise SignedArtifactReceiptError("Signed artifact receipt file digest does not match the workflow output.")
    receipt = _load_json(path, "signed artifact receipt")
    validate_receipt(receipt, expectation)
    return receipt


def validate_receipt_files(
    *,
    receipt_path: Path,
    release_receipt_path: Path,
    policy_path: Path,
    case_id: str,
    workflow_run_id: int,
    workflow_run_attempt: int,
    release_receipt_asset_id: int,
    release_receipt_file_sha256: str,
    receipt_file_sha256: str,
) -> Mapping[str, Any]:
    expected_release_receipt_file_sha256 = _sha256(
        release_receipt_file_sha256,
        "expected release receipt file SHA-256",
    )
    try:
        actual_release_receipt_file_sha256 = hashlib.sha256(release_receipt_path.read_bytes()).hexdigest()
    except OSError as error:
        raise SignedArtifactReceiptError(
            f"Unable to read release receipt at {release_receipt_path}: {error}"
        ) from error
    if actual_release_receipt_file_sha256 != expected_release_receipt_file_sha256:
        raise SignedArtifactReceiptError("Release receipt file digest does not match the expected immutable asset.")

    policy_id = validate_policy_case(
        _load_json(policy_path, "release qualification policy", max_bytes=MAX_POLICY_BYTES),
        case_id,
    )
    expectation = release_expectation_from_receipt(
        _load_json(release_receipt_path, "release receipt"),
        policy_id=policy_id,
        case_id=case_id,
        workflow_run_id=workflow_run_id,
        workflow_run_attempt=workflow_run_attempt,
        release_receipt_asset_id=release_receipt_asset_id,
        release_receipt_file_sha256=expected_release_receipt_file_sha256,
    )
    return load_validated_receipt(
        receipt_path,
        expectation,
        expected_file_sha256=receipt_file_sha256,
    )


def write_receipt(receipt: Mapping[str, Any], output: Path) -> None:
    content = (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode()
    if len(content) > MAX_RECEIPT_BYTES:
        raise SignedArtifactReceiptError(f"Signed artifact receipt exceeds the {MAX_RECEIPT_BYTES}-byte limit.")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(content)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build and validate signed artifact UI receipts.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_policy_parser = subparsers.add_parser("validate-policy", help="Validate the artifact-owned policy case.")
    validate_policy_parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY_PATH)
    validate_policy_parser.add_argument("--case-id", default=PROFILE_CASE_ID)

    validate_parser = subparsers.add_parser(
        "validate",
        help="Validate a signed artifact receipt against an exact checked release receipt.",
    )
    validate_parser.add_argument("--receipt", type=Path, required=True)
    validate_parser.add_argument("--receipt-file-sha256", required=True)
    validate_parser.add_argument("--release-receipt", type=Path, required=True)
    validate_parser.add_argument("--release-receipt-asset-id", type=int, required=True)
    validate_parser.add_argument("--release-receipt-file-sha256", required=True)
    validate_parser.add_argument("--workflow-run-id", type=int, required=True)
    validate_parser.add_argument("--workflow-run-attempt", type=int, required=True)
    validate_parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY_PATH)
    validate_parser.add_argument("--case-id", default=PROFILE_CASE_ID)

    args = parser.parse_args(argv)
    try:
        if args.command == "validate-policy":
            policy_id = validate_policy_case(
                _load_json(args.policy, "release qualification policy", max_bytes=MAX_POLICY_BYTES),
                args.case_id,
            )
            print(json.dumps({"case_id": args.case_id, "policy_id": policy_id, "valid": True}, sort_keys=True))
        elif args.command == "validate":
            receipt = validate_receipt_files(
                receipt_path=args.receipt,
                release_receipt_path=args.release_receipt,
                policy_path=args.policy,
                case_id=args.case_id,
                workflow_run_id=args.workflow_run_id,
                workflow_run_attempt=args.workflow_run_attempt,
                release_receipt_asset_id=args.release_receipt_asset_id,
                release_receipt_file_sha256=args.release_receipt_file_sha256,
                receipt_file_sha256=args.receipt_file_sha256,
            )
            print(
                json.dumps(
                    {
                        "candidate_sha": receipt["candidate_sha"],
                        "case_id": receipt["case_id"],
                        "receipt_sha256": receipt["receipt_sha256"],
                        "valid": True,
                    },
                    sort_keys=True,
                )
            )
    except SignedArtifactReceiptError as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
