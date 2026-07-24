import json
import subprocess
import sys
import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from bd_to_avp.modules import video
from bd_to_avp.modules.disc import DiscInfo
from bd_to_avp.modules.video_route import probe_direct_mv_hevc_capability
from scripts.qualify_direct_mv_hevc import DIRECT_REQUIRED_BOX_TYPES, box_types
from scripts.verify_apple_media import verify_apple_media_compatible


class DirectMVHEVCRouteIntegrationTests(unittest.TestCase):
    def test_real_normalizer_and_encoder_pipeline_produces_mv_hevc(self) -> None:
        capability = probe_direct_mv_hevc_capability()
        if not capability.supported:
            self.skipTest(f"Direct MV-HEVC helper is unavailable: {capability.reason}")

        with tempfile.TemporaryDirectory(prefix="direct-route-integration-") as temporary_directory:
            root = Path(temporary_directory)
            y4m_path = root / "stereo.y4m"
            source_path = root / "source.264"
            splitter_path = root / "fake-edge264"
            output_path = root / "Sample_MV-HEVC.mov"
            source_path.write_bytes(b"mvc")
            subprocess.run(
                [
                    video.config.FFMPEG_PATH,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=320x180:rate=24",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=320x180:rate=24,negate",
                    "-filter_complex",
                    "[0:v][1:v]hstack=inputs=2,format=yuv420p",
                    "-t",
                    "2",
                    "-f",
                    "yuv4mpegpipe",
                    "-y",
                    y4m_path,
                ],
                check=True,
            )
            splitter_path.write_text(
                f"#!{sys.executable}\n"
                "import sys\n"
                "from pathlib import Path\n"
                f"sys.stdout.buffer.write(Path({str(y4m_path)!r}).read_bytes())\n",
                encoding="utf-8",
            )
            splitter_path.chmod(0o755)
            disc_info = DiscInfo(
                name="Sample",
                resolution="320x180",
                frame_rate="24/1",
                color_depth=8,
            )

            with (
                patch.object(video.config, "EDGE264_TEST_PATH", splitter_path),
                patch.object(video.config, "keep_files", True),
                patch.object(video.config, "output_commands", False),
                patch.object(video.config, "frame_rate", ""),
                patch.object(video.config, "resolution", ""),
                patch.object(video.config, "swap_eyes", False),
                patch.object(video.config, "fov", 90),
            ):
                video.run_direct_mv_hevc_encoding(
                    source_path,
                    output_path,
                    video.generate_direct_mv_hevc_normalizer_command(disc_info, ""),
                    video.generate_direct_mv_hevc_encoder_command(output_path, None, 0.7),
                )

            completed = subprocess.run(
                [
                    video.config.FFPROBE_PATH,
                    "-v",
                    "error",
                    "-show_entries",
                    "stream=codec_name,width,height:format=duration,size",
                    "-of",
                    "json",
                    output_path,
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            probe = json.loads(completed.stdout)

            self.assertEqual(probe["streams"][0]["codec_name"], "hevc")
            self.assertEqual(probe["streams"][0]["width"], 320)
            self.assertEqual(probe["streams"][0]["height"], 180)
            self.assertGreater(int(probe["format"]["size"]), 0)
            self.assertTrue(DIRECT_REQUIRED_BOX_TYPES.issubset(box_types(output_path)))
            verify_apple_media_compatible(output_path)


if __name__ == "__main__":
    unittest.main()
