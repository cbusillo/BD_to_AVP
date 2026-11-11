from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any

import numpy as np
from numpy.typing import NDArray

from bd_to_avp.modules.video_route import VideoRoutePreflightError

FloatImage = NDArray[np.float32]
UHD_SIZE = (3840, 2160)
SAMPLE_SIZE = (960, 540)
MAX_COLOR_RMS_CODES = 16.0
MAX_COLOR_P99_CODES = 48.0
MAX_REGION_RMS_CODES = 24.0
MAX_FRAME_RMS_CODES = 24.0


@dataclass(frozen=True)
class HDRReferenceInfo:
    frame_rate: Fraction
    duration_seconds: float
    mastering_display: str | None
    max_cll: str | None
    frame_count: int = 0
    start_seconds: float = 0


def read_hdr_reference_info(probe: dict[str, Any]) -> HDRReferenceInfo:
    streams = [stream for stream in probe.get("streams", []) if stream.get("codec_type") == "video"]
    if not streams:
        raise VideoRoutePreflightError("The 4K copy has no video track.")
    stream = dict(streams[0])
    frames = probe.get("frames", [])
    if frames:
        for key in ("color_transfer", "color_primaries", "color_space", "color_range", "pix_fmt"):
            decoded = frames[0].get(key)
            declared = stream.get(key)
            if decoded is not None and declared not in {None, "unknown", "unspecified", decoded}:
                raise VideoRoutePreflightError(
                    "The 4K copy has conflicting HDR color tags in its container and picture."
                )
            if decoded is not None:
                stream[key] = decoded
    if (stream.get("width"), stream.get("height")) != UHD_SIZE:
        raise VideoRoutePreflightError("The matching copy must be 3840 by 2160 pixels.")
    if stream.get("pix_fmt") not in {"yuv420p10le", "p010le"}:
        raise VideoRoutePreflightError("The 4K copy must contain 10-bit HDR10 video. An 8-bit copy cannot be used.")
    if (
        stream.get("color_transfer") != "smpte2084"
        or stream.get("color_primaries") != "bt2020"
        or stream.get("color_space") != "bt2020nc"
        or stream.get("color_range", "tv") != "tv"
    ):
        raise VideoRoutePreflightError(
            "The 4K copy must be HDR10 with PQ and BT.2020 color. An SDR copy cannot be used."
        )
    try:
        frame_rate = Fraction(stream["avg_frame_rate"])
        nominal_rate = Fraction(stream["r_frame_rate"])
        duration = float(stream.get("duration") or probe["format"]["duration"])
        if not np.isfinite(duration) or duration <= 0 or frame_rate <= 0 or nominal_rate != frame_rate:
            raise ValueError
    except (KeyError, ValueError, ZeroDivisionError, TypeError) as error:
        raise VideoRoutePreflightError("The 4K copy needs a known, constant frame rate and runtime.") from error
    mastering_display = None
    max_cll = None
    try:
        side_data = list(stream.get("side_data_list", []))
        if frames:
            side_data.extend(frames[0].get("side_data_list", []))
        for metadata in side_data:
            if metadata.get("side_data_type") == "Mastering display metadata":
                coordinates = [
                    round(Fraction(metadata[key]) * 50000)
                    for key in (
                        "green_x",
                        "green_y",
                        "blue_x",
                        "blue_y",
                        "red_x",
                        "red_y",
                        "white_point_x",
                        "white_point_y",
                    )
                ]
                maximum = round(Fraction(metadata["max_luminance"]) * 10000)
                minimum = round(Fraction(metadata["min_luminance"]) * 10000)
                green_x, green_y, blue_x, blue_y, red_x, red_y, white_x, white_y = coordinates
                if any(value < 0 or value > 50000 for value in coordinates) or not 0 <= minimum < maximum <= 100000000:
                    raise ValueError
                mastering_display = (
                    f"G({green_x},{green_y})B({blue_x},{blue_y})R({red_x},{red_y})"
                    f"WP({white_x},{white_y})L({maximum},{minimum})"
                )
            if metadata.get("side_data_type") == "Content light level metadata":
                maximum_content = int(metadata["max_content"])
                maximum_average = int(metadata["max_average"])
                if not 0 <= maximum_average <= maximum_content <= 65535:
                    raise ValueError
                max_cll = f"{maximum_content},{maximum_average}"
    except (KeyError, ValueError, ZeroDivisionError, TypeError) as error:
        raise VideoRoutePreflightError("The 4K copy contains invalid HDR mastering metadata.") from error
    first_frame_time = (frames[0] if frames else {}).get("best_effort_timestamp_time", stream.get("start_time", "0"))
    try:
        if not isinstance(first_frame_time, (str, int, float)):
            raise ValueError
        start = float(first_frame_time)
        if not np.isfinite(start):
            raise ValueError
    except (ValueError, TypeError) as error:
        raise VideoRoutePreflightError("The 4K copy has no usable starting picture time.") from error
    return HDRReferenceInfo(frame_rate, duration, mastering_display, max_cll, start_seconds=start)


def color_features(pixels: FloatImage) -> FloatImage:
    pixels = pixels.reshape(-1, 3)
    red, green, blue = (pixels[:, index : index + 1] for index in range(3))
    return np.concatenate(
        (pixels, pixels**2, pixels**3, red * green, green * blue, red * blue, np.ones_like(red)), axis=1
    )


@dataclass(frozen=True)
class TitleColorMap:
    coefficients: FloatImage
    validation: dict[str, Any]

    def apply(self, image: FloatImage) -> FloatImage:
        output = np.empty_like(image, dtype=np.float32)
        for start in range(0, image.shape[0], 32):
            tile = image[start : start + 32]
            output[start : start + 32] = (color_features(tile) @ self.coefficients).reshape(tile.shape)
        return np.clip(output, 0, 1)


def color_error_report(predicted: FloatImage, target: FloatImage) -> dict[str, float | int]:
    errors = np.abs(predicted - target) * 1023
    return {
        "pixels": len(target),
        "rms_codes": float(np.sqrt(np.mean(errors**2))),
        "p99_codes": float(np.quantile(errors, 0.99)),
    }


def fit_title_color_map(
    training: list[tuple[FloatImage, FloatImage]], validation: list[tuple[FloatImage, FloatImage]]
) -> TitleColorMap:
    if not training or not validation:
        raise VideoRoutePreflightError("There are too few matching pictures to check the HDR color fit.")
    training_source = np.concatenate([pair[0] for pair in training])
    training_target = np.concatenate([pair[1] for pair in training])
    coefficients, _, rank, _ = np.linalg.lstsq(color_features(training_source), training_target, rcond=1e-5)
    if rank < 13 or not np.isfinite(coefficients).all():
        raise VideoRoutePreflightError(
            "The matching pictures do not contain enough color variation to learn HDR color."
        )
    source = np.concatenate([pair[0] for pair in validation])
    target = np.concatenate([pair[1] for pair in validation])
    predicted = np.clip(color_features(source) @ coefficients, 0, 1)
    report: dict[str, Any] = {"held_out": color_error_report(predicted, target), "held_out_frames": len(validation)}
    brightness = target @ np.array([0.2627, 0.6780, 0.0593], dtype=np.float32)
    saturation = target.max(axis=1) - target.min(axis=1)
    regions = {
        "highlights": brightness >= np.quantile(brightness, 0.9),
        "darks": brightness <= np.quantile(brightness, 0.1),
        "saturated": saturation >= np.quantile(saturation, 0.9),
    }
    for name, mask in regions.items():
        report[name] = color_error_report(predicted[mask], target[mask])
    report["worst_frame_rms_codes"] = max(
        float(color_error_report(np.clip(color_features(x) @ coefficients, 0, 1), y)["rms_codes"])
        for x, y in validation
    )
    poor_fit = (
        report["held_out"]["rms_codes"] > MAX_COLOR_RMS_CODES
        or report["held_out"]["p99_codes"] > MAX_COLOR_P99_CODES
        or report["worst_frame_rms_codes"] > MAX_FRAME_RMS_CODES
        or any(report[name]["rms_codes"] > MAX_REGION_RMS_CODES for name in regions)
    )
    if poor_fit:
        raise VideoRoutePreflightError(
            "The two copies have a poor HDR color fit on pictures kept out of training "
            f"(average {report['held_out']['rms_codes']:.1f}, highlights {report['highlights']['rms_codes']:.1f}, "
            f"darks {report['darks']['rms_codes']:.1f}, saturated color {report['saturated']['rms_codes']:.1f} "
            "in 10-bit levels). Choose copies with matching color grades."
        )
    return TitleColorMap(coefficients.astype(np.float32), report)
