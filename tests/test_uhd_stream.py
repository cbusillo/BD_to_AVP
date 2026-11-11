from __future__ import annotations

import sys
import threading
from collections.abc import Generator
from dataclasses import replace
from pathlib import Path

import pytest

from bd_to_avp.modules.uhd_stream import FrameStream, binary_process_spec, encode_frame_stream
from bd_to_avp.process_runner import ProcessCancelled, ProcessExecutionError


def python_spec(source: str):
    return binary_process_spec((sys.executable, "-c", source), "python")


def test_stream_preserves_frame_boundaries_and_child_failure() -> None:
    with FrameStream(
        (python_spec("import os; os.write(1, b'abcde' * 3)"),), 5, run_context=None, cancellation_event=None
    ) as stream:
        assert [stream.read(), stream.read(), stream.read(), stream.read()] == [b"abcde"] * 3 + [None]
    with FrameStream((python_spec("raise SystemExit(9)"),), 5, run_context=None, cancellation_event=None) as stream:
        with pytest.raises(ProcessExecutionError):
            stream.read()


def test_stream_rejects_truncated_frames_and_stops_a_blocked_decoder_on_cancel() -> None:
    with FrameStream(
        (python_spec("import os; os.write(1, b'ab')"),), 5, run_context=None, cancellation_event=None
    ) as stream:
        with pytest.raises(RuntimeError, match="incomplete frame"):
            stream.read()
    cancelled = threading.Event()
    with FrameStream(
        (python_spec("import time; time.sleep(30)"),), 5, run_context=None, cancellation_event=cancelled
    ) as stream:
        cancelled.set()
        with pytest.raises(ProcessCancelled):
            stream.read()
    assert not stream._thread.is_alive()


def test_encoder_stream_reports_producer_failure_and_joins_writer(tmp_path: Path) -> None:
    output = tmp_path / "stream.bin"
    spec = python_spec("import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())")

    def frames(_stop: threading.Event) -> Generator[bytes, None, None]:
        yield b"header\n"
        yield b"picture\n"

    with output.open("wb") as destination:
        encode_frame_stream(replace(spec, stdout=destination), frames, run_context=None, cancellation_event=None)
    assert output.read_bytes() == b"header\npicture\n"

    def failed_frames(_stop: threading.Event) -> Generator[bytes, None, None]:
        yield b"header\n"
        raise ValueError("right-eye decode failed")

    with pytest.raises(ValueError, match="right-eye decode failed"):
        encode_frame_stream(spec, failed_frames, run_context=None, cancellation_event=None)


def test_encoder_refusal_cancels_the_producer() -> None:
    stopped = threading.Event()

    def frames(stop: threading.Event) -> Generator[bytes, None, None]:
        try:
            while not stop.is_set():
                yield b"frame" * 100000
        finally:
            stopped.set()

    with pytest.raises(ProcessExecutionError):
        encode_frame_stream(python_spec("raise SystemExit(7)"), frames, run_context=None, cancellation_event=None)
    assert stopped.is_set()
