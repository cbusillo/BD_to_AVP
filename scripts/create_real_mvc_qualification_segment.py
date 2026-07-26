#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile

from datetime import UTC, datetime
from pathlib import Path
from typing import Mapping, Sequence


MVC_STEREO_MODE = 13
MAX_EVIDENCE_BYTES = 128 * 1024


class SegmentFailure(RuntimeError):
    pass


def command_path(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise SegmentFailure(f"Required command is unavailable: {name}")
    return path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def format_seconds(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".")


def redact_private_paths(message: str, paths: Sequence[Path]) -> str:
    redacted = message
    for path in paths:
        for candidate in {str(path), str(path.resolve())}:
            redacted = redacted.replace(candidate, "<private-source>")
    return redacted


def run(
    command: Sequence[str | Path],
    *,
    private_paths: Sequence[Path] = (),
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [str(item) for item in command],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "").strip()
        redacted = redact_private_paths(detail, private_paths)
        raise SegmentFailure(f"Qualification command failed: {redacted or 'no diagnostic output'}") from error


def run_json(
    command: Sequence[str | Path],
    *,
    private_paths: Sequence[Path] = (),
) -> Mapping[str, object]:
    completed = run(command, private_paths=private_paths)
    try:
        document = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise SegmentFailure("Qualification command did not emit valid JSON.") from error
    if not isinstance(document, Mapping):
        raise SegmentFailure("Qualification command did not emit a JSON object.")
    return document


def tool_version(command: str, *, argument: str) -> str:
    completed = run([command, argument])
    line = completed.stdout.splitlines()[0].strip() if completed.stdout else ""
    if not line:
        raise SegmentFailure(f"Could not read the version for {Path(command).name}.")
    return line


def build_mkvmerge_command(
    mkvmerge: str,
    source: Path,
    output: Path,
    *,
    start_seconds: float,
    duration_seconds: float,
    video_track: int,
    audio_track: int,
    subtitle_track: int,
    deterministic_seed: str,
) -> list[str]:
    end_seconds = start_seconds + duration_seconds
    return [
        mkvmerge,
        "--output",
        str(output),
        "--deterministic",
        deterministic_seed,
        "--no-date",
        "--disable-track-statistics-tags",
        "--title",
        "",
        "--no-chapters",
        "--no-attachments",
        "--no-global-tags",
        "--split",
        f"parts:{format_seconds(start_seconds)}s-{format_seconds(end_seconds)}s",
        "--video-tracks",
        str(video_track),
        "--audio-tracks",
        str(audio_track),
        "--subtitle-tracks",
        str(subtitle_track),
        "--track-name",
        f"{video_track}:",
        "--track-name",
        f"{audio_track}:",
        "--track-name",
        f"{subtitle_track}:",
        str(source),
    ]


def create_segment(
    mkvmerge: str,
    source: Path,
    output: Path,
    *,
    start_seconds: float,
    duration_seconds: float,
    video_track: int,
    audio_track: int,
    subtitle_track: int,
    deterministic_seed: str,
) -> None:
    command = build_mkvmerge_command(
        mkvmerge,
        source,
        output,
        start_seconds=start_seconds,
        duration_seconds=duration_seconds,
        video_track=video_track,
        audio_track=audio_track,
        subtitle_track=subtitle_track,
        deterministic_seed=deterministic_seed,
    )
    run(command, private_paths=(source,))
    if not output.is_file():
        raise SegmentFailure("mkvmerge did not create the requested qualification segment.")


def _tracks(document: Mapping[str, object]) -> list[Mapping[str, object]]:
    tracks = document.get("tracks")
    if not isinstance(tracks, list) or not all(isinstance(track, Mapping) for track in tracks):
        raise SegmentFailure("Matroska inspection did not report a valid track list.")
    return tracks


def _track_properties(track: Mapping[str, object]) -> Mapping[str, object]:
    properties = track.get("properties")
    return properties if isinstance(properties, Mapping) else {}


def public_track_summary(document: Mapping[str, object]) -> list[dict[str, object]]:
    summary: list[dict[str, object]] = []
    for track in _tracks(document):
        properties = _track_properties(track)
        summary.append(
            {
                "codec": track.get("codec"),
                "language": properties.get("language"),
                "pixel_dimensions": properties.get("pixel_dimensions"),
                "stereo_mode": properties.get("stereo_mode"),
                "type": track.get("type"),
            }
        )
    return summary


def validate_segment_document(document: Mapping[str, object]) -> None:
    tracks = _tracks(document)
    observed_types = [track.get("type") for track in tracks]
    if not all(isinstance(track_type, str) for track_type in observed_types):
        raise SegmentFailure("Qualification segment reported an invalid track type.")
    typed_observed_types = [track_type for track_type in observed_types if isinstance(track_type, str)]
    if sorted(typed_observed_types) != ["audio", "subtitles", "video"]:
        raise SegmentFailure("Qualification segment must contain one video, one audio, and one subtitle track.")
    video_track = next(track for track in tracks if track.get("type") == "video")
    video_properties = _track_properties(video_track)
    if video_properties.get("stereo_mode") != MVC_STEREO_MODE:
        raise SegmentFailure("Qualification segment did not preserve the MVC both-eyes-laced track mode.")
    if video_properties.get("pixel_dimensions") != "1920x1080":
        raise SegmentFailure("Qualification segment did not preserve 1920x1080 MVC eye dimensions.")
    if "AVC/H.264" not in str(video_track.get("codec")):
        raise SegmentFailure("Qualification segment did not preserve the MVC AVC video codec.")
    audio_track = next(track for track in tracks if track.get("type") == "audio")
    subtitle_track = next(track for track in tracks if track.get("type") == "subtitles")
    if not isinstance(audio_track.get("codec"), str) or not audio_track["codec"]:
        raise SegmentFailure("Qualification segment did not preserve an audio codec.")
    if subtitle_track.get("codec") != "HDMV PGS":
        raise SegmentFailure("Qualification segment did not preserve an HDMV PGS subtitle track.")


def media_duration_seconds(document: Mapping[str, object]) -> float:
    format_data = document.get("format")
    if not isinstance(format_data, Mapping) or format_data.get("duration") is None:
        raise SegmentFailure("Media inspection did not report a duration.")
    try:
        duration_seconds = float(format_data["duration"])
    except (TypeError, ValueError) as error:
        raise SegmentFailure("Media inspection reported an invalid duration.") from error
    if duration_seconds <= 0:
        raise SegmentFailure("Media inspection reported a non-positive duration.")
    return duration_seconds


def build_evidence(
    *,
    source_sha256: str,
    source_size_bytes: int,
    source_duration_seconds: float,
    source_track_count: int,
    segment_sha256: str,
    segment_size_bytes: int,
    segment_duration_seconds: float,
    segment_tracks: list[dict[str, object]],
    repeat_sha256: str,
    start_seconds: float,
    requested_duration_seconds: float,
    video_track: int,
    audio_track: int,
    subtitle_track: int,
    deterministic_seed: str,
    mkvmerge_version: str,
    ffprobe_version: str,
) -> dict[str, object]:
    deterministic = segment_sha256 == repeat_sha256
    evidence = {
        "acceptance": {
            "deterministic_repeat_match": deterministic,
            "preserved_mvc_both_eyes_laced": True,
            "preserved_required_track_types": True,
            "passed": deterministic,
        },
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "schema_version": 1,
        "segment": {
            "duration_seconds": segment_duration_seconds,
            "sha256": segment_sha256,
            "size_bytes": segment_size_bytes,
            "tracks": segment_tracks,
        },
        "selection": {
            "audio_track": audio_track,
            "deterministic_seed": deterministic_seed,
            "requested_duration_seconds": requested_duration_seconds,
            "start_seconds": start_seconds,
            "subtitle_track": subtitle_track,
            "video_track": video_track,
        },
        "source": {
            "duration_seconds": source_duration_seconds,
            "sha256": source_sha256,
            "size_bytes": source_size_bytes,
            "track_count": source_track_count,
        },
        "supersedes": {
            "description": "Deleted 65.648-second representative MVC clip",
            "reason": "Deterministically regenerated from the verified feature-length parent source",
        },
        "tools": {
            "ffprobe": ffprobe_version,
            "mkvmerge": mkvmerge_version,
        },
    }
    encoded = (json.dumps(evidence, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(encoded) > MAX_EVIDENCE_BYTES:
        raise SegmentFailure("Qualification segment evidence exceeded its bounded size limit.")
    return evidence


def qualify_segment(args: argparse.Namespace) -> dict[str, object]:
    if args.start_seconds < 0 or args.duration_seconds <= 0:
        raise SegmentFailure("Segment start must be non-negative and duration must be positive.")
    if min(args.video_track, args.audio_track, args.subtitle_track) < 0:
        raise SegmentFailure("Selected Matroska track ids must be non-negative.")

    source = args.source.expanduser().resolve()
    output = args.output.expanduser().resolve()
    evidence_output = args.evidence.expanduser().resolve()
    if not source.is_file():
        raise SegmentFailure("Private MVC source is unavailable.")
    if output == source or evidence_output in {source, output}:
        raise SegmentFailure("Source, segment, and evidence destinations must be distinct.")
    if (output.exists() or evidence_output.exists()) and not args.overwrite:
        raise SegmentFailure("Output already exists; pass --overwrite to replace qualification artifacts.")

    mkvmerge = command_path("mkvmerge")
    ffprobe = command_path("ffprobe")
    source_sha256 = sha256_file(source)
    if args.expected_source_sha256 is not None and source_sha256 != args.expected_source_sha256.lower():
        raise SegmentFailure("Private MVC source SHA-256 does not match the expected parent source.")
    source_mkv = run_json([mkvmerge, "-J", source], private_paths=(source,))
    source_probe = run_json(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "json", source],
        private_paths=(source,),
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    evidence_output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="real-mvc-segment-", dir=output.parent) as temporary_directory:
        temporary_root = Path(temporary_directory)
        first_segment = temporary_root / "segment.mkv"
        repeat_segment = temporary_root / "segment-repeat.mkv"
        for destination in (first_segment, repeat_segment):
            create_segment(
                mkvmerge,
                source,
                destination,
                start_seconds=args.start_seconds,
                duration_seconds=args.duration_seconds,
                video_track=args.video_track,
                audio_track=args.audio_track,
                subtitle_track=args.subtitle_track,
                deterministic_seed=args.deterministic_seed,
            )
        segment_sha256 = sha256_file(first_segment)
        repeat_sha256 = sha256_file(repeat_segment)
        if segment_sha256 != repeat_sha256:
            raise SegmentFailure("Repeated deterministic segment generation produced different SHA-256 values.")

        segment_mkv = run_json([mkvmerge, "-J", first_segment])
        validate_segment_document(segment_mkv)
        segment_probe = run_json(
            [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "json", first_segment]
        )
        segment_duration_seconds = media_duration_seconds(segment_probe)
        if abs(segment_duration_seconds - args.duration_seconds) > 0.5:
            raise SegmentFailure("Qualification segment duration differs from the requested range by more than 0.5s.")
        evidence = build_evidence(
            source_sha256=source_sha256,
            source_size_bytes=source.stat().st_size,
            source_duration_seconds=media_duration_seconds(source_probe),
            source_track_count=len(_tracks(source_mkv)),
            segment_sha256=segment_sha256,
            segment_size_bytes=first_segment.stat().st_size,
            segment_duration_seconds=segment_duration_seconds,
            segment_tracks=public_track_summary(segment_mkv),
            repeat_sha256=repeat_sha256,
            start_seconds=args.start_seconds,
            requested_duration_seconds=args.duration_seconds,
            video_track=args.video_track,
            audio_track=args.audio_track,
            subtitle_track=args.subtitle_track,
            deterministic_seed=args.deterministic_seed,
            mkvmerge_version=tool_version(mkvmerge, argument="--version"),
            ffprobe_version=tool_version(ffprobe, argument="-version"),
        )
        first_segment.replace(output)
        output.chmod(0o600)

    evidence_text = json.dumps(evidence, indent=2, sort_keys=True) + "\n"
    temporary_evidence = evidence_output.with_suffix(evidence_output.suffix + ".tmp")
    temporary_evidence.write_text(evidence_text, encoding="utf-8")
    temporary_evidence.replace(evidence_output)
    return evidence


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create and hash a deterministic, public-safe real-MVC qualification segment."
    )
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--start-seconds", type=float, required=True)
    parser.add_argument("--duration-seconds", type=float, required=True)
    parser.add_argument("--video-track", type=int, required=True)
    parser.add_argument("--audio-track", type=int, required=True)
    parser.add_argument("--subtitle-track", type=int, required=True)
    parser.add_argument("--expected-source-sha256")
    parser.add_argument("--deterministic-seed", default="bd-to-avp-real-mvc-segment-v1")
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        evidence = qualify_segment(args)
    except (OSError, SegmentFailure) as error:
        raise SystemExit(f"error: {error}") from error
    print(json.dumps(evidence, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
