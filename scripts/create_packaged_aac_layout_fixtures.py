#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import tempfile

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from bd_to_avp.modules.config import config
from scripts.create_spatial_audio_validation_fixtures import create_channel_identity_audio
from scripts.qualify_apple_aac_layouts import (
    CHANNEL_FREQUENCIES,
    IDENTITY_BURST_SECONDS,
    IDENTITY_GUARD_SECONDS,
    IDENTITY_SLOT_SECONDS,
    create_source_identity,
)
from scripts.qualify_mv_hevc_quality_match import sha256_file
from scripts.verify_packaged_aac_layouts import (
    DEFAULT_MANIFEST,
    FixtureCase,
    PackagedAacFailure,
    TrackSpec,
    _audio_streams,
    _input_summary,
    _managed_sigterm_exit,
    _validate_streams,
    load_fixture_manifest,
)


COMMAND_TIMEOUT_SECONDS = 30 * 60
MAX_SOURCE_DURATION_SECONDS = 120.0
MAX_SOURCE_SIZE_BYTES = 1024 * 1024 * 1024
EXTRA_CHANNEL_FREQUENCIES = {"TFL": 1430, "TFR": 1540}
FAILURE_IDENTITY_RECIPES = {
    "fail-missing-layout": ("5.1", ("FL", "FR", "FC", "LFE", "BL", "BR")),
    "fail-unknown-layout": ("3.1.2", ("FL", "FR", "FC", "LFE", "TFL", "TFR")),
    "fail-pce-layout": ("5.1(side)", ("FL", "FR", "FC", "LFE", "SL", "SR")),
}


class FixtureBuildFailure(RuntimeError):
    pass


def _redact_command_paths(detail: str, command: Sequence[str | Path]) -> str:
    redacted = detail
    paths = {
        str(item) for item in command if isinstance(item, Path) or (isinstance(item, str) and item.startswith("/"))
    }
    paths.add(str(Path.home()))
    for path in sorted(paths, key=len, reverse=True):
        if path:
            redacted = redacted.replace(path, "<redacted-path>")
    return redacted


def _hash_file(path: Path, label: str) -> str:
    try:
        return sha256_file(path)
    except OSError as error:
        raise FixtureBuildFailure(f"Could not hash the {label}.") from error


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
        raise FixtureBuildFailure(f"Fixture command timed out: {Path(str(command[0])).name}") from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "").strip()
        safe_detail = _redact_command_paths(detail, command)
        raise FixtureBuildFailure(f"Fixture command failed: {safe_detail or 'no diagnostic output'}") from error
    except OSError as error:
        raise FixtureBuildFailure(f"Fixture command could not start: {Path(str(command[0])).name}") from error


def _probe_document(path: Path) -> Mapping[str, object]:
    completed = _run(
        [
            config.FFPROBE_PATH,
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-show_data",
            "-of",
            "json",
            path,
        ]
    )
    try:
        document = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise FixtureBuildFailure("FFprobe returned malformed fixture JSON.") from error
    if not isinstance(document, Mapping):
        raise FixtureBuildFailure("FFprobe fixture output must be an object.")
    return document


def _source_video_summary(source: Path) -> dict[str, object]:
    try:
        source_size = source.stat().st_size
    except OSError as error:
        raise FixtureBuildFailure("MVC fixture source metadata is unavailable.") from error
    if source_size > MAX_SOURCE_SIZE_BYTES:
        raise FixtureBuildFailure("MVC fixture source exceeds the bounded fixture-generation size limit.")
    document = _probe_document(source)
    raw_streams = document.get("streams")
    streams = raw_streams if isinstance(raw_streams, list) else []
    video_streams = [
        stream for stream in streams if isinstance(stream, Mapping) and stream.get("codec_type") == "video"
    ]
    if source.suffix.lower() != ".mkv" or len(video_streams) != 1 or video_streams[0].get("codec_name") != "h264":
        raise FixtureBuildFailure("Fixture generation requires one H.264 video stream in a real MVC MKV source.")
    raw_format = document.get("format")
    format_data = raw_format if isinstance(raw_format, Mapping) else {}
    try:
        duration_seconds = float(str(format_data.get("duration")))
    except (TypeError, ValueError) as error:
        raise FixtureBuildFailure("MVC fixture source duration is unavailable.") from error
    if not math.isfinite(duration_seconds):
        raise FixtureBuildFailure("MVC fixture source duration must be finite.")
    minimum_duration = 2 * IDENTITY_GUARD_SECONDS + 8 * IDENTITY_SLOT_SECONDS
    if duration_seconds < minimum_duration:
        raise FixtureBuildFailure("MVC fixture source is too short for the eight-channel identity signal.")
    if duration_seconds > MAX_SOURCE_DURATION_SECONDS:
        raise FixtureBuildFailure("MVC fixture source exceeds the bounded fixture-generation duration limit.")
    stream = video_streams[0]
    return {
        "codec_name": stream.get("codec_name"),
        "profile": stream.get("profile"),
        "width": stream.get("width"),
        "height": stream.get("height"),
        "duration_seconds": round(duration_seconds, 6),
        "direct_mvc_route_proven": False,
    }


def _channel_frequency(channel: str) -> int:
    frequency = CHANNEL_FREQUENCIES.get(channel) or EXTRA_CHANNEL_FREQUENCIES.get(channel)
    if frequency is None:
        raise FixtureBuildFailure(f"No channel-identity frequency is defined for {channel}.")
    return frequency


def _create_identity_source(case: FixtureCase, track: TrackSpec, output: Path) -> None:
    policy = track.policy
    if policy is not None:
        create_source_identity(output, policy)
        return
    recipe = FAILURE_IDENTITY_RECIPES.get(case.case_id)
    if recipe is None:
        raise FixtureBuildFailure(f"Fixture {case.case_id} has no channel-identity generation recipe.")
    layout, channels = recipe
    duration_seconds = 2 * IDENTITY_GUARD_SECONDS + len(channels) * IDENTITY_SLOT_SECONDS
    create_channel_identity_audio(
        output,
        layout,
        tuple(_channel_frequency(channel) for channel in channels),
        duration_seconds=duration_seconds,
        cycle_seconds=duration_seconds,
        slot_seconds=IDENTITY_SLOT_SECONDS,
        burst_seconds=IDENTITY_BURST_SECONDS,
        start_offset_seconds=IDENTITY_GUARD_SECONDS,
    )


def _audio_encoding_command(track: TrackSpec, source: Path, output: Path) -> list[str | Path]:
    command: list[str | Path] = [
        config.FFMPEG_PATH,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        source,
        "-map",
        "0:a:0",
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        "-fflags",
        "+bitexact",
    ]
    if track.codec_name == "aac":
        command.extend(["-c:a", "aac", "-profile:a", "aac_low", "-b:a", "384k"])
    elif track.codec_name == "flac":
        command.extend(["-c:a", "flac"])
    elif track.codec_name == "pcm_s24le":
        command.extend(["-c:a", "pcm_s24le"])
        if track.source_layout is None:
            command.extend(["-ch_layout", f"{track.channels}c"])
    else:
        raise FixtureBuildFailure(f"Unsupported fixture audio codec: {track.codec_name}")
    command.extend(["-y", output])
    return command


def _mux_command(
    mvc_source: Path,
    case: FixtureCase,
    audio_sources: Sequence[Path],
    output: Path,
) -> list[str | Path]:
    command: list[str | Path] = [config.FFMPEG_PATH, "-hide_banner", "-loglevel", "error", "-i", mvc_source]
    for audio_source in audio_sources:
        command.extend(["-i", audio_source])
    command.extend(["-map", "0:v:0"])
    for index in range(len(audio_sources)):
        command.extend(["-map", f"{index + 1}:a:0"])
    command.extend(
        [
            "-c",
            "copy",
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            "-fflags",
            "+bitexact",
        ]
    )
    for index, track in enumerate(case.tracks):
        command.extend([f"-metadata:s:a:{index}", f"language={track.language}"])
        command.extend([f"-metadata:s:a:{index}", f"title={track.handler_title}"])
        command.extend([f"-disposition:a:{index}", "default" if track.default else "0"])
    command.extend(["-y", output])
    return command


def _version_line(command: Path) -> str:
    completed = _run([command, "-version"])
    return completed.stdout.splitlines()[0]


def create_packaged_aac_layout_fixtures(
    mvc_source: Path,
    output_directory: Path,
    *,
    manifest_path: Path = DEFAULT_MANIFEST,
) -> Mapping[str, object]:
    source = mvc_source.expanduser().resolve()
    output = output_directory.expanduser().resolve()
    manifest_path = manifest_path.expanduser().resolve()
    if not source.is_file():
        raise FixtureBuildFailure("MVC fixture source does not exist.")
    if output.exists():
        raise FixtureBuildFailure("Fixture output directory must not already exist.")
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        source_size = source.stat().st_size
    except OSError as error:
        raise FixtureBuildFailure("MVC fixture source metadata is unavailable.") from error
    if source_size > MAX_SOURCE_SIZE_BYTES:
        raise FixtureBuildFailure("MVC fixture source exceeds the bounded fixture-generation size limit.")
    source_sha256 = _hash_file(source, "MVC fixture source")
    manifest_sha256 = _hash_file(manifest_path, "fixture manifest")
    ffmpeg_sha256 = _hash_file(config.FFMPEG_PATH, "fixture FFmpeg")
    ffprobe_sha256 = _hash_file(config.FFPROBE_PATH, "fixture FFprobe")
    manifest = load_fixture_manifest(manifest_path)
    source_video = _source_video_summary(source)
    ffmpeg_version = _version_line(config.FFMPEG_PATH)
    ffprobe_version = _version_line(config.FFPROBE_PATH)
    try:
        validated_source_size = source.stat().st_size
    except OSError as error:
        raise FixtureBuildFailure("MVC fixture source metadata became unavailable.") from error
    if validated_source_size != source_size or _hash_file(source, "MVC fixture source") != source_sha256:
        raise FixtureBuildFailure("MVC fixture source changed during validation.")
    if _hash_file(manifest_path, "fixture manifest") != manifest_sha256:
        raise FixtureBuildFailure("Fixture manifest changed during validation.")
    if _hash_file(config.FFMPEG_PATH, "fixture FFmpeg") != ffmpeg_sha256:
        raise FixtureBuildFailure("Fixture FFmpeg changed during validation.")
    if _hash_file(config.FFPROBE_PATH, "fixture FFprobe") != ffprobe_sha256:
        raise FixtureBuildFailure("Fixture FFprobe changed during validation.")
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
    work_directory = temporary / ".work"
    work_directory.mkdir()
    fixture_evidence: list[dict[str, object]] = []
    published = False
    try:
        for case in manifest.cases:
            encoded_audio: list[Path] = []
            for index, track in enumerate(case.tracks):
                identity_source = work_directory / f"{case.case_id}-{index}.wav"
                encoded_source = work_directory / f"{case.case_id}-{index}.mka"
                _create_identity_source(case, track, identity_source)
                _run(_audio_encoding_command(track, identity_source, encoded_source))
                encoded_audio.append(encoded_source)
            fixture_path = temporary / case.source_file
            _run(_mux_command(source, case, encoded_audio, fixture_path))
            observed_streams = _audio_streams(_probe_document(fixture_path))
            _validate_streams(case, observed_streams, [_input_summary(track) for track in case.tracks])
            fixture_evidence.append(
                {
                    "id": case.case_id,
                    "file": case.source_file,
                    "sha256": _hash_file(fixture_path, f"fixture {case.case_id}"),
                    "size_bytes": fixture_path.stat().st_size,
                    "audio_streams": observed_streams,
                }
            )
        shutil.rmtree(work_directory)
        try:
            current_source_size = source.stat().st_size
        except OSError as error:
            raise FixtureBuildFailure("MVC fixture source metadata became unavailable.") from error
        if current_source_size != source_size or _hash_file(source, "MVC fixture source") != source_sha256:
            raise FixtureBuildFailure("MVC fixture source changed during generation.")
        if _hash_file(manifest_path, "fixture manifest") != manifest_sha256:
            raise FixtureBuildFailure("Fixture manifest changed during generation.")
        if _hash_file(config.FFMPEG_PATH, "fixture FFmpeg") != ffmpeg_sha256:
            raise FixtureBuildFailure("Fixture FFmpeg changed during generation.")
        if _hash_file(config.FFPROBE_PATH, "fixture FFprobe") != ffprobe_sha256:
            raise FixtureBuildFailure("Fixture FFprobe changed during generation.")
        receipt: dict[str, object] = {
            "schema_version": 2,
            "generated_at_utc": datetime.now(UTC).isoformat(),
            "fixture_set_id": manifest.fixture_set_id,
            "manifest_sha256": manifest_sha256,
            "source": {
                "sha256": source_sha256,
                "size_bytes": source_size,
                "video": source_video,
            },
            "tools": {
                "ffmpeg": {
                    "sha256": ffmpeg_sha256,
                    "version": ffmpeg_version,
                },
                "ffprobe": {
                    "sha256": ffprobe_sha256,
                    "version": ffprobe_version,
                },
            },
            "fixtures": fixture_evidence,
        }
        (temporary / "fixture-receipt.json").write_text(
            json.dumps(receipt, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if output.exists():
            raise FixtureBuildFailure("Fixture output directory appeared during generation.")
        os.replace(temporary, output)
        published = True
        return receipt
    finally:
        if not published:
            shutil.rmtree(temporary, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create the private checked AAC package fixture set from a short real MVC MKV."
    )
    parser.add_argument("--mvc-source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args()
    try:
        with _managed_sigterm_exit():
            receipt = create_packaged_aac_layout_fixtures(
                args.mvc_source,
                args.output,
                manifest_path=args.manifest,
            )
    except (FixtureBuildFailure, PackagedAacFailure) as error:
        parser.exit(1, f"error: {error}\n")
    except OSError:
        parser.exit(1, "error: Fixture generation encountered a filesystem error.\n")
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
