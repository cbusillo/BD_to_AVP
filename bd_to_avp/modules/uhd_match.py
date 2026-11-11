from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

import cv2
import numpy as np
from numpy.typing import NDArray

from bd_to_avp.modules.uhd_color import FloatImage, HDRReferenceInfo, SAMPLE_SIZE
from bd_to_avp.modules.video_route import VideoRoutePreflightError

SAMPLE_BINS = 24
MAX_OFFSET_SECONDS = 1
MIN_MATCHED_FEATURES = 24


@dataclass(frozen=True)
class EyeRegistration:
    matrix: NDArray[np.float64]
    matched_features: int
    aligned_fraction: float
    residual_pixels: float
    coverage_cells: int

    @property
    def is_pixel_aligned(self) -> bool:
        scale = np.hypot(self.matrix[0, 0], self.matrix[1, 0])
        rotation = np.degrees(np.arctan2(self.matrix[1, 0], self.matrix[0, 0]))
        return bool(
            abs(scale - 1) <= 0.002
            and abs(rotation) <= 0.05
            and np.max(np.abs(self.matrix[:, 2])) <= 1
            and self.residual_pixels <= 0.6
            and self.aligned_fraction >= 0.75
            and self.coverage_cells >= 4
        )


def check_reference_frame_rate(reference: HDRReferenceInfo, source_rate: str) -> None:
    try:
        rate = Fraction(source_rate)
    except (ValueError, ZeroDivisionError) as error:
        raise VideoRoutePreflightError("The 3D source needs a known frame rate to match a 4K copy.") from error
    if abs(float(rate - reference.frame_rate)) > 0.0001:
        raise VideoRoutePreflightError("The 3D source and 4K copy have different frame rates. Choose matching copies.")


def check_reference_timing(reference: HDRReferenceInfo, source_rate: str, source_duration: float) -> None:
    check_reference_frame_rate(reference, source_rate)
    if source_duration <= 0 or not np.isfinite(source_duration):
        raise VideoRoutePreflightError("The 3D source needs a known runtime to check the cut.")
    if abs(reference.duration_seconds - source_duration) > MAX_OFFSET_SECONDS:
        raise VideoRoutePreflightError("The 3D source and 4K copy have different runtimes and may be different cuts.")


def source_sample_indices(duration: float, rate: Fraction) -> tuple[int, ...]:
    frame_count = int(duration * rate)
    if frame_count < SAMPLE_BINS * 4:
        raise VideoRoutePreflightError("The source is too short to check matching frames across the film.")
    last_frame = frame_count - 1
    return tuple(
        sorted(
            {
                min(last_frame, max(0, int((bin_index + fraction) * frame_count / SAMPLE_BINS)))
                for bin_index in range(SAMPLE_BINS)
                for fraction in (0.25, 0.75)
            }
            | {min(last_frame, round(float(rate) / 2)), max(0, last_frame - round(float(rate) / 2))}
        )
    )


def register_eye(reference: FloatImage, eye: FloatImage) -> EyeRegistration | None:
    def gray(image: FloatImage) -> NDArray[np.uint8]:
        return np.asarray(
            cv2.equalizeHist(np.rint(cv2.cvtColor(image, cv2.COLOR_RGB2GRAY) * 255).clip(0, 255).astype(np.uint8)),
            dtype=np.uint8,
        )

    detector = cv2.SIFT.create(nfeatures=2500)
    reference_keys, reference_descriptors = detector.detectAndCompute(gray(reference), None)
    eye_keys, eye_descriptors = detector.detectAndCompute(gray(eye), None)
    if reference_descriptors is None or eye_descriptors is None:
        return None
    matches = cv2.BFMatcher().knnMatch(reference_descriptors, eye_descriptors, k=2)
    good = [pair[0] for pair in matches if len(pair) == 2 and pair[0].distance < 0.7 * pair[1].distance]
    if len(good) < MIN_MATCHED_FEATURES:
        return None
    source_points = np.array([reference_keys[match.queryIdx].pt for match in good], dtype=np.float32)
    target_points = np.array([eye_keys[match.trainIdx].pt for match in good], dtype=np.float32)
    matrix, _ = cv2.estimateAffinePartial2D(source_points, target_points, ransacReprojThreshold=0.75)
    if matrix is None:
        return None
    transform = np.asarray(matrix, dtype=np.float64)
    projected = np.hstack((source_points, np.ones((len(good), 1), dtype=np.float32))).astype(np.float64) @ transform.T
    residuals = np.linalg.norm(projected - target_points, axis=1)
    aligned = np.asarray(residuals <= 1, dtype=np.bool_)
    cells = {
        (int(point[0] * 4 / reference.shape[1]), int(point[1] * 3 / reference.shape[0]))
        for point in source_points[aligned]
    }
    return EyeRegistration(
        transform, len(good), float(np.count_nonzero(aligned) / aligned.size), float(np.median(residuals)), len(cells)
    )


def correspondence_score(reference: FloatImage, eye: FloatImage) -> float:
    first = cv2.cvtColor(reference, cv2.COLOR_RGB2GRAY)
    second = cv2.cvtColor(eye, cv2.COLOR_RGB2GRAY)
    first = cv2.Laplacian(cv2.GaussianBlur(first, (5, 5), 0), cv2.CV_32F)[12:-12, 12:-12]
    second = cv2.Laplacian(cv2.GaussianBlur(second, (5, 5), 0), cv2.CV_32F)[12:-12, 12:-12]
    denominator = float(np.linalg.norm(first) * np.linalg.norm(second))
    return float(np.sum(first * second) / denominator) if denominator > 1e-8 else 0.0


@dataclass(frozen=True)
class MatchedFrame:
    source_index: int
    reference_index: int
    left: FloatImage
    reference: FloatImage
    registration: EyeRegistration
    eye_verified: bool
    temporal_verified: bool


def match_frame(
    source_index: int,
    left: FloatImage,
    right: FloatImage,
    candidates: dict[int, FloatImage],
    *,
    expected_offset: int | None,
) -> MatchedFrame | None:
    ranked = sorted(
        (
            (max(correspondence_score(reference, left), correspondence_score(reference, right)), index)
            for index, reference in candidates.items()
        ),
        reverse=True,
    )
    if not ranked or ranked[0][0] < 0.4:
        if float(left.std()) < 0.025:
            return None
        raise VideoRoutePreflightError(
            f"The pictures do not match near frame {source_index}. "
            "The copies may be different cuts or differently framed."
        )
    _, best_index = ranked[0]
    if expected_offset is not None:
        expected_index = source_index + expected_offset
        if expected_index not in candidates:
            raise VideoRoutePreflightError("The 4K copy is missing pictures at the beginning or end of the 3D source.")
        expected_score = correspondence_score(candidates[expected_index], left)
        if best_index != expected_index and ranked[0][0] - expected_score > 0.03:
            raise VideoRoutePreflightError(
                f"The matching frames drift near frame {source_index}. The copies may contain different edits."
            )
        best_index = expected_index
    reference = candidates[best_index]
    left_match = register_eye(reference, left)
    right_match = register_eye(reference, right)
    if (
        right_match is not None
        and right_match.is_pixel_aligned
        and (left_match is None or not left_match.is_pixel_aligned)
    ):
        raise VideoRoutePreflightError(
            "The 4K copy matches the right eye. This route needs a copy matching the left eye."
        )
    if left_match is None:
        return None
    if not left_match.is_pixel_aligned:
        raise VideoRoutePreflightError(
            f"The 4K picture cannot be lined up with the 3D left eye near frame {source_index}. "
            "The scale, rotation, position or framing differs."
        )
    left_support = left_match.aligned_fraction * left_match.matched_features
    right_support = 0 if right_match is None else right_match.aligned_fraction * right_match.matched_features
    eye_verified = right_match is None or not right_match.is_pixel_aligned or right_support < left_support * 0.8
    temporal_verified = len(ranked) > 1 and ranked[0][0] - ranked[1][0] > 0.005
    return MatchedFrame(source_index, best_index, left, reference, left_match, eye_verified, temporal_verified)


def aligned_color_pixels(frame: MatchedFrame) -> tuple[FloatImage, FloatImage]:
    width, height = SAMPLE_SIZE
    aligned = cv2.warpAffine(
        frame.left,
        frame.registration.matrix,
        (width, height),
        flags=cv2.INTER_LANCZOS4 | cv2.WARP_INVERSE_MAP,
    )
    source_gray = cv2.cvtColor(aligned, cv2.COLOR_RGB2GRAY)
    target_gray = cv2.cvtColor(frame.reference, cv2.COLOR_RGB2GRAY)
    active_rows = np.flatnonzero(
        (np.quantile(source_gray, 0.9, axis=1) > 0.03) & (np.quantile(target_gray, 0.9, axis=1) > 0.03)
    )
    if not len(active_rows):
        raise VideoRoutePreflightError("There is no shared picture area for HDR color fitting.")
    top, bottom = int(active_rows[0]) + 8, int(active_rows[-1]) - 8
    if bottom - top < height // 2:
        raise VideoRoutePreflightError("The two copies do not share enough picture area for HDR color fitting.")
    return (
        aligned[top:bottom:4, 12:-12:4].reshape(-1, 3).astype(np.float32),
        frame.reference[top:bottom:4, 12:-12:4].reshape(-1, 3),
    )
