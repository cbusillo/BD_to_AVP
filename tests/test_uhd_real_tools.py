from __future__ import annotations

import json
import platform
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from bd_to_avp.modules.command import run_ffprobe
from bd_to_avp.modules.config import config
from bd_to_avp.modules.uhd import UHDConversionPlan, create_uhd_mv_hevc_file
from bd_to_avp.modules.uhd_color import TitleColorMap, read_hdr_reference_info
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
    reference_probe = run_ffprobe(reference, select_streams="v:0", read_intervals="%+1", show_frames=None)
    info = read_hdr_reference_info(reference_probe)
    coefficients = np.zeros((13, 3), dtype=np.float32)
    coefficients[:3] = np.eye(3, dtype=np.float32) * 0.5
    coefficients[-1] = 0.1
    plan = UHDConversionPlan(reference, info, 0, TitleColorMap(coefficients, {}), np.eye(2, 3, dtype=np.float32), 0)
    output = tmp_path / "stereo.mov"
    source = Path(__file__).resolve().parents[1] / "macos/BDToAVPPlayerTests/Fixtures/edge264-mvc-display-ordering.264"
    with patch.object(config, "MV_HEVC_ENCODER_PATH", encoder):
        create_uhd_mv_hevc_file(source, output, plan, uhd_video_route(reference, 100, None))
    result = run_ffprobe(output, select_streams="v:0", read_intervals="%+1", show_frames=None)
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
