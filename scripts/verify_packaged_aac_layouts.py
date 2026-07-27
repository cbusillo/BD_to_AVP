#!/usr/bin/env python3

from __future__ import annotations

import argparse
import array
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from bd_to_avp.modules.aac_layout_policy import (
    AAC_LAYOUT_POLICIES,
    AacLayoutAction,
    AacLayoutPolicy,
)
from bd_to_avp.modules.audio_mode import AudioMode
from scripts.qualify_apple_aac_layouts import (
    AAC_CHANNEL_CONFIGURATION_LAYOUTS,
    APPLE_LPCM_PRESET,
    CANONICAL_AAC_CONFIGURATION,
    analyze_identity_samples,
    parse_aac_channel_configuration,
    policy_digest,
)
from scripts.qualify_mv_hevc_quality_match import sha256_file
from scripts.verify_apple_media import AppleMediaFailure, inspect_apple_media_conversion
from scripts.verify_packaged_mv_hevc_routes import (
    AppBundle,
    PackagedRouteFailure,
    WORKER_TIMEOUT_SECONDS,
    _terminate_worker_process,
    app_tree_sha256,
    atomic_write_text,
    build_worker_request,
    parse_worker_events,
    read_app_bundle,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = REPO_ROOT / "docs/qualification/apple-aac-package-fixtures-v1.json"
IDENTITY_SIGNAL_PATH = REPO_ROOT / "docs/qualification/apple-aac-layouts-v1.json"
SOURCE_POLICY_PATH = REPO_ROOT / "bd_to_avp/modules/aac_layout_policy.py"
PACKAGED_POLICY_PATH = Path("Contents/Resources/app/bd_to_avp/modules/aac_layout_policy.py")
MAX_EVIDENCE_BYTES = 256 * 1024
COMMAND_TIMEOUT_SECONDS = 30 * 60
POLICY_BY_ID = {policy.id: policy for policy in AAC_LAYOUT_POLICIES}
REQUIRED_COVERAGE = frozenset(
    {
        "preserve-stereo",
        "preserve-5.1",
        "remap-5.1(side)-to-5.1",
        "downmix-7.1-to-5.1",
        "fail-missing-layout",
        "fail-unknown-layout",
        "fail-pce-layout",
        "multiple-languages",
        "handler-titles",
        "default-disposition",
    }
)


class PackagedAacFailure(RuntimeError):
    pass


@dataclass(frozen=True)
class TrackSpec:
    policy_id: str | None
    codec_name: str
    source_layout: str | None
    channels: int
    aac_channel_configuration: int | None
    language: str
    handler_title: str
    default: bool

    @property
    def policy(self) -> AacLayoutPolicy | None:
        return POLICY_BY_ID.get(self.policy_id) if self.policy_id is not None else None


@dataclass(frozen=True)
class FixtureCase:
    case_id: str
    source_file: str
    coverage: tuple[str, ...]
    audio_mode: AudioMode
    preferred_language: str | None
    expected_rejection: str | None
    tracks: tuple[TrackSpec, ...]

    @property
    def selected_tracks(self) -> tuple[TrackSpec, ...]:
        if self.preferred_language is None:
            return self.tracks
        return tuple(track for track in self.tracks if track.language == self.preferred_language)


@dataclass(frozen=True)
class FixtureManifest:
    fixture_set_id: str
    cases: tuple[FixtureCase, ...]


@dataclass(frozen=True)
class WorkerExecution:
    returncode: int
    events: tuple[Mapping[str, object], ...]


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise PackagedAacFailure(f"{label} must be an object.")
    return value


def _sequence(value: object, label: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise PackagedAacFailure(f"{label} must be an array.")
    return value


def _string(value: object, label: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip():
        raise PackagedAacFailure(f"{label} must be a non-empty string.")
    return value.strip()


def _integer(value: object, label: str, *, optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    if type(value) is not int or value < 0:
        raise PackagedAacFailure(f"{label} must be a non-negative integer.")
    return value


def _strings(value: object, label: str) -> tuple[str, ...]:
    items = tuple(str(_string(item, f"{label} item")) for item in _sequence(value, label))
    if not items or len(items) != len(set(items)):
        raise PackagedAacFailure(f"{label} must contain unique values.")
    return items


def _parse_track(raw: object, label: str) -> TrackSpec:
    track = _mapping(raw, label)
    policy_id = _string(track.get("policy_id"), f"{label}.policy_id", optional=True)
    codec_name = str(_string(track.get("codec_name"), f"{label}.codec_name"))
    source_layout = _string(track.get("source_layout"), f"{label}.source_layout", optional=True)
    channels = _integer(track.get("channels"), f"{label}.channels")
    configuration = _integer(
        track.get("aac_channel_configuration"),
        f"{label}.aac_channel_configuration",
        optional=True,
    )
    language = str(_string(track.get("language"), f"{label}.language"))
    handler_title = str(_string(track.get("handler_title"), f"{label}.handler_title"))
    default = track.get("default")
    if type(default) is not bool or channels is None:
        raise PackagedAacFailure(f"{label} has invalid default or channel count fields.")
    return TrackSpec(policy_id, codec_name, source_layout, channels, configuration, language, handler_title, default)


def _validate_case(case: FixtureCase) -> None:
    if not case.tracks or not case.selected_tracks:
        raise PackagedAacFailure(f"Fixture {case.case_id} must select at least one audio track.")
    if case.expected_rejection is None:
        for track in case.selected_tracks:
            policy = track.policy
            if policy is None:
                raise PackagedAacFailure(f"Fixture {case.case_id} completed tracks require policy ids.")
            if track.source_layout != policy.source_layout or track.channels != len(policy.source_channels):
                raise PackagedAacFailure(f"Fixture {case.case_id} does not match policy {policy.id}.")
            if track.codec_name == "aac":
                expected_configuration = CANONICAL_AAC_CONFIGURATION.get(track.source_layout)
                if (
                    policy.action is not AacLayoutAction.PRESERVE
                    or track.aac_channel_configuration != expected_configuration
                ):
                    raise PackagedAacFailure(f"Fixture {case.case_id} AAC input is not a qualified preserve source.")
            elif track.aac_channel_configuration is not None:
                raise PackagedAacFailure(f"Fixture {case.case_id} non-AAC input must not declare AAC configuration.")
        return
    if case.expected_rejection not in {"channel_layout_missing", "channel_layout_not_qualified"}:
        raise PackagedAacFailure(f"Fixture {case.case_id} uses an unsupported rejection reason.")
    if len(case.tracks) != 1 or case.tracks[0].policy_id is not None:
        raise PackagedAacFailure(f"Fixture {case.case_id} rejected input must have one track without a policy id.")
    track = case.tracks[0]
    if "fail-missing-layout" in case.coverage and (
        track.source_layout is not None or track.aac_channel_configuration is not None
    ):
        raise PackagedAacFailure(f"Fixture {case.case_id} does not model a missing layout.")
    if "fail-unknown-layout" in case.coverage and track.source_layout != "unknown":
        raise PackagedAacFailure(f"Fixture {case.case_id} does not model an unknown layout.")
    if "fail-pce-layout" in case.coverage and (
        track.codec_name != "aac" or track.source_layout is not None or track.aac_channel_configuration != 0
    ):
        raise PackagedAacFailure(f"Fixture {case.case_id} does not model AAC configuration 0.")


def _validate_coverage(cases: Sequence[FixtureCase]) -> None:
    policy_coverage = {
        "preserve-stereo": "ch02-stereo",
        "preserve-5.1": "ch06-5_1",
        "remap-5.1(side)-to-5.1": "ch06-5_1_side",
        "downmix-7.1-to-5.1": "ch08-7_1",
    }
    for coverage, policy_id in policy_coverage.items():
        if not any(
            coverage in case.coverage and any(track.policy_id == policy_id for track in case.tracks) for case in cases
        ):
            raise PackagedAacFailure(f"Fixture manifest coverage {coverage} is attached to the wrong policy.")
    multilingual = [case for case in cases if "multiple-languages" in case.coverage]
    if not any(len({track.language for track in case.tracks}) >= 2 for case in multilingual):
        raise PackagedAacFailure("Fixture manifest multiple-languages coverage is not substantive.")
    titled = [case for case in cases if "handler-titles" in case.coverage]
    if not any(len({track.handler_title for track in case.tracks}) >= 2 for case in titled):
        raise PackagedAacFailure("Fixture manifest handler-titles coverage is not substantive.")
    dispositions = [case for case in cases if "default-disposition" in case.coverage]
    if not any({track.default for track in case.tracks} == {False, True} for case in dispositions):
        raise PackagedAacFailure("Fixture manifest default-disposition coverage is not substantive.")


def load_fixture_manifest(path: Path = DEFAULT_MANIFEST) -> FixtureManifest:
    try:
        document = _mapping(json.loads(path.read_text(encoding="utf-8")), "fixture manifest")
    except (OSError, json.JSONDecodeError) as error:
        raise PackagedAacFailure(f"Could not read AAC package fixture manifest: {error}") from error
    if document.get("schema_version") != 1:
        raise PackagedAacFailure("Fixture manifest schema_version must be 1.")
    fixture_set_id = str(_string(document.get("fixture_set_id"), "fixture manifest.fixture_set_id"))
    identity = _mapping(document.get("identity_signal"), "fixture manifest.identity_signal")
    if identity.get("path") != IDENTITY_SIGNAL_PATH.relative_to(REPO_ROOT).as_posix():
        raise PackagedAacFailure("Fixture manifest points to the wrong identity evidence.")
    if identity.get("sha256") != sha256_file(IDENTITY_SIGNAL_PATH):
        raise PackagedAacFailure("Fixture manifest identity evidence digest is stale.")
    qualification = _mapping(
        json.loads(IDENTITY_SIGNAL_PATH.read_text(encoding="utf-8")),
        "AAC identity evidence",
    )
    qualification_policy = _mapping(qualification.get("policy"), "AAC identity evidence.policy")
    qualified_policy_digest = qualification_policy.get("policy_sha256")
    if identity.get("policy_sha256") != qualified_policy_digest or policy_digest() != qualified_policy_digest:
        raise PackagedAacFailure("Fixture manifest is not bound to the Apple-qualified AAC policy.")
    required_coverage = frozenset(_strings(document.get("required_coverage"), "required_coverage"))
    if required_coverage != REQUIRED_COVERAGE:
        raise PackagedAacFailure("Fixture manifest required coverage does not match the release gate.")

    cases: list[FixtureCase] = []
    case_ids: set[str] = set()
    source_files: set[str] = set()
    covered: set[str] = set()
    for case_index, raw_case in enumerate(_sequence(document.get("cases"), "fixture manifest.cases")):
        raw = _mapping(raw_case, f"fixture manifest.cases[{case_index}]")
        case_id = str(_string(raw.get("id"), "case.id"))
        source_file = str(_string(raw.get("source_file"), f"{case_id}.source_file"))
        if (
            not case_id.replace("-", "").replace("_", "").isalnum()
            or case_id in case_ids
            or Path(source_file).name != source_file
            or Path(source_file).suffix.lower() != ".mkv"
            or source_file in source_files
        ):
            raise PackagedAacFailure(f"Fixture {case_id} has an unsafe or duplicate id/source file.")
        coverage = _strings(raw.get("coverage"), f"{case_id}.coverage")
        if not set(coverage).issubset(REQUIRED_COVERAGE):
            raise PackagedAacFailure(f"Fixture {case_id} has unsupported coverage.")
        try:
            audio_mode = AudioMode(str(_string(raw.get("audio_mode"), f"{case_id}.audio_mode")))
        except ValueError as error:
            raise PackagedAacFailure(f"Fixture {case_id} has an unsupported audio mode.") from error
        if audio_mode not in {AudioMode.AUTOMATIC, AudioMode.CONVERT_AAC}:
            raise PackagedAacFailure(f"Fixture {case_id} must produce AAC.")
        preferred_language = _string(
            raw.get("preferred_language"),
            f"{case_id}.preferred_language",
            optional=True,
        )
        expected_rejection = _string(
            raw.get("expected_rejection"),
            f"{case_id}.expected_rejection",
            optional=True,
        )
        tracks = tuple(
            _parse_track(track, f"{case_id}.tracks[{track_index}]")
            for track_index, track in enumerate(_sequence(raw.get("tracks"), f"{case_id}.tracks"))
        )
        case = FixtureCase(
            case_id,
            source_file,
            coverage,
            audio_mode,
            preferred_language,
            expected_rejection,
            tracks,
        )
        _validate_case(case)
        cases.append(case)
        case_ids.add(case_id)
        source_files.add(source_file)
        covered.update(coverage)
    if covered != REQUIRED_COVERAGE:
        raise PackagedAacFailure("Fixture manifest cases do not cover the complete release gate.")
    _validate_coverage(cases)
    return FixtureManifest(fixture_set_id, tuple(cases))


def validate_paths(
    app_path: Path,
    fixture_root: Path,
    output_path: Path,
    artifact_directory: Path,
) -> tuple[Path, Path, Path, Path]:
    app = app_path.expanduser().resolve()
    fixtures = fixture_root.expanduser().resolve()
    output = output_path.expanduser().resolve()
    artifacts = artifact_directory.expanduser().resolve()
    if not app.is_dir() or not fixtures.is_dir():
        raise PackagedAacFailure("AAC package verification requires an app bundle and fixture directory.")
    if output_path.expanduser().is_symlink() or artifact_directory.expanduser().is_symlink():
        raise PackagedAacFailure("AAC package destinations must not be symlinks.")
    if output.is_dir():
        raise PackagedAacFailure("AAC package evidence output must not be a directory.")
    if artifacts.exists():
        raise PackagedAacFailure("AAC package artifact directory must not already exist.")
    for destination in (output, artifacts):
        if destination == app or destination.is_relative_to(app):
            raise PackagedAacFailure("AAC package outputs must be outside the app bundle.")
        if destination == fixtures or destination.is_relative_to(fixtures):
            raise PackagedAacFailure("AAC package outputs must be outside the fixture directory.")
    if output == artifacts or output.is_relative_to(artifacts) or artifacts.is_relative_to(output):
        raise PackagedAacFailure("AAC package evidence must be outside the artifact directory.")
    return app, fixtures, output, artifacts


def _require_apple_tools() -> None:
    if shutil.which("avconvert") is None:
        raise PackagedAacFailure("Apple media verification requires avconvert.")


def _run(command: Sequence[str | Path]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [str(item) for item in command],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise PackagedAacFailure(f"AAC package command timed out: {Path(str(command[0])).name}") from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "").strip()
        raise PackagedAacFailure(f"AAC package command failed: {detail or 'no diagnostic output'}") from error


def _probe(app: AppBundle, path: Path) -> Mapping[str, object]:
    completed = _run([app.ffprobe, "-v", "error", "-show_streams", "-show_format", "-show_data", "-of", "json", path])
    try:
        return _mapping(json.loads(completed.stdout), "packaged FFprobe output")
    except json.JSONDecodeError as error:
        raise PackagedAacFailure("Packaged FFprobe did not return valid JSON.") from error


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _audio_streams(probe: Mapping[str, object]) -> list[dict[str, object]]:
    streams = probe.get("streams")
    if not isinstance(streams, list):
        return []
    result: list[dict[str, object]] = []
    for stream in streams:
        if not isinstance(stream, Mapping) or stream.get("codec_type") != "audio":
            continue
        raw_tags = stream.get("tags")
        tags: Mapping[str, object] = raw_tags if isinstance(raw_tags, Mapping) else {}
        raw_disposition = stream.get("disposition")
        disposition: Mapping[str, object] = raw_disposition if isinstance(raw_disposition, Mapping) else {}
        result.append(
            {
                "codec_name": stream.get("codec_name"),
                "profile": stream.get("profile"),
                "sample_rate": _optional_int(stream.get("sample_rate")),
                "channels": _optional_int(stream.get("channels")),
                "channel_layout": stream.get("channel_layout") or None,
                "aac_channel_configuration": parse_aac_channel_configuration(stream.get("extradata")),
                "language": tags.get("language"),
                "handler_title": tags.get("title") or tags.get("handler_name"),
                "default": bool(_optional_int(disposition.get("default")) or 0),
            }
        )
    return result


def _input_summary(track: TrackSpec) -> dict[str, object]:
    return {
        "codec_name": track.codec_name,
        "sample_rate": 48_000,
        "channels": track.channels,
        "channel_layout": track.source_layout,
        "aac_channel_configuration": track.aac_channel_configuration,
        "language": track.language,
        "handler_title": track.handler_title,
        "default": track.default,
    }


def _output_summary(track: TrackSpec) -> dict[str, object]:
    policy = track.policy
    assert policy is not None
    return {
        "codec_name": "aac",
        "profile": "LC",
        "sample_rate": 48_000,
        "channels": len(policy.target_channels),
        "channel_layout": policy.target_layout,
        "aac_channel_configuration": CANONICAL_AAC_CONFIGURATION[policy.target_layout],
        "language": track.language,
        "handler_title": track.handler_title,
        "default": track.default,
    }


def _validate_streams(
    case: FixtureCase,
    observed: Sequence[Mapping[str, object]],
    expected: Sequence[Mapping[str, object]],
    *,
    passthrough: bool = False,
) -> None:
    if len(observed) != len(expected):
        raise PackagedAacFailure(f"Fixture {case.case_id} has an unexpected audio stream count.")
    ignored = {"handler_title", "default"} if passthrough else set()
    for stream_index, (actual, wanted) in enumerate(zip(observed, expected, strict=True)):
        for key, value in wanted.items():
            if key not in ignored and actual.get(key) != value:
                raise PackagedAacFailure(f"Fixture {case.case_id} audio stream {stream_index} has unexpected {key}.")


def _build_request(case: FixtureCase, source: Path, destination: Path, job_id: str) -> dict[str, object]:
    request = build_worker_request("convert_source", source, destination, job_id=job_id)
    encoding = request["encoding"]
    assert isinstance(encoding, dict)
    encoding["audio"] = {
        "mode": case.audio_mode.value,
        "bitrate": 384,
        "preferred_language": case.preferred_language,
    }
    encoding["subtitles"] = {"mode": "off", "preferred_language": None}
    return request


def _execute_worker(
    app: AppBundle,
    request: Mapping[str, object],
    *,
    home_directory: Path,
) -> WorkerExecution:
    environment = os.environ.copy()
    environment["HOME"] = home_directory.as_posix()
    home_directory.mkdir(parents=True, exist_ok=True)
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
        raise PackagedAacFailure("Packaged worker timed out during AAC fixture verification.") from error
    if process.returncode is None:
        raise PackagedAacFailure("Packaged worker did not report an exit status.")
    return WorkerExecution(
        process.returncode,
        parse_worker_events(stdout, job_id=str(request["job_id"])),
    )


def _terminal(execution: WorkerExecution, expected_type: str) -> Mapping[str, object]:
    event = execution.events[-1]
    if event.get("type") != expected_type:
        raise PackagedAacFailure(f"Packaged worker ended with {event.get('type')!r}, not {expected_type!r}.")
    return _mapping(event.get("payload"), "packaged worker terminal payload")


def _warnings(execution: WorkerExecution, code: str) -> list[Mapping[str, object]]:
    result: list[Mapping[str, object]] = []
    for event in execution.events:
        payload = event.get("payload")
        if event.get("type") == "warning" and isinstance(payload, Mapping) and payload.get("code") == code:
            result.append(payload)
    return result


def _policy_evidence(case: FixtureCase, execution: WorkerExecution) -> list[dict[str, object]]:
    changed: list[TrackSpec] = []
    for track in case.selected_tracks:
        policy = track.policy
        assert policy is not None
        if policy.action is not AacLayoutAction.PRESERVE:
            changed.append(track)
    warnings = _warnings(execution, "audio_layout_policy_applied")
    if not changed:
        if warnings:
            raise PackagedAacFailure(f"Fixture {case.case_id} unexpectedly transformed a preserved layout.")
        return []
    raw_decisions = warnings[0].get("layout_decisions") if len(warnings) == 1 else None
    if not isinstance(raw_decisions, list):
        raise PackagedAacFailure(f"Fixture {case.case_id} omitted its AAC layout warning.")
    decisions = raw_decisions
    if len(decisions) != len(case.selected_tracks):
        raise PackagedAacFailure(f"Fixture {case.case_id} reported the wrong AAC decision count.")
    evidence: list[dict[str, object]] = []
    for track, decision in zip(case.selected_tracks, decisions, strict=True):
        policy = track.policy
        if not isinstance(decision, Mapping) or policy is None:
            raise PackagedAacFailure(f"Fixture {case.case_id} reported an invalid AAC decision.")
        expected: dict[str, object] = {
            "action": policy.action.value,
            "source_layout": policy.source_layout,
            "target_layout": policy.target_layout,
            "policy_id": policy.id,
        }
        if any(decision.get(key) != value for key, value in expected.items()):
            raise PackagedAacFailure(f"Fixture {case.case_id} reported the wrong AAC decision.")
        evidence.append(expected)
    return evidence


def _decode_track(app: AppBundle, source: Path, index: int, output: Path) -> array.array[float]:
    _run(
        [
            app.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            source,
            "-map",
            f"0:a:{index}",
            "-c:a",
            "pcm_f32le",
            "-f",
            "f32le",
            "-y",
            output,
        ]
    )
    samples = array.array("f")
    try:
        samples.frombytes(output.read_bytes())
    except ValueError as error:
        raise PackagedAacFailure("Apple LPCM decode produced malformed sample data.") from error
    if sys.byteorder != "little":
        samples.byteswap()
    return samples


def _identity_evidence(
    app: AppBundle,
    case: FixtureCase,
    output: Path,
    work_directory: Path,
) -> list[Mapping[str, object]]:
    apple_lpcm = work_directory / "apple-lpcm.mov"
    report = inspect_apple_media_conversion(output, apple_lpcm, preset=APPLE_LPCM_PRESET)
    if report.output_streams["audio"] != len(case.selected_tracks):
        raise PackagedAacFailure(f"Fixture {case.case_id} Apple LPCM conversion dropped audio.")
    apple_streams = _audio_streams(_probe(app, apple_lpcm))
    if len(apple_streams) != len(case.selected_tracks):
        raise PackagedAacFailure(f"Fixture {case.case_id} Apple LPCM stream count changed.")
    evidence: list[Mapping[str, object]] = []
    for index, track in enumerate(case.selected_tracks):
        policy = track.policy
        assert policy is not None
        configuration = CANONICAL_AAC_CONFIGURATION[policy.target_layout]
        output_channel_names = AAC_CHANNEL_CONFIGURATION_LAYOUTS[configuration]
        actual_channel_count = apple_streams[index].get("channels")
        if actual_channel_count != len(output_channel_names):
            raise PackagedAacFailure(f"Fixture {case.case_id} Apple LPCM channel count changed.")
        samples = _decode_track(app, apple_lpcm, index, work_directory / f"audio-{index}.f32")
        if len(samples) % len(output_channel_names) != 0:
            raise PackagedAacFailure(f"Fixture {case.case_id} Apple LPCM samples are incomplete.")
        result = analyze_identity_samples(
            samples,
            output_channel_names,
            policy.source_channels,
            policy.expected_identity_map,
        )
        if result.get("status") != "passed":
            raise PackagedAacFailure(f"Fixture {case.case_id} failed channel-identity analysis.")
        evidence.append(result)
    return evidence


def _failed_case(case: FixtureCase, execution: WorkerExecution, destination: Path) -> dict[str, object]:
    payload = _terminal(execution, "job.failed")
    error = _mapping(payload.get("error"), "packaged worker failure")
    warnings = _warnings(execution, "audio_layout_policy_rejected")
    if execution.returncode == 0 or error.get("code") != "conversion_failed" or len(warnings) != 1:
        raise PackagedAacFailure(f"Fixture {case.case_id} did not fail closed visibly.")
    decisions = warnings[0].get("layout_decisions")
    if not isinstance(decisions, list) or not any(
        isinstance(decision, Mapping) and decision.get("reason") == case.expected_rejection for decision in decisions
    ):
        raise PackagedAacFailure(f"Fixture {case.case_id} reported the wrong rejection reason.")
    if any(destination.glob("*.mov")):
        raise PackagedAacFailure(f"Fixture {case.case_id} left a final MOV after rejection.")
    return {
        "passed": True,
        "terminal": "job.failed",
        "worker_error_code": error.get("code"),
        "rejection_reason": case.expected_rejection,
    }


def _completed_case(
    app: AppBundle,
    case: FixtureCase,
    execution: WorkerExecution,
    destination: Path,
    artifacts: Path,
    work_directory: Path,
) -> dict[str, object]:
    payload = _terminal(execution, "job.completed")
    result = _mapping(payload.get("conversion_result"), "packaged worker conversion result")
    raw_output = result.get("output_path")
    if execution.returncode != 0 or not isinstance(raw_output, str):
        raise PackagedAacFailure(f"Fixture {case.case_id} did not complete successfully.")
    output = Path(raw_output).resolve()
    if not output.is_file() or not output.is_relative_to(destination.resolve()):
        raise PackagedAacFailure(f"Fixture {case.case_id} output escaped its destination.")
    expected = [_output_summary(track) for track in case.selected_tracks]
    output_probe = _probe(app, output)
    output_streams = _audio_streams(output_probe)
    _validate_streams(case, output_streams, expected)

    passthrough = work_directory / "apple-passthrough.mov"
    report = inspect_apple_media_conversion(output, passthrough)
    passthrough_streams = _audio_streams(_probe(app, passthrough))
    if report.output_streams["audio"] != len(expected):
        raise PackagedAacFailure(f"Fixture {case.case_id} lost audio in Apple passthrough.")
    _validate_streams(case, passthrough_streams, expected, passthrough=True)

    artifact = artifacts / f"{case.case_id}.mov"
    shutil.copy2(output, artifact)
    return {
        "passed": True,
        "terminal": "job.completed",
        "policy_decisions": _policy_evidence(case, execution),
        "output": {
            "file": artifact.name,
            "sha256": sha256_file(artifact),
            "size_bytes": artifact.stat().st_size,
            "audio_streams": output_streams,
        },
        "apple_passthrough": {"passed": True, "audio_streams": passthrough_streams},
        "identity": _identity_evidence(app, case, output, work_directory),
    }


def verify_packaged_aac_layouts(
    app_path: Path,
    fixture_root: Path,
    artifact_directory: Path,
    *,
    manifest_path: Path = DEFAULT_MANIFEST,
) -> dict[str, object]:
    _require_apple_tools()
    manifest = load_fixture_manifest(manifest_path)
    app = read_app_bundle(app_path)
    packaged_policy = app.path / PACKAGED_POLICY_PATH
    if not packaged_policy.is_file() or sha256_file(packaged_policy) != sha256_file(SOURCE_POLICY_PATH):
        raise PackagedAacFailure("Packaged AAC policy does not match the verifier checkout.")
    package = {
        "app_tree_sha256": app_tree_sha256(app.path),
        "bundle_identifier": app.bundle_identifier,
        "version": app.version,
        "worker_sha256": sha256_file(app.worker),
        "ffmpeg_sha256": sha256_file(app.ffmpeg),
        "ffprobe_sha256": sha256_file(app.ffprobe),
        "helper_sha256": sha256_file(app.helper),
        "aac_policy_sha256": sha256_file(packaged_policy),
    }
    artifact_directory.mkdir(parents=True)
    case_evidence: list[dict[str, object]] = []
    try:
        with tempfile.TemporaryDirectory(prefix="bd-to-avp-aac-package-") as temporary_directory:
            temporary_root = Path(temporary_directory)
            for case in manifest.cases:
                source = (fixture_root / case.source_file).resolve()
                if not source.is_file() or not source.is_relative_to(fixture_root):
                    raise PackagedAacFailure(f"Fixture {case.case_id} source is unavailable.")
                input_streams = _audio_streams(_probe(app, source))
                _validate_streams(case, input_streams, [_input_summary(track) for track in case.tracks])
                destination = temporary_root / case.case_id / "destination"
                home = temporary_root / case.case_id / "home"
                destination.mkdir(parents=True)
                execution = _execute_worker(
                    app,
                    _build_request(case, source, destination, str(uuid.uuid4())),
                    home_directory=home,
                )
                result = (
                    _failed_case(case, execution, destination)
                    if case.expected_rejection is not None
                    else _completed_case(
                        app,
                        case,
                        execution,
                        destination,
                        artifact_directory,
                        temporary_root / case.case_id,
                    )
                )
                case_evidence.append(
                    {
                        "id": case.case_id,
                        "coverage": list(case.coverage),
                        "source": {
                            "sha256": sha256_file(source),
                            "size_bytes": source.stat().st_size,
                            "audio_streams": input_streams,
                        },
                        "result": result,
                    }
                )
    except Exception:
        shutil.rmtree(artifact_directory, ignore_errors=True)
        raise

    evidence = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "fixture_set": {
            "id": manifest.fixture_set_id,
            "manifest_sha256": sha256_file(manifest_path),
            "identity_signal_sha256": sha256_file(IDENTITY_SIGNAL_PATH),
        },
        "package": package,
        "cases": case_evidence,
        "acceptance": {
            "fixture_manifest_checked": True,
            "packaged_worker_executed": True,
            "apple_passthrough_retained_audio": True,
            "channel_identity_passed": True,
            "metadata_preserved": True,
            "fail_closed_visible": True,
            "signed_candidate_proven": False,
            "physical_vision_pro_proven": False,
            "passed": True,
        },
    }
    if len(json.dumps(evidence, sort_keys=True).encode("utf-8")) > MAX_EVIDENCE_BYTES:
        shutil.rmtree(artifact_directory, ignore_errors=True)
        raise PackagedAacFailure("AAC package evidence exceeded its bounded size limit.")
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify the checked AAC layout fixture matrix through a packaged application worker."
    )
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--fixture-root", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args()
    try:
        app, fixtures, output, artifacts = validate_paths(
            args.app,
            args.fixture_root,
            args.output,
            args.artifacts,
        )
        evidence = verify_packaged_aac_layouts(
            app,
            fixtures,
            artifacts,
            manifest_path=args.manifest.resolve(),
        )
        text = json.dumps(evidence, indent=2, sort_keys=True) + "\n"
        atomic_write_text(output, text)
        print(text, end="")
    except (
        AppleMediaFailure,
        PackagedAacFailure,
        PackagedRouteFailure,
        OSError,
        subprocess.SubprocessError,
    ) as error:
        parser.exit(1, f"error: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
