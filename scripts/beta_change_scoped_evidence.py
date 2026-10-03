"""Produce Beta change-scoped Tier 2 evidence for a blocked preparation classification.

Run from a clean ``qualify/<beta-tag>`` checkout whose HEAD is the candidate SHA.
The command reruns the preparation classifier, the native and focused Python
tests, and the local ad hoc package, checks every catalogued proof against the
actual results, then writes
``docs/qualification/<beta-tag>-change-scoped-evidence-v1.json`` and appends one
receipt per proved case to the evidence index. It never commits or pushes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import subprocess
import sys
import tempfile
import threading

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

from scripts.qualify_release_scope import load_policy
from scripts.release import (
    ReleaseError,
    ReleaseMetadata,
    load_release_metadata,
    parse_release_tag,
    validate_configured_qualification_record,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
QUALIFICATION_DIRECTORY = "docs/qualification"
EVIDENCE_INDEX_PATH = f"{QUALIFICATION_DIRECTORY}/release-evidence-v1.json"
POLICY_PATH = f"{QUALIFICATION_DIRECTORY}/release-qualification-policy-v1.json"
EVIDENCE_DOCUMENT_PATTERN = re.compile(r"^(v[^/]+)-change-scoped-evidence-v1\.json$")
EVIDENCE_SOURCE = "signed_artifact_receipt"
SUPPORT_DIAGNOSTICS_ENDPOINT_ENV = "BD_TO_AVP_SUPPORT_DIAGNOSTICS_ENDPOINT"
DEFAULT_SUPPORT_DIAGNOSTICS_ENDPOINT = "https://diagnostics.shinycomputers.com"
NATIVE_TEST_COMMAND = ("uv", "run", "python", "scripts/native_app.py", "test")
PACKAGE_COMMAND = ("uv", "run", "python", "scripts/native_app.py", "package")
ADHOC_SIGNATURE_SUMMARY = "codesign -dv reports Signature=adhoc and no TeamIdentifier"
EVIDENCE_MEANING = (
    "Change-scoped evidence from the exact locally packaged, ad-hoc code-signed app tree; "
    "production Developer ID signing remains owned by the guarded release run."
)

# Packaged smokes run inside ``scripts/native_app.py package``; the package command
# raises when any of them fails, so a successful package run proves each one.
PACKAGED_SMOKE_PROOFS = frozenset(
    {
        "scripts/native_app.py:packaged worker cancellation smoke",
        "scripts/native_app.py:smoke_packaged_worker",
    }
)

_CAPACITY_FIXTURE_SCOPE = {
    "qualification_kind": "exact_candidate_change_scoped_regression",
    "capacity_readings": "controlled_test_fixtures",
    "physical_low_disk_condition_forced": False,
    "prior_accepted_evidence": "docs/qualification/prior-signed-functional-evidence-v1.json",
}
_SUBTITLE_PARTIAL_OUTPUT_PROOF = [
    "tests/test_subtitles.py:test_completed_event_emitted_after_successful_rip",
    "tests/test_subtitles.py:test_partial_warning_emitted_when_empty_srts_produced",
    "tests/test_subtitles.py:test_partial_warning_emitted_when_ripper_skips_a_selected_track",
    "tests/test_subtitles.py:test_empty_output_emits_failure_before_strict_error",
    "tests/test_subtitles.py:test_postprocess_error_closes_started_event_with_failure",
    "tests/test_subtitles.py:test_keyboard_interrupt_closes_started_event_as_cancelled",
    "tests/test_subtitles.py:test_no_partial_warning_when_all_tracks_succeed",
]

# Proofs and observations for each release-candidate Tier 2 case this lane may
# prove. Seeded from the latest accepted change-scoped evidence covering each
# case; a proof identifier names a test or packaged smoke that must pass on the
# exact candidate before its observations may be recorded.
PROOF_CATALOG: Mapping[str, Mapping[str, Any]] = {
    "gui-preview-low-local-ample-destination": {
        "observations": {
            "destination_workspace_selected_for_disc_image_preview": True,
            "destination_workspace_selected_for_bluray_folder_preview": True,
            "sufficient_destination_capacity_allows_preview": True,
        },
        "proof": [
            "macos/BluRayToVisionProTests/PreviewViewModelTests.swift:testDiscImagePreviewUsesSelectedDestinationWorkspace",
            "macos/BluRayToVisionProTests/PreviewViewModelTests.swift:testBluRayFolderPreviewUsesSelectedDestinationAndTitle",
        ],
        "scope": _CAPACITY_FIXTURE_SCOPE,
    },
    "gui-preview-cancel-cleanup": {
        "observations": {
            "cancelled_preview_removes_partial_workspace": True,
            "owned_worker_and_descendant_processes_reaped": True,
            "packaged_worker_cancellation_smoke": "passed",
        },
        "proof": [
            "macos/BluRayToVisionProTests/PreviewViewModelTests.swift:testCancelledPreviewRemovesPartialWorkspace",
            "macos/BluRayToVisionProTests/WorkerProcessClientTests.swift:testCancellationAllowsWorkerToReapSeparateSessionChild",
            "scripts/native_app.py:packaged worker cancellation smoke",
        ],
    },
    "gui-preview-failure-cleanup": {
        "observations": {
            "destination_workspace_failure_is_actionable": True,
            "empty_owned_destination_root_removed": True,
            "transient_cleanup_failure_retried": True,
        },
        "proof": [
            "macos/BluRayToVisionProTests/PreviewViewModelTests.swift:testDestinationWorkspacePreparationFailureIsActionable",
            "macos/BluRayToVisionProTests/PreviewViewModelTests.swift:testDestinationWorkspaceCleanupRemovesEmptyHiddenRoot",
            "macos/BluRayToVisionProTests/PreviewViewModelTests.swift:testCleanupRetriesTransientRemovalFailure",
        ],
    },
    "capacity-known-low": {
        "observations": {
            "known_low_capacity_blocks_before_worker_launch": True,
            "partial_workspace_is_not_created": True,
            "support_diagnostic_evidence_is_available": True,
        },
        "proof": [
            "macos/BluRayToVisionProTests/PreviewViewModelTests.swift:testKnownLowDestinationCapacityBlocksBeforeWorkerLaunch",
            "macos/BluRayToVisionProTests/PreviewViewModelTests.swift:testCapacityAssessmentBlocksOnlyTrustworthyLowReading",
        ],
        "scope": _CAPACITY_FIXTURE_SCOPE,
    },
    "capacity-unknown-and-conflicting": {
        "observations": {
            "conflicting_capacity_warns_without_blocking": True,
            "unknown_capacity_warns_without_blocking": True,
            "worker_launch_remains_available": True,
        },
        "proof": [
            "macos/BluRayToVisionProTests/PreviewViewModelTests.swift:testUnknownAndConflictingDestinationCapacityWarnWithoutBlocking",
            "macos/BluRayToVisionProTests/PreviewViewModelTests.swift:testCapacityAssessmentBlocksOnlyTrustworthyLowReading",
        ],
        "scope": _CAPACITY_FIXTURE_SCOPE,
    },
    "network-generated-final-output": {
        "observations": {
            "destination_snapshots_preserved_in_batch_preparation": True,
            "generated_route_job_spec_matches_shared_worker_fixture": True,
            "final_output_move_returns_destination_path": True,
            "packaged_worker_protocol_smoke": "passed",
        },
        "proof": [
            "macos/BluRayToVisionProTests/ConversionWorkflowTests.swift:testBatchPreparationSnapshotsProfileDestinationAndOptionsPerItem",
            "macos/BluRayToVisionProTests/ConversionWorkflowTests.swift:testGeneratedRouteJobSpecMatchesSharedWorkerFixture",
            "tests/test_process_preflight.py:test_output_move_returns_final_output_path",
            "scripts/native_app.py:smoke_packaged_worker",
        ],
        "scope": {
            "mounted_network_conversion_rerun": False,
            "prior_real_network_evidence": "docs/qualification/rc3-targeted-qualification-v1.json",
            "qualification_kind": "exact_candidate_change_scoped_regression",
        },
    },
    "overwrite-and-conversion-cancel": {
        "observations": {
            "existing_output_rejected_before_workspace_mutation": True,
            "resumed_and_unowned_workspaces_preserved": True,
            "view_model_stop_transitions_to_stopping": True,
            "owned_workspaces_removed_after_cancellation": True,
        },
        "proof": [
            "tests/test_process_preflight.py:test_existing_output_aborts_before_workspace_mutation",
            "tests/test_process_preflight.py:test_cancelled_conversion_cleans_owned_workspaces",
            "tests/test_process_preflight.py:test_cancelled_conversion_preserves_unowned_workspaces",
            "macos/BluRayToVisionProTests/ConversionViewModelTests.swift:testStopActiveWorkerCancelsConversionAndTransitionsToStopping",
            "tests/test_process_preflight.py:test_resumed_cancellation_preserves_existing_workspaces",
        ],
    },
    "malformed-pgs-parser-recovery": {
        "observations": {
            "declared_pixel_bounds_enforced": True,
            "malformed_display_sets_isolated": True,
            "truncated_and_unknown_segments_bounded": True,
            "usable_peer_subtitles_preserved": True,
        },
        "proof": [
            "tests/test_vendor_pgsrip_edge_cases.py:test_rle_exceeding_declared_bounds_raises_value_error",
            "tests/test_vendor_pgsrip_edge_cases.py:test_decode_rle_image_max_pixels_parameter_raises_on_overflow",
            "tests/test_vendor_pgsrip_edge_cases.py:test_generate_image_returns_none_for_malformed_rle",
            "tests/test_vendor_pgsrip_edge_cases.py:test_create_items_skips_malformed_item_and_keeps_good_ones",
            "tests/test_vendor_pgsrip_edge_cases.py:test_skips_unknown_segment_and_continues",
            "tests/test_vendor_pgsrip_edge_cases.py:test_truncated_segment_is_ignored",
        ],
    },
    "subtitle-partial-output-diagnostics": {
        "observations": {
            "aggregate_track_counts_recorded": True,
            "cancelled_outcome_distinguished": True,
            "failed_outcome_distinguished": True,
            "partial_outcome_distinguished": True,
            "successful_outcome_distinguished": True,
        },
        "proof": _SUBTITLE_PARTIAL_OUTPUT_PROOF,
    },
}

SWIFT_PROOF_PATTERN = re.compile(r"^(?P<path>macos/(?P<module>\w+)/(?:[\w.-]+/)*\w+\.swift):(?P<method>test\w+)$")
SWIFT_TYPE_PATTERN = re.compile(
    r"^[ \t]*(?:(?:final|public|internal|private|fileprivate|@\w+)\s+)*(?:class|extension)\s+(\w+)", re.M
)
PYTHON_PROOF_PATTERN = re.compile(r"^(?P<path>tests/(?:\w+/)*test_\w+)\.py:(?P<method>test_\w+)$")
XCTEST_RESULT_PATTERN = re.compile(
    r"Test Case '-\[(?P<cls>[\w.]+) (?P<method>\w+)\]' (?P<status>passed|failed|skipped)"
)
UNITTEST_HEADER_PATTERN = re.compile(r"^(?P<method>\w+) \((?P<test_id>[\w.]+)\)(?P<rest>.*)$")
UNITTEST_SEPARATOR_PATTERN = re.compile(r"(?:=|-){70}")
UNITTEST_RAN_PATTERN = re.compile(r"^Ran (?P<count>\d+) tests? in ", re.MULTILINE)
UNITTEST_OK_PATTERN = re.compile(r"^OK(?: \((?P<details>[^)]*)\))?$", re.MULTILINE)


class BetaEvidenceError(RuntimeError):
    pass


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str


CommandRunner = Callable[[Sequence[str], Mapping[str, str] | None], CommandResult]


@dataclass(frozen=True)
class Runners:
    classify: CommandRunner
    native_tests: CommandRunner
    python_tests: CommandRunner
    package: CommandRunner
    app_facts: Callable[[Path], Mapping[str, str]]
    toolchain: Callable[[], str]


@dataclass(frozen=True)
class RecordedRun:
    command: str
    result: CommandResult
    log_sha256: str


@dataclass(frozen=True)
class EvidenceResult:
    document_path: str
    receipt_ids: tuple[str, ...]


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _git(repo_root: Path, *arguments: str) -> str:
    completed = subprocess.run(["git", *arguments], cwd=repo_root, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise BetaEvidenceError(f"git {' '.join(arguments)} failed: {completed.stderr.strip()}")
    return completed.stdout


def _require_candidate_checkout(repo_root: Path, release_tag: str) -> str:
    if _git(repo_root, "status", "--porcelain", "--untracked-files=all").strip():
        raise BetaEvidenceError("Change-scoped evidence requires a clean worktree.")
    expected_branch = f"qualify/{release_tag}"
    branch = _git(repo_root, "rev-parse", "--abbrev-ref", "HEAD").strip()
    if branch != expected_branch:
        raise BetaEvidenceError(f"Change-scoped evidence must run on branch {expected_branch!r}; found {branch!r}.")
    return _git(repo_root, "rev-parse", "HEAD").strip()


def eligible_case_ids(policy: Mapping[str, Any]) -> set[str]:
    """Return policy cases the Beta change-scoped lane may prove (mirrors the PR validator)."""
    return {
        str(case["id"])
        for case in policy["cases"]
        if case.get("tier") == 2
        and case.get("blocking_phase") == "release_candidate"
        and case.get("artifact_owned") is not True
        and case.get("requires_live_publication") is not True
        and EVIDENCE_SOURCE in case.get("allowed_evidence_sources", [])
    }


def swift_proof_key(proof: str, source_root: Path) -> str | None:
    """Resolve ``path.swift:testName`` to the ``Module.Class testName`` xcodebuild reports.

    The class is the type declaration (class or extension) enclosing the test method
    in that source file, because one test file may declare several test classes.
    """
    match = SWIFT_PROOF_PATTERN.fullmatch(proof)
    source_path = source_root / match["path"] if match is not None else None
    if match is None or source_path is None or not source_path.is_file():
        return None
    text = source_path.read_text(encoding="utf-8")
    method = re.search(rf"\bfunc {match['method']}\(", text)
    owners = SWIFT_TYPE_PATTERN.findall(text[: method.start()]) if method is not None else []
    if not owners:
        return None
    return f"{match['module']}.{owners[-1]} {match['method']}"


def python_proof_target(proof: str) -> tuple[str, str] | None:
    match = PYTHON_PROOF_PATTERN.fullmatch(proof)
    if match is None:
        return None
    return match["path"].replace("/", "."), match["method"]


def _require_known_proof_kind(proof: str) -> None:
    if proof in PACKAGED_SMOKE_PROOFS or SWIFT_PROOF_PATTERN.fullmatch(proof) or python_proof_target(proof):
        return
    raise BetaEvidenceError(f"Unrecognized proof identifier: {proof!r}.")


def parse_xctest_results(output: str) -> dict[str, set[str]]:
    """Map ``Module.Class method`` to every status xcodebuild reported for it."""
    results: dict[str, set[str]] = {}
    for match in XCTEST_RESULT_PATTERN.finditer(output):
        results.setdefault(f"{match['cls']} {match['method']}", set()).add(match["status"])
    return results


def _unittest_status(status_text: str) -> str:
    if status_text == "ok":
        return "ok"
    if status_text in {"FAIL", "ERROR", "unexpected success"}:
        return "failed"
    if status_text.startswith("skipped") or status_text == "expected failure":
        return "skipped"
    return "unknown"


def parse_unittest_results(output: str) -> dict[str, str]:
    """Map verbose unittest IDs to ``ok``, ``failed``, ``skipped``, or ``unknown``.

    unittest writes each status as the last text before the next test header, so
    output a test logs in between is skipped. A status that cannot be read
    unambiguously is ``unknown``, which never proves anything.
    """
    results: dict[str, str] = {}
    current: str | None = None
    chunk: list[str] = []

    def finish() -> None:
        if current is None:
            return
        text = "\n".join(chunk)
        status = "unknown"
        if " ... " in text:
            tail = text.split(" ... ", 1)[1].strip().splitlines()
            status = _unittest_status(tail[-1].strip()) if tail else "unknown"
        previous = results.get(current)
        results[current] = status if previous in (None, status) else "unknown"

    for line in output.splitlines():
        header = UNITTEST_HEADER_PATTERN.match(line)
        if header is not None and header["test_id"].endswith(f".{header['method']}"):
            finish()
            current, chunk = header["test_id"], [header["rest"]]
        elif UNITTEST_SEPARATOR_PATTERN.fullmatch(line):
            finish()
            current, chunk = None, []
        elif current is not None:
            chunk.append(line)
    finish()
    return results


def _missing_swift_proofs(proofs: Sequence[str], results: Mapping[str, set[str]], source_root: Path) -> list[str]:
    missing = []
    for proof in proofs:
        if SWIFT_PROOF_PATTERN.fullmatch(proof) is None:
            continue
        key = swift_proof_key(proof, source_root)
        if key is None or results.get(key) != {"passed"}:
            missing.append(proof)
    return missing


def _missing_python_proofs(proofs: Sequence[str], results: Mapping[str, str]) -> list[str]:
    missing = []
    for proof in proofs:
        target = python_proof_target(proof)
        if target is None:
            continue
        module, method = target
        statuses = [
            status
            for test_id, status in results.items()
            if test_id.startswith(f"{module}.")
            and test_id.endswith(f".{method}")
            and test_id.count(".") == module.count(".") + 2
        ]
        if not statuses or any(status != "ok" for status in statuses):
            missing.append(proof)
    return missing


def _unittest_counts(result: CommandResult) -> dict[str, int]:
    ran = UNITTEST_RAN_PATTERN.search(result.stderr)
    ok_lines = list(UNITTEST_OK_PATTERN.finditer(result.stderr))
    if result.exit_code != 0 or ran is None or not ok_lines:
        raise BetaEvidenceError("Focused Python tests did not pass.")
    details = ok_lines[-1]["details"] or ""
    counts = {key: int(value) for key, value in re.findall(r"([a-z ]+)=(\d+)", details)}
    skipped = counts.get("skipped", 0) + counts.get("expected failures", 0)
    return {"passed": int(ran["count"]) - skipped, "failed": 0, "skipped": skipped}


def _xctest_counts(result: CommandResult, results: Mapping[str, set[str]]) -> dict[str, int]:
    failed = sum(1 for statuses in results.values() if "failed" in statuses)
    passed = sum(1 for statuses in results.values() if statuses == {"passed"})
    skipped = sum(1 for statuses in results.values() if statuses == {"skipped"})
    if result.exit_code != 0 or failed or not passed:
        raise BetaEvidenceError("Native tests did not pass.")
    return {"passed": passed, "failed": failed, "skipped": skipped}


def _record_run(
    runner: CommandRunner,
    command: Sequence[str],
    *,
    display_command: str,
    log_path: Path,
    environment: Mapping[str, str] | None = None,
) -> RecordedRun:
    print(f"Running: {display_command}", flush=True)
    result = runner(command, environment)
    log_text = result.stdout + result.stderr
    log_path.write_text(log_text, encoding="utf-8")
    return RecordedRun(command=display_command, result=result, log_sha256=_sha256_text(log_text))


def _latest_precedent(repo_root: Path, release_tag: str) -> str | None:
    candidates = []
    for path in (repo_root / QUALIFICATION_DIRECTORY).glob("v*-change-scoped-evidence-v1.json"):
        match = EVIDENCE_DOCUMENT_PATTERN.fullmatch(path.name)
        if match is None or match[1] == release_tag:
            continue
        try:
            version = parse_release_tag(match[1], allow_legacy_rc=False)
        except ReleaseError:
            continue
        candidates.append((version.order_key, f"{QUALIFICATION_DIRECTORY}/{path.name}"))
    return max(candidates)[1] if candidates else None


def _classifier_command(qualification_relative: str, candidate_sha: str) -> list[str]:
    return [
        "uv",
        "run",
        "python",
        "-m",
        "scripts.qualify_release_scope",
        "--qualification",
        qualification_relative,
        "--candidate-sha",
        candidate_sha,
        "--evidence",
        EVIDENCE_INDEX_PATH,
        "--release-stage",
        "beta",
        "--workflow-phase",
        "preparation",
        "--require-evidence",
    ]


def _load_index_preserving_format(index_path: Path) -> dict[str, Any]:
    raw = index_path.read_text(encoding="utf-8")
    index = json.loads(raw)
    if raw != _render_index(index):
        raise BetaEvidenceError("Evidence index formatting is not canonical; refusing to rewrite it.")
    if index.get("schema_version") != 1 or not isinstance(index.get("receipts"), list):
        raise BetaEvidenceError("Evidence index must be schema_version 1 with a receipts list.")
    return index


def _render_index(index: Mapping[str, Any]) -> str:
    return json.dumps(index, indent=2, sort_keys=True) + "\n"


def _case_entry(case_id: str) -> dict[str, Any]:
    catalog = PROOF_CATALOG[case_id]
    entry: dict[str, Any] = {
        "case_id": case_id,
        "observations": dict(catalog["observations"]),
        "proof": list(catalog["proof"]),
    }
    if "scope" in catalog:
        entry["scope"] = dict(catalog["scope"])
    entry["status"] = "passed"
    return entry


def produce_change_scoped_evidence(
    repo_root: Path,
    metadata: ReleaseMetadata,
    qualification_relative: str,
    *,
    runners: Runners,
    log_dir: Path,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> EvidenceResult | None:
    """Prove the blocking preparation cases and write the evidence; ``None`` when nothing blocks."""
    if metadata.channel != "beta":
        raise BetaEvidenceError(f"Change-scoped evidence is limited to Beta releases; found {metadata.channel!r}.")
    release_tag = metadata.release_tag
    candidate_sha = _require_candidate_checkout(repo_root, release_tag)
    document_relative = f"{QUALIFICATION_DIRECTORY}/{release_tag}-change-scoped-evidence-v1.json"
    document_path = repo_root / document_relative
    if document_path.exists():
        raise BetaEvidenceError(f"{document_relative} already exists; evidence documents are immutable.")
    if log_dir.resolve().is_relative_to(repo_root.resolve()):
        raise BetaEvidenceError("Logs must stay outside the repository.")
    log_dir.mkdir(parents=True, exist_ok=True)

    classifier_command = _classifier_command(qualification_relative, candidate_sha)
    print(f"Running: {' '.join(classifier_command)}", flush=True)
    classification = runners.classify(classifier_command, None)
    (log_dir / "preparation-report.json").write_text(classification.stdout, encoding="utf-8")
    if classification.exit_code == 0:
        print(f"Preparation classification passed for {candidate_sha}; no change-scoped evidence is needed.")
        return None
    if classification.exit_code != 2:
        raise BetaEvidenceError(
            f"Preparation classifier failed with exit code {classification.exit_code}: {classification.stderr.strip()}"
        )
    try:
        report = json.loads(classification.stdout)
    except json.JSONDecodeError as error:
        raise BetaEvidenceError("Preparation classifier did not print a JSON report.") from error
    blocking_case_ids = [str(case_id) for case_id in report.get("blocking_retests", [])]
    if (
        report.get("candidate_sha") != candidate_sha
        or report.get("workflow_phase") != "preparation"
        or report.get("passed") is not False
        or not blocking_case_ids
    ):
        raise BetaEvidenceError("Preparation report does not describe blocking cases for this candidate.")

    eligible = eligible_case_ids(load_policy(repo_root / POLICY_PATH))
    ineligible = [case_id for case_id in blocking_case_ids if case_id not in eligible]
    if ineligible:
        raise BetaEvidenceError(f"Blocking cases are not eligible for Beta change-scoped evidence: {ineligible}.")
    uncatalogued = [case_id for case_id in blocking_case_ids if case_id not in PROOF_CATALOG]
    if uncatalogued:
        raise BetaEvidenceError(f"Blocking cases have no catalogued proof: {uncatalogued}.")
    proofs = list(dict.fromkeys(proof for case_id in blocking_case_ids for proof in PROOF_CATALOG[case_id]["proof"]))
    for proof in proofs:
        _require_known_proof_kind(proof)

    validation: dict[str, Any] = {}
    python_modules = sorted({target[0] for proof in proofs if (target := python_proof_target(proof))})
    python_run: RecordedRun | None = None
    if python_modules:
        python_command = ["uv", "run", "python", "-m", "unittest", "-v", *python_modules]
        python_run = _record_run(
            runners.python_tests,
            python_command,
            display_command=" ".join(python_command),
            log_path=log_dir / "focused-python-tests.log",
        )
        python_counts = _unittest_counts(python_run.result)
        missing = _missing_python_proofs(proofs, parse_unittest_results(python_run.result.stderr))
        if missing:
            raise BetaEvidenceError(f"Python proofs did not pass: {missing}.")

    native_run = _record_run(
        runners.native_tests,
        NATIVE_TEST_COMMAND,
        display_command=" ".join(NATIVE_TEST_COMMAND),
        log_path=log_dir / "native-tests.log",
    )
    native_results = parse_xctest_results(native_run.result.stdout + native_run.result.stderr)
    native_counts = _xctest_counts(native_run.result, native_results)
    missing = _missing_swift_proofs(proofs, native_results, repo_root)
    if missing:
        raise BetaEvidenceError(f"Native proofs did not pass: {missing}.")
    validation["native_tests"] = {"command": native_run.command, **native_counts, "log_sha256": native_run.log_sha256}
    if python_run is not None:
        validation["focused_python_tests"] = {
            "command": python_run.command,
            **python_counts,
            "log_sha256": python_run.log_sha256,
        }

    # Always the public production endpoint: the document is committed and must not name a private host.
    support_diagnostics_endpoint = DEFAULT_SUPPORT_DIAGNOSTICS_ENDPOINT
    package_display = f"{SUPPORT_DIAGNOSTICS_ENDPOINT_ENV}={support_diagnostics_endpoint} {' '.join(PACKAGE_COMMAND)}"
    package_run = _record_run(
        runners.package,
        PACKAGE_COMMAND,
        display_command=package_display,
        log_path=log_dir / "package.log",
        environment={**os.environ, SUPPORT_DIAGNOSTICS_ENDPOINT_ENV: support_diagnostics_endpoint},
    )
    output_lines = [line.strip() for line in package_run.result.stdout.splitlines() if line.strip()]
    if package_run.result.exit_code != 0 or not output_lines or not output_lines[-1].endswith(".app"):
        raise BetaEvidenceError("Local package run and packaged smokes did not pass.")
    validation["packaged_app"] = {"exit_code": package_run.result.exit_code, "log_sha256": package_run.log_sha256}
    app_facts = runners.app_facts(Path(output_lines[-1]))
    if app_facts.get("build_version") != metadata.build_version:
        raise BetaEvidenceError("Packaged app build version does not match the committed release metadata.")

    if _require_candidate_checkout(repo_root, release_tag) != candidate_sha:
        raise BetaEvidenceError("HEAD moved while producing evidence.")

    packaged_candidate: dict[str, Any] = {
        "app_tree_sha256": app_facts["app_tree_sha256"],
        "architecture": app_facts["architecture"],
        "bundle_identifier": app_facts["bundle_identifier"],
        "codesign_verification": "passed",
        "package_command": package_display,
        "preview_presentation_smoke": "passed",
        "signing": "ad_hoc_local",
        "worker_cancellation_smoke": "passed",
        "signature_verification": ADHOC_SIGNATURE_SUMMARY,
        "worker_protocol_smoke": "passed",
        "support_diagnostics_endpoint": support_diagnostics_endpoint,
        "toolchain": runners.toolchain(),
        "package_attempts": 1,
    }
    semantics: dict[str, Any] = {
        "developer_id_or_notarization_claimed": False,
        "index_source": EVIDENCE_SOURCE,
        "meaning": EVIDENCE_MEANING,
    }
    precedent = _latest_precedent(repo_root, release_tag)
    if precedent is not None:
        semantics["project_precedent"] = precedent
    cases = [_case_entry(case_id) for case_id in blocking_case_ids]
    recorded_at = now().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    document = {
        "schema_version": 1,
        "qualification_id": f"{release_tag}-change-scoped-evidence-v1",
        "recorded_at": recorded_at,
        "source": {
            "build_version": metadata.build_version,
            "package_version": metadata.package_version,
            "public_version": metadata.public_version,
            "release_tag": release_tag,
            "source_sha": candidate_sha,
        },
        "failed_preparation_run": {
            "execution": "local_preparation_classification",
            "candidate_sha": candidate_sha,
            "workflow_phase": "preparation",
            "exit_code": classification.exit_code,
            "blocking_case_ids": blocking_case_ids,
            "signing_started": False,
            "release_identity_created": False,
            "report_sha256": _sha256_text(classification.stdout),
            "command": " ".join(classifier_command),
        },
        "packaged_candidate": packaged_candidate,
        "evidence_source_semantics": semantics,
        "validation": validation,
        "cases": cases,
        "privacy": {
            "diagnostic_tokens_recorded": False,
            "private_hostnames_recorded": False,
            "private_media_identifiers_recorded": False,
            "private_paths_recorded": False,
        },
        "acceptance": {
            "blocking_case_ids": [],
            "failed_case_count": 0,
            "passed": True,
            "passed_case_count": len(cases),
        },
        "result": "accepted_public_safe_summary",
    }
    rendered = json.dumps(document, indent=2) + "\n"
    for private_value in {str(repo_root.resolve()), str(Path.home()), str(log_dir.resolve())}:
        if private_value in rendered:
            raise BetaEvidenceError("Evidence document would record a private path.")

    index_path = repo_root / EVIDENCE_INDEX_PATH
    index = _load_index_preserving_format(index_path)
    document_digest = _sha256_text(rendered)
    existing_receipt_ids = {receipt.get("receipt_id") for receipt in index["receipts"]}
    receipts = [
        {
            "accepted_at": recorded_at,
            "case_id": case["case_id"],
            "receipt_id": f"{release_tag}:{case['case_id']}:{candidate_sha[:7]}",
            "reference": document_relative,
            "sha256": document_digest,
            "source": EVIDENCE_SOURCE,
            "source_sha": candidate_sha,
            "status": "accepted",
        }
        for case in cases
    ]
    duplicates = [receipt["receipt_id"] for receipt in receipts if receipt["receipt_id"] in existing_receipt_ids]
    if duplicates:
        raise BetaEvidenceError(f"Evidence index already contains receipts: {duplicates}.")
    index["receipts"].extend(receipts)
    document_path.write_text(rendered, encoding="utf-8")
    index_path.write_text(_render_index(index), encoding="utf-8")
    return EvidenceResult(document_path=document_relative, receipt_ids=tuple(r["receipt_id"] for r in receipts))


def _pump(source: IO[str], sink: IO[str], collected: list[str]) -> None:
    for line in source:
        collected.append(line)
        sink.write(line)
        sink.flush()


def stream_command(command: Sequence[str], environment: Mapping[str, str] | None) -> CommandResult:
    """Run a command in the repository, echoing and capturing stdout and stderr separately."""
    process = subprocess.Popen(
        list(command),
        cwd=REPO_ROOT,
        env=dict(environment) if environment is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stdout: list[str] = []
    stderr: list[str] = []
    assert process.stdout is not None and process.stderr is not None
    threads = [
        threading.Thread(target=_pump, args=(process.stdout, sys.stdout, stdout)),
        threading.Thread(target=_pump, args=(process.stderr, sys.stderr, stderr)),
    ]
    for thread in threads:
        thread.start()
    exit_code = process.wait()
    for thread in threads:
        thread.join()
    return CommandResult(exit_code=exit_code, stdout="".join(stdout), stderr="".join(stderr))


def capture_command(command: Sequence[str], environment: Mapping[str, str] | None) -> CommandResult:
    completed = subprocess.run(
        list(command),
        cwd=REPO_ROOT,
        env=dict(environment) if environment is not None else None,
        capture_output=True,
        text=True,
        check=False,
    )
    return CommandResult(exit_code=completed.returncode, stdout=completed.stdout, stderr=completed.stderr)


def inspect_packaged_app(app_path: Path) -> dict[str, str]:
    from scripts.artifact_identity import app_tree_sha256

    info = plistlib.loads((app_path / "Contents" / "Info.plist").read_bytes())
    executable = app_path / "Contents" / "MacOS" / str(info["CFBundleExecutable"])
    architectures = subprocess.run(
        ["lipo", "-archs", str(executable)], capture_output=True, text=True, check=True
    ).stdout.split()
    verify = subprocess.run(
        ["codesign", "--verify", "--deep", "--strict", str(app_path)], capture_output=True, text=True, check=False
    )
    if verify.returncode != 0:
        raise BetaEvidenceError("Packaged app failed codesign verification.")
    details = subprocess.run(["codesign", "-dv", str(app_path)], capture_output=True, text=True, check=False)
    if "Signature=adhoc" not in details.stderr or "TeamIdentifier=not set" not in details.stderr:
        raise BetaEvidenceError("Packaged app is not ad hoc signed without a team identifier.")
    return {
        "app_tree_sha256": app_tree_sha256(app_path),
        "architecture": " ".join(sorted(architectures)),
        "bundle_identifier": str(info["CFBundleIdentifier"]),
        "build_version": str(info["CFBundleVersion"]),
    }


def xcode_toolchain() -> str:
    output = subprocess.run(["xcodebuild", "-version"], capture_output=True, text=True, check=True).stdout
    match = re.fullmatch(r"(?P<xcode>Xcode \S+)\s+Build version (?P<build>\S+)\s*", output)
    if match is None:
        raise BetaEvidenceError("Unable to read the Xcode toolchain version.")
    return f"{match['xcode']} ({match['build']})"


def default_runners() -> Runners:
    return Runners(
        classify=capture_command,
        native_tests=stream_command,
        python_tests=stream_command,
        package=stream_command,
        app_facts=inspect_packaged_app,
        toolchain=xcode_toolchain,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--log-dir",
        type=Path,
        help="Directory outside the repository for command logs (default: a new temporary directory).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    log_dir = args.log_dir or Path(tempfile.mkdtemp(prefix="bd-to-avp-beta-evidence-"))
    try:
        metadata = load_release_metadata()
        qualification_relative = validate_configured_qualification_record(metadata).as_posix()
        result = produce_change_scoped_evidence(
            REPO_ROOT,
            metadata,
            qualification_relative,
            runners=default_runners(),
            log_dir=log_dir,
        )
    except (BetaEvidenceError, ReleaseError) as error:
        print(f"Change-scoped evidence failed: {error}", file=sys.stderr)
        print(f"Logs: {log_dir}", file=sys.stderr)
        return 1
    print(f"Logs (not committed): {log_dir}")
    if result is None:
        return 0
    print(f"Wrote {result.document_path}")
    print(f"Appended {len(result.receipt_ids)} receipts to {EVIDENCE_INDEX_PATH}:")
    for receipt_id in result.receipt_ids:
        print(f"- {receipt_id}")
    print(
        "Next: review the diff, commit both files on this qualify branch, and open its pull request "
        "against the unchanged candidate SHA on main."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
