from __future__ import annotations

import sys
import threading
import time
import traceback

from contextlib import redirect_stdout
from typing import Callable, TextIO

from bd_to_avp.modules.config import config
from bd_to_avp.worker.operations import WorkerOperationError, run_operation
from bd_to_avp.worker.ownership import WorkerCancelled, WorkerProcessOwner
from bd_to_avp.worker.protocol import (
    MAX_REQUEST_BYTES,
    ZERO_JOB_ID,
    JobSpec,
    WorkerEventEmitter,
    WorkerEventType,
    WorkerProtocolError,
)

OperationRunner = Callable[[JobSpec, WorkerProcessOwner], dict[str, object]]


def run_worker(
    input_stream: TextIO,
    output_stream: TextIO,
    diagnostic_stream: TextIO,
    *,
    establish_session: bool = True,
    heartbeat_interval: float = 1.0,
    operation_runner: OperationRunner = run_operation,
) -> int:
    owner = WorkerProcessOwner()
    process_group_id = owner.establish_session() if establish_session else 0
    owner.install_signal_handlers()

    emitter: WorkerEventEmitter | None = None

    try:
        request_line = input_stream.readline(MAX_REQUEST_BYTES + 1)
        job = JobSpec.from_json_line(request_line)
        if input_stream.read(1):
            raise WorkerProtocolError(
                "multiple_requests",
                "The worker accepts exactly one request per process.",
                job_id=job.job_id,
            )
        emitter = WorkerEventEmitter(output_stream, job.job_id)
        emitter.emit(
            WorkerEventType.WORKER_READY,
            {
                "worker_version": config.app.code_version,
                "process_group_id": process_group_id,
            },
        )
        emitter.emit(WorkerEventType.JOB_STARTED, {"operation": job.operation.value})
        emitter.emit(
            WorkerEventType.STAGE_STARTED,
            {"stage": "inspect_source", "message": "Reading video metadata"},
        )

        heartbeat_stop = threading.Event()
        heartbeat_thread = threading.Thread(
            target=_emit_heartbeats,
            args=(emitter, owner, heartbeat_stop, heartbeat_interval),
            daemon=True,
        )
        heartbeat_thread.start()
        try:
            with redirect_stdout(diagnostic_stream):
                result = operation_runner(job, owner)
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=max(heartbeat_interval * 2, 0.2))

        owner.check_cancelled()
        emitter.emit(WorkerEventType.JOB_COMPLETED, {"result": result})
        return 0
    except WorkerProtocolError as error:
        emitter = emitter or WorkerEventEmitter(output_stream, error.job_id or ZERO_JOB_ID)
        emitter.fail(error.code, error.message)
        return 2
    except WorkerCancelled:
        owner.terminate_descendants()
        if emitter is not None and not emitter.terminal_emitted:
            emitter.emit(
                WorkerEventType.JOB_CANCELLED,
                {"message": "Source inspection cancelled."},
            )
        return 130
    except WorkerOperationError as error:
        if owner.cancellation_event.is_set():
            owner.terminate_descendants()
            if emitter is not None and not emitter.terminal_emitted:
                emitter.emit(
                    WorkerEventType.JOB_CANCELLED,
                    {"message": "Source inspection cancelled."},
                )
            return 130
        if emitter is not None and not emitter.terminal_emitted:
            emitter.fail(
                error.code,
                error.message,
                details=error.details,
                retryable=error.retryable,
            )
        return 1
    except Exception as error:
        traceback.print_exc(file=diagnostic_stream)
        if emitter is not None and not emitter.terminal_emitted:
            emitter.fail(
                "internal_error",
                "The worker encountered an unexpected error.",
                details=str(error),
            )
        return 1
    finally:
        owner.terminate_descendants()


def _emit_heartbeats(
    emitter: WorkerEventEmitter,
    owner: WorkerProcessOwner,
    stop_event: threading.Event,
    interval: float,
) -> None:
    started_at = time.monotonic()
    while not stop_event.wait(interval):
        if owner.cancellation_event.is_set() or emitter.terminal_emitted:
            return
        emitter.emit(
            WorkerEventType.HEARTBEAT,
            {
                "stage": "inspect_source",
                "elapsed_seconds": max(0, int(time.monotonic() - started_at)),
            },
        )


def main() -> None:
    raise SystemExit(run_worker(sys.stdin, sys.stdout, sys.stderr))


if __name__ == "__main__":
    main()
