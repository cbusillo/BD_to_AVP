from __future__ import annotations

import argparse
import json
import subprocess
import sys

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, cast

from scripts.qualify_release_scope import (
    QualificationScopeError,
    classify_release_scope,
    git_changed_paths,
    load_evidence,
    load_policy,
    load_qualification_overrides,
    validate_policy,
)
from scripts.release import ReleaseError, parse_release_tag
from scripts.release_milestone_context import (
    ReleaseMilestoneContext,
    ReleaseMilestoneContextError,
    resolve_milestone_context,
    resolve_milestone_manifest_context,
)
from scripts.release_qualification_manifest import (
    MANIFEST_NAME,
    ReleaseQualificationManifestError,
    load_validated_manifest,
    validate_manifest_evidence_history,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
STATUS_TYPE = "bd_to_avp.release_qualification_status"
STATUS_SCHEMA_VERSION = 1
STATUS_GROUPS = ("completed", "stale", "blocking", "optional", "operator_required")
STALE_TRIGGERS = {
    "carry_disallowed",
    "expired",
    "explicit_retest",
    "invalidated",
    "milestone_receipt_mismatch",
}


class ReleaseQualificationControllerError(RuntimeError):
    pass


@dataclass(frozen=True)
class EvidenceBinding:
    context: ReleaseMilestoneContext
    manifest: Mapping[str, Any] | None
    mode: str


def _mapping(value: object, description: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ReleaseQualificationControllerError(f"{description} must be a JSON object.")
    return cast(Mapping[str, Any], value)


def _sequence(value: object, description: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise ReleaseQualificationControllerError(f"{description} must be a JSON array.")
    return value


def _string(value: object, description: str) -> str:
    if not isinstance(value, str) or not value:
        raise ReleaseQualificationControllerError(f"{description} must be a non-empty string.")
    return value


def _canonical_release_tag(value: str) -> str:
    try:
        version = parse_release_tag(value)
    except ReleaseError as error:
        raise ReleaseQualificationControllerError(f"Release tag is invalid: {error}") from error
    if version.release_tag != value:
        raise ReleaseQualificationControllerError(
            f"Release tag must use its canonical public spelling {version.release_tag!r}."
        )
    return value


def _read_json_at_revision(
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
        detail = result.stderr.strip() or "git show failed"
        raise ReleaseQualificationControllerError(
            f"Unable to read {description} from runner revision {revision}: {detail}"
        )
    try:
        return _mapping(json.loads(result.stdout), description)
    except json.JSONDecodeError as error:
        raise ReleaseQualificationControllerError(
            f"Invalid JSON in {description} from runner revision {revision}: {error}"
        ) from error


def _require_checked_file(repo_root: Path, relative_path: str, description: str) -> None:
    path = Path(relative_path)
    if path.is_absolute() or ".." in path.parts or relative_path.startswith("./"):
        raise ReleaseQualificationControllerError(f"{description} must be a canonical repository-relative path.")
    result = subprocess.run(
        ["git", "show", f"HEAD:{relative_path}"],
        cwd=repo_root,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise ReleaseQualificationControllerError(f"{description} must be committed in the current repository HEAD.")
    try:
        current = (repo_root / path).read_bytes()
    except OSError as error:
        raise ReleaseQualificationControllerError(
            f"Unable to read {description} at {relative_path}: {error}"
        ) from error
    if current != result.stdout:
        raise ReleaseQualificationControllerError(f"{description} must match the checked content in repository HEAD.")


def resolve_evidence_binding(repo_root: Path, release_tag: str) -> EvidenceBinding:
    release_tag = _canonical_release_tag(release_tag)
    release_root = repo_root / "docs" / "release-evidence" / release_tag
    manifest_path = release_root / MANIFEST_NAME
    if manifest_path.is_file():
        context = resolve_milestone_manifest_context(repo_root, manifest_path)
        manifest = load_validated_manifest(manifest_path, repo_root=repo_root)
        mode = "manifest"
    else:
        receipt_path = release_root / "release-receipt.json"
        if not receipt_path.is_file():
            raise ReleaseQualificationControllerError(
                f"No checked qualification manifest or release receipt exists for {release_tag}."
            )
        context = resolve_milestone_context(repo_root, receipt_path)
        manifest = None
        mode = "legacy"
    if context.release_tag != release_tag:
        raise ReleaseQualificationControllerError(
            f"Resolved qualification evidence belongs to {context.release_tag}, not {release_tag}."
        )
    return EvidenceBinding(context=context, manifest=manifest, mode=mode)


def _load_bound_policy(repo_root: Path, binding: EvidenceBinding) -> Mapping[str, Any]:
    context = binding.context
    if binding.mode == "manifest":
        if not context.runner_sha:
            raise ReleaseQualificationControllerError("Manifest-backed qualification status requires a runner SHA.")
        return validate_policy(
            _read_json_at_revision(
                repo_root,
                context.runner_sha,
                context.policy_path,
                "runner-bound release qualification policy",
            )
        )
    return load_policy(repo_root / context.policy_path)


def _policy_cases(policy: Mapping[str, Any]) -> Mapping[str, Mapping[str, Any]]:
    cases: dict[str, Mapping[str, Any]] = {}
    for raw_case in _sequence(policy.get("cases"), "release qualification cases"):
        case = _mapping(raw_case, "release qualification case")
        case_id = _string(case.get("id"), "release qualification case ID")
        cases[case_id] = case
    return cases


def _case_categories(
    result: Mapping[str, Any],
    policy_case: Mapping[str, Any],
    blocking_case_ids: set[str],
) -> tuple[str, ...]:
    case_id = _string(result.get("case_id"), "qualification result case ID")
    status = _string(result.get("status"), f"qualification result {case_id} status")
    trigger = _string(result.get("trigger"), f"qualification result {case_id} trigger")
    categories: list[str] = []
    if status in {"covered", "carry"}:
        categories.append("completed")
    if status == "retest" and trigger in STALE_TRIGGERS:
        categories.append("stale")
    if case_id in blocking_case_ids:
        categories.append("blocking")
    if (
        result.get("applicable") is True
        and not bool(result.get("blocking"))
        and (result.get("evidence_due") is True or result.get("operational_status") in {"due", "failed", "skipped"})
    ):
        categories.append("optional")
    if (
        policy_case.get("execution_mode") == "operator_assisted"
        and result.get("evidence_due") is True
        and result.get("applicable") is True
    ):
        categories.append("operator_required")
    return tuple(category for category in STATUS_GROUPS if category in categories)


def _overall_status(groups: Mapping[str, Sequence[str]]) -> str:
    if groups["blocking"]:
        return "blocked"
    if groups["operator_required"]:
        return "operator_required"
    if groups["stale"] or groups["optional"]:
        return "ready_with_optional_actions"
    return "complete"


def build_status(
    repo_root: Path,
    release_tag: str,
    *,
    as_of: date | None = None,
) -> dict[str, Any]:
    repo_root = repo_root.resolve()
    binding = resolve_evidence_binding(repo_root, release_tag)
    context = binding.context
    checked_paths = {
        context.evidence_path: "qualification evidence",
        context.qualification_path: "qualification record",
        context.release_receipt_path: "release receipt",
    }
    if binding.mode == "legacy":
        checked_paths[context.policy_path] = "qualification policy"
        checked_paths[f"docs/release-evidence/{context.release_tag}/publication-record.json"] = "publication record"
    elif context.manifest_path:
        checked_paths[context.manifest_path] = "qualification manifest"
    for checked_path, description in checked_paths.items():
        _require_checked_file(repo_root, checked_path, description)
    if binding.manifest is not None:
        canonical_evidence = _mapping(binding.manifest.get("canonical_evidence"), "manifest canonical evidence")
        validate_manifest_evidence_history(
            binding.manifest,
            repo_root=repo_root,
            base_revision=_string(canonical_evidence.get("base_sha"), "manifest evidence base SHA"),
        )
    policy = _load_bound_policy(repo_root, binding)
    evidence = load_evidence(repo_root / context.evidence_path)
    qualification_id, fresh_retest = load_qualification_overrides(
        repo_root / context.qualification_path,
        policy,
    )
    classification = classify_release_scope(
        policy,
        evidence,
        candidate_sha=context.candidate_sha,
        changed_paths=git_changed_paths(repo_root),
        repo=repo_root,
        qualification_id=qualification_id,
        fresh_retest=fresh_retest,
        release_stage=context.release_stage,
        first_candidate_of_cycle=context.first_candidate_of_cycle,
        workflow_phase="milestone",
        milestone_receipt_path=repo_root / context.release_receipt_path,
        as_of=as_of,
    )

    blocking_case_ids = set(
        _string(value, "blocking qualification case ID")
        for value in _sequence(classification.get("blocking_retests"), "blocking qualification cases")
    )
    cases_by_id = _policy_cases(policy)
    groups: dict[str, list[str]] = {group: [] for group in STATUS_GROUPS}
    cases: list[dict[str, Any]] = []
    for raw_result in _sequence(classification.get("cases"), "qualification results"):
        result = _mapping(raw_result, "qualification result")
        case_id = _string(result.get("case_id"), "qualification result case ID")
        policy_case = cases_by_id.get(case_id)
        if policy_case is None:
            raise ReleaseQualificationControllerError(
                f"Qualification result references unknown policy case {case_id!r}."
            )
        categories = _case_categories(result, policy_case, blocking_case_ids)
        for category in categories:
            groups[category].append(case_id)
        rendered_case = dict(result)
        rendered_case["categories"] = list(categories)
        rendered_case["operator_assisted"] = policy_case.get("execution_mode") == "operator_assisted"
        cases.append(rendered_case)

    frozen_groups = {group: groups[group] for group in STATUS_GROUPS}
    return {
        "schema_version": STATUS_SCHEMA_VERSION,
        "status_type": STATUS_TYPE,
        "release_tag": context.release_tag,
        "candidate_sha": context.candidate_sha,
        "release_stage": context.release_stage,
        "first_candidate_of_cycle": context.first_candidate_of_cycle,
        "workflow_phase": "milestone",
        "as_of": classification["as_of"],
        "policy_id": classification["policy_id"],
        "qualification_id": classification["qualification_id"],
        "evidence_binding": {
            "mode": binding.mode,
            "expected_evidence_ref": f"automation/release-evidence-{context.release_tag}",
            "evidence_path": context.evidence_path,
            "manifest_path": context.manifest_path,
            "manifest_sha256": context.manifest_sha256,
            "qualification_path": context.qualification_path,
            "release_receipt_path": context.release_receipt_path,
            "runner_sha": context.runner_sha,
        },
        "overall_status": _overall_status(frozen_groups),
        "passed": not frozen_groups["blocking"],
        "summary": {group: len(frozen_groups[group]) for group in STATUS_GROUPS},
        "groups": frozen_groups,
        "cases": cases,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspect and resume release qualification safely.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    status_parser = subparsers.add_parser("status", help="Inspect checked qualification state without mutation.")
    status_parser.add_argument("--release-tag", required=True)
    status_parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    status_parser.add_argument("--as-of", type=date.fromisoformat)
    resume_parser = subparsers.add_parser(
        "resume",
        help="Observe qualification recovery and dispatch one exact milestone run when explicitly authorized.",
    )
    resume_parser.add_argument("--release-tag", required=True)
    resume_parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    resume_parser.add_argument("--expected-main-sha")
    resume_parser.add_argument("--expected-manifest-sha256")
    resume_parser.add_argument("--retry-run-id", type=int)
    resume_parser.add_argument("--retry-checkpoint-sha256")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "resume":
        from scripts.github_release_run import ReleaseRunError
        from scripts.release_qualification_resume import (
            EXIT_FAILED,
            EXIT_SAFETY_ERROR,
            QualificationResumeError,
            QualificationResumeSafetyError,
            resume_qualification,
            safety_error_payload,
        )

        try:
            result = resume_qualification(
                args.repo_root,
                args.release_tag,
                expected_main_sha=args.expected_main_sha,
                expected_manifest_sha256=args.expected_manifest_sha256,
                retry_run_id=args.retry_run_id,
                retry_checkpoint_sha256=args.retry_checkpoint_sha256,
            )
        except QualificationResumeSafetyError as error:
            print(json.dumps(safety_error_payload(error), indent=2, sort_keys=True))
            return EXIT_SAFETY_ERROR
        except (json.JSONDecodeError, OSError, QualificationResumeError, ReleaseRunError) as error:
            print(json.dumps(safety_error_payload(error, state="error"), indent=2, sort_keys=True))
            return EXIT_FAILED
        print(json.dumps(result.payload, indent=2, sort_keys=True))
        return result.exit_code
    try:
        payload = build_status(
            args.repo_root,
            args.release_tag,
            as_of=args.as_of,
        )
    except (
        json.JSONDecodeError,
        OSError,
        QualificationScopeError,
        ReleaseMilestoneContextError,
        ReleaseQualificationControllerError,
        ReleaseQualificationManifestError,
    ) as error:
        print(f"Release qualification status failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 2 if cast(bool, payload["groups"]["blocking"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
