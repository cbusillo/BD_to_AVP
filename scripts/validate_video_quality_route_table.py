#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import sys

from pathlib import Path
from typing import Mapping, Sequence

from bd_to_avp.modules.video_quality_defaults import (
    DIRECT_METALFX_2X_QUALITY_BY_STEP,
    DIRECT_QUALITY_BY_STEP,
    FILE_UPSCALE_QUALITY_BY_STEP,
    GENERATED_QUALITY_BY_STEP,
    QUALITY_STEP_IDS,
    VIDEO_QUALITY_MAPPING_VERSION,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROUTE_TABLE = REPOSITORY_ROOT / "docs/qualification/video-quality-route-table-v2.json"
EXPECTED_STEP_LABELS = (
    "Space Saver",
    "Compact",
    "Efficient",
    "Balanced",
    "Detailed",
    "High Detail",
    "Maximum Detail",
)
EXPECTED_EVIDENCE_IDS = {
    "ordinary_direct_confirmation",
    "metalfx_direct_confirmation",
    "generated_full_corpus_confirmation",
    "file_upscale_confirmation",
}
EXPECTED_ROUTES = {
    "direct_mv_hevc": (
        "candidate_seven_step",
        "ordinary_direct_confirmation",
        {step_id: {"quality": value} for step_id, value in DIRECT_QUALITY_BY_STEP.items()},
    ),
    "direct_mv_hevc_metalfx_2x": (
        "candidate_seven_step",
        "metalfx_direct_confirmation",
        {step_id: {"quality": value} for step_id, value in DIRECT_METALFX_2X_QUALITY_BY_STEP.items()},
    ),
    "generated_mv_hevc": (
        "candidate_supported_subset",
        "generated_full_corpus_confirmation",
        GENERATED_QUALITY_BY_STEP,
    ),
    "upscale_quality": (
        "candidate_supported_subset",
        "file_upscale_confirmation",
        {step_id: {"quality": value} for step_id, value in FILE_UPSCALE_QUALITY_BY_STEP.items()},
    ),
    "av1_sbs": ("custom_only", None, {}),
}


class RouteTableError(ValueError):
    pass


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise RouteTableError(f"{label} must be an object.")
    return value


def _array(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise RouteTableError(f"{label} must be an array.")
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RouteTableError(f"{label} must be a non-empty string.")
    return value.strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hex_identity(value: object, length: int, label: str) -> str:
    identity = _string(value, label)
    if len(identity) != length or any(character not in "0123456789abcdef" for character in identity):
        raise RouteTableError(f"{label} must be a lowercase {length}-character hexadecimal identity.")
    return identity


def _repository_file(binding: Mapping[str, object], label: str, repository_root: Path) -> None:
    relative_path = _string(binding.get("path"), f"{label}.path")
    path = (repository_root / relative_path).resolve()
    try:
        path.relative_to(repository_root.resolve())
    except ValueError as error:
        raise RouteTableError(f"{label}.path must remain inside the repository.") from error
    if not path.is_file():
        raise RouteTableError(f"{label}.path does not identify a repository file.")
    expected_sha256 = _hex_identity(binding.get("sha256"), 64, f"{label}.sha256")
    if _sha256(path) != expected_sha256:
        raise RouteTableError(f"{label}.sha256 does not match the referenced file.")


def _validate_steps(document: Mapping[str, object]) -> None:
    steps = _array(document.get("steps"), "steps")
    observed = []
    for index, raw_step in enumerate(steps, start=1):
        step = _mapping(raw_step, f"steps[{index - 1}]")
        observed.append((step.get("id"), step.get("ordinal"), step.get("label")))
    expected = list(zip(QUALITY_STEP_IDS, range(1, 8), EXPECTED_STEP_LABELS, strict=True))
    if observed != expected:
        raise RouteTableError("steps must match the stable seven-step identity and label order.")


def _validate_evidence(document: Mapping[str, object], repository_root: Path) -> None:
    receipts = _mapping(document.get("evidence_receipts"), "evidence_receipts")
    if set(receipts) != EXPECTED_EVIDENCE_IDS:
        raise RouteTableError("evidence_receipts must contain the four frozen objective receipts.")
    for receipt_id, raw_receipt in receipts.items():
        receipt = _mapping(raw_receipt, f"evidence_receipts.{receipt_id}")
        if receipt.get("kind") not in {
            "objective_full_corpus_confirmation",
            "negative_full_corpus_confirmation",
        }:
            raise RouteTableError(f"evidence_receipts.{receipt_id}.kind is unsupported.")
        _hex_identity(receipt.get("receipt_sha256"), 64, f"evidence_receipts.{receipt_id}.receipt_sha256")
        _hex_identity(receipt.get("source_git_sha"), 40, f"evidence_receipts.{receipt_id}.source_git_sha")
        if receipt.get("required_file_mode") != "0444":
            raise RouteTableError(f"evidence_receipts.{receipt_id} must require immutable mode 0444.")
        _repository_file(
            _mapping(receipt.get("plan"), f"evidence_receipts.{receipt_id}.plan"),
            f"evidence_receipts.{receipt_id}.plan",
            repository_root,
        )
        _repository_file(
            _mapping(receipt.get("corpus"), f"evidence_receipts.{receipt_id}.corpus"),
            f"evidence_receipts.{receipt_id}.corpus",
            repository_root,
        )


def _validate_routes(document: Mapping[str, object]) -> dict[str, list[str]]:
    routes = _array(document.get("routes"), "routes")
    if len(routes) != len(EXPECTED_ROUTES):
        raise RouteTableError("routes must contain every supported target exactly once.")
    supported: dict[str, list[str]] = {}
    seen: set[str] = set()
    for route_index, raw_route in enumerate(routes):
        route = _mapping(raw_route, f"routes[{route_index}]")
        route_id = _string(route.get("id"), f"routes[{route_index}].id")
        if route_id in seen or route_id not in EXPECTED_ROUTES:
            raise RouteTableError(f"routes contains an unsupported or duplicate target: {route_id}.")
        seen.add(route_id)
        expected_exposure, expected_receipt, expected_values = EXPECTED_ROUTES[route_id]
        if route.get("exposure") != expected_exposure:
            raise RouteTableError(f"{route_id}.exposure does not match the candidate contract.")
        if expected_receipt is None:
            if route.get("evidence_receipt_id") is not None:
                raise RouteTableError(f"{route_id} must not claim objective ladder evidence.")
        elif route.get("evidence_receipt_id") != expected_receipt:
            raise RouteTableError(f"{route_id}.evidence_receipt_id changed.")
        if route_id == "av1_sbs" and route.get("blocker") != "#409":
            raise RouteTableError("AV1 must remain Custom-only behind #409.")

        mappings = _array(route.get("mappings"), f"{route_id}.mappings")
        if len(mappings) != len(QUALITY_STEP_IDS):
            raise RouteTableError(f"{route_id}.mappings must define every step.")
        supported_steps: list[str] = []
        for mapping_index, step_id in enumerate(QUALITY_STEP_IDS):
            mapping = _mapping(mappings[mapping_index], f"{route_id}.mappings[{mapping_index}]")
            if mapping.get("step_id") != step_id:
                raise RouteTableError(f"{route_id}.mappings must follow the stable step order.")
            expected = expected_values.get(step_id)
            if expected is None:
                if mapping.get("status") != "unsupported" or mapping.get("values") is not None:
                    raise RouteTableError(f"{route_id}.{step_id} must remain unavailable without an alias.")
            else:
                if mapping.get("status") != "candidate" or mapping.get("values") != expected:
                    raise RouteTableError(f"{route_id}.{step_id} does not match the frozen candidate mapping.")
                supported_steps.append(step_id)
        supported[route_id] = supported_steps
    if seen != set(EXPECTED_ROUTES):
        raise RouteTableError("routes is missing a required target.")
    return supported


def _validate_fallback(document: Mapping[str, object]) -> None:
    fallbacks = _array(document.get("fallbacks"), "fallbacks")
    if len(fallbacks) != 1:
        raise RouteTableError("fallbacks must define exactly one direct-to-generated policy.")
    fallback = _mapping(fallbacks[0], "fallbacks[0]")
    expected = {
        "from_route_ids": ["direct_mv_hevc", "direct_mv_hevc_metalfx_2x"],
        "to_route_id": "generated_mv_hevc",
        "timing": "pre_input",
        "supported_step_ids": ["balanced"],
        "generated_values": GENERATED_QUALITY_BY_STEP["balanced"],
        "file_upscale_values_when_requested": {"quality": FILE_UPSCALE_QUALITY_BY_STEP["balanced"]},
        "unsupported_step_action": "fail_pre_input_without_alias",
    }
    if dict(fallback) != expected:
        raise RouteTableError("direct-to-generated fallback must remain Balanced-only and fail closed.")


def validate_route_table(
    path: Path = DEFAULT_ROUTE_TABLE,
    *,
    repository_root: Path = REPOSITORY_ROOT,
) -> dict[str, object]:
    try:
        document = _mapping(json.loads(path.read_text(encoding="utf-8")), "route table")
    except (OSError, json.JSONDecodeError) as error:
        raise RouteTableError(f"Could not read route table {path}: {error}") from error
    if document.get("schema_version") != 1:
        raise RouteTableError("schema_version must be 1.")
    if document.get("route_table_id") != "route-relative-video-quality-v2":
        raise RouteTableError("route_table_id must be 'route-relative-video-quality-v2'.")
    if document.get("mapping_version") != VIDEO_QUALITY_MAPPING_VERSION:
        raise RouteTableError("mapping_version does not match the runtime quality catalog.")
    if document.get("status") != "candidate_pending_qualification" or document.get("qualification_issue") != "#422":
        raise RouteTableError("the frozen table must remain a candidate pending #422 qualification.")
    custom = _mapping(document.get("custom"), "custom")
    if dict(custom) != {
        "id": "custom",
        "mode": "exact_route_controls",
        "available_for_every_route": True,
        "retained_independently_from_guided_intent": True,
    }:
        raise RouteTableError("Custom must preserve exact route controls independently from guided intent.")
    _validate_steps(document)
    _validate_evidence(document, repository_root)
    supported = _validate_routes(document)
    _validate_fallback(document)
    limits = _array(document.get("qualification_limits"), "qualification_limits")
    if len(limits) != 3 or any(not isinstance(item, str) or not item.strip() for item in limits):
        raise RouteTableError("qualification_limits must retain the three fail-closed release boundaries.")
    return {
        "schema_version": 1,
        "route_table_id": document["route_table_id"],
        "mapping_version": document["mapping_version"],
        "status": document["status"],
        "qualification_issue": document["qualification_issue"],
        "route_table_sha256": _sha256(path),
        "supported_step_ids": supported,
        "fallback_supported_step_ids": ["balanced"],
        "complete": True,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate the frozen route-aware video quality candidate table.")
    parser.add_argument("--route-table", type=Path, default=DEFAULT_ROUTE_TABLE)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        receipt = validate_route_table(args.route_table)
    except RouteTableError as error:
        print(f"video quality route table validation failed: {error}", file=sys.stderr)
        return 1
    payload = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
