#!/usr/bin/env python3

from __future__ import annotations

import argparse
import copy
import fcntl
import json
import math
import os
import platform
import shutil
import stat
import statistics
import subprocess
import sys
import tomllib

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from fractions import Fraction
from functools import partial
from itertools import pairwise, product
from pathlib import Path
from typing import Iterator, Mapping, Sequence

from bd_to_avp.modules.video_quality_defaults import (
    AUTOMATIC_GENERATED_EYE_BITRATE_MBPS,
    AUTOMATIC_GENERATED_MERGE_QUALITY,
)
from scripts.qualify_direct_mv_hevc import (
    CURRENT_REQUIRED_BOX_TYPES,
    QualificationFailure,
    box_types,
    ffprobe_stream,
    measure,
    run,
)
from scripts.qualify_direct_mv_hevc_quality_sweep import summarize_runs
from scripts.qualify_mv_hevc_corpus import (
    EDGE264,
    MP4BOX,
    SPATIAL_MEDIA_TOOL,
    CorpusCase,
    _encode_generated,
    _measure_output,
    _tool_version,
    load_manifest,
    prepare_case,
    redact_private_source_paths,
)
from scripts.qualify_mv_hevc_quality_match import sha256_file


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EXPERIMENT_PLAN = REPOSITORY_ROOT / "docs/qualification/generated-mv-hevc-calibration-sweep-v1.json"
EVIDENCE_SCHEMA_VERSION = 1
MAX_RUNS = 10
WORK_DIRECTORY_MARKER = ".bd-to-avp-generated-mv-hevc-calibration.json"
LOCK_SUFFIX = ".bd-to-avp-generated-mv-hevc-calibration.lock"


@dataclass(frozen=True)
class CorpusBinding:
    binding_id: str
    source_manifest_path: Path
    source_corpus_id: str
    source_manifest_sha256: str
    selected_case_ids: tuple[str, ...]
    expected_case_sources: Mapping[str, Mapping[str, object]]
    required_coverage: tuple[str, ...]
    relative_path: str | None = None


@dataclass(frozen=True)
class ExperimentCell:
    cell_id: str
    eye_bitrate_mbps: int
    merge_quality: int


@dataclass(frozen=True)
class ExperimentPlan:
    experiment_id: str
    binding_path: Path
    binding_id: str
    binding_sha256: str
    balanced_eye_bitrate_mbps: int
    balanced_merge_quality: int
    runs_per_cell: int
    eye_bitrates_mbps: tuple[int, ...]
    merge_qualities: tuple[int, ...]
    cells: tuple[ExperimentCell, ...]
    ffmpeg_manifest_path: Path
    ffmpeg_manifest_sha256: str
    generated_encoder_contract: str
    metric_contract: str
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


def _sha256_identity(value: object, label: str) -> str:
    digest = _string(value, label)
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise QualificationFailure(f"{label} must be a lowercase SHA-256 identity.")
    return digest


def _integer(value: object, label: str, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise QualificationFailure(f"{label} must be an integer between {minimum} and {maximum}.")
    return value


def _integer_axis(value: object, label: str, *, minimum: int, maximum: int) -> tuple[int, ...]:
    raw_values = _array(value, label)
    values = tuple(
        _integer(raw_value, f"{label}[{index}]", minimum=minimum, maximum=maximum)
        for index, raw_value in enumerate(raw_values)
    )
    if len(values) != 3:
        raise QualificationFailure(f"{label} must contain exactly three levels for the checked 3x3 design.")
    if tuple(sorted(values)) != values or len(set(values)) != len(values):
        raise QualificationFailure(f"{label} must be unique and strictly increasing.")
    return values


def _repository_path(relative_path: str, label: str) -> Path:
    path = (REPOSITORY_ROOT / relative_path).resolve()
    try:
        path.relative_to(REPOSITORY_ROOT)
    except ValueError as error:
        raise QualificationFailure(f"{label} escapes the repository: {relative_path}") from error
    return path


def _relative_repository_path(path: Path, label: str) -> str:
    try:
        return path.resolve().relative_to(REPOSITORY_ROOT).as_posix()
    except ValueError as error:
        raise QualificationFailure(f"{label} must be committed inside the repository.") from error


def parse_corpus_binding(raw: object) -> CorpusBinding:
    document = _mapping(raw, "corpus binding")
    if document.get("schema_version") != 1:
        raise QualificationFailure("corpus binding schema_version must be 1.")
    binding_id = _string(document.get("binding_id"), "binding_id")
    if document.get("purpose") != "matched_stress_subset_not_full_corpus":
        raise QualificationFailure("corpus binding must identify a matched stress subset rather than a full corpus.")
    source_manifest = _mapping(document.get("source_manifest"), "source_manifest")
    source_manifest_path = _repository_path(
        _string(source_manifest.get("path"), "source_manifest.path"),
        "Corpus source manifest path",
    )
    source_corpus_id = _string(source_manifest.get("corpus_id"), "source_manifest.corpus_id")
    source_manifest_sha256 = _sha256_identity(source_manifest.get("sha256"), "source_manifest.sha256")
    selected_case_ids = tuple(
        _string(case_id, f"selected_case_ids[{index}]")
        for index, case_id in enumerate(_array(document.get("selected_case_ids"), "selected_case_ids"))
    )
    if not selected_case_ids or len(set(selected_case_ids)) != len(selected_case_ids):
        raise QualificationFailure("selected_case_ids must be non-empty and unique.")
    expected_case_sources_document = _mapping(document.get("expected_case_sources"), "expected_case_sources")
    if set(expected_case_sources_document) != set(selected_case_ids):
        raise QualificationFailure("expected_case_sources must identify every selected case exactly once.")
    expected_case_sources = {
        case_id: dict(_mapping(expected_case_sources_document[case_id], f"expected_case_sources.{case_id}"))
        for case_id in selected_case_ids
    }
    required_coverage = tuple(
        _string(tag, f"required_coverage[{index}]")
        for index, tag in enumerate(_array(document.get("required_coverage"), "required_coverage"))
    )
    if not required_coverage or len(set(required_coverage)) != len(required_coverage):
        raise QualificationFailure("required_coverage must be non-empty and unique.")
    return CorpusBinding(
        binding_id=binding_id,
        source_manifest_path=source_manifest_path,
        source_corpus_id=source_corpus_id,
        source_manifest_sha256=source_manifest_sha256,
        selected_case_ids=selected_case_ids,
        expected_case_sources=expected_case_sources,
        required_coverage=required_coverage,
    )


def load_corpus_binding(path: Path) -> tuple[CorpusBinding, str]:
    resolved_path = path.resolve()
    relative_path = _relative_repository_path(resolved_path, "Generated calibration corpus binding")
    try:
        raw = json.loads(resolved_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise QualificationFailure(
            f"Could not read generated calibration corpus binding {path.name}: {error}"
        ) from error
    parsed = parse_corpus_binding(raw)
    if not parsed.source_manifest_path.is_file():
        raise QualificationFailure("The bound source corpus manifest is unavailable.")
    observed_sha256 = sha256_file(parsed.source_manifest_path)
    if observed_sha256 != parsed.source_manifest_sha256:
        raise QualificationFailure("The bound source corpus manifest does not match its pinned SHA-256 identity.")
    manifest = load_manifest(parsed.source_manifest_path)
    if manifest.corpus_id != parsed.source_corpus_id:
        raise QualificationFailure("The bound source corpus ID does not match the referenced manifest.")
    gated_cases = {case.case_id: case for case in manifest.cases if case.quality_gate}
    unknown = sorted(set(parsed.selected_case_ids) - set(gated_cases))
    if unknown:
        raise QualificationFailure("Corpus binding references unknown or non-gating cases: " + ", ".join(unknown))
    observed_coverage = {tag for case_id in parsed.selected_case_ids for tag in gated_cases[case_id].tags}
    missing_coverage = sorted(set(parsed.required_coverage) - observed_coverage)
    if missing_coverage:
        raise QualificationFailure("Corpus binding is missing required coverage: " + ", ".join(missing_coverage))
    binding = CorpusBinding(
        binding_id=parsed.binding_id,
        source_manifest_path=parsed.source_manifest_path,
        source_corpus_id=parsed.source_corpus_id,
        source_manifest_sha256=parsed.source_manifest_sha256,
        selected_case_ids=parsed.selected_case_ids,
        expected_case_sources=parsed.expected_case_sources,
        required_coverage=parsed.required_coverage,
        relative_path=relative_path,
    )
    return binding, sha256_file(resolved_path)


def _cell_id(eye_bitrate_mbps: int, merge_quality: int) -> str:
    return f"b{eye_bitrate_mbps:03d}-m{merge_quality:03d}"


def parse_experiment_plan(raw: object) -> ExperimentPlan:
    document = _mapping(raw, "experiment plan")
    if document.get("schema_version") != 1:
        raise QualificationFailure("experiment plan schema_version must be 1.")
    experiment_id = _string(document.get("experiment_id"), "experiment_id")
    if document.get("target_id") != "generated_mv_hevc":
        raise QualificationFailure("experiment plan target_id must be 'generated_mv_hevc'.")
    if document.get("purpose") != "joint_interaction_characterization_not_ladder_mappings":
        raise QualificationFailure("experiment plan must characterize interaction without assigning ladder mappings.")
    if document.get("design") != "full_factorial_3x3":
        raise QualificationFailure("experiment plan design must be 'full_factorial_3x3'.")

    corpus_binding = _mapping(document.get("corpus_binding"), "corpus_binding")
    binding_path = _repository_path(
        _string(corpus_binding.get("path"), "corpus_binding.path"),
        "Corpus binding path",
    )
    binding_id = _string(corpus_binding.get("binding_id"), "corpus_binding.binding_id")
    binding_sha256 = _sha256_identity(corpus_binding.get("sha256"), "corpus_binding.sha256")

    balanced = _mapping(document.get("balanced"), "balanced")
    balanced_eye_bitrate_mbps = _integer(
        balanced.get("eye_bitrate_mbps"),
        "balanced.eye_bitrate_mbps",
        minimum=1,
        maximum=500,
    )
    balanced_merge_quality = _integer(
        balanced.get("merge_quality"),
        "balanced.merge_quality",
        minimum=0,
        maximum=100,
    )
    if balanced_eye_bitrate_mbps != AUTOMATIC_GENERATED_EYE_BITRATE_MBPS:
        raise QualificationFailure("Balanced eye bitrate must match the production generated default exactly.")
    if balanced_merge_quality != AUTOMATIC_GENERATED_MERGE_QUALITY:
        raise QualificationFailure("Balanced merge quality must match the production generated default exactly.")
    if (
        balanced.get("eye_bitrate_source")
        != "bd_to_avp.modules.video_quality_defaults.AUTOMATIC_GENERATED_EYE_BITRATE_MBPS"
    ):
        raise QualificationFailure("Balanced eye bitrate source must identify the production generated default.")
    if (
        balanced.get("merge_quality_source")
        != "bd_to_avp.modules.video_quality_defaults.AUTOMATIC_GENERATED_MERGE_QUALITY"
    ):
        raise QualificationFailure("Balanced merge quality source must identify the production generated default.")

    runs_per_cell = document.get("runs_per_cell")
    if runs_per_cell != 3:
        raise QualificationFailure("runs_per_cell must be 3 for the checked cyclic ordering design.")
    if document.get("execution_order") != "cyclic_thirds":
        raise QualificationFailure("execution_order must be 'cyclic_thirds'.")
    axes = _mapping(document.get("axes"), "axes")
    eye_bitrates_mbps = _integer_axis(
        axes.get("eye_bitrate_mbps"),
        "axes.eye_bitrate_mbps",
        minimum=1,
        maximum=500,
    )
    merge_qualities = _integer_axis(
        axes.get("merge_quality"),
        "axes.merge_quality",
        minimum=0,
        maximum=100,
    )
    if eye_bitrates_mbps[1] != balanced_eye_bitrate_mbps:
        raise QualificationFailure("Balanced eye bitrate must be the center level of the checked design.")
    if merge_qualities[1] != balanced_merge_quality:
        raise QualificationFailure("Balanced merge quality must be the center level of the checked design.")

    toolchain = _mapping(document.get("toolchain"), "toolchain")
    ffmpeg_manifest = _mapping(toolchain.get("ffmpeg_manifest"), "toolchain.ffmpeg_manifest")
    ffmpeg_manifest_path = _repository_path(
        _string(ffmpeg_manifest.get("path"), "toolchain.ffmpeg_manifest.path"),
        "FFmpeg vendor manifest path",
    )
    ffmpeg_manifest_sha256 = _sha256_identity(
        ffmpeg_manifest.get("sha256"),
        "toolchain.ffmpeg_manifest.sha256",
    )
    generated_encoder_contract = _string(
        toolchain.get("generated_encoder_contract"),
        "toolchain.generated_encoder_contract",
    )
    if generated_encoder_contract != "production_generated_mv_hevc_v1":
        raise QualificationFailure("toolchain generated encoder contract is unsupported.")
    metric_contract = _string(toolchain.get("metric_contract"), "toolchain.metric_contract")
    if metric_contract != "ffmpeg_ssim_aggregate_and_per_frame_v1":
        raise QualificationFailure("toolchain metric contract is unsupported.")

    decision_policy = _mapping(document.get("decision_policy"), "decision_policy")
    if decision_policy.get("stage") != "interaction_characterization_only":
        raise QualificationFailure("decision_policy.stage must remain interaction_characterization_only.")
    if decision_policy.get("post_hoc_thresholds_forbidden") is not True:
        raise QualificationFailure("decision policy must forbid post-hoc thresholds.")
    if decision_policy.get("ladder_mapping_selected") is not False:
        raise QualificationFailure("interaction experiment must not select a ladder mapping.")

    cells = tuple(
        ExperimentCell(_cell_id(eye_bitrate, merge_quality), eye_bitrate, merge_quality)
        for eye_bitrate, merge_quality in product(eye_bitrates_mbps, merge_qualities)
    )
    return ExperimentPlan(
        experiment_id=experiment_id,
        binding_path=binding_path,
        binding_id=binding_id,
        binding_sha256=binding_sha256,
        balanced_eye_bitrate_mbps=balanced_eye_bitrate_mbps,
        balanced_merge_quality=balanced_merge_quality,
        runs_per_cell=runs_per_cell,
        eye_bitrates_mbps=eye_bitrates_mbps,
        merge_qualities=merge_qualities,
        cells=cells,
        ffmpeg_manifest_path=ffmpeg_manifest_path,
        ffmpeg_manifest_sha256=ffmpeg_manifest_sha256,
        generated_encoder_contract=generated_encoder_contract,
        metric_contract=metric_contract,
    )


def load_experiment_plan(path: Path) -> tuple[ExperimentPlan, CorpusBinding, str, str]:
    resolved_path = path.resolve()
    relative_path = _relative_repository_path(resolved_path, "Generated calibration experiment plan")
    try:
        raw = json.loads(resolved_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise QualificationFailure(
            f"Could not read generated calibration experiment plan {path.name}: {error}"
        ) from error
    parsed = parse_experiment_plan(raw)
    if not parsed.binding_path.is_file():
        raise QualificationFailure("The generated calibration corpus binding is unavailable.")
    binding, binding_sha256 = load_corpus_binding(parsed.binding_path)
    if binding_sha256 != parsed.binding_sha256:
        raise QualificationFailure("The corpus binding does not match its pinned SHA-256 identity.")
    if binding.binding_id != parsed.binding_id:
        raise QualificationFailure("The corpus binding ID does not match the experiment plan.")
    if not parsed.ffmpeg_manifest_path.is_file():
        raise QualificationFailure("The pinned FFmpeg vendor manifest is unavailable.")
    if sha256_file(parsed.ffmpeg_manifest_path) != parsed.ffmpeg_manifest_sha256:
        raise QualificationFailure("The FFmpeg vendor manifest does not match its pinned SHA-256 identity.")
    plan = ExperimentPlan(
        experiment_id=parsed.experiment_id,
        binding_path=parsed.binding_path,
        binding_id=parsed.binding_id,
        binding_sha256=parsed.binding_sha256,
        balanced_eye_bitrate_mbps=parsed.balanced_eye_bitrate_mbps,
        balanced_merge_quality=parsed.balanced_merge_quality,
        runs_per_cell=parsed.runs_per_cell,
        eye_bitrates_mbps=parsed.eye_bitrates_mbps,
        merge_qualities=parsed.merge_qualities,
        cells=parsed.cells,
        ffmpeg_manifest_path=parsed.ffmpeg_manifest_path,
        ffmpeg_manifest_sha256=parsed.ffmpeg_manifest_sha256,
        generated_encoder_contract=parsed.generated_encoder_contract,
        metric_contract=parsed.metric_contract,
        relative_path=relative_path,
    )
    return plan, binding, sha256_file(resolved_path), binding_sha256


def _require_head_tracked_file(path: Path, label: str) -> str:
    relative_path = _relative_repository_path(path, label)
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
        working = path.resolve().read_bytes()
    except (OSError, subprocess.CalledProcessError) as error:
        raise QualificationFailure(f"Could not verify committed {label} bytes.") from error
    if committed != working:
        raise QualificationFailure(f"{label} bytes must match the recorded source commit exactly.")
    return relative_path


def _git_head_from_clean_worktree() -> str:
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=normal"],
        cwd=REPOSITORY_ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()
    if status:
        raise QualificationFailure("Generated calibration evidence requires a clean source worktree.")
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPOSITORY_ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()


def _vendor_binary_hashes(plan: ExperimentPlan) -> dict[str, str]:
    try:
        document = tomllib.loads(plan.ffmpeg_manifest_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise QualificationFailure(f"Could not read the pinned FFmpeg vendor manifest: {error}") from error
    assets = document.get("assets")
    if not isinstance(assets, list):
        raise QualificationFailure("Pinned FFmpeg vendor manifest assets are invalid.")
    hashes: dict[str, str] = {}
    for asset in assets:
        if not isinstance(asset, Mapping):
            raise QualificationFailure("Pinned FFmpeg vendor manifest contains an invalid asset.")
        name = asset.get("name")
        digest = asset.get("binary_sha256")
        if isinstance(name, str):
            hashes[name] = _sha256_identity(digest, f"FFmpeg vendor asset {name}")
    if set(hashes) != {"ffmpeg", "ffprobe"}:
        raise QualificationFailure("Pinned FFmpeg vendor manifest must identify ffmpeg and ffprobe exactly.")
    return hashes


def _pinned_media_tool(plan: ExperimentPlan, name: str) -> str:
    expected_hash = _vendor_binary_hashes(plan)[name]
    path = REPOSITORY_ROOT / "bd_to_avp/bin" / name
    if not path.is_file() or not os.access(path, os.X_OK):
        raise QualificationFailure(
            f"Pinned {name} is unavailable; run 'uv run python -m scripts.vendor_ffmpeg_macos' first."
        )
    if sha256_file(path) != expected_hash:
        raise QualificationFailure(f"Pinned {name} does not match the checked vendor manifest.")
    return path.as_posix()


def _sysctl_value(name: str) -> str:
    try:
        return subprocess.run(
            ["sysctl", "-n", name],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout.strip()
    except subprocess.CalledProcessError as error:
        raise QualificationFailure(f"Could not read hardware identity {name}.") from error


def _environment_evidence(
    plan: ExperimentPlan,
    ffmpeg: str,
    ffprobe: str,
    source_git_sha: str,
) -> dict[str, object]:
    return {
        "cpu_brand": _sysctl_value("machdep.cpu.brand_string"),
        "edge264_sha256": sha256_file(EDGE264),
        "ffmpeg": _tool_version([ffmpeg, "-hide_banner", "-version"]),
        "ffmpeg_sha256": sha256_file(Path(ffmpeg)),
        "ffmpeg_vendor_manifest_sha256": plan.ffmpeg_manifest_sha256,
        "ffprobe": _tool_version([ffprobe, "-hide_banner", "-version"]),
        "ffprobe_sha256": sha256_file(Path(ffprobe)),
        "generated_encoder_contract": plan.generated_encoder_contract,
        "git_head": source_git_sha,
        "hardware_model": _sysctl_value("hw.model"),
        "machine": platform.machine(),
        "macos_build": _sysctl_value("kern.osversion"),
        "macos_version": platform.mac_ver()[0],
        "metric_contract": plan.metric_contract,
        "mp4box_sha256": sha256_file(MP4BOX),
        "platform": platform.system(),
        "spatial_media_tool_sha256": sha256_file(SPATIAL_MEDIA_TOOL),
    }


def _method_record(plan: ExperimentPlan) -> dict[str, object]:
    return {
        "design": "full_factorial_3x3",
        "runs_per_cell": plan.runs_per_cell,
        "balanced": {
            "eye_bitrate_mbps": plan.balanced_eye_bitrate_mbps,
            "merge_quality": plan.balanced_merge_quality,
        },
        "cell_order": "cyclic_thirds",
        "quality_metric": "aggregate and per-frame decoded same-eye SSIM",
        "interaction_metric": "adjacent 2x2 difference of merge-quality effects",
        "post_hoc_thresholds_forbidden": True,
        "decision_stage": "interaction_characterization_only",
    }


def _string_values(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for nested in value.values():
            yield from _string_values(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            yield from _string_values(nested)


def _assert_private_values_absent(document: object, private_paths: Sequence[Path]) -> None:
    forbidden = {path.as_posix().casefold() for path in private_paths}
    forbidden.update(
        value.casefold() for path in private_paths for value in (path.name, path.stem) if len(value.strip()) >= 12
    )
    for value in _string_values(document):
        folded = value.casefold()
        if (
            value.startswith("/")
            or "file://" in folded
            or "/users/" in folded
            or "/volumes/" in folded
            or any(token and token in folded for token in forbidden)
        ):
            raise QualificationFailure("Generated calibration evidence leaked private source information.")


def _private_source_paths(cases: Sequence[CorpusCase]) -> tuple[Path, ...]:
    paths: list[Path] = []
    for case in cases:
        path_env = case.source.get("path_env")
        if isinstance(path_env, str) and os.environ.get(path_env):
            paths.append(Path(os.environ[path_env]).expanduser().resolve())
    return tuple(paths)


def _atomic_write(path: Path, document: Mapping[str, object], private_paths: Sequence[Path]) -> None:
    if path.is_symlink():
        raise QualificationFailure("Generated calibration receipt must not be a symlink.")
    _assert_private_values_absent(document, private_paths)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    if temporary_path.is_symlink():
        raise QualificationFailure("Generated calibration temporary receipt must not be a symlink.")
    temporary_path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary_path.chmod(0o644)
    temporary_path.replace(path)


def _freeze_receipt(path: Path) -> None:
    path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)


def _prepare_owned_work_directory(work_directory: Path, plan: ExperimentPlan, plan_sha256: str) -> Path:
    resolved = work_directory.resolve()
    dangerous = {Path("/").resolve(), Path.home().resolve(), REPOSITORY_ROOT}
    if resolved in dangerous:
        raise QualificationFailure("Calibration work directory must be a dedicated non-root directory.")
    marker = resolved / WORK_DIRECTORY_MARKER
    expected_marker = {
        "schema_version": 1,
        "experiment_id": plan.experiment_id,
        "experiment_plan_sha256": plan_sha256,
    }
    if resolved.exists():
        if marker.is_file():
            try:
                observed = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise QualificationFailure(
                    f"Could not validate calibration work-directory ownership: {error}"
                ) from error
            if observed != expected_marker:
                raise QualificationFailure("Calibration work directory belongs to a different experiment identity.")
        elif any(resolved.iterdir()):
            raise QualificationFailure("Calibration work directory is non-empty and has no ownership marker.")
        else:
            _atomic_write(marker, expected_marker, ())
    else:
        resolved.mkdir(parents=True)
        _atomic_write(marker, expected_marker, ())
    return resolved


def _owned_case_directory(work_directory: Path, case_id: str) -> Path:
    relative_case = Path(case_id)
    if (
        not case_id
        or relative_case.is_absolute()
        or len(relative_case.parts) != 1
        or relative_case.name in {"", ".", ".."}
    ):
        raise QualificationFailure(f"Unsafe corpus case work path: {case_id}")
    case_directory = work_directory.resolve() / relative_case
    if case_directory.is_symlink():
        raise QualificationFailure(f"Corpus case work path must not be a symlink: {case_id}")
    if case_directory.exists() and not case_directory.is_dir():
        raise QualificationFailure(f"Corpus case work path must be a directory: {case_id}")
    if not (work_directory / WORK_DIRECTORY_MARKER).is_file():
        raise QualificationFailure("Calibration work-directory ownership marker is missing.")
    return case_directory


def _reset_case_directory(work_directory: Path, case_id: str) -> Path:
    case_directory = _owned_case_directory(work_directory, case_id)
    if case_directory.exists():
        shutil.rmtree(case_directory)
    case_directory.mkdir(parents=True)
    return case_directory


@contextmanager
def calibration_lock(output_path: Path, work_directory: Path) -> Iterator[None]:
    absolute_output_path = output_path.absolute()
    absolute_work_directory = work_directory.absolute()
    dangerous = {Path("/").resolve(), Path.home().resolve(), REPOSITORY_ROOT}
    if output_path.is_symlink():
        raise QualificationFailure("Calibration output is unsafe for locking.")
    if work_directory.is_symlink() or absolute_work_directory.resolve() in dangerous:
        raise QualificationFailure("Calibration work directory is unsafe for locking.")
    lock_paths = sorted(
        {
            absolute_output_path.parent / f".{absolute_output_path.name}{LOCK_SUFFIX}",
            absolute_work_directory.parent / f".{absolute_work_directory.name}{LOCK_SUFFIX}",
        },
        key=lambda path: path.as_posix(),
    )
    lock_files = []
    try:
        for lock_path in lock_paths:
            if lock_path.is_symlink():
                raise QualificationFailure("Calibration lock must not be a symlink.")
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_file = lock_path.open("a+", encoding="utf-8")
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                lock_file.close()
                raise QualificationFailure("Another generated MV-HEVC calibration is already running.") from error
            lock_files.append(lock_file)
        yield
    finally:
        for lock_file in reversed(lock_files):
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()


def _cell_record(case: Mapping[str, object], cell_id: str) -> dict[str, object] | None:
    cells = case.get("cells")
    if not isinstance(cells, list):
        return None
    for cell in cells:
        if isinstance(cell, dict) and cell.get("id") == cell_id:
            return cell
    return None


def _case_record(evidence: Mapping[str, object], case_id: str) -> dict[str, object] | None:
    cases = evidence.get("cases")
    if not isinstance(cases, list):
        return None
    for case in cases:
        if isinstance(case, dict) and case.get("id") == case_id:
            return case
    return None


def _run_for_index(cell: Mapping[str, object], run_index: int) -> Mapping[str, object] | None:
    runs = cell.get("runs")
    if not isinstance(runs, list):
        return None
    matches = [run for run in runs if isinstance(run, Mapping) and run.get("run_index") == run_index]
    if len(matches) > 1:
        raise QualificationFailure(f"Cell {cell.get('id')} contains duplicate run index {run_index}.")
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


def _validate_run_record(run: Mapping[str, object], cell: ExperimentCell, run_index: int) -> None:
    expected_keys = {
        "codec_name",
        "codec_tag_string",
        "duration_seconds",
        "effective_bitrate_mbps",
        "elapsed_seconds",
        "eye_bitrate_mbps",
        "final_bytes",
        "frame_count",
        "frame_quality_sample_count",
        "frame_ssim_standard_deviation",
        "left_cross_ssim",
        "left_match_ssim",
        "merge_quality",
        "maximum_adjacent_frame_ssim_drop",
        "median_frame_same_eye_ssim",
        "min_eye_order_margin",
        "min_same_eye_ssim",
        "minimum_frame_same_eye_ssim",
        "observed_box_types",
        "p05_frame_same_eye_ssim",
        "right_cross_ssim",
        "right_match_ssim",
        "run_index",
        "sha256",
        "system_cpu_seconds",
        "target_total_eye_bitrate_mbps",
        "user_cpu_seconds",
    }
    if set(run) != expected_keys:
        raise QualificationFailure(f"Cell {cell.cell_id} run {run_index} has an invalid record shape.")
    if (
        run.get("run_index") != run_index
        or run.get("eye_bitrate_mbps") != cell.eye_bitrate_mbps
        or run.get("merge_quality") != cell.merge_quality
        or run.get("target_total_eye_bitrate_mbps") != cell.eye_bitrate_mbps * 2
    ):
        raise QualificationFailure(f"Cell {cell.cell_id} run {run_index} has mismatched identity.")
    if type(run.get("final_bytes")) is not int or int(run["final_bytes"]) <= 0:
        raise QualificationFailure(f"Cell {cell.cell_id} run {run_index} final_bytes must be positive.")
    if type(run.get("frame_count")) is not int or int(run["frame_count"]) <= 0:
        raise QualificationFailure(f"Cell {cell.cell_id} run {run_index} frame_count must be positive.")
    if type(run.get("frame_quality_sample_count")) is not int or int(run["frame_quality_sample_count"]) <= 0:
        raise QualificationFailure(f"Cell {cell.cell_id} run {run_index} frame quality count must be positive.")
    if run.get("codec_name") != "hevc" or run.get("codec_tag_string") != "hvc1":
        raise QualificationFailure(f"Cell {cell.cell_id} run {run_index} is not an hvc1 HEVC stream.")
    _finite_number(run.get("effective_bitrate_mbps"), "effective_bitrate_mbps", positive=True)
    _finite_number(run.get("duration_seconds"), "duration_seconds", positive=True)
    ssim_values: dict[str, float] = {}
    for key in ("left_cross_ssim", "left_match_ssim", "min_same_eye_ssim", "right_cross_ssim", "right_match_ssim"):
        value = _finite_number(run.get(key), key)
        if not 0 <= value <= 1:
            raise QualificationFailure(f"Cell {cell.cell_id} run {run_index} {key} must be 0..1.")
        ssim_values[key] = value
    eye_order_margin = _finite_number(run.get("min_eye_order_margin"), "min_eye_order_margin")
    expected_same_eye = min(ssim_values["left_match_ssim"], ssim_values["right_match_ssim"])
    expected_eye_order_margin = min(
        ssim_values["left_match_ssim"] - ssim_values["left_cross_ssim"],
        ssim_values["right_match_ssim"] - ssim_values["right_cross_ssim"],
    )
    if not math.isclose(ssim_values["min_same_eye_ssim"], expected_same_eye, rel_tol=0.0, abs_tol=1e-12):
        raise QualificationFailure(f"Cell {cell.cell_id} run {run_index} min_same_eye_ssim contradicts eye metrics.")
    if not math.isclose(eye_order_margin, expected_eye_order_margin, rel_tol=0.0, abs_tol=1e-12):
        raise QualificationFailure(f"Cell {cell.cell_id} run {run_index} min_eye_order_margin contradicts eye metrics.")
    for key in (
        "minimum_frame_same_eye_ssim",
        "p05_frame_same_eye_ssim",
        "median_frame_same_eye_ssim",
        "maximum_adjacent_frame_ssim_drop",
    ):
        value = _finite_number(run.get(key), key)
        if not 0 <= value <= 1:
            raise QualificationFailure(f"Cell {cell.cell_id} run {run_index} {key} must be 0..1.")
    _finite_number(run.get("frame_ssim_standard_deviation"), "frame_ssim_standard_deviation", nonnegative=True)
    for key in ("elapsed_seconds", "user_cpu_seconds", "system_cpu_seconds"):
        _finite_number(run.get(key), key, positive=key == "elapsed_seconds", nonnegative=key != "elapsed_seconds")
    digest = run.get("sha256")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise QualificationFailure(f"Cell {cell.cell_id} run {run_index} has an invalid SHA-256.")
    observed_box_types = run.get("observed_box_types")
    if not isinstance(observed_box_types, list) or any(
        not isinstance(box_type, str) for box_type in observed_box_types
    ):
        raise QualificationFailure(f"Cell {cell.cell_id} run {run_index} observed_box_types are invalid.")
    if not CURRENT_REQUIRED_BOX_TYPES.issubset(set(observed_box_types)):
        raise QualificationFailure(f"Cell {cell.cell_id} run {run_index} is missing required spatial metadata.")


def _run_complete(cell_record: Mapping[str, object], cell: ExperimentCell, run_index: int) -> bool:
    run = _run_for_index(cell_record, run_index)
    if run is None:
        return False
    _validate_run_record(run, cell, run_index)
    return True


def _cell_order(plan: ExperimentPlan, run_index: int) -> tuple[ExperimentCell, ...]:
    shift = (run_index % 3) * (len(plan.cells) // 3)
    return plan.cells[shift:] + plan.cells[:shift]


def _summarize_measurement_runs(runs: Sequence[Mapping[str, object]]) -> dict[str, object]:
    summary = summarize_runs(runs)
    summary.update(
        {
            "median_left_cross_ssim": statistics.median(float(run["left_cross_ssim"]) for run in runs),
            "median_left_match_ssim": statistics.median(float(run["left_match_ssim"]) for run in runs),
            "median_right_cross_ssim": statistics.median(float(run["right_cross_ssim"]) for run in runs),
            "median_right_match_ssim": statistics.median(float(run["right_match_ssim"]) for run in runs),
            "minimum_frame_same_eye_ssim": min(float(run["minimum_frame_same_eye_ssim"]) for run in runs),
            "median_p05_frame_same_eye_ssim": statistics.median(float(run["p05_frame_same_eye_ssim"]) for run in runs),
            "maximum_frame_ssim_standard_deviation": max(float(run["frame_ssim_standard_deviation"]) for run in runs),
            "maximum_adjacent_frame_ssim_drop": max(float(run["maximum_adjacent_frame_ssim_drop"]) for run in runs),
        }
    )
    return summary


def _balanced_cell(plan: ExperimentPlan) -> ExperimentCell:
    return next(
        cell
        for cell in plan.cells
        if cell.eye_bitrate_mbps == plan.balanced_eye_bitrate_mbps and cell.merge_quality == plan.balanced_merge_quality
    )


def _axis_findings(case: Mapping[str, object], plan: ExperimentPlan) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    axis_groups = [
        (
            "eye_bitrate_mbps",
            "merge_quality",
            plan.merge_qualities,
            plan.eye_bitrates_mbps,
        ),
        (
            "merge_quality",
            "eye_bitrate_mbps",
            plan.eye_bitrates_mbps,
            plan.merge_qualities,
        ),
    ]
    for axis, fixed_control, fixed_values, ordered_values in axis_groups:
        for fixed_value in fixed_values:
            records: list[Mapping[str, object]] = []
            for ordered_value in ordered_values:
                bitrate = ordered_value if axis == "eye_bitrate_mbps" else fixed_value
                merge_quality = fixed_value if axis == "eye_bitrate_mbps" else ordered_value
                record = _cell_record(case, _cell_id(bitrate, merge_quality))
                if record is None or not isinstance(record.get("summary"), Mapping):
                    records = []
                    break
                records.append(record)
            for lower, higher in pairwise(records):
                lower_summary = lower["summary"]
                higher_summary = higher["summary"]
                assert isinstance(lower_summary, Mapping)
                assert isinstance(higher_summary, Mapping)
                base = {
                    "case_id": case["id"],
                    "axis": axis,
                    "fixed_control": fixed_control,
                    "fixed_value": fixed_value,
                    "lower_cell": lower["id"],
                    "higher_cell": higher["id"],
                }
                quality_separation = float(higher_summary["median_min_same_eye_ssim"]) - float(
                    lower_summary["median_min_same_eye_ssim"]
                )
                quality_noise = max(
                    float(lower_summary["repeat_ssim_spread"]),
                    float(higher_summary["repeat_ssim_spread"]),
                )
                if quality_separation < 0:
                    findings.append({"code": "quality_reversal", **base})
                elif quality_separation <= quality_noise:
                    findings.append(
                        {
                            "code": "quality_not_distinct_from_repeat_noise",
                            **base,
                            "observed_separation": quality_separation,
                            "repeat_noise": quality_noise,
                        }
                    )
                size_separation = float(higher_summary["output_size_ratio"]) - float(lower_summary["output_size_ratio"])
                size_noise = max(
                    float(lower_summary["repeat_size_ratio_spread_vs_balanced"]),
                    float(higher_summary["repeat_size_ratio_spread_vs_balanced"]),
                )
                if size_separation < 0:
                    findings.append({"code": "storage_reversal", **base})
                elif size_separation <= size_noise:
                    findings.append(
                        {
                            "code": "storage_not_distinct_from_repeat_noise",
                            **base,
                            "observed_separation": size_separation,
                            "repeat_noise": size_noise,
                        }
                    )
    return findings


def _interaction_observations(case: Mapping[str, object], plan: ExperimentPlan) -> list[dict[str, object]]:
    observations: list[dict[str, object]] = []
    for low_bitrate, high_bitrate in pairwise(plan.eye_bitrates_mbps):
        for low_merge, high_merge in pairwise(plan.merge_qualities):
            records = {
                "low_low": _cell_record(case, _cell_id(low_bitrate, low_merge)),
                "low_high": _cell_record(case, _cell_id(low_bitrate, high_merge)),
                "high_low": _cell_record(case, _cell_id(high_bitrate, low_merge)),
                "high_high": _cell_record(case, _cell_id(high_bitrate, high_merge)),
            }
            if any(record is None or not isinstance(record.get("summary"), Mapping) for record in records.values()):
                continue
            summaries = {key: record["summary"] for key, record in records.items()}
            merge_effect_low_bitrate = float(summaries["low_high"]["median_min_same_eye_ssim"]) - float(
                summaries["low_low"]["median_min_same_eye_ssim"]
            )
            merge_effect_high_bitrate = float(summaries["high_high"]["median_min_same_eye_ssim"]) - float(
                summaries["high_low"]["median_min_same_eye_ssim"]
            )
            size_effect_low_bitrate = float(summaries["low_high"]["output_size_ratio"]) - float(
                summaries["low_low"]["output_size_ratio"]
            )
            size_effect_high_bitrate = float(summaries["high_high"]["output_size_ratio"]) - float(
                summaries["high_low"]["output_size_ratio"]
            )
            observations.append(
                {
                    "case_id": case["id"],
                    "bitrate_pair_mbps": [low_bitrate, high_bitrate],
                    "merge_quality_pair": [low_merge, high_merge],
                    "quality_interaction": merge_effect_high_bitrate - merge_effect_low_bitrate,
                    "output_size_ratio_interaction": size_effect_high_bitrate - size_effect_low_bitrate,
                    "merge_quality_effect_at_low_bitrate": merge_effect_low_bitrate,
                    "merge_quality_effect_at_high_bitrate": merge_effect_high_bitrate,
                    "maximum_repeat_ssim_spread": max(
                        float(summary["repeat_ssim_spread"]) for summary in summaries.values()
                    ),
                }
            )
    return observations


def _refresh_summaries(
    evidence: dict[str, object],
    plan: ExperimentPlan,
    binding: CorpusBinding,
    case_definitions: Mapping[str, CorpusCase],
) -> None:
    cases = evidence["cases"]
    assert isinstance(cases, list)
    balanced = _balanced_cell(plan)
    baseline_cases: list[dict[str, object]] = []
    for case in cases:
        assert isinstance(case, dict)
        cells = case["cells"]
        assert isinstance(cells, list)
        for cell in cells:
            assert isinstance(cell, dict)
            runs = cell["runs"]
            assert isinstance(runs, list)
            cell["summary"] = _summarize_measurement_runs(runs) if len(runs) == plan.runs_per_cell else None
        balanced_record = _cell_record(case, balanced.cell_id)
        balanced_summary = balanced_record.get("summary") if balanced_record is not None else None
        if not isinstance(balanced_summary, Mapping):
            continue
        baseline_cases.append(
            {
                "case_id": case["id"],
                "median_min_same_eye_ssim": balanced_summary["median_min_same_eye_ssim"],
                "median_final_bytes": balanced_summary["median_final_bytes"],
                "median_elapsed_seconds": balanced_summary["median_elapsed_seconds"],
                "repeat_ssim_spread": balanced_summary["repeat_ssim_spread"],
                "repeat_size_ratio_spread": balanced_summary["repeat_size_ratio_spread"],
            }
        )
        for cell in cells:
            assert isinstance(cell, dict)
            summary = cell.get("summary")
            if not isinstance(summary, dict):
                continue
            if cell["id"] == balanced.cell_id:
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
            summary["repeat_size_ratio_spread_vs_balanced"] = (
                float(summary["maximum_final_bytes"]) - float(summary["minimum_final_bytes"])
            ) / float(balanced_summary["median_final_bytes"])

    cell_summaries: list[dict[str, object]] = []
    for cell in plan.cells:
        summaries: list[Mapping[str, object]] = []
        for case in cases:
            assert isinstance(case, Mapping)
            record = _cell_record(case, cell.cell_id)
            summary = record.get("summary") if record is not None else None
            if isinstance(summary, Mapping) and "quality_delta" in summary:
                summaries.append(summary)
        complete = len(summaries) == len(case_definitions)
        aggregate: dict[str, object] = {
            "id": cell.cell_id,
            "eye_bitrate_mbps": cell.eye_bitrate_mbps,
            "merge_quality": cell.merge_quality,
            "case_count": len(summaries),
            "complete": complete,
        }
        if summaries:
            aggregate.update(
                {
                    "worst_case_quality_delta": 0.0
                    if cell.cell_id == balanced.cell_id
                    else min(float(summary["quality_delta"]) for summary in summaries),
                    "median_quality_delta": statistics.median(float(summary["quality_delta"]) for summary in summaries),
                    "worst_case_output_size_ratio": 1.0
                    if cell.cell_id == balanced.cell_id
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
                        float(summary["repeat_size_ratio_spread_vs_balanced"]) for summary in summaries
                    ),
                    "minimum_frame_same_eye_ssim": min(
                        float(summary["minimum_frame_same_eye_ssim"]) for summary in summaries
                    ),
                    "minimum_median_p05_frame_same_eye_ssim": min(
                        float(summary["median_p05_frame_same_eye_ssim"]) for summary in summaries
                    ),
                    "maximum_frame_ssim_standard_deviation": max(
                        float(summary["maximum_frame_ssim_standard_deviation"]) for summary in summaries
                    ),
                    "maximum_adjacent_frame_ssim_drop": max(
                        float(summary["maximum_adjacent_frame_ssim_drop"]) for summary in summaries
                    ),
                }
            )
        cell_summaries.append(aggregate)
    evidence["cell_summaries"] = cell_summaries
    evidence["baseline_repeatability"] = {
        "cell_id": balanced.cell_id,
        "eye_bitrate_mbps": balanced.eye_bitrate_mbps,
        "merge_quality": balanced.merge_quality,
        "case_count": len(baseline_cases),
        "cases": baseline_cases,
        "maximum_repeat_ssim_spread": max(
            (float(case["repeat_ssim_spread"]) for case in baseline_cases),
            default=None,
        ),
        "maximum_repeat_size_ratio_spread": max(
            (float(case["repeat_size_ratio_spread"]) for case in baseline_cases),
            default=None,
        ),
    }
    evidence["axis_findings"] = [
        finding for case in cases if isinstance(case, Mapping) for finding in _axis_findings(case, plan)
    ]
    evidence["interaction_observations"] = [
        observation
        for case in cases
        if isinstance(case, Mapping)
        for observation in _interaction_observations(case, plan)
    ]

    cells_complete = len(cell_summaries) == len(plan.cells) and all(
        summary["complete"] is True for summary in cell_summaries
    )
    planned_stress_corpus = set(case_definitions) == set(binding.selected_case_ids)
    eye_order_passed = cells_complete and all(
        isinstance(summary.get("minimum_eye_order_margin"), (int, float))
        and float(summary["minimum_eye_order_margin"]) >= case_definitions[str(case["id"])].minimum_eye_order_margin
        for case in cases
        if isinstance(case, Mapping)
        for summary in [
            cell.get("summary")
            for cell in case.get("cells", [])
            if isinstance(cell, Mapping) and isinstance(cell.get("summary"), Mapping)
        ]
    )
    expected_interactions = len(case_definitions) * (len(plan.eye_bitrates_mbps) - 1) * (len(plan.merge_qualities) - 1)
    interaction_observations_complete = (
        cells_complete and len(evidence["interaction_observations"]) == expected_interactions
    )
    evidence["acceptance"] = {
        "complete": cells_complete,
        "planned_stress_corpus": planned_stress_corpus,
        "objective_validation_passed": cells_complete,
        "eye_order_passed": eye_order_passed,
        "baseline_repeatability_ready": len(baseline_cases) == len(case_definitions),
        "interaction_observations_complete": interaction_observations_complete,
        "execution_passed": cells_complete and eye_order_passed,
        "thresholds_selected": False,
        "ladder_evidence_ready": False,
        "ladder_mapping_selected": False,
        "experiment_complete": (
            cells_complete and planned_stress_corpus and eye_order_passed and interaction_observations_complete
        ),
    }


def _new_evidence(
    plan: ExperimentPlan,
    binding: CorpusBinding,
    plan_sha256: str,
    binding_sha256: str,
    environment: Mapping[str, object],
    selected_cases: Sequence[CorpusCase],
) -> dict[str, object]:
    if plan.relative_path is None or binding.relative_path is None:
        raise QualificationFailure("Generated calibration plan identities are not repository-bound.")
    now = datetime.now(UTC).isoformat()
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "experiment_id": plan.experiment_id,
        "created_at": now,
        "updated_at": now,
        "source_git_sha": environment["git_head"],
        "source_tree_dirty": False,
        "experiment_plan": {"path": plan.relative_path, "sha256": plan_sha256},
        "corpus_binding": {
            "path": binding.relative_path,
            "binding_id": binding.binding_id,
            "sha256": binding_sha256,
        },
        "manifest": {
            "path": _relative_repository_path(binding.source_manifest_path, "Bound corpus manifest"),
            "corpus_id": binding.source_corpus_id,
            "sha256": binding.source_manifest_sha256,
        },
        "method": _method_record(plan),
        "environment": dict(environment),
        "selected_case_ids": [case.case_id for case in selected_cases],
        "cells": [
            {
                "id": cell.cell_id,
                "eye_bitrate_mbps": cell.eye_bitrate_mbps,
                "merge_quality": cell.merge_quality,
            }
            for cell in plan.cells
        ],
        "cases": [],
        "baseline_repeatability": {},
        "cell_summaries": [],
        "axis_findings": [],
        "interaction_observations": [],
        "acceptance": {
            "complete": False,
            "planned_stress_corpus": False,
            "objective_validation_passed": False,
            "eye_order_passed": False,
            "baseline_repeatability_ready": False,
            "interaction_observations_complete": False,
            "execution_passed": False,
            "thresholds_selected": False,
            "ladder_evidence_ready": False,
            "ladder_mapping_selected": False,
            "experiment_complete": False,
        },
    }


def _validate_resume_cases(
    evidence: Mapping[str, object],
    plan: ExperimentPlan,
    binding: CorpusBinding,
    case_definitions: Mapping[str, CorpusCase],
) -> None:
    cases = evidence.get("cases")
    if not isinstance(cases, list):
        raise QualificationFailure("Resume evidence cases must be an array.")
    observed_case_ids: list[str] = []
    for case in cases:
        if not isinstance(case, Mapping) or not isinstance(case.get("id"), str):
            raise QualificationFailure("Resume evidence contains an invalid case record.")
        case_id = str(case["id"])
        observed_case_ids.append(case_id)
        definition = case_definitions.get(case_id)
        if definition is None:
            raise QualificationFailure("Resume evidence contains an unselected case.")
        if case.get("tags") != list(definition.tags) or case.get("quality_gate") is not True:
            raise QualificationFailure(f"Resume evidence case {case_id} metadata changed.")
        if case.get("source") != dict(binding.expected_case_sources[case_id]):
            raise QualificationFailure(f"Resume evidence case {case_id} source identity changed.")
        prepared = case.get("prepared")
        if not isinstance(prepared, Mapping):
            raise QualificationFailure(f"Resume evidence case {case_id} prepared metadata is invalid.")
        if (
            prepared.get("eye_width") != definition.output_eye_width
            or prepared.get("eye_height") != definition.output_eye_height
            or prepared.get("frame_rate") != definition.output_frame_rate
            or type(prepared.get("frame_count")) is not int
            or int(prepared["frame_count"]) <= 0
            or type(prepared.get("duration_seconds")) not in (int, float)
            or float(prepared["duration_seconds"]) <= 0
        ):
            raise QualificationFailure(f"Resume evidence case {case_id} prepared metadata changed.")
        cells = case.get("cells")
        if not isinstance(cells, list) or len(cells) != len(plan.cells):
            raise QualificationFailure(f"Resume evidence case {case_id} has an invalid cell grid.")
        for expected_cell, cell in zip(plan.cells, cells, strict=True):
            if not isinstance(cell, Mapping):
                raise QualificationFailure(f"Resume evidence case {case_id} contains an invalid cell.")
            if (
                cell.get("id") != expected_cell.cell_id
                or cell.get("eye_bitrate_mbps") != expected_cell.eye_bitrate_mbps
                or cell.get("merge_quality") != expected_cell.merge_quality
            ):
                raise QualificationFailure(f"Resume evidence case {case_id} cell identity changed.")
            runs = cell.get("runs")
            if not isinstance(runs, list) or len(runs) > plan.runs_per_cell:
                raise QualificationFailure(f"Resume evidence case {case_id} cell runs are invalid.")
            observed_run_indices: set[int] = set()
            for run_record in runs:
                if not isinstance(run_record, Mapping) or type(run_record.get("run_index")) is not int:
                    raise QualificationFailure(f"Resume evidence case {case_id} contains an invalid run.")
                run_index = int(run_record["run_index"])
                if not 0 <= run_index < plan.runs_per_cell or run_index in observed_run_indices:
                    raise QualificationFailure(f"Resume evidence case {case_id} contains an invalid run index.")
                observed_run_indices.add(run_index)
                _validate_run_record(run_record, expected_cell, run_index)
    if len(set(observed_case_ids)) != len(observed_case_ids) or not set(observed_case_ids).issubset(case_definitions):
        raise QualificationFailure("Resume evidence case identities do not match the selected experiment cases.")


def _load_resume_evidence(
    output_path: Path,
    *,
    plan: ExperimentPlan,
    binding: CorpusBinding,
    plan_sha256: str,
    binding_sha256: str,
    environment: Mapping[str, object],
    selected_cases: Sequence[CorpusCase],
    private_paths: Sequence[Path],
) -> dict[str, object]:
    if output_path.is_symlink():
        raise QualificationFailure("Resume evidence must not be a symlink.")
    try:
        raw_evidence = output_path.read_text(encoding="utf-8")
        evidence = json.loads(raw_evidence)
    except (OSError, json.JSONDecodeError) as error:
        raise QualificationFailure(f"Could not read resume evidence: {error}") from error
    if not isinstance(evidence, dict) or evidence.get("schema_version") != EVIDENCE_SCHEMA_VERSION:
        raise QualificationFailure("Resume evidence has an unsupported schema.")
    expected_identity = {
        "experiment_id": plan.experiment_id,
        "source_git_sha": environment["git_head"],
        "source_tree_dirty": False,
        "experiment_plan": {"path": plan.relative_path, "sha256": plan_sha256},
        "corpus_binding": {
            "path": binding.relative_path,
            "binding_id": binding.binding_id,
            "sha256": binding_sha256,
        },
        "manifest": {
            "path": _relative_repository_path(binding.source_manifest_path, "Bound corpus manifest"),
            "corpus_id": binding.source_corpus_id,
            "sha256": binding.source_manifest_sha256,
        },
        "environment": dict(environment),
        "method": _method_record(plan),
        "selected_case_ids": [case.case_id for case in selected_cases],
        "cells": [
            {
                "id": cell.cell_id,
                "eye_bitrate_mbps": cell.eye_bitrate_mbps,
                "merge_quality": cell.merge_quality,
            }
            for cell in plan.cells
        ],
    }
    for key, expected in expected_identity.items():
        if evidence.get(key) != expected:
            raise QualificationFailure(f"Resume evidence {key} does not match the current experiment identity.")
    case_definitions = {case.case_id: case for case in selected_cases}
    _validate_resume_cases(evidence, plan, binding, case_definitions)
    _assert_private_values_absent(evidence, private_paths)
    acceptance = evidence.get("acceptance")
    complete = isinstance(acceptance, Mapping) and acceptance.get("complete") is True
    mode = stat.S_IMODE(output_path.stat().st_mode)
    if complete:
        canonical = json.dumps(evidence, indent=2, sort_keys=True) + "\n"
        if raw_evidence != canonical:
            raise QualificationFailure("Completed resume evidence is not canonical JSON.")
        if mode != 0o444:
            raise QualificationFailure("Completed resume evidence must be frozen read-only.")
    elif mode & 0o222 == 0:
        raise QualificationFailure("Incomplete resume evidence is unexpectedly read-only.")
    return evidence


def _case_complete(case: Mapping[str, object], plan: ExperimentPlan) -> bool:
    return all(
        (record := _cell_record(case, cell.cell_id)) is not None
        and all(_run_complete(record, cell, run_index) for run_index in range(plan.runs_per_cell))
        for cell in plan.cells
    )


def _completed_resume_is_consistent(
    evidence: dict[str, object],
    plan: ExperimentPlan,
    binding: CorpusBinding,
    case_definitions: Mapping[str, CorpusCase],
) -> bool:
    cases = evidence.get("cases")
    if not isinstance(cases, list) or len(cases) != len(case_definitions):
        return False
    if not all(isinstance(case, Mapping) and _case_complete(case, plan) for case in cases):
        return False
    acceptance = evidence.get("acceptance")
    if not isinstance(acceptance, Mapping) or acceptance.get("complete") is not True:
        return False
    refreshed = copy.deepcopy(evidence)
    _refresh_summaries(refreshed, plan, binding, case_definitions)
    for key in ("baseline_repeatability", "cell_summaries", "axis_findings", "interaction_observations", "acceptance"):
        if refreshed.get(key) != evidence.get(key):
            raise QualificationFailure("Completed resume evidence summaries contradict the recorded runs.")
    return True


def _inspect_generated_output(ffprobe: str, prepared, output_path: Path) -> dict[str, object]:
    stream = ffprobe_stream(ffprobe, output_path)
    if stream.get("codec_name") != "hevc" or stream.get("codec_tag_string") != "hvc1":
        raise QualificationFailure("Generated calibration output is not an hvc1 HEVC stream.")
    if (
        stream.get("width") != prepared.definition.output_eye_width
        or stream.get("height") != prepared.definition.output_eye_height
    ):
        raise QualificationFailure("Generated calibration output has unexpected per-eye dimensions.")
    if stream.get("nb_read_frames") != str(prepared.frame_count):
        raise QualificationFailure("Generated calibration output has an unexpected decoded frame count.")
    try:
        if Fraction(str(stream.get("avg_frame_rate"))) != Fraction(prepared.definition.output_frame_rate):
            raise QualificationFailure("Generated calibration output has an unexpected frame rate.")
    except (ValueError, ZeroDivisionError) as error:
        raise QualificationFailure("Generated calibration output has an invalid frame rate.") from error
    duration_payload = json.loads(
        run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                output_path,
            ]
        ).stdout
    )
    try:
        duration_seconds = float(duration_payload["format"]["duration"])
    except (KeyError, TypeError, ValueError) as error:
        raise QualificationFailure("Generated calibration output has an invalid duration.") from error
    duration_tolerance = max(1.0, prepared.duration_seconds * 0.001)
    if not math.isfinite(duration_seconds) or abs(duration_seconds - prepared.duration_seconds) > duration_tolerance:
        raise QualificationFailure("Generated calibration output has an unexpected duration.")
    observed_box_types = box_types(output_path)
    missing = sorted(CURRENT_REQUIRED_BOX_TYPES - observed_box_types)
    if missing:
        raise QualificationFailure("Generated calibration output is missing required boxes: " + ", ".join(missing))
    return {
        "codec_name": "hevc",
        "codec_tag_string": "hvc1",
        "duration_seconds": duration_seconds,
        "frame_count": prepared.frame_count,
        "observed_box_types": sorted(observed_box_types),
    }


def _validate_prepared_source(binding: CorpusBinding, prepared) -> None:
    expected = binding.expected_case_sources[prepared.definition.case_id]
    if dict(prepared.source_evidence) != dict(expected):
        raise QualificationFailure(
            f"Prepared source identity does not match the checked binding for {prepared.definition.case_id}."
        )


def _run_calibration_unlocked(
    experiment_plan_path: Path,
    output_path: Path,
    work_directory: Path,
    *,
    resume: bool,
    case_ids: Sequence[str] = (),
) -> dict[str, object]:
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise QualificationFailure("Generated MV-HEVC calibration requires macOS arm64.")
    for tool in (EDGE264, SPATIAL_MEDIA_TOOL, MP4BOX):
        if not tool.is_file() or not os.access(tool, os.X_OK):
            raise QualificationFailure(f"Required bundled tool is unavailable: {tool.name}")
    source_git_sha = _git_head_from_clean_worktree()
    plan, binding, plan_sha256, binding_sha256 = load_experiment_plan(experiment_plan_path)
    if _require_head_tracked_file(experiment_plan_path, "Experiment plan") != plan.relative_path:
        raise QualificationFailure("Experiment plan repository identity changed during validation.")
    if _require_head_tracked_file(plan.binding_path, "Corpus binding") != binding.relative_path:
        raise QualificationFailure("Corpus binding repository identity changed during validation.")
    _require_head_tracked_file(binding.source_manifest_path, "Bound corpus manifest")
    _require_head_tracked_file(plan.ffmpeg_manifest_path, "FFmpeg vendor manifest")
    manifest = load_manifest(binding.source_manifest_path)
    gated_by_id = {case.case_id: case for case in manifest.cases if case.quality_gate}
    planned_cases = tuple(gated_by_id[case_id] for case_id in binding.selected_case_ids)
    if case_ids:
        if len(set(case_ids)) != len(case_ids):
            raise QualificationFailure("Subset case IDs must not contain duplicates.")
        unknown = sorted(set(case_ids) - set(binding.selected_case_ids))
        if unknown:
            raise QualificationFailure("Unknown or unplanned stress case IDs: " + ", ".join(unknown))
        requested = set(case_ids)
        selected_cases = tuple(case for case in planned_cases if case.case_id in requested)
    else:
        selected_cases = planned_cases
    if not selected_cases:
        raise QualificationFailure("At least one generated calibration stress case is required.")

    ffmpeg = _pinned_media_tool(plan, "ffmpeg")
    ffprobe = _pinned_media_tool(plan, "ffprobe")
    environment = _environment_evidence(plan, ffmpeg, ffprobe, source_git_sha)
    if environment["git_head"] != source_git_sha:
        raise QualificationFailure("Calibration environment Git identity changed during preflight.")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    work_directory = _prepare_owned_work_directory(work_directory, plan, plan_sha256)
    private_paths = _private_source_paths(selected_cases)
    if output_path.exists():
        if not resume:
            raise QualificationFailure("Calibration output already exists; use --resume or choose a new output path.")
        evidence = _load_resume_evidence(
            output_path,
            plan=plan,
            binding=binding,
            plan_sha256=plan_sha256,
            binding_sha256=binding_sha256,
            environment=environment,
            selected_cases=selected_cases,
            private_paths=private_paths,
        )
    else:
        evidence = _new_evidence(
            plan,
            binding,
            plan_sha256,
            binding_sha256,
            environment,
            selected_cases,
        )
        _atomic_write(output_path, evidence, private_paths)

    case_definitions = {case.case_id: case for case in selected_cases}
    if _completed_resume_is_consistent(evidence, plan, binding, case_definitions):
        return evidence
    for definition in selected_cases:
        existing_case = _case_record(evidence, definition.case_id)
        if existing_case is not None and _case_complete(existing_case, plan):
            continue
        case_work = _reset_case_directory(work_directory, definition.case_id)
        prepared = prepare_case(definition, case_work, ffmpeg=ffmpeg, ffprobe=ffprobe)
        _validate_prepared_source(binding, prepared)
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
                "cells": [
                    {
                        "id": cell.cell_id,
                        "eye_bitrate_mbps": cell.eye_bitrate_mbps,
                        "merge_quality": cell.merge_quality,
                        "runs": [],
                        "summary": None,
                    }
                    for cell in plan.cells
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
        for run_index in range(plan.runs_per_cell):
            for cell in _cell_order(plan, run_index):
                cell_record = _cell_record(existing_case, cell.cell_id)
                if cell_record is None:
                    raise QualificationFailure(f"Cell {cell.cell_id} is missing from calibration evidence.")
                if _run_complete(cell_record, cell, run_index):
                    continue
                output = case_work / f"{cell.cell_id}-run-{run_index + 1}.mov"
                try:
                    _, metrics = measure(
                        partial(
                            _encode_generated,
                            ffmpeg,
                            prepared,
                            output,
                            case_work,
                            eye_bitrate_mbps=cell.eye_bitrate_mbps,
                            merge_quality=cell.merge_quality,
                        )
                    )
                    structure = _inspect_generated_output(ffprobe, prepared, output)
                    measured = _measure_output(
                        ffmpeg,
                        prepared,
                        output,
                        case_work / f"{cell.cell_id}-run-{run_index + 1}-split",
                        target_bitrate_mbps=cell.eye_bitrate_mbps * 2,
                        include_frame_metrics=True,
                    )
                except QualificationFailure as error:
                    raise QualificationFailure(redact_private_source_paths(str(error), private_paths)) from None
                target_total_bitrate = measured.pop("target_bitrate_mbps", None)
                if target_total_bitrate != cell.eye_bitrate_mbps * 2:
                    raise QualificationFailure("Generated calibration measurement lost its target bitrate identity.")
                measured.update(
                    {
                        **structure,
                        "run_index": run_index,
                        "eye_bitrate_mbps": cell.eye_bitrate_mbps,
                        "merge_quality": cell.merge_quality,
                        "target_total_eye_bitrate_mbps": cell.eye_bitrate_mbps * 2,
                        "elapsed_seconds": round(metrics.elapsed_seconds, 6),
                        "user_cpu_seconds": round(metrics.user_cpu_seconds, 6),
                        "system_cpu_seconds": round(metrics.system_cpu_seconds, 6),
                    }
                )
                _validate_run_record(measured, cell, run_index)
                runs = cell_record.get("runs")
                if not isinstance(runs, list):
                    raise QualificationFailure(f"Cell {cell.cell_id} runs are invalid.")
                runs.append(measured)
                runs.sort(key=lambda run: int(run["run_index"]))
                evidence["updated_at"] = datetime.now(UTC).isoformat()
                _atomic_write(output_path, evidence, private_paths)
        _refresh_summaries(evidence, plan, binding, case_definitions)
        evidence["updated_at"] = datetime.now(UTC).isoformat()
        _atomic_write(output_path, evidence, private_paths)
        shutil.rmtree(case_work)

    evidence["updated_at"] = datetime.now(UTC).isoformat()
    _refresh_summaries(evidence, plan, binding, case_definitions)
    _atomic_write(output_path, evidence, private_paths)
    if isinstance(evidence.get("acceptance"), Mapping) and evidence["acceptance"].get("complete") is True:
        _freeze_receipt(output_path)
    return evidence


def run_calibration(
    experiment_plan_path: Path,
    output_path: Path,
    work_directory: Path,
    *,
    resume: bool,
    case_ids: Sequence[str] = (),
) -> dict[str, object]:
    with calibration_lock(output_path, work_directory):
        return _run_calibration_unlocked(
            experiment_plan_path,
            output_path,
            work_directory,
            resume=resume,
            case_ids=case_ids,
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Measure a checked generated MV-HEVC bitrate and merge-quality interaction grid."
    )
    parser.add_argument("--experiment-plan", type=Path, default=DEFAULT_EXPERIMENT_PLAN)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--work-directory", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--case-id", action="append", default=[])
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        evidence = run_calibration(
            args.experiment_plan.resolve(),
            args.output.absolute(),
            args.work_directory.absolute(),
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
        print(f"Generated MV-HEVC calibration failed: {error}", file=sys.stderr)
        return 2
    acceptance = evidence.get("acceptance")
    if not isinstance(acceptance, Mapping):
        print("Generated MV-HEVC calibration failed: acceptance record is missing.", file=sys.stderr)
        return 2
    return 0 if acceptance.get("execution_passed") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
