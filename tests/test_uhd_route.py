from __future__ import annotations

from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from unittest.mock import Mock, patch

import cv2
import numpy as np
import pytest

from bd_to_avp.modules import process, video_route
from bd_to_avp.modules.uhd_color import (
    fit_title_color_map,
    pack_hdr_stereo,
    read_hdr_reference_info,
)
from bd_to_avp.modules.uhd_match import check_reference_timing, match_frame, source_sample_indices
from bd_to_avp.modules.video_route import VideoRoutePreflightError
from tests.test_video_route import job_options, mv_hevc_encoding


def hdr_probe() -> dict:
    return {
        "streams": [
            {
                "codec_type": "video",
                "width": 3840,
                "height": 2160,
                "pix_fmt": "yuv420p10le",
                "color_transfer": "smpte2084",
                "color_primaries": "bt2020",
                "color_space": "bt2020nc",
                "avg_frame_rate": "24000/1001",
                "r_frame_rate": "24000/1001",
                "duration": "120",
                "side_data_list": [
                    {
                        "side_data_type": "Mastering display metadata",
                        "green_x": "3/10",
                        "green_y": "3/5",
                        "blue_x": "3/20",
                        "blue_y": "3/50",
                        "red_x": "16/25",
                        "red_y": "33/100",
                        "white_point_x": "3127/10000",
                        "white_point_y": "329/1000",
                        "max_luminance": "600",
                        "min_luminance": "1/200",
                    },
                    {"side_data_type": "Content light level metadata", "max_content": 789, "max_average": 234},
                ],
            }
        ],
        "format": {"duration": "120"},
    }


def texture() -> np.ndarray:
    rng = np.random.default_rng(77)
    image = cv2.GaussianBlur(rng.random((540, 960, 3), dtype=np.float32), (5, 5), 0)
    for index in range(40):
        center = tuple(int(value) for value in rng.integers([20, 20], [940, 520]))
        cv2.circle(image, center, 10 + index % 15, tuple(float(value) for value in rng.random(3)), -1)
    return image


def stereo_pair(left: np.ndarray) -> np.ndarray:
    right = np.roll(left, 12, axis=1)
    right[:, 480:] = np.roll(left[:, 480:], -9, axis=1)
    return right


def test_explicit_uhd_route_uses_hdr_encoder_without_generated_or_metalfx_fallback() -> None:
    capability = Mock(return_value=video_route.DirectMVHEVCCapability(True, "supported"))
    route = video_route.resolve_video_route(
        mv_hevc_encoding(), job_options(), uhd_reference=Path("reference.mkv"), capability_probe=capability
    )
    assert route.selected is video_route.VideoRouteKind.UHD_HDR_MV_HEVC
    assert route.uhd_reference == Path("reference.mkv")
    assert route.direct_quality is not None
    assert route.direct_upscale_mode is None
    with pytest.raises(VideoRoutePreflightError, match="4K HDR"):
        video_route.resolve_video_route(
            mv_hevc_encoding(),
            job_options(),
            uhd_reference=Path("reference.mkv"),
            capability_probe=lambda: video_route.DirectMVHEVCCapability(False, "unsupported"),
        )


@pytest.mark.parametrize(
    "overrides", [{"swap_eyes": True}, {"frame_rate": "24"}, {"resolution": "1280x720"}, {"crop_black_bars": True}]
)
def test_uhd_route_refuses_picture_overrides(overrides: dict) -> None:
    with pytest.raises(VideoRoutePreflightError):
        video_route.resolve_video_route(
            replace(mv_hevc_encoding(), **overrides),
            job_options(),
            uhd_reference=Path("reference.mkv"),
        )


@pytest.mark.parametrize(
    "fields,words",
    [
        ({"pix_fmt": "yuv420p"}, "8-bit"),
        ({"color_transfer": "bt709"}, "SDR"),
        ({"color_primaries": "bt709"}, "BT.2020"),
        ({"avg_frame_rate": "0/0"}, "frame rate"),
        ({"r_frame_rate": "30/1"}, "constant"),
    ],
)
def test_refuses_non_hdr_or_unknown_timing(fields: dict, words: str) -> None:
    probe = hdr_probe()
    probe["streams"][0].update(fields)
    with pytest.raises(VideoRoutePreflightError, match=words):
        read_hdr_reference_info(probe)


def test_copies_mastering_and_content_light_metadata_in_encoder_units() -> None:
    probe = hdr_probe()
    info = read_hdr_reference_info(probe)
    assert info.mastering_display == "G(15000,30000)B(7500,3000)R(32000,16500)WP(15635,16450)L(6000000,50)"
    assert info.max_cll == "789,234"
    probe["streams"][0]["side_data_list"] = []
    missing = read_hdr_reference_info(probe)
    assert missing.mastering_display is None
    assert missing.max_cll is None


@pytest.mark.parametrize("rate,duration,words", [("24", 120, "frame rates"), ("24000/1001", 125, "different cuts")])
def test_refuses_wrong_rate_or_runtime(rate: str, duration: float, words: str) -> None:
    with pytest.raises(VideoRoutePreflightError, match=words):
        check_reference_timing(read_hdr_reference_info(hdr_probe()), rate, duration)


def test_sampling_spans_the_film_including_both_tails() -> None:
    rate = Fraction(24000, 1001)
    indices = source_sample_indices(7200, rate)
    assert indices[0] / rate < 1
    assert 7200 - indices[-1] / rate < 1
    assert max(np.diff(indices)) / rate < 160


def test_refuses_matching_the_wrong_eye_and_wrong_spatial_fit() -> None:
    left = texture()
    right = stereo_pair(left)
    with pytest.raises(VideoRoutePreflightError, match="right eye"):
        match_frame(100, left, right, {100: right}, expected_offset=0)
    matrix = cv2.getRotationMatrix2D((480, 270), 2, 1.03)
    reframed = cv2.warpAffine(left, matrix, (960, 540))
    with pytest.raises(VideoRoutePreflightError, match=r"scale|framed"):
        match_frame(100, left, right, {100: reframed}, expected_offset=0)


def test_refuses_a_cut_or_frame_offset_change_in_the_middle() -> None:
    left = texture()
    right = stereo_pair(left)
    with pytest.raises(VideoRoutePreflightError, match="drift"):
        match_frame(100, left, right, {100: np.roll(left, 5, axis=0), 101: left}, expected_offset=0)
    with pytest.raises(VideoRoutePreflightError, match="different cuts"):
        match_frame(100, left, right, {100: texture()[::-1].copy()}, expected_offset=0)


def test_color_fit_scores_only_held_out_frames_and_refuses_highlight_tail_failure() -> None:
    rng = np.random.default_rng(1)
    frames = [rng.random((5000, 3), dtype=np.float32) for _ in range(6)]
    training = [(x, np.float32(0.1) + np.float32(0.5) * x) for x in frames[:3]]
    validation = [(x, np.float32(0.1) + np.float32(0.5) * x) for x in frames[3:]]
    fitted = fit_title_color_map(training, validation)
    assert fitted.validation["held_out"]["rms_codes"] < 0.01
    assert all(fitted.validation[region]["pixels"] > 0 for region in ("highlights", "darks", "saturated"))
    changed = []
    for source, target in validation:
        target = target.copy()
        target[source.mean(axis=1) > 0.7] += 0.3
        changed.append((source, target))
    with pytest.raises(VideoRoutePreflightError, match="poor HDR color fit"):
        fit_title_color_map(training, changed)


def test_packed_ten_bit_left_eye_is_byte_exact_and_right_eye_has_sub_eight_bit_steps() -> None:
    width, height = 32, 16
    rng = np.random.default_rng(7)
    left_planes = [
        rng.integers(64, 941, (height, width), dtype="<u2"),
        rng.integers(64, 961, (height // 2, width // 2), dtype="<u2"),
        rng.integers(64, 961, (height // 2, width // 2), dtype="<u2"),
    ]
    right = np.full((height, width, 3), 0.4, dtype=np.float32)
    right[:, :, :] += np.arange(width, dtype=np.float32)[None, :, None] / 1023
    packed = pack_hdr_stereo(b"".join(plane.tobytes() for plane in left_planes), right)
    values = np.frombuffer(packed[len(b"FRAME\n") :], dtype="<u2")
    luma = values[: width * height * 2].reshape(height, width * 2)
    blue = values[width * height * 2 : width * height * 5 // 2].reshape(height // 2, width)
    red = values[width * height * 5 // 2 :].reshape(height // 2, width)
    assert np.array_equal(luma[:, :width], left_planes[0])
    assert np.array_equal(blue[:, : width // 2], left_planes[1])
    assert np.array_equal(red[:, : width // 2], left_planes[2])
    assert len(np.unique(luma[:, width:])) > width // 2


def test_frame_mastering_metadata_is_copied_when_the_container_has_none() -> None:
    probe = hdr_probe()
    original = read_hdr_reference_info(probe)
    side_data = probe["streams"][0].pop("side_data_list")
    probe["frames"] = [{"side_data_list": side_data}]
    info = read_hdr_reference_info(probe)
    assert info.mastering_display == original.mastering_display
    assert info.max_cll == original.max_cll


def test_cli_passes_the_reference_to_route_selection() -> None:
    import sys
    from bd_to_avp.modules.config import Config

    settings = Config()
    with patch.object(sys, "argv", ["bd-to-avp", "--source", "3d.mkv", "--uhd-reference", "uhd.mkv"]):
        settings.parse_args()
    with patch.object(video_route, "config", settings):
        route = video_route.legacy_video_route()
    assert route.selected is video_route.VideoRouteKind.UHD_HDR_MV_HEVC
    assert route.uhd_reference == Path("uhd.mkv")


def test_match_refusal_happens_before_audio_or_video_encoding(tmp_path: Path) -> None:
    from bd_to_avp.modules.disc import DiscInfo
    from bd_to_avp.modules.config import Stage

    source = tmp_path / "source.mkv"
    source.touch()
    route = video_route.uhd_video_route(Path("uhd.mkv"), 100, None)
    with (
        patch.object(process.config, "source_path", source),
        patch.object(process.config, "output_root_path", tmp_path / "output"),
        patch.object(process.config, "start_stage", Stage.CREATE_MKV),
        patch.object(process.config, "source_folder_path", None),
        patch.object(process.config, "preview_range", None),
        patch.object(process.preflight, "verify_runtime_ready"),
        patch.object(process, "get_disc_and_mvc_video_info", return_value=DiscInfo(name="Movie")),
        patch.object(process, "create_mkv_file", return_value=source),
        patch.object(process, "prepare_uhd_conversion", side_effect=VideoRoutePreflightError("different cuts")),
        patch.object(process, "create_transcoded_audio_file") as audio,
        patch.object(process, "create_uhd_mv_hevc_file") as video,
        pytest.raises(VideoRoutePreflightError, match="different cuts"),
    ):
        process.process_each(video_route=route)
    audio.assert_not_called()
    video.assert_not_called()


def test_decoded_frame_color_tags_qualify_hdr_when_container_omits_them() -> None:
    probe = hdr_probe()
    original = read_hdr_reference_info(probe)
    tags = {key: probe["streams"][0].pop(key) for key in ("color_transfer", "color_primaries")}
    probe["frames"] = [tags]
    assert read_hdr_reference_info(probe) == original
    probe["streams"][0]["color_transfer"] = "bt709"
    with pytest.raises(VideoRoutePreflightError, match="conflicting HDR"):
        read_hdr_reference_info(probe)


def test_accepts_the_rounded_blu_ray_frame_rate_and_a_clear_left_eye_match() -> None:
    check_reference_timing(read_hdr_reference_info(hdr_probe()), "23.976", 120)
    left = texture()
    right = stereo_pair(left)
    result = match_frame(100, left, right, {99: np.roll(left, 5, axis=0), 100: left}, expected_offset=0)
    assert result is not None
    assert result.reference_index == 100
    assert result.eye_verified
