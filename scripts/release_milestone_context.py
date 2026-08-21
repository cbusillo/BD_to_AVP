from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys

from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from scripts.release import ReleaseError, parse_build_version, parse_release_tag, parse_release_version
from scripts.release_evidence import effective_successful_workflow_run_id
from scripts.release_qualification_manifest import (
    MANIFEST_NAME,
    ReleaseQualificationManifestError,
    load_validated_manifest,
)
from scripts.release_receipt import RECEIPT_ASSET_NAME, ReleaseReceiptError, load_validated_checked_receipt


REPO_ROOT = Path(__file__).resolve().parents[1]
GITHUB_CONFIG_PATH = Path(".github/github.json")
EVIDENCE_INDEX_PATH = "docs/qualification/release-evidence-v1.json"
RELEASE_LEDGER_PATH = "docs/release-evidence/index-v1.json"
RUNNER_BOUND_QUALIFICATION_PATHS = {
    "docs/qualification/release-qualification-policy-v1.json",
    "docs/qualification/video-quality-route-table-v2.json",
}
RECEIPT_PATH_PATTERN = re.compile(r"^docs/release-evidence/(v[^/]+)/release-receipt\.json$")
MANIFEST_PATH_PATTERN = re.compile(rf"^docs/release-evidence/(v[^/]+)/{re.escape(MANIFEST_NAME)}$")
CHANGE_SCOPED_EVIDENCE_PATH_PATTERN = re.compile(r"^docs/qualification/(v[^/]+)-change-scoped-evidence-v1\.json$")
FAILED_ATTEMPT_ROOT = Path("docs/release-attempts")
FAILED_ATTEMPT_RECORD_NAME = "failed-attempt-v1.json"
FAILED_ATTEMPT_RECEIPT_NAME = "release-receipt.json"
CANCELLED_ATTEMPT_RECORD_NAME = "cancelled-attempt-v1.json"
RECOVERY_AUTHORIZATION_PATHS = frozenset({"docs/release-evidence/v0.3.0-pypi-recovery.json"})
EXPECTED_REPOSITORY = "cbusillo/BD_to_AVP"
EXPECTED_BASE_BRANCH = "main"
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
CANCELLATION_REASON_PATTERN = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")
IMMUTABLE_CANDIDATE_FIELDS = (
    "source_git_sha",
    "release_run_id",
    "release_id",
    "dmg_sha256",
    "appcast_sha256",
    "signed_app_tree_sha256",
)
FAILED_ATTEMPT_RECORD_KEYS = frozenset(
    {
        "build_version",
        "draft_deleted",
        "draft_release_id",
        "failed_job",
        "failed_job_id",
        "failed_step",
        "must_not_rebuild",
        "package_version",
        "public_version",
        "published_at",
        "recorded_at",
        "release_receipt_file_sha256",
        "release_receipt_path",
        "release_receipt_sha256",
        "release_tag",
        "schema_version",
        "source_sha",
        "status",
        "workflow_conclusion",
        "workflow_run_attempt",
        "workflow_run_id",
    }
)
CANCELLED_ATTEMPT_RECORD_KEYS = frozenset(
    {
        "build_version",
        "cancellation_reason",
        "cancelled_job",
        "cancelled_job_id",
        "cancelled_step",
        "delete_requires_authorization",
        "draft_assets",
        "draft_checked_at",
        "draft_deleted",
        "draft_release_id",
        "draft_state",
        "must_not_rebuild",
        "package_version",
        "public_version",
        "published_at",
        "recorded_at",
        "release_receipt_asset_id",
        "release_receipt_asset_size_bytes",
        "release_receipt_file_sha256",
        "release_receipt_path",
        "release_receipt_sha256",
        "release_tag",
        "schema_version",
        "signing_approval_environment",
        "signing_approval_fingerprint",
        "signing_approval_reviewer",
        "source_sha",
        "status",
        "tag_exists",
        "workflow_completed_at",
        "workflow_conclusion",
        "workflow_run_attempt",
        "workflow_run_id",
    }
)
CANCELLED_ATTEMPT_ASSET_KEYS = frozenset({"asset_id", "kind", "name", "sha256", "size_bytes"})
CANCELLED_ATTEMPT_ASSET_KINDS = frozenset({"appcast", "checksum", "dmg", "receipt"})


class ReleaseMilestoneContextError(RuntimeError):
    pass


PublishedReceiptVerifier = Callable[[Path, Mapping[str, Any], str], None]


@dataclass(frozen=True)
class ReleaseMilestoneContext:
    candidate_sha: str
    evidence_path: str
    first_candidate_of_cycle: bool
    policy_path: str
    qualification_path: str
    release_receipt_path: str
    release_stage: str
    release_tag: str
    manifest_path: str = ""
    manifest_sha256: str = ""
    evidence_index_base_sha256: str = ""
    runner_sha: str = ""
    required: bool = True

    def github_outputs(self) -> dict[str, str]:
        return {
            key: str(value).lower() if isinstance(value, bool) else str(value) for key, value in asdict(self).items()
        }


def _mapping(value: object, description: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ReleaseMilestoneContextError(f"{description} must be a JSON object.")
    return cast(Mapping[str, Any], value)


def _string(value: object, description: str) -> str:
    if not isinstance(value, str) or not value:
        raise ReleaseMilestoneContextError(f"{description} must be a non-empty string.")
    return value


def require_manifest_runner_sha(context: ReleaseMilestoneContext, expected_sha: str) -> None:
    if context.runner_sha != expected_sha:
        raise ReleaseMilestoneContextError(
            "Milestone qualification manifest runner SHA must match the pull-request base SHA."
        )


def require_manifest_evidence_baseline(
    context: ReleaseMilestoneContext,
    repo_root: Path,
    base_sha: str,
) -> None:
    result = subprocess.run(
        ["git", "show", f"{base_sha}:{context.evidence_path}"],
        cwd=repo_root,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise ReleaseMilestoneContextError(
            "Unable to read the qualification evidence baseline from the pull-request base."
        )
    if hashlib.sha256(result.stdout).hexdigest() != context.evidence_index_base_sha256:
        raise ReleaseMilestoneContextError(
            "Milestone qualification manifest evidence baseline must match the pull-request base."
        )


def _load_json(path: Path, description: str) -> Mapping[str, Any]:
    try:
        return _mapping(json.loads(path.read_text(encoding="utf-8")), description)
    except OSError as error:
        raise ReleaseMilestoneContextError(f"Unable to read {description} at {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise ReleaseMilestoneContextError(f"Invalid JSON in {description} at {path}: {error}") from error


def _load_json_at_revision(
    repo_root: Path,
    revision: str,
    relative_path: str,
    description: str,
) -> Mapping[str, Any]:
    result = subprocess.run(
        ["git", "show", f"{revision}:{relative_path}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ReleaseMilestoneContextError(f"Unable to read {description} from the pull-request base.")
    try:
        return _mapping(json.loads(result.stdout), description)
    except json.JSONDecodeError as error:
        raise ReleaseMilestoneContextError(f"Invalid JSON in base {description}: {error}") from error


def _sequence(value: object, description: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise ReleaseMilestoneContextError(f"{description} must be a JSON array.")
    return cast(Sequence[Any], value)


def _positive_integer(value: object, description: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ReleaseMilestoneContextError(f"{description} must be a positive integer.")
    return value


def _sha(value: object, description: str) -> str:
    text = _string(value, description)
    if SHA_PATTERN.fullmatch(text) is None:
        raise ReleaseMilestoneContextError(f"{description} must be a full lowercase Git SHA.")
    return text


def _sha256(value: object, description: str) -> str:
    text = _string(value, description)
    if SHA256_PATTERN.fullmatch(text) is None:
        raise ReleaseMilestoneContextError(f"{description} must be a lowercase SHA-256 digest.")
    return text


def _timestamp(value: object, description: str) -> datetime:
    text = _string(value, description)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ReleaseMilestoneContextError(f"{description} must be an ISO-8601 timestamp.") from error


def validate_cancelled_attempt_record(repo_root: Path, release_tag: str) -> Mapping[str, Any]:
    attempt_root = FAILED_ATTEMPT_ROOT / release_tag
    record_relative = attempt_root / CANCELLED_ATTEMPT_RECORD_NAME
    receipt_relative = attempt_root / FAILED_ATTEMPT_RECEIPT_NAME
    record = _load_json(repo_root / record_relative, "cancelled release-attempt record")
    if set(record) != CANCELLED_ATTEMPT_RECORD_KEYS:
        raise ReleaseMilestoneContextError("Cancelled release-attempt record keys do not match the required schema.")
    if (
        record.get("schema_version") != 1
        or record.get("status") != "cancelled_unpublished_signed_candidate"
        or record.get("workflow_conclusion") != "cancelled"
        or record.get("published_at") is not None
        or record.get("draft_deleted") is not False
        or record.get("draft_state") != "present"
        or record.get("tag_exists") is not False
        or record.get("must_not_rebuild") is not True
        or record.get("delete_requires_authorization") is not True
    ):
        raise ReleaseMilestoneContextError(
            "Cancelled release-attempt record must prove a draft-present, unpublished, "
            "permanently non-reusable candidate."
        )
    cancellation_reason = _string(record.get("cancellation_reason"), "cancelled release reason")
    if CANCELLATION_REASON_PATTERN.fullmatch(cancellation_reason) is None:
        raise ReleaseMilestoneContextError("Cancelled release reason must be a lowercase machine-readable identifier.")
    if record.get("release_tag") != release_tag:
        raise ReleaseMilestoneContextError("Cancelled release-attempt tag does not match its canonical directory.")
    if record.get("release_receipt_path") != receipt_relative.as_posix():
        raise ReleaseMilestoneContextError("Cancelled release-attempt receipt path is not canonical.")
    github_config = _load_json(repo_root / GITHUB_CONFIG_PATH, "GitHub repository configuration")
    release_operations = _mapping(github_config.get("releaseOperations"), "release operations configuration")
    if record.get("signing_approval_environment") != release_operations.get("approvalEnvironment"):
        raise ReleaseMilestoneContextError("Cancelled release-attempt signing approval environment is invalid.")
    if record.get("signing_approval_reviewer") != release_operations.get("approvalActor"):
        raise ReleaseMilestoneContextError("Cancelled release-attempt signing approval reviewer is invalid.")
    _sha256(record.get("signing_approval_fingerprint"), "cancelled release signing approval fingerprint")
    _string(record.get("cancelled_job"), "cancelled release job")
    _positive_integer(record.get("cancelled_job_id"), "cancelled release job ID")
    _string(record.get("cancelled_step"), "cancelled release step")
    source_sha = _sha(record.get("source_sha"), "cancelled release source SHA")
    workflow_completed_at = _timestamp(record.get("workflow_completed_at"), "cancelled release workflow completion")
    draft_checked_at = _timestamp(record.get("draft_checked_at"), "cancelled release draft check")
    recorded_at = _timestamp(record.get("recorded_at"), "cancelled release record timestamp")
    if draft_checked_at < workflow_completed_at or recorded_at < draft_checked_at:
        raise ReleaseMilestoneContextError("Cancelled release-attempt timestamps are out of order.")

    try:
        receipt, receipt_file_sha256 = load_validated_checked_receipt(repo_root / receipt_relative)
    except ReleaseReceiptError as error:
        raise ReleaseMilestoneContextError(f"Cancelled release-attempt receipt is invalid: {error}") from error
    if record.get("release_receipt_file_sha256") != receipt_file_sha256:
        raise ReleaseMilestoneContextError("Cancelled release-attempt receipt file digest does not match the record.")
    if record.get("release_receipt_sha256") != receipt.get("receipt_sha256"):
        raise ReleaseMilestoneContextError("Cancelled release-attempt receipt self-digest does not match the record.")

    release = _mapping(receipt.get("release"), "cancelled release-attempt receipt release")
    versions = _mapping(receipt.get("versions"), "cancelled release-attempt receipt versions")
    workflow = _mapping(receipt.get("workflow"), "cancelled release-attempt receipt workflow")
    identity_pairs = {
        "package_version": versions.get("package"),
        "public_version": versions.get("public"),
        "build_version": versions.get("build"),
        "release_tag": release.get("tag"),
        "source_sha": receipt.get("source_sha"),
        "workflow_run_id": workflow.get("run_id"),
        "workflow_run_attempt": workflow.get("run_attempt"),
        "draft_release_id": release.get("id"),
    }
    if any(record.get(field) != value for field, value in identity_pairs.items()):
        raise ReleaseMilestoneContextError("Cancelled release-attempt identity differs from its checked receipt.")
    if (
        release.get("target_sha") != source_sha
        or release.get("prerelease") is not True
        or workflow.get("name") != "Prerelease"
        or workflow.get("actor") != "shiny-code-bot"
    ):
        raise ReleaseMilestoneContextError(
            "Cancelled release-attempt receipt does not preserve guarded prerelease identity."
        )

    draft_assets: dict[str, Mapping[str, Any]] = {}
    for index, raw_asset in enumerate(_sequence(record.get("draft_assets"), "cancelled release draft assets")):
        asset = _mapping(raw_asset, f"cancelled release draft asset {index}")
        if set(asset) != CANCELLED_ATTEMPT_ASSET_KEYS:
            raise ReleaseMilestoneContextError("Cancelled release draft asset keys do not match the required schema.")
        kind = _string(asset.get("kind"), f"cancelled release draft asset {index} kind")
        if kind not in CANCELLED_ATTEMPT_ASSET_KINDS or kind in draft_assets:
            raise ReleaseMilestoneContextError(
                "Cancelled release draft assets must contain four unique required kinds."
            )
        _positive_integer(asset.get("asset_id"), f"cancelled release draft asset {kind} ID")
        _positive_integer(asset.get("size_bytes"), f"cancelled release draft asset {kind} size")
        _string(asset.get("name"), f"cancelled release draft asset {kind} name")
        _sha256(asset.get("sha256"), f"cancelled release draft asset {kind} digest")
        draft_assets[kind] = asset
    if set(draft_assets) != CANCELLED_ATTEMPT_ASSET_KINDS:
        raise ReleaseMilestoneContextError("Cancelled release draft assets are incomplete.")

    for raw_artifact in _sequence(receipt.get("artifacts"), "cancelled release-attempt receipt artifacts"):
        artifact = _mapping(raw_artifact, "cancelled release-attempt receipt artifact")
        kind = _string(artifact.get("kind"), "cancelled release-attempt receipt artifact kind")
        if draft_assets.get(kind) != artifact:
            raise ReleaseMilestoneContextError(
                f"Cancelled release draft asset {kind} differs from the checked release receipt."
            )
    receipt_asset = draft_assets["receipt"]
    if (
        receipt_asset.get("name") != FAILED_ATTEMPT_RECEIPT_NAME
        or receipt_asset.get("asset_id") != record.get("release_receipt_asset_id")
        or receipt_asset.get("size_bytes") != record.get("release_receipt_asset_size_bytes")
        or receipt_asset.get("sha256") != receipt_file_sha256
    ):
        raise ReleaseMilestoneContextError("Cancelled release receipt asset identity does not match the record.")
    return record


def _require_immutable_if_tracked(repo_root: Path, base_sha: str, relative_path: Path) -> None:
    tracked = subprocess.run(
        ["git", "cat-file", "-e", f"{base_sha}:{relative_path.as_posix()}"],
        cwd=repo_root,
        capture_output=True,
        check=False,
    )
    if tracked.returncode != 0:
        return
    changed = subprocess.run(
        ["git", "diff", "--quiet", base_sha, "--", relative_path.as_posix()],
        cwd=repo_root,
        check=False,
    )
    if changed.returncode != 0:
        raise ReleaseMilestoneContextError("Committed cancelled release-attempt evidence must remain immutable.")


def _validate_failed_unpublished_candidate(
    repo_root: Path,
    *,
    base_sha: str,
    base_qualification: Mapping[str, Any],
    base_candidate: Mapping[str, Any],
) -> int:
    release_tag = _string(base_candidate.get("release_tag"), "failed candidate release tag")
    attempt_root = FAILED_ATTEMPT_ROOT / release_tag
    cancelled_record_relative = attempt_root / CANCELLED_ATTEMPT_RECORD_NAME
    cancelled_record_path = repo_root / cancelled_record_relative
    if cancelled_record_path.is_file():
        if (repo_root / attempt_root / FAILED_ATTEMPT_RECORD_NAME).exists():
            raise ReleaseMilestoneContextError("Release attempt cannot have both failed and cancelled records.")
        _require_immutable_if_tracked(repo_root, base_sha, cancelled_record_relative)
        _require_immutable_if_tracked(repo_root, base_sha, attempt_root / FAILED_ATTEMPT_RECEIPT_NAME)
        record = validate_cancelled_attempt_record(repo_root, release_tag)
        expected_candidate = {
            "package_version": record.get("package_version"),
            "public_version": record.get("public_version"),
            "build_version": record.get("build_version"),
            "release_tag": record.get("release_tag"),
            "dmg_name": next(
                (
                    asset.get("name")
                    for asset in _sequence(record.get("draft_assets"), "cancelled release draft assets")
                    if _mapping(asset, "cancelled release draft asset").get("kind") == "dmg"
                ),
                None,
            ),
            "workflow": "Prerelease",
        }
        if any(base_candidate.get(field) != value for field, value in expected_candidate.items()):
            raise ReleaseMilestoneContextError(
                "Cancelled release-attempt record does not bind the base qualification candidate."
            )
        raise ReleaseMilestoneContextError(
            "Cancelled signed candidate still has a retained draft; an explicitly authorized draft-deletion "
            "record is required before successor preparation."
        )
    record_relative = attempt_root / FAILED_ATTEMPT_RECORD_NAME
    receipt_relative = attempt_root / FAILED_ATTEMPT_RECEIPT_NAME
    for relative in (record_relative, receipt_relative):
        tracked_in_base = subprocess.run(
            ["git", "cat-file", "-e", f"{base_sha}:{relative.as_posix()}"],
            cwd=repo_root,
            capture_output=True,
            check=False,
        )
        if tracked_in_base.returncode == 0:
            raise ReleaseMilestoneContextError("Failed release-attempt recovery records must be new and immutable.")

    record = _load_json(repo_root / record_relative, "failed release-attempt record")
    if set(record) != FAILED_ATTEMPT_RECORD_KEYS:
        raise ReleaseMilestoneContextError("Failed release-attempt record keys do not match the required schema.")
    if (
        record.get("schema_version") != 1
        or record.get("status") != "failed_unpublished_signed_candidate"
        or record.get("workflow_conclusion") != "failure"
        or record.get("published_at") is not None
        or record.get("draft_deleted") is not True
        or record.get("must_not_rebuild") is not True
        or base_qualification.get("status") != "preregistered_pending_exact_candidate"
    ):
        raise ReleaseMilestoneContextError(
            "Failed release-attempt record must prove an unpublished, deleted-draft, permanently burned candidate."
        )
    if record.get("release_receipt_path") != receipt_relative.as_posix():
        raise ReleaseMilestoneContextError("Failed release-attempt receipt path is not canonical.")

    try:
        receipt, receipt_file_sha256 = load_validated_checked_receipt(repo_root / receipt_relative)
    except ReleaseReceiptError as error:
        raise ReleaseMilestoneContextError(f"Failed release-attempt receipt is invalid: {error}") from error
    if record.get("release_receipt_file_sha256") != receipt_file_sha256:
        raise ReleaseMilestoneContextError("Failed release-attempt receipt file digest does not match the record.")
    if record.get("release_receipt_sha256") != receipt.get("receipt_sha256"):
        raise ReleaseMilestoneContextError("Failed release-attempt receipt self-digest does not match the record.")

    release = _mapping(receipt.get("release"), "failed release-attempt receipt release")
    versions = _mapping(receipt.get("versions"), "failed release-attempt receipt versions")
    workflow = _mapping(receipt.get("workflow"), "failed release-attempt receipt workflow")
    identity_pairs = {
        "package_version": versions.get("package"),
        "public_version": versions.get("public"),
        "build_version": versions.get("build"),
        "release_tag": release.get("tag"),
        "source_sha": receipt.get("source_sha"),
        "workflow_run_id": workflow.get("run_id"),
        "workflow_run_attempt": workflow.get("run_attempt"),
        "draft_release_id": release.get("id"),
    }
    if any(record.get(field) != value for field, value in identity_pairs.items()):
        raise ReleaseMilestoneContextError("Failed release-attempt record identity differs from its checked receipt.")
    expected_candidate = {
        "package_version": versions.get("package"),
        "public_version": versions.get("public"),
        "build_version": versions.get("build"),
        "release_tag": release.get("tag"),
        "dmg_name": next(
            (
                artifact.get("name")
                for artifact in _sequence(receipt.get("artifacts"), "failed release-attempt receipt artifacts")
                if _mapping(artifact, "failed release-attempt receipt artifact").get("kind") == "dmg"
            ),
            None,
        ),
        "workflow": workflow.get("name"),
    }
    if any(base_candidate.get(field) != value for field, value in expected_candidate.items()):
        raise ReleaseMilestoneContextError(
            "Failed release-attempt receipt does not bind the base qualification candidate."
        )
    if (
        release.get("target_sha") != record.get("source_sha")
        or release.get("prerelease") is not True
        or workflow.get("actor") != "shiny-code-bot"
    ):
        raise ReleaseMilestoneContextError(
            "Failed release-attempt receipt does not preserve guarded prerelease identity."
        )
    try:
        return parse_build_version(_string(record.get("build_version"), "failed candidate build version"))
    except ReleaseError as error:
        raise ReleaseMilestoneContextError(f"Failed release-attempt build identity is invalid: {error}") from error


def _requires_unpublished_candidate_recovery(repo_root: Path, base_candidate: Mapping[str, Any]) -> bool:
    release_tag = _string(base_candidate.get("release_tag"), "base qualification candidate release tag")
    cancelled_record = repo_root / FAILED_ATTEMPT_ROOT / release_tag / CANCELLED_ATTEMPT_RECORD_NAME
    return cancelled_record.is_file() or any(
        field not in base_candidate or base_candidate[field] is None for field in IMMUTABLE_CANDIDATE_FIELDS
    )


def _validate_prepublication_candidate_transition(
    repo_root: Path,
    *,
    base_sha: str,
    qualification: Mapping[str, Any],
    candidate: Mapping[str, Any],
    published_prior_receipt: Mapping[str, Any] | None = None,
) -> None:
    base_config = _load_json_at_revision(
        repo_root,
        base_sha,
        GITHUB_CONFIG_PATH.as_posix(),
        "base GitHub repository config",
    )
    base_operations = _mapping(base_config.get("releaseOperations"), "base releaseOperations")
    base_qualification_value = base_operations.get("qualificationRecordPath")
    if not isinstance(base_qualification_value, str) or not base_qualification_value:
        raise ReleaseMilestoneContextError("Base qualificationRecordPath must be a repository-relative path.")
    base_qualification_relative = Path(base_qualification_value)
    if (
        base_qualification_relative.is_absolute()
        or ".." in base_qualification_relative.parts
        or base_qualification_value.startswith("./")
    ):
        raise ReleaseMilestoneContextError("Base qualificationRecordPath must be canonical.")
    base_qualification = _load_json_at_revision(
        repo_root,
        base_sha,
        base_qualification_relative.as_posix(),
        "base qualification record",
    )
    base_candidate = _mapping(base_qualification.get("candidate"), "base qualification candidate")
    if published_prior_receipt is not None:
        missing_base_fields = [field for field in IMMUTABLE_CANDIDATE_FIELDS if field not in base_candidate]
        bound_base_fields = [
            field
            for field in IMMUTABLE_CANDIDATE_FIELDS
            if field in base_candidate and base_candidate[field] is not None
        ]
        if missing_base_fields or bound_base_fields:
            raise ReleaseMilestoneContextError(
                "Published prior receipt carry-forward requires every base immutable candidate field "
                "to be explicit null."
            )
        release = _mapping(published_prior_receipt.get("release"), "published prior receipt release")
        versions = _mapping(published_prior_receipt.get("versions"), "published prior receipt versions")
        workflow = _mapping(published_prior_receipt.get("workflow"), "published prior receipt workflow")
        artifacts = [
            _mapping(item, "published prior receipt artifact")
            for item in _sequence(published_prior_receipt.get("artifacts"), "published prior receipt artifacts")
        ]
        dmg_artifacts = [item for item in artifacts if item.get("kind") == "dmg"]
        if len(dmg_artifacts) != 1:
            raise ReleaseMilestoneContextError("Published prior receipt must contain exactly one DMG artifact.")
        expected_base_candidate = {
            "package_version": versions.get("package"),
            "public_version": versions.get("public"),
            "build_version": versions.get("build"),
            "release_tag": release.get("tag"),
            "dmg_name": dmg_artifacts[0].get("name"),
            "workflow": workflow.get("name"),
        }
        for field, expected in expected_base_candidate.items():
            if base_candidate.get(field) != expected:
                raise ReleaseMilestoneContextError(
                    f"Published prior receipt does not match base qualification candidate.{field}."
                )
    elif _requires_unpublished_candidate_recovery(repo_root, base_candidate):
        failed_build = _validate_failed_unpublished_candidate(
            repo_root,
            base_sha=base_sha,
            base_qualification=base_qualification,
            base_candidate=base_candidate,
        )
        immutable_history = _mapping(qualification.get("immutable_history"), "qualification immutable_history")
        burned_builds = _sequence(immutable_history.get("burned_builds"), "qualification burned_builds")
        if failed_build not in burned_builds:
            raise ReleaseMilestoneContextError(
                "The successor qualification must permanently burn the failed candidate build."
            )
    try:
        base_version = parse_release_version(cast(str, base_candidate.get("package_version")))
        next_version = parse_release_version(cast(str, candidate.get("package_version")))
        base_build = parse_build_version(cast(str, base_candidate.get("build_version")))
        next_build = parse_build_version(cast(str, candidate.get("build_version")))
    except (ReleaseError, TypeError) as error:
        raise ReleaseMilestoneContextError(f"Prepublication qualification identity is invalid: {error}") from error
    if next_version.order_key <= base_version.order_key or next_build <= base_build:
        raise ReleaseMilestoneContextError(
            "Prepublication qualification must advance to a newer Stable or prerelease version and global build."
        )
    expected_identity = {
        "package_version": next_version.text,
        "public_version": next_version.public_version,
        "build_version": str(next_build),
        "release_tag": next_version.release_tag,
        "dmg_name": f"3D-Blu-ray-to-Vision-Pro-{next_version.public_version}.dmg",
        "workflow": "Prerelease" if next_version.prerelease else "Stable",
    }
    for field, expected in expected_identity.items():
        if candidate.get(field) != expected:
            raise ReleaseMilestoneContextError(
                f"Prepublication qualification candidate.{field} does not match the derived Stable identity."
            )


def verify_published_release_receipt(
    receipt_path: Path,
    receipt: Mapping[str, Any],
    receipt_file_sha256: str,
) -> None:
    release = _mapping(receipt.get("release"), "carried prior receipt release")
    workflow = _mapping(receipt.get("workflow"), "carried prior receipt workflow")
    release_tag = _string(release.get("tag"), "carried prior receipt release tag")
    release_result = subprocess.run(
        ["gh", "api", f"repos/{EXPECTED_REPOSITORY}/releases/tags/{release_tag}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if release_result.returncode != 0:
        raise ReleaseMilestoneContextError("Unable to verify the carried prior receipt against GitHub Releases.")
    try:
        published_release = _mapping(json.loads(release_result.stdout), "published GitHub release")
    except json.JSONDecodeError as error:
        raise ReleaseMilestoneContextError(f"GitHub release verification returned invalid JSON: {error}") from error
    expected_release = {
        "id": release.get("id"),
        "tag_name": release_tag,
        "target_commitish": receipt.get("source_sha"),
        "draft": False,
        "prerelease": release.get("prerelease"),
        "immutable": True,
    }
    for field, expected in expected_release.items():
        if published_release.get(field) != expected:
            raise ReleaseMilestoneContextError(
                f"Carried prior receipt does not match published GitHub release field {field}."
            )
    matching_assets = [
        _mapping(asset, "published release asset")
        for asset in _sequence(published_release.get("assets"), "published release assets")
        if _mapping(asset, "published release asset").get("name") == RECEIPT_ASSET_NAME
    ]
    if len(matching_assets) != 1:
        raise ReleaseMilestoneContextError("Published GitHub release must contain exactly one release receipt asset.")
    asset = matching_assets[0]
    expected_asset = {
        "name": RECEIPT_ASSET_NAME,
        "size": receipt_path.stat().st_size,
        "digest": f"sha256:{receipt_file_sha256}",
    }
    for field, expected in expected_asset.items():
        if asset.get(field) != expected:
            raise ReleaseMilestoneContextError(
                f"Carried prior receipt does not match published release receipt asset field {field}."
            )

    run_id = workflow.get("run_id")
    run_result = subprocess.run(
        ["gh", "api", f"repos/{EXPECTED_REPOSITORY}/actions/runs/{run_id}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if run_result.returncode != 0:
        raise ReleaseMilestoneContextError("Unable to verify the carried prior receipt release workflow run.")
    try:
        published_run = _mapping(json.loads(run_result.stdout), "published release workflow run")
    except json.JSONDecodeError as error:
        raise ReleaseMilestoneContextError(f"GitHub workflow verification returned invalid JSON: {error}") from error
    actor = _mapping(published_run.get("actor"), "published release workflow actor")
    expected_run = {
        "id": run_id,
        "name": workflow.get("name"),
        "path": workflow.get("path"),
        "head_sha": receipt.get("source_sha"),
        "status": "completed",
        "conclusion": "success",
        "run_attempt": workflow.get("run_attempt"),
        "event": "workflow_dispatch",
        "head_branch": EXPECTED_BASE_BRANCH,
    }
    for field, expected in expected_run.items():
        if published_run.get(field) != expected:
            raise ReleaseMilestoneContextError(
                f"Carried prior receipt does not match published release workflow field {field}."
            )
    if actor.get("login") != workflow.get("actor"):
        raise ReleaseMilestoneContextError("Carried prior receipt does not match the published release workflow actor.")


def _validate_carried_prior_receipt(
    repo_root: Path,
    *,
    base_sha: str,
    qualification: Mapping[str, Any],
    receipt_relative: str,
    release_tag: str,
    published_receipt_verifier: PublishedReceiptVerifier,
) -> Mapping[str, Any]:
    tracked_in_base = subprocess.run(
        ["git", "cat-file", "-e", f"{base_sha}:{receipt_relative}"],
        cwd=repo_root,
        capture_output=True,
        check=False,
    )
    if tracked_in_base.returncode == 0:
        raise ReleaseMilestoneContextError("A carried prior release receipt must be new to the preparation branch.")
    try:
        receipt_path = repo_root / receipt_relative
        receipt, receipt_file_sha256 = load_validated_checked_receipt(receipt_path)
    except ReleaseReceiptError as error:
        raise ReleaseMilestoneContextError(f"Carried prior release receipt is invalid: {error}") from error
    release = _mapping(receipt.get("release"), "carried prior receipt release")
    versions = _mapping(receipt.get("versions"), "carried prior receipt versions")
    workflow = _mapping(receipt.get("workflow"), "carried prior receipt workflow")
    if release.get("tag") != release_tag:
        raise ReleaseMilestoneContextError("Carried prior release receipt path conflicts with its release tag.")
    artifacts = [
        _mapping(item, "carried prior receipt artifact")
        for item in _sequence(receipt.get("artifacts"), "carried prior receipt artifacts")
    ]
    dmg_artifacts = [item for item in artifacts if item.get("kind") == "dmg"]
    appcast_artifacts = [item for item in artifacts if item.get("kind") == "appcast"]
    if len(dmg_artifacts) != 1 or len(appcast_artifacts) != 1:
        raise ReleaseMilestoneContextError(
            "Carried prior release receipt must contain exactly one DMG and one appcast artifact."
        )
    immutable_history = _mapping(qualification.get("immutable_history"), "qualification immutable_history")
    previous_beta = _mapping(immutable_history.get("previous_beta"), "qualification previous_beta")
    if previous_beta.get("must_not_rebuild") is not True:
        raise ReleaseMilestoneContextError("Carried previous_beta must remain immutable and must_not_rebuild.")
    expected_previous_beta = {
        "release_tag": release_tag,
        "package_version": versions.get("package"),
        "build_version": versions.get("build"),
        "source_git_sha": receipt.get("source_sha"),
        "release_run_id": workflow.get("run_id"),
        "release_id": release.get("id"),
        "dmg_sha256": dmg_artifacts[0].get("sha256"),
        "appcast_sha256": appcast_artifacts[0].get("sha256"),
        "signed_app_tree_sha256": receipt.get("signed_app_tree_sha256"),
    }
    for field, expected in expected_previous_beta.items():
        if previous_beta.get(field) != expected:
            raise ReleaseMilestoneContextError(
                f"Carried prior release receipt does not match qualification previous_beta.{field}."
            )
    _string(previous_beta.get("published_at"), "qualification previous_beta published_at")
    source_sha = _string(receipt.get("source_sha"), "carried prior receipt source SHA")
    tag_commit = subprocess.run(
        ["git", "rev-parse", f"{release_tag}^{{commit}}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if tag_commit.returncode != 0 or tag_commit.stdout.strip() != source_sha:
        raise ReleaseMilestoneContextError("Carried prior release receipt must match the immutable release tag commit.")
    _require_source_ancestor(repo_root, source_sha)
    published_receipt_verifier(receipt_path, receipt, receipt_file_sha256)
    return receipt


def _validate_prepublication_config_transition(
    repo_root: Path,
    *,
    base_sha: str,
    qualification_relative: str,
) -> None:
    base_config = _load_json_at_revision(
        repo_root,
        base_sha,
        GITHUB_CONFIG_PATH.as_posix(),
        "base GitHub repository config",
    )
    head_config = _load_json(repo_root / GITHUB_CONFIG_PATH, "GitHub repository config")
    expected_config = json.loads(json.dumps(base_config))
    expected_operations = _mapping(expected_config.get("releaseOperations"), "base releaseOperations")
    cast(dict[str, Any], expected_operations)["qualificationRecordPath"] = qualification_relative
    if expected_config != head_config:
        raise ReleaseMilestoneContextError(
            "Prior receipt carry-forward may change GitHub config only by advancing qualificationRecordPath."
        )


def _validate_append_only_evidence_index(repo_root: Path, *, base_sha: str) -> list[Mapping[str, Any]]:
    base_evidence = _load_json_at_revision(repo_root, base_sha, EVIDENCE_INDEX_PATH, "base qualification evidence")
    head_evidence = _load_json(repo_root / EVIDENCE_INDEX_PATH, "qualification evidence")
    if base_evidence.get("schema_version") != 1 or head_evidence.get("schema_version") != 1:
        raise ReleaseMilestoneContextError("Qualification evidence schema_version must remain 1.")
    base_receipts = list(_sequence(base_evidence.get("receipts"), "base qualification receipts"))
    head_receipts = list(_sequence(head_evidence.get("receipts"), "qualification receipts"))
    if len(head_receipts) <= len(base_receipts) or head_receipts[: len(base_receipts)] != base_receipts:
        raise ReleaseMilestoneContextError(
            "Prepublication qualification evidence may only append receipts without changing accepted history."
        )
    return [
        _mapping(receipt, f"appended qualification receipt {index}")
        for index, receipt in enumerate(head_receipts[len(base_receipts) :])
    ]


def _validate_beta_change_scoped_evidence_append(
    repo_root: Path,
    *,
    base_sha: str,
    head_branch: str,
    changed_paths: Sequence[str],
    policy_relative: str,
    appended_receipts: Sequence[Mapping[str, Any]],
) -> None:
    evidence_matches = [
        (path, match.group(1))
        for path in changed_paths
        if (match := CHANGE_SCOPED_EVIDENCE_PATH_PATTERN.fullmatch(path)) is not None
    ]
    if len(evidence_matches) != 1:
        raise ReleaseMilestoneContextError(
            "Prepublication Beta evidence requires exactly one change-scoped evidence document."
        )
    evidence_relative, release_tag = evidence_matches[0]
    try:
        version = parse_release_tag(release_tag, allow_legacy_rc=False)
    except ReleaseError as error:
        raise ReleaseMilestoneContextError(f"Prepublication Beta evidence release tag is invalid: {error}") from error
    if version.stage != "beta":
        raise ReleaseMilestoneContextError("Prepublication change-scoped evidence is limited to Beta releases.")
    if head_branch != f"qualify/{release_tag}":
        raise ReleaseMilestoneContextError(
            f"Prepublication Beta evidence must use branch {f'qualify/{release_tag}'!r}."
        )
    tracked_in_base = subprocess.run(
        ["git", "cat-file", "-e", f"{base_sha}:{evidence_relative}"],
        cwd=repo_root,
        capture_output=True,
        check=False,
    )
    if tracked_in_base.returncode == 0:
        raise ReleaseMilestoneContextError("Prepublication Beta evidence document must be new and immutable.")

    evidence_path = repo_root / evidence_relative
    evidence_bytes = evidence_path.read_bytes()
    evidence = _load_json(evidence_path, "prepublication Beta change-scoped evidence")
    source = _mapping(evidence.get("source"), "prepublication Beta evidence source")
    try:
        source_version = parse_release_version(_string(source.get("package_version"), "Beta package version"))
        source_build = parse_build_version(_string(source.get("build_version"), "Beta build version"))
    except ReleaseError as error:
        raise ReleaseMilestoneContextError(f"Prepublication Beta evidence identity is invalid: {error}") from error
    if (
        source_version.stage != "beta"
        or source_version.release_tag != release_tag
        or source.get("public_version") != source_version.public_version
        or source.get("release_tag") != release_tag
        or source.get("source_sha") != base_sha
        or source_build <= 0
    ):
        raise ReleaseMilestoneContextError(
            "Prepublication Beta evidence source identity must match the release tag and pull-request base SHA."
        )
    failed_run = _mapping(evidence.get("failed_preparation_run"), "prepublication Beta failed run")
    semantics = _mapping(evidence.get("evidence_source_semantics"), "prepublication Beta evidence semantics")
    privacy = _mapping(evidence.get("privacy"), "prepublication Beta evidence privacy")
    acceptance = _mapping(evidence.get("acceptance"), "prepublication Beta evidence acceptance")
    if (
        evidence.get("schema_version") != 1
        or evidence.get("result") != "accepted_public_safe_summary"
        or failed_run.get("signing_started") is not False
        or failed_run.get("release_identity_created") is not False
        or semantics.get("developer_id_or_notarization_claimed") is not False
        or semantics.get("index_source") != "signed_artifact_receipt"
        or acceptance.get("passed") is not True
        or acceptance.get("failed_case_count") != 0
        or list(_sequence(acceptance.get("blocking_case_ids"), "Beta blocking case IDs"))
    ):
        raise ReleaseMilestoneContextError(
            "Prepublication Beta evidence must remain public-safe, unsigned-release-scoped, and fully passing."
        )
    for field in (
        "private_paths_recorded",
        "private_hostnames_recorded",
        "private_media_identifiers_recorded",
        "diagnostic_tokens_recorded",
    ):
        if privacy.get(field) is not False:
            raise ReleaseMilestoneContextError(f"Prepublication Beta evidence privacy.{field} must be false.")

    evidence_cases: dict[str, Mapping[str, Any]] = {}
    for index, raw_case in enumerate(_sequence(evidence.get("cases"), "prepublication Beta evidence cases")):
        evidence_case = _mapping(raw_case, f"prepublication Beta evidence case {index}")
        case_id = _string(evidence_case.get("case_id"), "prepublication Beta evidence case ID")
        if case_id in evidence_cases or evidence_case.get("status") != "passed":
            raise ReleaseMilestoneContextError(
                "Prepublication Beta evidence cases must be uniquely identified and passed."
            )
        evidence_cases[case_id] = evidence_case
    if acceptance.get("passed_case_count") != len(evidence_cases):
        raise ReleaseMilestoneContextError(
            "Prepublication Beta evidence passed_case_count must match the proved case count."
        )

    policy = _load_json(repo_root / policy_relative, "prepublication Beta qualification policy")
    policy_cases = {
        _string(case.get("id"), "qualification policy case ID"): case
        for index, raw_case in enumerate(_sequence(policy.get("cases"), "qualification policy cases"))
        for case in [_mapping(raw_case, f"qualification policy case {index}")]
    }
    evidence_digest = hashlib.sha256(evidence_bytes).hexdigest()
    receipt_case_ids: set[str] = set()
    for receipt in appended_receipts:
        case_id = _string(receipt.get("case_id"), "prepublication Beta receipt case ID")
        policy_case = policy_cases.get(case_id)
        allowed_sources = (
            list(_sequence(policy_case.get("allowed_evidence_sources"), f"policy case {case_id} evidence sources"))
            if policy_case is not None
            else []
        )
        if (
            case_id in receipt_case_ids
            or case_id not in evidence_cases
            or policy_case is None
            or policy_case.get("tier") != 2
            or policy_case.get("blocking_phase") != "release_candidate"
            or policy_case.get("artifact_owned") is True
            or policy_case.get("requires_live_publication") is True
            or "signed_artifact_receipt" not in allowed_sources
        ):
            raise ReleaseMilestoneContextError(
                "Prepublication Beta receipts may cover only proved release-candidate Tier 2 cases."
            )
        receipt_id = _string(receipt.get("receipt_id"), "prepublication Beta receipt ID")
        if (
            receipt.get("status") != "accepted"
            or receipt.get("source") != "signed_artifact_receipt"
            or receipt.get("source_sha") != base_sha
            or receipt.get("reference") != evidence_relative
            or receipt.get("sha256") != evidence_digest
            or receipt_id != f"{release_tag}:{case_id}:{base_sha[:7]}"
        ):
            raise ReleaseMilestoneContextError(
                "Prepublication Beta receipt identity must match the base candidate and change-scoped evidence."
            )
        receipt_case_ids.add(case_id)
    if receipt_case_ids != set(evidence_cases):
        raise ReleaseMilestoneContextError(
            "Prepublication Beta evidence cases and appended receipt cases must match exactly."
        )


def _is_recovery_authorization_path(path: str) -> bool:
    return path in RECOVERY_AUTHORIZATION_PATHS


def _validate_release_evidence_tag_scope(changed_paths: Sequence[str], release_tag: str) -> None:
    expected_prefix = f"docs/release-evidence/{release_tag}/"
    unrelated_paths = sorted(
        path
        for path in changed_paths
        if path.startswith("docs/release-evidence/")
        and not path.startswith(expected_prefix)
        and path != RELEASE_LEDGER_PATH
        and not _is_recovery_authorization_path(path)
    )
    if unrelated_paths:
        raise ReleaseMilestoneContextError(
            "Release evidence pull requests may change only one release tag: "
            f"expected {release_tag!r}, unrelated={unrelated_paths!r}."
        )


def _validate_append_only_release_ledger(repo_root: Path, *, base_sha: str, release_tag: str) -> None:
    base_ledger = _load_json_at_revision(repo_root, base_sha, RELEASE_LEDGER_PATH, "base release evidence ledger")
    head_ledger = _load_json(repo_root / RELEASE_LEDGER_PATH, "release evidence ledger")
    if base_ledger.get("schema_version") != 1 or head_ledger.get("schema_version") != 1:
        raise ReleaseMilestoneContextError("Release evidence ledger schema_version must remain 1.")
    if {key: value for key, value in base_ledger.items() if key != "releases"} != {
        key: value for key, value in head_ledger.items() if key != "releases"
    }:
        raise ReleaseMilestoneContextError("Release evidence ledger metadata may not change.")

    def records_by_tag(ledger: Mapping[str, Any], description: str) -> dict[str, Mapping[str, Any]]:
        records: dict[str, Mapping[str, Any]] = {}
        raw_records = _sequence(ledger.get("releases"), f"{description} releases")
        tags: list[str] = []
        for index, raw_record in enumerate(raw_records):
            record = _mapping(raw_record, f"{description} release {index}")
            tag = _string(record.get("tag"), f"{description} release tag")
            if tag in records:
                raise ReleaseMilestoneContextError(f"{description.capitalize()} may not contain duplicate tags.")
            records[tag] = record
            tags.append(tag)
        if tags != sorted(tags):
            raise ReleaseMilestoneContextError(f"{description.capitalize()} releases must remain sorted by tag.")
        return records

    base_records = records_by_tag(base_ledger, "base release evidence ledger")
    head_records = records_by_tag(head_ledger, "release evidence ledger")
    if any(head_records.get(tag) != record for tag, record in base_records.items()):
        raise ReleaseMilestoneContextError(
            "Release evidence ledger may only append a release without changing accepted history."
        )
    added_tags = set(head_records) - set(base_records)
    if added_tags != {release_tag}:
        raise ReleaseMilestoneContextError(
            "Release evidence ledger must append exactly the pull request release tag: "
            f"expected {release_tag!r}, added={sorted(added_tags)!r}."
        )
    receipt_relative = f"docs/release-evidence/{release_tag}/release-receipt.json"
    publication_relative = f"docs/release-evidence/{release_tag}/publication-record.json"
    try:
        receipt, receipt_file_sha256 = load_validated_checked_receipt(repo_root / receipt_relative)
    except ReleaseReceiptError as error:
        raise ReleaseMilestoneContextError(f"Release evidence ledger receipt is invalid: {error}") from error
    release = _mapping(receipt.get("release"), "release evidence ledger receipt release")
    workflow = _mapping(receipt.get("workflow"), "release evidence ledger receipt workflow")
    publication = _load_json(repo_root / publication_relative, "release evidence ledger publication record")
    expected_publication = {
        "receipt_file_sha256": receipt_file_sha256,
        "release_id": release.get("id"),
        "release_tag": release_tag,
        "source_sha": receipt.get("source_sha"),
        "workflow_run_id": workflow.get("run_id"),
    }
    for field, expected in expected_publication.items():
        if publication.get(field) != expected:
            raise ReleaseMilestoneContextError(
                f"Release evidence ledger publication record {field} must match the checked release receipt."
            )
    if effective_successful_workflow_run_id(publication) is None:
        raise ReleaseMilestoneContextError(
            "Release evidence ledger publication record must identify a successful publication workflow."
        )
    expected_record: dict[str, Any] = {
        "publication_record": publication_relative,
        "published_at": _string(publication.get("published_at"), "release publication timestamp"),
        "receipt": receipt_relative,
        "receipt_file_sha256": receipt_file_sha256,
        "release_id": release.get("id"),
        "source_sha": receipt.get("source_sha"),
        "tag": release_tag,
        "workflow_run_id": workflow.get("run_id"),
    }
    if "recovery_workflow_run" in publication:
        expected_record["recovery_workflow_run"] = publication["recovery_workflow_run"]
    if head_records[release_tag] != expected_record:
        raise ReleaseMilestoneContextError(
            "Release evidence ledger record must match the checked release receipt and publication record."
        )


def _repository_path(repo_root: Path, value: object, description: str) -> tuple[Path, str]:
    if not isinstance(value, str) or not value:
        raise ReleaseMilestoneContextError(f"{description} must be a repository-relative path.")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts or value.startswith("./"):
        raise ReleaseMilestoneContextError(f"{description} must be a canonical repository-relative path.")
    return repo_root / relative, relative.as_posix()


def _qualification_path_for_release(repo_root: Path, release_tag: str) -> tuple[Path, str]:
    matches: list[Path] = []
    for path in sorted((repo_root / "docs" / "qualification").glob("*-signed-qualification-v1.json")):
        qualification = _load_json(path, "signed qualification record")
        candidate = qualification.get("candidate")
        if isinstance(candidate, Mapping) and candidate.get("release_tag") == release_tag:
            matches.append(path)
    if len(matches) != 1:
        raise ReleaseMilestoneContextError(
            f"Expected exactly one signed qualification record for {release_tag}; found {len(matches)}."
        )
    return matches[0], matches[0].relative_to(repo_root).as_posix()


def _require_tracked_path(repo_root: Path, relative_path: str, description: str) -> None:
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", relative_path],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if tracked.returncode != 0:
        raise ReleaseMilestoneContextError(f"{description} must be tracked in the current checkout.")


def _require_source_ancestor(repo_root: Path, source_sha: str) -> None:
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", source_sha, "HEAD"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if ancestor.returncode != 0:
        raise ReleaseMilestoneContextError(
            "Milestone release receipt source SHA must be an ancestor of the current evidence checkout."
        )


def discover_milestone_receipt(
    repo_root: Path,
    *,
    base_sha: str,
    head_sha: str,
    head_branch: str,
    base_repo: str,
    head_repo: str,
    base_branch: str,
    published_receipt_verifier: PublishedReceiptVerifier = verify_published_release_receipt,
) -> Path | None:
    if base_repo != EXPECTED_REPOSITORY or head_repo != EXPECTED_REPOSITORY or base_branch != EXPECTED_BASE_BRANCH:
        raise ReleaseMilestoneContextError(
            "Release evidence qualification requires a same-repository pull request targeting protected main."
        )
    changed = subprocess.run(
        ["git", "diff", "--name-only", f"{base_sha}...{head_sha}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if changed.returncode != 0:
        raise ReleaseMilestoneContextError("Unable to inspect pull-request evidence changes.")
    changed_paths = tuple(path for path in changed.stdout.splitlines() if path)
    receipt_matches = [
        (path, match.group(1)) for path in changed_paths if (match := RECEIPT_PATH_PATTERN.fullmatch(path)) is not None
    ]
    config = _load_json(repo_root / GITHUB_CONFIG_PATH, "GitHub repository config")
    operations = _mapping(config.get("releaseOperations"), "releaseOperations")
    _policy_path, policy_relative = _repository_path(
        repo_root,
        operations.get("qualificationPolicyPath"),
        "qualificationPolicyPath",
    )
    _qualification_path, qualification_relative = _repository_path(
        repo_root,
        operations.get("qualificationRecordPath"),
        "qualificationRecordPath",
    )
    checked_release_mutation = any(
        path.startswith("docs/release-evidence/") and not _is_recovery_authorization_path(path)
        for path in changed_paths
    )
    evidence_index_mutation = EVIDENCE_INDEX_PATH in changed_paths
    qualification_mutation = qualification_relative in changed_paths
    if len(receipt_matches) == 1 and qualification_mutation:
        qualification = _load_json(repo_root / qualification_relative, "configured qualification record")
        candidate = _mapping(qualification.get("candidate"), "configured qualification candidate")
        missing_immutable_fields = [field for field in IMMUTABLE_CANDIDATE_FIELDS if field not in candidate]
        if missing_immutable_fields:
            raise ReleaseMilestoneContextError(
                "Prepublication qualification must explicitly include every immutable candidate field as null."
            )
        if all(candidate[field] is None for field in IMMUTABLE_CANDIDATE_FIELDS):
            receipt_relative, release_tag = receipt_matches[0]
            candidate_release_tag = _string(candidate.get("release_tag"), "configured qualification release tag")
            expected_branch = f"prepare/{candidate_release_tag}"
            if head_branch != expected_branch:
                raise ReleaseMilestoneContextError(
                    f"Prior receipt carry-forward must use preparation branch {expected_branch!r}."
                )
            _validate_prepublication_config_transition(
                repo_root,
                base_sha=base_sha,
                qualification_relative=qualification_relative,
            )
            runner_input_changes = sorted(RUNNER_BOUND_QUALIFICATION_PATHS.intersection(changed_paths))
            if runner_input_changes:
                raise ReleaseMilestoneContextError(
                    "Prepublication prior-receipt carry-forward may not change runner-bound policy or route inputs: "
                    f"{runner_input_changes!r}."
                )
            other_checked_release_changes = [
                path
                for path in changed_paths
                if path.startswith("docs/release-evidence/")
                and not _is_recovery_authorization_path(path)
                and path != receipt_relative
            ]
            if other_checked_release_changes or evidence_index_mutation or RELEASE_LEDGER_PATH in changed_paths:
                raise ReleaseMilestoneContextError(
                    "Prepublication prior-receipt carry-forward may add only the exact checked receipt."
                )
            published_prior_receipt = _validate_carried_prior_receipt(
                repo_root,
                base_sha=base_sha,
                qualification=qualification,
                receipt_relative=receipt_relative,
                release_tag=release_tag,
                published_receipt_verifier=published_receipt_verifier,
            )
            _validate_prepublication_candidate_transition(
                repo_root,
                base_sha=base_sha,
                qualification=qualification,
                candidate=candidate,
                published_prior_receipt=published_prior_receipt,
            )
            return None
    if not receipt_matches:
        if checked_release_mutation:
            raise ReleaseMilestoneContextError(
                "Release evidence changes require exactly one checked release receipt in the pull-request diff."
            )
        runner_input_changes = sorted(RUNNER_BOUND_QUALIFICATION_PATHS.intersection(changed_paths))
        if evidence_index_mutation and runner_input_changes:
            raise ReleaseMilestoneContextError(
                f"Prepublication evidence may not change runner-bound policy or route inputs: {runner_input_changes!r}."
            )
        unbound_candidate = False
        if qualification_mutation:
            qualification = _load_json(repo_root / qualification_relative, "configured qualification record")
            candidate = _mapping(qualification.get("candidate"), "configured qualification candidate")
            missing_immutable_fields = [field for field in IMMUTABLE_CANDIDATE_FIELDS if field not in candidate]
            if missing_immutable_fields:
                raise ReleaseMilestoneContextError(
                    "Prepublication qualification must explicitly include every immutable candidate field as null."
                )
            if any(candidate[field] is not None for field in IMMUTABLE_CANDIDATE_FIELDS):
                raise ReleaseMilestoneContextError(
                    "Published qualification record changes require the checked release receipt and milestone gate."
                )
            _validate_prepublication_candidate_transition(
                repo_root,
                base_sha=base_sha,
                qualification=qualification,
                candidate=candidate,
            )
            unbound_candidate = True
        appended_receipts: list[Mapping[str, Any]] = []
        if evidence_index_mutation:
            appended_receipts = _validate_append_only_evidence_index(repo_root, base_sha=base_sha)
        change_scoped_mutation = any(CHANGE_SCOPED_EVIDENCE_PATH_PATTERN.fullmatch(path) for path in changed_paths)
        if unbound_candidate and change_scoped_mutation:
            raise ReleaseMilestoneContextError(
                "Beta change-scoped evidence may not be combined with a Stable qualification reset."
            )
        if evidence_index_mutation and not unbound_candidate:
            _validate_beta_change_scoped_evidence_append(
                repo_root,
                base_sha=base_sha,
                head_branch=head_branch,
                changed_paths=changed_paths,
                policy_relative=policy_relative,
                appended_receipts=appended_receipts,
            )
        return None
    if len(receipt_matches) != 1:
        raise ReleaseMilestoneContextError(
            "A release evidence pull request must contain exactly one checked release receipt."
        )
    receipt_relative, release_tag = receipt_matches[0]
    _validate_release_evidence_tag_scope(changed_paths, release_tag)
    expected_branch = f"automation/release-evidence-{release_tag}"
    if head_branch != expected_branch:
        raise ReleaseMilestoneContextError(f"Release evidence changes must use idempotent branch {expected_branch!r}.")
    out_of_scope = [path for path in changed_paths if not path.startswith("docs/")]
    if out_of_scope:
        raise ReleaseMilestoneContextError(f"Release evidence pull requests may change only docs/: {out_of_scope!r}.")
    runner_input_changes = sorted(RUNNER_BOUND_QUALIFICATION_PATHS.intersection(changed_paths))
    if runner_input_changes:
        raise ReleaseMilestoneContextError(
            "Release evidence pull requests may not change runner-bound policy or route inputs: "
            f"{runner_input_changes!r}."
        )
    if evidence_index_mutation:
        _validate_append_only_evidence_index(repo_root, base_sha=base_sha)
    if RELEASE_LEDGER_PATH in changed_paths:
        _validate_append_only_release_ledger(repo_root, base_sha=base_sha, release_tag=release_tag)
    return repo_root / receipt_relative


def discover_milestone_manifest(
    repo_root: Path,
    *,
    base_sha: str,
    head_sha: str,
    head_branch: str,
    base_repo: str,
    head_repo: str,
    base_branch: str,
) -> Path | None:
    if base_repo != EXPECTED_REPOSITORY or head_repo != EXPECTED_REPOSITORY or base_branch != EXPECTED_BASE_BRANCH:
        raise ReleaseMilestoneContextError(
            "Release evidence qualification requires a same-repository pull request targeting protected main."
        )
    changed = subprocess.run(
        ["git", "diff", "--name-only", f"{base_sha}...{head_sha}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if changed.returncode != 0:
        raise ReleaseMilestoneContextError("Unable to inspect pull-request evidence changes.")
    changed_paths = tuple(path for path in changed.stdout.splitlines() if path)
    manifest_matches = [
        (path, match.group(1)) for path in changed_paths if (match := MANIFEST_PATH_PATTERN.fullmatch(path)) is not None
    ]
    if not manifest_matches:
        return None
    if len(manifest_matches) != 1:
        raise ReleaseMilestoneContextError(
            "A release evidence pull request must contain exactly one checked qualification manifest."
        )
    manifest_relative, release_tag = manifest_matches[0]
    receipt_matches = [
        (path, match.group(1)) for path in changed_paths if (match := RECEIPT_PATH_PATTERN.fullmatch(path)) is not None
    ]
    if len(receipt_matches) > 1:
        raise ReleaseMilestoneContextError(
            "A qualification manifest pull request may change at most one checked release receipt."
        )
    if receipt_matches and receipt_matches[0][1] != release_tag:
        raise ReleaseMilestoneContextError(
            "Qualification manifest and changed release receipt must use the same release tag."
        )
    _validate_release_evidence_tag_scope(changed_paths, release_tag)
    expected_branch = f"automation/release-evidence-{release_tag}"
    if head_branch != expected_branch:
        raise ReleaseMilestoneContextError(f"Release evidence changes must use idempotent branch {expected_branch!r}.")
    out_of_scope = [path for path in changed_paths if not path.startswith("docs/")]
    if out_of_scope:
        raise ReleaseMilestoneContextError(f"Release evidence pull requests may change only docs/: {out_of_scope!r}.")
    runner_input_changes = sorted(RUNNER_BOUND_QUALIFICATION_PATHS.intersection(changed_paths))
    if runner_input_changes:
        raise ReleaseMilestoneContextError(
            "Release evidence pull requests may not change runner-bound policy or route inputs: "
            f"{runner_input_changes!r}."
        )
    if EVIDENCE_INDEX_PATH in changed_paths:
        _validate_append_only_evidence_index(repo_root, base_sha=base_sha)
    if RELEASE_LEDGER_PATH in changed_paths:
        _validate_append_only_release_ledger(repo_root, base_sha=base_sha, release_tag=release_tag)
    return repo_root / manifest_relative


def resolve_milestone_manifest_context(repo_root: Path, manifest_path: Path) -> ReleaseMilestoneContext:
    repo_root = repo_root.resolve()
    try:
        manifest_relative = manifest_path.resolve().relative_to(repo_root).as_posix()
    except ValueError as error:
        raise ReleaseMilestoneContextError("Milestone manifest must be inside the repository.") from error
    try:
        manifest = load_validated_manifest(manifest_path, repo_root=repo_root)
    except ReleaseQualificationManifestError as error:
        raise ReleaseMilestoneContextError(f"Milestone qualification manifest is invalid: {error}") from error
    candidate = _mapping(manifest.get("candidate"), "milestone manifest candidate")
    release_tag = _string(candidate.get("release_tag"), "manifest release tag")
    expected_manifest_path = f"docs/release-evidence/{release_tag}/{MANIFEST_NAME}"
    if manifest_relative != expected_manifest_path:
        raise ReleaseMilestoneContextError(
            f"Milestone qualification manifest must use checked path {expected_manifest_path!r}."
        )
    try:
        version = parse_release_version(cast(str, candidate["package_version"]))
    except ReleaseError as error:
        raise ReleaseMilestoneContextError(f"Milestone manifest release version is invalid: {error}") from error
    paths = _mapping(manifest.get("paths"), "milestone manifest paths")
    release_receipt = _mapping(manifest.get("release_receipt"), "milestone manifest release receipt")
    evidence = _mapping(manifest.get("canonical_evidence"), "milestone manifest canonical evidence")
    input_digests = _mapping(manifest.get("input_digests"), "milestone manifest input digests")
    evidence_ref = _string(evidence.get("ref"), "manifest evidence ref")
    if evidence_ref != f"automation/release-evidence-{release_tag}":
        raise ReleaseMilestoneContextError("Milestone manifest evidence ref conflicts with release tag.")
    source_sha = cast(str, candidate["source_sha"])
    manifest_digest = _string(manifest.get("manifest_sha256"), "manifest digest")
    _require_tracked_path(repo_root, manifest_relative, "Milestone qualification manifest")
    _require_tracked_path(repo_root, cast(str, release_receipt["path"]), "Milestone release receipt")
    _require_source_ancestor(repo_root, source_sha)
    return ReleaseMilestoneContext(
        candidate_sha=source_sha,
        evidence_path=cast(str, paths["evidence_index"]),
        first_candidate_of_cycle=version.first_candidate_of_cycle,
        evidence_index_base_sha256=_string(
            input_digests.get("evidence_index_base"),
            "manifest evidence index baseline SHA-256",
        ),
        manifest_path=manifest_relative,
        manifest_sha256=manifest_digest,
        policy_path=cast(str, paths["policy"]),
        qualification_path=cast(str, paths["qualification"]),
        release_receipt_path=cast(str, release_receipt["path"]),
        release_stage=version.stage,
        release_tag=release_tag,
        runner_sha=_string(manifest.get("runner_sha"), "manifest runner SHA"),
    )


def resolve_milestone_context(repo_root: Path, receipt_path: Path) -> ReleaseMilestoneContext:
    repo_root = repo_root.resolve()
    try:
        receipt_relative = receipt_path.resolve().relative_to(repo_root).as_posix()
    except ValueError as error:
        raise ReleaseMilestoneContextError("Milestone release receipt must be inside the repository.") from error

    try:
        receipt, receipt_file_sha256 = load_validated_checked_receipt(receipt_path)
    except ReleaseReceiptError as error:
        raise ReleaseMilestoneContextError(f"Milestone release receipt is invalid: {error}") from error
    release = _mapping(receipt.get("release"), "milestone release receipt release")
    versions = _mapping(receipt.get("versions"), "milestone release receipt versions")
    workflow = _mapping(receipt.get("workflow"), "milestone release receipt workflow")
    release_tag = cast(str, release["tag"])
    expected_receipt_path = f"docs/release-evidence/{release_tag}/release-receipt.json"
    if receipt_relative != expected_receipt_path:
        raise ReleaseMilestoneContextError(
            f"Milestone release receipt must use checked path {expected_receipt_path!r}."
        )

    config = _load_json(repo_root / GITHUB_CONFIG_PATH, "GitHub repository config")
    operations = _mapping(config.get("releaseOperations"), "releaseOperations")
    policy_path, policy_relative = _repository_path(
        repo_root,
        operations.get("qualificationPolicyPath"),
        "qualificationPolicyPath",
    )
    evidence_path, evidence_relative = _repository_path(
        repo_root,
        operations.get("qualificationEvidencePath"),
        "qualificationEvidencePath",
    )
    qualification_path, qualification_relative = _qualification_path_for_release(repo_root, release_tag)
    for path, description in (
        (policy_path, "qualification policy"),
        (evidence_path, "qualification evidence"),
        (qualification_path, "qualification record"),
    ):
        if not path.is_file():
            raise ReleaseMilestoneContextError(f"Configured {description} does not exist at {path}.")

    qualification = _load_json(qualification_path, "milestone qualification record")
    candidate = _mapping(qualification.get("candidate"), "milestone qualification candidate")
    artifacts = [_mapping(item, "release receipt artifact") for item in cast(Sequence[object], receipt["artifacts"])]
    dmg_artifacts = [item for item in artifacts if item.get("kind") == "dmg"]
    appcast_artifacts = [item for item in artifacts if item.get("kind") == "appcast"]
    if len(dmg_artifacts) != 1 or len(appcast_artifacts) != 1:
        raise ReleaseMilestoneContextError("Milestone release receipt must contain one DMG and one appcast artifact.")
    expected_candidate = {
        "package_version": versions["package"],
        "public_version": versions["public"],
        "build_version": versions["build"],
        "release_tag": release_tag,
        "source_git_sha": receipt["source_sha"],
        "workflow": workflow["name"],
        "release_run_id": workflow["run_id"],
        "release_id": release["id"],
        "dmg_sha256": dmg_artifacts[0]["sha256"],
        "appcast_sha256": appcast_artifacts[0]["sha256"],
        "signed_app_tree_sha256": receipt["signed_app_tree_sha256"],
    }
    for field, expected in expected_candidate.items():
        if candidate.get(field) != expected:
            raise ReleaseMilestoneContextError(
                f"Milestone qualification candidate.{field} does not match the checked release receipt."
            )
    if len(dmg_artifacts) != 1 or candidate.get("dmg_name") != dmg_artifacts[0].get("name"):
        raise ReleaseMilestoneContextError(
            "Milestone qualification candidate.dmg_name does not match the checked release receipt."
        )

    try:
        version = parse_release_version(cast(str, versions["package"]))
    except ReleaseError as error:
        raise ReleaseMilestoneContextError(f"Milestone release version is invalid: {error}") from error
    publication_relative = f"docs/release-evidence/{release_tag}/publication-record.json"
    publication_path = repo_root / publication_relative
    publication = _load_json(publication_path, "milestone publication record")
    expected_publication = {
        "schema_version": 1,
        "release_tag": release_tag,
        "release_id": release["id"],
        "source_sha": receipt["source_sha"],
        "workflow_run_id": workflow["run_id"],
        "receipt_file_sha256": receipt_file_sha256,
    }
    for field, expected in expected_publication.items():
        if publication.get(field) != expected:
            raise ReleaseMilestoneContextError(
                f"Milestone publication record {field} does not match the checked release receipt."
            )
    if effective_successful_workflow_run_id(publication) is None:
        raise ReleaseMilestoneContextError(
            "Milestone publication record does not contain a successful release or recovery workflow."
        )
    live_pages = _mapping(publication.get("live_pages"), "milestone publication live_pages")
    if (
        live_pages.get("state") != "verified"
        or live_pages.get("sha256") != appcast_artifacts[0]["sha256"]
        or live_pages.get("url") != "https://cbusillo.github.io/BD_to_AVP/appcast.xml"
    ):
        raise ReleaseMilestoneContextError(
            "Milestone publication record does not verify the checked release appcast digest."
        )
    published_at = publication.get("published_at")
    created_at = release.get("created_at")
    if not isinstance(published_at, str) or not isinstance(created_at, str):
        raise ReleaseMilestoneContextError("Milestone publication timestamps must be strings.")
    try:
        if datetime.fromisoformat(published_at.replace("Z", "+00:00")) < datetime.fromisoformat(
            created_at.replace("Z", "+00:00")
        ):
            raise ReleaseMilestoneContextError("Milestone publication timestamp predates release creation.")
    except ValueError as error:
        raise ReleaseMilestoneContextError("Milestone publication timestamps are invalid.") from error

    source_sha = cast(str, receipt["source_sha"])
    _require_tracked_path(repo_root, receipt_relative, "Milestone release receipt")
    _require_tracked_path(repo_root, publication_relative, "Milestone publication record")
    _require_source_ancestor(repo_root, source_sha)
    return ReleaseMilestoneContext(
        candidate_sha=source_sha,
        evidence_path=evidence_relative,
        first_candidate_of_cycle=version.first_candidate_of_cycle,
        policy_path=policy_relative,
        qualification_path=qualification_relative,
        release_receipt_path=receipt_relative,
        release_stage=version.stage,
        release_tag=release_tag,
    )


def _write_github_output(path: Path, outputs: Mapping[str, str]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for key, value in outputs.items():
            handle.write(f"{key}={value}\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resolve checked post-publication milestone qualification context.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--release-receipt", type=Path)
    source.add_argument("--manifest", type=Path)
    source.add_argument("--base-sha")
    source.add_argument("--cancelled-attempt-tag")
    parser.add_argument("--head-sha")
    parser.add_argument("--head-branch")
    parser.add_argument("--base-repo")
    parser.add_argument("--head-repo")
    parser.add_argument("--base-branch")
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.cancelled_attempt_tag is not None:
            record = validate_cancelled_attempt_record(args.repo_root.resolve(), args.cancelled_attempt_tag)
            outputs = {
                "build_version": _string(record.get("build_version"), "cancelled release build version"),
                "release_tag": args.cancelled_attempt_tag,
                "status": "validated_cancelled_attempt",
            }
            if args.github_output is not None:
                _write_github_output(args.github_output, outputs)
            print(json.dumps(outputs, sort_keys=True))
            return 0
        receipt_path = args.release_receipt
        manifest_path = args.manifest
        if args.base_sha is not None:
            if not all(
                (
                    args.head_sha,
                    args.head_branch,
                    args.base_repo,
                    args.head_repo,
                    args.base_branch,
                )
            ):
                raise ReleaseMilestoneContextError(
                    "Pull-request discovery requires head/base SHA, branch, and repository identity inputs."
                )
            manifest_path = discover_milestone_manifest(
                args.repo_root.resolve(),
                base_sha=args.base_sha,
                head_sha=args.head_sha,
                head_branch=args.head_branch,
                base_repo=args.base_repo,
                head_repo=args.head_repo,
                base_branch=args.base_branch,
            )
            receipt_path = None
            if manifest_path is None:
                receipt_path = discover_milestone_receipt(
                    args.repo_root.resolve(),
                    base_sha=args.base_sha,
                    head_sha=args.head_sha,
                    head_branch=args.head_branch,
                    base_repo=args.base_repo,
                    head_repo=args.head_repo,
                    base_branch=args.base_branch,
                )
        if manifest_path is not None:
            context = resolve_milestone_manifest_context(args.repo_root, manifest_path)
            if args.base_sha is not None:
                require_manifest_runner_sha(context, args.base_sha)
                require_manifest_evidence_baseline(context, args.repo_root.resolve(), args.base_sha)
        elif receipt_path is None:
            outputs = {"required": "false"}
            if args.github_output is not None:
                _write_github_output(args.github_output, outputs)
            print(json.dumps(outputs, sort_keys=True))
            return 0
        else:
            context = resolve_milestone_context(args.repo_root, receipt_path)
    except ReleaseMilestoneContextError as error:
        parser.exit(1, f"{error}\n")
    outputs = context.github_outputs()
    if args.github_output is not None:
        _write_github_output(args.github_output, outputs)
    print(json.dumps(outputs, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
