#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import math
import os
import plistlib
import shutil
import subprocess
import tempfile
import uuid

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator, Mapping, Sequence

from bd_to_avp.modules.video_route import (
    AUTOMATIC_DIRECT_QUALITY,
    AUTOMATIC_DIRECT_UPSCALE_QUALITY,
    AUTOMATIC_GENERATED_EYE_BITRATE_MBPS,
    AUTOMATIC_GENERATED_MERGE_QUALITY,
)
from scripts.qualify_direct_mv_hevc import (
    CURRENT_REQUIRED_BOX_TYPES,
    DIRECT_REQUIRED_BOX_TYPES,
    box_types,
    verify_seeks,
)
from scripts.qualify_mv_hevc_quality_match import sha256_file
from scripts.verify_apple_media import find_ffprobe, verify_apple_media_compatible


PROTOCOL_VERSION = 10
WORKER_EXECUTABLE_NAME = "BluRayToVisionProEngine"
HELPER_RELATIVE_PATH = Path("Contents/Resources/app/bd_to_avp/bin/mv-hevc-encoder")
MAX_EVIDENCE_BYTES = 256 * 1024
WORKER_TIMEOUT_SECONDS = 30 * 60


class PackagedRouteFailure(RuntimeError):
    pass


@dataclass(frozen=True)
class AppBundle:
    path: Path
    worker: Path
    helper: Path
    bundle_identifier: str
    version: str


@dataclass(frozen=True)
class WorkerResult:
    operation: str
    route: Mapping[str, object]
    output_path: Path
    events: tuple[Mapping[str, object], ...]
    preview: Mapping[str, object] | None


def read_app_bundle(path: Path) -> AppBundle:
    app_path = path.resolve()
    info_path = app_path / "Contents/Info.plist"
    if not info_path.is_file():
        raise PackagedRouteFailure(f"Packaged app is missing Info.plist: {info_path}")
    with info_path.open("rb") as info_file:
        info = plistlib.load(info_file)
    worker = app_path / "Contents/MacOS" / WORKER_EXECUTABLE_NAME
    helper = app_path / HELPER_RELATIVE_PATH
    for executable in (worker, helper):
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise PackagedRouteFailure(f"Packaged executable is unavailable: {executable}")
    bundle_identifier = info.get("CFBundleIdentifier")
    version = info.get("CFBundleShortVersionString")
    if not isinstance(bundle_identifier, str) or not bundle_identifier:
        raise PackagedRouteFailure("Packaged app bundle identifier is unavailable.")
    if not isinstance(version, str) or not version:
        raise PackagedRouteFailure("Packaged app version is unavailable.")
    return AppBundle(app_path, worker, helper, bundle_identifier, version)


def build_worker_request(
    operation: str,
    source_path: Path,
    destination_path: Path,
    *,
    job_id: str,
    parent_job_id: str | None = None,
    preview_duration_seconds: int = 60,
    upscale_enabled: bool = False,
) -> dict[str, object]:
    if operation not in {"convert_source", "preview_source"}:
        raise ValueError(f"Unsupported worker operation: {operation}")
    request: dict[str, object] = {
        "protocol_version": PROTOCOL_VERSION,
        "type": "job.start",
        "job_id": job_id,
        "operation": operation,
        "source": {"kind": "direct_file", "path": source_path.as_posix()},
        "destination": {"path": destination_path.as_posix()},
        "encoding": {
            "audio": {"mode": "automatic", "bitrate": 384, "preferred_language": "eng"},
            "video": {
                "mode": "mv_hevc",
                "route_intent": "automatic",
                "direct_bitrate": {"mode": "automatic"},
            },
            "upscale": {"enabled": True, "quality": 80} if upscale_enabled else {"enabled": False},
            "fov": 90,
            "frame_rate": "",
            "resolution": "",
            "crop_black_bars": False,
            "swap_eyes": False,
            "subtitles": {"mode": "preferred_plus_others", "preferred_language": "eng"},
        },
        "job": {
            "start_stage": 1,
            "keep_files": False,
            "overwrite": True,
            "remove_original": False,
            "continue_on_error": False,
            "software_encoder": False,
            "output_commands": False,
            "keep_awake": True,
        },
    }
    if operation == "preview_source":
        if parent_job_id is None:
            raise ValueError("Preview requests require a parent job id.")
        request["preview"] = {
            "parent_job_id": parent_job_id,
            "position": "middle",
            "duration_seconds": preview_duration_seconds,
        }
    return request


def parse_worker_events(stdout: str, *, job_id: str) -> tuple[Mapping[str, object], ...]:
    events: list[Mapping[str, object]] = []
    for line_number, line in enumerate(stdout.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as error:
            raise PackagedRouteFailure(f"Worker line {line_number} was not valid JSON.") from error
        if not isinstance(event, Mapping):
            raise PackagedRouteFailure(f"Worker line {line_number} was not an event object.")
        events.append(event)
    if not events:
        raise PackagedRouteFailure("Packaged worker produced no events.")
    for expected_sequence, event in enumerate(events):
        if event.get("protocol_version") != PROTOCOL_VERSION:
            raise PackagedRouteFailure("Packaged worker emitted the wrong protocol version.")
        if event.get("job_id") != job_id:
            raise PackagedRouteFailure("Packaged worker emitted an event for the wrong job id.")
        if event.get("sequence") != expected_sequence:
            raise PackagedRouteFailure("Packaged worker event sequence was not contiguous.")
    return tuple(events)


def _terminal_result(events: Sequence[Mapping[str, object]], operation: str) -> Mapping[str, object]:
    terminal = events[-1]
    if terminal.get("type") != "job.completed":
        message = (
            terminal.get("payload", {}).get("error", {}).get("message")
            if isinstance(terminal.get("payload"), Mapping)
            else None
        )
        raise PackagedRouteFailure(f"Packaged worker did not complete {operation}: {message or terminal.get('type')}")
    payload = terminal.get("payload")
    if not isinstance(payload, Mapping):
        raise PackagedRouteFailure("Packaged worker completion payload was invalid.")
    result_key = "conversion_result" if operation == "convert_source" else "preview_result"
    result = payload.get(result_key)
    if not isinstance(result, Mapping):
        raise PackagedRouteFailure(f"Packaged worker omitted {result_key}.")
    return result


def run_worker(
    app: AppBundle,
    request: Mapping[str, object],
    *,
    home_directory: Path,
) -> WorkerResult:
    job_id = str(request["job_id"])
    operation = str(request["operation"])
    environment = os.environ.copy()
    environment["HOME"] = home_directory.as_posix()
    home_directory.mkdir(parents=True, exist_ok=True)
    destination = request.get("destination")
    if not isinstance(destination, Mapping) or not isinstance(destination.get("path"), str):
        raise PackagedRouteFailure("Packaged worker request destination was invalid.")
    Path(destination["path"]).mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [app.worker.as_posix()],
        input=json.dumps(request, separators=(",", ":")) + "\n",
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        timeout=WORKER_TIMEOUT_SECONDS,
    )
    events = parse_worker_events(completed.stdout, job_id=job_id)
    result = _terminal_result(events, operation)
    if completed.returncode != 0:
        raise PackagedRouteFailure(f"Packaged worker exited {completed.returncode} after emitting a completion event.")
    route = result.get("video_route")
    output_path = result.get("output_path")
    if not isinstance(route, Mapping) or not isinstance(output_path, str):
        raise PackagedRouteFailure("Packaged worker completion omitted route or output artifact.")
    resolved_output = Path(output_path).resolve()
    if not resolved_output.is_file():
        raise PackagedRouteFailure("Packaged worker output artifact is unavailable.")
    preview = result if operation == "preview_source" else None
    return WorkerResult(operation, route, resolved_output, events, preview)


def validate_route_pair(
    full: WorkerResult,
    preview: WorkerResult,
    *,
    expected_selected: str,
    expected_fallback_reason: str | None,
    expected_upscale_mode: str | None = None,
) -> None:
    if full.route != preview.route:
        raise PackagedRouteFailure("Full conversion and finalized preview resolved different video routes.")
    if full.route.get("selected") != expected_selected:
        raise PackagedRouteFailure(f"Expected route {expected_selected}, found {full.route.get('selected')}.")
    if full.route.get("fallback_reason") != expected_fallback_reason:
        raise PackagedRouteFailure("Resolved route reported an unexpected fallback reason.")
    if full.route.get("upscale_mode") != expected_upscale_mode:
        raise PackagedRouteFailure("Resolved route reported an unexpected direct upscale mode.")
    if expected_fallback_reason is None:
        expected_quality = (
            AUTOMATIC_DIRECT_UPSCALE_QUALITY if expected_upscale_mode is not None else AUTOMATIC_DIRECT_QUALITY
        )
        if full.route.get("rate_control") != "quality":
            raise PackagedRouteFailure("Automatic direct route did not report quality rate control.")
        if full.route.get("quality") != expected_quality:
            raise PackagedRouteFailure("Automatic direct route reported an unexpected quality value.")
        if "bitrate_mbps" in full.route:
            raise PackagedRouteFailure("Automatic direct route unexpectedly reported a fixed bitrate.")
        if "eye_bitrate_mbps" in full.route or "merge_quality" in full.route:
            raise PackagedRouteFailure("Automatic direct route unexpectedly reported generated-route settings.")
        if "fallback_timing" in full.route:
            raise PackagedRouteFailure("Direct route unexpectedly reported fallback timing.")
    else:
        if full.route.get("fallback_timing") != "pre_input":
            raise PackagedRouteFailure("Generated fallback did not occur before input consumption.")
        if full.route.get("eye_bitrate_mbps") != AUTOMATIC_GENERATED_EYE_BITRATE_MBPS:
            raise PackagedRouteFailure("Generated fallback reported an unexpected automatic eye bitrate.")
        if full.route.get("merge_quality") != AUTOMATIC_GENERATED_MERGE_QUALITY:
            raise PackagedRouteFailure("Generated fallback reported an unexpected merge quality.")
        if "rate_control" in full.route or "quality" in full.route or "bitrate_mbps" in full.route:
            raise PackagedRouteFailure("Generated fallback unexpectedly reported direct rate control.")
    event_code = "video_route_selected" if expected_fallback_reason is None else "video_route_fallback"
    for worker_result in (full, preview):
        route_events = [
            event
            for event in worker_result.events
            if isinstance(event.get("payload"), Mapping) and event["payload"].get("code") == event_code
        ]
        if len(route_events) != 1 or route_events[0]["payload"].get("video_route") != worker_result.route:
            raise PackagedRouteFailure(f"Worker did not emit one truthful {event_code} event.")


def stage_names(result: WorkerResult) -> tuple[str, ...]:
    return tuple(
        str(event["payload"]["stage"])
        for event in result.events
        if event.get("type") == "stage.started"
        and isinstance(event.get("payload"), Mapping)
        and isinstance(event["payload"].get("stage"), str)
    )


def validate_stage_contract(
    result: WorkerResult,
    *,
    required: set[str],
    forbidden: set[str],
) -> None:
    observed = set(stage_names(result))
    missing = sorted(required - observed)
    unexpected = sorted(forbidden & observed)
    if missing:
        raise PackagedRouteFailure("Worker omitted required stages: " + ", ".join(missing))
    if unexpected:
        raise PackagedRouteFailure("Worker ran forbidden stages: " + ", ".join(unexpected))


def _probe_artifact(
    path: Path,
    required_box_types: set[str],
    *,
    expected_video_dimensions: tuple[int, int],
) -> dict[str, object]:
    verify_apple_media_compatible(path)
    observed_boxes = box_types(path)
    missing_boxes = sorted(required_box_types - observed_boxes)
    if missing_boxes:
        raise PackagedRouteFailure("Finalized artifact is missing spatial boxes: " + ", ".join(missing_boxes))
    completed = subprocess.run(
        [
            find_ffprobe(),
            "-v",
            "error",
            "-show_entries",
            "format=duration,size:stream=index,codec_name,codec_type,width,height:stream_tags=language",
            "-of",
            "json",
            path.as_posix(),
        ],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    probe = json.loads(completed.stdout)
    duration_seconds = float(probe["format"]["duration"])
    verify_seeks("ffmpeg", path, max(1, math.ceil(duration_seconds)))
    streams = probe.get("streams", [])
    stream_types = {stream.get("codec_type") for stream in streams if isinstance(stream, Mapping)}
    if not {"video", "audio", "subtitle"}.issubset(stream_types):
        raise PackagedRouteFailure("Finalized artifact did not preserve video, audio, and subtitles.")
    video_dimensions = {
        (stream.get("width"), stream.get("height"))
        for stream in streams
        if isinstance(stream, Mapping) and stream.get("codec_type") == "video"
    }
    if video_dimensions != {expected_video_dimensions}:
        raise PackagedRouteFailure(
            f"Finalized artifact video dimensions were {sorted(video_dimensions)!r}; "
            f"expected {expected_video_dimensions[0]}x{expected_video_dimensions[1]}."
        )
    return {
        "duration_seconds": duration_seconds,
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
        "video_dimensions": {
            "height": expected_video_dimensions[1],
            "width": expected_video_dimensions[0],
        },
        "streams": [
            {
                "codec_name": stream.get("codec_name"),
                "codec_type": stream.get("codec_type"),
                "height": stream.get("height"),
                "language": stream.get("tags", {}).get("language") if isinstance(stream.get("tags"), Mapping) else None,
                "width": stream.get("width"),
            }
            for stream in streams
            if isinstance(stream, Mapping)
        ],
    }


UNAVAILABLE_HELPER_SOURCE = r"""
#include <stdio.h>
#include <string.h>

int main(int argc, char **argv) {
    if (argc == 2 && strcmp(argv[1], "--capability-probe") == 0) {
        puts("{\"schema_version\":1,\"stereo_mv_hevc_encode_supported\":false}");
        return 2;
    }
    fputs("error: controlled unavailable-capability helper must not encode\n", stderr);
    return 64;
}
""".strip()


METALFX_UNAVAILABLE_HELPER_SOURCE = r"""
#include <stdio.h>
#include <string.h>

int main(int argc, char **argv) {
    if (argc == 2 && strcmp(argv[1], "--capability-probe") == 0) {
        puts("{\"metalfx_2x_mv_hevc_supported\":false,\"metalfx_spatial_scaling_supported\":false,\"pixel_transfer_2x_mv_hevc_supported\":false,\"schema_version\":1,\"stereo_mv_hevc_encode_supported\":true}");
        return 0;
    }
    fputs("error: controlled MetalFX-unavailable helper must not encode\n", stderr);
    return 64;
}
""".strip()


@contextmanager
def replacement_helper_app(
    app: AppBundle,
    root: Path,
    *,
    helper_source: str,
    helper_name: str,
) -> Iterator[AppBundle]:
    root.mkdir(parents=True, exist_ok=True)
    clone_path = root / app.path.name
    clone_process = subprocess.run(
        ["cp", "-cR", app.path.as_posix(), clone_path.as_posix()],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if clone_process.returncode != 0:
        subprocess.run(["ditto", app.path.as_posix(), clone_path.as_posix()], check=True)
    source_path = root / f"{helper_name}.c"
    helper_path = root / helper_name
    source_path.write_text(helper_source + "\n", encoding="utf-8")
    subprocess.run(
        [
            "xcrun",
            "clang",
            "-arch",
            "arm64",
            "-mmacosx-version-min=26.0",
            "-O2",
            source_path.as_posix(),
            "-o",
            helper_path.as_posix(),
        ],
        check=True,
    )
    nested_helper = clone_path / HELPER_RELATIVE_PATH
    shutil.copy2(helper_path, nested_helper)
    nested_helper.chmod(0o755)
    subprocess.run(["codesign", "--force", "--sign", "-", nested_helper.as_posix()], check=True)
    subprocess.run(["codesign", "--force", "--deep", "--sign", "-", clone_path.as_posix()], check=True)
    subprocess.run(["codesign", "--verify", "--deep", "--strict", clone_path.as_posix()], check=True)
    yield read_app_bundle(clone_path)


@contextmanager
def unavailable_capability_app(app: AppBundle, root: Path) -> Iterator[AppBundle]:
    with replacement_helper_app(
        app,
        root,
        helper_source=UNAVAILABLE_HELPER_SOURCE,
        helper_name="unavailable-helper",
    ) as replacement:
        yield replacement


@contextmanager
def metalfx_unavailable_capability_app(app: AppBundle, root: Path) -> Iterator[AppBundle]:
    with replacement_helper_app(
        app,
        root,
        helper_source=METALFX_UNAVAILABLE_HELPER_SOURCE,
        helper_name="metalfx-unavailable-helper",
    ) as replacement:
        yield replacement


def _route_evidence(result: WorkerResult, artifact: Mapping[str, object]) -> dict[str, object]:
    evidence: dict[str, object] = {
        "artifact": dict(artifact),
        "route": dict(result.route),
        "stages": list(stage_names(result)),
    }
    if result.preview is not None:
        evidence["preview"] = {
            "duration_seconds": result.preview.get("duration_seconds"),
            "position": result.preview.get("position"),
            "source_duration_seconds": result.preview.get("source_duration_seconds"),
            "start_seconds": result.preview.get("start_seconds"),
        }
    return evidence


def run_verified_route_pair(
    app: AppBundle,
    source: Path,
    root: Path,
    *,
    name: str,
    upscale_enabled: bool,
    expected_selected: str,
    expected_fallback_reason: str | None,
    expected_upscale_mode: str | None,
    expected_video_dimensions: tuple[int, int],
    required_box_types: set[str],
    required_stages: set[str],
    forbidden_stages: set[str],
) -> tuple[WorkerResult, WorkerResult, dict[str, object], dict[str, object]]:
    full_id = str(uuid.uuid4())
    preview_id = str(uuid.uuid4())
    full = run_worker(
        app,
        build_worker_request(
            "convert_source",
            source,
            root / f"{name}-full",
            job_id=full_id,
            upscale_enabled=upscale_enabled,
        ),
        home_directory=root / f"{name}-full-home",
    )
    preview = run_worker(
        app,
        build_worker_request(
            "preview_source",
            source,
            root / f"{name}-preview",
            job_id=preview_id,
            parent_job_id=full_id,
            upscale_enabled=upscale_enabled,
        ),
        home_directory=root / f"{name}-preview-home",
    )
    validate_route_pair(
        full,
        preview,
        expected_selected=expected_selected,
        expected_fallback_reason=expected_fallback_reason,
        expected_upscale_mode=expected_upscale_mode,
    )
    for result in (full, preview):
        validate_stage_contract(result, required=required_stages, forbidden=forbidden_stages)
    full_artifact = _probe_artifact(
        full.output_path,
        required_box_types,
        expected_video_dimensions=expected_video_dimensions,
    )
    preview_artifact = _probe_artifact(
        preview.output_path,
        required_box_types,
        expected_video_dimensions=expected_video_dimensions,
    )
    return full, preview, full_artifact, preview_artifact


def verify_packaged_routes(
    app_path: Path,
    source_path: Path,
    *,
    fixture_output: Path | None,
) -> dict[str, object]:
    app = read_app_bundle(app_path)
    source = source_path.resolve()
    if not source.is_file():
        raise PackagedRouteFailure("Representative MVC source is unavailable.")
    with tempfile.TemporaryDirectory(prefix="packaged-mv-hevc-routes-") as temporary_directory:
        root = Path(temporary_directory)
        direct_full, direct_preview, direct_full_artifact, direct_preview_artifact = run_verified_route_pair(
            app,
            source,
            root,
            name="direct",
            upscale_enabled=False,
            expected_selected="direct_mv_hevc",
            expected_fallback_reason=None,
            expected_upscale_mode=None,
            expected_video_dimensions=(1_920, 1_080),
            required_box_types=DIRECT_REQUIRED_BOX_TYPES,
            required_stages={"create_left_right_files"},
            forbidden_stages={"combine_to_mv_hevc", "upscale_video"},
        )

        with unavailable_capability_app(app, root / "fallback-app") as fallback_app:
            fallback_full, fallback_preview, fallback_full_artifact, fallback_preview_artifact = (
                run_verified_route_pair(
                    fallback_app,
                    source,
                    root,
                    name="fallback",
                    upscale_enabled=False,
                    expected_selected="generated_mv_hevc",
                    expected_fallback_reason="stereo_mv_hevc_encode_unavailable",
                    expected_upscale_mode=None,
                    expected_video_dimensions=(1_920, 1_080),
                    required_box_types=CURRENT_REQUIRED_BOX_TYPES,
                    required_stages={"combine_to_mv_hevc", "create_left_right_files"},
                    forbidden_stages={"upscale_video"},
                )
            )
            fallback_helper_sha256 = sha256_file(fallback_app.helper)

        metalfx_full, metalfx_preview, metalfx_full_artifact, metalfx_preview_artifact = run_verified_route_pair(
            app,
            source,
            root,
            name="metalfx",
            upscale_enabled=True,
            expected_selected="direct_mv_hevc",
            expected_fallback_reason=None,
            expected_upscale_mode="metalfx",
            expected_video_dimensions=(3_840, 2_160),
            required_box_types=DIRECT_REQUIRED_BOX_TYPES,
            required_stages={"create_left_right_files"},
            forbidden_stages={"combine_to_mv_hevc", "upscale_video"},
        )
        if fixture_output is not None:
            fixture_output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(metalfx_full.output_path, fixture_output)

        with metalfx_unavailable_capability_app(app, root / "metalfx-fallback-app") as metalfx_fallback_app:
            (
                metalfx_fallback_full,
                metalfx_fallback_preview,
                metalfx_fallback_full_artifact,
                metalfx_fallback_preview_artifact,
            ) = run_verified_route_pair(
                metalfx_fallback_app,
                source,
                root,
                name="metalfx-fallback",
                upscale_enabled=True,
                expected_selected="generated_mv_hevc",
                expected_fallback_reason="metalfx_2x_mv_hevc_unavailable",
                expected_upscale_mode=None,
                expected_video_dimensions=(3_840, 2_160),
                required_box_types=CURRENT_REQUIRED_BOX_TYPES,
                required_stages={"combine_to_mv_hevc", "create_left_right_files", "upscale_video"},
                forbidden_stages=set(),
            )
            metalfx_fallback_helper_sha256 = sha256_file(metalfx_fallback_app.helper)

    evidence: dict[str, object] = {
        "schema_version": 2,
        "generated_at": datetime.now(UTC).isoformat(),
        "package": {
            "bundle_identifier": app.bundle_identifier,
            "helper_sha256": sha256_file(app.helper),
            "version": app.version,
            "worker_sha256": sha256_file(app.worker),
        },
        "source": {"sha256": sha256_file(source), "size_bytes": source.stat().st_size},
        "ordinary": {
            "direct": {
                "full": _route_evidence(direct_full, direct_full_artifact),
                "preview": _route_evidence(direct_preview, direct_preview_artifact),
            },
            "fallback": {
                "capability_contract": {
                    "schema_version": 1,
                    "stereo_mv_hevc_encode_supported": False,
                },
                "helper_sha256": fallback_helper_sha256,
                "full": _route_evidence(fallback_full, fallback_full_artifact),
                "preview": _route_evidence(fallback_preview, fallback_preview_artifact),
            },
        },
        "metalfx_4k": {
            "direct": {
                "full": _route_evidence(metalfx_full, metalfx_full_artifact),
                "preview": _route_evidence(metalfx_preview, metalfx_preview_artifact),
            },
            "fallback": {
                "capability_contract": {
                    "metalfx_2x_mv_hevc_supported": False,
                    "metalfx_spatial_scaling_supported": False,
                    "pixel_transfer_2x_mv_hevc_supported": False,
                    "schema_version": 1,
                    "stereo_mv_hevc_encode_supported": True,
                },
                "helper_sha256": metalfx_fallback_helper_sha256,
                "full": _route_evidence(metalfx_fallback_full, metalfx_fallback_full_artifact),
                "preview": _route_evidence(metalfx_fallback_preview, metalfx_fallback_preview_artifact),
            },
        },
        "acceptance": {
            "metalfx_4k_direct_full_preview_parity": True,
            "metalfx_4k_fallback_full_preview_parity": True,
            "metalfx_4k_fallback_pre_input": True,
            "metalfx_4k_stage_contracts": True,
            "metalfx_4k_video_dimensions": True,
            "ordinary_direct_full_preview_parity": True,
            "ordinary_fallback_full_preview_parity": True,
            "ordinary_fallback_pre_input": True,
            "finalized_artifacts_valid": True,
            "passed": True,
        },
    }
    if fixture_output is not None:
        evidence["physical_fixture"] = {
            "sha256": sha256_file(fixture_output),
            "size_bytes": fixture_output.stat().st_size,
        }
    encoded = json.dumps(evidence, sort_keys=True).encode("utf-8")
    if len(encoded) > MAX_EVIDENCE_BYTES:
        raise PackagedRouteFailure("Packaged-route evidence exceeded its bounded size limit.")
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify packaged direct MV-HEVC, generated fallback, and finalized-preview route parity."
    )
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fixture-output", type=Path)
    args = parser.parse_args()
    try:
        evidence = verify_packaged_routes(
            args.app,
            args.source,
            fixture_output=args.fixture_output,
        )
    except (PackagedRouteFailure, OSError, subprocess.SubprocessError) as error:
        parser.exit(1, f"error: {error}\n")
    text = json.dumps(evidence, indent=2, sort_keys=True) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
