"""Check decoded pixels, rather than generator text or encoder byte identity."""

import json
import os
import struct
import subprocess

from pathlib import Path

import numpy as np
import pytest

from scripts.create_spatial_audio_validation_fixtures import (
    FIXTURE_CASES,
    FRAME_RATE,
    config,
    WarningRecorder,
    validate_warnings,
    VIDEO_HEIGHT,
    VIDEO_WIDTH,
    spatial_eye_filter,
)


FFMPEG = str(config.FFMPEG_PATH)
FFPROBE = str(config.FFPROBE_PATH)


@pytest.fixture(autouse=True)
def require_fixture_decoders() -> None:
    missing = [tool for tool in (FFMPEG, FFPROBE) if not Path(tool).is_file() or not os.access(tool, os.X_OK)]
    if missing:
        message = (
            "FFmpeg and FFprobe are needed to decode fixtures. Install FFmpeg as described in README.md, "
            "or set BD_TO_AVP_FFMPEG_PATH and BD_TO_AVP_FFPROBE_PATH to the installed tools."
        )
        if os.environ.get("CI"):
            pytest.fail(message)
        pytest.skip(message)


ROOT = Path(__file__).resolve().parents[1]


def decode_frame(arguments: list[str], width: int, height: int) -> np.ndarray:
    raw = subprocess.check_output(
        [FFMPEG, "-v", "error", *arguments, "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        timeout=30,
    )
    return np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 3)


def marker_centers(image: np.ndarray) -> dict[str, float]:
    # Exclude the eye-label panel's colored border, retaining all depth markers.
    h, w = image.shape[:2]
    pixels = image[int(h * 0.48) : int(h * 0.72), int(w * 0.08) : int(w * 0.92)].astype(int)
    red, green, blue = pixels.transpose(2, 0, 1)
    masks = {
        "blue": (blue > red * 1.5) & (blue > green * 1.5) & (blue > 120),
        "green": (green > red * 1.5) & (green > blue * 1.5) & (green > 100),
        "red": (red > green * 1.5) & (red > blue * 1.5) & (red > 120),
    }
    centers = {}
    for color, mask in masks.items():
        _, xs = np.nonzero(mask)
        assert len(xs) > 100, f"Missing {color} marker"
        centers[color] = float(xs.mean())
    return centers


def assert_depth_order(left: dict[str, float], right: dict[str, float]) -> None:
    # x_left - x_right: negative is behind, zero is screen, positive is in front.
    assert left["blue"] < right["blue"] - 2
    assert abs(left["green"] - right["green"]) < 1
    assert left["red"] > right["red"] + 2


@pytest.mark.parametrize("layout", ["SBS", "OU"])
def test_bundled_packed_checks_have_blue_behind_red_in_front(layout: str) -> None:
    path = ROOT / "macos" / "BDToAVPPlayer" / "Resources" / f"Stereo-Check-{layout}.mov"
    document = json.loads(subprocess.check_output([FFPROBE, "-v", "error", "-show_streams", "-of", "json", path]))
    stream = next(s for s in document["streams"] if s["codec_type"] == "video")
    width, height = stream["width"], stream["height"]
    image = decode_frame(["-ss", "1", "-i", str(path)], width, height)
    left, right = np.split(image, 2, axis=1) if layout == "SBS" else np.split(image, 2, axis=0)
    assert_depth_order(marker_centers(left), marker_centers(right))


def audio_eye(left_eye: bool, timestamp: float) -> np.ndarray:
    return decode_frame(
        [
            "-f",
            "lavfi",
            "-i",
            f"color=c=0x080c12:size={VIDEO_WIDTH}x{VIDEO_HEIGHT}:rate={FRAME_RATE}",
            "-ss",
            str(timestamp),
            "-vf",
            spatial_eye_filter(left_eye=left_eye),
        ],
        VIDEO_WIDTH,
        VIDEO_HEIGHT,
    )


def test_audio_visuals_have_three_depth_planes_and_synchronized_flash() -> None:
    images = [audio_eye(eye, 0.5) for eye in (True, False)]
    centers = []
    for image in images:
        # Audio markers occupy different vertical positions; stack their bands.
        bands = [
            image[int(VIDEO_HEIGHT * a) : int(VIDEO_HEIGHT * b)] for a, b in ((0.17, 0.38), (0.43, 0.64), (0.69, 0.9))
        ]
        red, green, blue = np.concatenate(bands).astype(int).transpose(2, 0, 1)
        masks = {
            "blue": (blue > red * 2) & (blue > green * 1.5) & (blue > 120),
            "green": (green > red * 2) & (green > blue * 1.5) & (green > 100),
            "red": (red > blue * 2) & (red > green * 1.5) & (red > 120),
        }
        centers.append({color: float(np.nonzero(mask)[1].mean()) for color, mask in masks.items()})
    assert_depth_order(*centers)
    for eye, image in zip((True, False), images, strict=True):
        flash = audio_eye(eye, 0.05)
        assert flash[10, 10].mean() > image[10, 10].mean() + 80


def test_audio_warning_gate_detects_missing_fallback_and_unexpected_warning() -> None:
    fallback = next(
        case for case in FIXTURE_CASES if case.expected_action == "convert_aac" and case.mode.value == "automatic"
    )
    streams = [{"index": 0, "codec_name": "ac3", "channel_layout": "stereo", "channels": 2}]
    recorder = WarningRecorder()
    with pytest.raises(RuntimeError, match="expected warnings"):
        validate_warnings(fallback, streams, recorder)
    recorder.warning("fallback", code="audio_automatic_fallback_to_aac")
    validate_warnings(fallback, streams, recorder)
    recorder.warning("unexpected", code="unexpected_audio_warning")
    with pytest.raises(RuntimeError, match="expected warnings"):
        validate_warnings(fallback, streams, recorder)


def test_decoded_audio_comparison_preserves_onset_while_allowing_tail_padding(tmp_path: Path) -> None:
    from scripts.create_spatial_audio_validation_fixtures import decoded_audio_fingerprint

    original, shorter, delayed = (tmp_path / name for name in ("original.wav", "shorter.wav", "delayed.wav"))
    subprocess.run(
        [
            FFMPEG,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:sample_rate=48000:duration=1",
            "-af",
            "volume=enable='gte(t,0.12)':volume=0",
            "-c:a",
            "pcm_s24le",
            str(original),
        ],
        check=True,
        timeout=30,
    )
    for path, options in ((shorter, ["-t", "0.99"]), (delayed, ["-af", "adelay=21:all=1"])):
        subprocess.run(
            [FFMPEG, "-v", "error", "-i", str(original), *options, "-c:a", "pcm_s24le", str(path)],
            check=True,
            timeout=30,
        )
    fingerprint = decoded_audio_fingerprint(original, 0, duration_seconds=0.95)
    assert fingerprint == decoded_audio_fingerprint(shorter, 0, duration_seconds=0.95)
    assert fingerprint != decoded_audio_fingerprint(delayed, 0, duration_seconds=0.95)


def test_final_spatial_depth_gate_rejects_swapped_views(tmp_path: Path) -> None:
    from scripts.create_spatial_audio_validation_fixtures import validate_spatial_depth

    paths = [tmp_path / name for name in ("left.mov", "right.mov")]
    for path, left_eye in zip(paths, (True, False), strict=True):
        subprocess.run(
            [
                FFMPEG,
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"color=c=0x080c12:size={VIDEO_WIDTH}x{VIDEO_HEIGHT}:rate={FRAME_RATE}",
                "-t",
                "1",
                "-vf",
                spatial_eye_filter(left_eye=left_eye),
                "-c:v",
                "mpeg4",
                str(path),
            ],
            check=True,
            timeout=30,
        )
    validate_spatial_depth(*paths)
    with pytest.raises(RuntimeError, match="incorrect depth order"):
        validate_spatial_depth(*reversed(paths))


def test_decoded_audio_comparison_detects_changed_aac_priming_edit(tmp_path: Path) -> None:
    from scripts.create_spatial_audio_validation_fixtures import decoded_audio_fingerprint

    original, shifted = (tmp_path / name for name in ("original.m4a", "shifted.m4a"))
    subprocess.run(
        [
            FFMPEG,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:sample_rate=48000:duration=1",
            "-c:a",
            "aac",
            str(original),
        ],
        check=True,
        timeout=30,
    )
    probe = json.loads(subprocess.check_output([FFPROBE, "-v", "error", "-show_streams", "-of", "json", original]))
    sample_rate = int(probe["streams"][0]["sample_rate"])
    data = bytearray(original.read_bytes())
    offset = data.index(b"elst")
    version = data[offset + 4]
    width, entry_size, integer_format = (8, 20, ">q") if version == 1 else (4, 12, ">i")
    entry_count = struct.unpack_from(">I", data, offset + 8)[0]
    for index in range(entry_count):
        position = offset + 12 + index * entry_size + width
        media_time = struct.unpack_from(integer_format, data, position)[0]
        if media_time >= 0:
            struct.pack_into(integer_format, data, position, media_time + round(sample_rate * 0.021))
            break
    else:
        pytest.fail("Generated AAC has no media edit to exercise priming")
    shifted.write_bytes(data)
    assert decoded_audio_fingerprint(original, 0, duration_seconds=0.95) != decoded_audio_fingerprint(
        shifted, 0, duration_seconds=0.95
    )
