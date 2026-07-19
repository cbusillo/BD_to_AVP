from __future__ import annotations

import codecs
import io
import os
import select
import threading

from collections import deque
from dataclasses import dataclass
from typing import TextIO


DIAGNOSTIC_WRITE_TIMEOUT_SECONDS = 0.1
DIAGNOSTIC_WRITE_CHUNK_BYTES = 4 * 1024


@dataclass(frozen=True)
class WorkerDiagnosticSnapshot:
    written_bytes: int
    dropped_bytes: int
    dropped_chunks: int
    failure_count: int
    pending_bytes: int


class WorkerDiagnosticRelay:
    def __init__(
        self,
        stream: TextIO,
        *,
        maximum_pending_bytes: int = 512 * 1024,
        maximum_chunk_bytes: int = 16 * 1024,
    ) -> None:
        if maximum_pending_bytes <= 0 or maximum_chunk_bytes <= 0:
            raise ValueError("diagnostic relay bounds must be positive")
        self._stream = stream
        self._file_descriptor = self._stream_file_descriptor(stream)
        self._maximum_pending_bytes = maximum_pending_bytes
        self._maximum_chunk_bytes = min(maximum_chunk_bytes, maximum_pending_bytes)
        self._condition = threading.Condition()
        self._pending: deque[tuple[str, bytes]] = deque()
        self._pending_bytes = 0
        self._in_flight_bytes = 0
        self._written_bytes = 0
        self._dropped_bytes = 0
        self._dropped_chunks = 0
        self._failure_count = 0
        self._closing = False
        self._failed = False
        self._thread = threading.Thread(
            target=self._drain,
            name="worker-diagnostic-relay",
            daemon=True,
        )
        self._thread.start()

    def emit(self, stream: str, payload: bytes) -> None:
        if not payload:
            return
        normalized_stream = stream if stream in {"stdout", "stderr"} else "stderr"
        for offset in range(0, len(payload), self._maximum_chunk_bytes):
            self._enqueue(normalized_stream, bytes(payload[offset : offset + self._maximum_chunk_bytes]))

    def close(self, timeout: float = 0.25) -> WorkerDiagnosticSnapshot:
        with self._condition:
            self._closing = True
            self._condition.notify_all()
        self._thread.join(timeout=max(0, timeout))
        return self.snapshot()

    def snapshot(self) -> WorkerDiagnosticSnapshot:
        with self._condition:
            return WorkerDiagnosticSnapshot(
                written_bytes=self._written_bytes,
                dropped_bytes=self._dropped_bytes,
                dropped_chunks=self._dropped_chunks,
                failure_count=self._failure_count,
                pending_bytes=self._pending_bytes + self._in_flight_bytes,
            )

    def _enqueue(self, stream: str, payload: bytes) -> None:
        with self._condition:
            if self._closing or self._failed:
                self._record_drop_locked(len(payload))
                return
            while (
                self._pending
                and self._in_flight_bytes + self._pending_bytes + len(payload) > self._maximum_pending_bytes
            ):
                _, removed = self._pending.popleft()
                self._pending_bytes -= len(removed)
                self._record_drop_locked(len(removed))
            available_bytes = self._maximum_pending_bytes - self._in_flight_bytes - self._pending_bytes
            if available_bytes <= 0:
                self._record_drop_locked(len(payload))
                return
            if len(payload) > available_bytes:
                retained = self._utf8_safe_tail(payload, available_bytes)
                self._record_drop_locked(len(payload) - len(retained))
                payload = retained
                if not payload:
                    return
            self._pending.append((stream, payload))
            self._pending_bytes += len(payload)
            self._condition.notify()

    def _record_drop_locked(self, byte_count: int) -> None:
        self._dropped_bytes += byte_count
        self._dropped_chunks += 1

    def _drain(self) -> None:
        decoders = {
            "stdout": codecs.getincrementaldecoder("utf-8")("replace"),
            "stderr": codecs.getincrementaldecoder("utf-8")("replace"),
        }
        try:
            while True:
                with self._condition:
                    while not self._pending and not self._closing:
                        self._condition.wait()
                    if not self._pending:
                        break
                    stream, payload = self._pending.popleft()
                    self._pending_bytes -= len(payload)
                    self._in_flight_bytes += len(payload)
                try:
                    text = decoders[stream].decode(payload, final=False)
                    if text:
                        self._write_text(text)
                except Exception:
                    with self._condition:
                        self._in_flight_bytes -= len(payload)
                        self._record_drop_locked(len(payload))
                    raise
                with self._condition:
                    self._in_flight_bytes -= len(payload)
                    self._written_bytes += len(payload)
            for decoder in decoders.values():
                text = decoder.decode(b"", final=True)
                if text:
                    self._write_text(text)
            if self._file_descriptor is None:
                self._stream.flush()
        except Exception:
            with self._condition:
                self._failure_count += 1
                self._failed = True
                while self._pending:
                    _, payload = self._pending.popleft()
                    self._pending_bytes -= len(payload)
                    self._record_drop_locked(len(payload))

    def _write_text(self, text: str) -> None:
        if self._file_descriptor is None:
            self._stream.write(text)
            self._stream.flush()
            return
        pending = memoryview(text.encode("utf-8"))
        while pending:
            _, writable, _ = select.select(
                [],
                [self._file_descriptor],
                [],
                DIAGNOSTIC_WRITE_TIMEOUT_SECONDS,
            )
            if not writable:
                raise TimeoutError("diagnostic stream remained blocked")
            try:
                written = os.write(
                    self._file_descriptor,
                    pending[:DIAGNOSTIC_WRITE_CHUNK_BYTES],
                )
            except InterruptedError:
                continue
            if written <= 0:
                raise BrokenPipeError("diagnostic stream closed during write")
            pending = pending[written:]

    @staticmethod
    def _stream_file_descriptor(stream: TextIO) -> int | None:
        try:
            file_descriptor = stream.fileno()
        except (AttributeError, io.UnsupportedOperation, OSError, ValueError):
            return None
        return file_descriptor if file_descriptor >= 0 else None

    @staticmethod
    def _utf8_safe_tail(payload: bytes, maximum_bytes: int) -> bytes:
        start = max(0, len(payload) - maximum_bytes)
        while start < len(payload) and payload[start] & 0xC0 == 0x80:
            start += 1
        return payload[start:]
