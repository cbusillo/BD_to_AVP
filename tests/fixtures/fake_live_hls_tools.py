#!/usr/bin/env python3
"""Stand-ins for edge264_test, FFmpeg, and the MV-HEVC encoder, chosen by file name.

The decoder and normalizer pass bytes through, the audio decoder copies its input to
the PCM path, and the encoder records what it received and writes staged HLS output.
"""

import json
import os
import sys
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


def encoder(arguments: list[str]) -> int:
    options = dict(zip(arguments[::2], arguments[1::2], strict=True))
    record("encoder.pid", str(os.getpid()).encode())
    record("encoder-arguments.json", json.dumps(arguments).encode())
    video = sys.stdin.buffer.read()
    with open(options["--audio-pcm"], "rb") as handle:
        audio = handle.read()
    record("video.bin", video)
    record("audio.bin", audio)
    if os.environ.get("FAKE_LIVE_HLS_ENCODER_FAIL"):
        sys.stderr.write("error: Synthetic encoder failure.\n")
        return 1
    directory = Path(options["--hls-directory"])
    directory.mkdir(parents=True)
    atomic_write(directory / "init.mp4", b"init")
    specification = os.environ.get("FAKE_LIVE_HLS_SEGMENTS", "2.002:64,2.002:64")
    segments = [
        (float(duration), int(size)) for duration, size in (item.split(":") for item in specification.split(","))
    ]
    lines = ["#EXTM3U", "#EXT-X-VERSION:7", "#EXT-X-TARGETDURATION:3", '#EXT-X-MAP:URI="init.mp4"']
    for index, (duration, size) in enumerate(segments, start=1):
        name = f"segment-{index:05d}.m4s"
        atomic_write(directory / name, b"s" * size)
        lines.extend([f"#EXTINF:{duration:.6f},", name])
        atomic_write(directory / "media.m3u8", ("\n".join(lines) + "\n").encode())
        time.sleep(float(os.environ.get("FAKE_LIVE_HLS_SEGMENT_DELAY", "0")))
    atomic_write(directory / "media.m3u8", ("\n".join([*lines, "#EXT-X-ENDLIST"]) + "\n").encode())
    print(json.dumps({"frame_count": len(segments), "output_mode": "hls"}))
    return 0


def ffmpeg(arguments: list[str]) -> int:
    data = sys.stdin.buffer.read()
    if "s16le" in arguments:
        with open(arguments[-1].removeprefix("file:"), "wb") as handle:
            handle.write(data)
    else:
        sys.stdout.buffer.write(data)
    return 0


def main() -> int:
    name = Path(sys.argv[0]).name
    if name == "mv-hevc-encoder":
        return encoder(sys.argv[1:])
    if name == "ffmpeg":
        return ffmpeg(sys.argv[1:])
    if name == "edge264_test":
        sys.stdout.buffer.write(sys.stdin.buffer.read())
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
