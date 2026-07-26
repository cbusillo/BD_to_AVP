from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
import json
import shutil
import subprocess
import tempfile

from pathlib import Path


class AppleMediaFailure(RuntimeError):
    pass


MEDIA_STREAM_TYPES = frozenset({"video", "audio", "subtitle"})
PASSTHROUGH_PRESET = "PresetPassthrough"


@dataclass(frozen=True)
class AppleMediaConversionReport:
    preset: str
    source_streams: Counter[str]
    output_streams: Counter[str]

    @property
    def dropped_streams(self) -> dict[str, int]:
        return {
            stream_type: self.source_streams[stream_type] - self.output_streams[stream_type]
            for stream_type in MEDIA_STREAM_TYPES
            if self.output_streams[stream_type] < self.source_streams[stream_type]
        }


def run(command: list[str | Path]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(item) for item in command],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def find_ffprobe() -> str:
    ffprobe_path = shutil.which("ffprobe")
    bundled_ffprobe = Path(__file__).resolve().parents[1] / "bd_to_avp" / "bin" / "ffprobe"
    if ffprobe_path is None and bundled_ffprobe.is_file():
        ffprobe_path = str(bundled_ffprobe)
    if ffprobe_path is None:
        raise AppleMediaFailure("Apple media compatibility check requires ffprobe")
    return ffprobe_path


def inspect_apple_media_conversion(
    media_path: Path,
    output_path: Path,
    *,
    preset: str = PASSTHROUGH_PRESET,
) -> AppleMediaConversionReport:
    if not media_path.is_file():
        raise AppleMediaFailure(f"Missing media file: {media_path}")
    avconvert_path = shutil.which("avconvert")
    if avconvert_path is None:
        raise AppleMediaFailure("Apple media compatibility check requires avconvert on macOS")
    ffprobe_path = find_ffprobe()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        run(
            [
                avconvert_path,
                "--source",
                media_path,
                "--preset",
                preset,
                "--output",
                output_path,
                "--replace",
                "--disableFastStart",
            ]
        )
    except subprocess.CalledProcessError as error:
        output = error.stdout or ""
        raise AppleMediaFailure(
            f"Apple media stack could not open/convert {media_path} with {preset}:\n{output.strip()}"
        ) from error
    return AppleMediaConversionReport(
        preset=preset,
        source_streams=probe_stream_counts(ffprobe_path, media_path),
        output_streams=probe_stream_counts(ffprobe_path, output_path),
    )


def verify_apple_media_compatible(media_path: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="bd-to-avp-avconvert-") as temp_dir:
        report = inspect_apple_media_conversion(
            media_path,
            Path(temp_dir) / "passthrough.mov",
        )
        if report.dropped_streams:
            detail = ", ".join(
                f"{count} {stream_type} track{'s' if count != 1 else ''}"
                for stream_type, count in sorted(report.dropped_streams.items())
            )
            raise AppleMediaFailure(f"Apple media stack dropped {detail} while opening {media_path}")


def probe_stream_counts(ffprobe_path: str, media_path: Path) -> Counter[str]:
    try:
        completed = run(
            [
                ffprobe_path,
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "json",
                media_path,
            ]
        )
        payload = json.loads(completed.stdout)
    except (subprocess.CalledProcessError, json.JSONDecodeError) as error:
        raise AppleMediaFailure(f"Could not inspect media streams in {media_path}") from error
    streams = payload.get("streams") if isinstance(payload, dict) else None
    if not isinstance(streams, list):
        raise AppleMediaFailure(f"ffprobe returned no stream list for {media_path}")
    return Counter(
        stream["codec_type"]
        for stream in streams
        if isinstance(stream, dict) and stream.get("codec_type") in MEDIA_STREAM_TYPES
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify media with Apple's avconvert passthrough before QuickTime/AVP manual testing."
    )
    parser.add_argument("media", nargs="+", type=Path, help="MOV/MP4 file(s) to verify")
    args = parser.parse_args()

    try:
        for media_path in args.media:
            verify_apple_media_compatible(media_path)
            print(f"Apple media compatibility passed: {media_path}")
    except AppleMediaFailure as error:
        print(f"Apple media compatibility failed: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
