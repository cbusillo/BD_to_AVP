#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import sys

from itertools import pairwise
from pathlib import Path
from typing import Mapping, Sequence

from bd_to_avp.modules.video_quality_defaults import (
    AUTOMATIC_DIRECT_QUALITY,
    AUTOMATIC_DIRECT_UPSCALE_QUALITY,
    AUTOMATIC_GENERATED_EYE_BITRATE_MBPS,
    AUTOMATIC_GENERATED_MERGE_QUALITY,
    DEFAULT_AV1_CRF,
    DEFAULT_UPSCALE_QUALITY,
)
from scripts.qualify_direct_mv_hevc import QualificationFailure
from scripts.qualify_mv_hevc_corpus import load_manifest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = REPOSITORY_ROOT / "docs/qualification/video-quality-ladder-v1.json"
EXPECTED_STEPS = (
    ("space_saver", 1, "Space Saver"),
    ("compact", 2, "Compact"),
    ("efficient", 3, "Efficient"),
    ("balanced", 4, "Balanced"),
    ("detailed", 5, "Detailed"),
    ("high_detail", 6, "High Detail"),
    ("maximum_detail", 7, "Maximum Detail"),
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
EXPECTED_METRICS = ("quality_delta", "output_size_ratio", "encode_time_seconds", "rationale")
EXPECTED_IDENTITIES = ("artifact_sha256", "source_git_sha", "fixture_manifest_sha256")
PHYSICAL_ANCHORS = ("space_saver", "balanced", "maximum_detail")
PHYSICAL_CHECKS = (
    "stereo_presentation",
    "beginning_seek",
    "middle_seek",
    "end_seek",
    "sustained_playback",
)
SHA256_LENGTH = 64
GIT_SHA_LENGTH = 40


class CalibrationError(ValueError):
    pass


def _target_contracts() -> dict[str, dict[str, object]]:
    return {
        "direct_mv_hevc": {
            "exposures": ("pending_calibration", "ladder"),
            "controls": (("quality", "number", 0.0, 1.0, "increase"),),
            "balanced": {"quality": AUTOMATIC_DIRECT_QUALITY},
            "sources": ("bd_to_avp.modules.video_route.AUTOMATIC_DIRECT_QUALITY",),
        },
        "direct_mv_hevc_metalfx_2x": {
            "exposures": ("pending_calibration", "ladder"),
            "controls": (("quality", "number", 0.0, 1.0, "increase"),),
            "balanced": {"quality": AUTOMATIC_DIRECT_UPSCALE_QUALITY},
            "sources": ("bd_to_avp.modules.video_route.AUTOMATIC_DIRECT_UPSCALE_QUALITY",),
        },
        "generated_mv_hevc": {
            "exposures": ("pending_calibration", "ladder"),
            "controls": (
                ("eye_bitrate_mbps", "integer", 1, 500, "increase"),
                ("merge_quality", "integer", 0, 100, "increase"),
            ),
            "balanced": {
                "eye_bitrate_mbps": AUTOMATIC_GENERATED_EYE_BITRATE_MBPS,
                "merge_quality": AUTOMATIC_GENERATED_MERGE_QUALITY,
            },
            "sources": (
                "bd_to_avp.modules.video_route.AUTOMATIC_GENERATED_EYE_BITRATE_MBPS",
                "bd_to_avp.modules.video_route.AUTOMATIC_GENERATED_MERGE_QUALITY",
            ),
        },
        "upscale_quality": {
            "exposures": ("pending_calibration", "ladder"),
            "controls": (("upscale_quality", "integer", 0, 100, "increase"),),
            "balanced": {"upscale_quality": DEFAULT_UPSCALE_QUALITY},
            "sources": ("bd_to_avp.modules.video_quality_defaults.DEFAULT_UPSCALE_QUALITY",),
        },
        "av1_sbs": {
            "exposures": ("custom_only", "ladder"),
            "controls": (("crf", "integer", 0, 63, "decrease"),),
            "balanced": {"crf": DEFAULT_AV1_CRF},
            "sources": ("bd_to_avp.modules.video_quality_defaults.DEFAULT_AV1_CRF",),
        },
    }


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise CalibrationError(f"{label} must be an object.")
    return value


def _array(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise CalibrationError(f"{label} must be an array.")
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CalibrationError(f"{label} must be a non-empty string.")
    return value.strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path) -> Mapping[str, object]:
    try:
        return _mapping(json.loads(path.read_text(encoding="utf-8")), "calibration manifest")
    except (OSError, json.JSONDecodeError) as error:
        raise CalibrationError(f"Could not read calibration manifest {path}: {error}") from error


def _repository_path(root: Path, relative_path: str) -> Path:
    candidate = (root / relative_path).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as error:
        raise CalibrationError(f"corpus path escapes the repository: {relative_path}") from error
    return candidate


def _validate_steps(document: Mapping[str, object]) -> tuple[str, ...]:
    steps = _array(document.get("steps"), "steps")
    observed: list[tuple[str, int, str]] = []
    for index, raw_step in enumerate(steps):
        step = _mapping(raw_step, f"steps[{index}]")
        step_id = _string(step.get("id"), f"steps[{index}].id")
        ordinal = step.get("ordinal")
        if type(ordinal) is not int:
            raise CalibrationError(f"steps[{index}].ordinal must be an integer.")
        observed.append((step_id, ordinal, _string(step.get("label"), f"steps[{index}].label")))
    if tuple(observed) != EXPECTED_STEPS:
        raise CalibrationError("steps must match the approved seven-step labels and order.")
    if document.get("balanced_step_id") != "balanced":
        raise CalibrationError("balanced_step_id must be 'balanced'.")
    custom = _mapping(document.get("custom"), "custom")
    expected_custom = {
        "id": "custom",
        "label": "Custom",
        "mode": "exact_route_controls",
        "separate_from_ladder": True,
    }
    if dict(custom) != expected_custom:
        raise CalibrationError("Custom must remain a separate exact-route-controls mode.")
    return tuple(step_id for step_id, _, _ in observed)


def _validate_corpus(document: Mapping[str, object], repository_root: Path) -> dict[str, object]:
    corpus = _mapping(document.get("corpus"), "corpus")
    relative_path = _string(corpus.get("path"), "corpus.path")
    corpus_path = _repository_path(repository_root, relative_path)
    try:
        parsed = load_manifest(corpus_path)
        digest = _sha256(corpus_path)
    except (OSError, QualificationFailure) as error:
        raise CalibrationError(f"Could not validate corpus manifest {relative_path}: {error}") from error
    if corpus.get("corpus_id") != parsed.corpus_id:
        raise CalibrationError("corpus_id does not match the referenced corpus manifest.")
    if corpus.get("sha256") != digest:
        raise CalibrationError("corpus sha256 does not match the referenced corpus manifest.")
    coverage = tuple(
        _string(item, "corpus.required_coverage")
        for item in _array(corpus.get("required_coverage"), "corpus.required_coverage")
    )
    if coverage != EXPECTED_COVERAGE or coverage != parsed.required_coverage:
        raise CalibrationError("corpus required_coverage does not match the approved representative corpus.")
    return {"path": relative_path, "corpus_id": parsed.corpus_id, "sha256": digest, "case_count": len(parsed.cases)}


def _validate_evidence_contract(document: Mapping[str, object]) -> None:
    contract = _mapping(document.get("evidence_contract"), "evidence_contract")
    for key, expected in (
        ("required_metrics", EXPECTED_METRICS),
        ("required_identities", EXPECTED_IDENTITIES),
        ("physical_anchor_step_ids", PHYSICAL_ANCHORS),
        ("physical_checks", PHYSICAL_CHECKS),
    ):
        observed = tuple(
            _string(item, f"evidence_contract.{key}") for item in _array(contract.get(key), f"evidence_contract.{key}")
        )
        if observed != expected:
            raise CalibrationError(f"evidence_contract.{key} does not match the required contract.")


def _validate_controls(
    target: Mapping[str, object], expected: Mapping[str, object], label: str
) -> dict[str, tuple[str, float, float, str]]:
    controls = _array(target.get("controls"), f"{label}.controls")
    observed: list[tuple[str, str, float, float, str]] = []
    for index, raw_control in enumerate(controls):
        control = _mapping(raw_control, f"{label}.controls[{index}]")
        observed.append(
            (
                _string(control.get("id"), f"{label}.controls[{index}].id"),
                _string(control.get("type"), f"{label}.controls[{index}].type"),
                control.get("minimum"),
                control.get("maximum"),
                _string(control.get("higher_step_direction"), f"{label}.controls[{index}].higher_step_direction"),
            )
        )
    if tuple(observed) != expected["controls"]:
        raise CalibrationError(f"{label}.controls does not match the route contract.")
    return {
        control_id: (kind, float(minimum), float(maximum), direction)
        for control_id, kind, minimum, maximum, direction in observed
    }


def _validate_values(
    values: object, controls: Mapping[str, tuple[str, float, float, str]], label: str
) -> dict[str, int | float]:
    raw_values = _mapping(values, label)
    if set(raw_values) != set(controls):
        raise CalibrationError(f"{label} must define exactly: {', '.join(controls)}.")
    validated: dict[str, int | float] = {}
    for control_id, (kind, minimum, maximum, _) in controls.items():
        value = raw_values[control_id]
        if kind == "integer" and (type(value) is not int):
            raise CalibrationError(f"{label}.{control_id} must be an integer.")
        if kind == "number" and type(value) not in (int, float):
            raise CalibrationError(f"{label}.{control_id} must be a number.")
        numeric = float(value)
        if not math.isfinite(numeric) or not minimum <= numeric <= maximum:
            raise CalibrationError(f"{label}.{control_id} must be between {minimum:g} and {maximum:g}.")
        validated[control_id] = value
    return validated


def _valid_hex(value: object, length: int) -> bool:
    return (
        isinstance(value, str) and len(value) == length and all(character in "0123456789abcdef" for character in value)
    )


def _validate_measured_evidence(
    evidence: object,
    label: str,
    *,
    fixture_manifest_sha256: str,
    physical_required: bool,
) -> Mapping[str, object]:
    document = _mapping(evidence, label)
    allowed_keys = {
        "artifact_sha256",
        "source_git_sha",
        "fixture_manifest_sha256",
        "quality_delta",
        "output_size_ratio",
        "encode_time_seconds",
        "rationale",
        "physical_playback",
    }
    if set(document) - allowed_keys:
        raise CalibrationError(f"{label} contains unsupported fields.")
    for key, length in (
        ("artifact_sha256", SHA256_LENGTH),
        ("source_git_sha", GIT_SHA_LENGTH),
        ("fixture_manifest_sha256", SHA256_LENGTH),
    ):
        if not _valid_hex(document.get(key), length):
            raise CalibrationError(f"{label}.{key} must be a lowercase {length}-character hexadecimal identity.")
    if document["fixture_manifest_sha256"] != fixture_manifest_sha256:
        raise CalibrationError(f"{label}.fixture_manifest_sha256 must match the approved corpus manifest.")
    for key in ("quality_delta", "output_size_ratio", "encode_time_seconds"):
        value = document.get(key)
        if type(value) not in (int, float) or not math.isfinite(float(value)):
            raise CalibrationError(f"{label}.{key} must be a finite number.")
    if float(document["output_size_ratio"]) <= 0 or float(document["encode_time_seconds"]) <= 0:
        raise CalibrationError(f"{label} size ratio and encode time must be positive.")
    _string(document.get("rationale"), f"{label}.rationale")
    physical = document.get("physical_playback")
    if physical_required and physical is None:
        raise CalibrationError(f"{label}.physical_playback is required.")
    if physical is not None:
        physical_document = _mapping(physical, f"{label}.physical_playback")
        allowed_physical_keys = {"candidate_sha256", "device_model", "os_version", *PHYSICAL_CHECKS}
        if set(physical_document) != allowed_physical_keys:
            raise CalibrationError(f"{label}.physical_playback must contain exactly the public evidence fields.")
        if not _valid_hex(physical_document.get("candidate_sha256"), SHA256_LENGTH):
            raise CalibrationError(f"{label}.physical_playback.candidate_sha256 must identify the tested candidate.")
        _string(physical_document.get("device_model"), f"{label}.physical_playback.device_model")
        _string(physical_document.get("os_version"), f"{label}.physical_playback.os_version")
        for check in PHYSICAL_CHECKS:
            if physical_document.get(check) is not True:
                raise CalibrationError(f"{label}.physical_playback.{check} must be true.")
    return document


def _validate_monotonicity(
    target_id: str,
    mappings: Sequence[tuple[int, str, Mapping[str, int | float], Mapping[str, object] | None]],
    controls: Mapping[str, tuple[str, float, float, str]],
) -> None:
    for previous, current in pairwise(mappings):
        _, previous_step, previous_values, previous_evidence = previous
        _, current_step, current_values, current_evidence = current
        changed = False
        for control_id, (_, _, _, direction) in controls.items():
            previous_value = float(previous_values[control_id])
            current_value = float(current_values[control_id])
            if direction == "increase" and current_value < previous_value:
                raise CalibrationError(f"{target_id} {control_id} decreases from {previous_step} to {current_step}.")
            if direction == "decrease" and current_value > previous_value:
                raise CalibrationError(f"{target_id} {control_id} increases from {previous_step} to {current_step}.")
            changed = changed or current_value != previous_value
        if not changed:
            raise CalibrationError(f"{target_id} has identical route values at {previous_step} and {current_step}.")
        if previous_evidence is not None and current_evidence is not None:
            if float(current_evidence["quality_delta"]) < float(previous_evidence["quality_delta"]):
                raise CalibrationError(
                    f"{target_id} measured quality decreases from {previous_step} to {current_step}."
                )
            if float(current_evidence["output_size_ratio"]) < float(previous_evidence["output_size_ratio"]):
                raise CalibrationError(
                    f"{target_id} measured storage decreases from {previous_step} to {current_step}."
                )


def _validate_targets(
    document: Mapping[str, object], step_ids: tuple[str, ...], fixture_manifest_sha256: str
) -> tuple[list[dict[str, object]], list[str], list[str]]:
    expected_targets = _target_contracts()
    targets = _array(document.get("calibration_targets"), "calibration_targets")
    seen: set[str] = set()
    summaries: list[dict[str, object]] = []
    blockers: list[str] = []
    deferred: list[str] = []
    for index, raw_target in enumerate(targets):
        target = _mapping(raw_target, f"calibration_targets[{index}]")
        target_id = _string(target.get("id"), f"calibration_targets[{index}].id")
        if target_id in seen:
            raise CalibrationError(f"duplicate calibration target: {target_id}")
        seen.add(target_id)
        expected = expected_targets.get(target_id)
        if expected is None:
            raise CalibrationError(f"unsupported calibration target: {target_id}")
        label = f"calibration target {target_id}"
        _string(target.get("label"), f"{label}.label")
        exposure = _string(target.get("ladder_exposure"), f"{label}.ladder_exposure")
        if exposure not in expected["exposures"]:
            raise CalibrationError(f"{label}.ladder_exposure must be one of {expected['exposures']!r}.")
        if target_id == "av1_sbs":
            if exposure == "custom_only" and target.get("blocker") != "#409":
                raise CalibrationError("AV1 custom-only exposure must remain linked to blocker #409.")
            if exposure == "ladder" and target.get("blocker") is not None:
                raise CalibrationError("AV1 ladder exposure must remove the resolved #409 blocker.")
        controls = _validate_controls(target, expected, label)
        raw_mappings = _array(target.get("mappings"), f"{label}.mappings")
        if len(raw_mappings) != len(step_ids):
            raise CalibrationError(f"{label}.mappings must define all seven steps.")
        mapped: list[tuple[int, str, Mapping[str, int | float], Mapping[str, object] | None]] = []
        statuses: dict[str, str] = {}
        for ordinal, raw_mapping in enumerate(raw_mappings, start=1):
            mapping = _mapping(raw_mapping, f"{label}.mappings[{ordinal - 1}]")
            step_id = _string(mapping.get("step_id"), f"{label}.mappings[{ordinal - 1}].step_id")
            if step_id != step_ids[ordinal - 1]:
                raise CalibrationError(f"{label}.mappings must follow the approved step order.")
            status = _string(mapping.get("status"), f"{label}.{step_id}.status")
            if status not in {"needs_calibration", "baseline_anchor", "measured", "qualified"}:
                raise CalibrationError(f"{label}.{step_id}.status is unsupported.")
            statuses[step_id] = status
            if step_id == "balanced" and status == "needs_calibration":
                raise CalibrationError(f"{label} must retain an exact Balanced production anchor.")
            if status == "needs_calibration":
                if mapping.get("values") is not None or mapping.get("evidence") is not None:
                    raise CalibrationError(f"{label}.{step_id} cannot carry guessed values or evidence.")
                continue
            values = _validate_values(mapping.get("values"), controls, f"{label}.{step_id}.values")
            evidence: Mapping[str, object] | None
            if status == "baseline_anchor":
                if step_id != "balanced" or dict(values) != expected["balanced"]:
                    raise CalibrationError(f"{label} baseline_anchor must be the exact Balanced production default.")
                anchor = _mapping(mapping.get("evidence"), f"{label}.{step_id}.evidence")
                if set(anchor) != {"kind", "sources", "rationale"}:
                    raise CalibrationError(f"{label}.{step_id}.evidence must contain exactly the anchor fields.")
                if anchor.get("kind") != "current_default":
                    raise CalibrationError(f"{label}.{step_id}.evidence.kind must be 'current_default'.")
                sources = tuple(
                    _string(item, f"{label}.{step_id}.evidence.sources")
                    for item in _array(anchor.get("sources"), f"{label}.{step_id}.evidence.sources")
                )
                if sources != expected["sources"]:
                    raise CalibrationError(f"{label}.{step_id}.evidence.sources does not match production defaults.")
                _string(anchor.get("rationale"), f"{label}.{step_id}.evidence.rationale")
                evidence = {"quality_delta": 0.0, "output_size_ratio": 1.0}
            else:
                if step_id == "balanced" and dict(values) != expected["balanced"]:
                    raise CalibrationError(f"{label} Balanced values must reproduce production defaults exactly.")
                evidence = _validate_measured_evidence(
                    mapping.get("evidence"),
                    f"{label}.{step_id}.evidence",
                    fixture_manifest_sha256=fixture_manifest_sha256,
                    physical_required=status == "qualified" and step_id in PHYSICAL_ANCHORS,
                )
                if step_id == "balanced" and (
                    float(evidence["quality_delta"]) != 0.0 or float(evidence["output_size_ratio"]) != 1.0
                ):
                    raise CalibrationError(
                        f"{label} Balanced evidence must define the 0.0 quality / 1.0 size baseline."
                    )
            mapped.append((ordinal, step_id, values, evidence))
        _validate_monotonicity(target_id, mapped, controls)
        if target_id == "av1_sbs":
            if exposure == "custom_only":
                invalid = [
                    step_id
                    for step_id, status in statuses.items()
                    if step_id != "balanced" and status != "needs_calibration"
                ]
                if invalid:
                    raise CalibrationError("AV1 must remain exact-CRF-only until #409 supplies physical M5 evidence.")
                deferred.append("av1_sbs: #409 physical M5 evidence is required before optional ladder exposure")
            else:
                incomplete = [step_id for step_id, status in statuses.items() if status != "qualified"]
                if incomplete:
                    raise CalibrationError("AV1 ladder exposure requires all seven qualified mappings.")
        else:
            incomplete = [step_id for step_id, status in statuses.items() if status != "qualified"]
            if incomplete:
                blockers.append(f"{target_id}: qualify {', '.join(incomplete)}")
                if exposure == "ladder":
                    raise CalibrationError(f"{target_id} ladder exposure requires all seven qualified mappings.")
            if exposure != "ladder":
                blockers.append(f"{target_id}: keep ladder hidden until calibration is complete")
        summaries.append(
            {
                "id": target_id,
                "ladder_exposure": exposure,
                "qualified_steps": sum(status == "qualified" for status in statuses.values()),
                "measured_steps": sum(status == "measured" for status in statuses.values()),
                "baseline_anchor_steps": sum(status == "baseline_anchor" for status in statuses.values()),
                "needs_calibration_steps": sum(status == "needs_calibration" for status in statuses.values()),
            }
        )
    if seen != set(expected_targets):
        missing = sorted(set(expected_targets) - seen)
        raise CalibrationError("missing calibration targets: " + ", ".join(missing))
    return summaries, blockers, deferred


def _git_state(repository_root: Path) -> tuple[str, bool | None]:
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repository_root,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=normal"],
            cwd=repository_root,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except OSError:
        return "unavailable", None
    git_sha = head if _valid_hex(head, GIT_SHA_LENGTH) else "unavailable"
    dirty = bool(status.stdout.strip()) if status.returncode == 0 else None
    return git_sha, dirty


def validate_manifest(
    manifest_path: Path = DEFAULT_MANIFEST, *, repository_root: Path = REPOSITORY_ROOT
) -> dict[str, object]:
    document = _load_json(manifest_path)
    if document.get("schema_version") != 1:
        raise CalibrationError("schema_version must be 1.")
    if document.get("calibration_id") != "route-relative-video-quality-v1":
        raise CalibrationError("calibration_id must be 'route-relative-video-quality-v1'.")
    step_ids = _validate_steps(document)
    corpus = _validate_corpus(document, repository_root)
    _validate_evidence_contract(document)
    targets, blockers, deferred = _validate_targets(document, step_ids, str(corpus["sha256"]))
    source_git_sha, source_tree_dirty = _git_state(repository_root)
    return {
        "schema_version": 1,
        "calibration_id": document["calibration_id"],
        "source_git_sha": source_git_sha,
        "source_tree_dirty": source_tree_dirty,
        "manifest": {"path": manifest_path.name, "sha256": _sha256(manifest_path)},
        "corpus": corpus,
        "step_ids": list(step_ids),
        "custom_mode": "exact_route_controls",
        "targets": targets,
        "complete": not blockers,
        "blockers": blockers,
        "deferred": deferred,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate the route-relative video quality ladder contract.")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--require-complete", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        receipt = validate_manifest(args.manifest.resolve())
    except CalibrationError as error:
        print(f"video quality ladder validation failed: {error}", file=sys.stderr)
        return 1
    rendered = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    if args.require_complete and not receipt["complete"]:
        print("video quality ladder calibration is incomplete.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
