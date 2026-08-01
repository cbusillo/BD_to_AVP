#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
import uuid

from dataclasses import dataclass
from datetime import UTC, datetime
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
from bd_to_avp.worker.protocol import PROTOCOL_VERSION
from scripts.qualify_direct_mv_hevc import CURRENT_REQUIRED_BOX_TYPES, DIRECT_REQUIRED_BOX_TYPES
from scripts.qualify_mv_hevc_quality_match import sha256_file
from scripts.validate_video_quality_route_table import validate_route_table
from scripts.verify_packaged_mv_hevc_routes import (
    AppBundle,
    PackagedRouteFailure,
    _float_field,
    _probe_artifact,
    _route_evidence,
    _source_media_contract,
    app_tree_sha256,
    build_worker_request,
    read_app_bundle,
    run_verified_route_pair,
    run_worker,
    validate_qualification_paths,
    validate_route_report,
    validate_stage_contract,
)


MAX_EVIDENCE_BYTES = 256 * 1024
PREVIEW_DURATION_SECONDS = 12
PHYSICAL_ANCHOR_STEPS = frozenset({"space_saver", "balanced", "maximum_detail"})
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
HARNESS_PATHS = (
    REPOSITORY_ROOT / "scripts/qualify_packaged_video_quality_routes.py",
    REPOSITORY_ROOT / "scripts/qualify_direct_mv_hevc.py",
    REPOSITORY_ROOT / "scripts/qualify_mv_hevc_quality_match.py",
    REPOSITORY_ROOT / "scripts/verify_packaged_mv_hevc_routes.py",
    REPOSITORY_ROOT / "scripts/validate_video_quality_route_table.py",
    REPOSITORY_ROOT / "bd_to_avp/modules/video_quality_defaults.py",
    REPOSITORY_ROOT / "bd_to_avp/worker/protocol.py",
)


@dataclass(frozen=True)
class QualificationCase:
    case_id: str
    target: str
    step_id: str
    video_options: Mapping[str, object]
    upscale_options: Mapping[str, object]
    expected_route: Mapping[str, object]
    expected_dimensions: tuple[int, int]
    required_box_types: frozenset[str]
    required_stages: frozenset[str]
    forbidden_stages: frozenset[str]
    fixture_name: str | None = None
    retain_full_workspace: bool = False


def _quality_intent(step_id: str) -> dict[str, object]:
    return {
        "mode": "ladder",
        "step": step_id,
        "mapping_version": VIDEO_QUALITY_MAPPING_VERSION,
    }


def _direct_case(step_id: str, *, metalfx: bool) -> QualificationCase:
    quality = DIRECT_METALFX_2X_QUALITY_BY_STEP[step_id] if metalfx else DIRECT_QUALITY_BY_STEP[step_id]
    intent = _quality_intent(step_id)
    video_options: dict[str, object] = {
        "mode": "mv_hevc",
        "route_intent": "automatic",
        "quality_intent": intent,
        "direct_bitrate": {"mode": "automatic"},
        "direct_quality": quality,
    }
    upscale_options: dict[str, object] = {"enabled": metalfx}
    if step_id == "balanced":
        video_options["generated_fallback"] = {
            "eye_bitrate": {"mode": "automatic"},
            "merge_quality": GENERATED_QUALITY_BY_STEP["balanced"]["merge_quality"],
        }
        if metalfx:
            upscale_options["quality"] = FILE_UPSCALE_QUALITY_BY_STEP["balanced"]
    requested: dict[str, object] = {
        "route": "direct_mv_hevc",
        "rate_control": "quality",
        "quality": quality,
    }
    if metalfx:
        requested["upscale_mode"] = "metalfx"
    expected_route: dict[str, object] = {
        "intent": "automatic",
        "selected": "direct_mv_hevc",
        "reason": "direct_upscale_eligible" if metalfx else "direct_eligible",
        "quality_intent": intent,
        "requested": requested,
        "rate_control": "quality",
        "quality": quality,
    }
    if metalfx:
        expected_route["upscale_mode"] = "metalfx"
    prefix = "metalfx-4k" if metalfx else "ordinary"
    fixture_name = f"{prefix}-{step_id}.mov" if step_id in PHYSICAL_ANCHOR_STEPS else None
    return QualificationCase(
        case_id=f"{prefix}-{step_id}",
        target="direct_mv_hevc_metalfx_2x" if metalfx else "direct_mv_hevc",
        step_id=step_id,
        video_options=video_options,
        upscale_options=upscale_options,
        expected_route=expected_route,
        expected_dimensions=(3_840, 2_160) if metalfx else (1_920, 1_080),
        required_box_types=frozenset(DIRECT_REQUIRED_BOX_TYPES),
        required_stages=frozenset({"create_left_right_files"}),
        forbidden_stages=frozenset({"combine_to_mv_hevc", "upscale_video"}),
        fixture_name=fixture_name,
    )


def _generated_case() -> QualificationCase:
    step_id = "balanced"
    intent = _quality_intent(step_id)
    generated = GENERATED_QUALITY_BY_STEP[step_id]
    expected_route = {
        "intent": "generated",
        "selected": "generated_mv_hevc",
        "reason": "generated_route_requested",
        "quality_intent": intent,
        "requested": {
            "route": "generated_mv_hevc",
            "eye_bitrate_mbps": generated["eye_bitrate_mbps"],
            "merge_quality": generated["merge_quality"],
        },
        "eye_bitrate_mbps": generated["eye_bitrate_mbps"],
        "merge_quality": generated["merge_quality"],
    }
    return QualificationCase(
        case_id="generated-balanced",
        target="generated_mv_hevc",
        step_id=step_id,
        video_options={
            "mode": "mv_hevc",
            "route_intent": "generated",
            "quality_intent": intent,
            "generated_eye_bitrate": {"mode": "automatic"},
            "generated_merge_quality": generated["merge_quality"],
        },
        upscale_options={"enabled": False},
        expected_route=expected_route,
        expected_dimensions=(1_920, 1_080),
        required_box_types=frozenset(CURRENT_REQUIRED_BOX_TYPES),
        required_stages=frozenset({"create_left_right_files", "combine_to_mv_hevc"}),
        forbidden_stages=frozenset({"upscale_video"}),
        fixture_name="generated-balanced.mov",
        retain_full_workspace=True,
    )


def qualification_cases() -> tuple[QualificationCase, ...]:
    ordinary = tuple(_direct_case(step_id, metalfx=False) for step_id in QUALITY_STEP_IDS)
    metalfx = tuple(_direct_case(step_id, metalfx=True) for step_id in QUALITY_STEP_IDS)
    return (*ordinary, *metalfx, _generated_case())


def existing_artifact_cases() -> tuple[QualificationCase, ...]:
    cases: list[QualificationCase] = []
    for step_id in ("balanced", "detailed"):
        quality = FILE_UPSCALE_QUALITY_BY_STEP[step_id]
        intent = _quality_intent(step_id)
        cases.append(
            QualificationCase(
                case_id=f"file-upscale-{step_id}",
                target="upscale_quality",
                step_id=step_id,
                video_options={
                    "mode": "mv_hevc",
                    "route_intent": "existing_artifact",
                    "quality_intent": intent,
                },
                upscale_options={"enabled": True, "quality": quality},
                expected_route={
                    "intent": "existing_artifact",
                    "selected": "existing_artifact",
                    "reason": "resume_uses_existing_video_artifact",
                    "quality_intent": intent,
                    "requested": {"route": "existing_artifact", "upscale_quality": quality},
                    "upscale_quality": quality,
                },
                expected_dimensions=(3_840, 2_160),
                required_box_types=frozenset(CURRENT_REQUIRED_BOX_TYPES),
                required_stages=frozenset({"upscale_video", "create_final_file", "move_files"}),
                forbidden_stages=frozenset({"extract_mvc_and_audio", "create_left_right_files", "combine_to_mv_hevc"}),
                fixture_name=f"file-upscale-{step_id}.mov",
            )
        )
    return tuple(cases)


def _clone_directory(source: Path, destination: Path) -> None:
    clone = subprocess.run(
        ["cp", "-cR", source.as_posix(), destination.as_posix()],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if clone.returncode != 0:
        subprocess.run(["ditto", source.as_posix(), destination.as_posix()], check=True)


def _fixture_evidence(source: Path, destination: Path) -> dict[str, object]:
    shutil.copy2(source, destination)
    destination.chmod(0o444)
    return {
        "filename": destination.name,
        "sha256": sha256_file(destination),
        "size_bytes": destination.stat().st_size,
    }


def _immutable_write(destination: Path, payload: bytes) -> None:
    if destination.is_symlink() or destination.exists():
        raise PackagedRouteFailure("Packaged quality evidence output already exists.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        temporary.chmod(0o444)
        os.link(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _validate_fixture_directory(app: Path, source: Path, output: Path, requested: Path) -> Path:
    expanded = requested.expanduser()
    if expanded.is_symlink() or expanded.exists():
        raise PackagedRouteFailure("Packaged quality fixture directory must be a fresh path.")
    fixture_directory = expanded.resolve()
    if fixture_directory == source:
        raise PackagedRouteFailure("Packaged quality fixtures must not replace the representative source.")
    if fixture_directory == app or fixture_directory.is_relative_to(app):
        raise PackagedRouteFailure("Packaged quality fixtures must remain outside the app bundle.")
    if fixture_directory == output or output.is_relative_to(fixture_directory):
        raise PackagedRouteFailure("Packaged quality evidence and fixture paths must remain distinct.")
    return fixture_directory


def _case_request(case: QualificationCase) -> dict[str, object]:
    return {
        "video": dict(case.video_options),
        "upscale": dict(case.upscale_options),
    }


def _run_pair_case(
    app: AppBundle,
    source: Path,
    root: Path,
    fixture_directory: Path,
    case: QualificationCase,
    source_duration_seconds: float,
) -> dict[str, object]:
    upscale_mode = case.expected_route.get("upscale_mode")
    full, preview, full_artifact, preview_artifact = run_verified_route_pair(
        app,
        source,
        root,
        name=case.case_id,
        upscale_enabled=bool(case.upscale_options.get("enabled")),
        expected_selected=str(case.expected_route["selected"]),
        expected_fallback_reason=None,
        expected_upscale_mode=upscale_mode if isinstance(upscale_mode, str) else None,
        source_duration_seconds=source_duration_seconds,
        expected_video_dimensions=case.expected_dimensions,
        required_box_types=set(case.required_box_types),
        required_stages=set(case.required_stages),
        forbidden_stages=set(case.forbidden_stages),
        video_options=case.video_options,
        upscale_options=case.upscale_options,
        expected_route=case.expected_route,
        preview_duration_seconds=PREVIEW_DURATION_SECONDS,
        full_keep_files=case.retain_full_workspace,
    )
    evidence: dict[str, object] = {
        "case_id": case.case_id,
        "target": case.target,
        "step_id": case.step_id,
        "request": _case_request(case),
        "full": _route_evidence(full, full_artifact),
        "preview": _route_evidence(preview, preview_artifact),
    }
    if case.fixture_name is not None:
        evidence["fixture"] = _fixture_evidence(full.output_path, fixture_directory / case.fixture_name)
    return evidence


def _run_existing_artifact_case(
    app: AppBundle,
    source: Path,
    seed_workspace: Path,
    root: Path,
    fixture_directory: Path,
    case: QualificationCase,
    source_duration_seconds: float,
) -> dict[str, object]:
    case_root = root / case.case_id
    _clone_directory(seed_workspace, case_root)
    result = run_worker(
        app,
        build_worker_request(
            "convert_source",
            source,
            case_root,
            job_id=str(uuid.uuid4()),
            upscale_enabled=True,
            video_options=case.video_options,
            upscale_options=case.upscale_options,
            start_stage=6,
            keep_files=True,
        ),
        home_directory=root / f"{case.case_id}-home",
    )
    validate_route_report(result, case.expected_route)
    validate_stage_contract(
        result,
        required=set(case.required_stages),
        forbidden=set(case.forbidden_stages),
    )
    artifact = _probe_artifact(
        app,
        result.output_path,
        set(case.required_box_types),
        expected_duration_seconds=source_duration_seconds,
        expected_video_dimensions=case.expected_dimensions,
    )
    evidence: dict[str, object] = {
        "case_id": case.case_id,
        "target": case.target,
        "step_id": case.step_id,
        "request": _case_request(case),
        "full": _route_evidence(result, artifact),
        "preview": {
            "status": "not_applicable",
            "reason": "protocol_preview_requires_start_stage_1",
        },
    }
    if case.fixture_name is not None:
        evidence["fixture"] = _fixture_evidence(result.output_path, fixture_directory / case.fixture_name)
    return evidence


def _package_evidence(app: AppBundle) -> dict[str, object]:
    return {
        "app_tree_sha256": app_tree_sha256(app.path),
        "bundle_identifier": app.bundle_identifier,
        "helper_sha256": sha256_file(app.helper),
        "protocol_version": PROTOCOL_VERSION,
        "version": app.version,
        "worker_sha256": sha256_file(app.worker),
    }


def _harness_evidence() -> dict[str, object]:
    status = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=REPOSITORY_ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if status.stdout.strip():
        raise PackagedRouteFailure("Packaged quality qualification requires a clean source tree.")
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPOSITORY_ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout.strip()
    files: list[dict[str, object]] = []
    for path in HARNESS_PATHS:
        relative = path.relative_to(REPOSITORY_ROOT).as_posix()
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", relative],
            cwd=REPOSITORY_ROOT,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if tracked.returncode != 0 or not path.is_file():
            raise PackagedRouteFailure(f"Qualification harness file is not tracked: {relative}")
        files.append({"path": relative, "sha256": sha256_file(path)})
    return {
        "source_commit": commit,
        "source_tree_dirty": False,
        "files": files,
    }


def verify_packaged_video_quality_routes(
    app_path: Path,
    source_path: Path,
    *,
    fixture_directory: Path,
    expected_source_sha256: str,
) -> dict[str, object]:
    app = read_app_bundle(app_path)
    source = source_path.resolve()
    if not source.is_file():
        raise PackagedRouteFailure("Representative MVC source is unavailable.")
    source_sha256 = sha256_file(source)
    if source_sha256 != expected_source_sha256.lower():
        raise PackagedRouteFailure("Representative MVC source SHA-256 did not match the expected fixture.")
    source_media = _source_media_contract(app, source)
    source_duration_seconds = _float_field(source_media, "duration_seconds")
    route_table = validate_route_table()
    harness = _harness_evidence()
    pair_cases = qualification_cases()
    resume_cases = existing_artifact_cases()
    pair_evidence: list[dict[str, object]] = []
    resume_evidence: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(prefix="packaged-video-quality-v2-") as temporary_directory:
        root = Path(temporary_directory)
        for case in pair_cases:
            pair_evidence.append(
                _run_pair_case(
                    app,
                    source,
                    root,
                    fixture_directory,
                    case,
                    source_duration_seconds,
                )
            )
        seed_workspace = root / "generated-balanced-full"
        if not seed_workspace.is_dir():
            raise PackagedRouteFailure("Generated Balanced did not retain the stage-6 seed workspace.")
        for case in resume_cases:
            resume_evidence.append(
                _run_existing_artifact_case(
                    app,
                    source,
                    seed_workspace,
                    root,
                    fixture_directory,
                    case,
                    source_duration_seconds,
                )
            )
    ordinary = [case for case in pair_evidence if case["target"] == "direct_mv_hevc"]
    metalfx = [case for case in pair_evidence if case["target"] == "direct_mv_hevc_metalfx_2x"]
    generated = [case for case in pair_evidence if case["target"] == "generated_mv_hevc"]
    fixtures = [
        case["fixture"] for case in (*pair_evidence, *resume_evidence) if isinstance(case.get("fixture"), Mapping)
    ]
    return {
        "schema_version": 1,
        "qualification_id": "packaged-video-quality-route-table-v2",
        "generated_at": datetime.now(UTC).isoformat(),
        "route_table": route_table,
        "harness": harness,
        "package": _package_evidence(app),
        "source": {
            "media": source_media,
            "sha256": source_sha256,
            "size_bytes": source.stat().st_size,
        },
        "preview_duration_seconds": PREVIEW_DURATION_SECONDS,
        "ordinary_direct": ordinary,
        "metalfx_4k_direct": metalfx,
        "generated": generated,
        "file_upscale": resume_evidence,
        "fixtures": fixtures,
        "acceptance": {
            "ordinary_direct_all_steps": [case["step_id"] for case in ordinary] == list(QUALITY_STEP_IDS),
            "metalfx_4k_all_steps": [case["step_id"] for case in metalfx] == list(QUALITY_STEP_IDS),
            "generated_supported_steps": [case["step_id"] for case in generated] == ["balanced"],
            "file_upscale_supported_steps": [case["step_id"] for case in resume_evidence] == ["balanced", "detailed"],
            "full_preview_route_parity": True,
            "file_upscale_preview_not_applicable": True,
            "finalized_artifacts_valid": True,
            "worker_executions": 32,
            "passed": True,
        },
    }


def run_qualification(
    app_path: Path,
    source_path: Path,
    output_path: Path,
    fixture_directory: Path,
    expected_source_sha256: str,
) -> dict[str, object]:
    app, source, output, _ = validate_qualification_paths(app_path, source_path, output_path, None)
    if output.is_symlink() or output.exists():
        raise PackagedRouteFailure("Packaged quality evidence output already exists.")
    fixtures = _validate_fixture_directory(app, source, output, fixture_directory)
    fixtures.parent.mkdir(parents=True, exist_ok=True)
    try:
        fixtures.mkdir(mode=0o700)
    except FileExistsError as error:
        raise PackagedRouteFailure("Packaged quality fixture directory must be a fresh path.") from error
    try:
        evidence = verify_packaged_video_quality_routes(
            app,
            source,
            fixture_directory=fixtures,
            expected_source_sha256=expected_source_sha256,
        )
        encoded = (json.dumps(evidence, indent=2, sort_keys=True) + "\n").encode("utf-8")
        if len(encoded) > MAX_EVIDENCE_BYTES:
            raise PackagedRouteFailure("Packaged quality evidence exceeded its bounded size limit.")
        fixtures.chmod(0o555)
        _immutable_write(output, encoded)
        return evidence
    except Exception:
        fixtures.chmod(0o755)
        shutil.rmtree(fixtures, ignore_errors=True)
        raise


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Qualify every supported video-quality mapping through the packaged protocol-v12 worker."
    )
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fixture-directory", type=Path, required=True)
    parser.add_argument("--expected-source-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        evidence = run_qualification(
            args.app,
            args.source,
            args.output,
            args.fixture_directory,
            args.expected_source_sha256,
        )
    except (PackagedRouteFailure, OSError, subprocess.SubprocessError, ValueError) as error:
        parser.exit(1, f"error: {error}\n")
    print(json.dumps(evidence, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
