from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
from contextlib import nullcontext

from bd_to_avp.modules import uhd, video_route
from bd_to_avp.modules.disc import DiscInfo
from bd_to_avp.modules.uhd_color import TitleColorMap, read_hdr_reference_info
from bd_to_avp.modules.uhd_color import FloatImage, HDRReferenceInfo
from bd_to_avp.modules.uhd_match import EyeRegistration, MatchedFrame, source_sample_indices
from bd_to_avp.modules.video_route import VideoRoutePreflightError
from tests.test_uhd_route import hdr_probe
from tests.test_uhd_route import texture, stereo_pair


def plan_for_test(reference: Path, frame_count: int = 3000) -> uhd.UHDConversionPlan:
    info = replace(read_hdr_reference_info(hdr_probe()), frame_count=frame_count)
    return uhd.UHDConversionPlan(
        reference,
        info,
        0,
        TitleColorMap(np.zeros((13, 3), dtype=np.float32), {}),
        np.eye(2, 3, dtype=np.float32),
        0,
        frame_count,
    )


@pytest.mark.parametrize("missing", [None, "temporal", "eye", "coverage", "stable_geometry", "beginning"])
def test_whole_film_match_requires_timing_eye_coverage_and_stable_geometry(tmp_path: Path, missing: str | None) -> None:
    plan = plan_for_test(tmp_path / "reference.mkv", 180000)
    info = replace(plan.info, duration_seconds=7507.5)
    indices = source_sample_indices(info.duration_seconds, info.frame_rate)
    if missing == "coverage":
        indices = tuple(index // 4 for index in indices)
    image = np.zeros((1, 1, 3), dtype=np.float32)
    stereo = [(image, image)] * len(indices)

    def match(index: int, left: np.ndarray, _right: np.ndarray, _candidates: dict, *, expected_offset: int | None):
        matrix = np.eye(2, 3, dtype=np.float64)
        if missing == "stable_geometry":
            matrix[0, 2] = -0.9 if index < info.frame_count / 2 else 0.9
        registration = EyeRegistration(matrix, 100, 0.99, 0.1, 8)
        return MatchedFrame(
            index,
            index + (expected_offset or 0) - (1 if missing == "beginning" else 0),
            left,
            left,
            registration,
            missing != "eye",
            missing != "temporal",
        )

    with (
        patch.object(uhd, "read_reference_window", return_value={}),
        patch.object(uhd, "match_frame", side_effect=match),
        pytest.raises(VideoRoutePreflightError) if missing is not None else nullcontext(),
    ):
        matched, offset = uhd.collect_matched_frames(
            plan.reference_path, info, indices, stereo, workspace=tmp_path, run_context=None, cancellation_event=None
        )
        assert len(matched) == len(indices)
        assert offset == 0


def test_missing_reference_tail_is_refused_before_color_fit_or_encoding(tmp_path: Path) -> None:
    source = tmp_path / "source.mkv"
    reference = tmp_path / "reference.mkv"
    info = replace(read_hdr_reference_info(hdr_probe()), frame_count=3000, duration_seconds=3000 * 1001 / 24000)
    disc = DiscInfo(frame_rate="24000/1001", duration_seconds=3010 * 1001 / 24000)
    with (
        patch.object(uhd, "probe_hdr_reference", return_value=info),
        patch.object(uhd, "collect_stereo_samples", return_value=([], 3010)),
        patch.object(uhd, "collect_matched_frames", return_value=([], 0)),
        patch.object(uhd, "fit_title_color_map") as fit,
        pytest.raises(VideoRoutePreflightError),
    ):
        uhd.prepare_uhd_conversion(source, reference, disc, workspace=tmp_path)
    fit.assert_not_called()


def test_an_ambiguous_opening_is_rechecked_against_the_later_frame_offset(tmp_path: Path) -> None:
    plan = plan_for_test(tmp_path / "reference.mkv")
    indices = source_sample_indices(plan.info.duration_seconds, plan.info.frame_rate)
    left = texture()
    right = stereo_pair(left)
    unrelated = left[::-1].copy()
    opening = indices[0]

    def candidates(
        _reference: Path, _info: HDRReferenceInfo, window: range, **_kwargs: object
    ) -> dict[int, FloatImage]:
        if len(window) == 1:
            return {window.start: unrelated}
        source_index = min(indices, key=lambda index: abs(index - (window.start + window.stop - 1) / 2))
        if source_index == opening:
            return {opening: left, opening + 1: left, opening + 2: unrelated}
        return {source_index + 1: unrelated, source_index + 2: left}

    with (
        patch.object(uhd, "read_reference_window", side_effect=candidates),
        pytest.raises(VideoRoutePreflightError, match="drift"),
    ):
        uhd.collect_matched_frames(
            plan.reference_path,
            plan.info,
            indices,
            [(left, right)] * len(indices),
            workspace=tmp_path,
            run_context=None,
            cancellation_event=None,
        )


@pytest.mark.parametrize("previous", [None, "unreplaced", "replaced"])
def test_failed_encode_removes_only_this_attempts_finalized_movie_and_its_own_lut(
    tmp_path: Path, previous: str | None
) -> None:
    output = tmp_path / "stereo.mov"
    if previous is not None:
        output.write_bytes(b"previous movie")
    sentinel = tmp_path / "another-movie.mov"
    sentinel.write_bytes(b"other work")
    plan = plan_for_test(tmp_path / "reference.mkv")

    def fail(*_args: object, **_kwargs: object) -> None:
        if previous != "unreplaced":
            replacement = tmp_path / "native-atomic-output.mov"
            replacement.write_bytes(b"incomplete movie finalized just before producer failure")
            replacement.replace(output)
        raise RuntimeError("source failed")

    with (
        patch.object(uhd, "run_direct_mv_hevc_encoding", side_effect=fail),
        pytest.raises(RuntimeError),
    ):
        uhd.create_uhd_mv_hevc_file(
            tmp_path / "source.h264", output, plan, video_route.uhd_video_route(plan.reference_path, 100, None)
        )
    if previous == "unreplaced":
        assert output.read_bytes() == b"previous movie"
    else:
        assert not output.exists()
    assert not output.with_suffix(".cube").exists()
    assert sentinel.read_bytes() == b"other work"
