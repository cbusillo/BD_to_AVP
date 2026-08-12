#!/usr/bin/env python3

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import platform
import plistlib
import stat
import shutil
import subprocess
import sys
import time

from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from typing import Iterator, Mapping, Sequence
from uuid import NAMESPACE_URL, uuid5

from bd_to_avp.modules.audio_mode import AudioMode
from bd_to_avp.modules.config import Stage, config
from bd_to_avp.modules.process import start_process
from bd_to_avp.modules.video_mode import VideoMode
from bd_to_avp.modules.video_quality_defaults import AUTOMATIC_DIRECT_QUALITY
from bd_to_avp.modules.video_route import (
    DirectMVHEVCCapability,
    ResolvedVideoRoute,
    VideoRouteKind,
    resolve_video_route,
)
from bd_to_avp.worker.operations import apply_video_route_to_config, configured_conversion
from bd_to_avp.worker.protocol import (
    PROTOCOL_VERSION,
    AudioOptions,
    BitrateMode,
    BitrateOptions,
    EncodingOptions,
    JobDestination,
    JobOptions,
    JobSource,
    JobSpec,
    SubtitleMode,
    SubtitleOptions,
    UpscaleOptions,
    VideoOptions,
    VideoRouteIntent,
    WorkerOperation,
    WorkerSourceKind,
)
from scripts.artifact_identity import app_tree_sha256
from scripts.qualify_direct_mv_hevc import BOX_TYPE_PATTERN, DIRECT_REQUIRED_BOX_TYPES, verify_seeks
from scripts.qualify_mv_hevc_quality_match import sha256_file
from scripts.verify_apple_media import inspect_apple_media_conversion


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PLAN = REPOSITORY_ROOT / "docs/qualification/direct-mv-hevc-anchor-plan-v1.json"
PACKAGE_BIN_RELATIVE_PATH = Path("Contents/Resources/app/bd_to_avp/bin")
INFO_RELATIVE_PATH = Path("Contents/Info.plist")
ROOT_MARKER = ".bd-to-avp-direct-anchor-root.json"
LOCK_NAME = ".bd-to-avp-direct-anchor.lock"
RECEIPT_NAME = "receipt.json"
CHECKLIST_NAME = "VISION-PRO-CHECKLIST.md"
PHYSICAL_TEMPLATE_NAME = "physical-evidence-template.json"
BUILD_METADATA_NAME = "build.json"
EVIDENCE_SCHEMA_VERSION = 1
MEDIA_DURATION_TOLERANCE_FRAMES = 2
AUDIO_START_TOLERANCE_SECONDS = 0.1
AUDIO_END_TOLERANCE_SECONDS = 0.25
CAPABILITY_KEYS = frozenset(
    {
        "metalfx_2x_mv_hevc_supported",
        "metalfx_spatial_scaling_supported",
        "pixel_transfer_2x_mv_hevc_supported",
        "schema_version",
        "stereo_mv_hevc_encode_supported",
    }
)
VALIDATION_FLAGS = frozenset(
    {
        "apple_passthrough_passed",
        "audio_decode_passed",
        "duration_passed",
        "seek_decode_passed",
        "spatial_boxes_passed",
        "stream_contract_passed",
    }
)


class AnchorQualificationFailure(RuntimeError):
    pass


@dataclass(frozen=True)
class SourceContract:
    path_env: str
    sha256: str
    size_bytes: int
    duration_seconds: float
    frame_rate: str
    frame_count: int
    width: int
    height: int
    pixel_format: str


@dataclass(frozen=True)
class PriorEvidenceContract:
    evidence_id: str
    path_env: str
    sha256: str
    sweep_id: str


@dataclass(frozen=True)
class ProductionOptions:
    audio_mode: AudioMode
    audio_bitrate_kbps: int
    audio_preferred_language: str
    subtitles: SubtitleMode
    crop_black_bars: bool
    swap_eyes: bool
    upscale: bool
    fov: int


@dataclass(frozen=True)
class ExecutionPolicy:
    minimum_initial_free_bytes: int
    reserve_free_bytes: int
    transient_copy_count: int


@dataclass(frozen=True)
class AnchorCandidate:
    candidate_id: str
    quality: float
    position: str
    maximum_size_ratio: float
    rationale: str

    @property
    def artifact_filename(self) -> str:
        return f"direct-mv-hevc-{self.candidate_id}.mov"


@dataclass(frozen=True)
class AnchorPlan:
    qualification_id: str
    source: SourceContract
    prior_evidence: tuple[PriorEvidenceContract, ...]
    production_options: ProductionOptions
    execution: ExecutionPolicy
    candidates: tuple[AnchorCandidate, ...]
    relative_path: str | None = None


@dataclass(frozen=True)
class PackagedTools:
    app: Path
    package_bin: Path
    encoder: Path
    edge264: Path
    ffmpeg: Path
    ffprobe: Path
    mp4box: Path
    spatial_media_tool: Path
    bundle_identifier: str
    version: str
    build_version: str
    app_tree_sha256: str
    build_metadata_sha256: str
    source_commit: str
    base_main_commit: str
    built_at_utc: str
    code_signature: str


@dataclass(frozen=True)
class FileSnapshot:
    device: int
    inode: int
    size_bytes: int
    modified_ns: int


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise AnchorQualificationFailure(f"{label} must be an object.")
    return value


def _array(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise AnchorQualificationFailure(f"{label} must be an array.")
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AnchorQualificationFailure(f"{label} must be a non-empty string.")
    return value.strip()


def _integer(value: object, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or int(value) < minimum:
        raise AnchorQualificationFailure(f"{label} must be an integer of at least {minimum}.")
    return int(value)


def _number(value: object, label: str, *, minimum: float = 0) -> float:
    if type(value) not in (int, float) or not math.isfinite(float(value)) or float(value) < minimum:
        raise AnchorQualificationFailure(f"{label} must be a finite number of at least {minimum}.")
    return float(value)


def _sha256(value: object, label: str) -> str:
    digest = _string(value, label)
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise AnchorQualificationFailure(f"{label} must be a lowercase SHA-256 identity.")
    return digest


def _git_sha(value: object, label: str) -> str:
    digest = _string(value, label)
    if len(digest) != 40 or any(character not in "0123456789abcdef" for character in digest):
        raise AnchorQualificationFailure(f"{label} must be a lowercase full Git SHA.")
    return digest


def _boolean(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise AnchorQualificationFailure(f"{label} must be a boolean.")
    return bool(value)


def parse_anchor_plan(raw: object) -> AnchorPlan:
    document = _mapping(raw, "anchor plan")
    if document.get("schema_version") != 1:
        raise AnchorQualificationFailure("anchor plan schema_version must be 1.")
    if document.get("target_id") != "direct_mv_hevc":
        raise AnchorQualificationFailure("anchor plan target_id must be direct_mv_hevc.")
    if document.get("purpose") != "physical_anchor_candidates_not_ladder_mappings":
        raise AnchorQualificationFailure("anchor plan must remain candidate evidence rather than ladder mappings.")

    source_document = _mapping(document.get("source"), "source")
    source = SourceContract(
        path_env=_string(source_document.get("path_env"), "source.path_env"),
        sha256=_sha256(source_document.get("sha256"), "source.sha256"),
        size_bytes=_integer(source_document.get("size_bytes"), "source.size_bytes", minimum=1),
        duration_seconds=_number(source_document.get("duration_seconds"), "source.duration_seconds", minimum=0.001),
        frame_rate=_string(source_document.get("frame_rate"), "source.frame_rate"),
        frame_count=_integer(source_document.get("frame_count"), "source.frame_count", minimum=1),
        width=_integer(source_document.get("width"), "source.width", minimum=1),
        height=_integer(source_document.get("height"), "source.height", minimum=1),
        pixel_format=_string(source_document.get("pixel_format"), "source.pixel_format"),
    )
    if not source.path_env.startswith("BD_TO_AVP_"):
        raise AnchorQualificationFailure("source.path_env must use the BD_TO_AVP_ prefix.")
    try:
        frame_rate = Fraction(source.frame_rate)
    except (ValueError, ZeroDivisionError) as error:
        raise AnchorQualificationFailure("source.frame_rate must be a positive rational value.") from error
    if frame_rate <= 0:
        raise AnchorQualificationFailure("source.frame_rate must be positive.")
    expected_video_duration = source.frame_count / float(frame_rate)
    if abs(expected_video_duration - source.duration_seconds) > 0.1:
        raise AnchorQualificationFailure("source frame count, frame rate, and duration do not agree.")

    prior_document = _mapping(document.get("prior_evidence"), "prior_evidence")
    prior_evidence: list[PriorEvidenceContract] = []
    for evidence_id in ("coarse", "upper"):
        evidence_document = _mapping(prior_document.get(evidence_id), f"prior_evidence.{evidence_id}")
        prior_evidence.append(
            PriorEvidenceContract(
                evidence_id=evidence_id,
                path_env=_string(evidence_document.get("path_env"), f"prior_evidence.{evidence_id}.path_env"),
                sha256=_sha256(evidence_document.get("sha256"), f"prior_evidence.{evidence_id}.sha256"),
                sweep_id=_string(evidence_document.get("sweep_id"), f"prior_evidence.{evidence_id}.sweep_id"),
            )
        )
        if not prior_evidence[-1].path_env.startswith("BD_TO_AVP_"):
            raise AnchorQualificationFailure(f"prior_evidence.{evidence_id}.path_env must use the BD_TO_AVP_ prefix.")

    options_document = _mapping(document.get("production_options"), "production_options")
    try:
        audio_mode = AudioMode(_string(options_document.get("audio_mode"), "production_options.audio_mode"))
        subtitle_mode = SubtitleMode(_string(options_document.get("subtitles"), "production_options.subtitles"))
    except ValueError as error:
        raise AnchorQualificationFailure("anchor production options use an unsupported mode.") from error
    options = ProductionOptions(
        audio_mode=audio_mode,
        audio_bitrate_kbps=_integer(
            options_document.get("audio_bitrate_kbps"), "production_options.audio_bitrate_kbps", minimum=1
        ),
        audio_preferred_language=_string(
            options_document.get("audio_preferred_language"), "production_options.audio_preferred_language"
        ),
        subtitles=subtitle_mode,
        crop_black_bars=_boolean(options_document.get("crop_black_bars"), "production_options.crop_black_bars"),
        swap_eyes=_boolean(options_document.get("swap_eyes"), "production_options.swap_eyes"),
        upscale=_boolean(options_document.get("upscale"), "production_options.upscale"),
        fov=_integer(options_document.get("fov"), "production_options.fov", minimum=1),
    )
    if options.audio_mode is not AudioMode.AUTOMATIC or options.subtitles is not SubtitleMode.OFF:
        raise AnchorQualificationFailure("anchor production options must use Automatic audio and disabled subtitles.")
    if options.upscale or options.crop_black_bars or options.swap_eyes:
        raise AnchorQualificationFailure("anchor production options must not alter geometry or eye order.")
    if options.audio_bitrate_kbps != 384 or options.audio_preferred_language != "eng" or options.fov != 90:
        raise AnchorQualificationFailure("anchor production options must preserve the reviewed release defaults.")

    execution_document = _mapping(document.get("execution"), "execution")
    execution = ExecutionPolicy(
        minimum_initial_free_bytes=_integer(
            execution_document.get("minimum_initial_free_bytes"), "execution.minimum_initial_free_bytes", minimum=1
        ),
        reserve_free_bytes=_integer(
            execution_document.get("reserve_free_bytes"), "execution.reserve_free_bytes", minimum=1
        ),
        transient_copy_count=_integer(
            execution_document.get("transient_copy_count"), "execution.transient_copy_count", minimum=2
        ),
    )
    if execution.minimum_initial_free_bytes < 96 * 1024**3 or execution.reserve_free_bytes < 32 * 1024**3:
        raise AnchorQualificationFailure("anchor execution storage guards are below the reviewed safety floor.")

    candidates: list[AnchorCandidate] = []
    for index, candidate_raw in enumerate(_array(document.get("candidates"), "candidates")):
        candidate_document = _mapping(candidate_raw, f"candidates[{index}]")
        candidate_id = _string(candidate_document.get("id"), f"candidates[{index}].id")
        if len(candidate_id) != 4 or not candidate_id.startswith("q") or not candidate_id[1:].isdigit():
            raise AnchorQualificationFailure(f"candidates[{index}].id must use qNNN format.")
        quality = _number(candidate_document.get("quality"), f"candidates[{index}].quality")
        if quality > 1:
            raise AnchorQualificationFailure(f"candidates[{index}].quality must not exceed 1.")
        candidates.append(
            AnchorCandidate(
                candidate_id=candidate_id,
                quality=quality,
                position=_string(candidate_document.get("position"), f"candidates[{index}].position"),
                maximum_size_ratio=_number(
                    candidate_document.get("maximum_size_ratio"),
                    f"candidates[{index}].maximum_size_ratio",
                    minimum=0.001,
                ),
                rationale=_string(candidate_document.get("rationale"), f"candidates[{index}].rationale"),
            )
        )
    if [candidate.candidate_id for candidate in candidates] != ["q070", "q085", "q040"]:
        raise AnchorQualificationFailure("anchor candidates must run in the reviewed q070, q085, q040 order.")
    if [candidate.quality for candidate in candidates] != [0.7, 0.85, 0.4]:
        raise AnchorQualificationFailure("anchor candidate qualities must remain 0.7, 0.85, and 0.4.")
    if [candidate.position for candidate in candidates] != ["balanced", "high", "low"]:
        raise AnchorQualificationFailure("anchor candidate positions must remain balanced, high, and low.")

    return AnchorPlan(
        qualification_id=_string(document.get("qualification_id"), "qualification_id"),
        source=source,
        prior_evidence=tuple(prior_evidence),
        production_options=options,
        execution=execution,
        candidates=tuple(candidates),
    )


def _repository_relative_file(path: Path, label: str) -> str:
    resolved = path.resolve()
    try:
        relative_path = resolved.relative_to(REPOSITORY_ROOT).as_posix()
    except ValueError as error:
        raise AnchorQualificationFailure(f"{label} must be inside the repository.") from error
    tracked = subprocess.run(
        ["git", "-C", REPOSITORY_ROOT, "ls-files", "--error-unmatch", "--", relative_path],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if tracked.returncode != 0:
        raise AnchorQualificationFailure(f"{label} must be tracked in the recorded source commit.")
    committed = subprocess.run(
        ["git", "-C", REPOSITORY_ROOT, "show", f"HEAD:{relative_path}"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout
    try:
        working = resolved.read_bytes()
    except OSError as error:
        raise AnchorQualificationFailure(f"Could not read {label}.") from error
    if committed != working:
        raise AnchorQualificationFailure(f"{label} bytes must match the recorded source commit.")
    return relative_path


def load_anchor_plan(path: Path) -> tuple[AnchorPlan, str]:
    relative_path = _repository_relative_file(path, "Anchor plan")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AnchorQualificationFailure("Could not read the anchor plan.") from error
    parsed = parse_anchor_plan(raw)
    return replace(parsed, relative_path=relative_path), sha256_file(path)


def clean_source_git_sha() -> str:
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=normal"],
        cwd=REPOSITORY_ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()
    if status:
        raise AnchorQualificationFailure("Full-length anchor evidence requires a clean source worktree.")
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPOSITORY_ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()


def runtime_evidence() -> dict[str, object]:
    python_executable = Path(sys.executable).resolve()
    return {
        "implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "python_executable_sha256": sha256_file(python_executable),
        "pyproject_sha256": sha256_file(REPOSITORY_ROOT / "pyproject.toml"),
        "uv_lock_sha256": sha256_file(REPOSITORY_ROOT / "uv.lock"),
    }


def file_snapshot(path: Path) -> FileSnapshot:
    metadata = path.stat()
    return FileSnapshot(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        size_bytes=metadata.st_size,
        modified_ns=metadata.st_mtime_ns,
    )


def _private_file_from_environment(environment_name: str, label: str) -> Path:
    raw_path = os.environ.get(environment_name)
    if not raw_path:
        raise AnchorQualificationFailure(f"{label} requires environment variable {environment_name}.")
    path = Path(raw_path).expanduser().resolve()
    if not path.is_file():
        raise AnchorQualificationFailure(f"{label} is unavailable.")
    return path


def _probe_json(command: Sequence[str | Path], label: str) -> Mapping[str, object]:
    try:
        completed = subprocess.run(
            [str(argument) for argument in command],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        payload = json.loads(completed.stdout)
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as error:
        raise AnchorQualificationFailure(f"Could not inspect {label}.") from error
    if not isinstance(payload, Mapping):
        raise AnchorQualificationFailure(f"{label} inspection did not return an object.")
    return payload


def inspect_source(ffprobe: Path, source_path: Path, contract: SourceContract) -> dict[str, object]:
    if source_path.stat().st_size != contract.size_bytes:
        raise AnchorQualificationFailure("Private source size does not match the anchor plan.")
    if sha256_file(source_path) != contract.sha256:
        raise AnchorQualificationFailure("Private source SHA-256 does not match the anchor plan.")
    probe = _probe_json(
        [
            ffprobe,
            "-v",
            "error",
            "-count_packets",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=avg_frame_rate,nb_read_packets,width,height,pix_fmt:format=duration",
            "-of",
            "json",
            source_path,
        ],
        "private MVC source",
    )
    streams = probe.get("streams")
    format_data = probe.get("format")
    if not isinstance(streams, list) or len(streams) != 1 or not isinstance(streams[0], Mapping):
        raise AnchorQualificationFailure("Private MVC source did not report one primary video stream.")
    if not isinstance(format_data, Mapping):
        raise AnchorQualificationFailure("Private MVC source did not report container timing.")
    stream = streams[0]
    try:
        duration_seconds = float(format_data["duration"])
        frame_count = int(stream["nb_read_packets"])
    except (KeyError, TypeError, ValueError) as error:
        raise AnchorQualificationFailure("Private MVC source timing was invalid.") from error
    observed = {
        "sha256": contract.sha256,
        "size_bytes": contract.size_bytes,
        "container_duration_seconds": duration_seconds,
        "frame_rate": stream.get("avg_frame_rate"),
        "frame_count": frame_count,
        "width": stream.get("width"),
        "height": stream.get("height"),
        "pixel_format": stream.get("pix_fmt"),
        "paths_recorded": False,
        "titles_recorded": False,
    }
    expected = {
        "frame_rate": contract.frame_rate,
        "frame_count": contract.frame_count,
        "width": contract.width,
        "height": contract.height,
        "pixel_format": contract.pixel_format,
    }
    for key, value in expected.items():
        if observed[key] != value:
            raise AnchorQualificationFailure(f"Private MVC source {key} does not match the anchor plan.")
    if abs(duration_seconds - contract.duration_seconds) > 0.001:
        raise AnchorQualificationFailure("Private MVC source duration does not match the anchor plan.")
    return observed


def inspect_prior_receipts(plan: AnchorPlan) -> dict[str, object]:
    evidence: dict[str, object] = {}
    for contract in plan.prior_evidence:
        path = _private_file_from_environment(contract.path_env, f"{contract.evidence_id} sweep receipt")
        if sha256_file(path) != contract.sha256:
            raise AnchorQualificationFailure(f"{contract.evidence_id} sweep receipt SHA-256 does not match the plan.")
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise AnchorQualificationFailure(f"Could not read {contract.evidence_id} sweep receipt.") from error
        if not isinstance(receipt, Mapping) or receipt.get("sweep_id") != contract.sweep_id:
            raise AnchorQualificationFailure(f"{contract.evidence_id} sweep receipt identity is invalid.")
        acceptance = receipt.get("acceptance")
        if (
            not isinstance(acceptance, Mapping)
            or acceptance.get("passed") is not True
            or acceptance.get("ladder_evidence_ready") is not False
            or acceptance.get("ladder_mapping_selected") is not False
        ):
            raise AnchorQualificationFailure(
                f"{contract.evidence_id} sweep receipt is not accepted exploratory evidence."
            )
        evidence[contract.evidence_id] = {
            "sha256": contract.sha256,
            "sweep_id": contract.sweep_id,
            "source_git_sha": receipt.get("source_git_sha"),
            "sweep_plan_sha256": (
                receipt.get("sweep_plan", {}).get("sha256") if isinstance(receipt.get("sweep_plan"), Mapping) else None
            ),
        }
    return evidence


def _code_signature_class(app_path: Path) -> str:
    completed = subprocess.run(
        ["codesign", "-dv", "--verbose=4", app_path],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    output = completed.stdout + completed.stderr
    if completed.returncode != 0:
        return "unavailable"
    if "Signature=adhoc" in output:
        return "ad_hoc"
    if "Authority=" in output:
        return "identified"
    return "other"


def inspect_packaged_app(app_path: Path, expected_source_commit: str) -> PackagedTools:
    app = app_path.resolve()
    info_path = app / INFO_RELATIVE_PATH
    build_metadata_path = app.parent / BUILD_METADATA_NAME
    if not info_path.is_file():
        raise AnchorQualificationFailure("Published ad-hoc app is missing Info.plist.")
    if build_metadata_path.is_symlink() or not build_metadata_path.is_file():
        raise AnchorQualificationFailure("Published ad-hoc app is missing immutable build metadata.")
    try:
        with info_path.open("rb") as info_file:
            info = plistlib.load(info_file)
        build_metadata = json.loads(build_metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, plistlib.InvalidFileException) as error:
        raise AnchorQualificationFailure("Could not read published ad-hoc app metadata.") from error
    if not isinstance(build_metadata, Mapping):
        raise AnchorQualificationFailure("Published ad-hoc app build metadata is invalid.")
    package_bin = app / PACKAGE_BIN_RELATIVE_PATH
    tools = {
        "encoder": package_bin / "mv-hevc-encoder",
        "edge264": package_bin / "edge264_test",
        "ffmpeg": package_bin / "ffmpeg",
        "ffprobe": package_bin / "ffprobe",
        "mp4box": package_bin / "MP4Box",
        "spatial_media_tool": package_bin / "spatial-media-kit-tool",
    }
    for label, path in tools.items():
        if not path.is_file() or not os.access(path, os.X_OK):
            raise AnchorQualificationFailure(f"Published ad-hoc app is missing executable {label}.")
    bundle_identifier = info.get("CFBundleIdentifier")
    version = info.get("CFBundleShortVersionString")
    build_version = info.get("CFBundleVersion")
    if not isinstance(bundle_identifier, str) or not bundle_identifier:
        raise AnchorQualificationFailure("Published ad-hoc app bundle identifier is unavailable.")
    if not isinstance(version, str) or not version:
        raise AnchorQualificationFailure("Published ad-hoc app version is unavailable.")
    if not isinstance(build_version, str) or not build_version:
        raise AnchorQualificationFailure("Published ad-hoc app build version is unavailable.")
    tree_sha256 = app_tree_sha256(app)
    source_commit = _git_sha(build_metadata.get("source_commit"), "build metadata source_commit")
    base_main_commit = _git_sha(build_metadata.get("base_main_commit"), "build metadata base_main_commit")
    built_at_utc = _string(build_metadata.get("built_at_utc"), "build metadata built_at_utc")
    if source_commit != expected_source_commit:
        raise AnchorQualificationFailure("Published ad-hoc app was not built from the recorded source commit.")
    if build_metadata.get("app_tree_sha256") != tree_sha256:
        raise AnchorQualificationFailure("Published ad-hoc app tree does not match its build metadata.")
    if build_metadata.get("short_version") != version or build_metadata.get("build_version") != build_version:
        raise AnchorQualificationFailure("Published ad-hoc app version does not match its build metadata.")
    if (
        build_metadata.get("signing") != "ad-hoc local"
        or build_metadata.get("release_status") != "not a production-signed release"
    ):
        raise AnchorQualificationFailure("Full-length anchors require a published ad-hoc build, not release evidence.")
    code_signature = _code_signature_class(app)
    if code_signature != "ad_hoc":
        raise AnchorQualificationFailure("Full-length anchors require an ad-hoc code signature.")
    return PackagedTools(
        app=app,
        package_bin=package_bin,
        encoder=tools["encoder"],
        edge264=tools["edge264"],
        ffmpeg=tools["ffmpeg"],
        ffprobe=tools["ffprobe"],
        mp4box=tools["mp4box"],
        spatial_media_tool=tools["spatial_media_tool"],
        bundle_identifier=bundle_identifier,
        version=version,
        build_version=build_version,
        app_tree_sha256=tree_sha256,
        build_metadata_sha256=sha256_file(build_metadata_path),
        source_commit=source_commit,
        base_main_commit=base_main_commit,
        built_at_utc=built_at_utc,
        code_signature=code_signature,
    )


def tool_bundle_evidence(tools: PackagedTools) -> dict[str, object]:
    return {
        "app_tree_sha256": tools.app_tree_sha256,
        "base_main_commit": tools.base_main_commit,
        "build_metadata_sha256": tools.build_metadata_sha256,
        "build_version": tools.build_version,
        "built_at_utc": tools.built_at_utc,
        "bundle_identifier": tools.bundle_identifier,
        "code_signature": tools.code_signature,
        "encoder_sha256": sha256_file(tools.encoder),
        "edge264_sha256": sha256_file(tools.edge264),
        "ffmpeg_sha256": sha256_file(tools.ffmpeg),
        "ffprobe_sha256": sha256_file(tools.ffprobe),
        "mp4box_sha256": sha256_file(tools.mp4box),
        "source_commit": tools.source_commit,
        "spatial_media_tool_sha256": sha256_file(tools.spatial_media_tool),
        "version": tools.version,
    }


def probe_packaged_capability(tools: PackagedTools) -> tuple[DirectMVHEVCCapability, dict[str, object]]:
    probe = _probe_json([tools.encoder, "--capability-probe"], "packaged direct capability")
    if set(probe) != CAPABILITY_KEYS:
        raise AnchorQualificationFailure("Published encoder capability schema is unexpected.")
    if probe.get("schema_version") != 1 or probe.get("stereo_mv_hevc_encode_supported") is not True:
        raise AnchorQualificationFailure("Published encoder does not support direct stereo MV-HEVC encoding.")
    if any(type(probe[key]) is not bool for key in CAPABILITY_KEYS - {"schema_version"}):
        raise AnchorQualificationFailure("Published encoder capability flags must be boolean.")
    metalfx_supported = probe.get("metalfx_2x_mv_hevc_supported") is True
    safe_probe = dict(sorted(probe.items()))
    return DirectMVHEVCCapability(True, "packaged_capability", metalfx_supported), safe_probe


@contextmanager
def configured_package_tools(tools: PackagedTools) -> Iterator[None]:
    fields = {
        "FFMPEG_PATH": tools.ffmpeg,
        "FFPROBE_PATH": tools.ffprobe,
        "MP4BOX_PATH": tools.mp4box,
        "EDGE264_TEST_PATH": tools.edge264,
        "SPATIAL_MEDIA_PATH": tools.spatial_media_tool,
        "MV_HEVC_ENCODER_PATH": tools.encoder,
    }
    previous_fields = {field: getattr(config, field) for field in fields}
    previous_path = os.environ.get("PATH")
    os.environ["PATH"] = os.pathsep.join([tools.package_bin.as_posix(), previous_path or ""])
    try:
        for field, value in fields.items():
            setattr(config, field, value)
        yield
    finally:
        for field, value in previous_fields.items():
            setattr(config, field, value)
        if previous_path is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = previous_path


def build_job(plan: AnchorPlan, source_path: Path, destination_path: Path, candidate: AnchorCandidate) -> JobSpec:
    options = plan.production_options
    encoding = EncodingOptions(
        audio=AudioOptions(options.audio_mode, options.audio_bitrate_kbps, options.audio_preferred_language),
        video=VideoOptions(
            mode=VideoMode.MV_HEVC,
            route_intent=VideoRouteIntent.AUTOMATIC,
            direct_bitrate=BitrateOptions(BitrateMode.AUTOMATIC),
            direct_quality=AUTOMATIC_DIRECT_QUALITY,
        ),
        upscale=UpscaleOptions(enabled=options.upscale),
        fov=options.fov,
        frame_rate="",
        resolution="",
        crop_black_bars=options.crop_black_bars,
        swap_eyes=options.swap_eyes,
        subtitles=SubtitleOptions(options.subtitles, options.audio_preferred_language),
    )
    return JobSpec(
        protocol_version=PROTOCOL_VERSION,
        job_id=str(uuid5(NAMESPACE_URL, f"{plan.qualification_id}:{candidate.candidate_id}")),
        operation=WorkerOperation.CONVERT_SOURCE,
        source=JobSource(WorkerSourceKind.DIRECT_FILE, source_path),
        destination=JobDestination(destination_path),
        encoding=encoding,
        job=JobOptions(
            start_stage=Stage.CREATE_MKV.value,
            keep_files=False,
            overwrite=True,
            remove_original=False,
            continue_on_error=False,
            software_encoder=False,
            output_commands=False,
            keep_awake=True,
        ),
    )


def candidate_route(job: JobSpec, candidate: AnchorCandidate, capability: DirectMVHEVCCapability) -> ResolvedVideoRoute:
    if job.encoding is None or job.job is None:
        raise AnchorQualificationFailure("Anchor job omitted encoding options.")
    automatic = resolve_video_route(job.encoding, job.job, capability_probe=lambda: capability)
    if (
        automatic.selected is not VideoRouteKind.DIRECT_MV_HEVC
        or automatic.direct_quality != 0.7
        or automatic.direct_bitrate_mbps is not None
    ):
        raise AnchorQualificationFailure("Production Automatic did not resolve to the qualified direct q0.7 route.")
    return replace(
        automatic,
        reason="full_length_quality_anchor_candidate",
        direct_bitrate_mbps=None,
        direct_quality=candidate.quality,
    )


def required_free_bytes(baseline_size_bytes: int, candidate: AnchorCandidate, policy: ExecutionPolicy) -> int:
    return math.ceil(
        baseline_size_bytes * candidate.maximum_size_ratio * policy.transient_copy_count + policy.reserve_free_bytes
    )


def _atomic_write_json(path: Path, document: Mapping[str, object]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def _has_symlink_component(path: Path) -> bool:
    return any(component.is_symlink() for component in (path, *path.parents))


def _owned_directory(path: Path, label: str) -> Path:
    if path.is_symlink():
        raise AnchorQualificationFailure(f"{label} must not be a symlink.")
    if path.exists() and not path.is_dir():
        raise AnchorQualificationFailure(f"{label} must be a directory.")
    path.mkdir(exist_ok=True)
    return path


def _prepare_anchor_root(
    root: Path,
    marker: Mapping[str, object],
    *,
    forbidden_paths: Sequence[Path],
    allow_create: bool,
) -> Path:
    requested = root.expanduser().absolute()
    if _has_symlink_component(requested):
        raise AnchorQualificationFailure("Anchor output root must not contain symlink components.")
    resolved = requested.resolve()
    if resolved in {Path("/").resolve(), Path.home().resolve()} or _paths_overlap(resolved, REPOSITORY_ROOT):
        raise AnchorQualificationFailure("Anchor output root must be a dedicated non-root directory.")
    if any(_paths_overlap(resolved, path.resolve()) for path in forbidden_paths):
        raise AnchorQualificationFailure("Anchor output root overlaps a protected source or app path.")
    marker_path = resolved / ROOT_MARKER
    if marker_path.is_symlink():
        raise AnchorQualificationFailure("Anchor output ownership marker must not be a symlink.")
    if resolved.exists():
        if marker_path.is_file():
            try:
                observed = json.loads(marker_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise AnchorQualificationFailure("Could not validate anchor output ownership.") from error
            if observed != marker:
                raise AnchorQualificationFailure("Anchor output root belongs to a different qualification identity.")
        elif any(resolved.iterdir()):
            raise AnchorQualificationFailure("Anchor output root is non-empty and has no ownership marker.")
        elif not allow_create:
            raise AnchorQualificationFailure("Anchor output root has no ownership marker.")
        else:
            _atomic_write_json(marker_path, marker)
    elif not allow_create:
        raise AnchorQualificationFailure("Anchor output root is unavailable.")
    else:
        resolved.mkdir(parents=True)
        _atomic_write_json(marker_path, marker)
    for name, label in (("work", "Anchor work directory"), ("artifacts", "Anchor artifact directory")):
        path = resolved / name
        if allow_create:
            _owned_directory(path, label)
        elif path.is_symlink() or not path.is_dir():
            raise AnchorQualificationFailure(f"{label} is unavailable or unsafe.")
    return resolved


def _safe_candidate_work_root(root: Path, candidate_id: str) -> Path:
    if len(candidate_id) != 4 or not candidate_id.startswith("q") or not candidate_id[1:].isdigit():
        raise AnchorQualificationFailure("Unsafe anchor candidate work identifier.")
    work_parent = _owned_directory(root / "work", "Anchor work directory")
    candidate_root = work_parent / candidate_id
    if candidate_root.is_symlink():
        raise AnchorQualificationFailure("Anchor candidate work root must not be a symlink.")
    if candidate_root.exists() and not candidate_root.is_dir():
        raise AnchorQualificationFailure("Anchor candidate work root must be a directory.")
    return candidate_root


def _reset_candidate_work_root(root: Path, candidate_id: str) -> Path:
    candidate_root = _safe_candidate_work_root(root, candidate_id)
    if candidate_root.exists():
        shutil.rmtree(candidate_root)
    candidate_root.mkdir(parents=True)
    return candidate_root


@contextmanager
def anchor_lock(root: Path) -> Iterator[None]:
    lock_path = root / LOCK_NAME
    if lock_path.is_symlink():
        raise AnchorQualificationFailure("Anchor lock must not be a symlink.")
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise AnchorQualificationFailure("Another full-length anchor qualification is already running.") from error
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _new_receipt(
    plan: AnchorPlan,
    plan_sha256: str,
    source_git_sha: str,
    source: Mapping[str, object],
    prior_evidence: Mapping[str, object],
    tool_bundle: Mapping[str, object],
    capability: Mapping[str, object],
) -> dict[str, object]:
    if plan.relative_path is None:
        raise AnchorQualificationFailure("Anchor plan is not bound to the repository.")
    now = datetime.now(UTC).isoformat()
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "qualification_id": plan.qualification_id,
        "created_at": now,
        "updated_at": now,
        "source_git_sha": source_git_sha,
        "source_tree_dirty": False,
        "plan": {"path": plan.relative_path, "sha256": plan_sha256},
        "runtime": runtime_evidence(),
        "source": dict(source),
        "prior_evidence": dict(prior_evidence),
        "tool_bundle": dict(tool_bundle),
        "capability": dict(capability),
        "method": {
            "pipeline": "source_level_production_conversion_context",
            "published_ad_hoc_tool_bundle": True,
            "packaged_worker_executed": False,
            "signed_package_evidence": False,
            "physical_device_evidence": False,
            "audio_mode": plan.production_options.audio_mode.value,
            "audio_bitrate_kbps": plan.production_options.audio_bitrate_kbps,
            "audio_preferred_language": plan.production_options.audio_preferred_language,
            "subtitles": plan.production_options.subtitles.value,
            "crop_black_bars": plan.production_options.crop_black_bars,
            "swap_eyes": plan.production_options.swap_eyes,
            "upscale": plan.production_options.upscale,
            "fov": plan.production_options.fov,
            "duration_tolerance_frames": MEDIA_DURATION_TOLERANCE_FRAMES,
            "audio_start_tolerance_seconds": AUDIO_START_TOLERANCE_SECONDS,
            "audio_end_tolerance_seconds": AUDIO_END_TOLERANCE_SECONDS,
        },
        "candidates": [
            {
                "id": candidate.candidate_id,
                "quality": candidate.quality,
                "position": candidate.position,
                "maximum_size_ratio": candidate.maximum_size_ratio,
                "rationale": candidate.rationale,
                "artifact_filename": candidate.artifact_filename,
                "status": "pending",
                "artifact": None,
                "elapsed_seconds": None,
                "route": None,
            }
            for candidate in plan.candidates
        ],
        "acceptance": {
            "complete": False,
            "artifacts_verified": False,
            "apple_passthrough_passed": False,
            "seek_decode_passed": False,
            "audio_consistent": False,
            "automated_passed": False,
            "physical_evidence_ready": False,
            "ladder_mapping_selected": False,
            "signed_package_evidence": False,
            "passed": False,
        },
    }


def _candidate_receipt(receipt: Mapping[str, object], candidate_id: str) -> dict[str, object]:
    candidates = receipt.get("candidates")
    if not isinstance(candidates, list):
        raise AnchorQualificationFailure("Anchor receipt candidates are invalid.")
    matches = [
        candidate for candidate in candidates if isinstance(candidate, dict) and candidate.get("id") == candidate_id
    ]
    if len(matches) != 1:
        raise AnchorQualificationFailure(f"Anchor receipt candidate {candidate_id} is missing or duplicated.")
    return matches[0]


def _load_receipt(
    path: Path,
    *,
    plan: AnchorPlan,
    plan_sha256: str,
    source_git_sha: str,
    source: Mapping[str, object],
    prior_evidence: Mapping[str, object],
    tool_bundle: Mapping[str, object],
    capability: Mapping[str, object],
) -> dict[str, object]:
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AnchorQualificationFailure("Could not read resumable anchor receipt.") from error
    if not isinstance(receipt, dict) or receipt.get("schema_version") != EVIDENCE_SCHEMA_VERSION:
        raise AnchorQualificationFailure("Anchor receipt uses an unsupported schema.")
    expected = {
        "qualification_id": plan.qualification_id,
        "source_git_sha": source_git_sha,
        "source_tree_dirty": False,
        "plan": {"path": plan.relative_path, "sha256": plan_sha256},
        "runtime": runtime_evidence(),
        "source": dict(source),
        "prior_evidence": dict(prior_evidence),
        "tool_bundle": dict(tool_bundle),
        "capability": dict(capability),
    }
    for key, value in expected.items():
        if receipt.get(key) != value:
            raise AnchorQualificationFailure(f"Anchor receipt does not match current {key} identity.")
    candidates = receipt.get("candidates")
    if not isinstance(candidates, list) or len(candidates) != len(plan.candidates):
        raise AnchorQualificationFailure("Anchor receipt candidate set is incomplete.")
    for candidate in plan.candidates:
        record = _candidate_receipt(receipt, candidate.candidate_id)
        expected_record = {
            "id": candidate.candidate_id,
            "quality": candidate.quality,
            "position": candidate.position,
            "maximum_size_ratio": candidate.maximum_size_ratio,
            "rationale": candidate.rationale,
            "artifact_filename": candidate.artifact_filename,
        }
        if any(record.get(key) != value for key, value in expected_record.items()):
            raise AnchorQualificationFailure(f"Anchor receipt candidate {candidate.candidate_id} changed identity.")
        if record.get("status") not in {"pending", "encoding", "validating", "complete"}:
            raise AnchorQualificationFailure(
                f"Anchor receipt candidate {candidate.candidate_id} has an invalid status."
            )
    return receipt


def _stream_duration(stream: Mapping[str, object], label: str) -> tuple[float, float]:
    try:
        start = float(stream.get("start_time", 0) or 0)
        duration = float(stream["duration"])
    except (KeyError, TypeError, ValueError) as error:
        raise AnchorQualificationFailure(f"Final anchor {label} timing is unavailable.") from error
    return start, duration


def _audio_fingerprint(ffmpeg: Path, artifact: Path, audio_position: int) -> str:
    completed = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-xerror",
            "-i",
            artifact,
            "-map",
            f"0:a:{audio_position}",
            "-vn",
            "-c:a",
            "pcm_s16le",
            "-f",
            "hash",
            "-hash",
            "sha256",
            "-",
        ],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    prefix = "SHA256="
    value = completed.stdout.strip()
    if not value.startswith(prefix):
        raise AnchorQualificationFailure("Decoded audio fingerprint did not return SHA-256.")
    return _sha256(value[len(prefix) :].lower(), "decoded audio fingerprint")


def _decode_audio_windows(ffmpeg: Path, artifact: Path, duration_seconds: float) -> None:
    positions = (0.0, duration_seconds / 2, max(0.0, duration_seconds - 2.0))
    for position in positions:
        subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-xerror",
                "-ss",
                f"{position:.6f}",
                "-i",
                artifact,
                "-map",
                "0:a:0",
                "-t",
                "1",
                "-f",
                "null",
                "-",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )


def _sanitize_stream(stream: Mapping[str, object]) -> dict[str, object]:
    tags = stream.get("tags")
    disposition = stream.get("disposition")
    return {
        "index": stream.get("index"),
        "codec_name": stream.get("codec_name"),
        "codec_tag_string": stream.get("codec_tag_string"),
        "codec_type": stream.get("codec_type"),
        "profile": stream.get("profile"),
        "level": stream.get("level"),
        "width": stream.get("width"),
        "height": stream.get("height"),
        "channels": stream.get("channels"),
        "channel_layout": stream.get("channel_layout"),
        "start_time": stream.get("start_time"),
        "duration": stream.get("duration"),
        "language": tags.get("language") if isinstance(tags, Mapping) else None,
        "default": disposition.get("default") if isinstance(disposition, Mapping) else 0,
    }


def _packaged_box_types(mp4box: Path, artifact: Path) -> set[str]:
    try:
        completed = subprocess.run(
            [mp4box, "-diso", artifact, "-std"],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise AnchorQualificationFailure("Packaged MP4Box could not inspect the final anchor.") from error
    return set(BOX_TYPE_PATTERN.findall(completed.stdout))


def validate_anchor_artifact(
    tools: PackagedTools,
    artifact: Path,
    plan: AnchorPlan,
    validation_root: Path,
) -> dict[str, object]:
    observed_boxes = _packaged_box_types(tools.mp4box, artifact)
    missing_boxes = sorted(DIRECT_REQUIRED_BOX_TYPES - observed_boxes)
    if missing_boxes:
        raise AnchorQualificationFailure("Final anchor is missing spatial boxes: " + ", ".join(missing_boxes))
    probe = _probe_json(
        [
            tools.ffprobe,
            "-v",
            "error",
            "-count_packets",
            "-show_entries",
            "format=duration,size:stream=index,codec_name,codec_tag_string,codec_type,profile,level,width,height,channels,channel_layout,start_time,duration,nb_read_packets:stream_tags=language,title:stream_disposition=default",
            "-of",
            "json",
            artifact,
        ],
        "final anchor artifact",
    )
    streams_raw = probe.get("streams")
    format_data = probe.get("format")
    if not isinstance(streams_raw, list) or not all(isinstance(stream, Mapping) for stream in streams_raw):
        raise AnchorQualificationFailure("Final anchor stream list is invalid.")
    if not isinstance(format_data, Mapping):
        raise AnchorQualificationFailure("Final anchor format metadata is invalid.")
    streams = [stream for stream in streams_raw if isinstance(stream, Mapping)]
    video_streams = [stream for stream in streams if stream.get("codec_type") == "video"]
    audio_streams = [stream for stream in streams if stream.get("codec_type") == "audio"]
    if len(video_streams) != 1 or not audio_streams:
        raise AnchorQualificationFailure(
            "Final anchor must contain one MV-HEVC video stream and at least one audio stream."
        )
    video = video_streams[0]
    if (
        video.get("codec_name") != "hevc"
        or video.get("codec_tag_string") != "hvc1"
        or video.get("profile") != "Main"
        or type(video.get("level")) is not int
        or int(video["level"]) <= 0
        or (video.get("width"), video.get("height")) != (plan.source.width, plan.source.height)
    ):
        raise AnchorQualificationFailure("Final anchor video stream does not match the direct MV-HEVC source contract.")
    try:
        frame_count = int(video["nb_read_packets"])
    except (KeyError, TypeError, ValueError) as error:
        raise AnchorQualificationFailure("Final anchor frame count is unavailable.") from error
    if frame_count != plan.source.frame_count:
        raise AnchorQualificationFailure("Final anchor frame count does not match the source contract.")
    if any(stream.get("codec_name") != "aac" for stream in audio_streams):
        raise AnchorQualificationFailure("Final anchor audio streams must all use Apple-qualified AAC.")
    if sum(int(stream.get("disposition", {}).get("default", 0) or 0) for stream in audio_streams) != 1:
        raise AnchorQualificationFailure("Final anchor must expose exactly one default audio stream.")
    try:
        duration_seconds = float(format_data["duration"])
    except (KeyError, TypeError, ValueError) as error:
        raise AnchorQualificationFailure("Final anchor duration is invalid.") from error
    frame_duration = 1 / float(Fraction(plan.source.frame_rate))
    if abs(duration_seconds - plan.source.duration_seconds) > MEDIA_DURATION_TOLERANCE_FRAMES * frame_duration:
        raise AnchorQualificationFailure("Final anchor duration differs from the full-length source contract.")
    video_start, video_duration = _stream_duration(video, "video")
    for index, audio in enumerate(audio_streams):
        audio_start, audio_duration = _stream_duration(audio, f"audio stream {index}")
        if abs(audio_start - video_start) > AUDIO_START_TOLERANCE_SECONDS:
            raise AnchorQualificationFailure("Final anchor audio/video start timing exceeds tolerance.")
        if abs((audio_start + audio_duration) - (video_start + video_duration)) > AUDIO_END_TOLERANCE_SECONDS:
            raise AnchorQualificationFailure("Final anchor audio/video end timing exceeds tolerance.")

    verify_seeks(tools.ffmpeg.as_posix(), artifact, max(1, math.ceil(duration_seconds)))
    _decode_audio_windows(tools.ffmpeg, artifact, duration_seconds)
    fingerprints = [_audio_fingerprint(tools.ffmpeg, artifact, position) for position in range(len(audio_streams))]

    validation_root.mkdir(parents=True, exist_ok=True)
    passthrough = validation_root / "apple-passthrough.mov"
    passthrough.unlink(missing_ok=True)
    report = inspect_apple_media_conversion(artifact, passthrough)
    if report.dropped_streams:
        raise AnchorQualificationFailure("Apple passthrough dropped one or more final anchor tracks.")
    passthrough.unlink(missing_ok=True)

    return {
        "sha256": sha256_file(artifact),
        "size_bytes": artifact.stat().st_size,
        "duration_seconds": duration_seconds,
        "frame_count": plan.source.frame_count,
        "required_spatial_boxes": sorted(DIRECT_REQUIRED_BOX_TYPES),
        "observed_spatial_boxes": sorted(observed_boxes),
        "streams": [_sanitize_stream(stream) for stream in streams],
        "audio_fingerprints_sha256": fingerprints,
        "seek_positions": ["beginning", "middle", "end"],
        "audio_decode_positions": ["beginning", "middle", "end"],
        "apple_passthrough_stream_counts": {
            "source": dict(sorted(report.source_streams.items())),
            "output": dict(sorted(report.output_streams.items())),
        },
        "validation": {flag: True for flag in sorted(VALIDATION_FLAGS)},
    }


def _artifact_record_is_valid(artifact: object, plan: AnchorPlan) -> bool:
    if not isinstance(artifact, Mapping):
        return False
    digest = artifact.get("sha256")
    size_bytes = artifact.get("size_bytes")
    duration_seconds = artifact.get("duration_seconds")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
        or type(size_bytes) is not int
        or size_bytes <= 0
        or type(duration_seconds) not in (int, float)
        or not math.isfinite(float(duration_seconds))
    ):
        return False
    frame_duration = 1 / float(Fraction(plan.source.frame_rate))
    if (
        artifact.get("frame_count") != plan.source.frame_count
        or abs(float(duration_seconds) - plan.source.duration_seconds)
        > MEDIA_DURATION_TOLERANCE_FRAMES * frame_duration
    ):
        return False
    required_boxes = artifact.get("required_spatial_boxes")
    observed_boxes = artifact.get("observed_spatial_boxes")
    if required_boxes != sorted(DIRECT_REQUIRED_BOX_TYPES) or not isinstance(observed_boxes, list):
        return False
    if not DIRECT_REQUIRED_BOX_TYPES.issubset(set(observed_boxes)):
        return False
    validation = artifact.get("validation")
    if (
        not isinstance(validation, Mapping)
        or set(validation) != VALIDATION_FLAGS
        or any(validation[flag] is not True for flag in VALIDATION_FLAGS)
    ):
        return False
    streams = artifact.get("streams")
    if not isinstance(streams, list) or not all(isinstance(stream, Mapping) for stream in streams):
        return False
    video_streams = [stream for stream in streams if stream.get("codec_type") == "video"]
    audio_streams = [stream for stream in streams if stream.get("codec_type") == "audio"]
    if len(video_streams) != 1 or not audio_streams:
        return False
    video = video_streams[0]
    if (
        video.get("codec_name") != "hevc"
        or video.get("codec_tag_string") != "hvc1"
        or video.get("profile") != "Main"
        or type(video.get("level")) is not int
        or int(video["level"]) <= 0
        or video.get("width") != plan.source.width
        or video.get("height") != plan.source.height
        or any(stream.get("codec_name") != "aac" for stream in audio_streams)
        or sum(int(stream.get("default", 0) or 0) for stream in audio_streams) != 1
    ):
        return False
    fingerprints = artifact.get("audio_fingerprints_sha256")
    if not isinstance(fingerprints, list) or len(fingerprints) != len(audio_streams):
        return False
    if any(
        not isinstance(fingerprint, str)
        or len(fingerprint) != 64
        or any(character not in "0123456789abcdef" for character in fingerprint)
        for fingerprint in fingerprints
    ):
        return False
    if artifact.get("seek_positions") != ["beginning", "middle", "end"]:
        return False
    if artifact.get("audio_decode_positions") != ["beginning", "middle", "end"]:
        return False
    stream_counts = artifact.get("apple_passthrough_stream_counts")
    if not isinstance(stream_counts, Mapping) or stream_counts.get("source") != stream_counts.get("output"):
        return False
    return True


def _update_acceptance(receipt: dict[str, object], plan: AnchorPlan) -> None:
    candidates = receipt.get("candidates")
    if not isinstance(candidates, list):
        raise AnchorQualificationFailure("Anchor receipt candidates are invalid.")
    complete_records: list[Mapping[str, object]] = []
    for definition in plan.candidates:
        record = _candidate_receipt(receipt, definition.candidate_id)
        route = record.get("route")
        elapsed_seconds = record.get("elapsed_seconds")
        expected_route = {
            "intent": VideoRouteIntent.AUTOMATIC.value,
            "selected": VideoRouteKind.DIRECT_MV_HEVC.value,
            "reason": "full_length_quality_anchor_candidate",
            "rate_control": "quality",
            "quality": definition.quality,
        }
        if (
            record.get("status") == "complete"
            and _artifact_record_is_valid(record.get("artifact"), plan)
            and route == expected_route
            and type(elapsed_seconds) in (int, float)
            and math.isfinite(float(elapsed_seconds))
            and float(elapsed_seconds) > 0
        ):
            complete_records.append(record)
    complete = len(complete_records) == len(candidates)
    fingerprints = [candidate["artifact"]["audio_fingerprints_sha256"] for candidate in complete_records]
    audio_consistent = (
        complete and bool(fingerprints) and all(fingerprint == fingerprints[0] for fingerprint in fingerprints)
    )
    acceptance = {
        "complete": complete,
        "artifacts_verified": complete,
        "apple_passthrough_passed": complete,
        "seek_decode_passed": complete,
        "audio_consistent": audio_consistent,
        "automated_passed": complete and audio_consistent,
        "physical_evidence_ready": False,
        "ladder_mapping_selected": False,
        "signed_package_evidence": False,
        "passed": False,
    }
    receipt["acceptance"] = acceptance


def _relative_artifact_path(candidate: AnchorCandidate) -> Path:
    return Path("artifacts") / candidate.candidate_id / candidate.artifact_filename


def _artifact_path(root: Path, candidate: AnchorCandidate, *, create_parent: bool = True) -> Path:
    artifact_root = root / "artifacts"
    if create_parent:
        _owned_directory(artifact_root, "Anchor artifact directory")
    elif artifact_root.is_symlink() or not artifact_root.is_dir():
        raise AnchorQualificationFailure("Anchor artifact directory is unavailable or unsafe.")
    candidate_root = artifact_root / candidate.candidate_id
    if candidate_root.is_symlink():
        raise AnchorQualificationFailure("Anchor candidate artifact directory must not be a symlink.")
    if create_parent:
        _owned_directory(candidate_root, "Anchor candidate artifact directory")
    elif candidate_root.exists() and not candidate_root.is_dir():
        raise AnchorQualificationFailure("Anchor candidate artifact directory is unsafe.")
    artifact = candidate_root / candidate.artifact_filename
    if artifact.is_symlink():
        raise AnchorQualificationFailure("Anchor artifact must not be a symlink.")
    return artifact


def _validate_completed_candidates(
    receipt: dict[str, object],
    plan: AnchorPlan,
    tools: PackagedTools,
    root: Path,
) -> None:
    for candidate in plan.candidates:
        record = _candidate_receipt(receipt, candidate.candidate_id)
        artifact_path = _artifact_path(root, candidate, create_parent=False)
        if record.get("status") == "complete":
            recorded_artifact = record.get("artifact")
            if not isinstance(recorded_artifact, Mapping) or not artifact_path.is_file():
                raise AnchorQualificationFailure(f"Completed candidate {candidate.candidate_id} lost its artifact.")
            if sha256_file(artifact_path) != recorded_artifact.get("sha256"):
                raise AnchorQualificationFailure(f"Completed candidate {candidate.candidate_id} artifact changed.")
            if artifact_path.stat().st_size != recorded_artifact.get("size_bytes"):
                raise AnchorQualificationFailure(f"Completed candidate {candidate.candidate_id} size changed.")
            required = artifact_path.stat().st_size + plan.execution.reserve_free_bytes
            if shutil.disk_usage(root).free < required:
                raise AnchorQualificationFailure(
                    f"Insufficient free space to revalidate {candidate.candidate_id}; {required} bytes are required."
                )
            work_root = _reset_candidate_work_root(root, candidate.candidate_id)
            try:
                with configured_package_tools(tools):
                    observed_artifact = validate_anchor_artifact(
                        tools,
                        artifact_path,
                        plan,
                        work_root / "validation",
                    )
            finally:
                shutil.rmtree(work_root, ignore_errors=True)
            if observed_artifact != recorded_artifact:
                raise AnchorQualificationFailure(
                    f"Completed candidate {candidate.candidate_id} validation evidence changed."
                )
        elif artifact_path.exists() and record.get("status") not in {"encoding", "validating"}:
            raise AnchorQualificationFailure(
                f"Unclaimed anchor artifact exists for candidate {candidate.candidate_id}; "
                "refusing automatic overwrite."
            )
    _update_acceptance(receipt, plan)


def _clean_stale_incomplete_work(receipt: Mapping[str, object], plan: AnchorPlan, root: Path) -> None:
    for candidate in plan.candidates:
        record = _candidate_receipt(receipt, candidate.candidate_id)
        artifact = _artifact_path(root, candidate, create_parent=False)
        if record.get("status") != "complete" and not artifact.exists():
            work_root = _safe_candidate_work_root(root, candidate.candidate_id)
            if work_root.exists():
                shutil.rmtree(work_root)


def _run_candidate(
    receipt: dict[str, object],
    receipt_path: Path,
    plan: AnchorPlan,
    candidate: AnchorCandidate,
    tools: PackagedTools,
    capability: DirectMVHEVCCapability,
    source_path: Path,
    root: Path,
) -> None:
    record = _candidate_receipt(receipt, candidate.candidate_id)
    artifact_path = _artifact_path(root, candidate)
    if record.get("status") == "complete":
        return
    if artifact_path.is_file() and record.get("status") in {"encoding", "validating"}:
        if not isinstance(record.get("route"), Mapping) or type(record.get("elapsed_seconds")) not in (int, float):
            raise AnchorQualificationFailure(
                f"Candidate {candidate.candidate_id} has an artifact without complete encode metadata; "
                "preserving it for operator review."
            )
        record["status"] = "validating"
        receipt["updated_at"] = datetime.now(UTC).isoformat()
        _atomic_write_json(receipt_path, receipt)
    else:
        work_root = _reset_candidate_work_root(root, candidate.candidate_id)
        baseline = _candidate_receipt(receipt, "q070")
        if candidate.candidate_id == "q070":
            required = plan.execution.minimum_initial_free_bytes
        else:
            baseline_artifact = baseline.get("artifact")
            if not isinstance(baseline_artifact, Mapping) or baseline.get("status") != "complete":
                raise AnchorQualificationFailure("Balanced anchor must complete before other candidates.")
            required = required_free_bytes(int(baseline_artifact["size_bytes"]), candidate, plan.execution)
        if shutil.disk_usage(root).free < required:
            raise AnchorQualificationFailure(
                f"Insufficient free space for {candidate.candidate_id}; {required} bytes are required."
            )

        destination = work_root / "output"
        destination.mkdir()
        record["status"] = "encoding"
        record["started_at"] = datetime.now(UTC).isoformat()
        receipt["updated_at"] = record["started_at"]
        _atomic_write_json(receipt_path, receipt)

        job = build_job(plan, source_path, destination, candidate)
        route = candidate_route(job, candidate, capability)
        started_at = time.monotonic()
        with configured_package_tools(tools), configured_conversion(job, source_path):
            apply_video_route_to_config(route)
            completed = start_process(video_route=route)
        elapsed_seconds = time.monotonic() - started_at
        if not isinstance(completed, Path) or not completed.is_file():
            raise AnchorQualificationFailure(f"Production pipeline did not produce {candidate.candidate_id} output.")
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        if artifact_path.exists():
            raise AnchorQualificationFailure(f"Anchor artifact already exists for {candidate.candidate_id}.")
        record["elapsed_seconds"] = round(elapsed_seconds, 3)
        record["route"] = route.report()
        receipt["updated_at"] = datetime.now(UTC).isoformat()
        _atomic_write_json(receipt_path, receipt)
        completed.replace(artifact_path)
        record["status"] = "validating"
        receipt["updated_at"] = datetime.now(UTC).isoformat()
        _atomic_write_json(receipt_path, receipt)

    work_root = _safe_candidate_work_root(root, candidate.candidate_id)
    validation_root = work_root / "validation"
    with configured_package_tools(tools):
        artifact = validate_anchor_artifact(tools, artifact_path, plan, validation_root)
    record["artifact"] = artifact
    record["status"] = "complete"
    record["completed_at"] = datetime.now(UTC).isoformat()
    receipt["updated_at"] = record["completed_at"]
    _update_acceptance(receipt, plan)
    _atomic_write_json(receipt_path, receipt)
    if work_root.exists():
        shutil.rmtree(work_root)


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
    if private_paths:
        source_path = private_paths[0]
        forbidden.update(value.casefold() for value in (source_path.name, source_path.stem) if len(value.strip()) >= 12)
    forbidden.update({Path.home().name.casefold(), platform.node().casefold()})
    for value in _string_values(document):
        folded = value.casefold()
        if (
            value.startswith("/")
            or "file://" in folded
            or "/users/" in folded
            or "/volumes/" in folded
            or any(token and token in folded for token in forbidden)
        ):
            raise AnchorQualificationFailure("Anchor evidence leaked private source or host information.")


def _render_checklist(receipt: Mapping[str, object], receipt_sha256: str) -> str:
    lines = [
        "# Vision Pro Direct MV-HEVC Anchor Checklist",
        "",
        f"Anchor receipt SHA-256: `{receipt_sha256}`",
        "",
        "Use the default **Stereo release movie** expectation in BD to AVP Playback Check.",
        "Transfer each finalized movie, read it back, and verify its SHA-256 before testing.",
        "",
    ]
    candidates = receipt.get("candidates")
    if not isinstance(candidates, list):
        raise AnchorQualificationFailure("Anchor receipt candidates are invalid.")
    for candidate in candidates:
        if not isinstance(candidate, Mapping) or not isinstance(candidate.get("artifact"), Mapping):
            raise AnchorQualificationFailure("Anchor checklist requires complete artifacts.")
        artifact = candidate["artifact"]
        lines.extend(
            [
                f"## {candidate['id']} · quality {candidate['quality']}",
                "",
                f"- File: `{candidate['artifact_filename']}`",
                f"- SHA-256: `{artifact['sha256']}`",
                "- Automatic validator: ready playback, Stereo · Screen presentation, "
                + "30-second sustained playback, and beginning/middle/end seeks.",
                "- Wearer: picture remains visible without corruption or freezes.",
                "- Wearer: depth is clearly three-dimensional, comfortable, and not inverted.",
                "- Wearer: dialogue and visible mouth movement remain synchronized at beginning, middle, and end.",
                "- Wearer: expected surround placement remains stable during sustained playback and after each seek.",
                "",
            ]
        )
    lines.extend(
        [
            "A candidate is not physically qualified until its exported playback report and wearer observations "
            + "are bound to the exact artifact SHA above.",
            "",
        ]
    )
    return "\n".join(lines)


def _physical_template(receipt: Mapping[str, object], receipt_sha256: str) -> dict[str, object]:
    candidates = receipt.get("candidates")
    if not isinstance(candidates, list):
        raise AnchorQualificationFailure("Anchor receipt candidates are invalid.")
    return {
        "schema_version": 1,
        "qualification_id": receipt["qualification_id"],
        "anchor_receipt_sha256": receipt_sha256,
        "device": {"hardware_model": None, "visionos_build": None},
        "candidates": [
            {
                "id": candidate["id"],
                "quality": candidate["quality"],
                "artifact_filename": candidate["artifact_filename"],
                "artifact_sha256": candidate["artifact"]["sha256"],
                "transferred_sha256": None,
                "playback_probe_report_sha256": None,
                "automatic": {
                    "ready_to_play": None,
                    "stereo_screen_presentation": None,
                    "sustained_playback": None,
                    "beginning_seek": None,
                    "middle_seek": None,
                    "end_seek": None,
                },
                "wearer": {
                    "visible_without_corruption": None,
                    "three_dimensional_depth": None,
                    "comfortable_non_inverted_depth": None,
                    "lip_sync_beginning": None,
                    "lip_sync_middle": None,
                    "lip_sync_end": None,
                    "surround_placement_stable": None,
                },
            }
            for candidate in candidates
            if isinstance(candidate, Mapping) and isinstance(candidate.get("artifact"), Mapping)
        ],
        "acceptance": {"complete": False, "passed": False},
    }


def _canonical_json_bytes(document: Mapping[str, object]) -> bytes:
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()


def _write_or_verify_immutable(path: Path, expected: bytes, *, allow_create: bool) -> None:
    if path.is_symlink():
        raise AnchorQualificationFailure("Anchor handoff file must not be a symlink.")
    if path.exists():
        if not path.is_file() or path.read_bytes() != expected:
            raise AnchorQualificationFailure("Anchor handoff file does not match the receipt.")
    elif not allow_create:
        raise AnchorQualificationFailure("Anchor handoff file is missing.")
    else:
        temporary = path.with_suffix(path.suffix + ".tmp")
        if temporary.is_symlink():
            raise AnchorQualificationFailure("Anchor handoff temporary file must not be a symlink.")
        temporary.write_bytes(expected)
        temporary.replace(path)
    if allow_create:
        path.chmod(0o444)
    elif stat.S_IMODE(path.stat().st_mode) & 0o222:
        raise AnchorQualificationFailure("Anchor handoff file is not frozen read-only.")


def _finalize_outputs(
    plan: AnchorPlan,
    root: Path,
    receipt_path: Path,
    receipt: Mapping[str, object],
    private_paths: Sequence[Path],
    *,
    allow_create: bool,
) -> str:
    acceptance = receipt.get("acceptance")
    if not isinstance(acceptance, Mapping) or acceptance.get("automated_passed") is not True:
        raise AnchorQualificationFailure("Anchor artifacts are not ready for physical testing.")
    _assert_private_values_absent(receipt, private_paths)
    if receipt_path.is_symlink() or not receipt_path.is_file():
        raise AnchorQualificationFailure("Anchor receipt is missing or unsafe.")
    if receipt_path.read_bytes() != _canonical_json_bytes(receipt):
        raise AnchorQualificationFailure("Anchor receipt bytes do not match the validated document.")
    receipt_sha256 = sha256_file(receipt_path)
    checklist = _render_checklist(receipt, receipt_sha256)
    physical_template = _physical_template(receipt, receipt_sha256)
    _assert_private_values_absent(checklist, private_paths)
    _assert_private_values_absent(physical_template, private_paths)
    checklist_path = root / CHECKLIST_NAME
    physical_path = root / PHYSICAL_TEMPLATE_NAME
    _write_or_verify_immutable(checklist_path, checklist.encode(), allow_create=allow_create)
    _write_or_verify_immutable(physical_path, _canonical_json_bytes(physical_template), allow_create=allow_create)
    if allow_create:
        receipt_path.chmod(0o444)
    elif stat.S_IMODE(receipt_path.stat().st_mode) & 0o222:
        raise AnchorQualificationFailure("Anchor receipt is not frozen read-only.")
    marker_path = root / ROOT_MARKER
    if marker_path.is_symlink() or not marker_path.is_file():
        raise AnchorQualificationFailure("Anchor output ownership marker is missing or unsafe.")
    if allow_create:
        marker_path.chmod(0o444)
    elif stat.S_IMODE(marker_path.stat().st_mode) & 0o222:
        raise AnchorQualificationFailure("Anchor output ownership marker is not frozen read-only.")
    candidates = receipt.get("candidates")
    if not isinstance(candidates, list):
        raise AnchorQualificationFailure("Anchor receipt candidates are invalid during finalization.")
    definitions = {candidate.candidate_id: candidate for candidate in plan.candidates}
    for candidate in candidates:
        if not isinstance(candidate, Mapping) or candidate.get("id") not in definitions:
            raise AnchorQualificationFailure("Anchor receipt candidate is invalid during finalization.")
        definition = definitions[str(candidate["id"])]
        artifact = _artifact_path(root, definition, create_parent=False)
        if not artifact.is_file():
            raise AnchorQualificationFailure("Anchor artifact is missing during finalization.")
        if allow_create:
            artifact.chmod(0o444)
        elif stat.S_IMODE(artifact.stat().st_mode) & 0o222:
            raise AnchorQualificationFailure("Anchor artifact is not frozen read-only.")
    return receipt_sha256


def create_anchors(plan_path: Path, app_path: Path, output_root: Path) -> dict[str, object]:
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise AnchorQualificationFailure("Full-length direct anchors require macOS arm64.")
    source_git_sha = clean_source_git_sha()
    plan, plan_sha256 = load_anchor_plan(plan_path)
    source_path = _private_file_from_environment(plan.source.path_env, "private full-length MVC source")
    prior_paths = [
        _private_file_from_environment(contract.path_env, f"{contract.evidence_id} sweep receipt")
        for contract in plan.prior_evidence
    ]
    tools = inspect_packaged_app(app_path, source_git_sha)
    tool_bundle = tool_bundle_evidence(tools)
    capability, capability_evidence = probe_packaged_capability(tools)
    source = inspect_source(tools.ffprobe, source_path, plan.source)
    source_snapshot = file_snapshot(source_path)
    prior_evidence = inspect_prior_receipts(plan)
    marker = {
        "schema_version": 1,
        "qualification_id": plan.qualification_id,
        "plan_sha256": plan_sha256,
        "source_sha256": plan.source.sha256,
        "source_git_sha": source_git_sha,
        "tool_bundle_app_tree_sha256": tools.app_tree_sha256,
    }
    root = _prepare_anchor_root(
        output_root,
        marker,
        forbidden_paths=(source_path, tools.app),
        allow_create=True,
    )
    receipt_path = root / RECEIPT_NAME

    with anchor_lock(root):
        if receipt_path.exists():
            receipt = _load_receipt(
                receipt_path,
                plan=plan,
                plan_sha256=plan_sha256,
                source_git_sha=source_git_sha,
                source=source,
                prior_evidence=prior_evidence,
                tool_bundle=tool_bundle,
                capability=capability_evidence,
            )
        else:
            receipt = _new_receipt(
                plan,
                plan_sha256,
                source_git_sha,
                source,
                prior_evidence,
                tool_bundle,
                capability_evidence,
            )
            _atomic_write_json(receipt_path, receipt)
        _clean_stale_incomplete_work(receipt, plan, root)
        has_completed_candidate = any(
            _candidate_receipt(receipt, candidate.candidate_id).get("status") == "complete"
            for candidate in plan.candidates
        )
        if not has_completed_candidate and shutil.disk_usage(root).free < plan.execution.minimum_initial_free_bytes:
            raise AnchorQualificationFailure("Anchor output volume does not satisfy the initial free-space gate.")
        _validate_completed_candidates(receipt, plan, tools, root)
        acceptance = receipt.get("acceptance")
        if isinstance(acceptance, Mapping) and acceptance.get("automated_passed") is True:
            _finalize_outputs(
                plan,
                root,
                receipt_path,
                receipt,
                [source_path, *prior_paths],
                allow_create=True,
            )
            return receipt
        for candidate in plan.candidates:
            _run_candidate(receipt, receipt_path, plan, candidate, tools, capability, source_path, root)
            if (
                clean_source_git_sha() != source_git_sha
                or app_tree_sha256(tools.app) != tools.app_tree_sha256
                or sha256_file(tools.app.parent / BUILD_METADATA_NAME) != tools.build_metadata_sha256
                or file_snapshot(source_path) != source_snapshot
            ):
                raise AnchorQualificationFailure(
                    "Source tree, private source, or published tool-bundle identity changed during anchor creation."
                )
        if sha256_file(source_path) != plan.source.sha256:
            raise AnchorQualificationFailure("Private source content changed during anchor creation.")
        for contract, path in zip(plan.prior_evidence, prior_paths, strict=True):
            if sha256_file(path) != contract.sha256:
                raise AnchorQualificationFailure("Prior direct-quality evidence changed during anchor creation.")
        _update_acceptance(receipt, plan)
        acceptance = receipt.get("acceptance")
        if not isinstance(acceptance, Mapping) or acceptance.get("automated_passed") is not True:
            raise AnchorQualificationFailure("Anchor candidates did not satisfy automated acceptance.")
        _atomic_write_json(receipt_path, receipt)
        _finalize_outputs(
            plan,
            root,
            receipt_path,
            receipt,
            [source_path, *prior_paths],
            allow_create=True,
        )
        return receipt


def verify_anchors(plan_path: Path, app_path: Path, output_root: Path) -> dict[str, object]:
    source_git_sha = clean_source_git_sha()
    plan, plan_sha256 = load_anchor_plan(plan_path)
    source_path = _private_file_from_environment(plan.source.path_env, "private full-length MVC source")
    prior_paths = [
        _private_file_from_environment(contract.path_env, f"{contract.evidence_id} sweep receipt")
        for contract in plan.prior_evidence
    ]
    tools = inspect_packaged_app(app_path, source_git_sha)
    tool_bundle = tool_bundle_evidence(tools)
    _, capability_evidence = probe_packaged_capability(tools)
    source = inspect_source(tools.ffprobe, source_path, plan.source)
    prior_evidence = inspect_prior_receipts(plan)
    marker = {
        "schema_version": 1,
        "qualification_id": plan.qualification_id,
        "plan_sha256": plan_sha256,
        "source_sha256": plan.source.sha256,
        "source_git_sha": source_git_sha,
        "tool_bundle_app_tree_sha256": tools.app_tree_sha256,
    }
    root = _prepare_anchor_root(
        output_root,
        marker,
        forbidden_paths=(source_path, tools.app),
        allow_create=False,
    )
    receipt_path = root / RECEIPT_NAME
    with anchor_lock(root):
        receipt = _load_receipt(
            receipt_path,
            plan=plan,
            plan_sha256=plan_sha256,
            source_git_sha=source_git_sha,
            source=source,
            prior_evidence=prior_evidence,
            tool_bundle=tool_bundle,
            capability=capability_evidence,
        )
        _validate_completed_candidates(receipt, plan, tools, root)
        acceptance = receipt.get("acceptance")
        if not isinstance(acceptance, Mapping) or acceptance.get("automated_passed") is not True:
            raise AnchorQualificationFailure("Anchor receipt does not pass automated verification.")
        _finalize_outputs(
            plan,
            root,
            receipt_path,
            receipt,
            [source_path, *prior_paths],
            allow_create=False,
        )
        return receipt


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create and verify full-length direct MV-HEVC quality anchors.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("create", "verify"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
        subparser.add_argument("--app", type=Path, required=True)
        subparser.add_argument("--output-root", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "create":
            receipt = create_anchors(args.plan.resolve(), args.app.resolve(), args.output_root)
        else:
            receipt = verify_anchors(args.plan.resolve(), args.app.resolve(), args.output_root)
    except AnchorQualificationFailure as error:
        print(f"Direct anchor qualification failed: {error}", file=os.sys.stderr)
        return 2
    except Exception as error:
        print(
            f"Direct anchor qualification failed with {type(error).__name__}; private details were suppressed.",
            file=os.sys.stderr,
        )
        return 2
    print(json.dumps(receipt.get("acceptance", {}), sort_keys=True))
    acceptance = receipt.get("acceptance")
    return 0 if isinstance(acceptance, Mapping) and acceptance.get("automated_passed") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
