from __future__ import annotations

import json
import platform
import shutil
import subprocess
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
from bd_to_avp.modules.video import generate_native_mvc_splitter_command
from bd_to_avp.process_runner import ProcessPipelineRunner, ProcessPipelineStage, ProcessSpec
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
    normalized = tmp_path / "normalized.y4m"
    with normalized.open("wb") as destination:
        ProcessPipelineRunner().run(
            (
                ProcessPipelineStage(
                    ProcessSpec(
                        argv=tuple(generate_native_mvc_splitter_command(source)),
                        tool_id="edge264",
                        display_name="Decode real MVC",
                    )
                ),
                ProcessPipelineStage(
                    ProcessSpec(
                        argv=tuple(generate_uhd_normalizer_command(plan, lut)),
                        tool_id="ffmpeg",
                        display_name="Normalize 10-bit stereo",
                        stdout=destination,
                        merge_stderr=False,
                    )
                ),
            )
        )

    def frame_hashes(path: Path, crop: bool) -> list[str]:
        command = [str(config.FFMPEG_PATH), "-v", "error", "-i", str(path), "-map", "0:v:0"]
        if crop:
            command += ["-vf", "crop=3840:2160:0:0"]
        result = subprocess.run(
            [*command, "-pix_fmt", "yuv420p10le", "-f", "framemd5", "-"],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return [
            line.rsplit(",", 1)[-1].strip() for line in result.stdout.splitlines() if line and not line.startswith("#")
        ]

    assert frame_hashes(normalized, True) == frame_hashes(reference, False)
    normalized.unlink()
