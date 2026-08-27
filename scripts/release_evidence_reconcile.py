from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from scripts.release_evidence_v2 import (
    CAPTURE_NAME,
    EVIDENCE_ROOT,
    QUALIFICATION_NAME,
    ReleaseEvidenceV2Error,
    check_index_v2,
    evidence_ref_for_tag,
    sanitize_release_tag,
    verify_tag,
    verify_write_once_history,
)
from scripts.release_receipt import EXPECTED_REPOSITORY


REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN_BRANCH = "main"
INDEX_PATH = "docs/release-evidence/index-v2.json"
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
GIT_TIMEOUT_SECONDS = 60
GITHUB_TIMEOUT_SECONDS = 30


class ReleaseEvidenceReconciliationError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReconciliationPlan:
    repository: str
    release_tag: str
    evidence_ref: str
    evidence_sha: str
    main_sha: str
    operator: str
    source_sha: str
    release_id: int
    release_actor: str
    release_run_id: int
    capture_actor: str
    capture_run_id: int
    qualification_actor: str
    qualification_run_id: int
    artifact_id: int
    artifact_name: str
    artifact_sha256: str
    required_checks: tuple[str, ...]

    def payload(self) -> dict[str, Any]:
        return {
            "artifact": {
                "artifact_id": self.artifact_id,
                "name": self.artifact_name,
                "sha256": self.artifact_sha256,
            },
            "capture": {"actor": self.capture_actor, "run_id": self.capture_run_id},
            "evidence_ref": self.evidence_ref,
            "evidence_sha": self.evidence_sha,
            "main_sha": self.main_sha,
            "operator": self.operator,
            "qualification": {"actor": self.qualification_actor, "run_id": self.qualification_run_id},
            "release": {"actor": self.release_actor, "id": self.release_id, "run_id": self.release_run_id},
            "release_tag": self.release_tag,
            "repository": self.repository,
            "required_checks": list(self.required_checks),
            "source_sha": self.source_sha,
        }

    @property
    def digest(self) -> str:
        return hashlib.sha256(_canonical_json(self.payload())).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        return {**self.payload(), "plan_digest": self.digest}


@dataclass(frozen=True)
class PreflightResult:
    action: str
    plan: ReconciliationPlan
    pull_request: Mapping[str, Any] | None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"action": self.action, "plan": self.plan.as_dict()}
        if self.pull_request is not None:
            payload["pull_request"] = {
                "number": self.pull_request["number"],
                "url": self.pull_request["url"],
            }
        return payload


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _string(value: object, description: str) -> str:
    if not isinstance(value, str) or not value:
        raise ReleaseEvidenceReconciliationError(f"{description} must be a non-empty string.")
    return value


def _sha(value: object, description: str) -> str:
    result = _string(value, description)
    if not SHA_PATTERN.fullmatch(result):
        raise ReleaseEvidenceReconciliationError(f"{description} must be a full lowercase Git SHA.")
    return result


def _sha256(value: object, description: str) -> str:
    result = _string(value, description)
    if not SHA256_PATTERN.fullmatch(result):
        raise ReleaseEvidenceReconciliationError(f"{description} must be a lowercase SHA-256 digest.")
    return result


def _positive_integer(value: object, description: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ReleaseEvidenceReconciliationError(f"{description} must be a positive integer.")
    return value


def _mapping(value: object, description: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ReleaseEvidenceReconciliationError(f"{description} must be an object.")
    return cast(Mapping[str, Any], value)


def _git_output(repo_root: Path, arguments: Sequence[str], description: str) -> str:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=repo_root,
            text=True,
            capture_output=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise ReleaseEvidenceReconciliationError(f"Timed out while attempting to {description}.") from error
    except OSError as error:
        raise ReleaseEvidenceReconciliationError(f"Unable to start git while attempting to {description}.") from error
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic"
        raise ReleaseEvidenceReconciliationError(f"Unable to {description}: {detail}")
    return result.stdout.strip()


def _operator_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for name in (
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "CODEX_GITHUB_TOKEN",
        "GH_ENTERPRISE_TOKEN",
        "GITHUB_ENTERPRISE_TOKEN",
        "GH_CONFIG_DIR",
        "GH_HOST",
        "GH_REPO",
        "XDG_CONFIG_HOME",
    ):
        environment.pop(name, None)
    return environment


def _run_gh(repo_root: Path, arguments: Sequence[str], description: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["gh", *arguments],
            cwd=repo_root,
            text=True,
            capture_output=True,
            env=_operator_environment(),
            timeout=GITHUB_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise ReleaseEvidenceReconciliationError(
            f"Timed out while attempting to {description} with local gh."
        ) from error
    except OSError as error:
        raise ReleaseEvidenceReconciliationError(
            f"Unable to start gh while attempting to {description} with local authentication."
        ) from error


def _gh_json(repo_root: Path, arguments: Sequence[str], description: str) -> Any:
    result = _run_gh(repo_root, arguments, description)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic"
        raise ReleaseEvidenceReconciliationError(f"Unable to {description} with local gh authentication: {detail}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ReleaseEvidenceReconciliationError(f"Unable to parse {description} response from gh.") from error


def _gh_command(repo_root: Path, arguments: Sequence[str], description: str) -> None:
    result = _run_gh(repo_root, arguments, description)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic"
        raise ReleaseEvidenceReconciliationError(f"Unable to {description} with local gh authentication: {detail}")


def _remote_ref_sha(repo_root: Path, ref: str) -> str:
    output = _git_output(repo_root, ["ls-remote", "--exit-code", "origin", ref], f"read remote ref {ref}")
    lines = output.splitlines()
    if len(lines) != 1:
        raise ReleaseEvidenceReconciliationError(f"Remote ref {ref} did not resolve to exactly one commit.")
    sha, _, resolved_ref = lines[0].partition("\t")
    if resolved_ref != ref:
        raise ReleaseEvidenceReconciliationError(f"Remote ref {ref} resolved unexpectedly as {resolved_ref!r}.")
    return _sha(sha, f"remote ref {ref} SHA")


def _fetch_remote_refs(repo_root: Path, evidence_ref: str) -> None:
    _git_output(
        repo_root,
        [
            "fetch",
            "--no-tags",
            "--no-write-fetch-head",
            "origin",
            f"refs/heads/{MAIN_BRANCH}",
            f"refs/heads/{evidence_ref}",
        ],
        "fetch the exact protected main and evidence commits",
    )


def _assert_commit_available(repo_root: Path, sha: str, description: str) -> None:
    _git_output(repo_root, ["cat-file", "-e", f"{sha}^{{commit}}"], f"read {description}")


def _verify_evidence_lineage(repo_root: Path, main_sha: str, evidence_sha: str) -> None:
    base = _git_output(repo_root, ["merge-base", main_sha, evidence_sha], "compute evidence branch merge base")
    if base != main_sha:
        raise ReleaseEvidenceReconciliationError(
            "Evidence branch is stale or diverged: its merge base is not the current protected main SHA."
        )


def _verify_docs_only_diff(repo_root: Path, main_sha: str, evidence_sha: str, release_tag: str) -> None:
    changed = _git_output(
        repo_root,
        ["diff", "--name-only", main_sha, evidence_sha],
        "list evidence branch changes",
    ).splitlines()
    bundle_prefix = f"docs/release-evidence/{release_tag}/"
    allowed = {INDEX_PATH}
    unexpected = sorted(path for path in changed if path not in allowed and not path.startswith(bundle_prefix))
    if unexpected:
        raise ReleaseEvidenceReconciliationError(
            f"Evidence branch changes files outside the exact release bundle: {', '.join(unexpected)}."
        )
    if not any(path.startswith(bundle_prefix) for path in changed):
        raise ReleaseEvidenceReconciliationError("Evidence branch does not change the requested release bundle.")


def _load_json_at_revision(repo_root: Path, revision: str, path: str, description: str) -> Mapping[str, Any]:
    raw = _git_output(repo_root, ["show", f"{revision}:{path}"], f"read {description}")
    try:
        return _mapping(json.loads(raw), description)
    except json.JSONDecodeError as error:
        raise ReleaseEvidenceReconciliationError(f"{description} is not valid JSON.") from error


def _current_operator(repo_root: Path) -> str:
    user = _mapping(_gh_json(repo_root, ["api", "user"], "read active GitHub operator"), "active GitHub operator")
    return _string(user.get("login"), "active GitHub operator login")


def _verify_canonical_checkout(repo_root: Path, repository: str) -> None:
    origin_url = _git_output(repo_root, ["remote", "get-url", "origin"], "resolve the origin remote")
    checkout = _mapping(
        _gh_json(
            repo_root,
            ["repo", "view", origin_url, "--json", "nameWithOwner"],
            "resolve the origin repository",
        ),
        "origin repository",
    )
    if checkout.get("nameWithOwner") != repository:
        raise ReleaseEvidenceReconciliationError(
            f"Current checkout must resolve to the canonical repository {repository}."
        )


def _required_checks_from_metadata(repo_root: Path, main_sha: str) -> tuple[str, ...]:
    metadata = _load_json_at_revision(repo_root, main_sha, ".github/github.json", "protected-main metadata")
    protection = _mapping(metadata.get("branchProtection"), "protected-main metadata branchProtection")
    main = _mapping(protection.get(MAIN_BRANCH), "protected-main metadata main")
    checks = main.get("requiredStatusChecks")
    if not isinstance(checks, list) or not checks or not all(isinstance(check, str) and check for check in checks):
        raise ReleaseEvidenceReconciliationError("Protected-main metadata must list required status checks.")
    if len(set(checks)) != len(checks):
        raise ReleaseEvidenceReconciliationError("Protected-main metadata contains duplicate required status checks.")
    return tuple(sorted(checks))


def _verify_branch_protection(repo_root: Path, repository: str, expected_checks: tuple[str, ...]) -> None:
    protection = _mapping(
        _gh_json(
            repo_root,
            ["api", f"repos/{repository}/branches/{MAIN_BRANCH}/protection"],
            "read protected main configuration",
        ),
        "protected main configuration",
    )
    status_checks = _mapping(protection.get("required_status_checks"), "protected main required status checks")
    contexts = status_checks.get("contexts")
    if not isinstance(contexts, list) or not all(isinstance(context, str) and context for context in contexts):
        raise ReleaseEvidenceReconciliationError("Protected main does not expose a valid required status-check list.")
    if status_checks.get("strict") is not True:
        raise ReleaseEvidenceReconciliationError("Protected main must require branches to be up to date before merge.")
    if tuple(sorted(contexts)) != expected_checks:
        raise ReleaseEvidenceReconciliationError(
            "Protected main required checks do not exactly match repository metadata: "
            f"expected={list(expected_checks)}, actual={sorted(contexts)}."
        )
    for key, expected in (
        ("enforce_admins", True),
        ("allow_force_pushes", False),
        ("allow_deletions", False),
        ("required_conversation_resolution", True),
    ):
        setting = _mapping(protection.get(key), f"protected main {key}")
        if setting.get("enabled") is not expected:
            raise ReleaseEvidenceReconciliationError(f"Protected main {key} must be {expected}.")


def _workflow_identity(value: object, description: str) -> tuple[str, int]:
    workflow = _mapping(value, description)
    return _string(workflow.get("actor"), f"{description} actor"), _positive_integer(
        workflow.get("run_id"), f"{description} run ID"
    )


def _build_plan(
    repo_root: Path,
    *,
    repository: str,
    release_tag: str,
    evidence_ref: str,
    evidence_sha: str,
    main_sha: str,
    operator: str,
    required_checks: tuple[str, ...],
) -> ReconciliationPlan:
    bundle = f"{EVIDENCE_ROOT.as_posix()}/{release_tag}"
    capture = _load_json_at_revision(repo_root, evidence_sha, f"{bundle}/{CAPTURE_NAME}", "capture-v2 record")
    qualification = _load_json_at_revision(
        repo_root, evidence_sha, f"{bundle}/{QUALIFICATION_NAME}", "qualification-v2 record"
    )
    receipt_binding = _mapping(capture.get("receipt"), "capture-v2 release receipt binding")
    receipt_path = _string(receipt_binding.get("path"), "capture-v2 release receipt path")
    receipt = _load_json_at_revision(repo_root, evidence_sha, receipt_path, "archived release receipt")
    release = _mapping(receipt.get("release"), "archived release receipt release")
    release_workflow = _mapping(receipt.get("workflow"), "archived release receipt workflow")
    source_sha = _sha(capture.get("source_sha"), "capture-v2 source SHA")
    if qualification.get("source_sha") != source_sha:
        raise ReleaseEvidenceReconciliationError("qualification-v2 source SHA does not exactly match capture-v2.")
    if receipt.get("source_sha") != source_sha:
        raise ReleaseEvidenceReconciliationError(
            "Archived release receipt source SHA does not exactly match capture-v2."
        )
    release_actor, release_run_id = _workflow_identity(capture.get("release_workflow"), "capture-v2 release workflow")
    receipt_actor, receipt_run_id = _workflow_identity(release_workflow, "archived release receipt workflow")
    if (release_actor, release_run_id) != (receipt_actor, receipt_run_id):
        raise ReleaseEvidenceReconciliationError(
            "capture-v2 release workflow does not exactly match the archived release receipt."
        )
    capture_actor, capture_run_id = _workflow_identity(capture.get("capture_workflow"), "capture-v2 workflow")
    qualification_actor, qualification_run_id = _workflow_identity(
        qualification.get("successful_milestone"), "qualification-v2 successful milestone"
    )
    artifact = _mapping(qualification.get("artifact"), "qualification-v2 artifact")
    artifact_run_id = _positive_integer(artifact.get("run_id"), "qualification-v2 artifact run ID")
    if artifact_run_id != qualification_run_id:
        raise ReleaseEvidenceReconciliationError(
            "qualification-v2 artifact run ID does not match the successful milestone."
        )
    if (
        release.get("tag") != release_tag
        or capture.get("release_tag") != release_tag
        or qualification.get("release_tag") != release_tag
    ):
        raise ReleaseEvidenceReconciliationError("Evidence records do not exactly match the requested release tag.")
    return ReconciliationPlan(
        repository=repository,
        release_tag=release_tag,
        evidence_ref=evidence_ref,
        evidence_sha=evidence_sha,
        main_sha=main_sha,
        operator=operator,
        source_sha=source_sha,
        release_id=_positive_integer(release.get("id"), "archived release ID"),
        release_actor=release_actor,
        release_run_id=release_run_id,
        capture_actor=capture_actor,
        capture_run_id=capture_run_id,
        qualification_actor=qualification_actor,
        qualification_run_id=qualification_run_id,
        artifact_id=_positive_integer(artifact.get("artifact_id"), "qualification artifact ID"),
        artifact_name=_string(artifact.get("name"), "qualification artifact name"),
        artifact_sha256=_sha256(artifact.get("sha256"), "qualification artifact SHA-256"),
        required_checks=required_checks,
    )


def _open_main_pull_requests(repo_root: Path, repository: str) -> list[Mapping[str, Any]]:
    value = _gh_json(
        repo_root,
        [
            "pr",
            "list",
            "--repo",
            repository,
            "--base",
            MAIN_BRANCH,
            "--state",
            "open",
            "--limit",
            "1000",
            "--json",
            "number,url,baseRefName,headRefName,headRefOid,headRepository,author",
        ],
        "list open pull requests to protected main",
    )
    if not isinstance(value, list):
        raise ReleaseEvidenceReconciliationError("Open pull-request query did not return a list.")
    return [_mapping(item, "open pull request") for item in value]


def _pr_action(
    pull_requests: Sequence[Mapping[str, Any]], plan: ReconciliationPlan
) -> tuple[str, Mapping[str, Any] | None]:
    exact: list[Mapping[str, Any]] = []
    for pull_request in pull_requests:
        if pull_request.get("baseRefName") != MAIN_BRANCH:
            raise ReleaseEvidenceReconciliationError("Open pull-request query returned a non-main base.")
        head_ref = _string(pull_request.get("headRefName"), "open pull-request head ref")
        if head_ref != plan.evidence_ref:
            number = _positive_integer(pull_request.get("number"), "open pull-request number")
            raise ReleaseEvidenceReconciliationError(
                f"Open unrelated pull request #{number} targets protected main; "
                "reconciliation refuses to create another PR."
            )
        if pull_request.get("headRefOid") != plan.evidence_sha:
            raise ReleaseEvidenceReconciliationError(
                "Open evidence pull request does not point at the exact remote evidence SHA."
            )
        head_repository = _mapping(pull_request.get("headRepository"), "open evidence pull-request head repository")
        if head_repository.get("nameWithOwner") != plan.repository:
            raise ReleaseEvidenceReconciliationError(
                "Open evidence pull request does not originate from the canonical repository."
            )
        author = _mapping(pull_request.get("author"), "open evidence pull-request author")
        if author.get("login") != plan.operator:
            raise ReleaseEvidenceReconciliationError(
                "Open exact evidence pull request was not created by the active local GitHub operator."
            )
        exact.append(pull_request)
    if len(exact) > 1:
        raise ReleaseEvidenceReconciliationError(
            "More than one exact evidence pull request is open; reconciliation refuses ambiguity."
        )
    if exact:
        return "adopt", exact[0]
    return "open", None


def preflight(repo_root: Path, *, release_tag: str, repository: str = EXPECTED_REPOSITORY) -> PreflightResult:
    repo_root = repo_root.resolve()
    tag = sanitize_release_tag(release_tag)
    if repository != EXPECTED_REPOSITORY:
        raise ReleaseEvidenceReconciliationError(
            f"Repository must be the canonical release repository {EXPECTED_REPOSITORY}."
        )
    _verify_canonical_checkout(repo_root, repository)
    evidence_ref = evidence_ref_for_tag(tag)
    remote_evidence_ref = f"refs/heads/{evidence_ref}"
    remote_main_ref = f"refs/heads/{MAIN_BRANCH}"
    evidence_sha = _remote_ref_sha(repo_root, remote_evidence_ref)
    main_sha = _remote_ref_sha(repo_root, remote_main_ref)
    _fetch_remote_refs(repo_root, evidence_ref)
    _assert_commit_available(repo_root, evidence_sha, "exact remote evidence commit")
    _assert_commit_available(repo_root, main_sha, "current protected main commit")
    _verify_evidence_lineage(repo_root, main_sha, evidence_sha)
    _verify_docs_only_diff(repo_root, main_sha, evidence_sha, tag)
    try:
        verified = verify_tag(repo_root, tag, verification_revision=evidence_sha)
        terminal_class = verified.get("class")
        if terminal_class != "v2-qualified":
            if terminal_class == "v2-failed":
                raise ReleaseEvidenceReconciliationError(
                    "Durable failed v2 evidence cannot be reconciled into protected main."
                )
            raise ReleaseEvidenceReconciliationError(
                f"Evidence bundle must have terminal class v2-qualified, found {terminal_class!r}."
            )
        verify_write_once_history(repo_root, main_sha, verification_revision=evidence_sha)
        check_index_v2(repo_root, revision=evidence_sha)
    except ReleaseEvidenceV2Error as error:
        raise ReleaseEvidenceReconciliationError(f"Offline release-evidence verification failed: {error}") from error
    required_checks = _required_checks_from_metadata(repo_root, main_sha)
    _verify_branch_protection(repo_root, repository, required_checks)
    operator = _current_operator(repo_root)
    plan = _build_plan(
        repo_root,
        repository=repository,
        release_tag=tag,
        evidence_ref=evidence_ref,
        evidence_sha=evidence_sha,
        main_sha=main_sha,
        operator=operator,
        required_checks=required_checks,
    )
    action, pull_request = _pr_action(_open_main_pull_requests(repo_root, repository), plan)
    if _remote_ref_sha(repo_root, remote_evidence_ref) != evidence_sha:
        raise ReleaseEvidenceReconciliationError(
            "Remote evidence branch moved during preflight; rerun from fresh evidence."
        )
    if _remote_ref_sha(repo_root, remote_main_ref) != main_sha:
        raise ReleaseEvidenceReconciliationError("Protected main moved during preflight; rerun from fresh evidence.")
    return PreflightResult(action=action, plan=plan, pull_request=pull_request)


def _require_echo(actual: str, expected: str, description: str) -> None:
    if actual != expected:
        raise ReleaseEvidenceReconciliationError(f"{description} echo does not match the fresh preflight plan.")


def _create_pull_request(repo_root: Path, plan: ReconciliationPlan) -> None:
    body = "\n".join(
        (
            "Release evidence v2 reconciliation.",
            "",
            f"- Release tag: `{plan.release_tag}`",
            f"- Evidence ref: `{plan.evidence_ref}`",
            f"- Evidence SHA: `{plan.evidence_sha}`",
            f"- Protected main SHA: `{plan.main_sha}`",
            f"- Reconciliation plan digest: `{plan.digest}`",
            "",
            "This PR is intentionally opened by the active local GitHub operator after offline verification.",
        )
    )
    _gh_command(
        repo_root,
        [
            "pr",
            "create",
            "--repo",
            plan.repository,
            "--base",
            MAIN_BRANCH,
            "--head",
            plan.evidence_ref,
            "--title",
            f"Reconcile release evidence {plan.release_tag}",
            "--body",
            body,
        ],
        "open the final protected release-evidence pull request",
    )


def reconcile(
    repo_root: Path,
    *,
    release_tag: str,
    evidence_sha: str,
    main_sha: str,
    plan_digest: str,
    repository: str = EXPECTED_REPOSITORY,
) -> PreflightResult:
    result = preflight(repo_root, release_tag=release_tag, repository=repository)
    _require_echo(_sha(evidence_sha, "evidence SHA"), result.plan.evidence_sha, "Evidence SHA")
    _require_echo(_sha(main_sha, "main SHA"), result.plan.main_sha, "Protected main SHA")
    _require_echo(_sha256(plan_digest, "plan digest"), result.plan.digest, "Plan digest")
    if result.action == "adopt":
        return result
    _create_pull_request(repo_root.resolve(), result.plan)
    adopted = preflight(repo_root, release_tag=release_tag, repository=repository)
    if adopted.action != "adopt":
        raise ReleaseEvidenceReconciliationError(
            "Pull-request creation did not produce one exact adoptable reconciliation PR."
        )
    return adopted


def _parse_preflight_arguments(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify release-evidence v2 before a protected reconciliation PR.")
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--repository", default=EXPECTED_REPOSITORY)
    return parser.parse_args(argv)


def _parse_reconcile_arguments(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Open or adopt exactly one verified protected reconciliation PR.")
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--evidence-sha", required=True)
    parser.add_argument("--main-sha", required=True)
    parser.add_argument("--plan-digest", required=True)
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--repository", default=EXPECTED_REPOSITORY)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(argv) if argv is not None else sys.argv[1:]
    if not arguments or arguments[0] not in {"preflight", "reconcile"}:
        print("release-evidence-reconcile: choose `preflight` or `reconcile`.", file=sys.stderr)
        return 2
    try:
        if arguments[0] == "preflight":
            args = _parse_preflight_arguments(arguments[1:])
            output = preflight(args.repo_root, release_tag=args.release_tag, repository=args.repository).as_dict()
        else:
            args = _parse_reconcile_arguments(arguments[1:])
            output = reconcile(
                args.repo_root,
                release_tag=args.release_tag,
                evidence_sha=args.evidence_sha,
                main_sha=args.main_sha,
                plan_digest=args.plan_digest,
                repository=args.repository,
            ).as_dict()
    except (OSError, ReleaseEvidenceReconciliationError, ReleaseEvidenceV2Error) as error:
        print(f"release-evidence-reconcile: {error}", file=sys.stderr)
        return 1
    print(json.dumps(output, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
