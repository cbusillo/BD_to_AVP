#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import re
import shutil
import statistics
import subprocess
import sys

from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from itertools import pairwise
from pathlib import Path
from typing import Mapping, Sequence

from bd_to_avp.modules.video_quality_defaults import AUTOMATIC_DIRECT_QUALITY
from scripts import build_mv_hevc_encoder_macos
from scripts.qualify_direct_mv_hevc import QualificationFailure, command_path, measure
from scripts.qualify_mv_hevc_corpus import (
    EDGE264,
    MP4BOX,
    SPATIAL_MEDIA_TOOL,
    CorpusCase,
    _encode_direct,
    _measure_output,
    _tool_version,
    load_manifest,
    prepare_case,
)
from scripts.qualify_mv_hevc_quality_match import sha256_file


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SWEEP_PLAN = REPOSITORY_ROOT / "docs/qualification/direct-mv-hevc-quality-sweep-coarse-v1.json"
DEFAULT_ENCODER = REPOSITORY_ROOT / "build/mv-hevc-encoder/mv-hevc-encoder"
EVIDENCE_SCHEMA_VERSION = 1
MAX_CANDIDATES = 16
MAX_RUNS = 10
CANDIDATE_ID_PATTERN = re.compile(r"^q[0-9]{3}$")
WORK_DIRECTORY_MARKER = ".bd-to-avp-direct-quality-sweep.json"


@dataclass(frozen=True)
class SweepCandidate:
    candidate_id: str
    quality: float


@dataclass(frozen=True)
class SweepPlan:
    sweep_id: str
    corpus_path: Path
    corpus_id: str
    corpus_sha256: str
    balanced_quality: float
    runs_per_candidate: int
    candidates: tuple[SweepCandidate, ...]
    relative_path: str | None = None


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise QualificationFailure(f"{label} must be an object.")
    return value


def _array(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise QualificationFailure(f"{label} must be an array.")
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise QualificationFailure(f"{label} must be a non-empty string.")
    return value.strip()


def _quality(value: object, label: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(float(value)) or not 0 <= float(value) <= 1:
        raise QualificationFailure(f"{label} must be a finite number between 0 and 1.")
    return float(value)


def _repository_path(relative_path: str) -> Path:
    path = (REPOSITORY_ROOT / relative_path).resolve()
    try:
        path.relative_to(REPOSITORY_ROOT)
    except ValueError as error:
        raise QualificationFailure(f"Sweep corpus path escapes the repository: {relative_path}") from error
    return path


def parse_sweep_plan(raw: object) -> SweepPlan:
    document = _mapping(raw, "sweep plan")
    if document.get("schema_version") != 1:
        raise QualificationFailure("sweep plan schema_version must be 1.")
    sweep_id = _string(document.get("sweep_id"), "sweep_id")
    if document.get("target_id") != "direct_mv_hevc":
        raise QualificationFailure("sweep plan target_id must be 'direct_mv_hevc'.")
    if document.get("purpose") != "exploratory_candidates_not_ladder_mappings":
        raise QualificationFailure("sweep plan must remain exploratory rather than assigning ladder steps.")

    corpus = _mapping(document.get("corpus"), "corpus")
    corpus_path = _repository_path(_string(corpus.get("path"), "corpus.path"))
    corpus_id = _string(corpus.get("corpus_id"), "corpus.corpus_id")
    corpus_sha256 = _string(corpus.get("sha256"), "corpus.sha256")
    if len(corpus_sha256) != 64 or any(character not in "0123456789abcdef" for character in corpus_sha256):
        raise QualificationFailure("corpus.sha256 must be a lowercase SHA-256 identity.")

    balanced = _mapping(document.get("balanced"), "balanced")
    balanced_quality = _quality(balanced.get("quality"), "balanced.quality")
    if balanced_quality != AUTOMATIC_DIRECT_QUALITY:
        raise QualificationFailure("sweep Balanced quality must match the production direct default exactly.")
    if balanced.get("source") != "bd_to_avp.modules.video_quality_defaults.AUTOMATIC_DIRECT_QUALITY":
        raise QualificationFailure("sweep Balanced source must identify the production direct default.")

    runs_per_candidate = document.get("runs_per_candidate")
    if type(runs_per_candidate) is not int or not 1 <= runs_per_candidate <= MAX_RUNS:
        raise QualificationFailure(f"runs_per_candidate must be between 1 and {MAX_RUNS}.")

    raw_candidates = _array(document.get("candidates"), "candidates")
    if not 2 <= len(raw_candidates) <= MAX_CANDIDATES:
        raise QualificationFailure(f"candidates must contain between 2 and {MAX_CANDIDATES} entries.")
    candidates: list[SweepCandidate] = []
    for index, raw_candidate in enumerate(raw_candidates):
        candidate = _mapping(raw_candidate, f"candidates[{index}]")
        candidate_id = _string(candidate.get("id"), f"candidates[{index}].id")
        if not CANDIDATE_ID_PATTERN.fullmatch(candidate_id):
            raise QualificationFailure(f"candidates[{index}].id must use the stable qNNN format.")
        candidates.append(
            SweepCandidate(
                candidate_id=candidate_id,
                quality=_quality(candidate.get("quality"), f"candidates[{index}].quality"),
            )
        )
    if len({candidate.candidate_id for candidate in candidates}) != len(candidates):
        raise QualificationFailure("candidate IDs must be unique.")
    qualities = tuple(candidate.quality for candidate in candidates)
    if tuple(sorted(qualities)) != qualities or len(set(qualities)) != len(qualities):
        raise QualificationFailure("candidate qualities must be unique and strictly increasing.")
    if qualities.count(balanced_quality) != 1:
        raise QualificationFailure("candidate qualities must contain the Balanced production quality exactly once.")
    return SweepPlan(
        sweep_id=sweep_id,
        corpus_path=corpus_path,
        corpus_id=corpus_id,
        corpus_sha256=corpus_sha256,
        balanced_quality=balanced_quality,
        runs_per_candidate=runs_per_candidate,
        candidates=tuple(candidates),
    )


def load_sweep_plan(path: Path) -> tuple[SweepPlan, str]:
    resolved_path = path.resolve()
    try:
        relative_path = resolved_path.relative_to(REPOSITORY_ROOT).as_posix()
    except ValueError as error:
        raise QualificationFailure("Direct-quality sweep plans must be committed inside the repository.") from error
    try:
        raw = json.loads(resolved_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise QualificationFailure(f"Could not read direct-quality sweep plan {path.name}: {error}") from error
    parsed = parse_sweep_plan(raw)
    plan = SweepPlan(
        sweep_id=parsed.sweep_id,
        corpus_path=parsed.corpus_path,
        corpus_id=parsed.corpus_id,
        corpus_sha256=parsed.corpus_sha256,
        balanced_quality=parsed.balanced_quality,
        runs_per_candidate=parsed.runs_per_candidate,
        candidates=parsed.candidates,
        relative_path=relative_path,
    )
    if not plan.corpus_path.is_file():
        raise QualificationFailure("The sweep corpus manifest is unavailable.")
    manifest_sha256 = sha256_file(plan.corpus_path)
    if manifest_sha256 != plan.corpus_sha256:
        raise QualificationFailure("The sweep corpus manifest does not match its pinned SHA-256 identity.")
    manifest = load_manifest(plan.corpus_path)
    if manifest.corpus_id != plan.corpus_id:
        raise QualificationFailure("The sweep corpus ID does not match the referenced manifest.")
    return plan, sha256_file(resolved_path)


def _require_head_tracked_file(path: Path, label: str) -> str:
    resolved = path.resolve()
    try:
        relative_path = resolved.relative_to(REPOSITORY_ROOT).as_posix()
    except ValueError as error:
        raise QualificationFailure(f"{label} must be inside the repository.") from error
    tracked = subprocess.run(
        ["git", "-C", REPOSITORY_ROOT, "ls-files", "--error-unmatch", "--", relative_path],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if tracked.returncode != 0:
        raise QualificationFailure(f"{label} must be tracked in the recorded source commit.")
    try:
        committed = subprocess.run(
            ["git", "-C", REPOSITORY_ROOT, "show", f"HEAD:{relative_path}"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout
        working = resolved.read_bytes()
    except (OSError, subprocess.CalledProcessError) as error:
        raise QualificationFailure(f"Could not verify committed {label} bytes.") from error
    if committed != working:
        raise QualificationFailure(f"{label} bytes must match the recorded source commit exactly.")
    return relative_path


def summarize_runs(runs: Sequence[Mapping[str, object]]) -> dict[str, object]:
    if not runs:
        raise ValueError("runs are required")
    qualities = [float(run["min_same_eye_ssim"]) for run in runs]
    sizes = [int(run["final_bytes"]) for run in runs]
    elapsed = [float(run["elapsed_seconds"]) for run in runs]
    median_quality = statistics.median(qualities)
    median_size = statistics.median(sizes)
    median_elapsed = statistics.median(elapsed)
    return {
        "run_count": len(runs),
        "median_min_same_eye_ssim": median_quality,
        "minimum_min_same_eye_ssim": min(qualities),
        "maximum_min_same_eye_ssim": max(qualities),
        "repeat_ssim_spread": max(qualities) - min(qualities),
        "median_final_bytes": median_size,
        "minimum_final_bytes": min(sizes),
        "maximum_final_bytes": max(sizes),
        "repeat_size_ratio_spread": (max(sizes) - min(sizes)) / median_size,
        "median_elapsed_seconds": median_elapsed,
        "minimum_elapsed_seconds": min(elapsed),
        "maximum_elapsed_seconds": max(elapsed),
        "minimum_eye_order_margin": min(float(run["min_eye_order_margin"]) for run in runs),
    }


def _candidate_record(case: Mapping[str, object], candidate_id: str) -> dict[str, object] | None:
    candidates = case.get("candidates")
    if not isinstance(candidates, list):
        return None
    for candidate in candidates:
        if isinstance(candidate, dict) and candidate.get("id") == candidate_id:
            return candidate
    return None


def _case_record(evidence: Mapping[str, object], case_id: str) -> dict[str, object] | None:
    cases = evidence.get("cases")
    if not isinstance(cases, list):
        return None
    for case in cases:
        if isinstance(case, dict) and case.get("id") == case_id:
            return case
    return None


def _run_for_index(candidate: Mapping[str, object], run_index: int) -> Mapping[str, object] | None:
    runs = candidate.get("runs")
    if not isinstance(runs, list):
        return None
    matches = [run for run in runs if isinstance(run, Mapping) and run.get("run_index") == run_index]
    if len(matches) > 1:
        raise QualificationFailure(f"Candidate {candidate.get('id')} contains duplicate run index {run_index}.")
    return matches[0] if matches else None


def _finite_number(value: object, label: str, *, positive: bool = False, nonnegative: bool = False) -> float:
    if type(value) not in (int, float) or not math.isfinite(float(value)):
        raise QualificationFailure(f"{label} must be a finite number.")
    numeric = float(value)
    if positive and numeric <= 0:
        raise QualificationFailure(f"{label} must be positive.")
    if nonnegative and numeric < 0:
        raise QualificationFailure(f"{label} must be non-negative.")
    return numeric


def _validate_run_record(run: Mapping[str, object], candidate: SweepCandidate, run_index: int) -> None:
    expected_keys = {
        "effective_bitrate_mbps",
        "final_bytes",
        "left_cross_ssim",
        "left_match_ssim",
        "min_eye_order_margin",
        "min_same_eye_ssim",
        "right_cross_ssim",
        "right_match_ssim",
        "sha256",
        "run_index",
        "target_quality",
        "elapsed_seconds",
        "user_cpu_seconds",
        "system_cpu_seconds",
    }
    if set(run) != expected_keys:
        raise QualificationFailure(f"Candidate {candidate.candidate_id} run {run_index} has an invalid record shape.")
    if run.get("run_index") != run_index or run.get("target_quality") != candidate.quality:
        raise QualificationFailure(f"Candidate {candidate.candidate_id} run {run_index} has mismatched identity.")
    if type(run.get("final_bytes")) is not int or int(run["final_bytes"]) <= 0:
        raise QualificationFailure(f"Candidate {candidate.candidate_id} run {run_index} final_bytes must be positive.")
    _finite_number(run.get("effective_bitrate_mbps"), "effective_bitrate_mbps", positive=True)
    for key in ("left_cross_ssim", "left_match_ssim", "min_same_eye_ssim", "right_cross_ssim", "right_match_ssim"):
        value = _finite_number(run.get(key), key)
        if not 0 <= value <= 1:
            raise QualificationFailure(f"Candidate {candidate.candidate_id} run {run_index} {key} must be 0..1.")
    _finite_number(run.get("min_eye_order_margin"), "min_eye_order_margin")
    _finite_number(run.get("elapsed_seconds"), "elapsed_seconds", positive=True)
    _finite_number(run.get("user_cpu_seconds"), "user_cpu_seconds", nonnegative=True)
    _finite_number(run.get("system_cpu_seconds"), "system_cpu_seconds", nonnegative=True)
    digest = run.get("sha256")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise QualificationFailure(f"Candidate {candidate.candidate_id} run {run_index} has an invalid SHA-256.")


def _run_complete(candidate_record: Mapping[str, object], candidate: SweepCandidate, run_index: int) -> bool:
    run = _run_for_index(candidate_record, run_index)
    if run is None:
        return False
    _validate_run_record(run, candidate, run_index)
    return True


def _candidate_order(plan: SweepPlan, run_index: int) -> tuple[SweepCandidate, ...]:
    return plan.candidates if run_index % 2 == 0 else tuple(reversed(plan.candidates))


def _refresh_summaries(
    evidence: dict[str, object],
    plan: SweepPlan,
    case_definitions: Mapping[str, CorpusCase],
    *,
    all_gated_case_ids: set[str],
) -> None:
    cases = evidence["cases"]
    assert isinstance(cases, list)
    for case in cases:
        assert isinstance(case, dict)
        candidates = case["candidates"]
        assert isinstance(candidates, list)
        for candidate in candidates:
            assert isinstance(candidate, dict)
            runs = candidate["runs"]
            assert isinstance(runs, list)
            candidate["summary"] = summarize_runs(runs) if len(runs) == plan.runs_per_candidate else None

        balanced = _candidate_record(case, _candidate_id_for_quality(plan, plan.balanced_quality))
        balanced_summary = balanced.get("summary") if balanced is not None else None
        if not isinstance(balanced_summary, Mapping):
            continue
        for candidate in candidates:
            assert isinstance(candidate, dict)
            summary = candidate.get("summary")
            if not isinstance(summary, dict):
                continue
            if float(candidate["quality"]) == plan.balanced_quality:
                summary["quality_delta"] = 0.0
                summary["output_size_ratio"] = 1.0
                summary["encode_time_ratio"] = 1.0
            else:
                summary["quality_delta"] = float(summary["median_min_same_eye_ssim"]) - float(
                    balanced_summary["median_min_same_eye_ssim"]
                )
                summary["output_size_ratio"] = float(summary["median_final_bytes"]) / float(
                    balanced_summary["median_final_bytes"]
                )
                summary["encode_time_ratio"] = float(summary["median_elapsed_seconds"]) / float(
                    balanced_summary["median_elapsed_seconds"]
                )

    candidate_summaries: list[dict[str, object]] = []
    for candidate in plan.candidates:
        summaries: list[Mapping[str, object]] = []
        for case in cases:
            assert isinstance(case, dict)
            record = _candidate_record(case, candidate.candidate_id)
            summary = record.get("summary") if record is not None else None
            if isinstance(summary, Mapping) and "quality_delta" in summary:
                summaries.append(summary)
        complete = len(summaries) == len(case_definitions)
        aggregate: dict[str, object] = {
            "id": candidate.candidate_id,
            "quality": candidate.quality,
            "case_count": len(summaries),
            "complete": complete,
        }
        if summaries:
            aggregate.update(
                {
                    "quality_delta": 0.0
                    if candidate.quality == plan.balanced_quality
                    else min(float(summary["quality_delta"]) for summary in summaries),
                    "median_quality_delta": statistics.median(float(summary["quality_delta"]) for summary in summaries),
                    "output_size_ratio": 1.0
                    if candidate.quality == plan.balanced_quality
                    else max(float(summary["output_size_ratio"]) for summary in summaries),
                    "median_output_size_ratio": statistics.median(
                        float(summary["output_size_ratio"]) for summary in summaries
                    ),
                    "encode_time_seconds": statistics.median(
                        float(summary["median_elapsed_seconds"]) for summary in summaries
                    ),
                    "median_encode_time_ratio": statistics.median(
                        float(summary["encode_time_ratio"]) for summary in summaries
                    ),
                    "minimum_eye_order_margin": min(
                        float(summary["minimum_eye_order_margin"]) for summary in summaries
                    ),
                    "maximum_repeat_ssim_spread": max(float(summary["repeat_ssim_spread"]) for summary in summaries),
                    "maximum_repeat_size_ratio_spread": max(
                        float(summary["repeat_size_ratio_spread"]) for summary in summaries
                    ),
                }
            )
            manifest = evidence.get("manifest")
            if not isinstance(manifest, Mapping):
                raise QualificationFailure("Sweep evidence is missing its corpus identity.")
            aggregate["ladder_evidence"] = {
                "ready": False,
                "source_git_sha": evidence["source_git_sha"],
                "fixture_manifest_sha256": manifest["sha256"],
                "quality_delta": aggregate["quality_delta"],
                "output_size_ratio": aggregate["output_size_ratio"],
                "encode_time_seconds": aggregate["encode_time_seconds"],
                "missing": ["artifact_sha256", "rationale", "ladder_step_assignment"],
            }
        candidate_summaries.append(aggregate)
    evidence["candidate_summaries"] = candidate_summaries

    warnings: list[dict[str, object]] = []
    for case in cases:
        assert isinstance(case, Mapping)
        complete_case_candidates: list[Mapping[str, object]] = []
        for candidate in plan.candidates:
            record = _candidate_record(case, candidate.candidate_id)
            summary = record.get("summary") if record is not None else None
            if not isinstance(summary, Mapping):
                complete_case_candidates = []
                break
            complete_case_candidates.append(record)
        for previous, current in pairwise(complete_case_candidates):
            previous_summary = previous["summary"]
            current_summary = current["summary"]
            assert isinstance(previous_summary, Mapping)
            assert isinstance(current_summary, Mapping)
            base_warning = {
                "case_id": case["id"],
                "lower_candidate": previous["id"],
                "higher_candidate": current["id"],
            }
            quality_separation = float(current_summary["quality_delta"]) - float(previous_summary["quality_delta"])
            if quality_separation < 0:
                warnings.append({"code": "quality_reversal", **base_warning})
            quality_noise = max(
                float(previous_summary["repeat_ssim_spread"]),
                float(current_summary["repeat_ssim_spread"]),
            )
            if quality_separation <= quality_noise:
                warnings.append(
                    {
                        "code": "quality_not_distinct_from_repeat_noise",
                        **base_warning,
                        "observed_separation": quality_separation,
                        "repeat_noise": quality_noise,
                    }
                )
            size_separation = float(current_summary["output_size_ratio"]) - float(previous_summary["output_size_ratio"])
            if size_separation < 0:
                warnings.append({"code": "storage_reversal", **base_warning})
            size_noise = max(
                float(previous_summary["repeat_size_ratio_spread"]),
                float(current_summary["repeat_size_ratio_spread"]),
            )
            if size_separation <= size_noise:
                warnings.append(
                    {
                        "code": "storage_not_distinct_from_repeat_noise",
                        **base_warning,
                        "observed_separation": size_separation,
                        "repeat_noise": size_noise,
                    }
                )
    evidence["monotonicity_warnings"] = warnings

    complete_summaries = [summary for summary in candidate_summaries if summary["complete"] is True]
    cells_complete = len(complete_summaries) == len(plan.candidates)
    full_corpus = set(case_definitions) == all_gated_case_ids
    eye_order_passed = cells_complete and all(
        isinstance(summary.get("minimum_eye_order_margin"), (int, float))
        and float(summary["minimum_eye_order_margin"]) >= case_definitions[str(case["id"])].minimum_eye_order_margin
        for case in cases
        if isinstance(case, Mapping)
        for summary in [
            candidate.get("summary")
            for candidate in case.get("candidates", [])
            if isinstance(candidate, Mapping) and isinstance(candidate.get("summary"), Mapping)
        ]
    )
    evidence["acceptance"] = {
        "complete": cells_complete,
        "full_quality_gated_corpus": full_corpus,
        "eye_order_passed": eye_order_passed,
        "strict_monotonicity_passed": not any(
            warning["code"] in {"quality_reversal", "storage_reversal"} for warning in warnings
        ),
        "candidate_separation_passed": not any(
            warning["code"] in {"quality_not_distinct_from_repeat_noise", "storage_not_distinct_from_repeat_noise"}
            for warning in warnings
        ),
        "execution_passed": cells_complete and eye_order_passed,
        "ladder_evidence_ready": False,
        "ladder_mapping_selected": False,
        "passed": cells_complete and full_corpus and eye_order_passed and not warnings,
    }


def _candidate_id_for_quality(plan: SweepPlan, quality: float) -> str:
    return next(candidate.candidate_id for candidate in plan.candidates if candidate.quality == quality)


def _atomic_write(path: Path, evidence: Mapping[str, object]) -> None:
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary_path.replace(path)


def _prepare_owned_work_directory(work_directory: Path, plan: SweepPlan, plan_sha256: str) -> Path:
    resolved = work_directory.resolve()
    dangerous = {Path("/").resolve(), Path.home().resolve(), REPOSITORY_ROOT}
    if resolved in dangerous:
        raise QualificationFailure("Sweep work directory must be a dedicated non-root directory.")
    marker = resolved / WORK_DIRECTORY_MARKER
    expected_marker = {
        "schema_version": 1,
        "sweep_id": plan.sweep_id,
        "sweep_plan_sha256": plan_sha256,
    }
    if resolved.exists():
        if marker.is_file():
            try:
                observed = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise QualificationFailure(f"Could not validate sweep work-directory ownership: {error}") from error
            if observed != expected_marker:
                raise QualificationFailure("Sweep work directory belongs to a different sweep identity.")
        elif any(resolved.iterdir()):
            raise QualificationFailure("Sweep work directory is non-empty and has no ownership marker.")
        else:
            _atomic_write(marker, expected_marker)
    else:
        resolved.mkdir(parents=True)
        _atomic_write(marker, expected_marker)
    return resolved


def _owned_case_directory(work_directory: Path, case_id: str) -> Path:
    case_directory = (work_directory / case_id).resolve()
    try:
        case_directory.relative_to(work_directory)
    except ValueError as error:
        raise QualificationFailure(f"Unsafe corpus case work path: {case_id}") from error
    marker = work_directory / WORK_DIRECTORY_MARKER
    if not marker.is_file():
        raise QualificationFailure("Sweep work-directory ownership marker is missing.")
    return case_directory


def _reset_case_directory(work_directory: Path, case_id: str) -> Path:
    case_directory = _owned_case_directory(work_directory, case_id)
    if case_directory.exists():
        shutil.rmtree(case_directory)
    case_directory.mkdir(parents=True)
    return case_directory


def _git_head_from_clean_worktree() -> str:
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=normal"],
        cwd=REPOSITORY_ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()
    if status:
        raise QualificationFailure("Direct-quality sweep evidence requires a clean source worktree.")
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPOSITORY_ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()


def _sweep_environment_evidence(encoder: Path, ffmpeg: str, ffprobe: str, source_git_sha: str) -> dict[str, object]:
    return {
        "encoder_sha256": sha256_file(encoder),
        "edge264_sha256": sha256_file(EDGE264),
        "ffmpeg": _tool_version([ffmpeg, "-hide_banner", "-version"]),
        "ffprobe": _tool_version([ffprobe, "-hide_banner", "-version"]),
        "git_head": source_git_sha,
        "machine": platform.machine(),
        "macos_version": platform.mac_ver()[0],
        "mp4box_sha256": sha256_file(MP4BOX),
        "platform": platform.system(),
        "spatial_media_tool_sha256": sha256_file(SPATIAL_MEDIA_TOOL),
    }


def _new_evidence(
    plan: SweepPlan,
    plan_sha256: str,
    environment: Mapping[str, object],
    selected_cases: Sequence[CorpusCase],
) -> dict[str, object]:
    if plan.relative_path is None:
        raise QualificationFailure("Sweep plan is not bound to a repository-relative path.")
    now = datetime.now(UTC).isoformat()
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "sweep_id": plan.sweep_id,
        "created_at": now,
        "updated_at": now,
        "source_git_sha": environment["git_head"],
        "source_tree_dirty": False,
        "sweep_plan": {"path": plan.relative_path, "sha256": plan_sha256},
        "manifest": {
            "path": plan.corpus_path.name,
            "corpus_id": plan.corpus_id,
            "sha256": plan.corpus_sha256,
        },
        "method": {
            "runs_per_candidate": plan.runs_per_candidate,
            "balanced_quality": plan.balanced_quality,
            "candidate_order": "ascending_even_runs_descending_odd_runs",
            "quality_metric": "minimum decoded same-eye SSIM",
            "aggregation": {
                "quality_delta": "minimum gated-case median SSIM delta versus same-sweep Balanced",
                "output_size_ratio": "maximum gated-case median byte ratio versus same-sweep Balanced",
                "encode_time_seconds": "median gated-case median direct encode wall time",
            },
            "non_gating_cases": "excluded from direct quality calibration",
        },
        "environment": dict(environment),
        "selected_case_ids": [case.case_id for case in selected_cases],
        "candidates": [{"id": candidate.candidate_id, "quality": candidate.quality} for candidate in plan.candidates],
        "cases": [],
        "candidate_summaries": [],
        "monotonicity_warnings": [],
        "acceptance": {
            "complete": False,
            "full_quality_gated_corpus": False,
            "eye_order_passed": False,
            "strict_monotonicity_passed": False,
            "candidate_separation_passed": False,
            "execution_passed": False,
            "ladder_evidence_ready": False,
            "ladder_mapping_selected": False,
            "passed": False,
        },
    }


def _load_resume_evidence(
    output_path: Path,
    *,
    plan: SweepPlan,
    plan_sha256: str,
    environment: Mapping[str, object],
    selected_case_ids: Sequence[str],
) -> dict[str, object]:
    try:
        evidence = json.loads(output_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise QualificationFailure(f"Could not read resumable sweep evidence: {error}") from error
    if not isinstance(evidence, dict) or evidence.get("schema_version") != EVIDENCE_SCHEMA_VERSION:
        raise QualificationFailure("Resumable sweep evidence uses an unsupported schema.")
    expected = {
        "sweep_id": plan.sweep_id,
        "source_git_sha": environment["git_head"],
        "selected_case_ids": list(selected_case_ids),
    }
    for key, value in expected.items():
        if evidence.get(key) != value:
            raise QualificationFailure(f"Resumable sweep evidence does not match current {key}.")
    if plan.relative_path is None or evidence.get("sweep_plan") != {
        "path": plan.relative_path,
        "sha256": plan_sha256,
    }:
        raise QualificationFailure("Resumable sweep evidence does not match the current sweep plan.")
    manifest = evidence.get("manifest")
    if not isinstance(manifest, Mapping) or manifest.get("sha256") != plan.corpus_sha256:
        raise QualificationFailure("Resumable sweep evidence does not match the current corpus.")
    existing_environment = evidence.get("environment")
    if not isinstance(existing_environment, Mapping) or dict(existing_environment) != dict(environment):
        raise QualificationFailure("Resumable sweep evidence does not match the current toolchain environment.")
    _validate_resume_cases(evidence, plan, selected_case_ids)
    return evidence


def _validate_resume_cases(evidence: Mapping[str, object], plan: SweepPlan, selected_case_ids: Sequence[str]) -> None:
    cases = evidence.get("cases")
    if not isinstance(cases, list):
        raise QualificationFailure("Resumable sweep evidence cases must be an array.")
    observed_case_ids = [case.get("id") for case in cases if isinstance(case, Mapping)]
    if len(observed_case_ids) != len(cases) or len(set(observed_case_ids)) != len(observed_case_ids):
        raise QualificationFailure("Resumable sweep evidence contains invalid or duplicate case IDs.")
    if not set(observed_case_ids).issubset(set(selected_case_ids)):
        raise QualificationFailure("Resumable sweep evidence contains an unselected case.")
    expected_candidate_ids = [candidate.candidate_id for candidate in plan.candidates]
    for case in cases:
        assert isinstance(case, Mapping)
        if not isinstance(case.get("source"), Mapping) or not isinstance(case.get("prepared"), Mapping):
            raise QualificationFailure(f"Resumable case {case.get('id')} is missing source identity.")
        candidates = case.get("candidates")
        if not isinstance(candidates, list) or len(candidates) != len(plan.candidates):
            raise QualificationFailure(f"Resumable case {case.get('id')} has an incomplete candidate set.")
        observed_candidate_ids = [candidate.get("id") for candidate in candidates if isinstance(candidate, Mapping)]
        if observed_candidate_ids != expected_candidate_ids:
            raise QualificationFailure(f"Resumable case {case.get('id')} candidate IDs do not match the sweep plan.")
        for candidate_record, candidate in zip(candidates, plan.candidates, strict=True):
            assert isinstance(candidate_record, Mapping)
            if candidate_record.get("quality") != candidate.quality:
                raise QualificationFailure(f"Resumable candidate {candidate.candidate_id} quality does not match.")
            runs = candidate_record.get("runs")
            if not isinstance(runs, list) or len(runs) > plan.runs_per_candidate:
                raise QualificationFailure(f"Resumable candidate {candidate.candidate_id} has invalid runs.")
            run_indices = [run.get("run_index") for run in runs if isinstance(run, Mapping)]
            if len(run_indices) != len(runs) or len(set(run_indices)) != len(run_indices):
                raise QualificationFailure(f"Resumable candidate {candidate.candidate_id} has duplicate run indices.")
            if any(type(index) is not int or not 0 <= index < plan.runs_per_candidate for index in run_indices):
                raise QualificationFailure(f"Resumable candidate {candidate.candidate_id} has an invalid run index.")
            for run in runs:
                assert isinstance(run, Mapping)
                _validate_run_record(run, candidate, int(run["run_index"]))


def _case_complete(case: Mapping[str, object], plan: SweepPlan) -> bool:
    candidates = case.get("candidates")
    if not isinstance(candidates, list) or len(candidates) != len(plan.candidates):
        return False
    for expected in plan.candidates:
        candidate = _candidate_record(case, expected.candidate_id)
        if candidate is None or any(
            not _run_complete(candidate, expected, run_index) for run_index in range(plan.runs_per_candidate)
        ):
            return False
    return True


def run_quality_sweep(
    sweep_plan_path: Path,
    output_path: Path,
    work_directory: Path,
    encoder_path: Path,
    *,
    resume: bool,
    case_ids: Sequence[str] = (),
) -> dict[str, object]:
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise QualificationFailure("Direct MV-HEVC quality sweep requires macOS arm64.")
    for tool in (EDGE264, SPATIAL_MEDIA_TOOL, MP4BOX):
        if not tool.is_file() or not os.access(tool, os.X_OK):
            raise QualificationFailure(f"Required bundled tool is unavailable: {tool.name}")
    source_git_sha = _git_head_from_clean_worktree()
    plan, plan_sha256 = load_sweep_plan(sweep_plan_path)
    if _require_head_tracked_file(sweep_plan_path, "Sweep plan") != plan.relative_path:
        raise QualificationFailure("Sweep plan repository identity changed during validation.")
    _require_head_tracked_file(plan.corpus_path, "Corpus manifest")
    manifest = load_manifest(plan.corpus_path)
    gated_cases = tuple(case for case in manifest.cases if case.quality_gate)
    gated_by_id = {case.case_id: case for case in gated_cases}
    if case_ids:
        if len(set(case_ids)) != len(case_ids):
            raise QualificationFailure("Subset case IDs must not contain duplicates.")
        unknown = sorted(set(case_ids) - set(gated_by_id))
        if unknown:
            raise QualificationFailure("Unknown or non-gating case IDs: " + ", ".join(unknown))
        requested = set(case_ids)
        selected_cases = tuple(case for case in gated_cases if case.case_id in requested)
    else:
        selected_cases = gated_cases
    if not selected_cases:
        raise QualificationFailure("At least one quality-gated corpus case is required.")

    ffmpeg = command_path("ffmpeg")
    ffprobe = command_path("ffprobe")
    encoder_path.parent.mkdir(parents=True, exist_ok=True)
    if not encoder_path.is_file():
        build_mv_hevc_encoder_macos.build_encoder(encoder_path)
    environment = _sweep_environment_evidence(encoder_path, ffmpeg, ffprobe, source_git_sha)
    if environment["git_head"] != source_git_sha:
        raise QualificationFailure("Sweep environment Git identity changed during preflight.")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    work_directory = _prepare_owned_work_directory(work_directory, plan, plan_sha256)
    selected_case_ids = [case.case_id for case in selected_cases]
    if output_path.exists():
        if not resume:
            raise QualificationFailure("Sweep output already exists; use --resume or choose a new output path.")
        evidence = _load_resume_evidence(
            output_path,
            plan=plan,
            plan_sha256=plan_sha256,
            environment=environment,
            selected_case_ids=selected_case_ids,
        )
    else:
        evidence = _new_evidence(plan, plan_sha256, environment, selected_cases)
        _atomic_write(output_path, evidence)

    case_definitions = {case.case_id: case for case in selected_cases}
    for definition in selected_cases:
        existing_case = _case_record(evidence, definition.case_id)
        case_work = _reset_case_directory(work_directory, definition.case_id)
        prepared = prepare_case(definition, case_work, ffmpeg=ffmpeg, ffprobe=ffprobe)
        if existing_case is None:
            existing_case = {
                "id": definition.case_id,
                "tags": list(definition.tags),
                "quality_gate": True,
                "source": dict(prepared.source_evidence),
                "prepared": {
                    "duration_seconds": prepared.duration_seconds,
                    "frame_count": prepared.frame_count,
                    "eye_width": definition.output_eye_width,
                    "eye_height": definition.output_eye_height,
                    "frame_rate": definition.output_frame_rate,
                },
                "candidates": [
                    {"id": candidate.candidate_id, "quality": candidate.quality, "runs": [], "summary": None}
                    for candidate in plan.candidates
                ],
            }
            evidence["cases"].append(existing_case)
        else:
            expected_prepared = {
                "duration_seconds": prepared.duration_seconds,
                "frame_count": prepared.frame_count,
                "eye_width": definition.output_eye_width,
                "eye_height": definition.output_eye_height,
                "frame_rate": definition.output_frame_rate,
            }
            if (
                existing_case.get("source") != dict(prepared.source_evidence)
                or existing_case.get("prepared") != expected_prepared
            ):
                raise QualificationFailure(
                    f"Prepared source identity changed while resuming case {definition.case_id}."
                )
            if _case_complete(existing_case, plan):
                shutil.rmtree(case_work)
                continue

        for run_index in range(plan.runs_per_candidate):
            for candidate in _candidate_order(plan, run_index):
                candidate_record = _candidate_record(existing_case, candidate.candidate_id)
                if candidate_record is None:
                    raise QualificationFailure(f"Candidate {candidate.candidate_id} is missing from sweep evidence.")
                if _run_complete(candidate_record, candidate, run_index):
                    continue
                output = case_work / f"{candidate.candidate_id}-run-{run_index + 1}.mov"
                _, metrics = measure(
                    partial(
                        _encode_direct,
                        ffmpeg,
                        encoder_path,
                        prepared,
                        output,
                        quality=candidate.quality,
                    )
                )
                measured = _measure_output(
                    ffmpeg,
                    prepared,
                    output,
                    case_work / f"{candidate.candidate_id}-run-{run_index + 1}-split",
                    target_bitrate_mbps=None,
                )
                measured.pop("target_bitrate_mbps", None)
                measured.update(
                    {
                        "run_index": run_index,
                        "target_quality": candidate.quality,
                        "elapsed_seconds": round(metrics.elapsed_seconds, 6),
                        "user_cpu_seconds": round(metrics.user_cpu_seconds, 6),
                        "system_cpu_seconds": round(metrics.system_cpu_seconds, 6),
                    }
                )
                runs = candidate_record.get("runs")
                if not isinstance(runs, list):
                    raise QualificationFailure(f"Candidate {candidate.candidate_id} runs are invalid.")
                runs.append(measured)
                runs.sort(key=lambda run: int(run["run_index"]))
                evidence["updated_at"] = datetime.now(UTC).isoformat()
                _refresh_summaries(
                    evidence,
                    plan,
                    case_definitions,
                    all_gated_case_ids=set(gated_by_id),
                )
                _atomic_write(output_path, evidence)
        shutil.rmtree(case_work)

    evidence["updated_at"] = datetime.now(UTC).isoformat()
    _refresh_summaries(
        evidence,
        plan,
        case_definitions,
        all_gated_case_ids=set(gated_by_id),
    )
    _atomic_write(output_path, evidence)
    return evidence


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Measure a bounded direct MV-HEVC quality grid against a same-sweep Balanced reference."
    )
    parser.add_argument("--sweep-plan", type=Path, default=DEFAULT_SWEEP_PLAN)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--work-directory", type=Path, required=True)
    parser.add_argument("--encoder", type=Path, default=DEFAULT_ENCODER)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--case-id", action="append", default=[])
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        evidence = run_quality_sweep(
            args.sweep_plan.resolve(),
            args.output.resolve(),
            args.work_directory.resolve(),
            args.encoder.resolve(),
            resume=args.resume,
            case_ids=args.case_id,
        )
    except (
        AssertionError,
        KeyError,
        OSError,
        TypeError,
        subprocess.SubprocessError,
        QualificationFailure,
        ValueError,
    ) as error:
        print(f"Direct MV-HEVC quality sweep failed: {error}", file=sys.stderr)
        return 2
    acceptance = evidence.get("acceptance")
    if not isinstance(acceptance, Mapping):
        print("Direct MV-HEVC quality sweep failed: acceptance record is missing.", file=sys.stderr)
        return 2
    if acceptance.get("execution_passed") is True and acceptance.get("full_quality_gated_corpus") is False:
        return 0
    return 0 if acceptance.get("passed") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
