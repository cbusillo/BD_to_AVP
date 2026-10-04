"""Check decoded pixels, rather than generator text or encoder byte identity."""

import json
import shutil
import subprocess

from pathlib import Path

import numpy as np
import pytest

from scripts.create_spatial_audio_validation_fixtures import (
    FIXTURE_CASES,
    FRAME_RATE,
    WarningRecorder,
    validate_warnings,
    VIDEO_HEIGHT,
    VIDEO_WIDTH,
    spatial_eye_filter,
)


FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
pytestmark = pytest.mark.skipif(not FFMPEG or not FFPROBE, reason="FFmpeg and FFprobe are needed to decode fixtures")
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
