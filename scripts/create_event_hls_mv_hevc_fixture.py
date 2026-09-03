#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET

from pathlib import Path
from typing import Mapping, Sequence

from bd_to_avp.modules.config import resolve_tool_path, tool_env_var


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DIRECT_FIXTURE_GENERATOR = REPOSITORY_ROOT / "scripts/create_direct_mv_hevc_playback_fixture.sh"
BUNDLED_BIN = REPOSITORY_ROOT / "bd_to_avp/bin"
DEFAULT_OUTPUT_DIRECTORY = REPOSITORY_ROOT / "build/relay-fixtures/mv-hevc-event-hls"
INIT_FILENAME = "init.mp4"
PLAYLIST_FILENAME = "media.m3u8"
MPD_FILENAME = "fragmentation.mpd"
SEGMENT_PREFIX = "segment-"
SEGMENT_SUFFIX = ".m4s"
TARGET_DURATION_SECONDS = 2
SEGMENT_DURATION_MILLISECONDS = TARGET_DURATION_SECONDS * 1_000
MAX_SEGMENT_COUNT = 12
MAX_SEGMENT_DURATION_SECONDS = TARGET_DURATION_SECONDS + 0.25
REQUIRED_MV_HEVC_BOX_TYPES = frozenset({"hvcC", "lhvC", "eyes", "vexu"})
BOX_TYPE_PATTERN = re.compile(r'Type="([A-Za-z0-9 ]{4})"')
MAP_PATTERN = re.compile(r'^#EXT-X-MAP:URI="([^"]+)"$')
EXTINF_PATTERN = re.compile(r"^#EXTINF:([0-9]+(?:\.[0-9]+)?),$")


class FixtureGenerationError(RuntimeError):
    pass


def _tool_path(name: str) -> Path:
    configured = os.environ.get(tool_env_var(name))
    path = resolve_tool_path(name, script_bin_path=BUNDLED_BIN)
    if configured or path.is_file():
        return path
    raise FixtureGenerationError(f"Required tool is unavailable: {name}")


def _command_environment(*tools: Path) -> dict[str, str]:
    environment = os.environ.copy()
    path_entries = [str(tool.parent) for tool in tools]
    existing_path = environment.get("PATH", "")
    environment["PATH"] = os.pathsep.join([*dict.fromkeys(path_entries), existing_path])
    return environment


def _run(
    command: Sequence[str | Path], *, environment: Mapping[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [str(item) for item in command],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(environment) if environment is not None else None,
            timeout=600,
        )
    except subprocess.TimeoutExpired as error:
        raise FixtureGenerationError(f"Fixture command timed out: {Path(str(command[0])).name}") from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "").strip()
        raise FixtureGenerationError(
            f"Fixture command failed: {Path(str(command[0])).name}: {detail or 'no diagnostic output'}"
        ) from error
    except OSError as error:
        raise FixtureGenerationError(f"Fixture command could not start: {Path(str(command[0])).name}") from error


def source_generator_command(source_path: Path, encoder_path: Path | None = None) -> list[str | Path]:
    command: list[str | Path] = [DIRECT_FIXTURE_GENERATOR, source_path]
    if encoder_path is not None:
        command.append(encoder_path)
    return command


def hls_packaging_command(mp4box_path: Path, source_path: Path, output_directory: Path) -> list[str | Path]:
    return [
        mp4box_path,
        "-dash",
        str(SEGMENT_DURATION_MILLISECONDS),
        "-frag",
        str(SEGMENT_DURATION_MILLISECONDS),
        "-rap",
        "-segment-name",
        SEGMENT_PREFIX,
        source_path,
        "-out",
        output_directory / MPD_FILENAME,
    ]


def _probe_document(ffprobe_path: Path, media_path: Path) -> dict[str, object]:
    completed = _run(
        [
            ffprobe_path,
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            media_path,
        ]
    )
    try:
        document = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise FixtureGenerationError(f"FFprobe returned malformed media JSON for {media_path.name}.") from error
    if not isinstance(document, dict):
        raise FixtureGenerationError(f"FFprobe returned a non-object for {media_path.name}.")
    return document


def _stream_duration(stream: Mapping[str, object]) -> float | None:
    value = stream.get("duration")
    try:
        duration = float(str(value))
    except (TypeError, ValueError):
        return None
    return duration if math.isfinite(duration) else None


def _validate_media_tracks(document: Mapping[str, object], *, require_audio: bool) -> None:
    raw_streams = document.get("streams")
    streams = [stream for stream in raw_streams if isinstance(stream, Mapping)] if isinstance(raw_streams, list) else []
    video_streams = [stream for stream in streams if stream.get("codec_type") == "video"]
    audio_streams = [stream for stream in streams if stream.get("codec_type") == "audio"]
    if len(video_streams) != 1 or video_streams[0].get("codec_name") not in {"hevc", "h265"}:
        raise FixtureGenerationError("Fixture must contain exactly one HEVC video track.")
    video = video_streams[0]
    if video.get("width") != 3840 or video.get("height") != 2160:
        raise FixtureGenerationError(
            f"Fixture has unexpected video dimensions: {video.get('width')}x{video.get('height')}."
        )
    if require_audio and not audio_streams:
        raise FixtureGenerationError("Fixture is missing the synchronized AAC audio track.")
    if audio_streams and not any(stream.get("codec_name") == "aac" for stream in audio_streams):
        raise FixtureGenerationError("Fixture audio track is not AAC.")
    if audio_streams:
        video_duration = _stream_duration(video)
        audio_duration = _stream_duration(audio_streams[0])
        if video_duration is not None and audio_duration is not None and abs(video_duration - audio_duration) > 0.25:
            raise FixtureGenerationError(
                f"Fixture video/audio durations are not synchronized: {video_duration:.3f}s vs {audio_duration:.3f}s."
            )
        starts = []
        for stream in (video, audio_streams[0]):
            try:
                start = float(str(stream.get("start_time", "0")))
            except (TypeError, ValueError):
                start = 0.0
            if math.isfinite(start):
                starts.append(start)
        if starts and max(starts) - min(starts) > 0.1:
            raise FixtureGenerationError("Fixture video/audio tracks do not share a synchronized start time.")


def _validate_mv_hevc_boxes(mp4box_path: Path, init_path: Path) -> None:
    completed = _run([mp4box_path, "-diso", init_path, "-std"])
    box_types = set(BOX_TYPE_PATTERN.findall(completed.stdout))
    missing = sorted(REQUIRED_MV_HEVC_BOX_TYPES - box_types)
    if missing:
        raise FixtureGenerationError(f"Fixture init segment is missing MV-HEVC boxes: {', '.join(missing)}")


def _fragment_durations(mpd_path: Path, segment_count: int) -> list[float]:
    try:
        root = ET.parse(mpd_path).getroot()
    except (ET.ParseError, OSError) as error:
        raise FixtureGenerationError("MP4Box returned an unreadable fragmentation manifest.") from error
    template = root.find(".//{*}SegmentTemplate")
    if template is None:
        raise FixtureGenerationError("MP4Box fragmentation manifest has no SegmentTemplate.")
    try:
        timescale = int(template.attrib["timescale"])
    except (KeyError, ValueError) as error:
        raise FixtureGenerationError("MP4Box fragmentation manifest has no valid timescale.") from error
    if timescale <= 0:
        raise FixtureGenerationError("MP4Box fragmentation manifest has an invalid timescale.")
    if "duration" in template.attrib:
        try:
            duration = int(template.attrib["duration"]) / timescale
        except ValueError as error:
            raise FixtureGenerationError("MP4Box fragmentation manifest has an invalid segment duration.") from error
        return [duration] * segment_count

    timeline = template.find("{*}SegmentTimeline")
    if timeline is None:
        raise FixtureGenerationError("MP4Box fragmentation manifest has no segment timeline.")
    durations: list[float] = []
    for segment in timeline.findall("{*}S"):
        try:
            duration_units = int(segment.attrib["d"])
            repeat_count = int(segment.attrib.get("r", "0"))
        except ValueError as error:
            raise FixtureGenerationError("MP4Box fragmentation manifest has an invalid segment timeline.") from error
        if duration_units <= 0 or repeat_count < 0:
            raise FixtureGenerationError("MP4Box fragmentation manifest has invalid segment timing.")
        durations.extend([duration_units / timescale] * (repeat_count + 1))
    if len(durations) != segment_count:
        raise FixtureGenerationError("MP4Box fragmentation manifest and segment files disagree.")
    return durations


def _publish_fragments(fixture_directory: Path) -> list[float]:
    init_source = fixture_directory / f"{SEGMENT_PREFIX}init{INIT_FILENAME[4:]}"
    segment_sources = sorted(
        fixture_directory.glob(f"{SEGMENT_PREFIX}*{SEGMENT_SUFFIX}"),
        key=lambda path: int(path.stem.removeprefix(SEGMENT_PREFIX)),
    )
    if not init_source.is_file() or not segment_sources:
        raise FixtureGenerationError("MP4Box did not produce an init segment and at least two media segments.")
    if len(segment_sources) < 2 or len(segment_sources) > MAX_SEGMENT_COUNT:
        raise FixtureGenerationError(f"MP4Box produced an invalid segment count: {len(segment_sources)}.")
    mpd_path = fixture_directory / MPD_FILENAME
    durations = _fragment_durations(mpd_path, len(segment_sources))
    mpd_path.unlink()
    init_source.replace(fixture_directory / INIT_FILENAME)
    for index, source in enumerate(segment_sources):
        source.replace(fixture_directory / f"{SEGMENT_PREFIX}{index:03d}{SEGMENT_SUFFIX}")
    return durations


def _write_event_playlist(fixture_directory: Path, durations: Sequence[float]) -> None:
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:7",
        "#EXT-X-PLAYLIST-TYPE:EVENT",
        "#EXT-X-TARGETDURATION:2",
        "#EXT-X-MEDIA-SEQUENCE:0",
        '#EXT-X-MAP:URI="init.mp4"',
        "#EXT-X-INDEPENDENT-SEGMENTS",
    ]
    for index, duration in enumerate(durations):
        lines.extend([f"#EXTINF:{duration:.3f},", f"{SEGMENT_PREFIX}{index:03d}{SEGMENT_SUFFIX}"])
    lines.append("#EXT-X-ENDLIST")
    (fixture_directory / PLAYLIST_FILENAME).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _safe_segment_name(value: str) -> bool:
    return (
        value.startswith(SEGMENT_PREFIX)
        and value.endswith(SEGMENT_SUFFIX)
        and value == Path(value).name
        and value[len(SEGMENT_PREFIX) : -len(SEGMENT_SUFFIX)].isdigit()
    )


def _playlist_segments(playlist_path: Path, fixture_directory: Path) -> list[tuple[str, float]]:
    try:
        lines = playlist_path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise FixtureGenerationError("Could not read generated media.m3u8.") from error
    if not lines or lines[0] != "#EXTM3U":
        raise FixtureGenerationError("media.m3u8 must begin with #EXTM3U.")
    if "#EXT-X-PLAYLIST-TYPE:EVENT" not in lines:
        raise FixtureGenerationError("media.m3u8 must use EVENT playlist semantics.")
    if "#EXT-X-INDEPENDENT-SEGMENTS" not in lines:
        raise FixtureGenerationError("media.m3u8 must declare independent fMP4 segments.")
    try:
        target_duration = int(
            next(line.split(":", 1)[1] for line in lines if line.startswith("#EXT-X-TARGETDURATION:"))
        )
    except (StopIteration, ValueError) as error:
        raise FixtureGenerationError("media.m3u8 must declare an integer target duration.") from error
    if target_duration != TARGET_DURATION_SECONDS:
        raise FixtureGenerationError(f"media.m3u8 has unexpected target duration: {target_duration}.")
    map_matches = [MAP_PATTERN.match(line) for line in lines]
    map_values = [match.group(1) for match in map_matches if match]
    if map_values != [INIT_FILENAME]:
        raise FixtureGenerationError(f"media.m3u8 must map exactly to {INIT_FILENAME}.")

    segments: list[tuple[str, float]] = []
    pending_duration: float | None = None
    for line in lines:
        duration_match = EXTINF_PATTERN.match(line)
        if duration_match:
            pending_duration = float(duration_match.group(1))
            if not 0 < pending_duration <= MAX_SEGMENT_DURATION_SECONDS:
                raise FixtureGenerationError(f"media.m3u8 contains an unbounded segment duration: {pending_duration}.")
            continue
        if not line or line.startswith("#"):
            continue
        if pending_duration is None or not _safe_segment_name(line):
            raise FixtureGenerationError(f"media.m3u8 contains an invalid segment reference: {line!r}.")
        segment_path = fixture_directory / line
        if not segment_path.is_file() or segment_path.stat().st_size == 0:
            raise FixtureGenerationError(f"media.m3u8 references a missing or empty segment: {line}.")
        segments.append((line, pending_duration))
        pending_duration = None
    if pending_duration is not None:
        raise FixtureGenerationError("media.m3u8 ends with an EXTINF tag without a segment.")
    if not 2 <= len(segments) <= MAX_SEGMENT_COUNT:
        raise FixtureGenerationError(f"media.m3u8 must contain 2-{MAX_SEGMENT_COUNT} bounded segments.")
    return segments


def _assemble_media(fixture_directory: Path, segment_names: Sequence[str]) -> Path:
    assembled = fixture_directory.parent / "assembled-media.mp4"
    with assembled.open("wb") as output:
        output.write((fixture_directory / INIT_FILENAME).read_bytes())
        for segment_name in segment_names:
            output.write((fixture_directory / segment_name).read_bytes())
    return assembled


def validate_fixture(fixture_directory: Path, *, ffprobe_path: Path, mp4box_path: Path) -> None:
    init_path = fixture_directory / INIT_FILENAME
    playlist_path = fixture_directory / PLAYLIST_FILENAME
    if not init_path.is_file() or init_path.stat().st_size == 0:
        raise FixtureGenerationError(f"Generated fixture is missing {INIT_FILENAME}.")
    if not playlist_path.is_file() or playlist_path.stat().st_size == 0:
        raise FixtureGenerationError(f"Generated fixture is missing {PLAYLIST_FILENAME}.")
    segments = _playlist_segments(playlist_path, fixture_directory)
    _validate_mv_hevc_boxes(mp4box_path, init_path)
    assembled_path = _assemble_media(fixture_directory, [name for name, _duration in segments])
    try:
        _validate_media_tracks(_probe_document(ffprobe_path, assembled_path), require_audio=True)
    finally:
        assembled_path.unlink(missing_ok=True)


def create_fixture(output_directory: Path, *, encoder_path: Path | None = None, overwrite: bool = False) -> None:
    output_directory = output_directory.expanduser().resolve()
    if encoder_path is not None:
        encoder_path = encoder_path.expanduser().resolve()
    if output_directory.exists() and not overwrite:
        raise FixtureGenerationError(f"Output directory already exists; pass --overwrite: {output_directory}")
    ffmpeg_path = _tool_path("ffmpeg")
    ffprobe_path = _tool_path("ffprobe")
    mp4box_path = REPOSITORY_ROOT / "bd_to_avp/bin/MP4Box"
    if not mp4box_path.is_file() or not os.access(mp4box_path, os.X_OK):
        raise FixtureGenerationError(f"Required bundled tool is unavailable: {mp4box_path}")
    if not DIRECT_FIXTURE_GENERATOR.is_file() or not os.access(DIRECT_FIXTURE_GENERATOR, os.X_OK):
        raise FixtureGenerationError(f"Required source fixture generator is unavailable: {DIRECT_FIXTURE_GENERATOR}")
    if encoder_path is not None and (not encoder_path.is_file() or not os.access(encoder_path, os.X_OK)):
        raise FixtureGenerationError(f"Requested MV-HEVC encoder is unavailable: {encoder_path}")

    output_directory.parent.mkdir(parents=True, exist_ok=True)
    environment = _command_environment(ffmpeg_path, ffprobe_path)
    with tempfile.TemporaryDirectory(prefix="mv-hevc-event-hls-") as temporary_directory:
        temporary_root = Path(temporary_directory)
        source_path = temporary_root / "source.mov"
        fixture_directory = temporary_root / "fixture"
        fixture_directory.mkdir()
        _run(source_generator_command(source_path, encoder_path), environment=environment)
        _validate_media_tracks(_probe_document(ffprobe_path, source_path), require_audio=True)
        _run(hls_packaging_command(mp4box_path, source_path, fixture_directory), environment=environment)
        _write_event_playlist(fixture_directory, _publish_fragments(fixture_directory))
        validate_fixture(fixture_directory, ffprobe_path=ffprobe_path, mp4box_path=mp4box_path)
        if output_directory.exists():
            shutil.rmtree(output_directory)
        fixture_directory.replace(output_directory)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate a small deterministic MV-HEVC stereo EVENT-HLS fixture for RelayHost ingestion."
    )
    parser.add_argument("output", nargs="?", type=Path, default=DEFAULT_OUTPUT_DIRECTORY)
    parser.add_argument("--encoder", type=Path, help="Use an existing MV-HEVC encoder instead of building the default.")
    parser.add_argument(
        "--overwrite", action="store_true", help="Replace an existing fixture directory after validation."
    )
    args = parser.parse_args()
    try:
        create_fixture(args.output, encoder_path=args.encoder, overwrite=args.overwrite)
    except FixtureGenerationError as error:
        parser.error(str(error))
    print(f"Created MV-HEVC EVENT-HLS fixture: {args.output.expanduser().resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
