from __future__ import annotations

import argparse
import hashlib
import json
import re

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from scripts.release import DMG_NAME_PREFIX, ReleaseError, parse_build_version, parse_release_version


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY_PATH = REPO_ROOT / "docs" / "qualification" / "release-qualification-policy-v1.json"
RECEIPT_ASSET_NAME = "release-receipt.json"
RECEIPT_TYPE = "bd-to-avp-release-engine"
SCHEMA_VERSION = 1
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
TAG_PATTERN = re.compile(r"^v[0-9A-Za-z][0-9A-Za-z.-]*$")
EXPECTED_REPOSITORY = "cbusillo/BD_to_AVP"
EXPECTED_BRANCH = "main"
ENGINE_WORKFLOW_PATH = ".github/workflows/release-engine.yml"
ROUTES = {
    "stable": ("Stable", ".github/workflows/briefcase.yml"),
    "prerelease": ("Prerelease", ".github/workflows/prerelease.yml"),
}
VERIFICATION_FIELDS = (
    "app_notarization",
    "app_staple",
    "dmg_notarization",
    "dmg_staple",
    "gatekeeper",
    "package_smoke",
    "asset_redownload",
    "attestation",
    "appcast",
)
TIER1_CASE_VERIFICATIONS = {
    "release-workflow-identity": VERIFICATION_FIELDS,
    "signed-packaged-route-parity": ("package_smoke", "asset_redownload", "attestation", "appcast"),
}


class ReleaseReceiptError(RuntimeError):
    pass


@dataclass(frozen=True)
class ArtifactReceiptExpectation:
    candidate_sha: str
    release_route: str
    workflow_run_id: int
    workflow_run_attempt: int
    release_id: int
    package_version: str
    public_version: str
    build_version: str
    release_tag: str
    dmg_name: str
    receipt_asset_id: int
    receipt_file_sha256: str
    receipt_sha256: str
    signed_app_tree_sha256: str
    dmg_asset_id: int
    dmg_size: int
    dmg_sha256: str
    checksum_asset_id: int
    checksum_sha256: str
    appcast_asset_id: int
    appcast_sha256: str


def _mapping(value: object, description: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ReleaseReceiptError(f"{description} must be a JSON object.")
    return cast(Mapping[str, Any], value)


def _sequence(value: object, description: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise ReleaseReceiptError(f"{description} must be a JSON array.")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], description: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ReleaseReceiptError(f"{description} keys changed; missing={missing}, extra={extra}.")


def _string(value: object, description: str) -> str:
    if not isinstance(value, str) or not value:
        raise ReleaseReceiptError(f"{description} must be a non-empty string.")
    return value


def _integer(value: object, description: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ReleaseReceiptError(f"{description} must be a positive integer.")
    return value


def _boolean(value: object, description: str) -> bool:
    if not isinstance(value, bool):
        raise ReleaseReceiptError(f"{description} must be a boolean.")
    return value


def _sha(value: object, description: str) -> str:
    text = _string(value, description)
    if SHA_PATTERN.fullmatch(text) is None:
        raise ReleaseReceiptError(f"{description} must be a full lowercase Git SHA.")
    return text


def _sha256(value: object, description: str) -> str:
    text = _string(value, description)
    if SHA256_PATTERN.fullmatch(text) is None:
        raise ReleaseReceiptError(f"{description} must be a lowercase SHA-256 digest.")
    return text


def _load_json(path: Path, description: str) -> Mapping[str, Any]:
    try:
        return _mapping(json.loads(path.read_text(encoding="utf-8")), description)
    except OSError as error:
        raise ReleaseReceiptError(f"Unable to read {description} at {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise ReleaseReceiptError(f"Invalid JSON in {description} at {path}: {error}") from error


def canonical_payload_bytes(receipt: Mapping[str, Any]) -> bytes:
    payload = dict(receipt)
    payload.pop("receipt_sha256", None)
    return (json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


def receipt_sha256(receipt: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_payload_bytes(receipt)).hexdigest()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tier1_case_ids(policy_path: Path) -> list[str]:
    policy = _load_json(policy_path, "release qualification policy")
    if policy.get("schema_version") != 1:
        raise ReleaseReceiptError("Release qualification policy schema_version must be 1.")
    case_ids: list[str] = []
    for index, raw_case in enumerate(_sequence(policy.get("cases"), "release qualification cases")):
        case = _mapping(raw_case, f"release qualification case {index}")
        if case.get("tier") == 1:
            case_ids.append(_string(case.get("id"), f"release qualification case {index} id"))
    if not case_ids or len(case_ids) != len(set(case_ids)):
        raise ReleaseReceiptError("Release qualification policy must define unique Tier 1 case IDs.")
    policy_case_ids = set(case_ids)
    mapped_case_ids = set(TIER1_CASE_VERIFICATIONS)
    if policy_case_ids != mapped_case_ids:
        raise ReleaseReceiptError(
            "Release qualification Tier 1 cases require explicit receipt verification mappings; "
            f"unmapped={sorted(policy_case_ids - mapped_case_ids)}, stale={sorted(mapped_case_ids - policy_case_ids)}."
        )
    return sorted(policy_case_ids)


def build_receipt(facts: Mapping[str, Any], policy_path: Path = DEFAULT_POLICY_PATH) -> dict[str, Any]:
    route = _string(facts.get("release_route"), "release_route")
    if route not in ROUTES:
        raise ReleaseReceiptError(f"release_route must be one of {sorted(ROUTES)}.")
    workflow_name, workflow_path = ROUTES[route]
    source_sha = _sha(facts.get("source_sha"), "source_sha")

    artifacts: list[dict[str, Any]] = []
    raw_artifacts = _sequence(facts.get("artifacts"), "artifacts")
    expected_artifacts = {"dmg", "checksum", "appcast"}
    seen_artifacts: set[str] = set()
    for index, raw_artifact in enumerate(raw_artifacts):
        artifact = _mapping(raw_artifact, f"artifact {index}")
        kind = _string(artifact.get("kind"), f"artifact {index} kind")
        if kind not in expected_artifacts or kind in seen_artifacts:
            raise ReleaseReceiptError("Artifacts must contain exactly one DMG, checksum, and appcast.")
        seen_artifacts.add(kind)
        artifacts.append(
            {
                "asset_id": _integer(artifact.get("asset_id"), f"artifact {kind} asset_id"),
                "kind": kind,
                "name": _string(artifact.get("name"), f"artifact {kind} name"),
                "sha256": _sha256(artifact.get("sha256"), f"artifact {kind} sha256"),
                "size_bytes": _integer(artifact.get("size_bytes"), f"artifact {kind} size_bytes"),
            }
        )
    if seen_artifacts != expected_artifacts:
        raise ReleaseReceiptError("Artifacts must contain exactly one DMG, checksum, and appcast.")
    artifacts.sort(key=lambda item: cast(str, item["kind"]))
    appcast = next(item for item in artifacts if item["kind"] == "appcast")

    receipt: dict[str, Any] = {
        "appcast": {
            "live_pages_sha256": cast(str, appcast["sha256"]),
            "live_pages_state": "pending_release_deploy",
            "live_pages_url": "https://cbusillo.github.io/BD_to_AVP/appcast.xml",
        },
        "artifacts": artifacts,
        "protected_branch": EXPECTED_BRANCH,
        "receipt_type": RECEIPT_TYPE,
        "release": {
            "created_at": _string(facts.get("release_created_at"), "release_created_at"),
            "id": _integer(facts.get("release_id"), "release_id"),
            "make_latest": _boolean(facts.get("make_latest"), "make_latest"),
            "name": _string(facts.get("release_name"), "release_name"),
            "prerelease": _boolean(facts.get("prerelease"), "prerelease"),
            "tag": _string(facts.get("release_tag"), "release_tag"),
            "target_sha": source_sha,
        },
        "release_route": route,
        "repository": EXPECTED_REPOSITORY,
        "schema_version": SCHEMA_VERSION,
        "signed_app_tree_sha256": _sha256(facts.get("signed_app_tree_sha256"), "signed_app_tree_sha256"),
        "source_sha": source_sha,
        "tier1_case_references": _tier1_case_ids(policy_path),
        "verification": {field: "passed" for field in VERIFICATION_FIELDS},
        "versions": {
            "build": _string(facts.get("build_version"), "build_version"),
            "package": _string(facts.get("package_version"), "package_version"),
            "public": _string(facts.get("public_version"), "public_version"),
        },
        "workflow": {
            "actor": _string(facts.get("workflow_actor"), "workflow_actor"),
            "engine_path": ENGINE_WORKFLOW_PATH,
            "name": workflow_name,
            "path": workflow_path,
            "run_attempt": _integer(facts.get("workflow_run_attempt"), "workflow_run_attempt"),
            "run_id": _integer(facts.get("workflow_run_id"), "workflow_run_id"),
        },
    }
    receipt["receipt_sha256"] = receipt_sha256(receipt)
    validate_receipt(receipt)
    return receipt


def validate_receipt(receipt: Mapping[str, Any]) -> None:
    _exact_keys(
        receipt,
        {
            "appcast",
            "artifacts",
            "protected_branch",
            "receipt_sha256",
            "receipt_type",
            "release",
            "release_route",
            "repository",
            "schema_version",
            "signed_app_tree_sha256",
            "source_sha",
            "tier1_case_references",
            "verification",
            "versions",
            "workflow",
        },
        "release receipt",
    )
    if receipt.get("schema_version") != SCHEMA_VERSION or receipt.get("receipt_type") != RECEIPT_TYPE:
        raise ReleaseReceiptError("Unsupported release receipt schema or type.")
    if receipt.get("repository") != EXPECTED_REPOSITORY or receipt.get("protected_branch") != EXPECTED_BRANCH:
        raise ReleaseReceiptError("Release receipt repository or protected branch is not canonical.")
    source_sha = _sha(receipt.get("source_sha"), "source_sha")
    _sha256(receipt.get("signed_app_tree_sha256"), "signed_app_tree_sha256")

    route = _string(receipt.get("release_route"), "release_route")
    if route not in ROUTES:
        raise ReleaseReceiptError(f"release_route must be one of {sorted(ROUTES)}.")
    workflow_name, workflow_path = ROUTES[route]
    workflow = _mapping(receipt.get("workflow"), "workflow")
    _exact_keys(workflow, {"actor", "engine_path", "name", "path", "run_attempt", "run_id"}, "workflow")
    if workflow.get("name") != workflow_name or workflow.get("path") != workflow_path:
        raise ReleaseReceiptError("Release receipt workflow does not match its route.")
    if workflow.get("engine_path") != ENGINE_WORKFLOW_PATH:
        raise ReleaseReceiptError("Release receipt engine workflow path is not canonical.")
    if workflow.get("actor") != "shiny-code-bot":
        raise ReleaseReceiptError("Release receipt actor is not the approved release actor.")
    _integer(workflow.get("run_id"), "workflow run_id")
    _integer(workflow.get("run_attempt"), "workflow run_attempt")

    versions = _mapping(receipt.get("versions"), "versions")
    _exact_keys(versions, {"build", "package", "public"}, "versions")
    for key in versions:
        _string(versions[key], f"versions.{key}")
    package_version = cast(str, versions["package"])
    try:
        parsed_version = parse_release_version(package_version)
        parse_build_version(cast(str, versions["build"]))
    except ReleaseError as error:
        raise ReleaseReceiptError(f"Release receipt version identity is invalid: {error}") from error
    if versions.get("public") != parsed_version.public_version:
        raise ReleaseReceiptError("Release receipt public version does not match its package version.")
    expected_route = "prerelease" if parsed_version.prerelease else "stable"
    if route != expected_route:
        raise ReleaseReceiptError("Release receipt route does not match its package version stage.")

    release = _mapping(receipt.get("release"), "release")
    _exact_keys(release, {"created_at", "id", "make_latest", "name", "prerelease", "tag", "target_sha"}, "release")
    tag = _string(release.get("tag"), "release tag")
    if TAG_PATTERN.fullmatch(tag) is None:
        raise ReleaseReceiptError("Release receipt tag is not canonical.")
    if tag != parsed_version.release_tag or release.get("name") != tag:
        raise ReleaseReceiptError("Release receipt tag or name does not match its package version.")
    _integer(release.get("id"), "release id")
    prerelease = _boolean(release.get("prerelease"), "release prerelease")
    make_latest = _boolean(release.get("make_latest"), "release make_latest")
    if (route, prerelease, make_latest) not in {
        ("stable", False, True),
        ("prerelease", True, False),
    }:
        raise ReleaseReceiptError("Release prerelease and Latest state do not match the guarded route.")
    if release.get("target_sha") != source_sha:
        raise ReleaseReceiptError("Release target SHA does not match source_sha.")
    created_at = _string(release.get("created_at"), "release created_at")
    try:
        parsed_created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise ReleaseReceiptError("Release receipt created_at must be an ISO-8601 timestamp.") from error
    if parsed_created_at.tzinfo is None:
        raise ReleaseReceiptError("Release receipt created_at must include a timezone.")

    artifacts = _sequence(receipt.get("artifacts"), "artifacts")
    seen: set[str] = set()
    artifacts_by_kind: dict[str, Mapping[str, Any]] = {}
    for index, raw_artifact in enumerate(artifacts):
        artifact = _mapping(raw_artifact, f"artifact {index}")
        _exact_keys(artifact, {"asset_id", "kind", "name", "sha256", "size_bytes"}, f"artifact {index}")
        kind = _string(artifact.get("kind"), f"artifact {index} kind")
        if kind not in {"dmg", "checksum", "appcast"} or kind in seen:
            raise ReleaseReceiptError("Release receipt artifacts are incomplete or duplicated.")
        seen.add(kind)
        artifacts_by_kind[kind] = artifact
        _integer(artifact.get("asset_id"), f"artifact {kind} asset_id")
        _integer(artifact.get("size_bytes"), f"artifact {kind} size_bytes")
        _string(artifact.get("name"), f"artifact {kind} name")
        _sha256(artifact.get("sha256"), f"artifact {kind} sha256")
    if seen != {"dmg", "checksum", "appcast"}:
        raise ReleaseReceiptError("Release receipt artifacts are incomplete or duplicated.")
    expected_names = {
        "dmg": f"{DMG_NAME_PREFIX}-{parsed_version.public_version}.dmg",
        "checksum": "SHA256SUMS",
        "appcast": "appcast.xml",
    }
    for kind, expected_name in expected_names.items():
        if artifacts_by_kind[kind].get("name") != expected_name:
            raise ReleaseReceiptError(f"Release receipt {kind} asset name does not match its release identity.")

    appcast = _mapping(receipt.get("appcast"), "appcast")
    _exact_keys(appcast, {"live_pages_sha256", "live_pages_state", "live_pages_url"}, "appcast")
    if appcast.get("live_pages_state") != "pending_release_deploy":
        raise ReleaseReceiptError("Engine receipt appcast state must be pending_release_deploy.")
    if appcast.get("live_pages_url") != "https://cbusillo.github.io/BD_to_AVP/appcast.xml":
        raise ReleaseReceiptError("Release receipt live Pages URL is not canonical.")
    appcast_sha = _sha256(appcast.get("live_pages_sha256"), "appcast live_pages_sha256")
    recorded_appcast = next(
        _mapping(item, "appcast artifact") for item in artifacts if _mapping(item, "artifact").get("kind") == "appcast"
    )
    if recorded_appcast.get("sha256") != appcast_sha:
        raise ReleaseReceiptError("Release receipt appcast digests disagree.")

    verification = _mapping(receipt.get("verification"), "verification")
    _exact_keys(verification, set(VERIFICATION_FIELDS), "verification")
    if any(verification.get(field) != "passed" for field in VERIFICATION_FIELDS):
        raise ReleaseReceiptError("Every engine verification outcome must be passed.")
    case_ids = _sequence(receipt.get("tier1_case_references"), "tier1_case_references")
    if not case_ids or not all(isinstance(case_id, str) and case_id for case_id in case_ids):
        raise ReleaseReceiptError("Tier 1 case references must contain non-empty strings.")
    if list(case_ids) != sorted(set(cast(list[str], case_ids))):
        raise ReleaseReceiptError("Tier 1 case references must be sorted and unique.")
    if set(case_ids) != set(TIER1_CASE_VERIFICATIONS):
        raise ReleaseReceiptError("Tier 1 case references do not match the explicit receipt verification mappings.")
    for case_id in cast(Sequence[str], case_ids):
        if any(verification.get(field) != "passed" for field in TIER1_CASE_VERIFICATIONS[case_id]):
            raise ReleaseReceiptError(f"Tier 1 case {case_id!r} is missing a required verification outcome.")

    recorded_digest = _sha256(receipt.get("receipt_sha256"), "receipt_sha256")
    if recorded_digest != receipt_sha256(receipt):
        raise ReleaseReceiptError("Release receipt self-digest mismatch.")


def load_validated_checked_receipt(path: Path) -> tuple[Mapping[str, Any], str]:
    try:
        content = path.read_bytes()
    except OSError as error:
        raise ReleaseReceiptError(f"Unable to read checked release receipt at {path}: {error}") from error
    try:
        receipt = _mapping(json.loads(content), "checked release receipt")
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReleaseReceiptError(f"Invalid JSON in checked release receipt at {path}: {error}") from error
    validate_receipt(receipt)
    return receipt, hashlib.sha256(content).hexdigest()


def load_validated_artifact_receipt(
    path: Path,
    expectation: ArtifactReceiptExpectation,
) -> Mapping[str, Any]:
    try:
        content = path.read_bytes()
    except OSError as error:
        raise ReleaseReceiptError(f"Unable to read artifact release receipt at {path}: {error}") from error
    expected_file_digest = _sha256(expectation.receipt_file_sha256, "expected receipt file SHA-256")
    if hashlib.sha256(content).hexdigest() != expected_file_digest:
        raise ReleaseReceiptError("Artifact release receipt file digest does not match the expected digest.")
    try:
        receipt = _mapping(json.loads(content), "artifact release receipt")
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReleaseReceiptError(f"Invalid JSON in artifact release receipt at {path}: {error}") from error
    validate_receipt(receipt)
    if receipt.get("receipt_sha256") != _sha256(expectation.receipt_sha256, "expected receipt self SHA-256"):
        raise ReleaseReceiptError("Artifact release receipt self-digest does not match the expected digest.")

    candidate_sha = _sha(expectation.candidate_sha, "expected candidate SHA")
    if receipt.get("source_sha") != candidate_sha:
        raise ReleaseReceiptError("Artifact release receipt is stale or bound to the wrong candidate SHA.")
    release_route = _string(expectation.release_route, "expected release route")
    if release_route not in ROUTES:
        raise ReleaseReceiptError(f"Expected release route must be one of {sorted(ROUTES)}.")
    if receipt.get("release_route") != release_route:
        raise ReleaseReceiptError("Artifact release receipt is bound to the wrong release route.")

    workflow = _mapping(receipt.get("workflow"), "workflow")
    expected_run_id = _integer(expectation.workflow_run_id, "expected workflow run ID")
    expected_run_attempt = _integer(expectation.workflow_run_attempt, "expected workflow run attempt")
    if workflow.get("run_id") != expected_run_id or workflow.get("run_attempt") != expected_run_attempt:
        raise ReleaseReceiptError("Artifact release receipt is not bound to the exact workflow run and attempt.")

    release = _mapping(receipt.get("release"), "release")
    if release.get("id") != _integer(expectation.release_id, "expected release ID"):
        raise ReleaseReceiptError("Artifact release receipt is bound to the wrong GitHub release ID.")
    expected_release_tag = _string(expectation.release_tag, "expected release tag")
    if release.get("tag") != expected_release_tag or release.get("name") != expected_release_tag:
        raise ReleaseReceiptError("Artifact release receipt tag or name does not match the committed release metadata.")
    versions = _mapping(receipt.get("versions"), "versions")
    expected_versions = {
        "package": _string(expectation.package_version, "expected package version"),
        "public": _string(expectation.public_version, "expected public version"),
        "build": _string(expectation.build_version, "expected build version"),
    }
    if any(versions.get(field) != value for field, value in expected_versions.items()):
        raise ReleaseReceiptError("Artifact release receipt versions do not match the committed release metadata.")
    expected_app_tree_digest = _sha256(
        expectation.signed_app_tree_sha256,
        "expected signed app tree SHA-256",
    )
    if receipt.get("signed_app_tree_sha256") != expected_app_tree_digest:
        raise ReleaseReceiptError("Artifact release receipt signed app tree digest does not match.")

    expected_artifacts = {
        "dmg": (
            _integer(expectation.dmg_asset_id, "expected DMG asset ID"),
            _sha256(expectation.dmg_sha256, "expected DMG SHA-256"),
        ),
        "checksum": (
            _integer(expectation.checksum_asset_id, "expected checksum asset ID"),
            _sha256(expectation.checksum_sha256, "expected checksum SHA-256"),
        ),
        "appcast": (
            _integer(expectation.appcast_asset_id, "expected appcast asset ID"),
            _sha256(expectation.appcast_sha256, "expected appcast SHA-256"),
        ),
    }
    artifacts = {
        cast(str, artifact["kind"]): artifact
        for artifact in (
            _mapping(raw_artifact, "artifact") for raw_artifact in _sequence(receipt.get("artifacts"), "artifacts")
        )
    }
    for kind, (expected_asset_id, expected_digest) in expected_artifacts.items():
        artifact = artifacts[kind]
        if artifact.get("asset_id") != expected_asset_id:
            raise ReleaseReceiptError(f"Artifact release receipt {kind} asset ID does not match.")
        if artifact.get("sha256") != expected_digest:
            raise ReleaseReceiptError(f"Artifact release receipt {kind} digest does not match.")
    if artifacts["dmg"].get("name") != _string(expectation.dmg_name, "expected DMG name"):
        raise ReleaseReceiptError("Artifact release receipt DMG name does not match the committed release metadata.")
    if artifacts["dmg"].get("size_bytes") != _integer(expectation.dmg_size, "expected DMG size"):
        raise ReleaseReceiptError("Artifact release receipt DMG size does not match the committed release metadata.")
    return receipt


def write_receipt(receipt: Mapping[str, Any], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build and validate deterministic public-safe release receipts.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    build_parser = subparsers.add_parser("build", help="Build a release receipt from verified workflow facts.")
    build_parser.add_argument("--facts", type=Path, required=True)
    build_parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY_PATH)
    build_parser.add_argument("--output", type=Path, required=True)

    validate_parser = subparsers.add_parser("validate", help="Validate an existing release receipt.")
    validate_parser.add_argument("--receipt", type=Path, required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            receipt = build_receipt(_load_json(args.facts, "release receipt facts"), args.policy)
            write_receipt(receipt, args.output)
        else:
            validate_receipt(_load_json(args.receipt, "release receipt"))
    except ReleaseReceiptError as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
