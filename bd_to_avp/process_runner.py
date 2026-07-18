from __future__ import annotations

import codecs
import os
import queue
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO, cast
from uuid import uuid4

from bd_to_avp.observability import (
    MAX_DETAIL_BYTES,
    ObservabilityActivity,
    ObservabilityArtifact,
    ObservabilityCancellation,
    ObservabilityContext,
    ObservabilityCounters,
    ObservabilityData,
    ObservabilityFailure,
    ObservabilityPrivacy,
    ObservabilityProcess,
    ObservabilityProgress,
    ObservabilitySeverity,
    ObservabilityText,
    ObservabilityTool,
)
from bd_to_avp.runtime import RunContext

DEFAULT_CAPTURE_LIMIT_BYTES = 16 * 1024 * 1024
DEFAULT_TAIL_LIMIT_BYTES = 64 * 1024
DEFAULT_LINE_LIMIT_BYTES = 16 * 1024
DEFAULT_QUEUE_CHUNKS = 128
DEFAULT_ACTIVITY_INTERVAL_SECONDS = 5.0
DEFAULT_ARTIFACT_INTERVAL_SECONDS = 2.0
DEFAULT_TERMINATION_GRACE_SECONDS = 5.0
DEFAULT_KILL_WAIT_SECONDS = 5.0
DEFAULT_PIPE_DRAIN_TIMEOUT_SECONDS = 5.0
READ_CHUNK_BYTES = 32 * 1024
ARTIFACT_SIZE_QUANTUM_BYTES = 64 * 1024
ARTIFACT_AGE_QUANTUM_SECONDS = 5
EVENT_QUEUE_LIMIT = 128
EVENT_FLUSH_TIMEOUT_SECONDS = 1.0


class ProcessStream(StrEnum):
    STDOUT = "stdout"
    STDERR = "stderr"


class CaptureOverflowPolicy(StrEnum):
    FAIL = "fail"
    TRUNCATE = "truncate"


class ProcessRunnerError(RuntimeError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.stdout_snapshot: ProcessOutputSnapshot | None = None
        self.stderr_snapshot: ProcessOutputSnapshot | None = None

    def attach_output(
        self,
        stdout_snapshot: ProcessOutputSnapshot,
        stderr_snapshot: ProcessOutputSnapshot,
    ) -> None:
        self.stdout_snapshot = stdout_snapshot
        self.stderr_snapshot = stderr_snapshot


class ProcessCancelled(ProcessRunnerError):
    pass


class ProcessOutputLimitError(ProcessRunnerError):
    pass


class ProcessOutputDispatchError(ProcessRunnerError):
    pass


class ProcessTimeoutError(ProcessRunnerError):
    pass


class ProcessPipeDrainError(ProcessRunnerError):
    pass


@dataclass(frozen=True)
class ProcessArtifactProbe:
    role: str
    path: Path | None = None
    resolver: Callable[[], Path | None] | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        if not self.role:
            raise ValueError("artifact role must not be empty")
        if (self.path is None) == (self.resolver is None):
            raise ValueError("artifact probe requires exactly one path source")
        ObservabilityArtifact(role=self.role, state="registered")

    def resolve_path(self) -> Path | None:
        if self.path is not None:
            return self.path
        assert self.resolver is not None
        return self.resolver()


@dataclass(frozen=True)
class ProcessSpec:
    argv: tuple[str | bytes | Path, ...]
    tool_id: str
    display_name: str
    env: Mapping[str, str] | None = None
    cwd: Path | None = None
    stdin: int | BinaryIO | None = None
    merge_stderr: bool = True
    event_context: ObservabilityContext = field(default_factory=ObservabilityContext)
    tool_version: str | None = None
    artifacts: tuple[ProcessArtifactProbe, ...] = ()
    capture_limit_bytes: int = DEFAULT_CAPTURE_LIMIT_BYTES
    tail_limit_bytes: int = DEFAULT_TAIL_LIMIT_BYTES
    line_limit_bytes: int = DEFAULT_LINE_LIMIT_BYTES
    queue_chunks: int = DEFAULT_QUEUE_CHUNKS
    capture_overflow: CaptureOverflowPolicy = CaptureOverflowPolicy.FAIL
    activity_interval_seconds: float = DEFAULT_ACTIVITY_INTERVAL_SECONDS
    artifact_interval_seconds: float = DEFAULT_ARTIFACT_INTERVAL_SECONDS
    timeout_seconds: float | None = None
    termination_grace_seconds: float = DEFAULT_TERMINATION_GRACE_SECONDS
    kill_wait_seconds: float = DEFAULT_KILL_WAIT_SECONDS
    pipe_drain_timeout_seconds: float = DEFAULT_PIPE_DRAIN_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if not self.argv:
            raise ValueError("process argv must not be empty")
        if not self.tool_id:
            raise ValueError("process tool_id must not be empty")
        if not self.display_name:
            raise ValueError("process display_name must not be empty")
        if not isinstance(self.event_context, ObservabilityContext):
            raise TypeError("event_context must be an ObservabilityContext")
        if not isinstance(self.capture_overflow, CaptureOverflowPolicy):
            raise TypeError("capture_overflow must be a CaptureOverflowPolicy")
        if any(not isinstance(probe, ProcessArtifactProbe) for probe in self.artifacts):
            raise TypeError("artifacts must contain ProcessArtifactProbe values")
        if isinstance(self.stdin, int) and self.stdin == subprocess.PIPE:
            raise ValueError("stdin=subprocess.PIPE requires an input writer and is not supported")
        for integer_name, integer_value in (
            ("capture_limit_bytes", self.capture_limit_bytes),
            ("tail_limit_bytes", self.tail_limit_bytes),
            ("line_limit_bytes", self.line_limit_bytes),
            ("queue_chunks", self.queue_chunks),
        ):
            if type(integer_value) is not int or integer_value <= 0:
                raise ValueError(f"{integer_name} must be a positive integer")
        for duration_name, duration_value in (
            ("activity_interval_seconds", self.activity_interval_seconds),
            ("artifact_interval_seconds", self.artifact_interval_seconds),
            ("termination_grace_seconds", self.termination_grace_seconds),
            ("kill_wait_seconds", self.kill_wait_seconds),
            ("pipe_drain_timeout_seconds", self.pipe_drain_timeout_seconds),
        ):
            if duration_value <= 0:
                raise ValueError(f"{duration_name} must be positive")
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")


@dataclass(frozen=True)
class ProcessOutputSnapshot:
    capture: bytes
    tail: bytes
    total_bytes: int
    retained_bytes: int
    dropped_bytes: int
    decode_replacements: int

    @property
    def truncated(self) -> bool:
        return self.dropped_bytes > 0

    def text(self) -> str:
        return self.capture.decode("utf-8", errors="replace")

    def tail_text(self) -> str:
        return self.tail.decode("utf-8", errors="replace")


class ProcessExecutionError(subprocess.CalledProcessError):
    def __init__(
        self,
        returncode: int,
        command: list[str | bytes],
        stdout_snapshot: ProcessOutputSnapshot,
        stderr_snapshot: ProcessOutputSnapshot,
        *,
        merge_stderr: bool = False,
    ) -> None:
        super().__init__(
            returncode,
            command,
            output=stdout_snapshot.text(),
            stderr=None if merge_stderr else stderr_snapshot.text(),
        )
        self.stdout_snapshot = stdout_snapshot
        self.stderr_snapshot = stderr_snapshot


@dataclass(frozen=True)
class ProcessResult:
    tool_run_id: str
    returncode: int
    elapsed_ms: int
    stdout: ProcessOutputSnapshot
    stderr: ProcessOutputSnapshot
    forced_termination: bool = False


LineHandler = Callable[[ProcessStream, str], object]
OutputObserver = Callable[[ProcessStream, bytes], object]
ProgressParser = Callable[[ProcessStream, str], ObservabilityProgress | None]


@dataclass(frozen=True)
class _OutputChunk:
    stream: ProcessStream
    payload: bytes


@dataclass(frozen=True)
class _StreamClosed:
    stream: ProcessStream


@dataclass(frozen=True)
class _PendingEvent:
    kind: str
    severity: ObservabilitySeverity
    context: ObservabilityContext
    data: ObservabilityData


class _AsyncRunEmitter:
    def __init__(self, run_context: RunContext | None) -> None:
        self._run_context = run_context
        self._queue: queue.Queue[_PendingEvent] = queue.Queue(maxsize=EVENT_QUEUE_LIMIT)
        self._closing = threading.Event()
        self._dropped = 0
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        if run_context is not None:
            self._thread = threading.Thread(target=self._run, name="observability-emitter", daemon=True)
            self._thread.start()

    @property
    def dropped_count(self) -> int:
        with self._lock:
            return self._dropped

    def emit(
        self,
        kind: str,
        *,
        severity: ObservabilitySeverity = ObservabilitySeverity.INFO,
        context: ObservabilityContext,
        data: ObservabilityData | None = None,
        terminal: bool = False,
    ) -> None:
        if self._run_context is None:
            return
        pending_event = _PendingEvent(
            kind=kind,
            severity=severity,
            context=context,
            data=data or ObservabilityData(),
        )
        try:
            self._queue.put_nowait(pending_event)
        except queue.Full:
            if terminal:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    pass
                else:
                    with self._lock:
                        self._dropped += 1
                try:
                    self._queue.put_nowait(pending_event)
                    return
                except queue.Full:
                    pass
            with self._lock:
                self._dropped += 1

    def close(self) -> None:
        if self._thread is None:
            return
        self._closing.set()
        self._thread.join(timeout=EVENT_FLUSH_TIMEOUT_SECONDS)

    def _run(self) -> None:
        assert self._run_context is not None
        while not self._closing.is_set() or not self._queue.empty():
            try:
                event = self._queue.get(timeout=0.05)
            except queue.Empty:
                continue
            self._run_context.emit(
                event.kind,
                severity=event.severity,
                privacy=ObservabilityPrivacy.PRIVATE,
                context=event.context,
                data=event.data,
            )


class _StreamState:
    def __init__(self, capture_limit_bytes: int, tail_limit_bytes: int) -> None:
        self._capture_limit_bytes = capture_limit_bytes
        self._tail_limit_bytes = tail_limit_bytes
        self._capture = bytearray()
        self._tail = bytearray()
        self._total_bytes = 0
        self._decode_replacements = 0
        self._last_output_at: float | None = None
        self._overflowed = False
        self._closed = False
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._lock = threading.Lock()

    @property
    def overflowed(self) -> bool:
        with self._lock:
            return self._overflowed

    @property
    def last_output_at(self) -> float | None:
        with self._lock:
            return self._last_output_at

    def record(self, payload: bytes, observed_at: float) -> None:
        decoded = self._decoder.decode(payload, final=False)
        with self._lock:
            self._total_bytes += len(payload)
            self._last_output_at = observed_at
            self._decode_replacements += decoded.count("\ufffd")
            available = self._capture_limit_bytes - len(self._capture)
            if available > 0:
                self._capture.extend(payload[:available])
            if len(payload) > available:
                self._overflowed = True
            if len(payload) >= self._tail_limit_bytes:
                self._tail[:] = payload[-self._tail_limit_bytes :]
            else:
                self._tail.extend(payload)
                excess = len(self._tail) - self._tail_limit_bytes
                if excess > 0:
                    del self._tail[:excess]

    def close(self) -> None:
        decoded = self._decoder.decode(b"", final=True)
        with self._lock:
            self._decode_replacements += decoded.count("\ufffd")
            self._closed = True

    def snapshot(self) -> ProcessOutputSnapshot:
        with self._lock:
            retained_bytes = len(self._capture)
            return ProcessOutputSnapshot(
                capture=bytes(self._capture),
                tail=bytes(self._tail),
                total_bytes=self._total_bytes,
                retained_bytes=retained_bytes,
                dropped_bytes=self._total_bytes - retained_bytes,
                decode_replacements=self._decode_replacements,
            )


class _LineFramer:
    def __init__(self, maximum_bytes: int) -> None:
        self._maximum_bytes = maximum_bytes
        self._pending = bytearray()

    def feed(self, payload: bytes) -> list[bytes]:
        self._pending.extend(payload)
        return self._extract_records(final=False)

    def flush(self) -> list[bytes]:
        return self._extract_records(final=True)

    def _extract_records(self, *, final: bool) -> list[bytes]:
        records: list[bytes] = []
        while self._pending:
            delimiter = self._find_delimiter(final=final)
            if delimiter is not None:
                delimiter_index, delimiter_size = delimiter
                record = bytes(self._pending[:delimiter_index])
                del self._pending[: delimiter_index + delimiter_size]
                records.extend(self._split_record(record))
                continue
            if len(self._pending) > self._maximum_bytes:
                prefix_length = self._safe_prefix_length(self._pending, self._maximum_bytes)
                records.append(bytes(self._pending[:prefix_length]))
                del self._pending[:prefix_length]
                continue
            if final:
                record = bytes(self._pending)
                self._pending.clear()
                records.extend(self._split_record(record))
            break
        return records

    def _find_delimiter(self, *, final: bool) -> tuple[int, int] | None:
        for index, value in enumerate(self._pending):
            if value == ord("\n"):
                return index, 1
            if value != ord("\r"):
                continue
            if index + 1 < len(self._pending):
                return index, 2 if self._pending[index + 1] == ord("\n") else 1
            if final:
                return index, 1
            return None
        return None

    def _split_record(self, record: bytes) -> list[bytes]:
        if not record:
            return [b""]
        records: list[bytes] = []
        remaining = record
        while remaining:
            prefix_length = self._safe_prefix_length(remaining, self._maximum_bytes)
            records.append(remaining[:prefix_length])
            remaining = remaining[prefix_length:]
        return records

    @staticmethod
    def _safe_prefix_length(value: bytes | bytearray, maximum_bytes: int) -> int:
        end = min(len(value), maximum_bytes)
        if end == len(value):
            return end
        lead_index = end - 1
        while lead_index >= 0 and value[lead_index] & 0xC0 == 0x80:
            lead_index -= 1
        if lead_index < 0:
            return end
        lead = value[lead_index]
        expected_bytes = 4 if lead & 0xF8 == 0xF0 else 3 if lead & 0xF0 == 0xE0 else 2 if lead & 0xE0 == 0xC0 else 1
        if expected_bytes > end - lead_index and lead_index > 0:
            return lead_index
        return end


@dataclass
class _ArtifactState:
    path: Path | None = None
    size_bytes: int | None = None
    observed_at: float | None = None
    resolver_failed: bool = False


class ChildProcessRunner:
    def __init__(
        self,
        *,
        monotonic_clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._monotonic_clock = monotonic_clock
        self._wall_clock = wall_clock

    def run(
        self,
        spec: ProcessSpec,
        *,
        run_context: RunContext | None = None,
        cancellation_event: threading.Event | None = None,
        line_handler: LineHandler | None = None,
        output_observer: OutputObserver | None = None,
        progress_parser: ProgressParser | None = None,
    ) -> ProcessResult:
        started_at = self._monotonic_clock()
        tool_run_id = str(uuid4())
        tool_context = replace(
            spec.event_context,
            tool=ObservabilityTool(id=spec.tool_id, run_id=tool_run_id, version=spec.tool_version),
            process=None,
        )
        emitter = _AsyncRunEmitter(run_context)
        if self._is_cancelled(run_context, cancellation_event):
            emitter.emit(
                "tool.cancelled",
                context=tool_context,
                data=ObservabilityData(cancellation=ObservabilityCancellation(requested=True, forced=False)),
                terminal=True,
            )
            emitter.close()
            raise ProcessCancelled(f"{spec.display_name} was cancelled before it started")

        try:
            process = subprocess.Popen(
                [os.fspath(argument) for argument in spec.argv],
                stdin=spec.stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT if spec.merge_stderr else subprocess.PIPE,
                env=dict(spec.env) if spec.env is not None else None,
                cwd=spec.cwd,
                bufsize=0,
                start_new_session=True,
                close_fds=True,
            )
        except OSError:
            emitter.emit(
                "tool.failed",
                severity=ObservabilitySeverity.ERROR,
                context=tool_context,
                data=ObservabilityData(failure=ObservabilityFailure(code="spawn_failed", retryable=False)),
                terminal=True,
            )
            emitter.close()
            raise

        process_context = replace(
            tool_context,
            process=ObservabilityProcess(pid=process.pid, process_group_id=process.pid),
        )
        emitter.emit("tool.started", context=process_context)

        stream_queue: queue.Queue[_OutputChunk | _StreamClosed] = queue.Queue(maxsize=spec.queue_chunks)
        dispatch_overflow = threading.Event()
        reader_stop = threading.Event()
        stream_states = {
            ProcessStream.STDOUT: _StreamState(spec.capture_limit_bytes, spec.tail_limit_bytes),
            ProcessStream.STDERR: _StreamState(spec.capture_limit_bytes, spec.tail_limit_bytes),
        }
        readers = self._start_readers(
            process,
            spec,
            stream_states,
            stream_queue,
            dispatch_overflow,
            reader_stop,
        )
        expected_streams = {ProcessStream.STDOUT}
        if not spec.merge_stderr:
            expected_streams.add(ProcessStream.STDERR)
        closed_streams: set[ProcessStream] = set()
        framers = {stream: _LineFramer(spec.line_limit_bytes) for stream in expected_streams}
        artifact_states = [_ArtifactState(path=probe.path) for probe in spec.artifacts]

        pending_error: BaseException | None = None
        failure_code: str | None = None
        cancelled = False
        keyboard_interrupt: KeyboardInterrupt | None = None
        termination_requested_at: float | None = None
        kill_requested_at: float | None = None
        forced_termination = False
        process_exited_at: float | None = None
        last_activity_event_at = started_at
        last_artifact_event_at = started_at

        try:
            while True:
                now = self._monotonic_clock()
                try:
                    item = stream_queue.get(timeout=0.05)
                except queue.Empty:
                    item = None
                except KeyboardInterrupt as error:
                    keyboard_interrupt = error
                    cancelled = True
                    item = None

                if isinstance(item, _OutputChunk) and pending_error is None:
                    try:
                        if output_observer is not None:
                            output_observer(item.stream, item.payload)
                        for record in framers[item.stream].feed(item.payload):
                            self._handle_line(
                                record,
                                item.stream,
                                process_context,
                                emitter,
                                line_handler,
                                progress_parser,
                            )
                    except KeyboardInterrupt as error:
                        keyboard_interrupt = error
                        cancelled = True
                    except (Exception, SystemExit) as error:
                        pending_error = error
                        failure_code = "output_handler_failed"
                elif isinstance(item, _StreamClosed):
                    closed_streams.add(item.stream)
                    if pending_error is None:
                        try:
                            for record in framers[item.stream].flush():
                                self._handle_line(
                                    record,
                                    item.stream,
                                    process_context,
                                    emitter,
                                    line_handler,
                                    progress_parser,
                                )
                        except KeyboardInterrupt as error:
                            keyboard_interrupt = error
                            cancelled = True
                        except (Exception, SystemExit) as error:
                            pending_error = error
                            failure_code = "output_handler_failed"

                if dispatch_overflow.is_set() and pending_error is None:
                    pending_error = ProcessOutputDispatchError(
                        f"{spec.display_name} produced output faster than it could be processed"
                    )
                    failure_code = "output_dispatch_overflow"

                if spec.capture_overflow is CaptureOverflowPolicy.FAIL and pending_error is None:
                    if any(stream_states[stream].overflowed for stream in expected_streams):
                        pending_error = ProcessOutputLimitError(
                            f"{spec.display_name} exceeded the {spec.capture_limit_bytes}-byte output capture limit"
                        )
                        failure_code = "output_limit_exceeded"

                if self._is_cancelled(run_context, cancellation_event) or keyboard_interrupt is not None:
                    if not cancelled:
                        cancelled = True
                    if termination_requested_at is None:
                        emitter.emit(
                            "tool.cancellation_requested",
                            context=process_context,
                            data=ObservabilityData(cancellation=ObservabilityCancellation(requested=True)),
                        )

                if spec.timeout_seconds is not None and now - started_at >= spec.timeout_seconds:
                    if pending_error is None:
                        pending_error = ProcessTimeoutError(
                            f"{spec.display_name} exceeded its {spec.timeout_seconds:g}-second timeout"
                        )
                        failure_code = "timeout"

                if (cancelled or pending_error is not None) and termination_requested_at is None:
                    self._signal_process_group(process, process.pid, signal.SIGTERM)
                    termination_requested_at = now

                if (
                    termination_requested_at is not None
                    and (process.poll() is None or self._process_group_exists(process.pid))
                    and kill_requested_at is None
                    and now - termination_requested_at >= spec.termination_grace_seconds
                ):
                    self._signal_process_group(process, process.pid, signal.SIGKILL)
                    kill_requested_at = now
                    forced_termination = True

                returncode = process.poll()
                if returncode is not None and process_exited_at is None:
                    process_exited_at = now

                if (
                    process_exited_at is not None
                    and closed_streams != expected_streams
                    and now - process_exited_at >= spec.pipe_drain_timeout_seconds
                ):
                    if pending_error is None:
                        pending_error = ProcessPipeDrainError(
                            f"{spec.display_name} exited while a descendant kept its output stream open"
                        )
                        failure_code = "pipe_drain_timeout"
                    if termination_requested_at is None:
                        self._signal_process_group(process, process.pid, signal.SIGTERM)
                        termination_requested_at = now
                    elif kill_requested_at is None and now - termination_requested_at >= spec.termination_grace_seconds:
                        self._signal_process_group(process, process.pid, signal.SIGKILL)
                        kill_requested_at = now
                        forced_termination = True

                if (
                    returncode is not None
                    and closed_streams == expected_streams
                    and stream_queue.empty()
                    and self._process_group_exists(process.pid)
                ):
                    if pending_error is None:
                        pending_error = ProcessRunnerError(
                            f"{spec.display_name} exited while a descendant remained running"
                        )
                        failure_code = "descendant_survived"
                    if termination_requested_at is None:
                        self._signal_process_group(process, process.pid, signal.SIGTERM)
                        termination_requested_at = now

                if (
                    process_exited_at is not None
                    and closed_streams != expected_streams
                    and kill_requested_at is not None
                    and now - kill_requested_at >= spec.kill_wait_seconds
                ):
                    reader_stop.set()
                    self._close_process_streams(process)
                    break

                if now - last_activity_event_at >= spec.activity_interval_seconds:
                    emitter.emit(
                        "tool.heartbeat",
                        context=self._process_context(process_context, process.poll()),
                        data=ObservabilityData(
                            activity=ObservabilityActivity(
                                last_output_age_seconds=self._last_output_age_seconds(
                                    stream_states,
                                    expected_streams,
                                    started_at,
                                    now,
                                )
                            ),
                            counters=self._counters(stream_states, expected_streams),
                        ),
                    )
                    last_activity_event_at = now

                if spec.artifacts and now - last_artifact_event_at >= spec.artifact_interval_seconds:
                    self._emit_artifacts(
                        spec,
                        artifact_states,
                        process_context,
                        emitter,
                        state="growing",
                        now=now,
                    )
                    last_artifact_event_at = now

                if (
                    returncode is not None
                    and closed_streams == expected_streams
                    and stream_queue.empty()
                    and not self._process_group_exists(process.pid)
                ):
                    break

                if (
                    kill_requested_at is not None
                    and returncode is None
                    and now - kill_requested_at >= spec.kill_wait_seconds
                ):
                    process.kill()

                if (
                    kill_requested_at is not None
                    and returncode is not None
                    and closed_streams == expected_streams
                    and now - kill_requested_at >= spec.kill_wait_seconds
                ):
                    break

            returncode = process.wait(timeout=spec.kill_wait_seconds)
        except KeyboardInterrupt as error:
            keyboard_interrupt = error
            cancelled = True
            forced_termination = (
                self._terminate_process_group(
                    process,
                    process.pid,
                    spec.termination_grace_seconds,
                    spec.kill_wait_seconds,
                )
                or forced_termination
            )
            returncode = process.poll()
            if returncode is None:
                process.kill()
                returncode = process.wait()
        except (Exception, SystemExit) as error:
            pending_error = error
            failure_code = failure_code or "runner_interrupted"
            forced_termination = (
                self._terminate_process_group(
                    process,
                    process.pid,
                    spec.termination_grace_seconds,
                    spec.kill_wait_seconds,
                )
                or forced_termination
            )
            returncode = process.poll()
            if returncode is None:
                process.kill()
                returncode = process.wait()
        finally:
            reader_stop.set()
            self._close_process_streams(process)
            for reader in readers:
                reader.join(timeout=1.0)

        elapsed_ms = max(0, int((self._monotonic_clock() - started_at) * 1000))
        stdout_snapshot = stream_states[ProcessStream.STDOUT].snapshot()
        stderr_snapshot = (
            ProcessOutputSnapshot(b"", b"", 0, 0, 0, 0)
            if spec.merge_stderr
            else stream_states[ProcessStream.STDERR].snapshot()
        )
        result = ProcessResult(
            tool_run_id=tool_run_id,
            returncode=returncode,
            elapsed_ms=elapsed_ms,
            stdout=stdout_snapshot,
            stderr=stderr_snapshot,
            forced_termination=forced_termination,
        )
        final_context = self._process_context(process_context, returncode)
        final_data = ObservabilityData(
            activity=ObservabilityActivity(
                last_output_age_seconds=self._last_output_age_seconds(
                    stream_states,
                    expected_streams,
                    started_at,
                    self._monotonic_clock(),
                )
            ),
            counters=self._counters(stream_states, expected_streams),
        )
        self._emit_artifacts(
            spec,
            artifact_states,
            final_context,
            emitter,
            state="complete" if returncode == 0 and pending_error is None and not cancelled else "incomplete",
            now=self._monotonic_clock(),
        )

        if cancelled:
            emitter.emit(
                "tool.cancelled",
                context=final_context,
                data=replace(
                    final_data,
                    cancellation=ObservabilityCancellation(requested=True, forced=forced_termination),
                ),
                terminal=True,
            )
            emitter.close()
            if keyboard_interrupt is not None:
                raise keyboard_interrupt
            raise ProcessCancelled(f"{spec.display_name} was cancelled")

        if pending_error is not None:
            emitter.emit(
                "tool.failed",
                severity=ObservabilitySeverity.ERROR,
                context=final_context,
                data=replace(
                    final_data,
                    failure=ObservabilityFailure(code=failure_code or "runner_failed", retryable=False),
                ),
                terminal=True,
            )
            emitter.close()
            if isinstance(pending_error, ProcessRunnerError):
                pending_error.attach_output(stdout_snapshot, stderr_snapshot)
            raise pending_error

        if returncode != 0:
            emitter.emit(
                "tool.failed",
                severity=ObservabilitySeverity.ERROR,
                context=final_context,
                data=replace(
                    final_data,
                    failure=ObservabilityFailure(code="nonzero_exit", retryable=False),
                ),
                terminal=True,
            )
            emitter.close()
            raise ProcessExecutionError(
                returncode,
                [os.fspath(argument) for argument in spec.argv],
                stdout_snapshot,
                stderr_snapshot,
                merge_stderr=spec.merge_stderr,
            )

        emitter.emit("tool.completed", context=final_context, data=final_data, terminal=True)
        emitter.close()
        return result

    def _start_readers(
        self,
        process: subprocess.Popen[bytes],
        spec: ProcessSpec,
        stream_states: dict[ProcessStream, _StreamState],
        stream_queue: queue.Queue[_OutputChunk | _StreamClosed],
        dispatch_overflow: threading.Event,
        reader_stop: threading.Event,
    ) -> list[threading.Thread]:
        readers: list[threading.Thread] = []
        assert process.stdout is not None
        readers.append(
            self._start_reader(
                cast(BinaryIO, process.stdout),
                ProcessStream.STDOUT,
                stream_states[ProcessStream.STDOUT],
                stream_queue,
                dispatch_overflow,
                reader_stop,
            )
        )
        if not spec.merge_stderr:
            assert process.stderr is not None
            readers.append(
                self._start_reader(
                    cast(BinaryIO, process.stderr),
                    ProcessStream.STDERR,
                    stream_states[ProcessStream.STDERR],
                    stream_queue,
                    dispatch_overflow,
                    reader_stop,
                )
            )
        return readers

    def _start_reader(
        self,
        pipe: BinaryIO,
        stream: ProcessStream,
        state: _StreamState,
        stream_queue: queue.Queue[_OutputChunk | _StreamClosed],
        dispatch_overflow: threading.Event,
        reader_stop: threading.Event,
    ) -> threading.Thread:
        def read() -> None:
            try:
                while not reader_stop.is_set():
                    try:
                        payload = pipe.read(READ_CHUNK_BYTES)
                    except (OSError, ValueError):
                        break
                    if not payload:
                        break
                    state.record(payload, self._monotonic_clock())
                    try:
                        stream_queue.put_nowait(_OutputChunk(stream, payload))
                    except queue.Full:
                        dispatch_overflow.set()
            finally:
                state.close()
                while not reader_stop.is_set():
                    try:
                        stream_queue.put(_StreamClosed(stream), timeout=0.05)
                        break
                    except queue.Full:
                        continue

        reader = threading.Thread(target=read, name=f"{stream}-reader", daemon=True)
        reader.start()
        return reader

    @staticmethod
    def _handle_line(
        record: bytes,
        stream: ProcessStream,
        process_context: ObservabilityContext,
        emitter: _AsyncRunEmitter,
        line_handler: LineHandler | None,
        progress_parser: ProgressParser | None,
    ) -> None:
        line = record.decode("utf-8", errors="replace").removesuffix("\r")
        if line_handler is not None:
            line_handler(stream, line)
        if progress_parser is not None:
            progress = progress_parser(stream, line)
            if progress is not None:
                emitter.emit(
                    "tool.progress",
                    context=process_context,
                    data=ObservabilityData(progress=progress),
                )

    @staticmethod
    def _is_cancelled(run_context: RunContext | None, cancellation_event: threading.Event | None) -> bool:
        return bool(
            (run_context is not None and run_context.cancellation.is_cancelled)
            or (cancellation_event is not None and cancellation_event.is_set())
        )

    @staticmethod
    def _signal_process_group(
        process: subprocess.Popen[bytes],
        process_group_id: int,
        requested_signal: signal.Signals,
    ) -> None:
        try:
            os.killpg(process_group_id, requested_signal)
        except (ProcessLookupError, PermissionError):
            if process.poll() is None:
                try:
                    process.send_signal(requested_signal)
                except ProcessLookupError:
                    return

    @staticmethod
    def _process_group_exists(process_group_id: int) -> bool:
        try:
            os.killpg(process_group_id, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _terminate_process_group(
        self,
        process: subprocess.Popen[bytes],
        process_group_id: int,
        termination_grace_seconds: float,
        kill_wait_seconds: float,
    ) -> bool:
        self._signal_process_group(process, process_group_id, signal.SIGTERM)
        deadline = self._monotonic_clock() + termination_grace_seconds
        while self._monotonic_clock() < deadline:
            if process.poll() is not None and not self._process_group_exists(process_group_id):
                return False
            time.sleep(0.01)

        self._signal_process_group(process, process_group_id, signal.SIGKILL)
        deadline = self._monotonic_clock() + kill_wait_seconds
        while self._monotonic_clock() < deadline:
            if process.poll() is not None and not self._process_group_exists(process_group_id):
                return True
            time.sleep(0.01)
        return True

    @staticmethod
    def _close_process_streams(process: subprocess.Popen[bytes]) -> None:
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except (OSError, ValueError):
                    continue

    @staticmethod
    def _process_context(context: ObservabilityContext, returncode: int | None) -> ObservabilityContext:
        current = context.process
        if current is None or returncode is None:
            return context
        return replace(
            context,
            process=replace(
                current,
                exit_code=returncode if returncode >= 0 else None,
                signal=-returncode if returncode < 0 else None,
            ),
        )

    @staticmethod
    def _last_output_age_seconds(
        states: dict[ProcessStream, _StreamState],
        streams: set[ProcessStream],
        started_at: float,
        now: float,
    ) -> int:
        last_output_at = max((states[stream].last_output_at or started_at for stream in streams), default=started_at)
        return max(0, int(now - last_output_at))

    @staticmethod
    def _counters(
        states: dict[ProcessStream, _StreamState],
        streams: set[ProcessStream],
    ) -> ObservabilityCounters:
        snapshots = [states[stream].snapshot() for stream in streams]
        return ObservabilityCounters(
            total_bytes=sum(snapshot.total_bytes for snapshot in snapshots),
            retained_bytes=sum(snapshot.retained_bytes for snapshot in snapshots),
            dropped_bytes=sum(snapshot.dropped_bytes for snapshot in snapshots),
            decode_replacements=sum(snapshot.decode_replacements for snapshot in snapshots),
        )

    def _emit_artifacts(
        self,
        spec: ProcessSpec,
        artifact_states: list[_ArtifactState],
        context: ObservabilityContext,
        emitter: _AsyncRunEmitter,
        *,
        state: str,
        now: float,
    ) -> None:
        for probe, artifact_state in zip(spec.artifacts, artifact_states, strict=True):
            try:
                path = probe.resolve_path()
            except Exception:
                if not artifact_state.resolver_failed:
                    emitter.emit(
                        "tool.artifact_probe_failed",
                        severity=ObservabilitySeverity.WARNING,
                        context=context,
                        data=ObservabilityData(
                            failure=ObservabilityFailure(code="artifact_resolver_failed", retryable=False)
                        ),
                    )
                    artifact_state.resolver_failed = True
                path = None
            if artifact_state.path != path:
                artifact_state.path = path
                artifact_state.size_bytes = None
                artifact_state.observed_at = None
            if path is None:
                artifact = ObservabilityArtifact(role=probe.role, state="missing")
                emitter.emit(
                    "tool.artifact",
                    context=context,
                    data=ObservabilityData(artifact=artifact),
                )
                continue
            try:
                status = path.stat()
            except OSError:
                artifact = ObservabilityArtifact(
                    role=probe.role,
                    state="missing",
                    location=self._artifact_location(path),
                )
            else:
                growth = None
                if artifact_state.size_bytes is not None and artifact_state.observed_at is not None:
                    elapsed = now - artifact_state.observed_at
                    if elapsed > 0:
                        growth = max(0, int((status.st_size - artifact_state.size_bytes) / elapsed))
                artifact_state.size_bytes = status.st_size
                artifact_state.observed_at = now
                artifact = ObservabilityArtifact(
                    role=probe.role,
                    state=state,
                    location=self._artifact_location(path),
                    size_bytes=self._round_down(status.st_size, ARTIFACT_SIZE_QUANTUM_BYTES),
                    modification_age_seconds=self._round_down(
                        max(0, int(self._wall_clock() - status.st_mtime)),
                        ARTIFACT_AGE_QUANTUM_SECONDS,
                    ),
                    growth_bytes_per_second=(
                        None if growth is None else self._round_down(growth, ARTIFACT_SIZE_QUANTUM_BYTES)
                    ),
                )
            emitter.emit(
                "tool.artifact",
                context=context,
                data=ObservabilityData(artifact=artifact),
            )

    @staticmethod
    def _artifact_location(path: Path) -> ObservabilityText | None:
        try:
            return ObservabilityText.bounded(
                path.as_posix(),
                privacy=ObservabilityPrivacy.PRIVATE,
                maximum_bytes=MAX_DETAIL_BYTES,
            )
        except (TypeError, ValueError, UnicodeError):
            return None

    @staticmethod
    def _round_down(value: int, quantum: int) -> int:
        return value - (value % quantum)
