"""Bounded stdin framing and job-scoped, acknowledged wait commands."""

from __future__ import annotations

import io
import json
import os
import queue
import select
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, Mapping, TextIO
from uuid import UUID

from bd_to_avp.runtime import WAIT_GRANT_SECONDS, WaitCommand
from bd_to_avp.worker.protocol import (
    MAX_REQUEST_BYTES,
    PROTOCOL_VERSION,
    WorkerEventEmitter,
    WorkerEventType,
    WorkerProtocolError,
)

MAX_CONTROL_BYTES = 4096
MAX_PENDING_CONTROLS = 16
MAX_CONTROL_DECISIONS = 256
CONTROL_CAPABILITY = "keep_waiting_v1"


@dataclass
class _Decision:
    command: WaitCommand
    outcome: dict[str, object] | None = None


class WorkerControls:
    def __init__(
        self,
        job_id: str,
        emitter: WorkerEventEmitter,
        *,
        monotonic_clock: Callable[[], float] = time.monotonic,
        queue_limit: int = MAX_PENDING_CONTROLS,
        ledger_limit: int = MAX_CONTROL_DECISIONS,
    ) -> None:
        if queue_limit <= 0 or ledger_limit <= 0:
            raise ValueError("control limits must be positive")
        self._job_id = job_id
        self._emitter = emitter
        self._clock = monotonic_clock
        self._queue_limit = queue_limit
        self._ledger_limit = ledger_limit
        self._runs: dict[str, list[WaitCommand]] = {}
        self._decisions: dict[str, _Decision] = {}
        self._closed = False
        self._lock = threading.Lock()

    def register_run(self, tool_run_id: str) -> None:
        with self._lock:
            if self._closed or tool_run_id in self._runs:
                raise RuntimeError("Cannot register a closed or repeated tool run")
            self._runs[tool_run_id] = []

    def take_commands(self, tool_run_id: str) -> list[WaitCommand]:
        with self._lock:
            commands = self._runs.get(tool_run_id, [])
            if tool_run_id in self._runs:
                self._runs[tool_run_id] = []
            return commands

    def receive_line(self, line: bytes | None) -> None:
        if line is None:
            self._reject_invalid("control_too_large")
            return
        try:
            raw = json.loads(line)
        except (ValueError, UnicodeError, RecursionError):
            self._reject_invalid("invalid_control")
            return
        identifiers = self._identifiers(raw)
        expected_keys = {"protocol_version", "type", "command_id", "job_id", "tool_run_id", "stall_episode_id"}
        if (
            not isinstance(raw, dict)
            or set(raw) != expected_keys
            or type(raw.get("protocol_version")) is not int
            or raw["protocol_version"] != PROTOCOL_VERSION
            or raw.get("type") != "job.keep_waiting"
            or len(identifiers) != 4
        ):
            self._reject_invalid("invalid_control", identifiers)
            return
        command = WaitCommand(**identifiers, received_at=self._clock())
        with self._lock:
            outcome = self._submit_command_locked(command)
        if outcome is not None:
            self._emit_result(outcome)

    def _submit_command_locked(self, command: WaitCommand) -> dict[str, object] | None:
        if self._closed:
            return None
        previous = self._decisions.get(command.command_id)
        if previous is not None:
            if (
                previous.command.job_id != command.job_id
                or previous.command.tool_run_id != command.tool_run_id
                or previous.command.stall_episode_id != command.stall_episode_id
            ):
                return self._outcome(command, False, "command_conflict")
            if previous.outcome is not None:
                return {**previous.outcome, "duplicate": True}
            # An identical pending submission shares the original decision.
            return None
        if len(self._decisions) >= self._ledger_limit:
            return self._outcome(command, False, "ledger_full")
        decision = _Decision(command)
        self._decisions[command.command_id] = decision
        code = None
        if command.job_id != self._job_id:
            code = "wrong_job"
        elif command.tool_run_id not in self._runs:
            code = "inactive_run"
        elif sum(len(commands) for commands in self._runs.values()) >= self._queue_limit:
            code = "queue_full"
        if code is not None:
            decision.outcome = self._outcome(command, False, code)
        else:
            self._runs[command.tool_run_id].append(command)
        return decision.outcome

    def complete_command(
        self, command: WaitCommand, *, accepted: bool, code: str, grants_used: int | None = None
    ) -> None:
        with self._lock:
            decision = self._decisions[command.command_id]
            if decision.command != command or decision.outcome is not None:
                raise RuntimeError("A control decision may be applied only once")
            outcome = self._outcome(command, accepted, code)
            if grants_used is not None:
                outcome["grants_used"] = grants_used
            if accepted:
                outcome["grant_seconds"] = WAIT_GRANT_SECONDS
            decision.outcome = outcome
        # Never hold a control state lock across protocol output/backpressure.
        self._emit_result(outcome)

    def emit_stall(self, payload: Mapping[str, object]) -> None:
        self._emitter.emit(WorkerEventType.TOOL_STALL, payload)

    def unregister_run(self, tool_run_id: str) -> None:
        outcomes = []
        with self._lock:
            self._runs.pop(tool_run_id, None)
            for decision in self._decisions.values():
                if decision.command.tool_run_id == tool_run_id and decision.outcome is None:
                    decision.outcome = self._outcome(decision.command, False, "inactive_run")
                    outcomes.append(decision.outcome)
        self._emit_results(outcomes)

    def close(self) -> None:
        outcomes = []
        with self._lock:
            if self._closed:
                return
            self._closed = True
            # Include dequeued but undecided controls if an unexpected runner
            # exception interrupted their application.
            for decision in self._decisions.values():
                if decision.outcome is None:
                    decision.outcome = self._outcome(decision.command, False, "job_ended")
                    outcomes.append(decision.outcome)
            self._runs.clear()
        self._emit_results(outcomes)

    def _reject_invalid(self, code: str, identifiers: Mapping[str, str] | None = None) -> None:
        with self._lock:
            if self._closed:
                return
        self._emit_result(
            {
                **{key: value for key, value in (identifiers or {}).items() if key != "job_id"},
                "accepted": False,
                "code": code,
                "duplicate": False,
            }
        )

    @staticmethod
    def _identifiers(raw: object) -> dict[str, str]:
        if not isinstance(raw, dict):
            return {}
        result: dict[str, str] = {}
        for key in ("command_id", "job_id", "tool_run_id", "stall_episode_id"):
            value = raw.get(key)
            if isinstance(value, str) and len(value) == 36:
                try:
                    result[key] = str(UUID(value))
                except ValueError:
                    # Omit malformed IDs so receive_line rejects the incomplete identity set.
                    pass
        return result

    @staticmethod
    def _outcome(command: WaitCommand, accepted: bool, code: str) -> dict[str, object]:
        return {
            "command_id": command.command_id,
            "tool_run_id": command.tool_run_id,
            "stall_episode_id": command.stall_episode_id,
            "accepted": accepted,
            "code": code,
            "duplicate": False,
        }

    def _emit_results(self, outcomes: list[dict[str, object]]) -> None:
        deadline = time.monotonic() + self._emitter.write_timeout_seconds
        for outcome in outcomes:
            self._emit_result(outcome, deadline=deadline)

    def _emit_result(self, payload: Mapping[str, object], *, deadline: float | None = None) -> None:
        try:
            self._emitter.emit(WorkerEventType.CONTROL_RESULT, payload, deadline=deadline)
        except RuntimeError:
            with self._lock:
                closed = self._closed
            if not (closed and self._emitter.terminal_emitted):
                raise


class WorkerInputReader:
    """One byte reader owns the first JobSpec and subsequent control lines.

    File descriptors use readiness waits so keeping stdin open cannot leave a
    blocked reader after job completion. StringIO is the finite test equivalent;
    arbitrary blocking TextIO implementations are intentionally unsupported.
    """

    def __init__(self, stream: TextIO, *, isolate_process_stdin: bool = False) -> None:
        if isolate_process_stdin and (stream is not sys.stdin or stream.fileno() != 0):
            raise WorkerProtocolError("invalid_input_stream", "Only the worker entrypoint may isolate process stdin.")
        self._memory = stream if isinstance(stream, io.StringIO) else None
        self._descriptor: int | None = None
        if self._memory is None:
            try:
                self._descriptor = os.dup(stream.fileno())
                if isolate_process_stdin:
                    try:
                        with open(os.devnull, "rb") as empty_input:
                            os.dup2(empty_input.fileno(), 0)
                    except OSError:
                        os.close(self._descriptor)
                        self._descriptor = None
                        raise
            except (OSError, ValueError, io.UnsupportedOperation) as error:
                raise WorkerProtocolError("invalid_input_stream", "Worker input requires a file descriptor.") from error
        self._stop = threading.Event()
        self._handler_ready = threading.Event()
        self._handler: Callable[[bytes | None], None] | None = None
        self._request: queue.Queue[bytes | WorkerProtocolError] = queue.Queue(maxsize=1)
        self._thread = threading.Thread(target=self._read, name="worker-control-reader", daemon=True)
        self._thread.start()

    def read_job_line(self, check_cancelled: Callable[[], None] | None = None) -> str:
        while True:
            if check_cancelled is not None:
                check_cancelled()
            try:
                value = self._request.get(timeout=0.05)
                break
            except queue.Empty:
                continue
        if isinstance(value, WorkerProtocolError):
            raise value
        try:
            return value.decode("utf-8")
        except UnicodeError as error:
            raise WorkerProtocolError("invalid_json", "The worker request was not valid UTF-8.") from error

    def set_control_handler(self, handler: Callable[[bytes | None], None]) -> None:
        self._handler = handler
        self._handler_ready.set()

    def close(self) -> bool:
        self._stop.set()
        self._handler_ready.set()
        self._thread.join(timeout=0.5)
        return not self._thread.is_alive()

    def _read(self) -> None:
        pending = bytearray()
        first = True
        discarding = False
        try:
            while not self._stop.is_set():
                if self._descriptor is not None:
                    if not select.select([self._descriptor], [], [], 0.05)[0]:
                        continue
                    chunk = os.read(self._descriptor, MAX_CONTROL_BYTES)
                else:
                    assert self._memory is not None
                    chunk = self._memory.read(MAX_CONTROL_BYTES).encode("utf-8")
                eof = not chunk
                for part in chunk.splitlines(keepends=True) if chunk else [b""]:
                    # JSONL uses LF only. splitlines recognizes other byte
                    # separators, so only LF terminates an actual frame.
                    complete = part.endswith(b"\n") or eof
                    if not discarding:
                        pending.extend(part)
                    limit = MAX_REQUEST_BYTES if first else MAX_CONTROL_BYTES
                    if len(pending) > limit:
                        pending.clear()
                        if first:
                            self._request.put(WorkerProtocolError("request_too_large", "Worker request too large."))
                            return
                        discarding = True
                    if not complete:
                        continue
                    if first:
                        self._request.put(bytes(pending))
                        first = False
                        while not self._handler_ready.wait(0.05):
                            if self._stop.is_set():
                                return
                    elif self._handler is not None and not self._stop.is_set() and (pending or discarding):
                        self._handler(None if discarding else bytes(pending))
                    pending.clear()
                    discarding = False
                    if self._stop.is_set():
                        return
                if eof:
                    return
        except (OSError, ValueError) as error:
            if first:
                self._request.put(WorkerProtocolError("input_failed", f"Worker input failed: {type(error).__name__}."))
        finally:
            if self._descriptor is not None:
                os.close(self._descriptor)
