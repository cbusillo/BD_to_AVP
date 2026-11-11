from __future__ import annotations

import json
import platform
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import ffmpeg
import numpy as np
import pytest

from bd_to_avp.modules.command import run_ffprobe
from bd_to_avp.modules.config import config
from bd_to_avp.modules.uhd import (
    UHDConversionPlan,
    collect_stereo_samples,
    create_uhd_mv_hevc_file,
    generate_uhd_normalizer_command,
    probe_hdr_reference,
    write_title_lut,
)
from bd_to_avp.modules.container import create_muxed_file
from bd_to_avp.modules.audio_mode import AudioMode
from bd_to_avp.modules.config import Stage
from bd_to_avp.modules.uhd_color import TitleColorMap, read_hdr_reference_info
from tests.test_uhd_route import hdr_probe
from bd_to_avp.modules.video_route import uhd_video_route
from scripts.build_mv_hevc_encoder_macos import build_encoder


@pytest.mark.skipif(
    platform.system() != "Darwin" or platform.machine() != "arm64" or not shutil.which("xcrun"),
    reason="4K HDR MV-HEVC requires macOS arm64 and Xcode",
)
def test_real_mvc_to_hdr_main10_copies_reference_mastering_metadata(tmp_path: Path) -> None:
    encoder = tmp_path / "mv-hevc-encoder"
    build_encoder(encoder)
    capability = subprocess.run([encoder, "--capability-probe"], capture_output=True, timeout=30)
    if capability.returncode == 2:
        pytest.skip("This Mac cannot encode MV-HEVC")
    assert capability.returncode == 0, capability.stderr.decode(errors="replace")
    assert json.loads(capability.stdout)["stereo_mv_hevc_encode_supported"]
    reference = tmp_path / "reference.mkv"
    mastering = "G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(6000000,50)"
    subprocess.run(
        [
            str(config.FFMPEG_PATH),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=3840x2160:rate=24000/1001",
            "-frames:v",
            "8",
            "-pix_fmt",
            "yuv420p10le",
            "-c:v",
            "libx265",
            "-preset",
            "ultrafast",
            "-color_primaries",
            "bt2020",
            "-color_trc",
            "smpte2084",
            "-colorspace",
            "bt2020nc",
            "-color_range",
            "tv",
            "-x265-params",
            f"lossless=1:pools=2:master-display={mastering}:max-cll=789,234:"
            "colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc:log-level=error",
            str(reference),
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )
    info = probe_hdr_reference(reference)
    coefficients = np.zeros((13, 3), dtype=np.float32)
    coefficients[:3] = np.eye(3, dtype=np.float32) * 0.5
    coefficients[-1] = 0.1
    source = Path(__file__).resolve().parents[1] / "macos/BDToAVPPlayerTests/Fixtures/edge264-mvc-display-ordering.264"
    _, source_count = collect_stereo_samples(
        source, (0, 2, 6), workspace=tmp_path, mvc_producer=None, run_context=None, cancellation_event=None
    )
    assert info.frame_count == source_count
    plan = UHDConversionPlan(
        reference, info, 0, TitleColorMap(coefficients, {}), np.eye(2, 3, dtype=np.float32), 0, source_count
    )
    output = tmp_path / "stereo.mov"
    with patch.object(config, "MV_HEVC_ENCODER_PATH", encoder):
        create_uhd_mv_hevc_file(source, output, plan, uhd_video_route(reference, 100, None))
    with patch.object(config, "audio_mode", AudioMode.PCM), patch.object(config, "start_stage", Stage.CREATE_MKV):
        final = create_muxed_file(reference, output, tmp_path, "Final HDR movie")
    result = run_ffprobe(final, select_streams="v:0", read_intervals="%+1", show_frames=None)
    video = result["streams"][0]
    assert (video["width"], video["height"]) == (3840, 2160)
    assert video["pix_fmt"] == "yuv420p10le"
    assert video["color_transfer"] == "smpte2084"
    assert video["color_primaries"] == "bt2020"
    assert video["color_space"] == "bt2020nc"
    copied = read_hdr_reference_info(result)
    assert copied.mastering_display == info.mastering_display
    assert copied.max_cll == info.max_cll
    assert not list(tmp_path.glob(".stereo.mov.partial-*"))

    lut = tmp_path / "color's map.cube"
    write_title_lut(plan.color_map, lut)
    pixels = np.random.default_rng(9).integers(0, 65536, (16, 16, 3), dtype="<u2")
    mapping = ffmpeg.input("pipe:0", format="rawvideo", pix_fmt="rgb48le", s="16x16").filter(
        "lut3d", file=str(lut), interp="tetrahedral"
    )
    mapping = ffmpeg.output(mapping, "pipe:1", format="rawvideo", pix_fmt="rgb48le")
    mapped = subprocess.run(
        ffmpeg.compile(mapping, cmd=str(config.FFMPEG_PATH)),
        input=pixels.tobytes(),
        capture_output=True,
        check=True,
        timeout=30,
    )
    actual = np.frombuffer(mapped.stdout, "<u2").reshape(16, 16, 3).astype(np.float32) / np.float32(65535)
    expected = plan.color_map.apply(pixels.astype(np.float32) / np.float32(65535))
    assert np.max(np.abs(actual - expected)) * 1023 < 1


def test_normalizer_preserves_uhd_outside_changing_per_eye_masks_and_keeps_warp_precision(tmp_path: Path) -> None:
    width, height = 3840, 2160
    source_width, source_height = 32, 18
    left = np.full((4, source_height, source_width), 80, dtype=np.uint8)
    right = np.broadcast_to(np.linspace(16, 235, source_width, dtype=np.uint8), left.shape).copy()
    left[0, :2] = left[0, -2:] = right[0, :2] = right[0, -2:] = 16
    left[1, :, :2] = right[1, :, :4] = 16
    left[1, 8:10] = right[1, 8:10] = 16
    left[2] = 16
    left[2, 7, 8] = 80
    left[3] = right[3] = 16
    source = bytearray(f"YUV4MPEG2 W{source_width * 2} H{source_height} F24000:1001 Ip C420jpeg\n".encode())
    for left_y, right_y in zip(left, right, strict=True):
        source.extend(b"FRAME\n")
        source.extend(np.hstack((left_y, right_y)).tobytes())
        source.extend(np.full((source_height // 2, source_width), 128, dtype=np.uint8).tobytes() * 2)
    rng = np.random.default_rng(42)
    reference_planes = (
        rng.integers(64, 941, (height, width), dtype="<u2"),
        rng.integers(64, 961, (height // 2, width // 2), dtype="<u2"),
        rng.integers(64, 961, (height // 2, width // 2), dtype="<u2"),
    )
    reference = tmp_path / "reference.y4m"
    with reference.open("wb") as destination:
        destination.write(f"YUV4MPEG2 W{width} H{height} F24000:1001 Ip C420p10\n".encode())
        for _ in left:
            destination.write(b"FRAME\n")
            for plane in reference_planes:
                destination.write(plane.tobytes())
    coefficients = np.zeros((13, 3), dtype=np.float32)
    coefficients[:3] = np.eye(3, dtype=np.float32)
    coefficients[-1] = 0.03
    registration = np.eye(2, 3, dtype=np.float32)
    registration[0, 2] = 1
    plan = UHDConversionPlan(
        reference,
        replace(read_hdr_reference_info(hdr_probe()), frame_count=len(left)),
        0,
        TitleColorMap(coefficients, {}),
        registration,
        0,
        len(left),
    )
    lut = tmp_path / "map.cube"
    write_title_lut(plan.color_map, lut)
    normalized = tmp_path / "normalized.y4m"
    with normalized.open("wb") as destination:
        subprocess.run(
            generate_uhd_normalizer_command(plan, lut),
            input=source,
            stdout=destination,
            stderr=subprocess.PIPE,
            check=True,
            timeout=120,
        )

    def expected_mask(luma: np.ndarray) -> np.ndarray:
        active = luma > 18
        rows = np.flatnonzero(active.any(axis=1))
        columns = np.flatnonzero(active.any(axis=0))
        mask = np.zeros_like(active)
        if len(rows):
            mask[rows[0] : rows[-1] + 1, columns[0] : columns[-1] + 1] = True
        mask = np.repeat(np.repeat(mask, height // source_height, axis=0), width // source_width, axis=1)
        shifted = np.zeros_like(mask)
        shifted[:, :-4] = mask[:, 4:]
        return shifted

    with normalized.open("rb") as frames:
        assert frames.readline().startswith(b"YUV4MPEG2")
        for index in range(len(left)):
            assert frames.readline().startswith(b"FRAME")
            planes = (
                np.frombuffer(frames.read(height * width * 4), "<u2").reshape(height, width * 2),
                np.frombuffer(frames.read(height * width), "<u2").reshape(height // 2, width),
                np.frombuffer(frames.read(height * width), "<u2").reshape(height // 2, width),
            )
            if index == 0:
                assert len(np.unique(planes[0][height // 2, width + 200 : -200])) > 300
            left_mask = expected_mask(left[index])
            right_mask = expected_mask(right[index])
            for channel, (plane, original) in enumerate(zip(planes, reference_planes, strict=True)):
                keep_left = left_mask if channel == 0 else left_mask[::2, ::2]
                keep_right = right_mask if channel == 0 else right_mask[::2, ::2]
                left_plane, right_plane = np.split(plane, 2, axis=1)
                assert np.array_equal(left_plane[keep_left], original[keep_left])
                assert np.all(left_plane[~keep_left] == (64 if channel == 0 else 512))
                assert np.all(right_plane[~keep_right] == (64 if channel == 0 else 512))
        assert not frames.read(1)
    normalized.unlink()
