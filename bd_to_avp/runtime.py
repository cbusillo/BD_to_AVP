from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import uuid4

from bd_to_avp.observability import (
    EventSink,
    NullEventSink,
    ObservabilityContext,
    ObservabilityData,
    ObservabilityEmitter,
    ObservabilityEvent,
    ObservabilityPrivacy,
    ObservabilityRedaction,
    ObservabilitySeverity,
)


DiagnosticObserver = Callable[[str, bytes], object]


class CancellationToken:
    def __init__(self, event: threading.Event | None = None) -> None:
        self._event = event or threading.Event()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def event(self) -> threading.Event:
        return self._event

    def cancel(self) -> None:
        self._event.set()


class ObservabilityStream:
    def __init__(
        self,
        emitter: ObservabilityEmitter,
        sink: EventSink | None = None,
        *,
        stream_id: str | None = None,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._emitter = emitter
        self._sink = sink or NullEventSink()
        self._stream_id = stream_id or str(uuid4())
        self._wall_clock = wall_clock
        self._monotonic_clock = monotonic_clock
        self._started_at = monotonic_clock()
        self._lock = threading.Lock()
        self._counter_lock = threading.Lock()
        self._emitting = threading.local()
        self._sequence = 0
        self._failure_count = 0
        self._reentrant_drop_count = 0

    @property
    def stream_id(self) -> str:
        return self._stream_id

    @property
    def failure_count(self) -> int:
        with self._counter_lock:
            return self._failure_count

    @property
    def reentrant_drop_count(self) -> int:
        with self._counter_lock:
            return self._reentrant_drop_count

    def emit(
        self,
        kind: str,
        *,
        severity: ObservabilitySeverity = ObservabilitySeverity.INFO,
        privacy: ObservabilityPrivacy = ObservabilityPrivacy.PRIVATE,
        redaction: ObservabilityRedaction = ObservabilityRedaction.RAW,
        context: ObservabilityContext | None = None,
        data: ObservabilityData | None = None,
    ) -> ObservabilityEvent | None:
        if getattr(self._emitting, "active", False):
            with self._counter_lock:
                self._reentrant_drop_count += 1
            return None
        with self._lock:
            event = ObservabilityEvent(
                emitter=self._emitter,
                stream_id=self._stream_id,
                sequence=self._sequence,
                occurred_at=self._wall_clock(),
                elapsed_ms=max(0, int((self._monotonic_clock() - self._started_at) * 1000)),
                kind=kind,
                severity=severity,
                privacy=privacy,
                redaction=redaction,
                context=context or ObservabilityContext(),
                data=data or ObservabilityData(),
            )
            self._sequence += 1
            self._emitting.active = True
            try:
                self._sink.emit(event)
            except Exception:
                with self._counter_lock:
                    self._failure_count += 1
            finally:
                self._emitting.active = False
            return event


@dataclass(frozen=True)
class RunContext:
    observability: ObservabilityStream
    cancellation: CancellationToken = field(default_factory=CancellationToken)
    diagnostic_observer: DiagnosticObserver | None = field(default=None, compare=False, repr=False)

    def emit(self, kind: str, **kwargs: Any) -> ObservabilityEvent | None:
        return self.observability.emit(kind, **kwargs)

    def observe_diagnostic_output(self, stream: str, payload: bytes) -> None:
        if self.diagnostic_observer is None:
            return
        try:
            self.diagnostic_observer(stream, payload)
        except Exception:
            return
