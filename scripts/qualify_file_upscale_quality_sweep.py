#!/usr/bin/env python3

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tomllib

from dataclasses import dataclass
from datetime import UTC, datetime
from fractions import Fraction
from functools import partial
from itertools import pairwise
from pathlib import Path
from typing import Mapping, Sequence

from bd_to_avp.modules import video
from bd_to_avp.modules.video_quality_defaults import (
    AUTOMATIC_GENERATED_EYE_BITRATE_MBPS,
    AUTOMATIC_GENERATED_MERGE_QUALITY,
    DEFAULT_UPSCALE_QUALITY,
)
from scripts.qualify_direct_mv_hevc import (
    CURRENT_REQUIRED_BOX_TYPES,
    QualificationFailure,
    box_types,
    ffprobe_stream,
    measure,
    run,
    split_mv_hevc,
    ssim,
)
from scripts.qualify_mv_hevc_corpus import (
    CorpusCase,
    PreparedCase,
    _encode_generated,
    _ssim_with_frame_scores,
    _tool_version,
    effective_bitrate_mbps,
    load_manifest,
    prepare_case,
    redact_private_source_paths,
    summarize_frame_quality,
)
from scripts.qualify_generated_mv_hevc_calibration import (
    _assert_private_values_absent,
    _atomic_write,
    _freeze_receipt,
    calibration_lock,
)
from scripts.qualify_mv_hevc_quality_match import sha256_file


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SWEEP_PLAN = REPOSITORY_ROOT / "docs/qualification/file-upscale-quality-sweep-v1.json"
DEFAULT_OUTPUT = REPOSITORY_ROOT / "build/qualification/file-upscale-quality-sweep-v1.json"
DEFAULT_WORK_DIRECTORY = REPOSITORY_ROOT / "build/qualification/file-upscale-quality-sweep-v1-work"
EVIDENCE_SCHEMA_VERSION = 1
WORK_DIRECTORY_MARKER = ".bd-to-avp-file-upscale-quality-sweep.json"
PRIVATE_SOURCE_ENV_NAMES = ("BD_TO_AVP_RELEASE_MVC_SOURCE",)
EXPECTED_CASE_IDS = (
    "production-dark",
    "production-grain-rain",
    "production-crop",
    "production-rate-override",
    "synthetic-animation",
)
EXPECTED_COVERAGE = (
    "real_mvc",
    "motion",
    "grain",
    "dark",
    "animation",
    "crop",
    "disparity",
    "frame_rate_override",
    "bit_depth_8",
)
EXPECTED_QUALITIES = (65, 75, 85)
EXPECTED_ORDERS = ((65, 75, 85), (75, 85, 65), (85, 65, 75))
EXPECTED_TOOL_KEYS = ("edge264_test", "mp4box", "spatial_media_tool")


@dataclass(frozen=True)
class FileBinding:
    path: Path
    sha256: str


@dataclass(frozen=True)
class CorpusBinding:
    binding_id: str
    source_manifest_path: Path
    source_corpus_id: str
    source_manifest_sha256: str
    selected_case_ids: tuple[str, ...]
    expected_case_sources: Mapping[str, Mapping[str, object]]
    required_coverage: tuple[str, ...]
    private_source_identity: Mapping[str, object]
    relative_path: str | None = None
    schema_version: int = 1
    purpose: str = "checked_file_upscale_response_subset_not_ladder_mappings"


@dataclass(frozen=True)
class UpscaleCandidate:
    candidate_id: str
    quality: int

    @property
    def bitrate_scaling_factor(self) -> str:
        return quality_factor_string(self.quality)


@dataclass(frozen=True)
class SweepPlan:
    experiment_id: str
    binding_path: Path
    binding_id: str
    binding_sha256: str
    balanced_quality: int
    base_eye_bitrate_mbps: int
    base_merge_quality: int
    runs_per_candidate: int
    orders: tuple[tuple[int, ...], ...]
    candidates: tuple[UpscaleCandidate, ...]
    ffmpeg_manifest: FileBinding
    fx_upscale_binary: FileBinding
    bundled_tools: Mapping[str, FileBinding]
    generated_encoder_contract: str
    file_upscale_command_contract: str
    metric_contract: str
    geometry_contract: str
    frame_rate_contract: str
    duration_tolerance_frames: int
    relative_path: str | None = None
    schema_version: int = 1
    target_id: str = "upscale_quality"
    purpose: str = "independent_response_characterization_not_ladder_mappings"
    design: str = "fixed_integer_quality_sweep_65_75_85_v1"
    execution_order: str = "explicit_cyclic_quality_orders"
    decision_stage: str = "response_characterization_only"


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


def _number(value: object, label: str, *, positive: bool = False, nonnegative: bool = False) -> float:
    if type(value) not in (int, float) or not math.isfinite(float(value)):
        raise QualificationFailure(f"{label} must be a finite number.")
    parsed = float(value)
    if positive and parsed <= 0:
        raise QualificationFailure(f"{label} must be positive.")
    if nonnegative and parsed < 0:
        raise QualificationFailure(f"{label} must be non-negative.")
    return parsed


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if key in document:
            raise QualificationFailure(f"JSON object contains duplicate key: {key}")
        document[key] = value
    return document


def _loads_json_bytes(data: bytes, label: str) -> Mapping[str, object]:
    try:
        document = json.loads(data.decode("utf-8"), object_pairs_hook=_strict_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise QualificationFailure(f"Could not parse {label} as strict UTF-8 JSON.") from error
    return _mapping(document, label)


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


def _file_binding(value: object, label: str) -> FileBinding:
    document = _mapping(value, label)
    return FileBinding(
        path=_repository_path(_string(document.get("path"), f"{label}.path"), f"{label} path"),
        sha256=_sha256_identity(document.get("sha256"), f"{label}.sha256"),
    )


def quality_factor_string(quality: int) -> str:
    if type(quality) is not int or not 0 <= quality <= 100:
        raise QualificationFailure("Upscale quality must be an integer from 0 through 100.")
    whole, hundredths = divmod(quality, 100)
    return str(whole) if hundredths == 0 else f"{whole}.{hundredths:02d}".rstrip("0")


def parse_corpus_binding(raw: object) -> CorpusBinding:
    document = _mapping(raw, "file-upscale corpus binding")
    schema_version = _integer(document.get("schema_version"), "schema_version", minimum=1, maximum=1)
    binding_id = _string(document.get("binding_id"), "binding_id")
    if binding_id != "file-upscale-quality-corpus-v1":
        raise QualificationFailure("file-upscale corpus binding_id is unsupported.")
    purpose = _string(document.get("purpose"), "purpose")
    if purpose != "checked_file_upscale_response_subset_not_ladder_mappings":
        raise QualificationFailure("file-upscale corpus binding purpose is unsupported.")
    source_manifest = _mapping(document.get("source_manifest"), "source_manifest")
    source_manifest_path = _repository_path(
        _string(source_manifest.get("path"), "source_manifest.path"),
        "Source manifest path",
    )
    source_corpus_id = _string(source_manifest.get("corpus_id"), "source_manifest.corpus_id")
    source_manifest_sha256 = _sha256_identity(source_manifest.get("sha256"), "source_manifest.sha256")
    selected_case_ids = tuple(
        _string(case_id, f"selected_case_ids[{index}]")
        for index, case_id in enumerate(_array(document.get("selected_case_ids"), "selected_case_ids"))
    )
    if selected_case_ids != EXPECTED_CASE_IDS:
        raise QualificationFailure("file-upscale corpus binding must use exactly the checked five-case subset.")
    expected_case_sources_document = _mapping(document.get("expected_case_sources"), "expected_case_sources")
    if set(expected_case_sources_document) != set(selected_case_ids):
        raise QualificationFailure("expected_case_sources must identify every selected file-upscale case exactly once.")
    expected_case_sources = {
        case_id: dict(_mapping(expected_case_sources_document[case_id], f"expected_case_sources.{case_id}"))
        for case_id in selected_case_ids
    }
    required_coverage = tuple(
        _string(tag, f"required_coverage[{index}]")
        for index, tag in enumerate(_array(document.get("required_coverage"), "required_coverage"))
    )
    if required_coverage != EXPECTED_COVERAGE:
        raise QualificationFailure("file-upscale corpus binding must retain the checked coverage contract.")
    private_source_identity = dict(_mapping(document.get("private_source_identity"), "private_source_identity"))
    return CorpusBinding(
        schema_version=schema_version,
        binding_id=binding_id,
        purpose=purpose,
        source_manifest_path=source_manifest_path,
        source_corpus_id=source_corpus_id,
        source_manifest_sha256=source_manifest_sha256,
        selected_case_ids=selected_case_ids,
        expected_case_sources=expected_case_sources,
        required_coverage=required_coverage,
        private_source_identity=private_source_identity,
    )


def _validate_private_source_identity(binding: CorpusBinding) -> None:
    identity = binding.private_source_identity
    anchor_source = _repository_path(_string(identity.get("source"), "private_source_identity.source"), "Anchor plan")
    anchor_sha256 = _sha256_identity(identity.get("source_sha256"), "private_source_identity.source_sha256")
    if not anchor_source.is_file() or sha256_file(anchor_source) != anchor_sha256:
        raise QualificationFailure("The private source anchor plan does not match its checked SHA-256 identity.")
    anchor = _loads_json_bytes(anchor_source.read_bytes(), "direct anchor plan")
    if anchor.get("qualification_id") != identity.get("source_qualification_id"):
        raise QualificationFailure("The private source anchor plan identity changed.")
    source = _mapping(anchor.get("source"), "direct anchor source")
    expected = {
        "path_env": _string(identity.get("path_env"), "private_source_identity.path_env"),
        "sha256": _sha256_identity(identity.get("sha256"), "private_source_identity.sha256"),
        "size_bytes": _integer(
            identity.get("size_bytes"), "private_source_identity.size_bytes", minimum=1, maximum=10**15
        ),
        "duration_seconds": _number(
            identity.get("duration_seconds"), "private_source_identity.duration_seconds", positive=True
        ),
        "frame_rate": _string(identity.get("frame_rate"), "private_source_identity.frame_rate"),
        "frame_count": _integer(
            identity.get("frame_count"), "private_source_identity.frame_count", minimum=1, maximum=10**12
        ),
        "width": _integer(identity.get("width"), "private_source_identity.width", minimum=1, maximum=100000),
        "height": _integer(identity.get("height"), "private_source_identity.height", minimum=1, maximum=100000),
        "pixel_format": _string(identity.get("pixel_format"), "private_source_identity.pixel_format"),
    }
    for key, expected_value in expected.items():
        if source.get(key) != expected_value:
            raise QualificationFailure("The private source identity does not match the direct anchor plan.")


def _validate_expected_case_sources(binding: CorpusBinding, cases_by_id: Mapping[str, CorpusCase]) -> None:
    for case_id in binding.selected_case_ids:
        case = cases_by_id[case_id]
        expected = _mapping(binding.expected_case_sources[case_id], f"expected_case_sources.{case_id}")
        if expected.get("kind") != case.source["kind"]:
            raise QualificationFailure(f"Case {case_id} source kind changed from the checked binding.")
        if case.source["kind"] in {"mvc_container", "mvc_annex_b"}:
            if expected.get("path_env") != case.source.get("path_env"):
                raise QualificationFailure(f"Case {case_id} source environment changed from the checked binding.")
        if case.source["kind"] == "mvc_container":
            if expected.get("start_seconds") != float(case.source["start_seconds"]) or expected.get(
                "requested_duration_seconds"
            ) != float(case.source["duration_seconds"]):
                raise QualificationFailure(f"Case {case_id} segment boundary changed from the checked binding.")
            _sha256_identity(expected.get("segment_sha256"), f"expected_case_sources.{case_id}.segment_sha256")
            _integer(
                expected.get("segment_bytes"),
                f"expected_case_sources.{case_id}.segment_bytes",
                minimum=1,
                maximum=10**12,
            )
        if case.source["kind"] == "synthetic":
            filter_hash = hashlib.sha256(str(case.source["filter"]).encode()).hexdigest()
            if expected.get("filter_sha256") != filter_hash:
                raise QualificationFailure(f"Case {case_id} synthetic source changed from the checked binding.")
            if expected.get("requested_duration_seconds") != float(case.source["duration_seconds"]):
                raise QualificationFailure(f"Case {case_id} synthetic duration changed from the checked binding.")


def load_corpus_binding(path: Path) -> tuple[CorpusBinding, str]:
    resolved_path = path.resolve()
    relative_path = _relative_repository_path(resolved_path, "File-upscale corpus binding")
    try:
        raw = _loads_json_bytes(resolved_path.read_bytes(), "file-upscale corpus binding")
    except OSError as error:
        raise QualificationFailure(f"Could not read file-upscale corpus binding {path.name}: {error}") from error
    parsed = parse_corpus_binding(raw)
    if not parsed.source_manifest_path.is_file():
        raise QualificationFailure("The bound direct corpus manifest is unavailable.")
    if sha256_file(parsed.source_manifest_path) != parsed.source_manifest_sha256:
        raise QualificationFailure("The bound direct corpus manifest does not match its pinned SHA-256 identity.")
    manifest = load_manifest(parsed.source_manifest_path)
    if manifest.corpus_id != parsed.source_corpus_id:
        raise QualificationFailure("The bound direct corpus ID does not match the referenced manifest.")
    cases_by_id = {case.case_id: case for case in manifest.cases}
    missing = sorted(set(parsed.selected_case_ids) - set(cases_by_id))
    if missing:
        raise QualificationFailure("Corpus binding references unknown cases: " + ", ".join(missing))
    observed_coverage = {tag for case_id in parsed.selected_case_ids for tag in cases_by_id[case_id].tags}
    missing_coverage = sorted(set(parsed.required_coverage) - observed_coverage)
    if missing_coverage:
        raise QualificationFailure("Corpus binding is missing required coverage: " + ", ".join(missing_coverage))
    if any(not cases_by_id[case_id].quality_gate for case_id in parsed.selected_case_ids):
        raise QualificationFailure("File-upscale corpus binding must use quality-gated direct-corpus cases only.")
    _validate_expected_case_sources(parsed, cases_by_id)
    _validate_private_source_identity(parsed)
    return (
        CorpusBinding(
            binding_id=parsed.binding_id,
            source_manifest_path=parsed.source_manifest_path,
            source_corpus_id=parsed.source_corpus_id,
            source_manifest_sha256=parsed.source_manifest_sha256,
            selected_case_ids=parsed.selected_case_ids,
            expected_case_sources=parsed.expected_case_sources,
            required_coverage=parsed.required_coverage,
            private_source_identity=parsed.private_source_identity,
            relative_path=relative_path,
            schema_version=parsed.schema_version,
            purpose=parsed.purpose,
        ),
        sha256_file(resolved_path),
    )


def parse_sweep_plan(raw: object) -> SweepPlan:
    document = _mapping(raw, "file-upscale sweep plan")
    schema_version = _integer(document.get("schema_version"), "schema_version", minimum=1, maximum=1)
    experiment_id = _string(document.get("experiment_id"), "experiment_id")
    if experiment_id != "file-upscale-quality-sweep-v1":
        raise QualificationFailure("file-upscale experiment_id is unsupported.")
    target_id = _string(document.get("target_id"), "target_id")
    if target_id != "upscale_quality":
        raise QualificationFailure("file-upscale sweep target_id must be 'upscale_quality'.")
    purpose = _string(document.get("purpose"), "purpose")
    if purpose != "independent_response_characterization_not_ladder_mappings":
        raise QualificationFailure("file-upscale sweep must remain independent response characterization.")
    design = _string(document.get("design"), "design")
    if design != "fixed_integer_quality_sweep_65_75_85_v1":
        raise QualificationFailure("file-upscale sweep design is unsupported.")

    corpus_binding = _mapping(document.get("corpus_binding"), "corpus_binding")
    binding_path = _repository_path(
        _string(corpus_binding.get("path"), "corpus_binding.path"),
        "Corpus binding path",
    )
    binding_id = _string(corpus_binding.get("binding_id"), "corpus_binding.binding_id")
    binding_sha256 = _sha256_identity(corpus_binding.get("sha256"), "corpus_binding.sha256")

    balanced = _mapping(document.get("balanced"), "balanced")
    balanced_quality = _integer(balanced.get("quality"), "balanced.quality", minimum=0, maximum=100)
    if balanced_quality != DEFAULT_UPSCALE_QUALITY:
        raise QualificationFailure("Balanced quality must match DEFAULT_UPSCALE_QUALITY exactly.")
    if balanced.get("quality_source") != "bd_to_avp.modules.video_quality_defaults.DEFAULT_UPSCALE_QUALITY":
        raise QualificationFailure("Balanced quality source must identify DEFAULT_UPSCALE_QUALITY.")

    generated_base = _mapping(document.get("generated_base"), "generated_base")
    base_eye_bitrate = _integer(
        generated_base.get("eye_bitrate_mbps"),
        "generated_base.eye_bitrate_mbps",
        minimum=1,
        maximum=500,
    )
    base_merge_quality = _integer(
        generated_base.get("merge_quality"),
        "generated_base.merge_quality",
        minimum=0,
        maximum=100,
    )
    if base_eye_bitrate != AUTOMATIC_GENERATED_EYE_BITRATE_MBPS:
        raise QualificationFailure("Generated base eye bitrate must match the production default exactly.")
    if base_merge_quality != AUTOMATIC_GENERATED_MERGE_QUALITY:
        raise QualificationFailure("Generated base merge quality must match the production default exactly.")
    if (
        generated_base.get("eye_bitrate_source")
        != "bd_to_avp.modules.video_quality_defaults.AUTOMATIC_GENERATED_EYE_BITRATE_MBPS"
    ):
        raise QualificationFailure("Generated base eye bitrate source must identify the production default.")
    if (
        generated_base.get("merge_quality_source")
        != "bd_to_avp.modules.video_quality_defaults.AUTOMATIC_GENERATED_MERGE_QUALITY"
    ):
        raise QualificationFailure("Generated base merge quality source must identify the production default.")
    if generated_base.get("contract") != "production_generated_mv_hevc_v1":
        raise QualificationFailure("Generated base contract is unsupported.")

    runs_per_candidate = _integer(document.get("runs_per_candidate"), "runs_per_candidate", minimum=3, maximum=3)
    execution_order = _string(document.get("execution_order"), "execution_order")
    if execution_order != "explicit_cyclic_quality_orders":
        raise QualificationFailure("file-upscale sweep execution_order is unsupported.")
    orders = tuple(
        tuple(
            _integer(raw_quality, f"orders[{order_index}][{quality_index}]", minimum=0, maximum=100)
            for quality_index, raw_quality in enumerate(_array(raw_order, f"orders[{order_index}]"))
        )
        for order_index, raw_order in enumerate(_array(document.get("orders"), "orders"))
    )
    if orders != EXPECTED_ORDERS:
        raise QualificationFailure("file-upscale sweep orders must match the checked cyclic design exactly.")

    raw_candidates = _array(document.get("candidates"), "candidates")
    candidates: list[UpscaleCandidate] = []
    for index, raw_candidate in enumerate(raw_candidates):
        candidate = _mapping(raw_candidate, f"candidates[{index}]")
        quality = _integer(candidate.get("quality"), f"candidates[{index}].quality", minimum=0, maximum=100)
        candidate_id = _string(candidate.get("id"), f"candidates[{index}].id")
        if candidate_id != f"q{quality:03d}":
            raise QualificationFailure("file-upscale candidate id must be qNNN for its integer quality.")
        candidates.append(UpscaleCandidate(candidate_id=candidate_id, quality=quality))
    if tuple(candidate.quality for candidate in candidates) != EXPECTED_QUALITIES:
        raise QualificationFailure("file-upscale candidates must be exactly 65, 75, and 85.")

    toolchain = _mapping(document.get("toolchain"), "toolchain")
    ffmpeg_manifest = _file_binding(toolchain.get("ffmpeg_manifest"), "toolchain.ffmpeg_manifest")
    generated_encoder_contract = _string(
        toolchain.get("generated_encoder_contract"), "toolchain.generated_encoder_contract"
    )
    if generated_encoder_contract != "production_generated_mv_hevc_v1":
        raise QualificationFailure("toolchain generated encoder contract is unsupported.")
    file_upscale_command_contract = _string(
        toolchain.get("file_upscale_command_contract"),
        "toolchain.file_upscale_command_contract",
    )
    if file_upscale_command_contract != "bd_to_avp.modules.video.fx_upscale_command_v1":
        raise QualificationFailure("toolchain file-upscale command contract is unsupported.")
    fx_upscale_binary = _file_binding(toolchain.get("fx_upscale_binary"), "toolchain.fx_upscale_binary")
    bundled_tools_document = _mapping(toolchain.get("bundled_tools"), "toolchain.bundled_tools")
    if tuple(sorted(bundled_tools_document)) != EXPECTED_TOOL_KEYS:
        raise QualificationFailure("toolchain bundled_tools must identify the checked helper set exactly.")
    bundled_tools = {
        key: _file_binding(bundled_tools_document[key], f"toolchain.bundled_tools.{key}") for key in EXPECTED_TOOL_KEYS
    }
    metric_contract = _string(toolchain.get("metric_contract"), "toolchain.metric_contract")
    if metric_contract != "ffmpeg_ssim_aggregate_and_per_frame_v1":
        raise QualificationFailure("toolchain metric contract is unsupported.")
    geometry_contract = _string(toolchain.get("geometry_contract"), "toolchain.geometry_contract")
    if geometry_contract != "fx_upscale_2x_spatial_output_v1":
        raise QualificationFailure("toolchain geometry contract is unsupported.")
    timing_contract = _mapping(toolchain.get("timing_contract"), "toolchain.timing_contract")
    frame_rate_contract = _string(
        timing_contract.get("frame_rate"),
        "toolchain.timing_contract.frame_rate",
    )
    if frame_rate_contract != "exact_rational_match_v1":
        raise QualificationFailure("toolchain frame-rate contract is unsupported.")
    duration_tolerance_frames = _integer(
        timing_contract.get("duration_tolerance_frames"),
        "toolchain.timing_contract.duration_tolerance_frames",
        minimum=1,
        maximum=1,
    )

    decision_policy = _mapping(document.get("decision_policy"), "decision_policy")
    if _string(decision_policy.get("stage"), "decision_policy.stage") != "response_characterization_only":
        raise QualificationFailure("file-upscale decision policy stage is unsupported.")
    for key in (
        "post_hoc_thresholds_forbidden",
        "public_mapping_changes_forbidden",
        "perceptual_4k_quality_claim_forbidden",
        "vision_pro_quality_claim_forbidden",
    ):
        if decision_policy.get(key) is not True:
            raise QualificationFailure(f"decision_policy.{key} must be true.")
    for key in ("ladder_mapping_selected", "thresholds_selected"):
        if decision_policy.get(key) is not False:
            raise QualificationFailure(f"decision_policy.{key} must be false.")

    return SweepPlan(
        schema_version=schema_version,
        experiment_id=experiment_id,
        target_id=target_id,
        purpose=purpose,
        design=design,
        binding_path=binding_path,
        binding_id=binding_id,
        binding_sha256=binding_sha256,
        balanced_quality=balanced_quality,
        base_eye_bitrate_mbps=base_eye_bitrate,
        base_merge_quality=base_merge_quality,
        runs_per_candidate=runs_per_candidate,
        orders=orders,
        candidates=tuple(candidates),
        ffmpeg_manifest=ffmpeg_manifest,
        fx_upscale_binary=fx_upscale_binary,
        bundled_tools=bundled_tools,
        generated_encoder_contract=generated_encoder_contract,
        file_upscale_command_contract=file_upscale_command_contract,
        metric_contract=metric_contract,
        geometry_contract=geometry_contract,
        frame_rate_contract=frame_rate_contract,
        duration_tolerance_frames=duration_tolerance_frames,
        execution_order=execution_order,
    )


def load_sweep_plan(path: Path) -> tuple[SweepPlan, CorpusBinding, str, str]:
    resolved_path = path.resolve()
    relative_path = _relative_repository_path(resolved_path, "File-upscale sweep plan")
    try:
        raw = _loads_json_bytes(resolved_path.read_bytes(), "file-upscale sweep plan")
    except OSError as error:
        raise QualificationFailure(f"Could not read file-upscale sweep plan {path.name}: {error}") from error
    parsed = parse_sweep_plan(raw)
    binding, binding_sha256 = load_corpus_binding(parsed.binding_path)
    if binding_sha256 != parsed.binding_sha256:
        raise QualificationFailure("The file-upscale corpus binding does not match its pinned SHA-256 identity.")
    if binding.binding_id != parsed.binding_id:
        raise QualificationFailure("The file-upscale corpus binding ID does not match the sweep plan.")
    for label, file_binding in (
        ("FFmpeg vendor manifest", parsed.ffmpeg_manifest),
        ("FX Upscale binary", parsed.fx_upscale_binary),
        *((f"bundled tool {key}", tool) for key, tool in parsed.bundled_tools.items()),
    ):
        if not file_binding.path.is_file():
            raise QualificationFailure(f"{label} is unavailable.")
        if sha256_file(file_binding.path) != file_binding.sha256:
            raise QualificationFailure(f"{label} does not match its pinned SHA-256 identity.")
    return (
        SweepPlan(
            experiment_id=parsed.experiment_id,
            binding_path=parsed.binding_path,
            binding_id=parsed.binding_id,
            binding_sha256=parsed.binding_sha256,
            balanced_quality=parsed.balanced_quality,
            base_eye_bitrate_mbps=parsed.base_eye_bitrate_mbps,
            base_merge_quality=parsed.base_merge_quality,
            runs_per_candidate=parsed.runs_per_candidate,
            orders=parsed.orders,
            candidates=parsed.candidates,
            ffmpeg_manifest=parsed.ffmpeg_manifest,
            fx_upscale_binary=parsed.fx_upscale_binary,
            bundled_tools=parsed.bundled_tools,
            generated_encoder_contract=parsed.generated_encoder_contract,
            file_upscale_command_contract=parsed.file_upscale_command_contract,
            metric_contract=parsed.metric_contract,
            geometry_contract=parsed.geometry_contract,
            frame_rate_contract=parsed.frame_rate_contract,
            duration_tolerance_frames=parsed.duration_tolerance_frames,
            relative_path=relative_path,
            schema_version=parsed.schema_version,
            target_id=parsed.target_id,
            purpose=parsed.purpose,
            design=parsed.design,
            execution_order=parsed.execution_order,
            decision_stage=parsed.decision_stage,
        ),
        binding,
        sha256_file(resolved_path),
        binding_sha256,
    )


def _binding_record(binding: FileBinding) -> dict[str, object]:
    return {"path": _relative_repository_path(binding.path, "Tool binding"), "sha256": binding.sha256}


def _toolchain_record(plan: SweepPlan) -> dict[str, object]:
    return {
        "ffmpeg_manifest": _binding_record(plan.ffmpeg_manifest),
        "generated_encoder_contract": plan.generated_encoder_contract,
        "file_upscale_command_contract": plan.file_upscale_command_contract,
        "fx_upscale_binary": _binding_record(plan.fx_upscale_binary),
        "bundled_tools": {key: _binding_record(plan.bundled_tools[key]) for key in EXPECTED_TOOL_KEYS},
        "metric_contract": plan.metric_contract,
        "geometry_contract": plan.geometry_contract,
        "timing_contract": {
            "frame_rate": plan.frame_rate_contract,
            "duration_tolerance_frames": plan.duration_tolerance_frames,
        },
    }


def _method_record(plan: SweepPlan) -> dict[str, object]:
    return {
        "design": plan.design,
        "runs_per_candidate": plan.runs_per_candidate,
        "balanced_quality": plan.balanced_quality,
        "generated_base": {
            "eye_bitrate_mbps": plan.base_eye_bitrate_mbps,
            "merge_quality": plan.base_merge_quality,
            "target_total_eye_bitrate_mbps": plan.base_eye_bitrate_mbps * 2,
        },
        "orders": [list(order) for order in plan.orders],
        "candidate_order": plan.execution_order,
        "quality_metric": "downscaled aggregate and per-frame decoded same-eye SSIM",
        "timing_validation": {
            "frame_rate": plan.frame_rate_contract,
            "duration_tolerance_frames": plan.duration_tolerance_frames,
        },
        "pairing": "one fresh generated base per case/repeat; all candidates run against exact copies of that base",
        "paired_delta": "candidate minus the same case/repeat q075 output",
        "post_hoc_thresholds_forbidden": True,
        "thresholds_selected": False,
        "public_mapping_changes_forbidden": True,
        "ladder_mapping_selected": False,
        "perceptual_4k_quality_claim_forbidden": True,
        "vision_pro_quality_claim_forbidden": True,
        "decision_stage": plan.decision_stage,
    }


def _candidate_plan_record(candidate: UpscaleCandidate) -> dict[str, object]:
    return {
        "id": candidate.candidate_id,
        "quality": candidate.quality,
        "quality_factor": f"{candidate.quality}/100",
        "bitrate_scaling_factor": candidate.bitrate_scaling_factor,
    }


def _prepare_owned_work_directory(work_directory: Path, plan: SweepPlan, plan_sha256: str) -> Path:
    resolved = work_directory.resolve()
    dangerous = {Path("/").resolve(), Path.home().resolve(), REPOSITORY_ROOT}
    if resolved in dangerous:
        raise QualificationFailure("File-upscale sweep work directory must be a dedicated non-root directory.")
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
                raise QualificationFailure(f"Could not validate sweep work-directory ownership: {error}") from error
            if observed != expected_marker:
                raise QualificationFailure("File-upscale sweep work directory belongs to a different experiment.")
        elif any(resolved.iterdir()):
            raise QualificationFailure("File-upscale sweep work directory is non-empty and has no ownership marker.")
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
        raise QualificationFailure("File-upscale sweep work-directory ownership marker is missing.")
    return case_directory


def _reset_case_directory(work_directory: Path, case_id: str) -> Path:
    case_directory = _owned_case_directory(work_directory, case_id)
    if case_directory.exists():
        shutil.rmtree(case_directory)
    case_directory.mkdir(parents=True)
    return case_directory


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
        raise QualificationFailure("File-upscale sweep evidence requires a clean source worktree.")
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPOSITORY_ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()


def _vendor_binary_hashes(plan: SweepPlan) -> dict[str, str]:
    try:
        document = tomllib.loads(plan.ffmpeg_manifest.path.read_text(encoding="utf-8"))
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


def _pinned_media_tool(plan: SweepPlan, name: str) -> str:
    expected_hash = _vendor_binary_hashes(plan)[name]
    path = REPOSITORY_ROOT / "bd_to_avp/bin" / name
    if not path.is_file() or not os.access(path, os.X_OK):
        raise QualificationFailure(f"Pinned {name} is unavailable; run 'uv run python -m scripts.vendor_ffmpeg_macos'.")
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


def _environment_evidence(plan: SweepPlan, ffmpeg: str, ffprobe: str, source_git_sha: str) -> dict[str, object]:
    return {
        "cpu_brand": _sysctl_value("machdep.cpu.brand_string"),
        "edge264_sha256": sha256_file(plan.bundled_tools["edge264_test"].path),
        "ffmpeg": _tool_version([ffmpeg, "-hide_banner", "-version"]),
        "ffmpeg_sha256": sha256_file(Path(ffmpeg)),
        "ffmpeg_vendor_manifest_sha256": plan.ffmpeg_manifest.sha256,
        "ffprobe": _tool_version([ffprobe, "-hide_banner", "-version"]),
        "ffprobe_sha256": sha256_file(Path(ffprobe)),
        "file_upscale_command_contract": plan.file_upscale_command_contract,
        "fx_upscale_sha256": sha256_file(plan.fx_upscale_binary.path),
        "generated_encoder_contract": plan.generated_encoder_contract,
        "geometry_contract": plan.geometry_contract,
        "git_head": source_git_sha,
        "hardware_model": _sysctl_value("hw.model"),
        "machine": platform.machine(),
        "macos_build": _sysctl_value("kern.osversion"),
        "macos_version": platform.mac_ver()[0],
        "metric_contract": plan.metric_contract,
        "mp4box_sha256": sha256_file(plan.bundled_tools["mp4box"].path),
        "platform": platform.system(),
        "spatial_media_tool_sha256": sha256_file(plan.bundled_tools["spatial_media_tool"].path),
    }


def _private_source_paths(cases: Sequence[CorpusCase]) -> tuple[Path, ...]:
    paths: list[Path] = []
    for case in cases:
        path_env = case.source.get("path_env")
        if isinstance(path_env, str) and os.environ.get(path_env):
            paths.append(Path(os.environ[path_env]).expanduser().resolve())
    return tuple(paths)


def _configured_private_paths() -> tuple[Path, ...]:
    paths: set[Path] = set()
    try:
        for environment_name in PRIVATE_SOURCE_ENV_NAMES:
            configured = os.environ.get(environment_name)
            if configured:
                expanded = Path(configured).expanduser()
                paths.add(expanded)
                paths.add(expanded.resolve())
    except (OSError, RuntimeError) as error:
        raise QualificationFailure("Configured private source path could not be normalized.") from error
    return tuple(paths)


def _safe_error_message(error: BaseException, private_paths: Sequence[Path]) -> str:
    if isinstance(error, subprocess.SubprocessError):
        return "Subprocess execution failed."
    return redact_private_source_paths(str(error), private_paths)


def _candidate_for_quality(plan: SweepPlan, quality: int) -> UpscaleCandidate:
    return next(candidate for candidate in plan.candidates if candidate.quality == quality)


def _candidate_order(plan: SweepPlan, repeat_index: int) -> tuple[UpscaleCandidate, ...]:
    if not 0 <= repeat_index < plan.runs_per_candidate:
        raise QualificationFailure("File-upscale repeat index is outside the checked schedule.")
    return tuple(_candidate_for_quality(plan, quality) for quality in plan.orders[repeat_index])


def _frame_rate(value: object, label: str) -> Fraction:
    text = _string(value, label)
    try:
        rate = Fraction(text)
    except (ValueError, ZeroDivisionError) as error:
        raise QualificationFailure(f"{label} is not a valid rational frame rate.") from error
    if rate <= 0:
        raise QualificationFailure(f"{label} must be positive.")
    return rate


def _format_duration_seconds(ffprobe: str, path: Path) -> float:
    completed = run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            path,
        ]
    )
    try:
        document = json.loads(completed.stdout)
        raw_duration = _mapping(document.get("format"), "ffprobe format").get("duration")
        duration = _number(float(raw_duration), "ffprobe format.duration", positive=True)
    except (TypeError, ValueError, json.JSONDecodeError, QualificationFailure) as error:
        raise QualificationFailure(f"Could not read output duration for {path.name}.") from error
    return duration


def _inspect_mv_hevc_output(
    ffprobe: str,
    prepared: PreparedCase,
    plan: SweepPlan,
    output_path: Path,
    *,
    geometry_scale: int,
) -> dict[str, object]:
    stream = ffprobe_stream(ffprobe, output_path)
    expected_width = prepared.definition.output_eye_width * geometry_scale
    expected_height = prepared.definition.output_eye_height * geometry_scale
    expected_frame_rate = _frame_rate(prepared.definition.output_frame_rate, "expected frame rate")
    observed_frame_rate = _frame_rate(stream.get("avg_frame_rate"), "observed average frame rate")
    observed_r_frame_rate = _frame_rate(stream.get("r_frame_rate"), "observed real frame rate")
    if stream.get("codec_name") != "hevc" or stream.get("codec_tag_string") != "hvc1":
        raise QualificationFailure("File-upscale output is not an hvc1 HEVC stream.")
    if stream.get("width") != expected_width or stream.get("height") != expected_height:
        raise QualificationFailure(
            f"File-upscale output has {stream.get('width')}x{stream.get('height')}; "
            f"expected {expected_width}x{expected_height}."
        )
    if stream.get("nb_read_frames") != str(prepared.frame_count):
        raise QualificationFailure("File-upscale output has an unexpected decoded frame count.")
    if observed_frame_rate != expected_frame_rate or observed_r_frame_rate != expected_frame_rate:
        raise QualificationFailure("File-upscale output frame rate does not match the checked source timing contract.")
    observed_duration = _format_duration_seconds(ffprobe, output_path)
    expected_duration = prepared.frame_count / float(expected_frame_rate)
    duration_tolerance = plan.duration_tolerance_frames / float(expected_frame_rate)
    if abs(observed_duration - expected_duration) > duration_tolerance + 1e-6:
        raise QualificationFailure("File-upscale output duration exceeds the checked one-frame timing tolerance.")
    observed_box_types = sorted(box_types(output_path))
    if not CURRENT_REQUIRED_BOX_TYPES.issubset(set(observed_box_types)):
        raise QualificationFailure("File-upscale output is missing required spatial metadata.")
    return {
        "codec_name": stream.get("codec_name"),
        "codec_tag_string": stream.get("codec_tag_string"),
        "width": expected_width,
        "height": expected_height,
        "frame_count": prepared.frame_count,
        "frame_rate": str(observed_frame_rate),
        "r_frame_rate": str(observed_r_frame_rate),
        "duration_seconds": observed_duration,
        "duration_tolerance_frames": plan.duration_tolerance_frames,
        "geometry_scale": geometry_scale,
        "observed_box_types": observed_box_types,
    }


def _downscale_eye(ffmpeg: str, input_path: Path, output_path: Path, *, width: int, height: int) -> None:
    run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            input_path,
            "-vf",
            f"scale={width}:{height}:flags=lanczos,format=yuv420p",
            "-c:v",
            "ffv1",
            "-level",
            "3",
            "-g",
            "1",
            "-y",
            output_path,
        ]
    )


def _measure_downscaled_output(
    ffmpeg: str,
    prepared: PreparedCase,
    output_path: Path,
    split_directory: Path,
    *,
    duration_seconds: float,
) -> dict[str, object]:
    left_upscaled, right_upscaled = split_mv_hevc(output_path, split_directory)
    left_downscaled = split_directory / "left-downscaled.mkv"
    right_downscaled = split_directory / "right-downscaled.mkv"
    width = prepared.definition.output_eye_width
    height = prepared.definition.output_eye_height
    _downscale_eye(ffmpeg, left_upscaled, left_downscaled, width=width, height=height)
    _downscale_eye(ffmpeg, right_upscaled, right_downscaled, width=width, height=height)
    left_match, left_frame_scores = _ssim_with_frame_scores(ffmpeg, left_downscaled, prepared.reference_left)
    right_match, right_frame_scores = _ssim_with_frame_scores(ffmpeg, right_downscaled, prepared.reference_right)
    left_cross = ssim(ffmpeg, left_downscaled, prepared.reference_right)
    right_cross = ssim(ffmpeg, right_downscaled, prepared.reference_left)
    frame_summary = summarize_frame_quality(left_frame_scores, right_frame_scores)
    shutil.rmtree(split_directory, ignore_errors=True)
    return {
        "effective_bitrate_mbps": round(effective_bitrate_mbps(output_path.stat().st_size, duration_seconds), 6),
        "left_cross_ssim": left_cross,
        "left_match_ssim": left_match,
        "min_eye_order_margin": min(left_match - left_cross, right_match - right_cross),
        "min_same_eye_ssim": min(left_match, right_match),
        "right_cross_ssim": right_cross,
        "right_match_ssim": right_match,
        **frame_summary,
    }


def _expected_upscaled_output(input_path: Path) -> Path:
    return input_path.with_name(f"{input_path.stem} Upscaled{input_path.suffix}")


def _run_fx_upscale(fx_upscale_path: Path, input_path: Path, quality: int) -> Path:
    previous_path = video.config.FX_UPSCALE_PATH
    video.config.FX_UPSCALE_PATH = fx_upscale_path
    try:
        command = video.fx_upscale_command(input_path, quality)
    finally:
        video.config.FX_UPSCALE_PATH = previous_path
    expected_command = [fx_upscale_path, "--bitrate-scaling-factor", quality_factor_string(quality), input_path]
    if command != expected_command:
        raise QualificationFailure("FX Upscale command helper diverged from the checked sweep contract.")
    output_path = _expected_upscaled_output(input_path)
    output_path.unlink(missing_ok=True)
    run(command)
    if not output_path.is_file():
        raise QualificationFailure("FX Upscale did not create its expected output.")
    return output_path


def _record_base(
    ffmpeg: str,
    ffprobe: str,
    prepared: PreparedCase,
    plan: SweepPlan,
    repeat_index: int,
    base_path: Path,
) -> dict[str, object]:
    _, metrics = measure(
        partial(
            _encode_generated,
            ffmpeg,
            prepared,
            base_path,
            base_path.parent,
            eye_bitrate_mbps=plan.base_eye_bitrate_mbps,
            merge_quality=plan.base_merge_quality,
        )
    )
    structure = _inspect_mv_hevc_output(ffprobe, prepared, plan, base_path, geometry_scale=1)
    return {
        "repeat_index": repeat_index,
        "generated_eye_bitrate_mbps": plan.base_eye_bitrate_mbps,
        "generated_merge_quality": plan.base_merge_quality,
        "target_total_eye_bitrate_mbps": plan.base_eye_bitrate_mbps * 2,
        "source_sha256": sha256_file(prepared.source_path),
        "sha256": sha256_file(base_path),
        "bytes": base_path.stat().st_size,
        "effective_bitrate_mbps": round(
            effective_bitrate_mbps(base_path.stat().st_size, float(structure["duration_seconds"])),
            6,
        ),
        "elapsed_seconds": round(metrics.elapsed_seconds, 6),
        "user_cpu_seconds": round(metrics.user_cpu_seconds, 6),
        "system_cpu_seconds": round(metrics.system_cpu_seconds, 6),
        **structure,
    }


def _record_candidate(
    ffmpeg: str,
    ffprobe: str,
    prepared: PreparedCase,
    plan: SweepPlan,
    candidate: UpscaleCandidate,
    repeat_index: int,
    execution_ordinal: int,
    base_path: Path,
    base_record: Mapping[str, object],
    repeat_directory: Path,
) -> dict[str, object]:
    if sha256_file(base_path) != base_record.get("sha256"):
        raise QualificationFailure("Generated base artifact changed before candidate copy.")
    candidate_input = repeat_directory / f"{candidate.candidate_id}-input.mov"
    final_path = repeat_directory / f"{candidate.candidate_id}-upscaled.mov"
    candidate_input.unlink(missing_ok=True)
    final_path.unlink(missing_ok=True)
    shutil.copyfile(base_path, candidate_input)
    input_copy_sha256 = sha256_file(candidate_input)
    if input_copy_sha256 != base_record["sha256"]:
        raise QualificationFailure("Candidate input is not an exact copy of its paired generated base.")
    _, metrics = measure(partial(_run_fx_upscale, plan.fx_upscale_binary.path, candidate_input, candidate.quality))
    shutil.move(_expected_upscaled_output(candidate_input), final_path)
    structure = _inspect_mv_hevc_output(ffprobe, prepared, plan, final_path, geometry_scale=2)
    measured = _measure_downscaled_output(
        ffmpeg,
        prepared,
        final_path,
        repeat_directory / f"{candidate.candidate_id}-split",
        duration_seconds=float(structure["duration_seconds"]),
    )
    final_bytes = final_path.stat().st_size
    base_bytes = int(base_record["bytes"])
    return {
        "id": candidate.candidate_id,
        "repeat_index": repeat_index,
        "execution_ordinal": execution_ordinal,
        "quality": candidate.quality,
        "quality_factor": f"{candidate.quality}/100",
        "bitrate_scaling_factor": candidate.bitrate_scaling_factor,
        "source_sha256": base_record["source_sha256"],
        "base_sha256": base_record["sha256"],
        "base_bytes": base_bytes,
        "base_effective_bitrate_mbps": base_record["effective_bitrate_mbps"],
        "input_copy_sha256": input_copy_sha256,
        "final_sha256": sha256_file(final_path),
        "final_bytes": final_bytes,
        "final_to_base_size_ratio": final_bytes / base_bytes,
        "upscale_elapsed_seconds": round(metrics.elapsed_seconds, 6),
        "upscale_user_cpu_seconds": round(metrics.user_cpu_seconds, 6),
        "upscale_system_cpu_seconds": round(metrics.system_cpu_seconds, 6),
        "projected_full_route_elapsed_seconds": round(
            float(base_record["elapsed_seconds"]) + metrics.elapsed_seconds, 6
        ),
        "paired_delta_to_q075": None,
        **structure,
        **measured,
    }


def _validate_base_record(base: Mapping[str, object], plan: SweepPlan, repeat_index: int) -> None:
    expected_keys = {
        "bytes",
        "codec_name",
        "codec_tag_string",
        "effective_bitrate_mbps",
        "duration_seconds",
        "duration_tolerance_frames",
        "elapsed_seconds",
        "frame_count",
        "frame_rate",
        "r_frame_rate",
        "generated_eye_bitrate_mbps",
        "generated_merge_quality",
        "geometry_scale",
        "height",
        "observed_box_types",
        "repeat_index",
        "sha256",
        "source_sha256",
        "system_cpu_seconds",
        "target_total_eye_bitrate_mbps",
        "user_cpu_seconds",
        "width",
    }
    if set(base) != expected_keys:
        raise QualificationFailure(f"Repeat {repeat_index} generated base has an invalid record shape.")
    if (
        type(base.get("repeat_index")) is not int
        or base.get("repeat_index") != repeat_index
        or base.get("generated_eye_bitrate_mbps") != plan.base_eye_bitrate_mbps
        or base.get("generated_merge_quality") != plan.base_merge_quality
        or base.get("target_total_eye_bitrate_mbps") != plan.base_eye_bitrate_mbps * 2
        or base.get("geometry_scale") != 1
    ):
        raise QualificationFailure(f"Repeat {repeat_index} generated base identity changed.")
    if type(base.get("bytes")) is not int or int(base["bytes"]) <= 0:
        raise QualificationFailure(f"Repeat {repeat_index} generated base bytes must be positive.")
    if base.get("codec_name") != "hevc" or base.get("codec_tag_string") != "hvc1":
        raise QualificationFailure(f"Repeat {repeat_index} generated base is not hvc1 HEVC.")
    for key in ("sha256", "source_sha256"):
        _sha256_identity(base.get(key), f"base.{key}")
    for key in ("effective_bitrate_mbps", "duration_seconds", "elapsed_seconds"):
        _number(base.get(key), f"base.{key}", positive=True)
    expected_bitrate = round(
        effective_bitrate_mbps(int(base["bytes"]), float(base["duration_seconds"])),
        6,
    )
    if not math.isclose(
        float(base["effective_bitrate_mbps"]),
        expected_bitrate,
        rel_tol=0.0,
        abs_tol=1e-12,
    ):
        raise QualificationFailure(f"Repeat {repeat_index} generated base bitrate contradicts its timing.")
    if base.get("duration_tolerance_frames") != plan.duration_tolerance_frames:
        raise QualificationFailure(f"Repeat {repeat_index} generated base timing contract changed.")
    _frame_rate(base.get("frame_rate"), "base.frame_rate")
    _frame_rate(base.get("r_frame_rate"), "base.r_frame_rate")
    for key in ("user_cpu_seconds", "system_cpu_seconds"):
        _number(base.get(key), f"base.{key}", nonnegative=True)
    if not isinstance(base.get("observed_box_types"), list) or not CURRENT_REQUIRED_BOX_TYPES.issubset(
        set(str(item) for item in base["observed_box_types"])
    ):
        raise QualificationFailure(f"Repeat {repeat_index} generated base is missing spatial metadata.")


def _validate_base_against_case(
    base: Mapping[str, object],
    definition: CorpusCase,
    prepared: Mapping[str, object],
) -> None:
    prepared_source_sha256 = _sha256_identity(prepared.get("source_sha256"), "prepared.source_sha256")
    expected_frame_rate = _frame_rate(definition.output_frame_rate, "expected case frame rate")
    if (
        base.get("source_sha256") != prepared_source_sha256
        or base.get("frame_count") != prepared.get("frame_count")
        or base.get("width") != definition.output_eye_width
        or base.get("height") != definition.output_eye_height
        or _frame_rate(base.get("frame_rate"), "base.frame_rate") != expected_frame_rate
        or _frame_rate(base.get("r_frame_rate"), "base.r_frame_rate") != expected_frame_rate
    ):
        raise QualificationFailure(f"Repeat base does not match prepared case {definition.case_id}.")
    expected_duration = int(base["frame_count"]) / float(expected_frame_rate)
    duration_tolerance = int(base["duration_tolerance_frames"]) / float(expected_frame_rate)
    if abs(float(base["duration_seconds"]) - expected_duration) > duration_tolerance + 1e-6:
        raise QualificationFailure(f"Repeat base timing does not match prepared case {definition.case_id}.")


def _validate_paired_delta(delta: object, candidate: UpscaleCandidate) -> None:
    if delta is None:
        return
    document = _mapping(delta, "paired_delta_to_q075")
    expected_keys = {
        "effective_bitrate_mbps",
        "final_bytes",
        "final_to_base_size_ratio",
        "maximum_adjacent_frame_ssim_drop",
        "median_frame_same_eye_ssim",
        "min_eye_order_margin",
        "min_same_eye_ssim",
        "minimum_frame_same_eye_ssim",
        "p05_frame_same_eye_ssim",
        "projected_full_route_elapsed_seconds",
        "upscale_elapsed_seconds",
    }
    if set(document) != expected_keys:
        raise QualificationFailure(f"Candidate {candidate.candidate_id} paired delta has an invalid shape.")
    for key in expected_keys:
        _number(document.get(key), f"paired_delta_to_q075.{key}")


def _validate_candidate_record(
    record: Mapping[str, object],
    candidate: UpscaleCandidate,
    repeat_index: int,
    execution_ordinal: int,
) -> None:
    expected_keys = {
        "base_bytes",
        "base_effective_bitrate_mbps",
        "base_sha256",
        "bitrate_scaling_factor",
        "codec_name",
        "codec_tag_string",
        "effective_bitrate_mbps",
        "duration_seconds",
        "duration_tolerance_frames",
        "execution_ordinal",
        "final_bytes",
        "final_sha256",
        "final_to_base_size_ratio",
        "frame_count",
        "frame_quality_sample_count",
        "frame_rate",
        "r_frame_rate",
        "frame_ssim_standard_deviation",
        "geometry_scale",
        "height",
        "id",
        "input_copy_sha256",
        "left_cross_ssim",
        "left_match_ssim",
        "maximum_adjacent_frame_ssim_drop",
        "median_frame_same_eye_ssim",
        "min_eye_order_margin",
        "min_same_eye_ssim",
        "minimum_frame_same_eye_ssim",
        "observed_box_types",
        "p05_frame_same_eye_ssim",
        "paired_delta_to_q075",
        "projected_full_route_elapsed_seconds",
        "quality",
        "quality_factor",
        "repeat_index",
        "right_cross_ssim",
        "right_match_ssim",
        "source_sha256",
        "upscale_elapsed_seconds",
        "upscale_system_cpu_seconds",
        "upscale_user_cpu_seconds",
        "width",
    }
    if set(record) != expected_keys:
        raise QualificationFailure(f"Candidate {candidate.candidate_id} repeat {repeat_index} has an invalid shape.")
    if (
        type(record.get("repeat_index")) is not int
        or type(record.get("execution_ordinal")) is not int
        or type(record.get("quality")) is not int
        or type(record.get("geometry_scale")) is not int
        or record.get("id") != candidate.candidate_id
        or record.get("repeat_index") != repeat_index
        or record.get("execution_ordinal") != execution_ordinal
        or record.get("quality") != candidate.quality
        or record.get("quality_factor") != f"{candidate.quality}/100"
        or record.get("bitrate_scaling_factor") != candidate.bitrate_scaling_factor
        or record.get("geometry_scale") != 2
    ):
        raise QualificationFailure(f"Candidate {candidate.candidate_id} repeat {repeat_index} identity changed.")
    for key in ("base_sha256", "input_copy_sha256", "final_sha256", "source_sha256"):
        _sha256_identity(record.get(key), f"candidate.{key}")
    if record["base_sha256"] != record["input_copy_sha256"]:
        raise QualificationFailure(f"Candidate {candidate.candidate_id} did not run from an exact base copy.")
    for key in ("base_bytes", "final_bytes", "frame_count", "frame_quality_sample_count", "width", "height"):
        if type(record.get(key)) is not int or int(record[key]) <= 0:
            raise QualificationFailure(f"Candidate {candidate.candidate_id} {key} must be a positive integer.")
    if record.get("codec_name") != "hevc" or record.get("codec_tag_string") != "hvc1":
        raise QualificationFailure(f"Candidate {candidate.candidate_id} is not hvc1 HEVC.")
    for key in (
        "base_effective_bitrate_mbps",
        "effective_bitrate_mbps",
        "duration_seconds",
        "final_to_base_size_ratio",
        "projected_full_route_elapsed_seconds",
        "upscale_elapsed_seconds",
    ):
        _number(record.get(key), f"candidate.{key}", positive=True)
    if record.get("duration_tolerance_frames") != 1:
        raise QualificationFailure(f"Candidate {candidate.candidate_id} timing contract changed.")
    _frame_rate(record.get("frame_rate"), "candidate.frame_rate")
    _frame_rate(record.get("r_frame_rate"), "candidate.r_frame_rate")
    for key in ("upscale_user_cpu_seconds", "upscale_system_cpu_seconds", "frame_ssim_standard_deviation"):
        _number(record.get(key), f"candidate.{key}", nonnegative=True)
    ssim_values: dict[str, float] = {}
    for key in ("left_cross_ssim", "left_match_ssim", "min_same_eye_ssim", "right_cross_ssim", "right_match_ssim"):
        value = _number(record.get(key), f"candidate.{key}")
        if not 0 <= value <= 1:
            raise QualificationFailure(f"Candidate {candidate.candidate_id} {key} must be 0..1.")
        ssim_values[key] = value
    expected_same_eye = min(ssim_values["left_match_ssim"], ssim_values["right_match_ssim"])
    expected_eye_margin = min(
        ssim_values["left_match_ssim"] - ssim_values["left_cross_ssim"],
        ssim_values["right_match_ssim"] - ssim_values["right_cross_ssim"],
    )
    if not math.isclose(ssim_values["min_same_eye_ssim"], expected_same_eye, rel_tol=0.0, abs_tol=1e-12):
        raise QualificationFailure(f"Candidate {candidate.candidate_id} min_same_eye_ssim contradicts eye metrics.")
    if not math.isclose(
        _number(record.get("min_eye_order_margin"), "candidate.min_eye_order_margin"),
        expected_eye_margin,
        rel_tol=0.0,
        abs_tol=1e-12,
    ):
        raise QualificationFailure(f"Candidate {candidate.candidate_id} min_eye_order_margin contradicts eye metrics.")
    for key in (
        "minimum_frame_same_eye_ssim",
        "p05_frame_same_eye_ssim",
        "median_frame_same_eye_ssim",
        "maximum_adjacent_frame_ssim_drop",
    ):
        value = _number(record.get(key), f"candidate.{key}")
        if not 0 <= value <= 1:
            raise QualificationFailure(f"Candidate {candidate.candidate_id} {key} must be 0..1.")
    if not isinstance(record.get("observed_box_types"), list) or not CURRENT_REQUIRED_BOX_TYPES.issubset(
        set(str(item) for item in record["observed_box_types"])
    ):
        raise QualificationFailure(f"Candidate {candidate.candidate_id} is missing required spatial metadata.")
    _validate_paired_delta(record.get("paired_delta_to_q075"), candidate)


def _validate_candidate_against_base(
    record: Mapping[str, object],
    base: Mapping[str, object],
    candidate: UpscaleCandidate,
) -> None:
    exact_pairs = (
        ("base_sha256", "sha256"),
        ("base_bytes", "bytes"),
        ("base_effective_bitrate_mbps", "effective_bitrate_mbps"),
        ("source_sha256", "source_sha256"),
        ("frame_count", "frame_count"),
        ("frame_rate", "frame_rate"),
        ("r_frame_rate", "r_frame_rate"),
        ("duration_tolerance_frames", "duration_tolerance_frames"),
    )
    for candidate_key, base_key in exact_pairs:
        if record.get(candidate_key) != base.get(base_key):
            raise QualificationFailure(
                f"Candidate {candidate.candidate_id} does not match its recorded generated base."
            )
    if record.get("input_copy_sha256") != base.get("sha256"):
        raise QualificationFailure(f"Candidate {candidate.candidate_id} did not consume its recorded base bytes.")
    if record.get("width") != int(base["width"]) * 2 or record.get("height") != int(base["height"]) * 2:
        raise QualificationFailure(f"Candidate {candidate.candidate_id} geometry does not match its generated base.")
    duration_tolerance = int(base["duration_tolerance_frames"]) / float(
        _frame_rate(base["frame_rate"], "base.frame_rate")
    )
    if abs(float(record["duration_seconds"]) - float(base["duration_seconds"])) > duration_tolerance + 1e-6:
        raise QualificationFailure(f"Candidate {candidate.candidate_id} timing does not match its generated base.")
    expected_size_ratio = int(record["final_bytes"]) / int(base["bytes"])
    if not math.isclose(
        float(record["final_to_base_size_ratio"]),
        expected_size_ratio,
        rel_tol=0.0,
        abs_tol=1e-12,
    ):
        raise QualificationFailure(f"Candidate {candidate.candidate_id} size ratio contradicts its generated base.")
    expected_bitrate = round(
        effective_bitrate_mbps(int(record["final_bytes"]), float(record["duration_seconds"])),
        6,
    )
    if not math.isclose(
        float(record["effective_bitrate_mbps"]),
        expected_bitrate,
        rel_tol=0.0,
        abs_tol=1e-12,
    ):
        raise QualificationFailure(f"Candidate {candidate.candidate_id} bitrate contradicts its output timing.")


def _candidate_record(repeat: Mapping[str, object], candidate_id: str) -> dict[str, object] | None:
    candidates = repeat.get("candidates")
    if not isinstance(candidates, list):
        return None
    for candidate in candidates:
        if isinstance(candidate, dict) and candidate.get("id") == candidate_id:
            return candidate
    return None


def _validate_candidate_prefix(repeat: Mapping[str, object], plan: SweepPlan, repeat_index: int) -> None:
    candidates = repeat.get("candidates")
    if not isinstance(candidates, list):
        raise QualificationFailure(f"Repeat {repeat_index} candidates must be an array.")
    observed_ids: list[str] = []
    for record in candidates:
        if not isinstance(record, Mapping) or not isinstance(record.get("id"), str):
            raise QualificationFailure(f"Repeat {repeat_index} contains an invalid candidate record.")
        observed_ids.append(str(record["id"]))
    expected_ids = [candidate.candidate_id for candidate in _candidate_order(plan, repeat_index)]
    if observed_ids != expected_ids[: len(observed_ids)]:
        raise QualificationFailure(f"Repeat {repeat_index} candidates do not match the checked execution prefix.")


def _candidate_complete(
    repeat: Mapping[str, object],
    plan: SweepPlan,
    candidate: UpscaleCandidate,
    repeat_index: int,
) -> bool:
    record = _candidate_record(repeat, candidate.candidate_id)
    if record is None:
        return False
    base = repeat.get("base")
    if not isinstance(base, Mapping):
        raise QualificationFailure("A candidate record exists without its generated base.")
    execution_ordinal = plan.orders[repeat_index].index(candidate.quality)
    _validate_candidate_record(record, candidate, repeat_index, execution_ordinal)
    _validate_candidate_against_base(record, base, candidate)
    return True


def _repeat_complete(repeat: Mapping[str, object], plan: SweepPlan, repeat_index: int) -> bool:
    base = repeat.get("base")
    if not isinstance(base, Mapping):
        return False
    _validate_base_record(base, plan, repeat_index)
    expected_order = _candidate_order(plan, repeat_index)
    candidates = repeat.get("candidates")
    if not isinstance(candidates, list):
        return False
    _validate_candidate_prefix(repeat, plan, repeat_index)
    if len(candidates) != len(expected_order):
        return False
    return all(_candidate_complete(repeat, plan, candidate, repeat_index) for candidate in expected_order)


def _repeat_record(case: Mapping[str, object], repeat_index: int) -> dict[str, object] | None:
    repeats = case.get("repeats")
    if not isinstance(repeats, list):
        return None
    for repeat in repeats:
        if isinstance(repeat, dict) and repeat.get("repeat_index") == repeat_index:
            return repeat
    return None


def _case_complete(case: Mapping[str, object], plan: SweepPlan) -> bool:
    repeats = case.get("repeats")
    if not isinstance(repeats, list) or len(repeats) != plan.runs_per_candidate:
        return False
    return all(
        (repeat := _repeat_record(case, repeat_index)) is not None and _repeat_complete(repeat, plan, repeat_index)
        for repeat_index in range(plan.runs_per_candidate)
    )


def _case_record(evidence: Mapping[str, object], case_id: str) -> dict[str, object] | None:
    cases = evidence.get("cases")
    if not isinstance(cases, list):
        return None
    for case in cases:
        if isinstance(case, dict) and case.get("id") == case_id:
            return case
    return None


def _paired_delta(candidate: Mapping[str, object], balanced: Mapping[str, object]) -> dict[str, object]:
    keys = (
        "effective_bitrate_mbps",
        "final_bytes",
        "final_to_base_size_ratio",
        "maximum_adjacent_frame_ssim_drop",
        "median_frame_same_eye_ssim",
        "min_eye_order_margin",
        "min_same_eye_ssim",
        "minimum_frame_same_eye_ssim",
        "p05_frame_same_eye_ssim",
        "projected_full_route_elapsed_seconds",
        "upscale_elapsed_seconds",
    )
    return {key: candidate[key] - balanced[key] for key in keys}


def _summarize_candidate_records(records: Sequence[Mapping[str, object]]) -> dict[str, object]:
    if not records:
        raise ValueError("candidate records are required")
    final_bytes = [int(record["final_bytes"]) for record in records]
    quality = [float(record["min_same_eye_ssim"]) for record in records]
    return {
        "run_count": len(records),
        "median_final_bytes": statistics.median(final_bytes),
        "minimum_final_bytes": min(final_bytes),
        "maximum_final_bytes": max(final_bytes),
        "median_final_to_base_size_ratio": statistics.median(
            float(record["final_to_base_size_ratio"]) for record in records
        ),
        "median_effective_bitrate_mbps": statistics.median(
            float(record["effective_bitrate_mbps"]) for record in records
        ),
        "median_min_same_eye_ssim": statistics.median(quality),
        "minimum_min_same_eye_ssim": min(quality),
        "maximum_min_same_eye_ssim": max(quality),
        "repeat_ssim_spread": max(quality) - min(quality),
        "minimum_frame_same_eye_ssim": min(float(record["minimum_frame_same_eye_ssim"]) for record in records),
        "median_p05_frame_same_eye_ssim": statistics.median(
            float(record["p05_frame_same_eye_ssim"]) for record in records
        ),
        "maximum_frame_ssim_standard_deviation": max(
            float(record["frame_ssim_standard_deviation"]) for record in records
        ),
        "maximum_adjacent_frame_ssim_drop": max(
            float(record["maximum_adjacent_frame_ssim_drop"]) for record in records
        ),
        "minimum_eye_order_margin": min(float(record["min_eye_order_margin"]) for record in records),
        "median_upscale_elapsed_seconds": statistics.median(
            float(record["upscale_elapsed_seconds"]) for record in records
        ),
        "median_projected_full_route_elapsed_seconds": statistics.median(
            float(record["projected_full_route_elapsed_seconds"]) for record in records
        ),
    }


def _refresh_summaries(
    evidence: dict[str, object],
    plan: SweepPlan,
    binding: CorpusBinding,
    case_definitions: Mapping[str, CorpusCase],
) -> None:
    cases = evidence.get("cases")
    if not isinstance(cases, list):
        raise QualificationFailure("File-upscale evidence cases must be an array.")
    monotonicity_findings: list[dict[str, object]] = []
    case_summaries: list[dict[str, object]] = []
    candidate_records: dict[str, list[Mapping[str, object]]] = {
        candidate.candidate_id: [] for candidate in plan.candidates
    }
    complete_case_count = 0
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("id"), str):
            raise QualificationFailure("File-upscale evidence contains an invalid case record.")
        case_id = str(case["id"])
        definition = case_definitions.get(case_id)
        if definition is None:
            raise QualificationFailure("File-upscale evidence contains an unselected case.")
        repeats = case.get("repeats")
        if not isinstance(repeats, list):
            raise QualificationFailure(f"Case {case_id} repeats must be an array.")
        case_complete = _case_complete(case, plan)
        if case_complete:
            complete_case_count += 1
        case_candidate_records: dict[str, list[Mapping[str, object]]] = {
            candidate.candidate_id: [] for candidate in plan.candidates
        }
        for repeat_index in range(plan.runs_per_candidate):
            repeat = _repeat_record(case, repeat_index)
            if repeat is None:
                continue
            if repeat.get("order") != list(plan.orders[repeat_index]):
                raise QualificationFailure(
                    f"Case {case_id} repeat {repeat_index} order changed from the checked schedule."
                )
            _validate_candidate_prefix(repeat, plan, repeat_index)
            repeat_base = repeat.get("base")
            if repeat.get("candidates") and not isinstance(repeat_base, Mapping):
                raise QualificationFailure(f"Case {case_id} repeat {repeat_index} has candidates without a base.")
            balanced = _candidate_record(repeat, "q075")
            if balanced is not None:
                balanced_candidate = _candidate_for_quality(plan, 75)
                balanced_ordinal = plan.orders[repeat_index].index(75)
                _validate_candidate_record(balanced, balanced_candidate, repeat_index, balanced_ordinal)
                _validate_candidate_against_base(balanced, _mapping(repeat_base, "repeat.base"), balanced_candidate)
                for candidate in plan.candidates:
                    record = _candidate_record(repeat, candidate.candidate_id)
                    if record is None:
                        continue
                    execution_ordinal = plan.orders[repeat_index].index(candidate.quality)
                    _validate_candidate_record(record, candidate, repeat_index, execution_ordinal)
                    _validate_candidate_against_base(record, _mapping(repeat_base, "repeat.base"), candidate)
                    record["paired_delta_to_q075"] = _paired_delta(record, balanced)
                    _validate_candidate_record(record, candidate, repeat_index, execution_ordinal)
            complete_records: list[Mapping[str, object]] = []
            for candidate in plan.candidates:
                record = _candidate_record(repeat, candidate.candidate_id)
                if record is None:
                    continue
                execution_ordinal = plan.orders[repeat_index].index(candidate.quality)
                _validate_candidate_record(record, candidate, repeat_index, execution_ordinal)
                _validate_candidate_against_base(record, _mapping(repeat_base, "repeat.base"), candidate)
                candidate_records[candidate.candidate_id].append(record)
                case_candidate_records[candidate.candidate_id].append(record)
                complete_records.append(record)
            if len(complete_records) == len(plan.candidates):
                ordered = sorted(complete_records, key=lambda record: int(record["quality"]))
                for lower, higher in pairwise(ordered):
                    base = {
                        "case_id": case_id,
                        "repeat_index": repeat_index,
                        "lower_quality": lower["quality"],
                        "higher_quality": higher["quality"],
                        "lower_candidate": lower["id"],
                        "higher_candidate": higher["id"],
                    }
                    if int(higher["final_bytes"]) < int(lower["final_bytes"]):
                        monotonicity_findings.append({"code": "storage_reversal", **base})
                    elif int(higher["final_bytes"]) == int(lower["final_bytes"]):
                        monotonicity_findings.append({"code": "storage_tie_ambiguous", **base})
                    if higher["final_sha256"] == lower["final_sha256"]:
                        monotonicity_findings.append({"code": "identical_output_ambiguous", **base})
        summary: dict[str, object] = {"id": case_id, "complete": case_complete}
        if any(case_candidate_records.values()):
            summary["candidates"] = []
            for candidate in plan.candidates:
                records = case_candidate_records[candidate.candidate_id]
                candidate_summary: dict[str, object] = {
                    "id": candidate.candidate_id,
                    "quality": candidate.quality,
                    "complete": len(records) == plan.runs_per_candidate,
                    "run_count": len(records),
                }
                if records:
                    candidate_summary.update(_summarize_candidate_records(records))
                summary["candidates"].append(candidate_summary)
        case_summaries.append(summary)
    evidence["case_summaries"] = case_summaries

    candidate_summaries: list[dict[str, object]] = []
    expected_record_count = len(case_definitions) * plan.runs_per_candidate
    for candidate in plan.candidates:
        records = candidate_records[candidate.candidate_id]
        summary = {
            "id": candidate.candidate_id,
            "quality": candidate.quality,
            "quality_factor": f"{candidate.quality}/100",
            "bitrate_scaling_factor": candidate.bitrate_scaling_factor,
            "complete": len(records) == expected_record_count,
            "run_count": len(records),
        }
        if records:
            summary.update(_summarize_candidate_records(records))
            if candidate.quality == plan.balanced_quality:
                summary["paired_median_min_same_eye_ssim_delta_to_q075"] = 0.0
                summary["paired_median_final_bytes_delta_to_q075"] = 0.0
            else:
                deltas = [
                    record["paired_delta_to_q075"]
                    for record in records
                    if isinstance(record.get("paired_delta_to_q075"), Mapping)
                ]
                if deltas:
                    summary["paired_median_min_same_eye_ssim_delta_to_q075"] = statistics.median(
                        float(delta["min_same_eye_ssim"]) for delta in deltas
                    )
                    summary["paired_median_final_bytes_delta_to_q075"] = statistics.median(
                        float(delta["final_bytes"]) for delta in deltas
                    )
        candidate_summaries.append(summary)
    evidence["candidate_summaries"] = candidate_summaries
    evidence["monotonicity_findings"] = monotonicity_findings

    selected_case_ids = evidence.get("selected_case_ids")
    planned_full = selected_case_ids == list(binding.selected_case_ids)
    complete = complete_case_count == len(case_definitions) and all(
        summary.get("complete") is True for summary in candidate_summaries
    )
    eye_order_passed = complete and all(
        float(record["min_eye_order_margin"]) >= case_definitions[str(case["id"])].minimum_eye_order_margin
        for case in cases
        if isinstance(case, Mapping)
        for repeat in case.get("repeats", [])
        if isinstance(repeat, Mapping)
        for record in repeat.get("candidates", [])
        if isinstance(record, Mapping)
    )
    storage_reversal = any(finding["code"] == "storage_reversal" for finding in monotonicity_findings)
    ambiguous_codes = {"storage_tie_ambiguous", "identical_output_ambiguous"}
    response_ambiguous = any(finding["code"] in ambiguous_codes for finding in monotonicity_findings)
    size_monotonic_passed = complete and not storage_reversal
    size_decision_ready = size_monotonic_passed and not any(
        finding["code"] == "storage_tie_ambiguous" for finding in monotonicity_findings
    )
    decision_ready = complete and planned_full and eye_order_passed and size_decision_ready and not response_ambiguous
    previous_acceptance = evidence.get("acceptance")
    finalized = (
        isinstance(previous_acceptance, Mapping)
        and previous_acceptance.get("finalized") is True
        and complete
        and planned_full
    )
    evidence["acceptance"] = {
        "complete": complete,
        "finalized": finalized,
        "planned_full_stress_subset": planned_full,
        "structural_passed": complete,
        "eye_order_passed": eye_order_passed,
        "size_monotonic_passed": size_monotonic_passed,
        "size_decision_ready": size_decision_ready,
        "response_ambiguous": response_ambiguous or not complete,
        "execution_passed": complete and eye_order_passed,
        "decision_ready": decision_ready,
        "thresholds_selected": False,
        "post_hoc_thresholds_forbidden": True,
        "public_mapping_changes_forbidden": True,
        "ladder_mapping_selected": False,
        "perceptual_4k_quality_claimed": False,
        "vision_pro_quality_claimed": False,
        "passed": decision_ready,
    }


def _new_evidence(
    plan: SweepPlan,
    binding: CorpusBinding,
    plan_sha256: str,
    binding_sha256: str,
    environment: Mapping[str, object],
    selected_cases: Sequence[CorpusCase],
) -> dict[str, object]:
    if plan.relative_path is None or binding.relative_path is None:
        raise QualificationFailure("File-upscale sweep inputs are not bound to repository-relative paths.")
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
            "path": _relative_repository_path(binding.source_manifest_path, "Bound direct corpus manifest"),
            "corpus_id": binding.source_corpus_id,
            "sha256": binding.source_manifest_sha256,
        },
        "private_source_identity": dict(binding.private_source_identity),
        "toolchain": _toolchain_record(plan),
        "method": _method_record(plan),
        "environment": dict(environment),
        "selected_case_ids": [case.case_id for case in selected_cases],
        "candidates": [_candidate_plan_record(candidate) for candidate in plan.candidates],
        "cases": [],
        "case_summaries": [],
        "candidate_summaries": [],
        "monotonicity_findings": [],
        "acceptance": {
            "complete": False,
            "finalized": False,
            "planned_full_stress_subset": False,
            "structural_passed": False,
            "eye_order_passed": False,
            "size_monotonic_passed": False,
            "size_decision_ready": False,
            "response_ambiguous": True,
            "execution_passed": False,
            "decision_ready": False,
            "thresholds_selected": False,
            "post_hoc_thresholds_forbidden": True,
            "public_mapping_changes_forbidden": True,
            "ladder_mapping_selected": False,
            "perceptual_4k_quality_claimed": False,
            "vision_pro_quality_claimed": False,
            "passed": False,
        },
    }


def _validate_resume_cases(
    evidence: Mapping[str, object],
    plan: SweepPlan,
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
        if case.get("tags") != list(definition.tags) or case.get("quality_gate") is not definition.quality_gate:
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
            or not isinstance(prepared.get("source_sha256"), str)
        ):
            raise QualificationFailure(f"Resume evidence case {case_id} prepared metadata changed.")
        repeats = case.get("repeats")
        if not isinstance(repeats, list) or len(repeats) > plan.runs_per_candidate:
            raise QualificationFailure(f"Resume evidence case {case_id} repeats are invalid.")
        observed_repeats: list[int] = []
        for repeat in repeats:
            if not isinstance(repeat, Mapping) or type(repeat.get("repeat_index")) is not int:
                raise QualificationFailure(f"Resume evidence case {case_id} contains an invalid repeat.")
            repeat_index = int(repeat["repeat_index"])
            if not 0 <= repeat_index < plan.runs_per_candidate or repeat_index in observed_repeats:
                raise QualificationFailure(f"Resume evidence case {case_id} contains an invalid repeat index.")
            observed_repeats.append(repeat_index)
            if repeat.get("order") != list(plan.orders[repeat_index]):
                raise QualificationFailure(f"Resume evidence case {case_id} repeat order changed.")
            base = repeat.get("base")
            if base is not None:
                base_record = _mapping(base, "repeat.base")
                _validate_base_record(base_record, plan, repeat_index)
                _validate_base_against_case(base_record, definition, prepared)
            candidates = repeat.get("candidates")
            if not isinstance(candidates, list) or len(candidates) > len(plan.candidates):
                raise QualificationFailure(f"Resume evidence case {case_id} repeat candidates are invalid.")
            _validate_candidate_prefix(repeat, plan, repeat_index)
            if candidates and not isinstance(base, Mapping):
                raise QualificationFailure(f"Resume evidence case {case_id} has candidates without a base.")
            for execution_ordinal, candidate_record in enumerate(candidates):
                candidate = _candidate_order(plan, repeat_index)[execution_ordinal]
                _validate_candidate_record(candidate_record, candidate, repeat_index, execution_ordinal)
                _validate_candidate_against_base(candidate_record, _mapping(base, "repeat.base"), candidate)
        if observed_repeats != list(range(len(observed_repeats))):
            raise QualificationFailure(f"Resume evidence case {case_id} repeats do not match the execution prefix.")
    if len(set(observed_case_ids)) != len(observed_case_ids) or not set(observed_case_ids).issubset(case_definitions):
        raise QualificationFailure("Resume evidence case identities do not match the selected experiment cases.")


def _load_resume_evidence(
    output_path: Path,
    *,
    plan: SweepPlan,
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
        raw_evidence_bytes = output_path.read_bytes()
        raw_evidence = raw_evidence_bytes.decode("utf-8")
        evidence = dict(_loads_json_bytes(raw_evidence_bytes, "resume evidence"))
    except (OSError, UnicodeDecodeError) as error:
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
            "path": _relative_repository_path(binding.source_manifest_path, "Bound direct corpus manifest"),
            "corpus_id": binding.source_corpus_id,
            "sha256": binding.source_manifest_sha256,
        },
        "private_source_identity": dict(binding.private_source_identity),
        "toolchain": _toolchain_record(plan),
        "method": _method_record(plan),
        "environment": dict(environment),
        "selected_case_ids": [case.case_id for case in selected_cases],
        "candidates": [_candidate_plan_record(candidate) for candidate in plan.candidates],
    }
    for key, expected in expected_identity.items():
        if evidence.get(key) != expected:
            raise QualificationFailure(f"Resume evidence {key} does not match the current experiment identity.")
    _validate_resume_cases(evidence, plan, binding, {case.case_id: case for case in selected_cases})
    _assert_private_values_absent(evidence, private_paths)
    acceptance = evidence.get("acceptance")
    complete = isinstance(acceptance, Mapping) and acceptance.get("complete") is True
    planned_full = isinstance(acceptance, Mapping) and acceptance.get("planned_full_stress_subset") is True
    finalized = isinstance(acceptance, Mapping) and acceptance.get("finalized") is True
    if (complete or planned_full or finalized) and not _completed_resume_is_consistent(
        evidence,
        plan,
        binding,
        {case.case_id: case for case in selected_cases},
    ):
        raise QualificationFailure("Resume evidence completion claims contradict the recorded runs.")
    writable = bool(output_path.stat().st_mode & 0o222)
    if complete and planned_full:
        canonical = json.dumps(evidence, indent=2, sort_keys=True) + "\n"
        if raw_evidence != canonical:
            raise QualificationFailure("Completed resume evidence is not canonical JSON.")
        if not writable and not finalized:
            raise QualificationFailure("Unfinalized complete resume evidence is unexpectedly read-only.")
    else:
        if finalized:
            raise QualificationFailure("Incomplete resume evidence must not be marked finalized.")
        if not writable:
            raise QualificationFailure("Incomplete resume evidence is unexpectedly read-only.")
    return evidence


def _completed_resume_is_consistent(
    evidence: dict[str, object],
    plan: SweepPlan,
    binding: CorpusBinding,
    case_definitions: Mapping[str, CorpusCase],
) -> bool:
    cases = evidence.get("cases")
    if not isinstance(cases, list) or len(cases) != len(case_definitions):
        return False
    if not all(isinstance(case, Mapping) and _case_complete(case, plan) for case in cases):
        return False
    refreshed = copy.deepcopy(evidence)
    _refresh_summaries(refreshed, plan, binding, case_definitions)
    for key in ("case_summaries", "candidate_summaries", "monotonicity_findings", "acceptance"):
        if refreshed.get(key) != evidence.get(key):
            raise QualificationFailure("Completed resume evidence summaries contradict the recorded runs.")
    return True


def _validate_prepared_source(binding: CorpusBinding, prepared: PreparedCase) -> None:
    expected = binding.expected_case_sources[prepared.definition.case_id]
    if dict(prepared.source_evidence) != dict(expected):
        raise QualificationFailure(
            f"Prepared source identity does not match the checked binding for {prepared.definition.case_id}."
        )


def _case_record_template(definition: CorpusCase, prepared: PreparedCase) -> dict[str, object]:
    return {
        "id": definition.case_id,
        "tags": list(definition.tags),
        "quality_gate": definition.quality_gate,
        "source": dict(prepared.source_evidence),
        "prepared": {
            "duration_seconds": prepared.duration_seconds,
            "frame_count": prepared.frame_count,
            "eye_width": definition.output_eye_width,
            "eye_height": definition.output_eye_height,
            "frame_rate": definition.output_frame_rate,
            "source_sha256": sha256_file(prepared.source_path),
        },
        "repeats": [],
    }


def _repeat_record_template(plan: SweepPlan, repeat_index: int) -> dict[str, object]:
    return {
        "repeat_index": repeat_index,
        "order": list(plan.orders[repeat_index]),
        "base": None,
        "candidates": [],
    }


def _cases_selected_from_binding(binding: CorpusBinding) -> tuple[CorpusCase, ...]:
    manifest = load_manifest(binding.source_manifest_path)
    cases_by_id = {case.case_id: case for case in manifest.cases}
    return tuple(cases_by_id[case_id] for case_id in binding.selected_case_ids)


def _run_quality_sweep_unlocked(
    sweep_plan_path: Path,
    output_path: Path,
    work_directory: Path,
    *,
    resume: bool,
    case_ids: Sequence[str] = (),
) -> dict[str, object]:
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise QualificationFailure("File-upscale quality sweep requires macOS arm64.")
    source_git_sha = _git_head_from_clean_worktree()
    plan, binding, plan_sha256, binding_sha256 = load_sweep_plan(sweep_plan_path)
    if _require_head_tracked_file(sweep_plan_path, "Experiment plan") != plan.relative_path:
        raise QualificationFailure("Experiment plan repository identity changed during validation.")
    if _require_head_tracked_file(plan.binding_path, "Corpus binding") != binding.relative_path:
        raise QualificationFailure("Corpus binding repository identity changed during validation.")
    _require_head_tracked_file(binding.source_manifest_path, "Bound direct corpus manifest")
    _require_head_tracked_file(
        _repository_path(str(binding.private_source_identity["source"]), "Anchor plan"), "Direct anchor plan"
    )
    _require_head_tracked_file(plan.ffmpeg_manifest.path, "FFmpeg vendor manifest")
    _require_head_tracked_file(plan.fx_upscale_binary.path, "FX Upscale binary")
    for key in EXPECTED_TOOL_KEYS:
        _require_head_tracked_file(plan.bundled_tools[key].path, f"bundled tool {key}")
    for tool in (plan.fx_upscale_binary.path, *(plan.bundled_tools[key].path for key in EXPECTED_TOOL_KEYS)):
        if not tool.is_file() or not os.access(tool, os.X_OK):
            raise QualificationFailure(f"Required bundled tool is unavailable or not executable: {tool.name}")

    planned_cases = _cases_selected_from_binding(binding)
    if case_ids:
        if len(set(case_ids)) != len(case_ids):
            raise QualificationFailure("Subset case IDs must not contain duplicates.")
        unknown = sorted(set(case_ids) - set(binding.selected_case_ids))
        if unknown:
            raise QualificationFailure("Unknown or unplanned file-upscale case IDs: " + ", ".join(unknown))
        requested = set(case_ids)
        selected_cases = tuple(case for case in planned_cases if case.case_id in requested)
    else:
        selected_cases = planned_cases
    if not selected_cases:
        raise QualificationFailure("At least one file-upscale sweep case is required.")

    ffmpeg = _pinned_media_tool(plan, "ffmpeg")
    ffprobe = _pinned_media_tool(plan, "ffprobe")
    environment = _environment_evidence(plan, ffmpeg, ffprobe, source_git_sha)
    if environment["git_head"] != source_git_sha:
        raise QualificationFailure("File-upscale sweep environment Git identity changed during preflight.")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    work_directory = _prepare_owned_work_directory(work_directory, plan, plan_sha256)
    private_paths = _private_source_paths(selected_cases)
    if output_path.exists():
        if not resume:
            raise QualificationFailure(
                "File-upscale sweep output already exists; use --resume or choose a new output path."
            )
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
        evidence = _new_evidence(plan, binding, plan_sha256, binding_sha256, environment, selected_cases)
        _atomic_write(output_path, evidence, private_paths)

    case_definitions = {case.case_id: case for case in selected_cases}
    _completed_resume_is_consistent(evidence, plan, binding, case_definitions)

    for definition in selected_cases:
        existing_case = _case_record(evidence, definition.case_id)
        if existing_case is not None and _case_complete(existing_case, plan):
            continue
        case_work = (
            _owned_case_directory(work_directory, definition.case_id)
            if existing_case is not None
            else _reset_case_directory(work_directory, definition.case_id)
        )
        case_work.mkdir(parents=True, exist_ok=True)
        try:
            prepared = prepare_case(definition, case_work, ffmpeg=ffmpeg, ffprobe=ffprobe)
            _validate_prepared_source(binding, prepared)
        except (OSError, subprocess.SubprocessError, QualificationFailure, ValueError) as error:
            raise QualificationFailure(_safe_error_message(error, private_paths)) from None
        expected_case = _case_record_template(definition, prepared)
        prepared_record = _mapping(expected_case["prepared"], "prepared case metadata")
        if existing_case is None:
            existing_case = expected_case
            evidence["cases"].append(existing_case)
            evidence["updated_at"] = datetime.now(UTC).isoformat()
            _atomic_write(output_path, evidence, private_paths)
        else:
            for key in ("source", "prepared", "tags", "quality_gate"):
                if existing_case.get(key) != expected_case[key]:
                    raise QualificationFailure(
                        f"Prepared source identity changed while resuming case {definition.case_id}."
                    )

        repeats = existing_case.get("repeats")
        if not isinstance(repeats, list):
            raise QualificationFailure(f"Case {definition.case_id} repeats are invalid.")
        for repeat_index in range(plan.runs_per_candidate):
            repeat = _repeat_record(existing_case, repeat_index)
            if repeat is None:
                repeat = _repeat_record_template(plan, repeat_index)
                repeats.append(repeat)
                repeats.sort(key=lambda item: int(item["repeat_index"]))
                evidence["updated_at"] = datetime.now(UTC).isoformat()
                _atomic_write(output_path, evidence, private_paths)
            if _repeat_complete(repeat, plan, repeat_index):
                continue
            repeat_directory = case_work / f"repeat-{repeat_index + 1}"
            repeat_directory.mkdir(parents=True, exist_ok=True)
            base_path = repeat_directory / "generated-base.mov"
            base = repeat.get("base")
            if base is None:
                try:
                    base_path.unlink(missing_ok=True)
                    base = _record_base(ffmpeg, ffprobe, prepared, plan, repeat_index, base_path)
                except (OSError, subprocess.SubprocessError, QualificationFailure, ValueError) as error:
                    raise QualificationFailure(_safe_error_message(error, private_paths)) from None
                _validate_base_against_case(base, definition, prepared_record)
                repeat["base"] = base
                evidence["updated_at"] = datetime.now(UTC).isoformat()
                _atomic_write(output_path, evidence, private_paths)
            else:
                base = _mapping(base, "repeat.base")
                _validate_base_record(base, plan, repeat_index)
                _validate_base_against_case(base, definition, prepared_record)
                if not base_path.is_file() or sha256_file(base_path) != base["sha256"]:
                    has_candidate_record = any(
                        _candidate_record(repeat, candidate.candidate_id) is not None for candidate in plan.candidates
                    )
                    if has_candidate_record:
                        raise QualificationFailure(
                            "Resume requires the recorded base artifact for an incomplete repeat."
                        )
                    base_path.unlink(missing_ok=True)
                    base = _record_base(ffmpeg, ffprobe, prepared, plan, repeat_index, base_path)
                    _validate_base_against_case(base, definition, prepared_record)
                    repeat["base"] = base
                    evidence["updated_at"] = datetime.now(UTC).isoformat()
                    _atomic_write(output_path, evidence, private_paths)
            candidates = repeat.get("candidates")
            if not isinstance(candidates, list):
                raise QualificationFailure(f"Case {definition.case_id} repeat {repeat_index} candidates are invalid.")
            _validate_candidate_prefix(repeat, plan, repeat_index)
            for execution_ordinal, candidate in enumerate(_candidate_order(plan, repeat_index)):
                if _candidate_complete(repeat, plan, candidate, repeat_index):
                    continue
                try:
                    record = _record_candidate(
                        ffmpeg,
                        ffprobe,
                        prepared,
                        plan,
                        candidate,
                        repeat_index,
                        execution_ordinal,
                        base_path,
                        base,
                        repeat_directory,
                    )
                except (OSError, subprocess.SubprocessError, QualificationFailure, ValueError) as error:
                    raise QualificationFailure(_safe_error_message(error, private_paths)) from None
                candidates.append(record)
                _refresh_summaries(evidence, plan, binding, case_definitions)
                evidence["updated_at"] = datetime.now(UTC).isoformat()
                _atomic_write(output_path, evidence, private_paths)
                (repeat_directory / f"{candidate.candidate_id}-input.mov").unlink(missing_ok=True)
                (repeat_directory / f"{candidate.candidate_id}-upscaled.mov").unlink(missing_ok=True)
            if _repeat_complete(repeat, plan, repeat_index):
                shutil.rmtree(repeat_directory, ignore_errors=True)
                evidence["updated_at"] = datetime.now(UTC).isoformat()
                _refresh_summaries(evidence, plan, binding, case_definitions)
                _atomic_write(output_path, evidence, private_paths)
        if _case_complete(existing_case, plan):
            shutil.rmtree(case_work, ignore_errors=True)

    if _git_head_from_clean_worktree() != source_git_sha:
        raise QualificationFailure("File-upscale sweep Git identity changed before final receipt freeze.")
    final_plan, final_binding, final_plan_sha256, final_binding_sha256 = load_sweep_plan(sweep_plan_path)
    if (
        final_plan != plan
        or final_binding != binding
        or final_plan_sha256 != plan_sha256
        or final_binding_sha256 != binding_sha256
    ):
        raise QualificationFailure("File-upscale sweep plan or corpus binding changed before final receipt freeze.")
    final_environment = _environment_evidence(plan, ffmpeg, ffprobe, source_git_sha)
    if final_environment != environment:
        raise QualificationFailure("File-upscale sweep environment changed before final receipt freeze.")
    acceptance = _mapping(evidence.get("acceptance"), "acceptance")
    if (
        acceptance.get("complete") is True
        and acceptance.get("planned_full_stress_subset") is True
        and acceptance.get("finalized") is True
    ):
        _freeze_receipt(output_path)
        return evidence
    evidence["updated_at"] = datetime.now(UTC).isoformat()
    _refresh_summaries(evidence, plan, binding, case_definitions)
    acceptance = _mapping(evidence.get("acceptance"), "acceptance")
    if acceptance.get("complete") is True and acceptance.get("planned_full_stress_subset") is True:
        acceptance["finalized"] = True
    _atomic_write(output_path, evidence, private_paths)
    if acceptance.get("finalized") is True:
        _freeze_receipt(output_path)
    return evidence


def run_quality_sweep(
    sweep_plan_path: Path,
    output_path: Path,
    work_directory: Path,
    *,
    resume: bool,
    case_ids: Sequence[str] = (),
) -> dict[str, object]:
    with calibration_lock(output_path, work_directory):
        return _run_quality_sweep_unlocked(
            sweep_plan_path,
            output_path,
            work_directory,
            resume=resume,
            case_ids=case_ids,
        )


def exit_code_for_evidence(evidence: Mapping[str, object]) -> int:
    acceptance = evidence.get("acceptance")
    if not isinstance(acceptance, Mapping):
        raise QualificationFailure("File-upscale quality sweep acceptance record is missing.")
    if acceptance.get("decision_ready") is True:
        return 0
    if acceptance.get("complete") is True and acceptance.get("planned_full_stress_subset") is True:
        return 1
    return 3


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Measure the checked file-upscale quality response sweep without selecting public mappings."
    )
    parser.add_argument("--sweep-plan", type=Path, default=DEFAULT_SWEEP_PLAN)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--work-directory", type=Path, default=DEFAULT_WORK_DIRECTORY)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--case-id", action="append", default=[])
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    private_paths: tuple[Path, ...] = ()
    try:
        private_paths = _configured_private_paths()
        evidence = run_quality_sweep(
            args.sweep_plan.resolve(),
            args.output.absolute(),
            args.work_directory.absolute(),
            resume=args.resume,
            case_ids=args.case_id,
        )
        return exit_code_for_evidence(evidence)
    except (
        AssertionError,
        KeyError,
        OSError,
        TypeError,
        subprocess.SubprocessError,
        QualificationFailure,
        ValueError,
    ) as error:
        message = _safe_error_message(error, private_paths)
        print(f"File-upscale quality sweep failed: {message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
