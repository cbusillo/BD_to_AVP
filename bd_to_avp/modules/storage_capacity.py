from __future__ import annotations

import os
import queue
import threading

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from bd_to_avp.observability import (
    ObservabilityData,
    ObservabilityPrivacy,
    ObservabilityRedaction,
    ObservabilityStorage,
)

if TYPE_CHECKING:
    from bd_to_avp.worker.protocol import WorkerActivityReporter


@dataclass(frozen=True)
class _POSIXCapacitySnapshot:
    available_bytes: int
    total_bytes: int
    read_only: bool
    writable: bool


def probe_storage_capacity(
    path: Path,
    *,
    role: str,
    required_bytes: int | None = None,
    timeout_seconds: float = 2.0,
) -> ObservabilityStorage:
    result: queue.Queue[_POSIXCapacitySnapshot | BaseException] = queue.Queue(maxsize=1)

    def collect() -> None:
        try:
            candidate = path
            while True:
                try:
                    values = os.statvfs(candidate)
                    break
                except FileNotFoundError:
                    parent = candidate.parent
                    if parent == candidate:
                        raise
                    candidate = parent
            result.put(
                _POSIXCapacitySnapshot(
                    available_bytes=values.f_bavail * values.f_frsize,
                    total_bytes=values.f_blocks * values.f_frsize,
                    read_only=bool(values.f_flag & getattr(os, "ST_RDONLY", 1)),
                    writable=os.access(candidate, os.W_OK),
                )
            )
        except BaseException as error:
            result.put(error)

    thread = threading.Thread(target=collect, name="storage-capacity-probe", daemon=True)
    thread.start()
    thread.join(timeout_seconds)
    if thread.is_alive():
        return _unknown_storage(role, status="error", reason="timeout")

    outcome = result.get_nowait()
    if isinstance(outcome, PermissionError):
        return _unknown_storage(role, status="inaccessible", reason="permission_denied")
    if isinstance(outcome, BaseException):
        return _unknown_storage(role, status="error", reason="unavailable")

    available_bytes = outcome.available_bytes
    if available_bytes < 0:
        return _unknown_storage(role, status="error", reason="invalid_reading")
    if available_bytes == 0 and not outcome.read_only:
        return ObservabilityStorage(
            role=role,
            status="available",
            available_bytes=None,
            total_bytes=outcome.total_bytes,
            read_only=outcome.read_only,
            writable=outcome.writable,
            capacity_state="unknown",
            capacity_sufficiency="unknown",
            capacity_provenance=("worker_posix_statvfs",),
            capacity_unknown_reason="zero_on_writable_volume",
            required_bytes=required_bytes,
        )

    if required_bytes is None:
        sufficiency = "unknown"
    elif available_bytes < required_bytes:
        sufficiency = "insufficient"
    else:
        sufficiency = "sufficient"
    return ObservabilityStorage(
        role=role,
        status="available",
        available_bytes=available_bytes,
        total_bytes=outcome.total_bytes,
        read_only=outcome.read_only,
        writable=outcome.writable,
        capacity_state="known",
        capacity_sufficiency=sufficiency,
        capacity_provenance=("worker_posix_statvfs",),
        required_bytes=required_bytes,
        available_lower_bytes=available_bytes,
        available_upper_bytes=available_bytes,
    )


def emit_storage_capacity(
    activity: WorkerActivityReporter,
    path: Path,
    *,
    role: str,
    required_bytes: int | None = None,
) -> ObservabilityStorage:
    storage = probe_storage_capacity(
        path,
        role=role,
        required_bytes=required_bytes,
    )
    if activity.run_context is not None:
        activity.run_context.emit(
            "storage.probed",
            privacy=ObservabilityPrivacy.PRIVATE,
            redaction=ObservabilityRedaction.RAW,
            data=ObservabilityData(storage=storage),
        )
    return storage


def _unknown_storage(role: str, *, status: str, reason: str) -> ObservabilityStorage:
    return ObservabilityStorage(
        role=role,
        status=status,
        capacity_state="unknown",
        capacity_sufficiency="unknown",
        capacity_provenance=("worker_posix_statvfs",),
        capacity_unknown_reason=reason,
    )
