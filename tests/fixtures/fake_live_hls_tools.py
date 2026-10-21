#!/usr/bin/env python3
"""Stand-ins for edge264_test, FFmpeg, and the MV-HEVC encoder, chosen by file name.

The decoder and normalizer stream bytes through, the audio decoder copies its input to
the PCM path, and the encoder records what it received and writes staged HLS output.
FAKE_LIVE_HLS_MODE selects the encoder: ``batch`` (default) publishes after reading all
input, ``streaming`` publishes a segment per input chunk, ``stall`` publishes one
segment and then keeps reading without publishing again, and ``media`` counts the
``base-`` frame markers it receives and publishes one segment per
FAKE_LIVE_HLS_FRAMES_PER_SEGMENT frames, recording the most completed segments it
ever saw waiting in staging.
"""

import json
import os
import sys
import threading
import time

from pathlib import Path


def record(name: str, data: bytes) -> None:
    directory = os.environ.get("FAKE_LIVE_HLS_RECORD_DIR")
    if directory:
        (Path(directory) / name).write_bytes(data)


def atomic_write(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


class StagedOutput:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.lines = ["#EXTM3U", "#EXT-X-VERSION:7", "#EXT-X-TARGETDURATION:3", '#EXT-X-MAP:URI="init.mp4"']
        self.count = 0
        directory.mkdir(parents=True)
        atomic_write(directory / "init.mp4", b"init")

    def segment(self, duration: float, size: int) -> None:
        self.count += 1
        name = f"segment-{self.count:05d}.m4s"
        atomic_write(self.directory / name, b"s" * size)
        self.lines.extend([f"#EXTINF:{duration:.6f},", name])
        atomic_write(self.directory / "media.m3u8", ("\n".join(self.lines) + "\n").encode())
        waiting = len(list(self.directory.glob("segment-*.m4s")))
        peak = int(os.environ.get("FAKE_LIVE_HLS_PEAK", "0"))
        if waiting > peak:
            os.environ["FAKE_LIVE_HLS_PEAK"] = str(waiting)
            record("peak-staged", str(waiting).encode())

    def end(self) -> None:
        atomic_write(self.directory / "media.m3u8", ("\n".join([*self.lines, "#EXT-X-ENDLIST"]) + "\n").encode())


def encoder(arguments: list[str]) -> int:
    options = dict(zip(arguments[::2], arguments[1::2], strict=True))
    record("encoder.pid", str(os.getpid()).encode())
    record("encoder-arguments.json", json.dumps(arguments).encode())
    mode = os.environ.get("FAKE_LIVE_HLS_MODE", "batch")
    specification = os.environ.get("FAKE_LIVE_HLS_SEGMENTS", "2.002:64,2.002:64")
    segments = [
        (float(duration), int(size)) for duration, size in (item.split(":") for item in specification.split(","))
    ]
    audio = bytearray()

    def read_audio() -> None:
        with open(options["--audio-pcm"], "rb") as handle:
            audio.extend(handle.read())

    audio_reader = threading.Thread(target=read_audio)
    audio_reader.start()
    output: StagedOutput | None = None
    video = bytearray()
    frames_per_segment = int(os.environ.get("FAKE_LIVE_HLS_FRAMES_PER_SEGMENT", "24"))
    emitted_frames = 0
    segment_size = segments[0][1]
    while chunk := os.read(0, 65_536):
        video.extend(chunk)
        if mode == "media":
            output = output or StagedOutput(Path(options["--hls-directory"]))
            while video.count(b"base-") - emitted_frames >= frames_per_segment:
                output.segment(frames_per_segment * 1001 / 24000, segment_size)
                emitted_frames += frames_per_segment
        elif mode == "streaming" and segments:
            output = output or StagedOutput(Path(options["--hls-directory"]))
            output.segment(*segments.pop(0))
        elif mode == "stall" and output is None:
            output = StagedOutput(Path(options["--hls-directory"]))
            output.segment(*segments.pop(0))
    audio_reader.join()
    record("video.bin", bytes(video))
    record("audio.bin", bytes(audio))
    if os.environ.get("FAKE_LIVE_HLS_ENCODER_FAIL"):
        sys.stderr.write("error: Synthetic encoder failure.\n")
        return 1
    if mode == "stall":
        time.sleep(60)
    output = output or StagedOutput(Path(options["--hls-directory"]))
    if mode == "media":
        remaining = video.count(b"base-") - emitted_frames
        segments = [(remaining * 1001 / 24000, segment_size)] if remaining else []
    for duration, size in segments:
        output.segment(duration, size)
    output.end()
    print(json.dumps({"frame_count": output.count, "output_mode": "hls"}))
    return 0


def pass_through() -> None:
    while chunk := os.read(0, 65_536):
        sys.stdout.buffer.write(chunk)
        sys.stdout.buffer.flush()


def ffmpeg(arguments: list[str]) -> int:
    if "s16le" not in arguments:
        pass_through()
        return 0
    data = sys.stdin.buffer.read()
    with open(arguments[-1].removeprefix("file:"), "wb") as handle:
        handle.write(data)
    return 0


def note_descriptors(name: str) -> None:
    """Records, under FAKE_TOOLS_FD_LOG, the descriptors above stdio this process inherited."""
    log = os.environ.get("FAKE_TOOLS_FD_LOG")
    if not log:
        return
    inherited = []
    for descriptor in range(3, 256):
        try:
            os.fstat(descriptor)
        except OSError:
            continue
        inherited.append(str(descriptor))
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(f"{name} - -" + " " + (",".join(inherited) or "none") + "\n")


def main() -> int:
    name = Path(sys.argv[0]).name
    note_descriptors(name)
    if name == "mv-hevc-encoder":
        return encoder(sys.argv[1:])
    if name == "ffmpeg":
        return ffmpeg(sys.argv[1:])
    if name == "edge264_test":
        pass_through()
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
