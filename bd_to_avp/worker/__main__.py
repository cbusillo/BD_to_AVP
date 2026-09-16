from __future__ import annotations

import sys
import threading
import time
import traceback

from typing import Callable, TextIO

from bd_to_avp.modules.config import config
from bd_to_avp.observability import ObservabilityEmitter
from bd_to_avp.runtime import CancellationToken, ObservabilityStream, RunContext
from bd_to_avp.worker.operations import WorkerDecisionRequired, WorkerOperationError, run_operation
from bd_to_avp.worker.diagnostics import WorkerDiagnosticRelay
from bd_to_avp.worker.controls import CONTROL_CAPABILITY, WorkerControls, WorkerInputReader
from bd_to_avp.worker.ownership import WorkerCancelled, WorkerProcessOwner
from bd_to_avp.worker.protocol import (
    EVENT_WRITE_TIMEOUT_SECONDS,
    ZERO_JOB_ID,
    JobSpec,
    WorkerActivityReporter,
    WorkerEventEmitter,
    WorkerEventTransportError,
    WorkerEventType,
    WorkerObservabilitySink,
    WorkerOperation,
    WorkerProtocolError,
    bounded_detail,
)

OperationRunner = Callable[[JobSpec, WorkerProcessOwner, WorkerActivityReporter], dict[str, object]]
APPLE_VISION_OCR_SMOKE_ARGUMENT = "--smoke-apple-vision-ocr"
TRANSPORT_FAILURE_EXIT_CODE = 74


def run_smoke_command(
    arguments: list[str],
    output_stream: TextIO,
    *,
    apple_vision_loader: Callable[[], object] | None = None,
) -> int | None:
    if arguments != [APPLE_VISION_OCR_SMOKE_ARGUMENT]:
        return None
    if apple_vision_loader is None:
        from bd_to_avp.vendor.pgsrip.ocr import AppleVisionOcr

        apple_vision_loader = AppleVisionOcr._load_frameworks
    apple_vision_loader()
    output_stream.write("Apple Vision OCR import smoke passed\n")
    return 0


def run_worker(
    input_stream: TextIO,
    output_stream: TextIO,
    diagnostic_stream: TextIO,
    *,
    establish_session: bool = True,
    heartbeat_interval: float = 1.0,
    operation_runner: OperationRunner = run_operation,
    isolate_process_stdin: bool = False,
    event_write_timeout_seconds: float = EVENT_WRITE_TIMEOUT_SECONDS,
) -> int:
    try:
        return _run_worker(
            input_stream,
            output_stream,
            diagnostic_stream,
            establish_session=establish_session,
            heartbeat_interval=heartbeat_interval,
            operation_runner=operation_runner,
            isolate_process_stdin=isolate_process_stdin,
            event_write_timeout_seconds=event_write_timeout_seconds,
        )
    except WorkerEventTransportError:
        # A partial record makes subsequent JSONL unsafe. Cleanup already ran;
        # the host must treat this exit as missing protocol delivery, not a
        # media stall or a claim that a terminal event was received.
        return TRANSPORT_FAILURE_EXIT_CODE


def _run_worker(
    input_stream: TextIO,
    output_stream: TextIO,
    diagnostic_stream: TextIO,
    *,
    establish_session: bool,
    heartbeat_interval: float,
    operation_runner: OperationRunner,
    isolate_process_stdin: bool,
    event_write_timeout_seconds: float,
) -> int:
    owner = WorkerProcessOwner()
    process_group_id = owner.establish_session() if establish_session else 0
    owner.install_signal_handlers()

    emitter: WorkerEventEmitter | None = None
    input_reader: WorkerInputReader | None = None
    controls: WorkerControls | None = None
    diagnostic_relay: WorkerDiagnosticRelay | None = None
    heartbeat_stop: threading.Event | None = None
    heartbeat_thread: threading.Thread | None = None
    cleanup_done = False

    def cleanup() -> None:
        nonlocal cleanup_done
        if cleanup_done:
            return
        cleanup_done = True
        _close_worker_resources(owner, input_reader, controls, diagnostic_relay, heartbeat_stop, heartbeat_thread)

    try:
        input_reader = WorkerInputReader(input_stream, isolate_process_stdin=isolate_process_stdin)
        job = JobSpec.from_json_line(input_reader.read_job_line(owner.check_cancelled))
        emitter = WorkerEventEmitter(output_stream, job.job_id, write_timeout_seconds=event_write_timeout_seconds)
        emitter.emit(
            WorkerEventType.WORKER_READY,
            {
                "worker_version": config.app.code_version,
                "process_group_id": process_group_id,
                "control_capabilities": [CONTROL_CAPABILITY],
            },
        )
        controls = WorkerControls(job.job_id, emitter)
        input_reader.set_control_handler(controls.receive_line)
        emitter.emit(WorkerEventType.JOB_STARTED, {"operation": job.operation.value})
        diagnostic_relay = WorkerDiagnosticRelay(diagnostic_stream)
        run_context = RunContext(
            observability=ObservabilityStream(
                ObservabilityEmitter.WORKER,
                WorkerObservabilitySink(emitter),
                stream_id=job.job_id,
            ),
            cancellation=CancellationToken(owner.cancellation_event),
            diagnostic_observer=diagnostic_relay.emit,
            process_controls=controls,
        )
        activity = WorkerActivityReporter(emitter, run_context)
        if job.operation.value == "inspect_source":
            activity.stage_started("inspect_source", "Reading video metadata")

        heartbeat_stop = threading.Event()
        heartbeat_thread = threading.Thread(
            target=_emit_heartbeats,
            args=(emitter, activity, owner, heartbeat_stop, heartbeat_interval),
            daemon=True,
        )
        heartbeat_thread.start()
        try:
            result = operation_runner(job, owner, activity)
        finally:
            cleanup()
            diagnostic_snapshot = diagnostic_relay.snapshot()
            if not emitter.terminal_emitted and (
                diagnostic_snapshot.dropped_bytes > 0
                or diagnostic_snapshot.failure_count > 0
                or diagnostic_snapshot.pending_bytes > 0
            ):
                activity.warning(
                    "Child diagnostic output was truncated.",
                    code="diagnostic_output_truncated",
                    dropped_bytes=diagnostic_snapshot.dropped_bytes,
                    dropped_chunks=diagnostic_snapshot.dropped_chunks,
                    pending_bytes=diagnostic_snapshot.pending_bytes,
                    relay_failures=diagnostic_snapshot.failure_count,
                )

        if job.operation is not WorkerOperation.START_LIVE_SOURCE:
            owner.check_cancelled()
        result_key = {
            "convert_source": "conversion_result",
            "preview_source": "preview_result",
            "start_live_source": "live_source_result",
        }.get(job.operation.value, "result")
        emitter.emit(WorkerEventType.JOB_COMPLETED, {result_key: result})
        return 0
    except WorkerProtocolError as error:
        emitter = emitter or WorkerEventEmitter(
            output_stream, error.job_id or ZERO_JOB_ID, write_timeout_seconds=event_write_timeout_seconds
        )
        emitter.fail(error.code, error.message)
        return 2
    except WorkerCancelled:
        owner.terminate_descendants()
        if emitter is not None and not emitter.terminal_emitted:
            emitter.emit(
                WorkerEventType.JOB_CANCELLED,
                {"message": "Worker job cancelled."},
            )
        return 130
    except WorkerDecisionRequired as error:
        if emitter is not None and not emitter.terminal_emitted:
            decision: dict[str, object] = {
                "id": error.code,
                "prompt": error.message,
                "choices": list(error.choices),
            }
            if error.details:
                decision["details"] = bounded_detail(error.details)
            emitter.emit(WorkerEventType.JOB_DECISION_REQUIRED, {"decision": decision})
        return 3
    except WorkerOperationError as error:
        if owner.cancellation_event.is_set():
            owner.terminate_descendants()
            if emitter is not None and not emitter.terminal_emitted:
                emitter.emit(
                    WorkerEventType.JOB_CANCELLED,
                    {"message": "Worker job cancelled."},
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
    except WorkerEventTransportError:
        raise
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
        cleanup()


def _close_worker_resources(
    owner: WorkerProcessOwner,
    input_reader: WorkerInputReader | None,
    controls: WorkerControls | None,
    diagnostics: WorkerDiagnosticRelay | None,
    heartbeat_stop: threading.Event | None,
    heartbeat_thread: threading.Thread | None,
) -> None:
    # Descendants are reaped before protocol output can wait for the host.
    # Every independent cleanup is attempted even if another one fails.
    closing: list[Callable[[], object]] = [owner.terminate_descendants]
    if heartbeat_stop is not None:
        closing.append(heartbeat_stop.set)
    if heartbeat_thread is not None:
        closing.append(lambda: heartbeat_thread.join(timeout=0.2))
    if input_reader is not None:
        closing.append(input_reader.close)
    if controls is not None:
        closing.append(controls.close)
    if diagnostics is not None:
        closing.append(diagnostics.close)
    first_error: Exception | None = None
    for close in closing:
        try:
            close()
        except WorkerEventTransportError:
            # The emitter retains the failed transport state. A later terminal
            # attempt fails promptly and the outer wrapper reports exit 74.
            continue
        except Exception as error:
            first_error = first_error or error
    if first_error is not None:
        raise first_error


def _emit_heartbeats(
    emitter: WorkerEventEmitter,
    activity: WorkerActivityReporter,
    owner: WorkerProcessOwner,
    stop_event: threading.Event,
    interval: float,
) -> None:
    started_at = time.monotonic()
    while not stop_event.wait(interval):
        if owner.cancellation_event.is_set() or emitter.terminal_emitted:
            return
        try:
            activity.emit_heartbeat(int(time.monotonic() - started_at))
        except (RuntimeError, WorkerEventTransportError):
            return


def main() -> None:
    smoke_result = run_smoke_command(sys.argv[1:], sys.stdout)
    if smoke_result is not None:
        raise SystemExit(smoke_result)
    raise SystemExit(run_worker(sys.stdin, sys.stdout, sys.stderr, isolate_process_stdin=True))


if __name__ == "__main__":
    main()
