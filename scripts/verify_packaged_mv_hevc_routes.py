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
            "upscale": {"enabled": False},
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
) -> None:
    if full.route != preview.route:
        raise PackagedRouteFailure("Full conversion and finalized preview resolved different video routes.")
    if full.route.get("selected") != expected_selected:
        raise PackagedRouteFailure(f"Expected route {expected_selected}, found {full.route.get('selected')}.")
    if full.route.get("fallback_reason") != expected_fallback_reason:
        raise PackagedRouteFailure("Resolved route reported an unexpected fallback reason.")
    if expected_fallback_reason is None:
        if "fallback_timing" in full.route:
            raise PackagedRouteFailure("Direct route unexpectedly reported fallback timing.")
    elif full.route.get("fallback_timing") != "pre_input":
        raise PackagedRouteFailure("Generated fallback did not occur before input consumption.")
    event_code = "video_route_selected" if expected_fallback_reason is None else "video_route_fallback"
    for worker_result in (full, preview):
        route_events = [
            event
            for event in worker_result.events
            if isinstance(event.get("payload"), Mapping) and event["payload"].get("code") == event_code
        ]
        if len(route_events) != 1 or route_events[0]["payload"].get("video_route") != worker_result.route:
            raise PackagedRouteFailure(f"Worker did not emit one truthful {event_code} event.")


def _probe_artifact(path: Path, required_box_types: set[str]) -> dict[str, object]:
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
            "format=duration,size:stream=index,codec_name,codec_type:stream_tags=language",
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
    return {
        "duration_seconds": duration_seconds,
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
        "streams": [
            {
                "codec_name": stream.get("codec_name"),
                "codec_type": stream.get("codec_type"),
                "language": stream.get("tags", {}).get("language") if isinstance(stream.get("tags"), Mapping) else None,
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


@contextmanager
def unavailable_capability_app(app: AppBundle, root: Path) -> Iterator[AppBundle]:
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
    source_path = root / "unavailable-helper.c"
    helper_path = root / "unavailable-helper"
    source_path.write_text(UNAVAILABLE_HELPER_SOURCE + "\n", encoding="utf-8")
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


def _route_evidence(result: WorkerResult, artifact: Mapping[str, object]) -> dict[str, object]:
    evidence: dict[str, object] = {"route": dict(result.route), "artifact": dict(artifact)}
    if result.preview is not None:
        evidence["preview"] = {
            "duration_seconds": result.preview.get("duration_seconds"),
            "position": result.preview.get("position"),
            "source_duration_seconds": result.preview.get("source_duration_seconds"),
            "start_seconds": result.preview.get("start_seconds"),
        }
    return evidence


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
        direct_full_id = str(uuid.uuid4())
        direct_preview_id = str(uuid.uuid4())
        direct_full = run_worker(
            app,
            build_worker_request("convert_source", source, root / "direct-full", job_id=direct_full_id),
            home_directory=root / "direct-full-home",
        )
        direct_preview = run_worker(
            app,
            build_worker_request(
                "preview_source",
                source,
                root / "direct-preview",
                job_id=direct_preview_id,
                parent_job_id=direct_full_id,
            ),
            home_directory=root / "direct-preview-home",
        )
        validate_route_pair(
            direct_full,
            direct_preview,
            expected_selected="direct_mv_hevc",
            expected_fallback_reason=None,
        )
        direct_full_artifact = _probe_artifact(direct_full.output_path, DIRECT_REQUIRED_BOX_TYPES)
        direct_preview_artifact = _probe_artifact(direct_preview.output_path, DIRECT_REQUIRED_BOX_TYPES)
        if fixture_output is not None:
            fixture_output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(direct_full.output_path, fixture_output)

        with unavailable_capability_app(app, root / "fallback-app") as fallback_app:
            fallback_full_id = str(uuid.uuid4())
            fallback_preview_id = str(uuid.uuid4())
            fallback_full = run_worker(
                fallback_app,
                build_worker_request(
                    "convert_source",
                    source,
                    root / "fallback-full",
                    job_id=fallback_full_id,
                ),
                home_directory=root / "fallback-full-home",
            )
            fallback_preview = run_worker(
                fallback_app,
                build_worker_request(
                    "preview_source",
                    source,
                    root / "fallback-preview",
                    job_id=fallback_preview_id,
                    parent_job_id=fallback_full_id,
                ),
                home_directory=root / "fallback-preview-home",
            )
            validate_route_pair(
                fallback_full,
                fallback_preview,
                expected_selected="generated_mv_hevc",
                expected_fallback_reason="stereo_mv_hevc_encode_unavailable",
            )
            fallback_full_artifact = _probe_artifact(fallback_full.output_path, CURRENT_REQUIRED_BOX_TYPES)
            fallback_preview_artifact = _probe_artifact(fallback_preview.output_path, CURRENT_REQUIRED_BOX_TYPES)
            fallback_helper_sha256 = sha256_file(fallback_app.helper)

    evidence: dict[str, object] = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "package": {
            "bundle_identifier": app.bundle_identifier,
            "helper_sha256": sha256_file(app.helper),
            "version": app.version,
            "worker_sha256": sha256_file(app.worker),
        },
        "source": {"sha256": sha256_file(source), "size_bytes": source.stat().st_size},
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
        "acceptance": {
            "direct_full_preview_parity": True,
            "fallback_full_preview_parity": True,
            "fallback_pre_input": True,
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
