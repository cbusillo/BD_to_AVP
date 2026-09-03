#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import time

from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
RAINFOREST_ISO_ENV = "BD_TO_AVP_RAINFOREST_ISO"
DEFAULT_ENCODER = REPOSITORY_ROOT / "build/mv-hevc-encoder/mv-hevc-encoder"
DEFAULT_SSIF_PROBE = REPOSITORY_ROOT / "build/ssif-probe/ssif_probe"
DEFAULT_EDGE264 = REPOSITORY_ROOT / "bd_to_avp/bin/edge264_test"
Y4M_INTEGER_TAG = re.compile(rb"(?:^|\s)([WH])(\d+)(?=\s|$)")
Y4M_FRAME_RATE_TAG = re.compile(rb"(?:^|\s)F(\d+):(\d+)(?=\s|$)")
Y4M_CHROMA_TAG = re.compile(rb"(?:^|\s)C([^\s]+)(?=\s|$)")


class ThroughputProbeFailure(RuntimeError):
    pass


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a number") from error
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a finite number greater than zero")
    return parsed


def command_path(value: Path | str, label: str) -> Path:
    candidate = Path(value)
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return candidate.resolve()
    resolved = shutil.which(str(value))
    if resolved:
        return Path(resolved).resolve()
    raise ThroughputProbeFailure(f"Required {label} executable is unavailable: {value}")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def json_sha256(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def y4m_details(header: bytes) -> tuple[int, int, int, int, int]:
    if not header.startswith(b"YUV4MPEG2 "):
        raise ThroughputProbeFailure("Y4M input does not begin with a YUV4MPEG2 header")
    integers = {match.group(1): int(match.group(2)) for match in Y4M_INTEGER_TAG.finditer(header)}
    frame_rate = Y4M_FRAME_RATE_TAG.search(header)
    chroma = Y4M_CHROMA_TAG.search(header)
    if not {b"W", b"H"}.issubset(integers) or frame_rate is None or chroma is None:
        raise ThroughputProbeFailure("Y4M header must declare width, height, frame rate, and chroma")
    if not chroma.group(1).startswith(b"420"):
        raise ThroughputProbeFailure("throughput probe requires 4:2:0 Y4M input")
    width = integers[b"W"]
    height = integers[b"H"]
    frame_rate_numerator = int(frame_rate.group(1))
    frame_rate_denominator = int(frame_rate.group(2))
    if width <= 0 or height <= 0 or frame_rate_numerator <= 0 or frame_rate_denominator <= 0:
        raise ThroughputProbeFailure("Y4M dimensions and frame rate must be positive")
    frame_bytes = width * height + (width // 2) * (height // 2) * 2
    return width, height, frame_rate_numerator, frame_rate_denominator, frame_bytes


def encoder_command(args: argparse.Namespace, encoder: Path) -> list[str]:
    return [
        str(encoder),
        "--hls-directory",
        str(args.output_directory),
        "--segment-duration",
        f"{args.segment_duration:g}",
        "--bitrate-mbps",
        f"{args.bitrate_mbps:g}",
        "--expected-frames",
        str(args.max_frames),
    ]


def consume_bounded_y4m(source: Path, encoder_process: subprocess.Popen[bytes], max_frames: int) -> None:
    assert encoder_process.stdin is not None
    with source.open("rb") as handle:
        header = handle.readline()
        _, _, _, _, frame_bytes = y4m_details(header)
        encoder_process.stdin.write(header)
        for _ in range(max_frames):
            frame_header = handle.readline()
            if not frame_header:
                raise ThroughputProbeFailure(f"Y4M input ended before the requested {max_frames} frames")
            if not frame_header.startswith(b"FRAME"):
                raise ThroughputProbeFailure("Y4M input contains an invalid frame header")
            frame = handle.read(frame_bytes)
            if len(frame) != frame_bytes:
                raise ThroughputProbeFailure("Y4M input ended in an incomplete frame")
            encoder_process.stdin.write(frame_header)
            encoder_process.stdin.write(frame)
    encoder_process.stdin.close()
    encoder_process.stdin = None


def bounded_normalizer_command(ffmpeg: Path, args: argparse.Namespace) -> list[str]:
    filter_graph = (
        "[0:v]split=2[left_source][right_source];"
        f"[left_source]crop={args.rainforest_eye_width}:{args.rainforest_eye_height}:0:0[left];"
        f"[right_source]crop={args.rainforest_eye_width}:{args.rainforest_eye_height}:"
        f"{args.rainforest_eye_width}:0[right];"
        "[left][right]hstack=inputs=2,format=yuv420p[stereo]"
    )
    return [
        str(ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "yuv4mpegpipe",
        "-r",
        args.rainforest_frame_rate,
        "-i",
        "pipe:0",
        "-filter_complex",
        filter_graph,
        "-map",
        "[stereo]",
        "-frames:v",
        str(args.max_frames),
        "-f",
        "yuv4mpegpipe",
        "pipe:1",
    ]


def communicate(process: subprocess.Popen[bytes], timeout: int, label: str) -> tuple[bytes, bytes]:
    try:
        return process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        process.kill()
        process.communicate()
        raise ThroughputProbeFailure(f"{label} exceeded the {timeout}-second timeout") from error


def run_y4m_probe(args: argparse.Namespace, encoder: Path) -> dict[str, object]:
    assert args.y4m is not None
    with args.y4m.open("rb") as handle:
        _, _, frame_rate_numerator, frame_rate_denominator, _ = y4m_details(handle.readline())
    encoder_process = subprocess.Popen(
        encoder_command(args, encoder),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        consume_bounded_y4m(args.y4m, encoder_process, args.max_frames)
    except BaseException:
        encoder_process.kill()
        encoder_process.communicate()
        raise
    stdout, stderr = communicate(encoder_process, args.timeout, "MV-HEVC encoder")
    if encoder_process.returncode != 0:
        raise ThroughputProbeFailure(stderr.decode(errors="replace").strip() or "MV-HEVC encoder failed")
    return {
        "encoder_summary": json.loads(stdout),
        "frame_rate_denominator": frame_rate_denominator,
        "frame_rate_numerator": frame_rate_numerator,
        "tool_paths": {"mv_hevc_encoder": encoder},
    }


def run_rainforest_probe(args: argparse.Namespace, encoder: Path) -> dict[str, object]:
    source = os.environ.get(RAINFOREST_ISO_ENV)
    if not source:
        raise ThroughputProbeFailure(f"Set {RAINFOREST_ISO_ENV} before using --rainforest")
    source_path = Path(source)
    if not source_path.is_file():
        raise ThroughputProbeFailure(f"Rainforest ISO is unavailable: {source_path}")
    ssif_probe = command_path(args.ssif_probe, "ssif_probe")
    edge264 = command_path(args.edge264, "edge264")
    ffmpeg = command_path(args.ffmpeg, "FFmpeg")
    pair_bound = args.rainforest_pair_bound or args.max_frames + 16
    stream_process = subprocess.Popen(
        [str(ssif_probe), "stream-mvc", str(source_path), str(args.rainforest_playlist), str(pair_bound)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert stream_process.stdout is not None
    edge_process = subprocess.Popen(
        [str(edge264), "-", "-Osk"],
        stdin=stream_process.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stream_process.stdout.close()
    assert edge_process.stdout is not None
    normalizer_process = subprocess.Popen(
        bounded_normalizer_command(ffmpeg, args),
        stdin=edge_process.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    edge_process.stdout.close()
    assert normalizer_process.stdout is not None
    encoder_process = subprocess.Popen(
        encoder_command(args, encoder),
        stdin=normalizer_process.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    normalizer_process.stdout.close()
    encoder_stdout, encoder_stderr = communicate(encoder_process, args.timeout, "MV-HEVC encoder")
    _, normalizer_stderr = communicate(normalizer_process, args.timeout, "FFmpeg normalizer")
    _, edge_stderr = communicate(edge_process, args.timeout, "edge264")
    _, stream_stderr = communicate(stream_process, args.timeout, "ssif_probe")
    if encoder_process.returncode != 0:
        raise ThroughputProbeFailure(encoder_stderr.decode(errors="replace").strip() or "MV-HEVC encoder failed")
    if normalizer_process.returncode != 0:
        raise ThroughputProbeFailure(normalizer_stderr.decode(errors="replace").strip() or "FFmpeg normalizer failed")
    if edge_process.returncode not in {0, -13}:
        raise ThroughputProbeFailure(edge_stderr.decode(errors="replace").strip() or "edge264 failed")
    if stream_process.returncode not in {0, -13}:
        raise ThroughputProbeFailure(stream_stderr.decode(errors="replace").strip() or "ssif_probe failed")
    frame_rate_numerator, frame_rate_denominator = (int(value) for value in args.rainforest_frame_rate.split("/"))
    return {
        "encoder_summary": json.loads(encoder_stdout),
        "frame_rate_denominator": frame_rate_denominator,
        "frame_rate_numerator": frame_rate_numerator,
        "tool_paths": {
            "edge264": edge264,
            "ffmpeg": ffmpeg,
            "mv_hevc_encoder": encoder,
            "ssif_probe": ssif_probe,
        },
    }


def cpu_brand() -> str:
    if platform.system() == "Darwin":
        result = subprocess.run(
            ["sysctl", "-n", "machdep.cpu.brand_string"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    return platform.processor() or "unknown"


def storage_details(path: Path) -> dict[str, object]:
    status = os.statvfs(path)
    result = subprocess.run(
        ["df", "-P", str(path)],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    fields = result.stdout.splitlines()[-1].split() if result.returncode == 0 and result.stdout.splitlines() else []
    return {
        "available_bytes": status.f_bavail * status.f_frsize,
        "block_size_bytes": status.f_frsize,
        "filesystem_device": fields[0] if fields else "unknown",
        "mount_point": fields[-1] if fields else "unknown",
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Measure bounded segmented MV-HEVC HLS throughput and emit one JSON record."
    )
    source = result.add_mutually_exclusive_group(required=True)
    source.add_argument("--y4m", type=Path, help="Side-by-side 8-bit 4:2:0 Y4M input.")
    source.add_argument(
        "--rainforest",
        action="store_true",
        help=f"Use the bounded ssif_probe -> edge264 -> normalizer chain from {RAINFOREST_ISO_ENV}.",
    )
    result.add_argument("--output-directory", type=Path, required=True, help="New HLS output directory.")
    result.add_argument("--encoder", type=Path, default=DEFAULT_ENCODER, help="Segmented MV-HEVC encoder executable.")
    result.add_argument("--max-frames", type=positive_int, default=240, help="Exact frame count to encode (default: 240).")
    result.add_argument("--segment-duration", type=positive_float, default=2.0, help="HLS segment duration in seconds.")
    result.add_argument("--bitrate-mbps", type=positive_float, default=20.0, help="MV-HEVC average bitrate in Mbps.")
    result.add_argument("--timeout", type=positive_int, default=300, help="Per-process timeout in seconds.")
    result.add_argument("--ssif-probe", type=Path, default=DEFAULT_SSIF_PROBE, help="ssif_probe executable for --rainforest.")
    result.add_argument("--edge264", type=Path, default=DEFAULT_EDGE264, help="edge264 executable for --rainforest.")
    result.add_argument("--ffmpeg", default="ffmpeg", help="FFmpeg executable for --rainforest.")
    result.add_argument("--rainforest-playlist", type=positive_int, default=1005, help="Rainforest playlist number.")
    result.add_argument("--rainforest-pair-bound", type=positive_int, help="MVC pair bound; defaults to max frames plus 16.")
    result.add_argument("--rainforest-eye-width", type=positive_int, default=1920, help="Rainforest eye width.")
    result.add_argument("--rainforest-eye-height", type=positive_int, default=1080, help="Rainforest eye height.")
    result.add_argument("--rainforest-frame-rate", default="24000/1001", help="Rainforest frame rate numerator/denominator.")
    return result


def main() -> int:
    args = parser().parse_args()
    if not re.fullmatch(r"[1-9]\d*/[1-9]\d*", args.rainforest_frame_rate):
        raise ThroughputProbeFailure("--rainforest-frame-rate must be NUMERATOR/DENOMINATOR")
    if args.output_directory.exists():
        raise ThroughputProbeFailure("--output-directory must not already exist")
    if args.y4m is not None and not args.y4m.is_file():
        raise ThroughputProbeFailure(f"Y4M input is unavailable: {args.y4m}")
    encoder = command_path(args.encoder, "MV-HEVC encoder")
    args.output_directory.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    result = run_rainforest_probe(args, encoder) if args.rainforest else run_y4m_probe(args, encoder)
    elapsed_seconds = time.monotonic() - started
    summary = result["encoder_summary"]
    assert isinstance(summary, dict)
    frame_rate_numerator = result["frame_rate_numerator"]
    frame_rate_denominator = result["frame_rate_denominator"]
    if not isinstance(frame_rate_numerator, int) or not isinstance(frame_rate_denominator, int):
        raise ThroughputProbeFailure("throughput probe recorded an invalid frame rate")
    frame_count = summary.get("frame_count")
    if not isinstance(frame_count, int) or frame_count <= 0:
        raise ThroughputProbeFailure("encoder summary did not report a positive frame count")
    media_duration_seconds = frame_count * frame_rate_denominator / frame_rate_numerator
    tool_paths = result["tool_paths"]
    assert isinstance(tool_paths, dict)
    named_tool_paths: dict[str, Path] = {}
    for name, path in tool_paths.items():
        if not isinstance(name, str) or not isinstance(path, Path):
            raise ThroughputProbeFailure("throughput probe recorded an invalid tool path")
        named_tool_paths[name] = path
    tool_hashes = {name: sha256(path) for name, path in sorted(named_tool_paths.items())}
    hardware = {
        "cpu_brand": cpu_brand(),
        "machine": platform.machine(),
        "operating_system": platform.system(),
        "operating_system_version": platform.version(),
    }
    storage = storage_details(args.output_directory.parent)
    payload = {
        "encoder_summary": summary,
        "hardware": hardware,
        "hardware_fingerprint_sha256": json_sha256(hardware),
        "media_duration_seconds": media_duration_seconds,
        "realtime_ratio": media_duration_seconds / elapsed_seconds,
        "schema_version": 1,
        "source_mode": "rainforest" if args.rainforest else "y4m",
        "storage": storage,
        "storage_fingerprint_sha256": json_sha256(storage),
        "tool_hashes": tool_hashes,
        "wall_elapsed_seconds": elapsed_seconds,
    }
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ThroughputProbeFailure as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1) from error
