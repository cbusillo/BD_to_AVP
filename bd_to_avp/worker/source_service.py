from __future__ import annotations

import json
import os
import select
import shutil
import struct
import subprocess
import threading
import time

from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import BinaryIO, Mapping, cast

from bd_to_avp.modules.config import resolve_tool_path
from bd_to_avp.process_runner import (
    CaptureOverflowPolicy,
    ChildProcessRunner,
    ProcessCancelled,
    ProcessExecutionError,
    ProcessResult,
    ProcessRunnerError,
    ProcessSpec,
    ProcessTimeoutError,
)
from bd_to_avp.worker.ownership import WorkerCancelled, WorkerProcessOwner
from bd_to_avp.worker.protocol import JobSpec, LiveSourceOptions, WorkerActivityReporter


SERVICE_FRAME_MAGIC = b"SSFS"
SERVICE_FRAME_VERSION = 1
SERVICE_FRAME_HEADER = struct.Struct(">4sBBHQQQII")
SERVICE_FLAG_RANDOM_ACCESS = 1
MAXIMUM_RECORD_BYTES = 64 * 1_024 * 1_024
MAXIMUM_DIAGNOSTIC_BYTES = 1 * 1_024 * 1_024
MAXIMUM_PENDING_AUDIO_BYTES = 64 * 1_024 * 1_024
SOURCE_FRAME_TIMEOUT_SECONDS = 120
SOURCE_READ_POLL_SECONDS = 0.1
TIMESTAMP_MODULUS = 1 << 33
TIMESTAMP_HALF_RANGE = TIMESTAMP_MODULUS // 2


class SourceServiceError(Exception):
    def __init__(self, code: str, message: str, details: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


class ServiceRecordKind(IntEnum):
    VIDEO = 1
    AUDIO = 2
    COMPLETE = 3


@dataclass(frozen=True)
class ServiceRecord:
    kind: ServiceRecordKind
    sequence: int
    pts: int
    dts: int
    primary: bytes
    secondary: bytes
    random_access: bool


@dataclass(frozen=True)
class ReplayEntry:
    sequence: int
    start_ticks: int
    end_ticks: int
    video_samples: int
    audio_samples: int
    size_bytes: int
    video_path: str
    audio_path: str
    index_path: str


class CombinedCancellation:
    def __init__(self, *events: threading.Event) -> None:
        self._events = events

    def is_set(self) -> bool:
        return any(event.is_set() for event in self._events)


@dataclass
class ProcessRunState:
    result: ProcessResult | None = None
    error: BaseException | None = None


class ServiceFrameReader:
    def __init__(
        self,
        stream: BinaryIO,
        *,
        cancellation_event: threading.Event | CombinedCancellation | None = None,
        read_timeout_seconds: float | None = None,
    ) -> None:
        self._stream = stream
        self._cancellation_event = cancellation_event
        self._read_timeout_seconds = read_timeout_seconds
        self._expected_sequence = 0

    def read(self) -> ServiceRecord | None:
        header = self._read_exact(SERVICE_FRAME_HEADER.size, allow_eof=True)
        if header is None:
            return None
        magic, version, raw_kind, flags, sequence, pts, dts, primary_length, secondary_length = (
            SERVICE_FRAME_HEADER.unpack(header)
        )
        if magic != SERVICE_FRAME_MAGIC or version != SERVICE_FRAME_VERSION:
            raise SourceServiceError("invalid_source_stream", "The source helper returned an incompatible frame.")
        try:
            kind = ServiceRecordKind(raw_kind)
        except ValueError as error:
            raise SourceServiceError(
                "invalid_source_stream", "The source helper returned an unknown record type."
            ) from error
        if sequence != self._expected_sequence:
            raise SourceServiceError(
                "invalid_source_stream",
                f"The source helper skipped record sequence {self._expected_sequence}.",
            )
        self._expected_sequence += 1
        payload_length = primary_length + secondary_length
        if payload_length > MAXIMUM_RECORD_BYTES:
            raise SourceServiceError("invalid_source_stream", "The source helper returned an oversized record.")
        if flags & ~SERVICE_FLAG_RANDOM_ACCESS:
            raise SourceServiceError("invalid_source_stream", "The source helper returned unsupported frame flags.")
        if kind is ServiceRecordKind.VIDEO and (primary_length == 0 or secondary_length == 0):
            raise SourceServiceError("invalid_source_stream", "An MVC record must contain both stereo views.")
        if kind is ServiceRecordKind.AUDIO and (primary_length == 0 or secondary_length != 0 or flags != 0):
            raise SourceServiceError("invalid_source_stream", "An audio record has an invalid payload shape.")
        if kind is ServiceRecordKind.COMPLETE and (payload_length != 0 or flags != 0 or pts != 0 or dts != 0):
            raise SourceServiceError("invalid_source_stream", "The source helper completion record is malformed.")
        payload = self._read_exact(payload_length, allow_eof=False) or b""
        return ServiceRecord(
            kind=kind,
            sequence=sequence,
            pts=pts,
            dts=dts,
            primary=payload[:primary_length],
            secondary=payload[primary_length:],
            random_access=bool(flags & SERVICE_FLAG_RANDOM_ACCESS),
        )

    def _read_exact(self, size: int, *, allow_eof: bool) -> bytes | None:
        data = bytearray()
        while len(data) < size:
            chunk = self._read_chunk(size - len(data))
            if not chunk:
                if allow_eof and not data:
                    return None
                raise SourceServiceError("truncated_source_stream", "The source helper ended inside a media record.")
            data.extend(chunk)
        return bytes(data)

    def _read_chunk(self, size: int) -> bytes:
        if self._cancellation_event is None and self._read_timeout_seconds is None:
            return self._stream.read(size)
        deadline = None
        if self._read_timeout_seconds is not None:
            deadline = time.monotonic() + self._read_timeout_seconds
        while True:
            if self._cancellation_event is not None and self._cancellation_event.is_set():
                raise WorkerCancelled("The live source job was cancelled.")
            wait_seconds = SOURCE_READ_POLL_SECONDS
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SourceServiceError(
                        "source_stream_timeout",
                        "The live source helper stopped producing media records.",
                    )
                wait_seconds = min(wait_seconds, remaining)
            readable, _, _ = select.select((self._stream,), (), (), wait_seconds)
            if readable:
                return self._stream.read(size)


class ReplayStore:
    def __init__(
        self,
        root: Path,
        options: LiveSourceOptions,
        video_stream: Mapping[str, object],
        audio_stream: Mapping[str, object],
    ) -> None:
        self.root = root
        self.options = options
        self.video_stream = dict(video_stream)
        self.audio_stream = dict(audio_stream)
        self.entries: list[ReplayEntry] = []
        self.retired_entries: list[ReplayEntry] = []
        self.origin_dts: int | None = None
        self.pending_audio: list[ServiceRecord] = []
        self.pending_audio_bytes = 0
        self.current_sequence: int | None = None
        self.current_start_ticks = 0
        self.current_end_ticks = 0
        self.current_video_samples = 0
        self.current_audio_samples = 0
        self.current_video_bytes = 0
        self.current_audio_bytes = 0
        self.current_index_bytes = 0
        self.current_video: BinaryIO | None = None
        self.current_audio: BinaryIO | None = None
        self.current_index: BinaryIO | None = None
        self.ready_emitted = False
        if root.exists() and any(root.iterdir()):
            raise SourceServiceError("destination_not_empty", "The live source workspace must be empty.")
        root.mkdir(parents=True, exist_ok=True)

    @property
    def manifest_path(self) -> Path:
        return self.root / "live-source.json"

    def consume(self, record: ServiceRecord) -> bool:
        if record.kind is ServiceRecordKind.AUDIO:
            self._queue_audio(record)
            return False
        if record.kind is not ServiceRecordKind.VIDEO:
            return False
        if self.origin_dts is None:
            if not record.random_access:
                raise SourceServiceError(
                    "missing_initial_keyframe",
                    "The live source did not begin at a replayable keyframe.",
                )
            self.origin_dts = record.dts
        record_ticks = self._normalize(record.dts)
        if record.random_access and self.current_video_samples > 0:
            self._ensure_current_capacity(0, record_ticks)
            self._flush_audio(before_ticks=record_ticks)
            self._finalize_current()
        if self.current_sequence is None:
            if not record.random_access:
                raise SourceServiceError(
                    "missing_replay_boundary",
                    "The live source produced video before a replay boundary.",
                )
            self._start_current(record.sequence, record_ticks)
            self._flush_audio(before_ticks=record_ticks, include_earlier=True)
        self._write_video(record)
        return self.ready_emitted

    def finish(self) -> None:
        if self.current_sequence is None:
            raise SourceServiceError("empty_source_stream", "The live source produced no replayable video.")
        self._flush_audio(before_ticks=None)
        self._finalize_current()
        self._write_manifest(complete=True)
        self._retire([])

    def cleanup(self) -> None:
        self._close_current()
        try:
            shutil.rmtree(self.root)
        except FileNotFoundError:
            return
        except OSError as error:
            raise SourceServiceError(
                "source_cleanup_failed",
                "The partial live source workspace could not be removed.",
                str(error),
            ) from error

    def result(self) -> dict[str, object]:
        if not self.entries:
            raise SourceServiceError("empty_source_stream", "The live source produced no retained replay entries.")
        return {
            "manifest_path": str(self.manifest_path),
            "video_stream_id": self.video_stream["id"],
            "audio_stream_id": self.audio_stream["id"],
            "earliest_available_ticks": self.entries[0].start_ticks,
            "latest_available_ticks": self.entries[-1].end_ticks,
            "retained_gops": len(self.entries),
        }

    def artifact(self) -> dict[str, object]:
        return {
            "kind": "live_source_manifest",
            "manifest_path": str(self.manifest_path),
            "video_stream_id": self.video_stream["id"],
            "audio_stream_id": self.audio_stream["id"],
        }

    def _queue_audio(self, record: ServiceRecord) -> None:
        self.pending_audio.append(record)
        self.pending_audio_bytes += len(record.primary)
        if self.pending_audio_bytes > MAXIMUM_PENDING_AUDIO_BYTES:
            raise SourceServiceError("audio_backpressure", "Selected audio exceeded the bounded pending buffer.")

    def _flush_audio(self, before_ticks: int | None, *, include_earlier: bool = False) -> None:
        retained: list[ServiceRecord] = []
        retained_bytes = 0
        for record in self.pending_audio:
            normalized = self._normalize(record.dts)
            should_write = include_earlier or before_ticks is None or normalized < before_ticks
            if should_write:
                if self.current_audio is None:
                    raise SourceServiceError("invalid_replay_state", "Audio has no active replay boundary.")
                self._write_audio(record, normalized)
            else:
                retained.append(record)
                retained_bytes += len(record.primary)
        self.pending_audio = retained
        self.pending_audio_bytes = retained_bytes

    def _start_current(self, sequence: int, start_ticks: int) -> None:
        self.current_sequence = sequence
        self.current_start_ticks = start_ticks
        self.current_end_ticks = start_ticks
        self.current_video_samples = 0
        self.current_audio_samples = 0
        self.current_video_bytes = 0
        self.current_audio_bytes = 0
        self.current_index_bytes = 0
        prefix = self._prefix(sequence)
        self.current_video = (self.root / f"{prefix}.mvc.part").open("wb")
        self.current_audio = (self.root / f"{prefix}.audio.part").open("wb")
        self.current_index = (self.root / f"{prefix}.index.jsonl.part").open("wb")

    def _write_video(self, record: ServiceRecord) -> None:
        if self.current_video is None or self.current_index is None:
            raise SourceServiceError("invalid_replay_state", "Video has no active replay boundary.")
        offset = self.current_video.tell()
        normalized_pts = self._normalize(record.pts)
        normalized_dts = self._normalize(record.dts)
        index_line = self._index_line(
            "video",
            record.sequence,
            normalized_pts,
            normalized_dts,
            offset,
            len(record.primary),
            len(record.secondary),
            random_access=record.random_access,
        )
        self._ensure_current_capacity(len(record.primary) + len(record.secondary) + len(index_line), normalized_dts)
        self.current_video.write(record.primary)
        self.current_video.write(record.secondary)
        self.current_index.write(index_line)
        self.current_video_samples += 1
        self.current_video_bytes += len(record.primary) + len(record.secondary)
        self.current_index_bytes += len(index_line)
        self.current_end_ticks = max(self.current_end_ticks, normalized_dts)

    def _write_audio(self, record: ServiceRecord, normalized_dts: int) -> None:
        if self.current_audio is None or self.current_index is None:
            raise SourceServiceError("invalid_replay_state", "Audio has no active replay boundary.")
        normalized_dts = max(0, normalized_dts)
        offset = self.current_audio.tell()
        index_line = self._index_line(
            "audio",
            record.sequence,
            max(0, self._normalize(record.pts)),
            normalized_dts,
            offset,
            len(record.primary),
            0,
            random_access=False,
        )
        self._ensure_current_capacity(len(record.primary) + len(index_line), normalized_dts)
        self.current_audio.write(record.primary)
        self.current_index.write(index_line)
        self.current_audio_samples += 1
        self.current_audio_bytes += len(record.primary)
        self.current_index_bytes += len(index_line)
        self.current_end_ticks = max(self.current_end_ticks, normalized_dts)

    def _ensure_current_capacity(self, additional_bytes: int, timestamp_ticks: int) -> None:
        maximum_ticks = self.options.replay_window_seconds * 90_000
        if timestamp_ticks - self.current_start_ticks > maximum_ticks:
            raise SourceServiceError(
                "replay_keyframe_interval_too_long",
                "The source exceeded the replay window without another keyframe.",
            )
        current_bytes = self.current_video_bytes + self.current_audio_bytes + self.current_index_bytes
        if current_bytes + additional_bytes > self.options.replay_max_bytes:
            raise SourceServiceError(
                "replay_gop_too_large",
                "One replay unit exceeds the configured byte limit.",
            )

    def _finalize_current(self) -> None:
        if self.current_sequence is None:
            return
        self._close_current()
        prefix = self._prefix(self.current_sequence)
        video_path = f"{prefix}.mvc"
        audio_path = f"{prefix}.audio"
        index_path = f"{prefix}.index.jsonl"
        (self.root / f"{video_path}.part").replace(self.root / video_path)
        (self.root / f"{audio_path}.part").replace(self.root / audio_path)
        (self.root / f"{index_path}.part").replace(self.root / index_path)
        size_bytes = self.current_video_bytes + self.current_audio_bytes + self.current_index_bytes
        self.entries.append(
            ReplayEntry(
                sequence=self.current_sequence,
                start_ticks=self.current_start_ticks,
                end_ticks=self.current_end_ticks,
                video_samples=self.current_video_samples,
                audio_samples=self.current_audio_samples,
                size_bytes=size_bytes,
                video_path=video_path,
                audio_path=audio_path,
                index_path=index_path,
            )
        )
        self.current_sequence = None
        evicted = self._evict()
        self._write_manifest(complete=False)
        self._retire(evicted)
        self.ready_emitted = True

    def _close_current(self) -> None:
        for handle in (self.current_video, self.current_audio, self.current_index):
            if handle is not None:
                handle.close()
        self.current_video = None
        self.current_audio = None
        self.current_index = None

    def _evict(self) -> list[ReplayEntry]:
        maximum_ticks = self.options.replay_window_seconds * 90_000
        removed: list[ReplayEntry] = []
        while len(self.entries) > 1:
            total_bytes = sum(entry.size_bytes for entry in self.entries)
            duration_ticks = self.entries[-1].end_ticks - self.entries[0].start_ticks
            if total_bytes <= self.options.replay_max_bytes and duration_ticks <= maximum_ticks:
                break
            removed.append(self.entries.pop(0))
        return removed

    def _retire(self, entries: list[ReplayEntry]) -> None:
        for entry in self.retired_entries:
            for relative_path in (entry.video_path, entry.audio_path, entry.index_path):
                (self.root / relative_path).unlink(missing_ok=True)
        self.retired_entries = entries

    def _write_manifest(self, *, complete: bool) -> None:
        if not self.entries:
            return
        manifest = {
            "schema_version": 1,
            "type": "live_source",
            "complete": complete,
            "timebase": 90_000,
            "video_stream": self.video_stream,
            "audio_stream": self.audio_stream,
            "earliest_available_ticks": self.entries[0].start_ticks,
            "latest_available_ticks": self.entries[-1].end_ticks,
            "replay_window_seconds": self.options.replay_window_seconds,
            "replay_max_bytes": self.options.replay_max_bytes,
            "gops": [entry.__dict__ for entry in self.entries],
        }
        temporary_path = self.manifest_path.with_suffix(".json.tmp")
        temporary_path.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
        temporary_path.replace(self.manifest_path)

    def _normalize(self, timestamp: int) -> int:
        if self.origin_dts is None:
            return 0
        delta = (timestamp - self.origin_dts) % TIMESTAMP_MODULUS
        if delta >= TIMESTAMP_HALF_RANGE:
            delta -= TIMESTAMP_MODULUS
        return delta

    @staticmethod
    def _prefix(sequence: int) -> str:
        return f"gop-{sequence:020d}"

    @staticmethod
    def _index_line(
        kind: str,
        sequence: int,
        pts: int,
        dts: int,
        offset: int,
        primary_bytes: int,
        secondary_bytes: int,
        *,
        random_access: bool,
    ) -> bytes:
        record = {
            "kind": kind,
            "sequence": sequence,
            "pts_ticks": pts,
            "dts_ticks": dts,
            "offset": offset,
            "primary_bytes": primary_bytes,
            "secondary_bytes": secondary_bytes,
            "random_access": random_access,
        }
        return (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()


class SSIFLiveSourceService:
    def __init__(
        self,
        job: JobSpec,
        owner: WorkerProcessOwner,
        activity: WorkerActivityReporter,
        *,
        probe_path: Path | None = None,
    ) -> None:
        if job.live_source is None or job.destination is None:
            raise SourceServiceError("invalid_live_source", "The live source job is incomplete.")
        self.job = job
        self.options = job.live_source
        self.destination = job.destination.path
        self.owner = owner
        self.activity = activity
        self.process_runner = ChildProcessRunner()
        repository_probe = Path(__file__).resolve().parents[1] / "bin/ssif_probe"
        self.probe_path = probe_path or resolve_tool_path("ssif_probe", extra_paths=(repository_probe,))

    def run(self) -> dict[str, object]:
        self._validate_tool()
        self.activity.set_stage_plan(("inspect_live_source", "stream_live_source"))
        self.activity.stage_started("inspect_live_source", "Inspecting the live source")
        inspection = self._inspect()
        video_stream, audio_stream = self._stream_descriptors(inspection)
        self.activity.stage_started("stream_live_source", "Producing replayable MVC and selected audio")
        store = ReplayStore(self.destination, self.options, video_stream, audio_stream)
        try:
            return self._stream(store)
        except (Exception, KeyboardInterrupt, SystemExit):
            try:
                store.cleanup()
            except SourceServiceError as cleanup_error:
                self.activity.warning(
                    cleanup_error.message,
                    code=cleanup_error.code,
                    details=cleanup_error.details,
                )
            raise

    def _validate_tool(self) -> None:
        if not self.probe_path.is_file():
            raise SourceServiceError("ssif_probe_missing", "The direct SSIF source helper could not be found.")
        result = self._communicate([str(self.probe_path), "--version"])
        if result.returncode != 0 or result.stdout != b"ssif_probe contract 2\n":
            raise SourceServiceError(
                "ssif_probe_incompatible",
                "The direct SSIF source helper has an incompatible contract.",
                result.stderr.decode("utf-8", errors="replace"),
            )

    def _inspect(self) -> Mapping[str, object]:
        result = self._communicate(
            [str(self.probe_path), "inspect", str(self.job.source.path), str(self.options.playlist)]
        )
        if result.returncode != 0:
            self._raise_helper_error(result.stderr, "source_inspection_failed")
        try:
            inspection = json.loads(result.stdout)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SourceServiceError(
                "invalid_source_inspection",
                "The source helper returned invalid inspection metadata.",
            ) from error
        if (
            not isinstance(inspection, Mapping)
            or inspection.get("schema_version") != 2
            or inspection.get("type") != "source.inspect"
        ):
            raise SourceServiceError("invalid_source_inspection", "The source helper returned unexpected metadata.")
        title = inspection.get("title")
        if not isinstance(title, Mapping) or title.get("eligible") is not True:
            reason = title.get("unsupported_reason") if isinstance(title, Mapping) else None
            raise SourceServiceError(
                str(reason or "unsupported_source"),
                "The selected source is outside the supported live-source boundary.",
            )
        return inspection

    def _stream_descriptors(
        self,
        inspection: Mapping[str, object],
    ) -> tuple[dict[str, object], dict[str, object]]:
        title = inspection.get("title")
        if not isinstance(title, Mapping):
            raise SourceServiceError("invalid_source_inspection", "The source returned no title metadata.")
        mvc_pids = title.get("mvc_pids")
        if not isinstance(mvc_pids, Mapping):
            raise SourceServiceError("invalid_source_inspection", "The source has no stable MVC stream identities.")
        base_pid = mvc_pids.get("base")
        dependent_pid = mvc_pids.get("dependent")
        if not isinstance(base_pid, int) or not isinstance(dependent_pid, int):
            raise SourceServiceError("invalid_source_inspection", "The source returned invalid MVC stream identities.")
        clips = title.get("clips")
        if not isinstance(clips, list) or len(clips) != 1 or not isinstance(clips[0], Mapping):
            raise SourceServiceError("unsupported_source", "The live source requires exactly one clip.")
        audio_streams = clips[0].get("audio_streams")
        if not isinstance(audio_streams, list):
            raise SourceServiceError("selected_audio_unavailable", "The source has no selectable audio streams.")
        selected_audio = next(
            (
                stream
                for stream in audio_streams
                if isinstance(stream, Mapping) and stream.get("pid") == self.options.audio_pid
            ),
            None,
        )
        if selected_audio is None:
            raise SourceServiceError(
                "selected_audio_unavailable",
                "The selected audio stream is not present in the live source.",
            )
        video_stream = {
            "id": "mvc",
            "format": "annex_b",
            "base_pid": base_pid,
            "dependent_pid": dependent_pid,
            "timebase": 90_000,
        }
        audio_stream = {
            "id": f"audio:{self.options.audio_pid}",
            "pid": self.options.audio_pid,
            "coding_type": selected_audio.get("coding_type"),
            "format": selected_audio.get("format"),
            "rate": selected_audio.get("rate"),
            "language": selected_audio.get("language"),
            "timebase": 90_000,
        }
        return video_stream, audio_stream

    def _stream(self, store: ReplayStore) -> dict[str, object]:
        command = [
            str(self.probe_path),
            "stream-service",
            str(self.job.source.path),
            str(self.options.playlist),
            str(self.options.audio_pid),
        ]
        if self.options.maximum_pairs is not None:
            command.append(str(self.options.maximum_pairs))
        read_descriptor, write_descriptor = os.pipe()
        read_stream = os.fdopen(read_descriptor, "rb", buffering=0)
        write_stream = os.fdopen(write_descriptor, "wb", buffering=0)
        internal_cancellation = threading.Event()
        combined_cancellation = CombinedCancellation(internal_cancellation, self.owner.cancellation_event)
        started = threading.Event()
        state = ProcessRunState()
        spec = ProcessSpec(
            argv=tuple(command),
            tool_id="ssif_probe",
            display_name="Direct SSIF live source",
            stdout=write_stream,
            merge_stderr=False,
            capture_limit_bytes=MAXIMUM_DIAGNOSTIC_BYTES,
            capture_overflow=CaptureOverflowPolicy.TRUNCATE,
            timeout_seconds=None,
            termination_grace_seconds=2,
            kill_wait_seconds=2,
            pipe_drain_timeout_seconds=2,
        )
        process_thread = threading.Thread(
            target=self._run_process,
            args=(spec, combined_cancellation, started, state),
            name="ssif-source-process",
            daemon=False,
        )
        process_thread.start()
        if not started.wait(10):
            start_error = SourceServiceError("source_start_timeout", "The live source helper did not start in time.")
            internal_cancellation.set()
            cleanup_error = self._wait_for_process_thread(process_thread, internal_cancellation, initial_timeout=10)
            read_stream.close()
            write_stream.close()
            self._raise_primary_or_cleanup(start_error, cleanup_error)
            raise start_error
        write_stream.close()
        completed = False
        ready_sent = False
        primary_error: BaseException | None = None
        try:
            reader = ServiceFrameReader(
                cast(BinaryIO, read_stream),
                cancellation_event=combined_cancellation,
                read_timeout_seconds=SOURCE_FRAME_TIMEOUT_SECONDS,
            )
            while True:
                self.owner.check_cancelled()
                record = reader.read()
                if record is None:
                    break
                if record.kind is ServiceRecordKind.COMPLETE:
                    completed = True
                    if reader.read() is not None:
                        raise SourceServiceError(
                            "invalid_source_stream",
                            "The source helper returned media after its completion record.",
                        )
                    break
                store.consume(record)
                if store.ready_emitted and not ready_sent:
                    self.activity.live_source_ready(store.artifact())
                    ready_sent = True
        except (Exception, KeyboardInterrupt, SystemExit) as error:
            primary_error = error
            internal_cancellation.set()
        finally:
            read_stream.close()
        cleanup_error = self._wait_for_process_thread(process_thread, internal_cancellation, initial_timeout=15)
        self._raise_primary_or_cleanup(primary_error, cleanup_error)
        if self.owner.cancellation_event.is_set():
            raise WorkerCancelled("The live source job was cancelled.")
        self._raise_process_error(state.error, "source_stream_failed")
        if state.result is None:
            raise SourceServiceError("source_stream_failed", "The live source helper returned no process result.")
        if not completed:
            raise SourceServiceError(
                "truncated_source_stream", "The source helper ended without completing the stream."
            )
        store.finish()
        if not ready_sent:
            self.activity.live_source_ready(store.artifact())
        return store.result()

    @staticmethod
    def _wait_for_process_thread(
        process_thread: threading.Thread,
        internal_cancellation: threading.Event,
        *,
        initial_timeout: float,
    ) -> SourceServiceError | None:
        process_thread.join(timeout=initial_timeout)
        if process_thread.is_alive():
            internal_cancellation.set()
            process_thread.join(timeout=10)
        if process_thread.is_alive():
            return SourceServiceError("source_cleanup_timeout", "The live source helper did not terminate.")
        return None

    @staticmethod
    def _raise_primary_or_cleanup(
        primary_error: BaseException | None,
        cleanup_error: SourceServiceError | None,
    ) -> None:
        if primary_error is not None:
            if cleanup_error is not None:
                raise primary_error from cleanup_error
            raise primary_error
        if cleanup_error is not None:
            raise cleanup_error

    def _communicate(self, command: list[str]) -> subprocess.CompletedProcess[bytes]:
        try:
            result = self.process_runner.run(
                ProcessSpec(
                    argv=tuple(command),
                    tool_id="ssif_probe",
                    display_name="Direct SSIF source helper",
                    merge_stderr=False,
                    capture_limit_bytes=MAXIMUM_DIAGNOSTIC_BYTES,
                    timeout_seconds=120,
                    termination_grace_seconds=2,
                    kill_wait_seconds=2,
                    pipe_drain_timeout_seconds=2,
                ),
                run_context=self.activity.run_context,
                cancellation_event=self.owner.cancellation_event,
            )
        except ProcessCancelled as error:
            raise WorkerCancelled("The live source job was cancelled.") from error
        except ProcessExecutionError as error:
            return subprocess.CompletedProcess(
                command,
                error.returncode,
                error.stdout_snapshot.capture,
                error.stderr_snapshot.capture,
            )
        except ProcessTimeoutError as error:
            raise SourceServiceError(
                "source_helper_timeout",
                "The direct SSIF source helper did not respond in time.",
                self._process_error_details(error),
            ) from error
        except ProcessRunnerError as error:
            raise SourceServiceError(
                "source_helper_failed",
                "The direct SSIF source helper could not complete.",
                self._process_error_details(error),
            ) from error
        return subprocess.CompletedProcess(command, result.returncode, result.stdout.capture, result.stderr.capture)

    def _run_process(
        self,
        spec: ProcessSpec,
        cancellation: CombinedCancellation,
        started: threading.Event,
        state: ProcessRunState,
    ) -> None:
        try:
            state.result = self.process_runner.run(
                spec,
                run_context=self.activity.run_context,
                cancellation_event=cancellation,
                started_event=started,
            )
        except (Exception, KeyboardInterrupt, SystemExit) as error:
            state.error = error

    def _raise_process_error(self, error: BaseException | None, fallback_code: str) -> None:
        if error is None:
            return
        if isinstance(error, ProcessCancelled):
            if self.owner.cancellation_event.is_set():
                raise WorkerCancelled("The live source job was cancelled.") from error
            raise SourceServiceError(fallback_code, "The live source helper was stopped.") from error
        if isinstance(error, ProcessExecutionError):
            self._raise_helper_error(error.stderr_snapshot.capture, fallback_code)
        if isinstance(error, ProcessRunnerError):
            raise SourceServiceError(
                fallback_code,
                "The live source helper could not complete.",
                self._process_error_details(error),
            ) from error
        raise SourceServiceError(fallback_code, "The live source helper failed.", str(error)) from error

    @staticmethod
    def _process_error_details(error: ProcessRunnerError) -> str | None:
        stderr = error.stderr_snapshot
        if stderr is not None and stderr.retained_bytes > 0:
            return stderr.tail_text()
        return str(error) or None

    @staticmethod
    def _raise_helper_error(stderr: bytes, fallback_code: str) -> None:
        details = stderr.decode("utf-8", errors="replace").strip()
        for line in reversed(details.splitlines()):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, Mapping) and payload.get("type") == "error":
                raise SourceServiceError(
                    str(payload.get("code") or fallback_code),
                    str(payload.get("message") or "The live source helper failed."),
                    details,
                )
        raise SourceServiceError(fallback_code, "The live source helper failed.", details or None)
