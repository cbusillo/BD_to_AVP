from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys

from collections.abc import Mapping, Sequence
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
from scripts.release_receipt import ReleaseReceiptError, load_validated_checked_receipt


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
RECOVERY_AUTHORIZATION_PATHS = frozenset({"docs/release-evidence/v0.3.0-pypi-recovery.json"})
EXPECTED_REPOSITORY = "cbusillo/BD_to_AVP"
EXPECTED_BASE_BRANCH = "main"
IMMUTABLE_CANDIDATE_FIELDS = (
    "source_git_sha",
    "release_run_id",
    "release_id",
    "dmg_sha256",
    "appcast_sha256",
    "signed_app_tree_sha256",
)


class ReleaseMilestoneContextError(RuntimeError):
    pass


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


def _validate_prepublication_candidate_transition(
    repo_root: Path,
    *,
    base_sha: str,
    candidate: Mapping[str, Any],
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
    if any(field not in base_candidate or base_candidate[field] is None for field in IMMUTABLE_CANDIDATE_FIELDS):
        raise ReleaseMilestoneContextError(
            "Prepublication qualification must advance from a bound published candidate."
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
        for index, raw_record in enumerate(_sequence(ledger.get("releases"), f"{description} releases")):
            record = _mapping(raw_record, f"{description} release {index}")
            tag = _string(record.get("tag"), f"{description} release tag")
            if tag in records:
                raise ReleaseMilestoneContextError(f"{description.capitalize()} may not contain duplicate tags.")
            records[tag] = record
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
    parser.add_argument("--head-sha")
    parser.add_argument("--head-branch")
    parser.add_argument("--base-repo")
    parser.add_argument("--head-repo")
    parser.add_argument("--base-branch")
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args(argv)
    try:
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
