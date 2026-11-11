from __future__ import annotations

import os
import queue
import select
import threading
import time
from collections.abc import Generator
from dataclasses import replace
from pathlib import Path
from types import TracebackType
from typing import Callable

from bd_to_avp.process_runner import (
    CaptureOverflowPolicy,
    ChildProcessRunner,
    ProcessCancelled,
    ProcessPipelineRunner,
    ProcessPipelineStage,
    ProcessSpec,
    ProcessStream,
)
from bd_to_avp.runtime import RunContext

STREAM_TIMEOUT_SECONDS = 120


def binary_process_spec(argv: tuple[str | Path, ...], tool_id: str) -> ProcessSpec:
    return ProcessSpec(
        argv=tuple(str(argument) for argument in argv),
        tool_id=tool_id,
        display_name=f"Read {tool_id} video frames",
        env=os.environ.copy(),
        merge_stderr=False,
        capture_limit_bytes=1024,
        tail_limit_bytes=1024,
        capture_overflow=CaptureOverflowPolicy.TRUNCATE,
    )


class FrameStream:
    def __init__(
        self,
        specs: tuple[ProcessSpec, ...],
        frame_bytes: int,
        *,
        run_context: RunContext | None,
        cancellation_event: threading.Event | None,
        line_handler: Callable[[ProcessStream, str], object] | None = None,
        timeout_seconds: float = STREAM_TIMEOUT_SECONDS,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._specs = specs
        self._frame_bytes = frame_bytes
        self._run_context = run_context
        self._cancellation = cancellation_event
        self._line_handler = line_handler
        self._stop = threading.Event()
        self._done = threading.Event()
        self._frames: queue.Queue[bytes] = queue.Queue(maxsize=2)
        self._error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, name="uhd-frame-reader")

    def is_set(self) -> bool:
        return (
            self._stop.is_set()
            or (self._cancellation is not None and self._cancellation.is_set())
            or (self._run_context is not None and self._run_context.cancellation.is_cancelled)
        )

    def _check_cancelled(self) -> None:
        if self.is_set():
            raise ProcessCancelled("The 4K HDR conversion was cancelled.")

    def _run(self) -> None:
        read_descriptor, write_descriptor = os.pipe()
        with (
            os.fdopen(read_descriptor, "rb", buffering=0) as reader,
            os.fdopen(write_descriptor, "wb", buffering=0) as writer,
        ):

            def decode() -> None:
                try:
                    stages = tuple(
                        ProcessPipelineStage(
                            replace(spec, stdout=writer) if index == len(self._specs) - 1 else spec,
                            line_handler=self._line_handler,
                        )
                        for index, spec in enumerate(self._specs)
                    )
                    if len(stages) == 1:
                        ChildProcessRunner().run(
                            stages[0].spec,
                            run_context=self._run_context,
                            cancellation_event=self,
                            line_handler=self._line_handler,
                        )
                    else:
                        ProcessPipelineRunner().run(stages, run_context=self._run_context, cancellation_event=self)
                except BaseException as decode_error:
                    self._error = decode_error
                finally:
                    writer.close()

            decoder = threading.Thread(target=decode, name="uhd-decoder-processes")
            decoder.start()
            try:
                pending = bytearray()
                while True:
                    self._check_cancelled()
                    chunk = reader.read(min(1024 * 1024, self._frame_bytes - len(pending)))
                    if not chunk:
                        if pending:
                            raise RuntimeError("A video decoder returned an incomplete frame.")
                        break
                    pending.extend(chunk)
                    if len(pending) < self._frame_bytes:
                        continue
                    frame = bytes(pending)
                    pending.clear()
                    while True:
                        self._check_cancelled()
                        try:
                            self._frames.put(frame, timeout=0.1)
                            break
                        except queue.Full:
                            continue
            except BaseException as error:
                if self._error is None:
                    self._error = error
                self._stop.set()
            finally:
                decoder.join()
                self._done.set()

    def __enter__(self) -> FrameStream:
        self._thread.start()
        return self

    def read(self) -> bytes | None:
        deadline = time.monotonic() + self._timeout_seconds
        while True:
            self._check_cancelled()
            try:
                return self._frames.get(timeout=0.1)
            except queue.Empty:
                if self._done.is_set():
                    if self._error is not None:
                        raise self._error from None
                    return None
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        "The video decoder stopped sending pictures. No 4K HDR movie was completed."
                    ) from None

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._stop.set()
        self._thread.join()


def encode_frame_stream(
    encoder_spec: ProcessSpec,
    frames: Callable[[threading.Event], Generator[bytes, None, None]],
    *,
    run_context: RunContext | None,
    cancellation_event: threading.Event | None,
) -> None:
    read_descriptor, write_descriptor = os.pipe()
    failures: list[BaseException] = []
    stop = threading.Event()
    os.set_blocking(write_descriptor, False)

    def write(payload: bytes) -> None:
        remaining = memoryview(payload)
        deadline = time.monotonic() + STREAM_TIMEOUT_SECONDS
        while remaining:
            if stop.is_set() or (cancellation_event is not None and cancellation_event.is_set()):
                raise ProcessCancelled("The 4K HDR conversion was cancelled.")
            if run_context is not None and run_context.cancellation.is_cancelled:
                raise ProcessCancelled("The 4K HDR conversion was cancelled.")
            try:
                written = os.write(write_descriptor, remaining)
                remaining = remaining[written:]
                deadline = time.monotonic() + STREAM_TIMEOUT_SECONDS
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("The HDR encoder stopped reading pictures.") from None
                select.select([], [write_descriptor], [], 0.1)

    def produce() -> None:
        iterator = frames(stop)
        try:
            for payload in iterator:
                write(payload)
        except BaseException as error:
            failures.append(error)
            stop.set()
        finally:
            iterator.close()
            os.close(write_descriptor)

    producer = threading.Thread(target=produce, name="uhd-stereo-writer")
    with os.fdopen(read_descriptor, "rb", buffering=0) as reader:
        producer.start()
        try:
            ChildProcessRunner().run(
                replace(encoder_spec, stdin=reader), run_context=run_context, cancellation_event=stop
            )
        except BaseException as encoder_error:
            stop.set()
            producer.join()
            source_failure = next(
                (error for error in failures if not isinstance(error, (BrokenPipeError, ProcessCancelled))), None
            )
            if source_failure is not None:
                raise source_failure from encoder_error
            raise
        finally:
            stop.set()
            producer.join()
    if failures:
        raise failures[0]
