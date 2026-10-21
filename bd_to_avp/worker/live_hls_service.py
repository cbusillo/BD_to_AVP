"""Bounded live MV-HEVC HLS production from one direct SSIF source read.

One ``ssif_probe stream-service`` read feeds both the MVC decoder and the selected
audio decoder. The MV-HEVC encoder writes into a private staging directory; the
publisher here is the single owner of the public workspace: it renames finished
segments into place, evicts whole segments to honor the retention limits, and
atomically republishes ``media.m3u8`` and ``live-hls.json``.
"""

from __future__ import annotations

from contextlib import suppress
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time

from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, Callable, Mapping, cast

from bd_to_avp.modules.config import resolve_tool_path
from bd_to_avp.process_runner import (
    CaptureOverflowPolicy,
    ProcessCancelled,
    ProcessExecutionError,
    ProcessResult,
    ProcessRunnerError,
    ProcessSpec,
)
from bd_to_avp.worker.ownership import WorkerCancelled, WorkerProcessOwner
from bd_to_avp.worker.protocol import JobSpec, LiveHLSOptions, WorkerActivityReporter
from bd_to_avp.worker.source_service import (
    MAXIMUM_DIAGNOSTIC_BYTES,
    SOURCE_FRAME_TIMEOUT_SECONDS,
    TIMESTAMP_HALF_RANGE,
    TIMESTAMP_MODULUS,
    CombinedCancellation,
    ServiceFrameReader,
    ServiceRecord,
    ServiceRecordKind,
    SourceServiceError,
    SSIFSourceHelper,
)

if TYPE_CHECKING:
    from bd_to_avp.worker.source_handoff import HeldSource


MANIFEST_FILENAME = "live-hls.json"
PLAYLIST_FILENAME = "media.m3u8"
INITIALIZATION_FILENAME = "init.mp4"
PIPELINE_DIRECTORY = ".pipeline"
SEGMENT_DURATION_SECONDS = 2
VIDEO_BITRATE_MBPS = 20
AUDIO_SAMPLE_RATE = 48_000
AUDIO_CHANNELS = 2
PUBLISH_POLL_SECONDS = 0.1
HOLD_POLL_SECONDS = 0.02
# Completed segments allowed to wait in staging, and so the media the worker lets run
# ahead of publication (source records not yet published as segments).
STAGING_SEGMENT_CAP = 2
NO_PROGRESS_TIMEOUT_SECONDS = SOURCE_FRAME_TIMEOUT_SECONDS
STARTUP_RECORD_LIMIT = 4_096
STARTUP_BYTE_LIMIT = 64 * 1_024 * 1_024
FEEDER_BYTE_LIMIT = 64 * 1_024 * 1_024
MAXIMUM_STAGING_PLAYLIST_BYTES = 16 * 1_024 * 1_024
STAGED_SEGMENT_PATTERN = re.compile(r"segment-[0-9]{5,}\.m4s")

# Blu-ray stream coding types whose PES payloads FFmpeg reads as a raw elementary stream.
AUDIO_INPUT_FORMATS = {
    0x81: "ac3",
    0x82: "dts",
    0x84: "eac3",
    0x85: "dts",
    0x86: "dts",
    0xA1: "eac3",
    0xA2: "dts",
}
# Blu-ray progressive video formats and frame rates the normalizer can describe.
VIDEO_FORMAT_DIMENSIONS = {5: (1280, 720), 6: (1920, 1080)}
VIDEO_FRAME_RATES = {1: "24000/1001", 2: "24/1", 3: "25/1", 4: "30000/1001", 6: "50/1", 7: "60000/1001"}


def live_video_shape(title: Mapping[str, object]) -> tuple[int, int, str]:
    """Return the eye size and frame rate live HLS produces for an eligible single-clip title."""
    clip = cast(Mapping[str, object], cast(list[object], title["clips"])[0])
    video_streams = clip.get("video_streams")
    if not isinstance(video_streams, list) or not video_streams or not isinstance(video_streams[0], Mapping):
        raise SourceServiceError("unsupported_live_video", "The live source has no described base video.")
    video = video_streams[0]
    dimensions = VIDEO_FORMAT_DIMENSIONS.get(cast(int, video.get("format")))
    frame_rate = VIDEO_FRAME_RATES.get(cast(int, video.get("rate")))
    if dimensions is None or frame_rate is None:
        raise SourceServiceError(
            "unsupported_live_video",
            "Live HLS requires progressive 720p or 1080p MVC video at a standard frame rate.",
        )
    return dimensions[0], dimensions[1], frame_rate


def live_audio_input_format(audio_stream: Mapping[str, object]) -> str:
    """Return the raw elementary-stream format FFmpeg reads for a supported live audio stream."""
    audio_format = AUDIO_INPUT_FORMATS.get(cast(int, audio_stream.get("coding_type")))
    if audio_format is None:
        raise SourceServiceError(
            "unsupported_live_audio",
            "Live HLS supports Dolby Digital, Dolby Digital Plus, and DTS selected audio.",
        )
    return audio_format


@dataclass(frozen=True)
class PublishedSegment:
    sequence: int
    uri: str
    start_microseconds: int
    duration_microseconds: int
    size_bytes: int

    @property
    def end_microseconds(self) -> int:
        return self.start_microseconds + self.duration_microseconds


@dataclass(frozen=True)
class RetiredSegment:
    """An evicted segment kept readable for clients still holding an older playlist."""

    uri: str
    size_bytes: int
    delete_after_unix: float


@dataclass(frozen=True)
class StagedPlaylist:
    target_duration_seconds: int
    initialization: str
    segments: tuple[tuple[str, int], ...]
    ended: bool


def parse_staged_playlist(text: str) -> StagedPlaylist:
    """Parses the encoder's private playlist; anything unexpected fails closed."""
    lines = text.split("\n")
    if not lines or lines[0] != "#EXTM3U":
        raise SourceServiceError("invalid_encoder_playlist", "The encoder returned an invalid HLS playlist.")
    target_duration: int | None = None
    initialization: str | None = None
    segments: list[tuple[str, int]] = []
    pending_duration: int | None = None
    ended = False
    for line in lines[1:]:
        if not line:
            continue
        if ended:
            raise SourceServiceError("invalid_encoder_playlist", "The encoder playlist continued after ENDLIST.")
        if line.startswith("#EXT-X-TARGETDURATION:"):
            raw = line.removeprefix("#EXT-X-TARGETDURATION:")
            if not raw.isdigit() or int(raw) <= 0:
                raise SourceServiceError("invalid_encoder_playlist", "The encoder playlist target is invalid.")
            target_duration = int(raw)
        elif line.startswith("#EXT-X-MAP:URI="):
            initialization = line.removeprefix("#EXT-X-MAP:URI=").strip('"')
        elif line.startswith("#EXTINF:"):
            pending_duration = _parse_duration_microseconds(line.removeprefix("#EXTINF:").rstrip(","))
        elif line == "#EXT-X-ENDLIST":
            ended = True
        elif line.startswith("#"):
            continue
        else:
            if pending_duration is None or not STAGED_SEGMENT_PATTERN.fullmatch(line):
                raise SourceServiceError("invalid_encoder_playlist", "The encoder playlist named an invalid segment.")
            segments.append((line, pending_duration))
            pending_duration = None
    if target_duration is None or initialization != INITIALIZATION_FILENAME or pending_duration is not None:
        raise SourceServiceError("invalid_encoder_playlist", "The encoder playlist is incomplete.")
    if any(duration > target_duration * 1_000_000 for _, duration in segments):
        raise SourceServiceError("invalid_encoder_playlist", "An encoder segment exceeds the target duration.")
    return StagedPlaylist(target_duration, initialization, tuple(segments), ended)


def _parse_duration_microseconds(raw: str) -> int:
    whole, _, fraction = raw.partition(".")
    if not whole.isdigit() or (fraction and not fraction.isdigit()) or len(fraction) > 6:
        raise SourceServiceError("invalid_encoder_playlist", "The encoder playlist has an invalid duration.")
    duration = int(whole) * 1_000_000 + int((fraction or "0").ljust(6, "0"))
    if duration <= 0:
        raise SourceServiceError("invalid_encoder_playlist", "The encoder playlist has an empty segment.")
    return duration


def _format_seconds(microseconds: int) -> str:
    return f"{microseconds // 1_000_000}.{microseconds % 1_000_000:06d}"


def _atomic_write(path: Path, text: str) -> None:
    temporary_path = path.with_name(f".{path.name}.tmp")
    with temporary_path.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    temporary_path.replace(path)


class HLSPublisher:
    """Publishes encoder segments into a bounded sliding-window HLS workspace.

    Publication order for every segment: the finished file is renamed to its public
    name, then the playlist and manifest are atomically replaced. Clients never see a
    playlist naming a partial file, and the manifest never advertises evicted history.

    RFC 8216 section 6.2.2 requires a removed segment to stay available for its
    duration plus the longest playlist duration that contained it. Evicted segments
    are therefore retired, not deleted, and are never removed before that deadline.
    Retired bytes are bounded by ``retired_allowance_bytes``. When publishing the next
    staged segment would push retired bytes past that allowance, the publisher holds
    the segment in staging (``held_for_grace``) until the oldest retired segment
    expires; the service stops reading the source meanwhile.
    """

    def __init__(
        self,
        root: Path,
        staging: Path,
        *,
        retention_seconds: int,
        retention_max_bytes: int,
        streams: Mapping[str, object],
        clock: Callable[[], float] = time.time,
        producer_stats: Callable[[], Mapping[str, object]] | None = None,
    ) -> None:
        self.root = root
        self.producer_stats = producer_stats
        self.clock = clock
        self.staging = staging
        self.retention_microseconds = retention_seconds * 1_000_000
        self.retention_seconds = retention_seconds
        self.retention_max_bytes = retention_max_bytes
        self.streams = dict(streams)
        self.segments: deque[PublishedSegment] = deque()
        self.target_duration_seconds: int | None = None
        self.initialization_bytes = 0
        self.staged_published = 0
        self.next_sequence = 0
        self.next_start_microseconds = 0
        self.complete = False
        self.peak_retained_bytes = 0
        self.retired: deque[RetiredSegment] = deque()
        self.longest_playlist_microseconds = 0
        self.peak_retired_bytes = 0
        self.max_segment_bytes = 0
        self.max_segment_microseconds = 0
        self.held_for_grace = False
        self.grace_holds = 0
        self.staged_backlog = 0
        self.peak_staged_backlog = 0

    @property
    def manifest_path(self) -> Path:
        return self.root / MANIFEST_FILENAME

    @property
    def playlist_path(self) -> Path:
        return self.root / PLAYLIST_FILENAME

    @property
    def retained_bytes(self) -> int:
        return self.initialization_bytes + sum(segment.size_bytes for segment in self.segments)

    @property
    def retired_bytes(self) -> int:
        return sum(segment.size_bytes for segment in self.retired)

    @property
    def retired_allowance_bytes(self) -> int:
        """Room for one grace period of retired history at realtime production.

        Each retired segment lives for its duration plus the longest playlist, so at
        realtime the retired set approaches one published window plus one segment.
        Two maximum-size segments of slack absorb segment-size variation.
        """
        return self.retention_max_bytes + 2 * self.max_segment_bytes

    @property
    def has_published(self) -> bool:
        return bool(self.segments)

    def poll(self) -> bool:
        """Publishes every newly finished staged segment; returns whether any were published."""
        self._delete_expired_retired()
        staged = self._read_staged_playlist()
        if staged is None:
            return False
        if self.target_duration_seconds is None:
            self.target_duration_seconds = staged.target_duration_seconds
            self._publish_initialization()
        elif staged.target_duration_seconds != self.target_duration_seconds:
            raise SourceServiceError("invalid_encoder_playlist", "The encoder changed its target duration.")
        if len(staged.segments) < self.staged_published:
            raise SourceServiceError("invalid_encoder_playlist", "The encoder playlist lost published segments.")
        published = False
        was_held = self.held_for_grace
        self.held_for_grace = False
        for name, duration in staged.segments[self.staged_published :]:
            if not self._publish_segment(name, duration):
                self.held_for_grace = True
                self.grace_holds += 0 if was_held else 1
                break
            self.staged_published += 1
            published = True
        self.staged_backlog = len(staged.segments) - self.staged_published
        self.peak_staged_backlog = max(self.peak_staged_backlog, self.staged_backlog)
        if staged.ended and self.staged_backlog == 0:
            self.complete = True
            self._write_outputs()
        return published

    def finish(self) -> None:
        self.poll()
        if not self.complete or not self.segments:
            raise SourceServiceError("incomplete_live_hls", "The encoder finished without a complete playlist.")

    def _read_staged_playlist(self) -> StagedPlaylist | None:
        path = self.staging / PLAYLIST_FILENAME
        try:
            with path.open("rb") as handle:
                data = handle.read(MAXIMUM_STAGING_PLAYLIST_BYTES + 1)
        except FileNotFoundError:
            return None
        if len(data) > MAXIMUM_STAGING_PLAYLIST_BYTES:
            raise SourceServiceError("invalid_encoder_playlist", "The encoder playlist is too large.")
        try:
            return parse_staged_playlist(data.decode("utf-8"))
        except UnicodeDecodeError as error:
            raise SourceServiceError("invalid_encoder_playlist", "The encoder playlist is not UTF-8.") from error

    def _publish_initialization(self) -> None:
        source = self.staging / INITIALIZATION_FILENAME
        try:
            size = source.stat().st_size
        except FileNotFoundError as error:
            raise SourceServiceError("invalid_encoder_output", "The encoder playlist has no initialization.") from error
        if size <= 0:
            raise SourceServiceError("invalid_encoder_output", "The encoder initialization segment is empty.")
        source.replace(self.root / INITIALIZATION_FILENAME)
        self.initialization_bytes = size

    def _publish_segment(self, staged_name: str, duration_microseconds: int) -> bool:
        """Publishes one staged segment, or returns False to hold it for retired grace."""
        source = self.staging / staged_name
        try:
            size = source.stat().st_size
        except FileNotFoundError as error:
            raise SourceServiceError(
                "invalid_encoder_output", "The encoder playlist named a missing segment."
            ) from error
        if size <= 0:
            raise SourceServiceError("invalid_encoder_output", "The encoder produced an empty segment.")
        if self.initialization_bytes + size > self.retention_max_bytes:
            raise SourceServiceError(
                "live_hls_segment_too_large",
                "One HLS segment exceeds the configured retention byte limit.",
            )
        segment = PublishedSegment(
            sequence=self.next_sequence,
            uri=f"segment-{self.next_sequence:09d}.m4s",
            start_microseconds=self.next_start_microseconds,
            duration_microseconds=duration_microseconds,
            size_bytes=size,
        )
        self.max_segment_bytes = max(self.max_segment_bytes, size)
        self.max_segment_microseconds = max(self.max_segment_microseconds, duration_microseconds)
        evicted_bytes = sum(old.size_bytes for old in self._eviction_plan(segment))
        if self.retired and self.retired_bytes + evicted_bytes > self.retired_allowance_bytes:
            return False
        source.replace(self.root / segment.uri)
        self.segments.append(segment)
        self.next_sequence += 1
        self.next_start_microseconds += duration_microseconds
        evicted = self._evict()
        self.peak_retained_bytes = max(self.peak_retained_bytes, self.retained_bytes)
        self.longest_playlist_microseconds = max(
            self.longest_playlist_microseconds,
            self.segments[-1].end_microseconds - self.segments[0].start_microseconds,
        )
        now = self.clock()
        for segment in evicted:
            grace_seconds = (segment.duration_microseconds + self.longest_playlist_microseconds) / 1_000_000
            self.retired.append(RetiredSegment(segment.uri, segment.size_bytes, now + grace_seconds))
        self.peak_retired_bytes = max(self.peak_retired_bytes, self.retired_bytes)
        self._write_outputs()
        return True

    def _delete_expired_retired(self) -> None:
        now = self.clock()
        changed = False
        while self.retired and self.retired[0].delete_after_unix <= now:
            (self.root / self.retired.popleft().uri).unlink(missing_ok=True)
            changed = True
        if changed:
            self._write_outputs()

    def _eviction_plan(self, new_segment: PublishedSegment) -> list[PublishedSegment]:
        """Segments that publishing ``new_segment`` would evict, oldest first."""
        window = [*self.segments, new_segment]
        window_bytes = self.initialization_bytes + sum(segment.size_bytes for segment in window)
        count = 0
        while len(window) - count > 1:
            duration = window[-1].end_microseconds - window[count].start_microseconds
            if duration <= self.retention_microseconds and window_bytes <= self.retention_max_bytes:
                break
            window_bytes -= window[count].size_bytes
            count += 1
        return window[:count]

    def _evict(self) -> list[PublishedSegment]:
        evicted: list[PublishedSegment] = []
        while len(self.segments) > 1:
            duration = self.segments[-1].end_microseconds - self.segments[0].start_microseconds
            if duration <= self.retention_microseconds and self.retained_bytes <= self.retention_max_bytes:
                break
            evicted.append(self.segments.popleft())
        if evicted and self.target_duration_seconds is not None:
            remaining = self.segments[-1].end_microseconds - self.segments[0].start_microseconds
            if remaining < 3 * self.target_duration_seconds * 1_000_000:
                raise SourceServiceError(
                    "live_hls_retention_too_small",
                    "The retention byte limit cannot hold three target durations of HLS media.",
                )
        return evicted

    def _write_outputs(self) -> None:
        if not self.segments or self.target_duration_seconds is None:
            return
        _atomic_write(self.playlist_path, self.render_playlist())
        _atomic_write(self.manifest_path, json.dumps(self.manifest(), sort_keys=True, separators=(",", ":")) + "\n")

    def render_playlist(self) -> str:
        assert self.target_duration_seconds is not None
        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:7",
            f"#EXT-X-TARGETDURATION:{self.target_duration_seconds}",
            f"#EXT-X-MEDIA-SEQUENCE:{self.segments[0].sequence}",
            "#EXT-X-INDEPENDENT-SEGMENTS",
            f'#EXT-X-MAP:URI="{INITIALIZATION_FILENAME}"',
        ]
        for segment in self.segments:
            lines.append(f"#EXTINF:{_format_seconds(segment.duration_microseconds)},")
            lines.append(segment.uri)
        if self.complete:
            lines.append("#EXT-X-ENDLIST")
        return "\n".join(lines) + "\n"

    def manifest(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "type": "live_hls",
            "complete": self.complete,
            "playlist": PLAYLIST_FILENAME,
            "initialization": INITIALIZATION_FILENAME,
            "target_duration_seconds": self.target_duration_seconds,
            "media_sequence": self.segments[0].sequence,
            "earliest_available_microseconds": self.segments[0].start_microseconds,
            "latest_available_microseconds": self.segments[-1].end_microseconds,
            "retention_seconds": self.retention_seconds,
            "retention_max_bytes": self.retention_max_bytes,
            "retained_bytes": self.retained_bytes,
            "segments": [segment.__dict__ for segment in self.segments],
            "retired_segments": [segment.__dict__ for segment in self.retired],
            "retired_bytes": self.retired_bytes,
            "retired_allowance_bytes": self.retired_allowance_bytes,
            **({"producer": dict(self.producer_stats())} if self.producer_stats is not None else {}),
            **self.streams,
        }

    def artifact(self) -> dict[str, object]:
        return {
            "kind": "live_hls_manifest",
            "manifest_path": str(self.manifest_path),
            "playlist_path": str(self.playlist_path),
        }

    def result(self) -> dict[str, object]:
        if not self.segments:
            raise SourceServiceError("empty_live_hls", "The live HLS stream produced no segments.")
        return {
            "manifest_path": str(self.manifest_path),
            "playlist_path": str(self.playlist_path),
            "earliest_available_microseconds": self.segments[0].start_microseconds,
            "latest_available_microseconds": self.segments[-1].end_microseconds,
            "retained_segments": len(self.segments),
            "published_segments": self.next_sequence,
            "peak_retained_bytes": self.peak_retained_bytes,
            "peak_retired_bytes": self.peak_retired_bytes,
            "peak_staged_segments": self.peak_staged_backlog,
            "grace_holds": self.grace_holds,
        }


class PipeFeeder:
    """Writes records to one child's stdin on its own thread with a bounded queue.

    Separate feeders let the video decoder and audio decoder drain independently, so
    a temporarily full sink cannot stop the single source read from serving the other.
    """

    def __init__(
        self,
        name: str,
        stream: BinaryIO,
        cancellation: CombinedCancellation,
        on_failure: Callable[[BaseException], None],
        *,
        byte_limit: int = FEEDER_BYTE_LIMIT,
        no_progress_seconds: float = NO_PROGRESS_TIMEOUT_SECONDS,
    ) -> None:
        self.name = name
        self._stream = stream
        self._cancellation = cancellation
        self._on_failure = on_failure
        self._byte_limit = byte_limit
        self._no_progress_seconds = no_progress_seconds
        self._queue: queue.Queue[bytes | None] = queue.Queue()
        self._condition = threading.Condition()
        self._queued_bytes = 0
        self.error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, name=f"live-hls-{name}-feeder", daemon=True)
        self._thread.start()

    def put(self, data: bytes) -> None:
        deadline = time.monotonic() + self._no_progress_seconds
        with self._condition:
            while self._queued_bytes > 0 and self._queued_bytes + len(data) > self._byte_limit:
                if self.error is not None:
                    raise SourceServiceError("live_hls_pipeline_failed", f"The {self.name} input stopped.")
                if self._cancellation.is_set():
                    raise WorkerCancelled("The live HLS job was cancelled.")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SourceServiceError(
                        "live_hls_pipeline_stalled",
                        f"The {self.name} decoder stopped accepting media.",
                    )
                self._condition.wait(min(remaining, 0.1))
            if self.error is not None:
                raise SourceServiceError("live_hls_pipeline_failed", f"The {self.name} input stopped.")
            self._queued_bytes += len(data)
        self._queue.put(data)

    def close(self) -> None:
        self._queue.put(None)

    def join(self, timeout: float) -> bool:
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def abandon(self) -> None:
        # Abandoning a feeder only needs the descriptor gone; a close error changes nothing.
        with suppress(OSError):
            self._stream.close()

    def _run(self) -> None:
        try:
            while True:
                data = self._queue.get()
                if data is None:
                    break
                self._stream.write(data)
                with self._condition:
                    self._queued_bytes -= len(data)
                    self._condition.notify_all()
            self._stream.close()
        except (Exception, KeyboardInterrupt, SystemExit) as error:
            with self._condition:
                self.error = error
                self._condition.notify_all()
            self.abandon()
            if not self._cancellation.is_set():
                self._on_failure(error)


@dataclass
class ChildRun:
    spec: ProcessSpec
    error_code: str
    started: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    result: ProcessResult | None = None
    error: BaseException | None = None
    thread: threading.Thread | None = None


class SSIFLiveHLSService(SSIFSourceHelper):
    def __init__(
        self,
        job: JobSpec,
        owner: WorkerProcessOwner,
        activity: WorkerActivityReporter,
        *,
        probe_path: Path | None = None,
        edge264_path: Path | None = None,
        ffmpeg_path: Path | None = None,
        encoder_path: Path | None = None,
        clock: Callable[[], float] = time.time,
        held_source: HeldSource | None = None,
    ) -> None:
        if job.live_hls is None or job.destination is None:
            raise SourceServiceError("invalid_live_hls", "The live HLS job is incomplete.")
        super().__init__(
            job,
            job.live_hls,
            job.destination.path,
            owner,
            activity,
            probe_path=probe_path,
            held_source=held_source,
        )
        self.options: LiveHLSOptions = job.live_hls
        self.no_progress_seconds: float = NO_PROGRESS_TIMEOUT_SECONDS
        self.edge264_path = edge264_path or resolve_tool_path("edge264_test")
        self.ffmpeg_path = ffmpeg_path or resolve_tool_path("ffmpeg")
        self.encoder_path = encoder_path or resolve_tool_path("mv-hevc-encoder")
        self.internal_cancellation = threading.Event()
        self.failure_event = threading.Event()
        self.cancellation = CombinedCancellation(
            self.internal_cancellation, self.failure_event, self.owner.cancellation_event
        )
        self.children: list[ChildRun] = []
        self.feeders: list[PipeFeeder] = []
        self.feeder_failure: BaseException | None = None
        self.publisher: HLSPublisher | None = None
        self.publisher_error: BaseException | None = None
        self.last_publication = time.monotonic()
        self.ready_sent = False
        self.clock = clock
        # Deliberate holds: while any returns True the worker stops reading the source
        # and the no-progress deadlines treat the wait as progress. Retired-segment
        # grace is built in; viewer-position pacing can append its own condition.
        self.hold_conditions: list[Callable[[], bool]] = [self._held_for_grace]
        self.viewer_lead_microseconds: int | None = None
        run_context = activity.run_context
        self.viewer_position: Callable[[], int | None] | None = (
            run_context.viewer_position if run_context is not None else None
        )
        if self.options.viewer_lead_seconds is not None:
            self.viewer_lead_microseconds = self.options.viewer_lead_seconds * 1_000_000
            self.hold_conditions.append(self._held_for_viewer)
        self.held_seconds = 0.0
        self.viewer_held_seconds = 0.0
        self.production_started: float | None = None
        self.forwarded_microseconds = 0.0
        self.frame_microseconds = 0.0

    def run(self) -> dict[str, object]:
        for path, code, message in (
            (self.edge264_path, "edge264_missing", "The MVC decoder could not be found."),
            (self.ffmpeg_path, "ffmpeg_missing", "FFmpeg could not be found."),
            (self.encoder_path, "mv_hevc_encoder_missing", "The MV-HEVC encoder could not be found."),
        ):
            if not path.is_file():
                raise SourceServiceError(code, message)
        self._validate_tool()
        self.activity.set_stage_plan(("inspect_live_source", "produce_live_hls"))
        self.activity.stage_started("inspect_live_source", "Inspecting the live source")
        self._verify_source_identity()
        inspection = self._inspect()
        video_stream, audio_stream = self._stream_descriptors(inspection)
        eye_width, eye_height, frame_rate = live_video_shape(cast(Mapping[str, object], inspection["title"]))
        audio_format = live_audio_input_format(audio_stream)
        if self.destination.exists() and any(self.destination.iterdir()):
            raise SourceServiceError("destination_not_empty", "The live HLS workspace must be empty.")
        self.destination.mkdir(parents=True, exist_ok=True)
        self.activity.stage_started("produce_live_hls", "Producing live MV-HEVC HLS with selected audio")
        streams = {
            "video_stream": {
                "id": "mv-hevc",
                "codec": "mv-hevc",
                "eye_width": eye_width,
                "eye_height": eye_height,
                "frame_rate": frame_rate,
                "bitrate_mbps": VIDEO_BITRATE_MBPS,
                "source_base_pid": video_stream["base_pid"],
                "source_dependent_pid": video_stream["dependent_pid"],
            },
            "audio_stream": {
                "id": f"aac:{self.options.audio_pid}",
                "codec": "aac",
                "channels": AUDIO_CHANNELS,
                "sample_rate": AUDIO_SAMPLE_RATE,
                "source_pid": self.options.audio_pid,
                "source_coding_type": audio_stream["coding_type"],
                "source_language": audio_stream["language"],
            },
        }
        try:
            return self._produce(streams, eye_width, eye_height, frame_rate, audio_format)
        except (Exception, KeyboardInterrupt, SystemExit):
            self._remove_workspace()
            raise

    def producer_stats(self) -> dict[str, object]:
        """Producer capacity separate from pacing: unheld throughput is
        ``produced_microseconds / ((wall_seconds - held_seconds) * 1e6)``."""
        assert self.publisher is not None
        started = self.production_started
        return {
            "produced_microseconds": self.publisher.next_start_microseconds,
            "wall_seconds": round(0.0 if started is None else time.monotonic() - started, 3),
            "held_seconds": round(self.held_seconds, 3),
            "viewer_held_seconds": round(self.viewer_held_seconds, 3),
        }

    def _remove_workspace(self) -> None:
        try:
            shutil.rmtree(self.destination)
        except FileNotFoundError:
            return
        except OSError as error:
            self.activity.warning(
                "The partial live HLS workspace could not be removed.",
                code="live_hls_cleanup_failed",
                details=str(error),
            )

    # Production -----------------------------------------------------------------

    def _produce(
        self,
        streams: Mapping[str, object],
        eye_width: int,
        eye_height: int,
        frame_rate: str,
        audio_format: str,
    ) -> dict[str, object]:
        pipeline = self.destination / PIPELINE_DIRECTORY
        pipeline.mkdir()
        staging = pipeline / "encoder"
        audio_fifo = pipeline / "audio.pcm"
        os.mkfifo(audio_fifo, 0o600)
        self.publisher = HLSPublisher(
            self.destination,
            staging,
            retention_seconds=self.options.retention_seconds,
            retention_max_bytes=self.options.retention_max_bytes,
            streams=streams,
            clock=self.clock,
            producer_stats=self.producer_stats,
        )
        self.production_started = time.monotonic()
        numerator, denominator = (int(value) for value in frame_rate.split("/"))
        self.frame_microseconds = 1_000_000 * denominator / numerator
        source_read, source_write = self._pipe()
        source = self._child(
            self._spec(
                tuple(self._source_command()),
                "ssif_probe",
                "Direct SSIF live source",
                stdout=source_write,
                pass_fds=self._source_pass_fds(),
            ),
            "source_stream_failed",
        )
        primary_error: BaseException | None = None
        publisher_thread: threading.Thread | None = None
        try:
            self._verify_source_identity()
            self._start(source, close_after_start=(source_write,))
            reader = ServiceFrameReader(
                source_read,
                cancellation_event=self.cancellation,
                read_timeout_seconds=SOURCE_FRAME_TIMEOUT_SECONDS,
            )
            startup, audio_start_ticks = self._read_startup(reader)
            self._start_media_pipeline(
                staging, audio_fifo, eye_width, eye_height, frame_rate, audio_format, audio_start_ticks
            )
            publisher_thread = threading.Thread(target=self._publish_loop, name="live-hls-publisher", daemon=True)
            publisher_thread.start()
            self._forward(reader, startup)
            self._await_media_pipeline(publisher_thread)
        except (Exception, KeyboardInterrupt, SystemExit) as error:
            primary_error = error
            self.internal_cancellation.set()
        finally:
            source_read.close()
            for feeder in self.feeders:
                feeder.close()
        cleanup_error = self._join_children()
        for feeder in self.feeders:
            feeder.join(timeout=5)
        if publisher_thread is not None:
            publisher_thread.join(timeout=5)
        self._raise_outcome(primary_error, cleanup_error)
        assert self.publisher is not None
        self.publisher.finish()
        shutil.rmtree(pipeline)
        if not self.ready_sent:
            self.activity.live_hls_ready(self.publisher.artifact())
        result = self.publisher.result()
        result["held_seconds"] = round(self.held_seconds, 3)
        result["viewer_held_seconds"] = round(self.viewer_held_seconds, 3)
        encoder_run = self.children[-1] if self.children else None
        if encoder_run is not None and encoder_run.result is not None:
            # The summary is diagnostic only; an unreadable one leaves the result without it.
            with suppress(UnicodeDecodeError, json.JSONDecodeError):
                result["encoder_summary"] = json.loads(encoder_run.result.stdout.capture)
        return result

    def _source_command(self) -> list[str]:
        command = [
            str(self.probe_path),
            "stream-service",
            self._source_argument(),
            str(self.options.playlist),
            str(self.options.audio_pid),
        ]
        if self.options.maximum_pairs is not None:
            command.append(str(self.options.maximum_pairs))
        return command

    def _read_startup(self, reader: ServiceFrameReader) -> tuple[list[ServiceRecord], int]:
        """Buffers records until the first keyframe and first audio fix the shared timeline."""
        records: list[ServiceRecord] = []
        buffered_bytes = 0
        first_video: ServiceRecord | None = None
        first_audio: ServiceRecord | None = None
        while first_video is None or first_audio is None:
            record = reader.read()
            if record is None or record.kind is ServiceRecordKind.COMPLETE:
                raise SourceServiceError(
                    "selected_audio_unavailable" if first_video is not None else "empty_source_stream",
                    "The live source ended before both video and selected audio started.",
                )
            if record.kind is ServiceRecordKind.VIDEO and first_video is None:
                if not record.random_access:
                    raise SourceServiceError(
                        "missing_initial_keyframe",
                        "The live source did not begin at a decodable keyframe.",
                    )
                first_video = record
            if record.kind is ServiceRecordKind.AUDIO and first_audio is None:
                first_audio = record
            records.append(record)
            buffered_bytes += len(record.primary) + len(record.secondary)
            if len(records) > STARTUP_RECORD_LIMIT or buffered_bytes > STARTUP_BYTE_LIMIT:
                raise SourceServiceError(
                    "selected_audio_unavailable",
                    "The selected audio did not start near the first video keyframe.",
                )
        audio_start_ticks = self._timestamp_delta(first_audio.pts, first_video.pts)
        return records, audio_start_ticks

    @staticmethod
    def _timestamp_delta(timestamp: int, origin: int) -> int:
        delta = (timestamp - origin) % TIMESTAMP_MODULUS
        if delta >= TIMESTAMP_HALF_RANGE:
            delta -= TIMESTAMP_MODULUS
        return delta

    def _forward(self, reader: ServiceFrameReader, startup: list[ServiceRecord]) -> None:
        """Forwards whole GOPs to the decoder and audio immediately.

        A bounded run stops at an arbitrary access unit; its trailing partial GOP is
        dropped so the decoder never emits a display-order gap at the end.
        """
        video_feeder, audio_feeder = self.feeders
        gop: list[bytes] = []
        video_records = 0

        def handle(item: ServiceRecord) -> None:
            nonlocal gop, video_records
            if item.kind is ServiceRecordKind.AUDIO:
                audio_feeder.put(item.primary)
                return
            video_records += 1
            if item.random_access and gop:
                self._forward_gop(video_feeder, gop)
                gop = []
            gop.append(item.primary + item.secondary)

        for startup_record in startup:
            handle(startup_record)
        while True:
            self.owner.check_cancelled()
            record = reader.read()
            if record is None:
                raise SourceServiceError(
                    "truncated_source_stream", "The source helper ended without completing the stream."
                )
            if record.kind is ServiceRecordKind.COMPLETE:
                if reader.read() is not None:
                    raise SourceServiceError(
                        "invalid_source_stream",
                        "The source helper returned media after its completion record.",
                    )
                break
            handle(record)
        bounded_cut = self.options.maximum_pairs is not None and video_records >= self.options.maximum_pairs
        if gop and not (bounded_cut and video_records > len(gop)):
            self._forward_gop(video_feeder, gop)
        video_feeder.close()
        audio_feeder.close()

    def _forward_gop(self, feeder: PipeFeeder, gop: list[bytes]) -> None:
        self._wait_for_room()
        feeder.put(b"".join(gop))
        self.forwarded_microseconds += len(gop) * self.frame_microseconds

    def _held_for_grace(self) -> bool:
        return self.publisher is not None and self.publisher.held_for_grace

    def _held_for_viewer(self) -> bool:
        """Holds while the published edge is at least the configured lead past the viewer.

        Before the first report the viewer counts as being at the start, so production
        runs one lead ahead and then waits. Every value is read fresh, so a viewer that
        advances releases the hold on the next poll.
        """
        if self.publisher is None or self.viewer_lead_microseconds is None:
            return False
        position = self.viewer_position() if self.viewer_position is not None else None
        return self.publisher.next_start_microseconds - (position or 0) >= self.viewer_lead_microseconds

    def _deliberately_held(self) -> bool:
        return any(condition() for condition in self.hold_conditions)

    def _wait_for_room(self) -> None:
        """Blocks source reading while publication is held or the encoder is far enough ahead.

        Not reading leaves the helper blocked on its stdout pipe, so the whole pipeline
        drains to at most ``STAGING_SEGMENT_CAP`` completed segments in staging. A
        deliberate hold counts as progress for the encoder-output deadline (the
        publisher thread refreshes it); waiting for an encoder that has stopped
        publishing does not, so a real stall still fails.
        """
        assert self.publisher is not None
        while True:
            segment_microseconds = self.publisher.max_segment_microseconds or SEGMENT_DURATION_SECONDS * 1_000_000
            budget = STAGING_SEGMENT_CAP * segment_microseconds
            held = self._deliberately_held()
            viewer_held = held and self._held_for_viewer()
            ahead = self.forwarded_microseconds - self.publisher.next_start_microseconds
            if not held and ahead < budget and self.publisher.staged_backlog < STAGING_SEGMENT_CAP:
                return
            if self.owner.cancellation_event.is_set():
                raise WorkerCancelled("The live HLS job was cancelled.")
            if self.failure_event.is_set():
                raise SourceServiceError("live_hls_pipeline_failed", "A live HLS pipeline process failed.")
            started = time.monotonic()
            time.sleep(HOLD_POLL_SECONDS)
            if held:
                waited = time.monotonic() - started
                self.held_seconds += waited
                if viewer_held:
                    self.viewer_held_seconds += waited

    def _start_media_pipeline(
        self,
        staging: Path,
        audio_fifo: Path,
        eye_width: int,
        eye_height: int,
        frame_rate: str,
        audio_format: str,
        audio_start_ticks: int,
    ) -> None:
        video_read, video_write = self._pipe()
        decoded_read, decoded_write = self._pipe()
        normalized_read, normalized_write = self._pipe()
        audio_read, audio_write = self._pipe()
        filter_graph = (
            "[0:v]split=2[left_source][right_source];"
            f"[left_source]crop={eye_width}:{eye_height}:0:0[left];"
            f"[right_source]crop={eye_width}:{eye_height}:{eye_width}:0[right];"
            "[left][right]hstack=inputs=2,format=yuv420p[stereo]"
        )
        encoder = self._child(
            self._spec(
                (
                    str(self.encoder_path),
                    "--hls-directory",
                    str(staging),
                    "--segment-duration",
                    str(SEGMENT_DURATION_SECONDS),
                    "--bitrate-mbps",
                    str(VIDEO_BITRATE_MBPS),
                    "--audio-pcm",
                    str(audio_fifo),
                    "--audio-start-ticks",
                    str(audio_start_ticks),
                ),
                "mv-hevc-encoder",
                "Live MV-HEVC HLS encoder",
                stdin=normalized_read,
            ),
            "live_video_encode_failed",
        )
        normalizer = self._child(
            self._spec(
                (
                    str(self.ffmpeg_path),
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "yuv4mpegpipe",
                    "-r",
                    frame_rate,
                    "-i",
                    "pipe:0",
                    "-filter_complex",
                    filter_graph,
                    "-map",
                    "[stereo]",
                    "-f",
                    "yuv4mpegpipe",
                    "pipe:1",
                ),
                "ffmpeg",
                "Live stereo frame normalizer",
                stdin=decoded_read,
                stdout=normalized_write,
            ),
            "live_video_normalize_failed",
        )
        decoder = self._child(
            self._spec(
                (str(self.edge264_path), "-", "-Osk"),
                "edge264",
                "Live MVC decoder",
                stdin=video_read,
                stdout=decoded_write,
            ),
            "live_video_decode_failed",
        )
        audio = self._child(
            self._spec(
                (
                    str(self.ffmpeg_path),
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    audio_format,
                    "-i",
                    "pipe:0",
                    "-ac",
                    str(AUDIO_CHANNELS),
                    "-ar",
                    str(AUDIO_SAMPLE_RATE),
                    "-f",
                    "s16le",
                    "-y",
                    f"file:{audio_fifo}",
                ),
                "ffmpeg",
                "Live selected-audio decoder",
                stdin=audio_read,
            ),
            "live_audio_decode_failed",
        )
        # Keep the encoder last so its captured summary is easy to find.
        self.children.remove(encoder)
        self.children.append(encoder)
        self._start(encoder, close_after_start=(normalized_read,))
        self.last_publication = time.monotonic()
        self._start(normalizer, close_after_start=(decoded_read, normalized_write))
        self._start(decoder, close_after_start=(video_read, decoded_write))
        self._start(audio, close_after_start=(audio_read,))
        self.feeders = [
            PipeFeeder(
                "video",
                video_write,
                self.cancellation,
                self._feeder_failed,
                no_progress_seconds=self.no_progress_seconds,
            ),
            PipeFeeder(
                "audio",
                audio_write,
                self.cancellation,
                self._feeder_failed,
                no_progress_seconds=self.no_progress_seconds,
            ),
        ]

    def _await_media_pipeline(self, publisher_thread: threading.Thread) -> None:
        """Waits for every child to exit and for the last staged segment to publish."""
        while not all(child.done.is_set() for child in self.children) or publisher_thread.is_alive():
            if self.owner.cancellation_event.is_set():
                raise WorkerCancelled("The live HLS job was cancelled.")
            if self.failure_event.is_set():
                raise SourceServiceError("live_hls_pipeline_failed", "A live HLS pipeline process failed.")
            self._check_publication_progress()
            time.sleep(PUBLISH_POLL_SECONDS)
        for feeder in self.feeders:
            feeder.join(timeout=5)

    def _check_publication_progress(self) -> None:
        if time.monotonic() - self.last_publication > self.no_progress_seconds:
            raise SourceServiceError("live_hls_pipeline_stalled", "The live HLS encoder stopped producing media.")

    def _publish_loop(self) -> None:
        """Publishes segments while the source is still being read and enforces the
        encoder-output deadline from the last real publication, not from source EOF."""
        assert self.publisher is not None
        try:
            while not self.internal_cancellation.is_set() and not self.failure_event.is_set():
                encoder_done = self.children[-1].done.is_set()
                if self.publisher.poll():
                    self.last_publication = time.monotonic()
                    if not self.ready_sent:
                        self.activity.live_hls_ready(self.publisher.artifact())
                        self.ready_sent = True
                if self.publisher.complete:
                    return
                if encoder_done and not self.publisher.held_for_grace and self.publisher.staged_backlog == 0:
                    return
                if self._deliberately_held():
                    self.last_publication = time.monotonic()
                self._check_publication_progress()
                time.sleep(PUBLISH_POLL_SECONDS)
        except (Exception, KeyboardInterrupt, SystemExit) as error:
            self.publisher_error = error
            self.failure_event.set()

    # Process ownership ----------------------------------------------------------

    @staticmethod
    def _pipe() -> tuple[BinaryIO, BinaryIO]:
        read_descriptor, write_descriptor = os.pipe()
        return (
            cast(BinaryIO, os.fdopen(read_descriptor, "rb", buffering=0)),
            cast(BinaryIO, os.fdopen(write_descriptor, "wb", buffering=0)),
        )

    @staticmethod
    def _spec(
        argv: tuple[str, ...],
        tool_id: str,
        display_name: str,
        *,
        stdin: BinaryIO | None = None,
        stdout: int | BinaryIO = subprocess.PIPE,
        pass_fds: tuple[int, ...] = (),
    ) -> ProcessSpec:
        return ProcessSpec(
            argv=argv,
            pass_fds=pass_fds,
            tool_id=tool_id,
            display_name=display_name,
            stdin=stdin,
            stdout=stdout,
            merge_stderr=False,
            capture_limit_bytes=MAXIMUM_DIAGNOSTIC_BYTES,
            capture_overflow=CaptureOverflowPolicy.TRUNCATE,
            timeout_seconds=None,
            termination_grace_seconds=2,
            kill_wait_seconds=2,
            pipe_drain_timeout_seconds=2,
        )

    def _child(self, spec: ProcessSpec, error_code: str) -> ChildRun:
        child = ChildRun(spec=spec, error_code=error_code)
        self.children.append(child)
        return child

    def _start(self, child: ChildRun, *, close_after_start: tuple[BinaryIO, ...]) -> None:
        child.thread = threading.Thread(
            target=self._run_child, args=(child,), name=f"live-hls-{child.spec.tool_id}", daemon=False
        )
        child.thread.start()
        try:
            if not child.started.wait(10):
                raise SourceServiceError("live_hls_start_timeout", f"{child.spec.display_name} did not start.")
        finally:
            for stream in close_after_start:
                stream.close()
        if child.error is not None:
            self._raise_child_error(child)

    def _run_child(self, child: ChildRun) -> None:
        try:
            child.result = self.process_runner.run(
                child.spec,
                run_context=self.activity.run_context,
                cancellation_event=self.cancellation,
                started_event=child.started,
            )
        except (Exception, KeyboardInterrupt, SystemExit) as error:
            child.error = error
            if not isinstance(error, ProcessCancelled):
                self.failure_event.set()
        finally:
            child.started.set()
            child.done.set()

    def _feeder_failed(self, error: BaseException) -> None:
        if self.feeder_failure is None:
            self.feeder_failure = error
        self.failure_event.set()

    def _join_children(self) -> SourceServiceError | None:
        for child in self.children:
            if child.thread is not None:
                child.thread.join(timeout=15)
        if any(child.thread is not None and child.thread.is_alive() for child in self.children):
            self.internal_cancellation.set()
            for child in self.children:
                if child.thread is not None:
                    child.thread.join(timeout=10)
        if any(child.thread is not None and child.thread.is_alive() for child in self.children):
            return SourceServiceError("live_hls_cleanup_timeout", "A live HLS process did not terminate.")
        return None

    def _raise_outcome(self, primary_error: BaseException | None, cleanup_error: SourceServiceError | None) -> None:
        """Raises the most specific failure: user cancellation, then a child's own error."""
        if self.owner.cancellation_event.is_set():
            raise WorkerCancelled("The live HLS job was cancelled.") from (primary_error or cleanup_error)
        failed_child = next(
            (
                child
                for child in self.children
                if child.error is not None and not isinstance(child.error, ProcessCancelled)
            ),
            None,
        )
        if failed_child is not None:
            self._raise_child_error(failed_child, cause=primary_error)
        if self.feeder_failure is not None and self.publisher_error is None:
            raise SourceServiceError(
                "live_hls_pipeline_failed", "A live HLS decoder stopped accepting media.", str(self.feeder_failure)
            ) from self.feeder_failure
        if isinstance(self.publisher_error, SourceServiceError):
            raise self.publisher_error
        if self.publisher_error is not None:
            raise SourceServiceError(
                "live_hls_publish_failed", "The live HLS workspace could not be updated.", str(self.publisher_error)
            ) from self.publisher_error
        if primary_error is not None:
            if cleanup_error is not None:
                raise primary_error from cleanup_error
            raise primary_error
        if cleanup_error is not None:
            raise cleanup_error
        unfinished = next((child for child in self.children if child.result is None), None)
        if unfinished is not None:
            raise SourceServiceError(unfinished.error_code, f"{unfinished.spec.display_name} returned no result.")

    def _raise_child_error(self, child: ChildRun, *, cause: BaseException | None = None) -> None:
        error = child.error
        assert error is not None
        if child.spec.tool_id == "ssif_probe" and isinstance(error, ProcessExecutionError):
            self._raise_helper_error(error.stderr_snapshot.capture, child.error_code)
        details: str | None
        if isinstance(error, ProcessExecutionError):
            details = error.stderr_snapshot.tail_text().strip() or None
        elif isinstance(error, ProcessRunnerError):
            details = self._process_error_details(error)
        else:
            details = str(error) or None
        raise SourceServiceError(child.error_code, f"{child.spec.display_name} failed.", details) from (cause or error)
