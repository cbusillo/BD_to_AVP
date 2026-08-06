from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import re
import subprocess
import sys

from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, cast

from scripts.release_receipt import (
    ArtifactReceiptExpectation,
    ReleaseReceiptError,
    load_validated_artifact_receipt,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY_PATH = REPO_ROOT / "docs" / "qualification" / "release-qualification-policy-v1.json"
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
RESULT_STATES = ("covered", "carry", "retest", "external")
WORKFLOW_PHASES = ("preparation", "artifact")


class QualificationScopeError(RuntimeError):
    pass


ChangedPaths = Callable[[str, str], set[str]]
ReferenceContent = Callable[[str], bytes]


def _mapping(value: object, description: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise QualificationScopeError(f"{description} must be a JSON object.")
    return cast(Mapping[str, Any], value)


def _sequence(value: object, description: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise QualificationScopeError(f"{description} must be a JSON array.")
    return value


def _strings(value: object, description: str) -> tuple[str, ...]:
    items = _sequence(value, description)
    if not all(isinstance(item, str) and item for item in items):
        raise QualificationScopeError(f"{description} must contain non-empty strings.")
    return tuple(cast(str, item) for item in items)


def _path_patterns(value: object, description: str) -> tuple[str, ...]:
    patterns = _strings(value, description)
    for pattern in patterns:
        path = Path(pattern)
        if path.is_absolute() or ".." in path.parts or pattern.startswith("./"):
            raise QualificationScopeError(f"{description} must be repository-relative patterns: {pattern!r}.")
    return patterns


def _load_json(path: Path, description: str) -> Mapping[str, Any]:
    try:
        return _mapping(json.loads(path.read_text(encoding="utf-8")), description)
    except OSError as error:
        raise QualificationScopeError(f"Unable to read {description} at {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise QualificationScopeError(f"Invalid JSON in {description} at {path}: {error}") from error


def load_policy(path: Path = DEFAULT_POLICY_PATH) -> Mapping[str, Any]:
    policy = _load_json(path, "release qualification policy")
    if policy.get("schema_version") != 1:
        raise QualificationScopeError("Release qualification policy schema_version must be 1.")
    if not isinstance(policy.get("policy_id"), str) or not policy["policy_id"]:
        raise QualificationScopeError("Release qualification policy requires policy_id.")
    if tuple(policy.get("result_states", ())) != RESULT_STATES:
        raise QualificationScopeError(f"Release qualification result_states must be {RESULT_STATES!r}.")

    tiers = _mapping(policy.get("tiers"), "release qualification tiers")
    if set(tiers) != {"0", "1", "2", "3", "4"}:
        raise QualificationScopeError("Release qualification policy must define tiers 0 through 4 exactly once.")
    contracts = _mapping(policy.get("contracts"), "release qualification contracts")
    for contract_id, patterns in contracts.items():
        if not isinstance(contract_id, str) or not contract_id:
            raise QualificationScopeError("Qualification contract IDs must be non-empty strings.")
        _path_patterns(patterns, f"qualification contract {contract_id!r}")

    cases = _sequence(policy.get("cases"), "release qualification cases")
    seen: set[str] = set()
    for index, raw_case in enumerate(cases):
        case = _mapping(raw_case, f"release qualification case {index}")
        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id:
            raise QualificationScopeError(f"Release qualification case {index} requires a stable id.")
        if case_id in seen:
            raise QualificationScopeError(f"Duplicate release qualification case ID: {case_id}")
        seen.add(case_id)
        tier = case.get("tier")
        if not isinstance(tier, int) or isinstance(tier, bool) or tier not in range(5):
            raise QualificationScopeError(
                f"Qualification case {case_id!r} must have exactly one tier from 0 through 4."
            )
        blocking_phase = case.get("blocking_phase")
        expected_blocking_phase = _mapping(tiers[str(tier)], f"qualification tier {tier}").get("blocking_phase")
        if blocking_phase != expected_blocking_phase:
            raise QualificationScopeError(
                f"Qualification case {case_id!r} blocking_phase must match tier {tier}: {expected_blocking_phase!r}."
            )
        invalidates_on = _mapping(case.get("invalidates_on"), f"qualification case {case_id!r} invalidates_on")
        direct_paths = _path_patterns(
            invalidates_on.get("paths"),
            f"qualification case {case_id!r} invalidating paths",
        )
        contract_ids = _strings(
            invalidates_on.get("contracts"),
            f"qualification case {case_id!r} invalidating contracts",
        )
        unknown_contracts = sorted(set(contract_ids) - set(contracts))
        if unknown_contracts:
            raise QualificationScopeError(
                f"Qualification case {case_id!r} references unknown contracts: {unknown_contracts!r}."
            )
        if tier in {2, 3} and not direct_paths and not contract_ids:
            raise QualificationScopeError(f"Qualification case {case_id!r} requires an invalidating path or contract.")
        evidence_sources = _strings(
            case.get("allowed_evidence_sources"),
            f"qualification case {case_id!r} allowed evidence sources",
        )
        if len(evidence_sources) != 1:
            raise QualificationScopeError(
                f"Qualification case {case_id!r} must have exactly one allowed evidence source."
            )
        carry = _mapping(case.get("carry_forward"), f"qualification case {case_id!r} carry_forward")
        for field in ("allowed", "requires_prior_accepted_receipt", "requires_clean_invalidation"):
            if not isinstance(carry.get(field), bool):
                raise QualificationScopeError(f"Qualification case {case_id!r} carry_forward.{field} must be boolean.")
        carry_allowed = cast(bool, carry["allowed"])
        if tier in {0, 1, 4} and carry_allowed:
            raise QualificationScopeError(f"Qualification case {case_id!r} cannot allow carry-forward at tier {tier}.")
        if tier in {2, 3} and (
            not carry_allowed
            or not carry["requires_prior_accepted_receipt"]
            or not carry["requires_clean_invalidation"]
        ):
            raise QualificationScopeError(
                f"Qualification case {case_id!r} must require accepted prior evidence and clean invalidation to carry."
            )
        if tier == 3:
            cadence = _mapping(case.get("cadence"), f"qualification case {case_id!r} cadence")
            max_age_days = cadence.get("max_age_days")
            if not isinstance(max_age_days, int) or isinstance(max_age_days, bool) or max_age_days <= 0:
                raise QualificationScopeError(f"Tier 3 qualification case {case_id!r} requires positive max_age_days.")
            if not isinstance(cadence.get("first_rc_or_stable_candidate"), bool):
                raise QualificationScopeError(
                    f"Tier 3 qualification case {case_id!r} requires boolean first_rc_or_stable_candidate."
                )
            if not _strings(cadence.get("hardware"), f"Tier 3 qualification case {case_id!r} hardware"):
                raise QualificationScopeError(f"Tier 3 qualification case {case_id!r} requires hardware metadata.")
    return policy


def load_evidence(path: Path | None) -> Mapping[str, Any]:
    if path is None:
        return {"schema_version": 1, "receipts": []}
    evidence = _load_json(path, "release qualification evidence")
    if evidence.get("schema_version") != 1:
        raise QualificationScopeError("Release qualification evidence schema_version must be 1.")
    _sequence(evidence.get("receipts"), "release qualification receipts")
    return evidence


def load_qualification_overrides(
    path: Path | None,
    policy: Mapping[str, Any],
) -> tuple[str | None, dict[str, bool]]:
    if path is None:
        return None, {}
    qualification = _load_json(path, "release qualification migration")
    policy_id = policy.get("policy_id")
    migration_policy = _mapping(qualification.get("qualification_policy"), "qualification_policy")
    if migration_policy.get("id") != policy_id:
        raise QualificationScopeError(
            f"Qualification migration expects policy {migration_policy.get('id')!r}, not {policy_id!r}."
        )
    policy_cases = {
        cast(str, case["id"]): case
        for case in (_mapping(raw_case, "release qualification case") for raw_case in policy["cases"])
    }
    overrides: dict[str, bool] = {}
    matrix_case_ids: set[str] = set()
    for raw_case in _sequence(qualification.get("matrix"), "qualification matrix"):
        case = _mapping(raw_case, "qualification matrix case")
        matrix_case_id = case.get("id")
        if not isinstance(matrix_case_id, str) or not matrix_case_id:
            raise QualificationScopeError("Every qualification matrix case requires id.")
        if matrix_case_id in matrix_case_ids:
            raise QualificationScopeError(f"Qualification matrix case {matrix_case_id!r} is duplicated.")
        matrix_case_ids.add(matrix_case_id)
        policy_case_id = case.get("policy_case_id")
        if not isinstance(policy_case_id, str) or not policy_case_id:
            raise QualificationScopeError("Every qualification matrix case requires policy_case_id.")
        if policy_case_id in overrides:
            raise QualificationScopeError(
                f"Qualification migration maps policy case {policy_case_id!r} more than once."
            )
        if policy_case_id not in policy_cases:
            raise QualificationScopeError(f"Qualification migration references unknown policy case {policy_case_id!r}.")
        migration = case.get("migration")
        tier = cast(int, policy_cases[policy_case_id]["tier"])
        allowed_migrations = {
            0: {"covered"},
            1: {"release_run_receipt"},
            2: {"fresh_retest", "scope_evaluated"},
            3: {"fresh_retest", "scope_evaluated"},
            4: {"external_nonblocking"},
        }[tier]
        if migration not in allowed_migrations:
            raise QualificationScopeError(
                f"Qualification migration {migration!r} is invalid for tier {tier} case {policy_case_id!r}."
            )
        overrides[policy_case_id] = migration == "fresh_retest"
    acceptance = _mapping(qualification.get("acceptance"), "qualification acceptance")
    required_case_ids = set(_strings(acceptance.get("required_case_ids"), "qualification required case IDs"))
    nonblocking_case_ids = set(_strings(acceptance.get("nonblocking_case_ids"), "qualification nonblocking case IDs"))
    preregistered_case_ids = set(
        _strings(
            acceptance.get("preregistered_matrix_case_ids"),
            "qualification preregistered matrix case IDs",
        )
    )
    expected_required = {case_id for case_id in overrides if cast(int, policy_cases[case_id]["tier"]) in {0, 1, 2, 3}}
    expected_nonblocking = {case_id for case_id in overrides if cast(int, policy_cases[case_id]["tier"]) == 4}
    if required_case_ids != expected_required:
        raise QualificationScopeError(
            "Qualification acceptance required_case_ids must use the mapped blocking policy case IDs."
        )
    if nonblocking_case_ids != expected_nonblocking:
        raise QualificationScopeError(
            "Qualification acceptance nonblocking_case_ids must use the mapped Tier 4 policy case IDs."
        )
    if preregistered_case_ids != matrix_case_ids:
        raise QualificationScopeError(
            "Qualification acceptance preregistered_matrix_case_ids must preserve every original matrix case ID."
        )
    qualification_id = qualification.get("qualification_id")
    if not isinstance(qualification_id, str) or not qualification_id:
        raise QualificationScopeError("Qualification migration requires qualification_id.")
    return qualification_id, overrides


def git_changed_paths(repo: Path) -> ChangedPaths:
    cache: dict[tuple[str, str], set[str]] = {}

    def changed(base_sha: str, candidate_sha: str) -> set[str]:
        key = (base_sha, candidate_sha)
        if key not in cache:
            result = subprocess.run(
                ["git", "diff", "--name-only", "-z", f"{base_sha}..{candidate_sha}"],
                cwd=repo,
                check=False,
                capture_output=True,
            )
            if result.returncode != 0:
                detail = result.stderr.decode("utf-8", errors="replace").strip() or "git diff failed"
                raise QualificationScopeError(
                    f"Unable to compare prior evidence SHA {base_sha} with candidate {candidate_sha}: {detail}"
                )
            cache[key] = {item.decode("utf-8", errors="surrogateescape") for item in result.stdout.split(b"\0") if item}
        return cache[key]

    return changed


def git_checked_reference_content(repo: Path) -> ReferenceContent:
    def read(reference: str) -> bytes:
        result = subprocess.run(
            ["git", "show", f"HEAD:{reference}"],
            cwd=repo,
            check=False,
            capture_output=True,
        )
        if result.returncode != 0:
            raise QualificationScopeError(
                f"Evidence reference {reference!r} must be committed in the current repository HEAD."
            )
        return result.stdout

    return read


def _parse_accepted_at(value: object, case_id: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise QualificationScopeError(f"Evidence for {case_id!r} requires accepted_at.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise QualificationScopeError(f"Evidence for {case_id!r} has invalid accepted_at {value!r}.") from error
    if parsed.tzinfo is None:
        raise QualificationScopeError(f"Evidence for {case_id!r} accepted_at must include a timezone.")
    return parsed.astimezone(timezone.utc)


def _validated_receipts(
    evidence: Mapping[str, Any],
    cases_by_id: Mapping[str, Mapping[str, Any]],
    reference_content: ReferenceContent,
    as_of: date,
) -> dict[str, list[Mapping[str, Any]]]:
    receipts: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    receipt_ids: set[str] = set()
    for index, raw_receipt in enumerate(_sequence(evidence.get("receipts"), "release qualification receipts")):
        receipt = _mapping(raw_receipt, f"release qualification receipt {index}")
        case_id = receipt.get("case_id")
        if not isinstance(case_id, str) or case_id not in cases_by_id:
            raise QualificationScopeError(f"Evidence receipt {index} references unknown case {case_id!r}.")
        if receipt.get("status") != "accepted":
            continue
        case = cases_by_id[case_id]
        evidence_source = _strings(
            case["allowed_evidence_sources"],
            f"case {case_id!r} evidence sources",
        )[0]
        source = receipt.get("source")
        if source != evidence_source:
            raise QualificationScopeError(
                f"Evidence for {case_id!r} must use source {evidence_source!r}, not {source!r}."
            )
        source_sha = receipt.get("source_sha")
        if not isinstance(source_sha, str) or SHA_PATTERN.fullmatch(source_sha) is None:
            raise QualificationScopeError(f"Evidence for {case_id!r} requires a full lowercase source_sha.")
        receipt_id = receipt.get("receipt_id")
        if not isinstance(receipt_id, str) or not receipt_id:
            raise QualificationScopeError(f"Evidence for {case_id!r} requires receipt_id.")
        if receipt_id in receipt_ids:
            raise QualificationScopeError(f"Duplicate release qualification receipt ID: {receipt_id}")
        receipt_ids.add(receipt_id)
        accepted_at = _parse_accepted_at(receipt.get("accepted_at"), case_id)
        if accepted_at.date() > as_of:
            raise QualificationScopeError(f"Evidence for {case_id!r} cannot be accepted after {as_of.isoformat()}.")
        tier = cast(int, case["tier"])
        if tier == 1:
            if receipt.get("workflow_conclusion") != "success":
                raise QualificationScopeError(f"Tier 1 case {case_id!r} requires a successful workflow conclusion.")
            run_id = receipt.get("release_run_id")
            if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id <= 0:
                raise QualificationScopeError(f"Tier 1 case {case_id!r} requires a positive release_run_id.")
        if tier == 3:
            cadence = _mapping(case["cadence"], f"case {case_id!r} cadence")
            required_hardware = set(_strings(cadence["hardware"], f"case {case_id!r} hardware"))
            receipt_hardware = set(_strings(receipt.get("hardware"), f"evidence for {case_id!r} hardware"))
            if not required_hardware.issubset(receipt_hardware):
                raise QualificationScopeError(
                    f"Evidence for {case_id!r} does not cover required hardware: "
                    f"{sorted(required_hardware - receipt_hardware)!r}."
                )
        reference = receipt.get("reference")
        digest = receipt.get("sha256")
        if (
            not isinstance(reference, str)
            or not reference
            or Path(reference).is_absolute()
            or ".." in Path(reference).parts
        ):
            raise QualificationScopeError(f"Evidence for {case_id!r} requires a repository-relative reference.")
        if not isinstance(digest, str) or SHA256_PATTERN.fullmatch(digest) is None:
            raise QualificationScopeError(f"Evidence for {case_id!r} requires a lowercase SHA-256 digest.")
        try:
            actual_digest = hashlib.sha256(reference_content(reference)).hexdigest()
        except OSError as error:
            raise QualificationScopeError(f"Unable to read evidence reference {reference!r}: {error}") from error
        if actual_digest != digest:
            raise QualificationScopeError(
                f"Evidence reference {reference!r} does not match its recorded SHA-256 digest."
            )
        receipts[case_id].append(receipt)
    for case_receipts in receipts.values():
        case_receipts.sort(
            key=lambda receipt: (
                _parse_accepted_at(receipt["accepted_at"], str(receipt["case_id"])),
                str(receipt["receipt_id"]),
            )
        )
    return receipts


def _case_patterns(case: Mapping[str, Any], contracts: Mapping[str, Any]) -> tuple[str, ...]:
    invalidates_on = _mapping(case["invalidates_on"], "case invalidates_on")
    patterns = list(_path_patterns(invalidates_on["paths"], "case invalidating paths"))
    for contract_id in _strings(invalidates_on["contracts"], "case invalidating contracts"):
        patterns.extend(_path_patterns(contracts[contract_id], f"contract {contract_id!r}"))
    return tuple(dict.fromkeys(patterns))


def _matching_paths(changed_paths: set[str], patterns: Sequence[str]) -> list[str]:
    return sorted(path for path in changed_paths if any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns))


def _receipt_summary(receipt: Mapping[str, Any] | None) -> dict[str, Any]:
    if receipt is None:
        return {"receipt_id": None, "reference": None, "source_sha": None}
    return {
        "receipt_id": receipt["receipt_id"],
        "reference": receipt["reference"],
        "source_sha": receipt["source_sha"],
    }


def _artifact_receipt_summary(receipt: Mapping[str, Any], path: Path) -> dict[str, Any]:
    return {
        "receipt_id": receipt["receipt_sha256"],
        "reference": path.name,
        "source_sha": receipt["source_sha"],
    }


def _evidence_requirement(
    case: Mapping[str, Any],
    *,
    status: str,
    applicable: bool,
    deferred: bool,
) -> dict[str, Any]:
    case_id = cast(str, case["id"])
    tier = cast(int, case["tier"])
    source = _strings(case["allowed_evidence_sources"], f"case {case_id!r} evidence sources")[0]
    if tier == 0:
        binding = "automatic_per_commit_gate"
    elif tier == 1:
        binding = "exact_same_run_release_receipt"
    elif tier in {2, 3}:
        binding = "exact_candidate_or_valid_carry_forward"
    else:
        binding = "post_publication_observation"
    required = status == "retest"
    action: str | None = None
    if required:
        timing = "before the artifact phase" if deferred else "before this phase can pass"
        action = f"Provide accepted {source} evidence {timing}."
    return {
        "source": source,
        "binding": binding,
        "required": required,
        "required_for_phase": required and applicable,
        "satisfied": not required,
        "action": action,
    }


def classify_release_scope(
    policy: Mapping[str, Any],
    evidence: Mapping[str, Any],
    *,
    candidate_sha: str,
    changed_paths: ChangedPaths,
    repo: Path = REPO_ROOT,
    reference_content: ReferenceContent | None = None,
    qualification_id: str | None = None,
    fresh_retest: Mapping[str, bool] | None = None,
    release_stage: str = "rc",
    first_candidate_of_cycle: bool = False,
    workflow_phase: str = "preparation",
    artifact_receipt_path: Path | None = None,
    artifact_receipt_expectation: ArtifactReceiptExpectation | None = None,
    as_of: date | None = None,
) -> dict[str, Any]:
    if SHA_PATTERN.fullmatch(candidate_sha) is None:
        raise QualificationScopeError("Candidate SHA must be a full lowercase Git SHA.")
    if release_stage not in {"alpha", "beta", "rc", "stable"}:
        raise QualificationScopeError("Release stage must be alpha, beta, rc, or stable.")
    if workflow_phase not in WORKFLOW_PHASES:
        raise QualificationScopeError(f"Workflow phase must be one of {WORKFLOW_PHASES!r}.")
    as_of = as_of or datetime.now(timezone.utc).date()
    contracts = _mapping(policy["contracts"], "release qualification contracts")
    cases = [_mapping(case, "release qualification case") for case in _sequence(policy["cases"], "cases")]
    case_ids = {cast(str, case["id"]) for case in cases}
    cases_by_id = {cast(str, case["id"]): case for case in cases}
    receipt_map = _validated_receipts(
        evidence,
        cases_by_id,
        reference_content or git_checked_reference_content(repo),
        as_of,
    )
    artifact_tier1_cases: set[str] = set()
    artifact_receipt_evidence: dict[str, Any] | None = None
    if (artifact_receipt_path is None) != (artifact_receipt_expectation is None):
        raise QualificationScopeError(
            "Artifact release receipt and exact run, release, asset, and digest inputs must be supplied together."
        )
    if workflow_phase == "preparation" and artifact_receipt_path is not None:
        raise QualificationScopeError("Preparation workflow phase cannot consume artifact release evidence.")
    if workflow_phase == "artifact":
        if artifact_receipt_path is None or artifact_receipt_expectation is None:
            raise QualificationScopeError(
                "Artifact workflow phase requires the exact same-run release receipt and all binding inputs."
            )
        try:
            artifact_receipt = load_validated_artifact_receipt(
                artifact_receipt_path,
                artifact_receipt_expectation,
            )
        except ReleaseReceiptError as error:
            raise QualificationScopeError(f"Artifact release receipt validation failed: {error}") from error
        expected_tier1_cases = {case_id for case_id, case in cases_by_id.items() if case["tier"] == 1}
        artifact_tier1_cases = set(
            _strings(artifact_receipt.get("tier1_case_references"), "artifact receipt Tier 1 case references")
        )
        if artifact_tier1_cases != expected_tier1_cases:
            raise QualificationScopeError(
                "Artifact release receipt Tier 1 case references do not match the current qualification policy."
            )
        artifact_receipt_evidence = _artifact_receipt_summary(artifact_receipt, artifact_receipt_path)
    fresh_retest = fresh_retest or {}
    unknown_overrides = sorted(set(fresh_retest) - case_ids)
    if unknown_overrides:
        raise QualificationScopeError(
            f"Qualification migration references unknown policy cases: {unknown_overrides!r}."
        )

    results: list[dict[str, Any]] = []
    for case in cases:
        case_id = cast(str, case["id"])
        tier = cast(int, case["tier"])
        accepted = receipt_map.get(case_id, [])
        exact = next((receipt for receipt in reversed(accepted) if receipt["source_sha"] == candidate_sha), None)
        prior = next((receipt for receipt in reversed(accepted) if receipt["source_sha"] != candidate_sha), None)
        status: str
        reason: str
        invalidating_paths: list[str] = []
        selected_receipt: Mapping[str, Any] | None = exact or prior

        if tier == 0:
            status, reason = "covered", "Owned by the automatic per-commit quality gate."
        elif tier == 4:
            status, reason = "external", "Post-publication field evidence cannot block publication."
        elif tier == 1 and case_id in artifact_tier1_cases:
            status, reason = "covered", "Validated evidence is bound to the exact artifact workflow run and release."
            selected_receipt = None
        elif exact is not None:
            status, reason = "covered", "Accepted evidence is bound to the exact candidate SHA."
        elif tier == 1:
            status, reason = "retest", "The guarded release engine has no accepted receipt for the exact candidate SHA."
            selected_receipt = None
        elif fresh_retest.get(case_id, False):
            status, reason = "retest", "The candidate migration explicitly requires fresh targeted evidence."
            selected_receipt = None
        elif prior is None:
            status, reason = "retest", "No accepted prior evidence receipt is available for carry-forward."
        else:
            carry = _mapping(case["carry_forward"], f"case {case_id!r} carry_forward")
            if not carry["allowed"]:
                status, reason = "retest", "The policy does not allow this evidence to carry forward."
                selected_receipt = None
            else:
                patterns = _case_patterns(case, contracts)
                invalidating_paths = _matching_paths(
                    changed_paths(cast(str, prior["source_sha"]), candidate_sha),
                    patterns,
                )
                if invalidating_paths:
                    status, reason = "retest", "Candidate changes invalidate the accepted prior evidence."
                elif tier == 3:
                    cadence = _mapping(case["cadence"], f"case {case_id!r} cadence")
                    accepted_date = _parse_accepted_at(prior["accepted_at"], case_id).date()
                    age_days = (as_of - accepted_date).days
                    due_for_milestone = bool(cadence.get("first_rc_or_stable_candidate")) and (
                        release_stage == "stable" or (release_stage == "rc" and first_candidate_of_cycle)
                    )
                    if due_for_milestone:
                        status, reason = "retest", "Tier 3 evidence is due for this release milestone."
                    elif age_days > cast(int, cadence["max_age_days"]):
                        status, reason = "retest", "Tier 3 evidence exceeded its maximum age."
                    else:
                        status, reason = (
                            "carry",
                            "Tier 3 evidence remains within cadence and no invalidating paths changed.",
                        )
                else:
                    status, reason = "carry", "Accepted prior evidence remains valid because no mapped paths changed."

        blocking_phase = cast(str, case["blocking_phase"])
        deferred = workflow_phase == "preparation" and blocking_phase == "publication"
        applicable = blocking_phase != "none" and not deferred
        evidence_summary = (
            artifact_receipt_evidence
            if tier == 1 and case_id in artifact_tier1_cases and artifact_receipt_evidence is not None
            else _receipt_summary(selected_receipt)
        )
        result = {
            "case_id": case_id,
            "tier": tier,
            "status": status,
            "blocking_phase": blocking_phase,
            "blocking": blocking_phase != "none",
            "applicable": applicable,
            "deferred": deferred,
            "reason": reason,
            "invalidating_paths": invalidating_paths,
            "evidence": evidence_summary,
            "evidence_required": status == "retest",
            "evidence_requirement": _evidence_requirement(
                case,
                status=status,
                applicable=applicable,
                deferred=deferred,
            ),
        }
        results.append(result)

    counts = Counter(result["status"] for result in results)
    blocking_retests = [
        cast(str, result["case_id"])
        for result in results
        if result["status"] == "retest" and result["blocking"] and result["applicable"]
    ]
    deferred_retests = [
        cast(str, result["case_id"]) for result in results if result["status"] == "retest" and result["deferred"]
    ]
    return {
        "schema_version": 1,
        "policy_id": policy["policy_id"],
        "qualification_id": qualification_id,
        "candidate_sha": candidate_sha,
        "release_stage": release_stage,
        "first_candidate_of_cycle": first_candidate_of_cycle,
        "workflow_phase": workflow_phase,
        "as_of": as_of.isoformat(),
        "cases": results,
        "summary": {state: counts[state] for state in RESULT_STATES},
        "blocking_retests": blocking_retests,
        "deferred_retests": deferred_retests,
        "passed": not blocking_retests,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Classify release qualification scope from policy and checked evidence."
    )
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY_PATH)
    parser.add_argument("--validate-policy", action="store_true")
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--qualification", type=Path)
    parser.add_argument("--candidate-sha")
    parser.add_argument("--release-stage", choices=("alpha", "beta", "rc", "stable"), default="rc")
    parser.add_argument("--first-candidate-of-cycle", action="store_true")
    parser.add_argument("--workflow-phase", choices=WORKFLOW_PHASES, default="preparation")
    parser.add_argument("--release-receipt", type=Path)
    parser.add_argument("--release-route", choices=("stable", "prerelease"))
    parser.add_argument("--workflow-run-id", type=int)
    parser.add_argument("--workflow-run-attempt", type=int)
    parser.add_argument("--release-id", type=int)
    parser.add_argument("--package-version")
    parser.add_argument("--public-version")
    parser.add_argument("--build-version")
    parser.add_argument("--release-tag")
    parser.add_argument("--dmg-name")
    parser.add_argument("--release-receipt-sha256")
    parser.add_argument("--signed-app-tree-sha256")
    parser.add_argument("--dmg-asset-id", type=int)
    parser.add_argument("--dmg-sha256")
    parser.add_argument("--checksum-asset-id", type=int)
    parser.add_argument("--checksum-sha256")
    parser.add_argument("--appcast-asset-id", type=int)
    parser.add_argument("--appcast-sha256")
    parser.add_argument("--as-of", type=date.fromisoformat)
    parser.add_argument("--repo", type=Path, default=REPO_ROOT)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--require-evidence", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        policy = load_policy(args.policy)
        if args.validate_policy:
            payload = {
                "policy_id": policy["policy_id"],
                "case_count": len(cast(Sequence[Any], policy["cases"])),
                "valid": True,
            }
        else:
            if args.candidate_sha is None:
                raise QualificationScopeError("--candidate-sha is required unless --validate-policy is used.")
            evidence = load_evidence(args.evidence)
            qualification_id, fresh_retest = load_qualification_overrides(args.qualification, policy)
            artifact_receipt_expectation: ArtifactReceiptExpectation | None = None
            receipt_inputs = {
                "--release-receipt": args.release_receipt,
                "--release-route": args.release_route,
                "--workflow-run-id": args.workflow_run_id,
                "--workflow-run-attempt": args.workflow_run_attempt,
                "--release-id": args.release_id,
                "--package-version": args.package_version,
                "--public-version": args.public_version,
                "--build-version": args.build_version,
                "--release-tag": args.release_tag,
                "--dmg-name": args.dmg_name,
                "--release-receipt-sha256": args.release_receipt_sha256,
                "--signed-app-tree-sha256": args.signed_app_tree_sha256,
                "--dmg-asset-id": args.dmg_asset_id,
                "--dmg-sha256": args.dmg_sha256,
                "--checksum-asset-id": args.checksum_asset_id,
                "--checksum-sha256": args.checksum_sha256,
                "--appcast-asset-id": args.appcast_asset_id,
                "--appcast-sha256": args.appcast_sha256,
            }
            supplied_receipt_inputs = [name for name, value in receipt_inputs.items() if value is not None]
            if supplied_receipt_inputs:
                if args.workflow_phase != "artifact":
                    raise QualificationScopeError("Artifact receipt inputs require --workflow-phase artifact.")
                missing_inputs = [name for name, value in receipt_inputs.items() if value is None]
                if missing_inputs:
                    raise QualificationScopeError(
                        "Artifact workflow phase requires exact receipt inputs: " + ", ".join(missing_inputs) + "."
                    )
                artifact_receipt_expectation = ArtifactReceiptExpectation(
                    candidate_sha=args.candidate_sha,
                    release_route=args.release_route,
                    workflow_run_id=args.workflow_run_id,
                    workflow_run_attempt=args.workflow_run_attempt,
                    release_id=args.release_id,
                    package_version=args.package_version,
                    public_version=args.public_version,
                    build_version=args.build_version,
                    release_tag=args.release_tag,
                    dmg_name=args.dmg_name,
                    receipt_file_sha256=args.release_receipt_sha256,
                    signed_app_tree_sha256=args.signed_app_tree_sha256,
                    dmg_asset_id=args.dmg_asset_id,
                    dmg_sha256=args.dmg_sha256,
                    checksum_asset_id=args.checksum_asset_id,
                    checksum_sha256=args.checksum_sha256,
                    appcast_asset_id=args.appcast_asset_id,
                    appcast_sha256=args.appcast_sha256,
                )
            payload = classify_release_scope(
                policy,
                evidence,
                candidate_sha=args.candidate_sha,
                changed_paths=git_changed_paths(args.repo),
                repo=args.repo,
                qualification_id=qualification_id,
                fresh_retest=fresh_retest,
                release_stage=args.release_stage,
                first_candidate_of_cycle=args.first_candidate_of_cycle,
                workflow_phase=args.workflow_phase,
                artifact_receipt_path=args.release_receipt,
                artifact_receipt_expectation=artifact_receipt_expectation,
                as_of=args.as_of,
            )
        rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        if args.output is not None:
            args.output.write_text(rendered, encoding="utf-8")
        print(rendered, end="")
        if args.require_evidence and not cast(bool, payload.get("passed", True)):
            print(
                f"Release qualification evidence is required for workflow phase {payload['workflow_phase']}:",
                file=sys.stderr,
            )
            cases = {cast(str, case["case_id"]): case for case in cast(Sequence[Mapping[str, Any]], payload["cases"])}
            for case_id in cast(Sequence[str], payload["blocking_retests"]):
                case = cases[case_id]
                requirement = _mapping(case["evidence_requirement"], "evidence requirement")
                print(
                    f"- {case_id}: {requirement['action']} Reason: {case['reason']}",
                    file=sys.stderr,
                )
            return 2
        return 0
    except QualificationScopeError as error:
        if args.output is not None:
            failure_payload = {
                "schema_version": 1,
                "candidate_sha": args.candidate_sha,
                "workflow_phase": args.workflow_phase,
                "passed": False,
                "error": str(error),
                "blocking_retests": [],
                "deferred_retests": [],
                "cases": [],
            }
            args.output.write_text(json.dumps(failure_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"Release qualification scope failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
