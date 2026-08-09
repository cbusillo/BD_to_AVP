from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from scripts.release import ReleaseError, parse_build_version, parse_release_version
from scripts.release_evidence import effective_successful_workflow_run_id
from scripts.release_receipt import ReleaseReceiptError, load_validated_checked_receipt


REPO_ROOT = Path(__file__).resolve().parents[1]
GITHUB_CONFIG_PATH = Path(".github/github.json")
EVIDENCE_INDEX_PATH = "docs/qualification/release-evidence-v1.json"
RECEIPT_PATH_PATTERN = re.compile(r"^docs/release-evidence/(v[^/]+)/release-receipt\.json$")
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
    required: bool = True

    def github_outputs(self) -> dict[str, str]:
        return {
            key: str(value).lower() if isinstance(value, bool) else str(value) for key, value in asdict(self).items()
        }


def _mapping(value: object, description: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ReleaseMilestoneContextError(f"{description} must be a JSON object.")
    return cast(Mapping[str, Any], value)


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
    qualification_relative: str,
    candidate: Mapping[str, Any],
) -> None:
    base_qualification = _load_json_at_revision(
        repo_root,
        base_sha,
        qualification_relative,
        "base qualification record",
    )
    base_candidate = _mapping(base_qualification.get("candidate"), "base qualification candidate")
    if any(base_candidate.get(field) is None for field in IMMUTABLE_CANDIDATE_FIELDS):
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
    if next_version.stage != "stable" or next_version.order_key <= base_version.order_key or next_build <= base_build:
        raise ReleaseMilestoneContextError(
            "Prepublication qualification must advance to a newer Stable version and global build."
        )
    expected_identity = {
        "package_version": next_version.text,
        "public_version": next_version.public_version,
        "build_version": str(next_build),
        "release_tag": next_version.release_tag,
        "dmg_name": f"3D-Blu-ray-to-Vision-Pro-{next_version.public_version}.dmg",
        "workflow": "Stable",
    }
    for field, expected in expected_identity.items():
        if candidate.get(field) != expected:
            raise ReleaseMilestoneContextError(
                f"Prepublication qualification candidate.{field} does not match the derived Stable identity."
            )


def _validate_append_only_evidence_index(repo_root: Path, *, base_sha: str) -> None:
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


def _repository_path(repo_root: Path, value: object, description: str) -> tuple[Path, str]:
    if not isinstance(value, str) or not value:
        raise ReleaseMilestoneContextError(f"{description} must be a repository-relative path.")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts or value.startswith("./"):
        raise ReleaseMilestoneContextError(f"{description} must be a canonical repository-relative path.")
    return repo_root / relative, relative.as_posix()


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
    _qualification_path, qualification_relative = _repository_path(
        repo_root,
        operations.get("qualificationRecordPath"),
        "qualificationRecordPath",
    )
    checked_release_mutation = any(
        path.startswith("docs/release-evidence/") and path not in RECOVERY_AUTHORIZATION_PATHS for path in changed_paths
    )
    evidence_index_mutation = EVIDENCE_INDEX_PATH in changed_paths
    qualification_mutation = qualification_relative in changed_paths
    if not receipt_matches:
        if checked_release_mutation:
            raise ReleaseMilestoneContextError(
                "Release evidence changes require exactly one checked release receipt in the pull-request diff."
            )
        unbound_candidate = False
        if qualification_mutation:
            qualification = _load_json(repo_root / qualification_relative, "configured qualification record")
            candidate = _mapping(qualification.get("candidate"), "configured qualification candidate")
            if any(candidate.get(field) is not None for field in IMMUTABLE_CANDIDATE_FIELDS):
                raise ReleaseMilestoneContextError(
                    "Published qualification record changes require the checked release receipt and milestone gate."
                )
            _validate_prepublication_candidate_transition(
                repo_root,
                base_sha=base_sha,
                qualification_relative=qualification_relative,
                candidate=candidate,
            )
            unbound_candidate = True
        if evidence_index_mutation and not unbound_candidate:
            raise ReleaseMilestoneContextError(
                "Release evidence changes require exactly one checked release receipt in the pull-request diff."
            )
        if evidence_index_mutation:
            _validate_append_only_evidence_index(repo_root, base_sha=base_sha)
        return None
    if len(receipt_matches) != 1:
        raise ReleaseMilestoneContextError(
            "A release evidence pull request must contain exactly one checked release receipt."
        )
    receipt_relative, release_tag = receipt_matches[0]
    expected_branch = f"automation/release-evidence-{release_tag}"
    if head_branch != expected_branch:
        raise ReleaseMilestoneContextError(f"Release evidence changes must use idempotent branch {expected_branch!r}.")
    out_of_scope = [path for path in changed_paths if not path.startswith("docs/")]
    if out_of_scope:
        raise ReleaseMilestoneContextError(f"Release evidence pull requests may change only docs/: {out_of_scope!r}.")
    return repo_root / receipt_relative


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
    qualification_path, qualification_relative = _repository_path(
        repo_root,
        operations.get("qualificationRecordPath"),
        "qualificationRecordPath",
    )
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
            receipt_path = discover_milestone_receipt(
                args.repo_root.resolve(),
                base_sha=args.base_sha,
                head_sha=args.head_sha,
                head_branch=args.head_branch,
                base_repo=args.base_repo,
                head_repo=args.head_repo,
                base_branch=args.base_branch,
            )
        if receipt_path is None:
            outputs = {"required": "false"}
            if args.github_output is not None:
                _write_github_output(args.github_output, outputs)
            print(json.dumps(outputs, sort_keys=True))
            return 0
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
