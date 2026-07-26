#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import plistlib
import signal
import shutil
import subprocess
import tempfile
import uuid

from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator, Mapping, Sequence

import psutil

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
from scripts.verify_apple_media import verify_apple_media_compatible


PROTOCOL_VERSION = 10
WORKER_EXECUTABLE_NAME = "BluRayToVisionProEngine"
PACKAGE_BIN_RELATIVE_PATH = Path("Contents/Resources/app/bd_to_avp/bin")
HELPER_RELATIVE_PATH = PACKAGE_BIN_RELATIVE_PATH / "mv-hevc-encoder"
MAX_EVIDENCE_BYTES = 256 * 1024
WORKER_TIMEOUT_SECONDS = 30 * 60
MEDIA_DURATION_TOLERANCE_SECONDS = 0.5
PREVIEW_DURATION_SECONDS = 60


class PackagedRouteFailure(RuntimeError):
    pass


@dataclass(frozen=True)
class AppBundle:
    path: Path
    worker: Path
    helper: Path
    ffmpeg: Path
    ffprobe: Path
    bundle_identifier: str
    version: str


@dataclass(frozen=True)
class WorkerResult:
    operation: str
    route: Mapping[str, object]
    output_path: Path
    events: tuple[Mapping[str, object], ...]
    preview: Mapping[str, object] | None


def app_tree_sha256(app_path: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(app_path.rglob("*"), key=lambda item: item.relative_to(app_path).as_posix()):
        relative = path.relative_to(app_path).as_posix().encode("utf-8")
        status = path.lstat()
        mode = status.st_mode & 0o7777
        if path.is_symlink():
            payload = os.readlink(path).encode("utf-8")
            kind = b"symlink"
        elif path.is_file():
            payload_digest = hashlib.sha256()
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    payload_digest.update(chunk)
            payload = payload_digest.digest()
            kind = b"file"
        elif path.is_dir():
            payload = b""
            kind = b"directory"
        else:
            continue
        for component in (kind, relative, f"{mode:o}".encode("ascii"), payload):
            digest.update(len(component).to_bytes(8, "big"))
            digest.update(component)
    return digest.hexdigest()


def validate_qualification_paths(
    app_path: Path,
    source_path: Path,
    output_path: Path,
    fixture_output: Path | None,
) -> tuple[Path, Path, Path, Path | None]:
    requested_output = output_path.expanduser()
    requested_fixture = fixture_output.expanduser() if fixture_output is not None else None
    for requested, description in (
        (requested_output, "evidence output"),
        (requested_fixture, "fixture output"),
    ):
        if requested is not None and requested.is_symlink():
            raise PackagedRouteFailure(f"Packaged-route {description} must not be a symlink.")

    app = app_path.expanduser().resolve()
    source = source_path.expanduser().resolve()
    output = requested_output.resolve()
    fixture = requested_fixture.resolve() if requested_fixture is not None else None
    destinations = [output, *(path for path in (fixture,) if path is not None)]
    if len(destinations) != len(set(destinations)):
        raise PackagedRouteFailure("Packaged-route evidence and fixture outputs must be distinct.")
    for destination in destinations:
        if destination == source:
            raise PackagedRouteFailure("Packaged-route output must not replace the representative source.")
        if destination == app or destination.is_relative_to(app):
            raise PackagedRouteFailure("Packaged-route output must remain outside the app bundle.")
        if destination.exists() and not destination.is_file():
            raise PackagedRouteFailure("Packaged-route output must be a file destination.")
    return app, source, output, fixture


def atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        shutil.copy2(source, temporary)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_text(destination: Path, text: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def read_app_bundle(path: Path) -> AppBundle:
    app_path = path.resolve()
    info_path = app_path / "Contents/Info.plist"
    if not info_path.is_file():
        raise PackagedRouteFailure(f"Packaged app is missing Info.plist: {info_path}")
    with info_path.open("rb") as info_file:
        info = plistlib.load(info_file)
    worker = app_path / "Contents/MacOS" / WORKER_EXECUTABLE_NAME
    helper = app_path / HELPER_RELATIVE_PATH
    ffmpeg = app_path / PACKAGE_BIN_RELATIVE_PATH / "ffmpeg"
    ffprobe = app_path / PACKAGE_BIN_RELATIVE_PATH / "ffprobe"
    for executable in (worker, helper, ffmpeg, ffprobe):
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise PackagedRouteFailure(f"Packaged executable is unavailable: {executable}")
    bundle_identifier = info.get("CFBundleIdentifier")
    version = info.get("CFBundleShortVersionString")
    if not isinstance(bundle_identifier, str) or not bundle_identifier:
        raise PackagedRouteFailure("Packaged app bundle identifier is unavailable.")
    if not isinstance(version, str) or not version:
        raise PackagedRouteFailure("Packaged app version is unavailable.")
    return AppBundle(app_path, worker, helper, ffmpeg, ffprobe, bundle_identifier, version)


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
        payload = terminal.get("payload")
        message: object | None = None
        if isinstance(payload, Mapping):
            error = payload.get("error")
            if isinstance(error, Mapping):
                message = error.get("message")
            decision = payload.get("decision")
            if message is None and isinstance(decision, Mapping):
                decision_id = decision.get("id")
                prompt = decision.get("prompt")
                if isinstance(decision_id, str) and isinstance(prompt, str):
                    message = f"{decision_id}: {prompt}"
        raise PackagedRouteFailure(f"Packaged worker did not complete {operation}: {message or terminal.get('type')}")
    payload = terminal.get("payload")
    if not isinstance(payload, Mapping):
        raise PackagedRouteFailure("Packaged worker completion payload was invalid.")
    result_key = "conversion_result" if operation == "convert_source" else "preview_result"
    result = payload.get(result_key)
    if not isinstance(result, Mapping):
        raise PackagedRouteFailure(f"Packaged worker omitted {result_key}.")
    return result


def _process_tree(process_id: int) -> list[psutil.Process]:
    try:
        root = psutil.Process(process_id)
        return [root, *root.children(recursive=True)]
    except (psutil.AccessDenied, psutil.NoSuchProcess):
        return []


def _process_is_running(process: psutil.Process) -> bool:
    try:
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except (psutil.AccessDenied, psutil.NoSuchProcess):
        return False


def _terminate_worker_process(process: subprocess.Popen[str]) -> None:
    tracked = {candidate.pid: candidate for candidate in _process_tree(process.pid)}
    if process.poll() is None:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
    try:
        process.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        for candidate in _process_tree(process.pid):
            tracked[candidate.pid] = candidate
        descendants = [candidate for process_id, candidate in tracked.items() if process_id != process.pid]
        for descendant in descendants:
            try:
                descendant.terminate()
            except psutil.Error:
                continue
        _, alive = psutil.wait_procs(descendants, timeout=5)
        for descendant in alive:
            try:
                descendant.kill()
            except psutil.Error:
                continue
        if process.poll() is None:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        process.communicate(timeout=10)
    surviving_descendants = [
        candidate
        for process_id, candidate in tracked.items()
        if process_id != process.pid and _process_is_running(candidate)
    ]
    for descendant in surviving_descendants:
        try:
            descendant.kill()
        except psutil.Error:
            continue
    _, alive = psutil.wait_procs(surviving_descendants, timeout=5)
    if alive:
        raise PackagedRouteFailure("Packaged worker timeout cleanup left surviving descendants.")


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
    destination_root = Path(destination["path"]).resolve()
    destination_root.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
        [app.worker.as_posix()],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
        start_new_session=True,
    )
    try:
        stdout, _stderr = process.communicate(
            json.dumps(request, separators=(",", ":")) + "\n",
            timeout=WORKER_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        _terminate_worker_process(process)
        raise PackagedRouteFailure(f"Packaged worker timed out during {operation}.") from error
    events = parse_worker_events(stdout, job_id=job_id)
    result = _terminal_result(events, operation)
    if process.returncode != 0:
        raise PackagedRouteFailure(f"Packaged worker exited {process.returncode} after emitting a completion event.")
    route = result.get("video_route")
    output_path = result.get("output_path")
    if not isinstance(route, Mapping) or not isinstance(output_path, str):
        raise PackagedRouteFailure("Packaged worker completion omitted route or output artifact.")
    resolved_output = Path(output_path).resolve()
    if not resolved_output.is_file():
        raise PackagedRouteFailure("Packaged worker output artifact is unavailable.")
    if not resolved_output.is_relative_to(destination_root):
        raise PackagedRouteFailure("Packaged worker output escaped the requested destination.")
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
        route_payloads: list[Mapping[str, object]] = []
        for event in worker_result.events:
            payload = event.get("payload")
            if isinstance(payload, Mapping) and payload.get("code") == event_code:
                route_payloads.append(payload)
        if len(route_payloads) != 1 or route_payloads[0].get("video_route") != worker_result.route:
            raise PackagedRouteFailure(f"Worker did not emit one truthful {event_code} event.")


def stage_names(result: WorkerResult) -> tuple[str, ...]:
    stages: list[str] = []
    for event in result.events:
        payload = event.get("payload")
        if event.get("type") != "stage.started" or not isinstance(payload, Mapping):
            continue
        stage = payload.get("stage")
        if isinstance(stage, str):
            stages.append(stage)
    return tuple(stages)


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


def _media_probe(app: AppBundle, path: Path) -> Mapping[str, object]:
    completed = subprocess.run(
        [
            app.ffprobe.as_posix(),
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
    try:
        probe = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise PackagedRouteFailure("Packaged ffprobe emitted invalid media JSON.") from error
    if not isinstance(probe, Mapping):
        raise PackagedRouteFailure("Packaged ffprobe did not emit a media object.")
    return probe


def _media_duration(probe: Mapping[str, object]) -> float:
    format_data = probe.get("format")
    if not isinstance(format_data, Mapping) or format_data.get("duration") is None:
        raise PackagedRouteFailure("Media probe omitted duration.")
    try:
        duration_seconds = float(format_data["duration"])
    except (TypeError, ValueError) as error:
        raise PackagedRouteFailure("Media probe reported an invalid duration.") from error
    if duration_seconds <= 0:
        raise PackagedRouteFailure("Media probe reported a non-positive duration.")
    return duration_seconds


def _float_field(document: Mapping[str, object], key: str) -> float:
    value = document.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise PackagedRouteFailure(f"Media evidence omitted numeric {key!r}.")
    try:
        return float(value)
    except ValueError as error:
        raise PackagedRouteFailure(f"Media evidence reported invalid numeric {key!r}.") from error


def _media_streams(probe: Mapping[str, object]) -> list[dict[str, object]]:
    streams = probe.get("streams")
    if not isinstance(streams, list) or not all(isinstance(stream, Mapping) for stream in streams):
        raise PackagedRouteFailure("Media probe omitted a valid stream list.")
    return [
        {
            "codec_name": stream.get("codec_name"),
            "codec_type": stream.get("codec_type"),
            "height": stream.get("height"),
            "language": stream.get("tags", {}).get("language") if isinstance(stream.get("tags"), Mapping) else None,
            "width": stream.get("width"),
        }
        for stream in streams
        if isinstance(stream, Mapping)
    ]


def _validate_stream_contract(
    streams: Sequence[Mapping[str, object]],
    expected: Mapping[str, tuple[str, str | None]],
) -> None:
    grouped = {
        stream_type: [stream for stream in streams if stream.get("codec_type") == stream_type]
        for stream_type in expected
    }
    if len(streams) != len(expected) or any(len(matches) != 1 for matches in grouped.values()):
        raise PackagedRouteFailure("Media did not contain exactly one video, audio, and subtitle stream.")
    for stream_type, (codec_name, language) in expected.items():
        stream = grouped[stream_type][0]
        if stream.get("codec_name") != codec_name:
            raise PackagedRouteFailure(f"Media {stream_type} stream reported an unexpected codec.")
        if language is not None and stream.get("language") != language:
            raise PackagedRouteFailure(f"Media {stream_type} stream did not preserve the expected language.")


def _source_media_contract(app: AppBundle, path: Path) -> dict[str, object]:
    probe = _media_probe(app, path)
    streams = _media_streams(probe)
    _validate_stream_contract(
        streams,
        {
            "audio": ("truehd", "eng"),
            "subtitle": ("hdmv_pgs_subtitle", "eng"),
            "video": ("h264", None),
        },
    )
    video_stream = next(stream for stream in streams if stream.get("codec_type") == "video")
    if (video_stream.get("width"), video_stream.get("height")) != (1920, 1080):
        raise PackagedRouteFailure("Representative MVC source did not preserve 1920x1080 eye dimensions.")
    return {"duration_seconds": _media_duration(probe), "streams": streams}


def _preview_duration(result: WorkerResult, source_duration_seconds: float) -> float:
    preview = result.preview
    if not isinstance(preview, Mapping) or preview.get("position") != "middle":
        raise PackagedRouteFailure("Packaged preview omitted its middle-position timing contract.")
    duration_seconds = _float_field(preview, "duration_seconds")
    reported_source_duration = _float_field(preview, "source_duration_seconds")
    start_seconds = _float_field(preview, "start_seconds")
    expected_duration = min(PREVIEW_DURATION_SECONDS, source_duration_seconds)
    expected_start = max(0.0, (source_duration_seconds - duration_seconds) / 2)
    if abs(reported_source_duration - source_duration_seconds) > MEDIA_DURATION_TOLERANCE_SECONDS:
        raise PackagedRouteFailure("Packaged preview reported the wrong source duration.")
    if abs(duration_seconds - expected_duration) > MEDIA_DURATION_TOLERANCE_SECONDS:
        raise PackagedRouteFailure("Packaged preview duration differed from the requested range.")
    if abs(start_seconds - expected_start) > MEDIA_DURATION_TOLERANCE_SECONDS:
        raise PackagedRouteFailure("Packaged preview did not use the requested middle position.")
    return duration_seconds


def _probe_artifact(
    app: AppBundle,
    path: Path,
    required_box_types: set[str],
    *,
    expected_duration_seconds: float,
    expected_video_dimensions: tuple[int, int],
) -> dict[str, object]:
    verify_apple_media_compatible(path)
    observed_boxes = box_types(path)
    missing_boxes = sorted(required_box_types - observed_boxes)
    if missing_boxes:
        raise PackagedRouteFailure("Finalized artifact is missing spatial boxes: " + ", ".join(missing_boxes))
    probe = _media_probe(app, path)
    duration_seconds = _media_duration(probe)
    if abs(duration_seconds - expected_duration_seconds) > MEDIA_DURATION_TOLERANCE_SECONDS:
        raise PackagedRouteFailure("Finalized artifact duration did not match the requested workload.")
    verify_seeks(app.ffmpeg.as_posix(), path, max(1, math.ceil(duration_seconds)))
    streams = _media_streams(probe)
    _validate_stream_contract(
        streams,
        {
            "audio": ("aac", "eng"),
            "subtitle": ("mov_text", "eng"),
            "video": ("hevc", None),
        },
    )
    video_dimensions = {
        (stream.get("width"), stream.get("height")) for stream in streams if stream.get("codec_type") == "video"
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
        "streams": streams,
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
    source_duration_seconds: float,
    expected_video_dimensions: tuple[int, int],
    required_box_types: set[str],
    required_stages: set[str],
    forbidden_stages: set[str],
) -> tuple[WorkerResult, WorkerResult, dict[str, object], dict[str, object]]:
    full_id = str(uuid.uuid4())
    preview_id = str(uuid.uuid4())
    try:
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
    except PackagedRouteFailure as error:
        raise PackagedRouteFailure(f"{name} full route failed: {error}") from error
    try:
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
    except PackagedRouteFailure as error:
        raise PackagedRouteFailure(f"{name} preview route failed: {error}") from error
    validate_route_pair(
        full,
        preview,
        expected_selected=expected_selected,
        expected_fallback_reason=expected_fallback_reason,
        expected_upscale_mode=expected_upscale_mode,
    )
    for result in (full, preview):
        validate_stage_contract(result, required=required_stages, forbidden=forbidden_stages)
    preview_duration_seconds = _preview_duration(preview, source_duration_seconds)
    full_artifact = _probe_artifact(
        app,
        full.output_path,
        required_box_types,
        expected_duration_seconds=source_duration_seconds,
        expected_video_dimensions=expected_video_dimensions,
    )
    preview_artifact = _probe_artifact(
        app,
        preview.output_path,
        required_box_types,
        expected_duration_seconds=preview_duration_seconds,
        expected_video_dimensions=expected_video_dimensions,
    )
    return full, preview, full_artifact, preview_artifact


def verify_packaged_routes(
    app_path: Path,
    source_path: Path,
    *,
    fixture_output: Path | None,
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
            source_duration_seconds=source_duration_seconds,
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
                    source_duration_seconds=source_duration_seconds,
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
            source_duration_seconds=source_duration_seconds,
            expected_video_dimensions=(3_840, 2_160),
            required_box_types=DIRECT_REQUIRED_BOX_TYPES,
            required_stages={"create_left_right_files"},
            forbidden_stages={"combine_to_mv_hevc", "upscale_video"},
        )
        if fixture_output is not None:
            atomic_copy(metalfx_full.output_path, fixture_output)

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
                source_duration_seconds=source_duration_seconds,
                expected_video_dimensions=(3_840, 2_160),
                required_box_types=CURRENT_REQUIRED_BOX_TYPES,
                required_stages={"combine_to_mv_hevc", "create_left_right_files", "upscale_video"},
                forbidden_stages=set(),
            )
            metalfx_fallback_helper_sha256 = sha256_file(metalfx_fallback_app.helper)

    evidence: dict[str, object] = {
        "schema_version": 3,
        "generated_at": datetime.now(UTC).isoformat(),
        "package": {
            "app_tree_sha256": app_tree_sha256(app.path),
            "bundle_identifier": app.bundle_identifier,
            "helper_sha256": sha256_file(app.helper),
            "version": app.version,
            "worker_sha256": sha256_file(app.worker),
        },
        "source": {
            "media": source_media,
            "sha256": source_sha256,
            "size_bytes": source.stat().st_size,
        },
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
    encoded = (json.dumps(evidence, indent=2, sort_keys=True) + "\n").encode("utf-8")
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
    parser.add_argument("--expected-source-sha256", required=True)
    args = parser.parse_args()
    try:
        app, source, output, fixture_output = validate_qualification_paths(
            args.app,
            args.source,
            args.output,
            args.fixture_output,
        )
        evidence = verify_packaged_routes(
            app,
            source,
            fixture_output=fixture_output,
            expected_source_sha256=args.expected_source_sha256,
        )
    except (PackagedRouteFailure, OSError, subprocess.SubprocessError) as error:
        parser.exit(1, f"error: {error}\n")
    text = json.dumps(evidence, indent=2, sort_keys=True) + "\n"
    atomic_write_text(output, text)
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
