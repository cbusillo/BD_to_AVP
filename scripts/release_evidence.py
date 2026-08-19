from __future__ import annotations

import argparse
import hashlib
import json
import re
import tempfile

from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from scripts.release import CUT_PACKET_PREPARED, CUT_PACKET_PUBLISHED, CUT_PACKET_RECOVERY_PENDING
from scripts.release_receipt import (
    EXPECTED_BRANCH,
    EXPECTED_REPOSITORY,
    RECEIPT_ASSET_NAME,
    ROUTES,
    ReleaseReceiptError,
    file_sha256,
    validate_receipt,
)


EVIDENCE_INDEX_PATH = Path("docs/qualification/release-evidence-v1.json")
RELEASE_LEDGER_PATH = Path("docs/release-evidence/index-v1.json")
QUALIFICATION_RECORD_NAME = "qualification-record.json"
QUALIFICATION_RECORD_KEYS = {
    "acceptance",
    "candidate",
    "execution_policy",
    "immutable_history",
    "issues",
    "matrix",
    "prior_evidence",
    "qualification_id",
    "qualification_policy",
    "schema_version",
    "status",
}
QUALIFICATION_RECORD_OPTIONAL_KEYS = {"immutable_publication"}
QUALIFICATION_CANDIDATE_KEYS = {
    "appcast_sha256",
    "build_version",
    "dmg_name",
    "dmg_sha256",
    "mapping_version",
    "package_version",
    "public_version",
    "release_id",
    "release_run_id",
    "release_tag",
    "route_table_id",
    "route_table_sha256",
    "signed_app_tree_sha256",
    "source_git_sha",
    "worker_protocol_version",
    "workflow",
}
QUALIFICATION_ACCEPTANCE_REQUIRED_KEYS = {
    "blocking_case_ids",
    "nonblocking_case_ids",
    "passed",
    "preregistered_matrix_case_ids",
    "required_case_ids",
}
QUALIFICATION_ACCEPTANCE_ALLOWED_KEYS = QUALIFICATION_ACCEPTANCE_REQUIRED_KEYS | {
    "carried_case_count",
    "failed_case_count",
    "field_case_open",
    "milestone_complete",
    "passed_case_count",
    "signed_rc_complete",
}
QUALIFICATION_EXECUTION_POLICY_KEYS = {
    "approval_command",
    "approval_required_exit_code",
    "field_qualification_timing",
    "generic_run_waiter_is_sufficient",
    "macos_signing_requires_fresh_explicit_run_bound_authorization",
    "main_must_remain_fixed_while_nonterminal",
    "release_stage",
    "watch_command",
}
QUALIFICATION_MATRIX_REQUIRED_KEYS = {"id", "migration", "phase", "policy_case_id"}
QUALIFICATION_MATRIX_ALLOWED_KEYS = QUALIFICATION_MATRIX_REQUIRED_KEYS | {
    "evidence",
    "result",
    "retest_reason",
}
QUALIFICATION_HISTORY_RELEASE_KEYS = {
    "appcast_sha256",
    "build_version",
    "dmg_sha256",
    "must_not_rebuild",
    "package_version",
    "published_at",
    "release_id",
    "release_run_id",
    "release_tag",
    "signed_app_tree_sha256",
    "source_git_sha",
}
QUALIFICATION_IMMUTABLE_PUBLICATION_REQUIRED_KEYS = {
    "dmg_size_bytes",
    "must_not_rebuild",
    "published_at",
    "receipt",
    "result",
}
QUALIFICATION_IMMUTABLE_PUBLICATION_ALLOWED_KEYS = QUALIFICATION_IMMUTABLE_PUBLICATION_REQUIRED_KEYS | {
    "blocking_case_ids",
    "failed_case_ids",
    "outstanding_case_ids",
    "targeted_qualification",
}
PRIVATE_QUALIFICATION_FIELD_FRAGMENTS = {
    "address",
    "certificate",
    "contact",
    "email",
    "hostname",
    "keychain",
    "password",
    "private",
    "secret",
    "serial",
    "token",
    "username",
}
QUALIFICATION_HISTORY_KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
ISSUE_REFERENCE_PATTERN = re.compile(r"^#[1-9][0-9]*$")


class ReleaseEvidenceError(RuntimeError):
    pass


def effective_successful_workflow_run_id(
    record: Mapping[str, Any],
    *,
    run_id_field: str = "workflow_run_id",
) -> int | None:
    run_id = record.get(run_id_field)
    if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id <= 0:
        return None
    if record.get("workflow_conclusion") == "success":
        return run_id
    if record.get("workflow_conclusion") != "failure":
        return None
    recovery = record.get("recovery_workflow_run")
    if not isinstance(recovery, Mapping) or recovery.get("operation") != "pypi_recovery":
        return None
    recovery_run_id = recovery.get("workflow_run_id")
    if (
        recovery.get("workflow_conclusion") != "success"
        or isinstance(recovery_run_id, bool)
        or not isinstance(recovery_run_id, int)
        or recovery_run_id <= 0
        or recovery_run_id == run_id
    ):
        return None
    return recovery_run_id


def _mapping(value: object, description: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ReleaseEvidenceError(f"{description} must be a JSON object.")
    return cast(Mapping[str, Any], value)


def _sequence(value: object, description: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise ReleaseEvidenceError(f"{description} must be a JSON array.")
    return value


def _string(value: object, description: str) -> str:
    if not isinstance(value, str) or not value:
        raise ReleaseEvidenceError(f"{description} must be a non-empty string.")
    return value


def _integer(value: object, description: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ReleaseEvidenceError(f"{description} must be a positive integer.")
    return value


def _load_json(path: Path, description: str) -> Mapping[str, Any]:
    try:
        return _mapping(json.loads(path.read_text(encoding="utf-8")), description)
    except OSError as error:
        raise ReleaseEvidenceError(f"Unable to read {description} at {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise ReleaseEvidenceError(f"Invalid JSON in {description} at {path}: {error}") from error


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("wb", dir=path.parent, delete=False) as temporary:
        temporary.write(content)
        temporary_path = Path(temporary.name)
    temporary_path.replace(path)


def _atomic_write_text(path: Path, content: str) -> None:
    _atomic_write_bytes(path, content.encode())


def _write_json(path: Path, value: object) -> None:
    _atomic_write_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def _parse_timestamp(value: object, description: str) -> str:
    text = _string(value, description)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ReleaseEvidenceError(f"{description} must be an ISO-8601 timestamp.") from error
    if parsed.tzinfo is None:
        raise ReleaseEvidenceError(f"{description} must include a timezone.")
    return text


def _artifact(receipt: Mapping[str, Any], kind: str) -> Mapping[str, Any]:
    matches = [
        _mapping(item, "release receipt artifact")
        for item in _sequence(receipt.get("artifacts"), "release receipt artifacts")
        if _mapping(item, "release receipt artifact").get("kind") == kind
    ]
    if len(matches) != 1:
        raise ReleaseEvidenceError(f"Release receipt must contain one {kind} artifact.")
    return matches[0]


def validate_publication(
    workflow_run: Mapping[str, Any],
    release: Mapping[str, Any],
    receipt: Mapping[str, Any],
    receipt_path: Path,
    live_appcast_path: Path,
    recovery_workflow_run: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        validate_receipt(receipt)
    except ReleaseReceiptError as error:
        raise ReleaseEvidenceError(str(error)) from error

    route = _string(receipt.get("release_route"), "release route")
    expected_workflow_name, expected_workflow_path = ROUTES[route]
    workflow = _mapping(receipt.get("workflow"), "receipt workflow")
    source_sha = _string(receipt.get("source_sha"), "receipt source_sha")
    if workflow_run.get("name") != expected_workflow_name or workflow_run.get("path") != expected_workflow_path:
        raise ReleaseEvidenceError("Completed workflow identity does not match the release route.")
    if workflow_run.get("event") != "workflow_dispatch":
        raise ReleaseEvidenceError("Evidence reconciliation requires a workflow_dispatch release run.")
    if workflow_run.get("status") != "completed":
        raise ReleaseEvidenceError("Evidence reconciliation requires a completed release run.")
    workflow_conclusion = workflow_run.get("conclusion")
    recovery_workflow_run_id: int | None = None
    if workflow_conclusion != "success":
        if workflow_conclusion != "failure" or recovery_workflow_run is None:
            raise ReleaseEvidenceError("Evidence reconciliation requires a successful release or checked recovery run.")
        from scripts.stable_pypi_recovery import StablePyPIRecoveryError, load_evidence

        try:
            recovery_evidence = load_evidence()
        except StablePyPIRecoveryError as error:
            raise ReleaseEvidenceError(str(error)) from error
        expected_failed_run = _mapping(recovery_evidence.get("failed_run"), "Stable PyPI recovery failed run")
        expected_release = _mapping(recovery_evidence.get("release"), "Stable PyPI recovery release")
        if (
            route != "stable"
            or workflow_run.get("id") != expected_failed_run.get("id")
            or workflow_run.get("head_sha") != expected_release.get("source_sha")
            or _mapping(receipt.get("release"), "receipt release").get("tag") != expected_release.get("tag")
        ):
            raise ReleaseEvidenceError("Failed release run does not match the reviewed Stable PyPI recovery evidence.")
        expected_recovery_identity = {
            "name": "Stable PyPI recovery",
            "path": ".github/workflows/briefcase.yml",
            "event": "workflow_dispatch",
            "status": "completed",
            "conclusion": "success",
            "head_branch": EXPECTED_BRANCH,
            "display_title": "Stable PyPI recovery",
        }
        for field, expected in expected_recovery_identity.items():
            if recovery_workflow_run.get(field) != expected:
                raise ReleaseEvidenceError(f"Checked PyPI recovery run has unexpected {field}.")
        for actor_field in ("actor", "triggering_actor"):
            actor = _mapping(recovery_workflow_run.get(actor_field), f"recovery workflow run {actor_field}")
            if actor.get("login") != "shiny-code-bot":
                raise ReleaseEvidenceError(f"Recovery workflow run {actor_field} is not the approved release actor.")
        recovery_repository = _mapping(recovery_workflow_run.get("repository"), "recovery workflow repository")
        if recovery_repository.get("full_name") != EXPECTED_REPOSITORY:
            raise ReleaseEvidenceError("Recovery workflow run repository is not canonical.")
        recovery_workflow_run_id = _integer(recovery_workflow_run.get("id"), "recovery workflow run ID")
        if recovery_workflow_run_id == workflow_run.get("id"):
            raise ReleaseEvidenceError("Recovery workflow run must differ from the failed release run.")
    elif recovery_workflow_run is not None:
        raise ReleaseEvidenceError("A successful release run must not be reconciled through recovery mode.")
    if workflow_run.get("head_branch") != EXPECTED_BRANCH or workflow_run.get("head_sha") != source_sha:
        raise ReleaseEvidenceError("Completed release run is not bound to the receipt's protected-main SHA.")
    if workflow_run.get("id") != workflow.get("run_id") or workflow_run.get("run_attempt") != workflow.get(
        "run_attempt"
    ):
        raise ReleaseEvidenceError("Completed release run ID or attempt does not match the receipt.")
    for actor_field in ("actor", "triggering_actor"):
        actor = _mapping(workflow_run.get(actor_field), f"workflow run {actor_field}")
        if actor.get("login") != "shiny-code-bot":
            raise ReleaseEvidenceError(f"Workflow run {actor_field} is not the approved release actor.")
    repository = _mapping(workflow_run.get("repository"), "workflow run repository")
    if repository.get("full_name") != EXPECTED_REPOSITORY:
        raise ReleaseEvidenceError("Workflow run repository is not canonical.")

    receipt_release = _mapping(receipt.get("release"), "receipt release")
    release_id = _integer(receipt_release.get("id"), "receipt release id")
    if release.get("id") != release_id:
        raise ReleaseEvidenceError("Published release ID does not match the receipt.")
    if release.get("tag_name") != receipt_release.get("tag") or release.get("name") != receipt_release.get("name"):
        raise ReleaseEvidenceError("Published release tag or name does not match the receipt.")
    if release.get("target_commitish") != source_sha:
        raise ReleaseEvidenceError("Published release target does not match the receipt source SHA.")
    if release.get("immutable") is not True:
        raise ReleaseEvidenceError("Published release is not protected by GitHub release immutability.")
    if release.get("draft") is not False or release.get("prerelease") is not receipt_release.get("prerelease"):
        raise ReleaseEvidenceError("Release is mutable or its prerelease state conflicts with the receipt.")
    published_at = _parse_timestamp(release.get("published_at"), "release published_at")

    release_assets = _sequence(release.get("assets"), "release assets")
    expected_names = {
        _string(_artifact(receipt, "dmg").get("name"), "DMG name"),
        _string(_artifact(receipt, "checksum").get("name"), "checksum name"),
        _string(_artifact(receipt, "appcast").get("name"), "appcast name"),
        RECEIPT_ASSET_NAME,
    }
    names = [_string(_mapping(asset, "release asset").get("name"), "release asset name") for asset in release_assets]
    if len(names) != len(expected_names) or set(names) != expected_names:
        raise ReleaseEvidenceError("Published release asset set is incomplete or conflicting.")
    for kind in ("dmg", "checksum", "appcast"):
        recorded = _artifact(receipt, kind)
        matches = [
            _mapping(asset, "release asset")
            for asset in release_assets
            if _mapping(asset, "release asset").get("name") == recorded.get("name")
        ]
        if len(matches) != 1:
            raise ReleaseEvidenceError(f"Published release does not contain exactly one {kind} asset.")
        actual = matches[0]
        if actual.get("id") != recorded.get("asset_id") or actual.get("size") != recorded.get("size_bytes"):
            raise ReleaseEvidenceError(f"Published {kind} asset identity differs from the receipt.")
        if actual.get("digest") != f"sha256:{recorded.get('sha256')}":
            raise ReleaseEvidenceError(f"Published {kind} asset digest differs from the receipt.")

    receipt_assets = [
        _mapping(asset, "release receipt asset")
        for asset in release_assets
        if _mapping(asset, "release asset").get("name") == RECEIPT_ASSET_NAME
    ]
    receipt_file_digest = file_sha256(receipt_path)
    if len(receipt_assets) != 1 or receipt_assets[0].get("size") != receipt_path.stat().st_size:
        raise ReleaseEvidenceError("Published receipt asset size does not match the downloaded receipt.")
    if receipt_assets[0].get("digest") != f"sha256:{receipt_file_digest}":
        raise ReleaseEvidenceError("Published receipt asset digest does not match the downloaded receipt.")
    live_appcast_sha256 = hashlib.sha256(live_appcast_path.read_bytes()).hexdigest()
    appcast = _mapping(receipt.get("appcast"), "receipt appcast")
    if live_appcast_sha256 != appcast.get("live_pages_sha256"):
        raise ReleaseEvidenceError("Live Pages appcast does not match the verified release appcast.")

    return {
        "published_at": published_at,
        "receipt_asset_id": _integer(receipt_assets[0].get("id"), "receipt asset id"),
        "receipt_file_sha256": receipt_file_digest,
        "live_appcast_sha256": live_appcast_sha256,
        "original_workflow_conclusion": workflow_conclusion,
        "recovery_workflow_run_id": recovery_workflow_run_id,
    }


def _merge_unique_record(records: list[dict[str, Any]], record: dict[str, Any], key: str) -> None:
    matches = [existing for existing in records if existing.get(key) == record[key]]
    if not matches:
        records.append(record)
        return
    if len(matches) != 1 or matches[0] != record:
        raise ReleaseEvidenceError(f"Existing immutable record {record[key]!r} conflicts with this publication.")


def _update_qualification(repo_root: Path, receipt: Mapping[str, Any]) -> Path:
    release = _mapping(receipt.get("release"), "receipt release")
    tag = _string(release.get("tag"), "release tag")
    candidates: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted((repo_root / "docs" / "qualification").glob("*-signed-qualification-v1.json")):
        document = dict(_load_json(path, "signed qualification"))
        candidate = document.get("candidate")
        if isinstance(candidate, Mapping) and candidate.get("release_tag") == tag:
            candidates.append((path, document))
    if len(candidates) != 1:
        raise ReleaseEvidenceError(
            f"Expected exactly one signed qualification record for {tag}; found {len(candidates)}."
        )
    path, document = candidates[0]
    candidate = dict(_mapping(document.get("candidate"), "qualification candidate"))
    updates = {
        "appcast_sha256": _artifact(receipt, "appcast")["sha256"],
        "dmg_sha256": _artifact(receipt, "dmg")["sha256"],
        "release_id": release["id"],
        "release_run_id": _mapping(receipt.get("workflow"), "receipt workflow")["run_id"],
        "signed_app_tree_sha256": receipt["signed_app_tree_sha256"],
        "source_git_sha": receipt["source_sha"],
    }
    for field, value in updates.items():
        if field not in candidate:
            continue
        if candidate[field] not in (None, value):
            raise ReleaseEvidenceError(f"Qualification field candidate.{field} conflicts with immutable receipt data.")
        candidate[field] = value
    document["candidate"] = candidate
    _write_json(path, document)
    return path


def validate_qualification_record_identity(
    qualification: Mapping[str, Any],
    receipt: Mapping[str, Any],
) -> None:
    candidate = _mapping(qualification.get("candidate"), "qualification candidate")
    release = _mapping(receipt.get("release"), "receipt release")
    versions = _mapping(receipt.get("versions"), "receipt versions")
    workflow = _mapping(receipt.get("workflow"), "receipt workflow")
    expected = {
        "appcast_sha256": _artifact(receipt, "appcast")["sha256"],
        "build_version": versions["build"],
        "dmg_name": _artifact(receipt, "dmg")["name"],
        "dmg_sha256": _artifact(receipt, "dmg")["sha256"],
        "package_version": versions["package"],
        "public_version": versions["public"],
        "release_id": release["id"],
        "release_run_id": workflow["run_id"],
        "release_tag": release["tag"],
        "signed_app_tree_sha256": receipt["signed_app_tree_sha256"],
        "source_git_sha": receipt["source_sha"],
        "workflow": workflow["name"],
    }
    for field, value in expected.items():
        if candidate.get(field) != value:
            raise ReleaseEvidenceError(
                f"Qualification record candidate.{field} conflicts with exact release receipt identity."
            )


def _reject_private_qualification_fields(value: object, path: str = "qualification") -> None:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            if not isinstance(raw_key, str) or not raw_key:
                raise ReleaseEvidenceError(f"{path} contains a non-string key.")
            lowered = raw_key.lower()
            if any(fragment in lowered for fragment in PRIVATE_QUALIFICATION_FIELD_FRAGMENTS):
                raise ReleaseEvidenceError(f"{path}.{raw_key} is not a public-safe qualification field name.")
            _reject_private_qualification_fields(child, f"{path}.{raw_key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_private_qualification_fields(child, f"{path}[{index}]")
    elif isinstance(value, str) and (value.startswith("/") or value.startswith("~/")):
        raise ReleaseEvidenceError(f"{path} contains an absolute or home-relative path.")


def _validate_object_keys(
    value: Mapping[str, Any],
    *,
    required: set[str],
    allowed: set[str],
    description: str,
) -> None:
    actual = set(value)
    missing = required - actual
    extra = actual - allowed
    if missing or extra:
        raise ReleaseEvidenceError(f"{description} keys changed; missing={sorted(missing)}, extra={sorted(extra)}.")


def _validate_string_list(value: object, description: str) -> None:
    for item in _sequence(value, description):
        _string(item, description)


def _validate_boolean(value: object, description: str) -> None:
    if not isinstance(value, bool):
        raise ReleaseEvidenceError(f"{description} must be boolean.")


def _validate_nonnegative_integer(value: object, description: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ReleaseEvidenceError(f"{description} must be a nonnegative integer.")


def _validate_repository_relative_path(value: object, description: str) -> None:
    text = _string(value, description)
    path = Path(text)
    if path.is_absolute() or ".." in path.parts or text.startswith("./"):
        raise ReleaseEvidenceError(f"{description} must be a canonical repository-relative path.")


def _validate_qualification_sections(qualification: Mapping[str, Any]) -> None:
    acceptance = _mapping(qualification.get("acceptance"), "qualification acceptance")
    _validate_object_keys(
        acceptance,
        required=QUALIFICATION_ACCEPTANCE_REQUIRED_KEYS,
        allowed=QUALIFICATION_ACCEPTANCE_ALLOWED_KEYS,
        description="Qualification acceptance",
    )
    for field in (
        "blocking_case_ids",
        "nonblocking_case_ids",
        "preregistered_matrix_case_ids",
        "required_case_ids",
    ):
        _validate_string_list(acceptance.get(field), f"qualification acceptance {field}")
    for field in ("field_case_open", "milestone_complete", "passed", "signed_rc_complete"):
        if field in acceptance:
            _validate_boolean(acceptance[field], f"qualification acceptance {field}")
    for field in ("carried_case_count", "failed_case_count", "passed_case_count"):
        if field in acceptance:
            _validate_nonnegative_integer(acceptance[field], f"qualification acceptance {field}")

    execution_policy = _mapping(qualification.get("execution_policy"), "qualification execution policy")
    _validate_object_keys(
        execution_policy,
        required=QUALIFICATION_EXECUTION_POLICY_KEYS,
        allowed=QUALIFICATION_EXECUTION_POLICY_KEYS,
        description="Qualification execution policy",
    )
    for field in ("approval_command", "field_qualification_timing", "release_stage", "watch_command"):
        _string(execution_policy.get(field), f"qualification execution policy {field}")
    _integer(
        execution_policy.get("approval_required_exit_code"),
        "qualification execution policy approval_required_exit_code",
    )
    for field in (
        "generic_run_waiter_is_sufficient",
        "macos_signing_requires_fresh_explicit_run_bound_authorization",
        "main_must_remain_fixed_while_nonterminal",
    ):
        _validate_boolean(execution_policy.get(field), f"qualification execution policy {field}")

    for index, raw_case in enumerate(_sequence(qualification.get("matrix"), "qualification matrix")):
        case = _mapping(raw_case, f"qualification matrix case {index}")
        _validate_object_keys(
            case,
            required=QUALIFICATION_MATRIX_REQUIRED_KEYS,
            allowed=QUALIFICATION_MATRIX_ALLOWED_KEYS,
            description=f"Qualification matrix case {index}",
        )
        for field in QUALIFICATION_MATRIX_REQUIRED_KEYS:
            _string(case.get(field), f"qualification matrix case {index} {field}")
        if "evidence" in case:
            _validate_repository_relative_path(case["evidence"], f"qualification matrix case {index} evidence")
        for field in ("result", "retest_reason"):
            if field in case:
                _string(case[field], f"qualification matrix case {index} {field}")

    for index, raw_evidence in enumerate(
        _sequence(qualification.get("prior_evidence"), "qualification prior evidence")
    ):
        evidence = _mapping(raw_evidence, f"qualification prior evidence {index}")
        _validate_object_keys(
            evidence,
            required={"id", "scope"},
            allowed={"id", "scope"},
            description=f"Qualification prior evidence {index}",
        )
        _string(evidence.get("id"), f"qualification prior evidence {index} id")
        _string(evidence.get("scope"), f"qualification prior evidence {index} scope")

    for issue in _sequence(qualification.get("issues"), "qualification issues"):
        if ISSUE_REFERENCE_PATTERN.fullmatch(_string(issue, "qualification issue")) is None:
            raise ReleaseEvidenceError("Qualification issues must use canonical GitHub issue references.")

    history = _mapping(qualification.get("immutable_history"), "qualification immutable history")
    for history_key, raw_history in history.items():
        if not isinstance(history_key, str) or QUALIFICATION_HISTORY_KEY_PATTERN.fullmatch(history_key) is None:
            raise ReleaseEvidenceError("Qualification immutable history keys must use canonical identifiers.")
        if isinstance(raw_history, list):
            for build in raw_history:
                _integer(build, f"qualification immutable history {history_key} build")
            continue
        release_history = _mapping(raw_history, f"qualification immutable history {history_key}")
        _validate_object_keys(
            release_history,
            required=QUALIFICATION_HISTORY_RELEASE_KEYS,
            allowed=QUALIFICATION_HISTORY_RELEASE_KEYS,
            description=f"Qualification immutable history {history_key}",
        )
        for field in ("release_id", "release_run_id"):
            _integer(release_history.get(field), f"qualification immutable history {history_key} {field}")
        for field in QUALIFICATION_HISTORY_RELEASE_KEYS - {"release_id", "release_run_id", "must_not_rebuild"}:
            _string(release_history.get(field), f"qualification immutable history {history_key} {field}")
        _validate_boolean(
            release_history.get("must_not_rebuild"),
            f"qualification immutable history {history_key} must_not_rebuild",
        )

    if "immutable_publication" in qualification:
        publication = _mapping(qualification["immutable_publication"], "qualification immutable publication")
        _validate_object_keys(
            publication,
            required=QUALIFICATION_IMMUTABLE_PUBLICATION_REQUIRED_KEYS,
            allowed=QUALIFICATION_IMMUTABLE_PUBLICATION_ALLOWED_KEYS,
            description="Qualification immutable publication",
        )
        _integer(publication.get("dmg_size_bytes"), "qualification immutable publication dmg_size_bytes")
        _validate_boolean(
            publication.get("must_not_rebuild"),
            "qualification immutable publication must_not_rebuild",
        )
        for field in ("published_at", "result"):
            _string(publication.get(field), f"qualification immutable publication {field}")
        _validate_repository_relative_path(
            publication.get("receipt"),
            "qualification immutable publication receipt",
        )
        if "targeted_qualification" in publication:
            _validate_repository_relative_path(
                publication["targeted_qualification"],
                "qualification immutable publication targeted_qualification",
            )
        for field in ("blocking_case_ids", "failed_case_ids", "outstanding_case_ids"):
            if field in publication:
                _validate_string_list(publication[field], f"qualification immutable publication {field}")


def validate_qualification_record(
    qualification_path: Path,
    receipt: Mapping[str, Any],
    *,
    policy_path: Path,
    route_table_path: Path,
) -> Mapping[str, Any]:
    qualification = _load_json(qualification_path, "qualification record")
    _validate_object_keys(
        qualification,
        required=QUALIFICATION_RECORD_KEYS,
        allowed=QUALIFICATION_RECORD_KEYS | QUALIFICATION_RECORD_OPTIONAL_KEYS,
        description="Qualification record",
    )
    if qualification.get("schema_version") != 1:
        raise ReleaseEvidenceError("Qualification record schema_version must be 1.")
    _string(qualification.get("qualification_id"), "qualification ID")
    _string(qualification.get("status"), "qualification status")
    candidate = _mapping(qualification.get("candidate"), "qualification candidate")
    if set(candidate) != QUALIFICATION_CANDIDATE_KEYS:
        raise ReleaseEvidenceError("Qualification record candidate must use the complete canonical schema.")
    _reject_private_qualification_fields(qualification)
    _validate_qualification_sections(qualification)
    validate_qualification_record_identity(qualification, receipt)

    try:
        from scripts.qualify_release_scope import (
            QualificationScopeError,
            load_policy,
            load_qualification_overrides,
        )

        policy = load_policy(policy_path)
        load_qualification_overrides(qualification_path, policy)
    except QualificationScopeError as error:
        raise ReleaseEvidenceError(f"Qualification record is invalid: {error}") from error

    qualification_policy = _mapping(qualification.get("qualification_policy"), "qualification policy binding")
    if set(qualification_policy) != {"id", "path", "scope_command"}:
        raise ReleaseEvidenceError("Qualification policy binding must use the canonical schema.")
    if qualification_policy.get("id") != policy.get("policy_id"):
        raise ReleaseEvidenceError("Qualification policy binding conflicts with the validated policy.")
    policy_reference = _string(qualification_policy.get("path"), "qualification policy path")
    if policy_reference != "docs/qualification/release-qualification-policy-v1.json":
        raise ReleaseEvidenceError("Qualification policy path must use the canonical repository path.")
    _string(qualification_policy.get("scope_command"), "qualification scope command")

    route_table = _load_json(route_table_path, "qualification route table")
    route_table_digest = hashlib.sha256(route_table_path.read_bytes()).hexdigest()
    if candidate.get("route_table_id") != route_table.get("route_table_id"):
        raise ReleaseEvidenceError("Qualification candidate route_table_id conflicts with the route table.")
    if candidate.get("route_table_sha256") != route_table_digest:
        raise ReleaseEvidenceError("Qualification candidate route_table_sha256 conflicts with the route table.")
    if candidate.get("mapping_version") != route_table.get("mapping_version"):
        raise ReleaseEvidenceError("Qualification candidate mapping_version conflicts with the route table.")
    _integer(candidate.get("worker_protocol_version"), "qualification worker protocol version")
    return qualification


def _snapshot_qualification(
    repo_root: Path,
    release_tag: str,
    qualification_path: Path,
    receipt: Mapping[str, Any],
) -> Path:
    validate_qualification_record(
        qualification_path,
        receipt,
        policy_path=repo_root / "docs/qualification/release-qualification-policy-v1.json",
        route_table_path=repo_root / "docs/qualification/video-quality-route-table-v2.json",
    )
    qualification = _load_json(qualification_path, "signed qualification")
    snapshot_path = repo_root / "docs" / "release-evidence" / release_tag / QUALIFICATION_RECORD_NAME
    content = (json.dumps(qualification, indent=2, sort_keys=True) + "\n").encode()
    if snapshot_path.exists():
        if snapshot_path.read_bytes() != content:
            raise ReleaseEvidenceError(f"Checked qualification record for immutable release {release_tag} conflicts.")
        return snapshot_path
    _atomic_write_bytes(snapshot_path, content)
    return snapshot_path


def _update_cut_packet(repo_root: Path, receipt: Mapping[str, Any], publication: Mapping[str, Any]) -> Path:
    versions = _mapping(receipt.get("versions"), "receipt versions")
    path = repo_root / "docs" / f"{_string(versions.get('public'), 'public version')}-cut-packet.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ReleaseEvidenceError(f"Unable to read release cut packet at {path}: {error}") from error
    if CUT_PACKET_PREPARED in text:
        text = text.replace(CUT_PACKET_PREPARED, CUT_PACKET_PUBLISHED, 1)
    elif CUT_PACKET_RECOVERY_PENDING in text:
        text = text.replace(CUT_PACKET_RECOVERY_PENDING, CUT_PACKET_PUBLISHED, 1)
    elif CUT_PACKET_PUBLISHED not in text:
        raise ReleaseEvidenceError("Release cut packet has no recognized publication state.")
    marker = "<!-- release-evidence-receipt-v1 -->"
    release = _mapping(receipt.get("release"), "receipt release")
    workflow = _mapping(receipt.get("workflow"), "receipt workflow")
    block = (
        f"\n\n{marker}\n"
        "## Immutable Publication Receipt\n\n"
        f"- Source SHA: `{receipt['source_sha']}`\n"
        f"- Release workflow run: `{workflow['run_id']}` attempt `{workflow['run_attempt']}`\n"
        f"- GitHub release ID: `{release['id']}`\n"
        f"- Published at: `{publication['published_at']}`\n"
        f"- DMG SHA-256: `{_artifact(receipt, 'dmg')['sha256']}`\n"
        f"- Signed app-tree SHA-256: `{receipt['signed_app_tree_sha256']}`\n"
        f"- Appcast SHA-256: `{_artifact(receipt, 'appcast')['sha256']}`\n"
        f"- Receipt file SHA-256: `{publication['receipt_file_sha256']}`\n"
    )
    if marker in text:
        prefix = text.split(marker, 1)[0].rstrip()
        text = prefix + block
    else:
        text = text.rstrip() + block
    _atomic_write_text(path, text.rstrip() + "\n")
    return path


def reconcile(
    repo_root: Path,
    workflow_run: Mapping[str, Any],
    release: Mapping[str, Any],
    receipt: Mapping[str, Any],
    receipt_path: Path,
    live_appcast_path: Path,
    recovery_workflow_run: Mapping[str, Any] | None = None,
    *,
    prior_tag: str | None = None,
    signed_ui_artifact_id: int | None = None,
    signed_ui_artifact_digest: str | None = None,
    signed_ui_archive_path: Path | None = None,
    signed_ui_receipt_path: Path | None = None,
    signed_ui_receipt_file_sha256: str | None = None,
    evidence_ref: str | None = None,
    evidence_base_sha: str | None = None,
    runner_sha: str | None = None,
    sparkle_route: str | None = None,
) -> dict[str, Any]:
    manifest_inputs = {
        "prior_tag": prior_tag,
        "signed_ui_artifact_id": signed_ui_artifact_id,
        "signed_ui_artifact_digest": signed_ui_artifact_digest,
        "signed_ui_archive_path": signed_ui_archive_path,
        "signed_ui_receipt_path": signed_ui_receipt_path,
        "signed_ui_receipt_file_sha256": signed_ui_receipt_file_sha256,
        "evidence_ref": evidence_ref,
        "evidence_base_sha": evidence_base_sha,
        "runner_sha": runner_sha,
        "sparkle_route": sparkle_route,
    }
    supplied_manifest_inputs = {key for key, value in manifest_inputs.items() if value is not None}
    if supplied_manifest_inputs and supplied_manifest_inputs != set(manifest_inputs):
        missing = sorted(set(manifest_inputs) - supplied_manifest_inputs)
        raise ReleaseEvidenceError(f"Qualification manifest inputs are partial; missing={missing}.")
    publication = validate_publication(
        workflow_run,
        release,
        receipt,
        receipt_path,
        live_appcast_path,
        recovery_workflow_run,
    )
    release_data = _mapping(receipt.get("release"), "receipt release")
    workflow = _mapping(receipt.get("workflow"), "receipt workflow")
    tag = _string(release_data.get("tag"), "release tag")
    evidence_directory = repo_root / "docs" / "release-evidence" / tag
    checked_receipt = evidence_directory / RECEIPT_ASSET_NAME
    if checked_receipt.exists() and checked_receipt.read_bytes() != receipt_path.read_bytes():
        raise ReleaseEvidenceError(f"Checked receipt for immutable release {tag} conflicts with the published asset.")
    _atomic_write_bytes(checked_receipt, receipt_path.read_bytes())
    recovery_workflow_run = (
        {
            "operation": "pypi_recovery",
            "workflow_conclusion": "success",
            "workflow_run_id": publication["recovery_workflow_run_id"],
        }
        if publication["recovery_workflow_run_id"] is not None
        else None
    )

    publication_record = {
        "live_pages": {
            "sha256": publication["live_appcast_sha256"],
            "state": "verified",
            "url": _mapping(receipt.get("appcast"), "receipt appcast")["live_pages_url"],
        },
        "published_at": publication["published_at"],
        "receipt_asset_id": publication["receipt_asset_id"],
        "receipt_file_sha256": publication["receipt_file_sha256"],
        "release_id": release_data["id"],
        "release_tag": tag,
        "schema_version": 1,
        "source_sha": receipt["source_sha"],
        "workflow_conclusion": publication["original_workflow_conclusion"],
        "workflow_run_id": workflow["run_id"],
    }
    if recovery_workflow_run is not None:
        publication_record["recovery_workflow_run"] = recovery_workflow_run
    publication_path = evidence_directory / "publication-record.json"
    if publication_path.exists() and _load_json(publication_path, "publication record") != publication_record:
        raise ReleaseEvidenceError(f"Checked publication record for immutable release {tag} conflicts.")
    _write_json(publication_path, publication_record)

    ledger_path = repo_root / RELEASE_LEDGER_PATH
    ledger = (
        dict(_load_json(ledger_path, "release evidence ledger"))
        if ledger_path.exists()
        else {"schema_version": 1, "releases": []}
    )
    if ledger.get("schema_version") != 1:
        raise ReleaseEvidenceError("Release evidence ledger schema_version must be 1.")
    releases = [
        dict(_mapping(item, "release ledger record"))
        for item in _sequence(ledger.get("releases"), "release ledger releases")
    ]
    ledger_record = {
        "publication_record": publication_path.relative_to(repo_root).as_posix(),
        "published_at": publication["published_at"],
        "receipt": checked_receipt.relative_to(repo_root).as_posix(),
        "receipt_file_sha256": publication["receipt_file_sha256"],
        "release_id": release_data["id"],
        "source_sha": receipt["source_sha"],
        "tag": tag,
        "workflow_run_id": workflow["run_id"],
    }
    if recovery_workflow_run is not None:
        ledger_record["recovery_workflow_run"] = recovery_workflow_run
    _merge_unique_record(releases, ledger_record, "tag")
    ledger["releases"] = sorted(releases, key=lambda item: cast(str, item["tag"]))
    _write_json(ledger_path, ledger)

    evidence_path = repo_root / EVIDENCE_INDEX_PATH
    evidence = (
        dict(_load_json(evidence_path, "release qualification evidence"))
        if evidence_path.exists()
        else {"schema_version": 1, "receipts": []}
    )
    if evidence.get("schema_version") != 1:
        raise ReleaseEvidenceError("Release qualification evidence schema_version must be 1.")
    receipts = [
        dict(_mapping(item, "qualification evidence receipt"))
        for item in _sequence(evidence.get("receipts"), "qualification evidence receipts")
    ]
    reference = checked_receipt.relative_to(repo_root).as_posix()
    for case_id in _sequence(receipt.get("tier1_case_references"), "Tier 1 case references"):
        case = _string(case_id, "Tier 1 case ID")
        evidence_record = {
            "accepted_at": publication["published_at"],
            "case_id": case,
            "receipt_id": f"{tag}:{case}",
            "reference": reference,
            "release_run_id": workflow["run_id"],
            "sha256": publication["receipt_file_sha256"],
            "source": "release_run_receipt",
            "source_sha": receipt["source_sha"],
            "status": "accepted",
            "workflow_conclusion": publication["original_workflow_conclusion"],
        }
        if recovery_workflow_run is not None:
            evidence_record["recovery_workflow_run"] = recovery_workflow_run
        _merge_unique_record(receipts, evidence_record, "receipt_id")
    evidence["receipts"] = receipts
    _write_json(evidence_path, evidence)

    qualification_record_path = repo_root / "docs" / "release-evidence" / tag / QUALIFICATION_RECORD_NAME
    if qualification_record_path.exists():
        validate_qualification_record_identity(
            _load_json(qualification_record_path, "qualification record"),
            receipt,
        )
    else:
        qualification_path = _update_qualification(repo_root, receipt)
        qualification_record_path = _snapshot_qualification(repo_root, tag, qualification_path, receipt)
    cut_packet_path = _update_cut_packet(repo_root, receipt, publication)
    outputs = {
        "cut_packet": cut_packet_path.relative_to(repo_root).as_posix(),
        "evidence_index": EVIDENCE_INDEX_PATH.as_posix(),
        "publication_record": publication_path.relative_to(repo_root).as_posix(),
        "qualification": qualification_record_path.relative_to(repo_root).as_posix(),
        "qualification_record": qualification_record_path.relative_to(repo_root).as_posix(),
        "receipt": reference,
        "receipt_file_sha256": publication["receipt_file_sha256"],
        "release_ledger": RELEASE_LEDGER_PATH.as_posix(),
        "release_run_id": workflow["run_id"],
        "release_tag": tag,
        "source_sha": receipt["source_sha"],
    }
    if supplied_manifest_inputs:
        from scripts.release_qualification_manifest import (
            MANIFEST_NAME,
            ReleaseQualificationManifestError,
            build_manifest_for_reconciled_release,
        )

        try:
            manifest = build_manifest_for_reconciled_release(
                repo_root=repo_root,
                release_tag=tag,
                prior_tag=cast(str, prior_tag),
                signed_ui_artifact_id=cast(int, signed_ui_artifact_id),
                signed_ui_artifact_digest=cast(str, signed_ui_artifact_digest),
                signed_ui_archive_source_path=cast(Path, signed_ui_archive_path),
                signed_ui_receipt_source_path=cast(Path, signed_ui_receipt_path),
                signed_ui_receipt_file_sha256=cast(str, signed_ui_receipt_file_sha256),
                evidence_ref=cast(str, evidence_ref),
                evidence_base_sha=cast(str, evidence_base_sha),
                runner_sha=cast(str, runner_sha),
                sparkle_route=cast(str, sparkle_route),
                qualification_path=qualification_record_path.relative_to(repo_root),
            )
        except ReleaseQualificationManifestError as error:
            raise ReleaseEvidenceError(str(error)) from error
        outputs.update(
            {
                "manifest_sha256": manifest["manifest_sha256"],
                "qualification_manifest": (evidence_directory / MANIFEST_NAME).relative_to(repo_root).as_posix(),
                "signed_ui_receipt": (evidence_directory / "signed-artifact-ui-receipt.json")
                .relative_to(repo_root)
                .as_posix(),
                "signed_ui_archive": (evidence_directory / "signed-artifact-ui.zip").relative_to(repo_root).as_posix(),
                "signed_ui_receipt_sha256": _mapping(
                    manifest.get("signed_ui_artifact"),
                    "manifest signed UI artifact",
                )["receipt_sha256"],
                "signed_ui_artifact_sha256": _mapping(
                    manifest.get("signed_ui_artifact"),
                    "manifest signed UI artifact",
                )["artifact_sha256"],
            }
        )
    return outputs


def _write_github_output(path: Path, outputs: Mapping[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for key, value in outputs.items():
            handle.write(f"{key}={value}\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a published release receipt and prepare checked evidence.")
    parser.add_argument("--workflow-run", type=Path, required=True)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--live-appcast", type=Path, required=True)
    parser.add_argument("--recovery-workflow-run", type=Path)
    parser.add_argument("--prior-tag")
    parser.add_argument("--signed-ui-artifact-id", type=int)
    parser.add_argument("--signed-ui-artifact-digest")
    parser.add_argument("--signed-ui-artifact-archive", type=Path)
    parser.add_argument("--signed-ui-receipt", type=Path)
    parser.add_argument("--signed-ui-receipt-file-sha256")
    parser.add_argument("--evidence-ref")
    parser.add_argument("--evidence-base-sha")
    parser.add_argument("--runner-sha")
    parser.add_argument("--sparkle-route")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args(argv)
    try:
        outputs = reconcile(
            args.repo_root.resolve(),
            _load_json(args.workflow_run, "workflow run"),
            _load_json(args.release, "published release"),
            _load_json(args.receipt, "release receipt"),
            args.receipt,
            args.live_appcast,
            (
                _load_json(args.recovery_workflow_run, "recovery workflow run")
                if args.recovery_workflow_run is not None
                else None
            ),
            prior_tag=args.prior_tag,
            signed_ui_artifact_id=args.signed_ui_artifact_id,
            signed_ui_artifact_digest=args.signed_ui_artifact_digest,
            signed_ui_archive_path=args.signed_ui_artifact_archive,
            signed_ui_receipt_path=args.signed_ui_receipt,
            signed_ui_receipt_file_sha256=args.signed_ui_receipt_file_sha256,
            evidence_ref=args.evidence_ref,
            evidence_base_sha=args.evidence_base_sha,
            runner_sha=args.runner_sha,
            sparkle_route=args.sparkle_route,
        )
        if args.github_output is not None:
            _write_github_output(args.github_output, outputs)
        else:
            print(json.dumps(outputs, sort_keys=True))
    except ReleaseEvidenceError as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
