import hashlib
import json
import os
import signal
import shutil
import subprocess
import sys
import tempfile
import unittest

from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
RAINFOREST_ISO_ENV = "BD_TO_AVP_RAINFOREST_ISO"
RAINFOREST_PLAYLIST = "1005"
RAINFOREST_CLIP = "00007"
RAINFOREST_FIRST_100_FRAMEMD5_SHA256 = "7ce83ff76fa9998967932874364907dfd8c45482f89db9265c474cbd65c228ae"


@unittest.skipUnless(os.environ.get(RAINFOREST_ISO_ENV), f"Set {RAINFOREST_ISO_ENV} to run real-media tests")
class SsifProbeIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source_path = Path(os.environ[RAINFOREST_ISO_ENV])
        if not cls.source_path.is_file():
            raise unittest.SkipTest(f"Rainforest ISO is unavailable: {cls.source_path}")
        cls.temporary_directory = tempfile.TemporaryDirectory()
        cls.helper_path = Path(cls.temporary_directory.name) / "ssif_probe"
        subprocess.run(
            [
                sys.executable,
                "scripts/build_ssif_probe_macos.py",
                "--output",
                str(cls.helper_path),
            ],
            cwd=REPOSITORY_ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        if hasattr(cls, "temporary_directory"):
            cls.temporary_directory.cleanup()

    def test_rainforest_inspection_contract(self) -> None:
        result = subprocess.run(
            [str(self.helper_path), "inspect", str(self.source_path), RAINFOREST_PLAYLIST],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )

        self.assertEqual(result.stderr, "")
        inspection = json.loads(result.stdout)
        self.assertEqual(inspection["libbluray_version"], "1.4.1")
        self.assertTrue(inspection["content_3d"])
        self.assertFalse(inspection["aacs_detected"])
        self.assertFalse(inspection["aacs_handled"])
        self.assertFalse(inspection["bdplus_detected"])
        self.assertFalse(inspection["bdplus_handled"])
        self.assertEqual(inspection["title"]["playlist"], 1005)
        self.assertTrue(inspection["title"]["main_feature"])
        self.assertTrue(inspection["title"]["eligible"])
        self.assertTrue(inspection["title"]["complete_clip"])
        self.assertEqual(inspection["title"]["mvc_pids"], {"base": 0x1011, "dependent": 0x1012})
        self.assertEqual(inspection["title"]["clips"][0]["id"], RAINFOREST_CLIP)
        self.assertEqual(inspection["title"]["clips"][0]["ssif_size_bytes"], 16970784768)
        self.assertEqual(
            [stream["pid"] for stream in inspection["title"]["clips"][0]["audio_streams"]],
            [0x1100, 0x1101],
        )
        self.assertEqual(
            [stream["language"] for stream in inspection["title"]["clips"][0]["audio_streams"]],
            ["deu", "eng"],
        )
        self.assertEqual(inspection["title"]["clips"][0]["pg_streams"], [])

    def test_first_100_stereo_frames_match_accepted_fixture(self) -> None:
        ffmpeg_path = shutil.which("ffmpeg")
        if ffmpeg_path is None:
            raise unittest.SkipTest("FFmpeg is unavailable")
        edge264_path = REPOSITORY_ROOT / "bd_to_avp/bin/edge264_test"
        output_path = Path(self.temporary_directory.name) / "first-100.framemd5"
        stream_process = subprocess.Popen(
            [str(self.helper_path), "stream-mvc", str(self.source_path), RAINFOREST_PLAYLIST, "116"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert stream_process.stdout is not None
        edge_process = subprocess.Popen(
            [str(edge264_path), "-", "-Osk"],
            stdin=stream_process.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stream_process.stdout.close()
        assert edge_process.stdout is not None
        ffmpeg_result = subprocess.run(
            [
                ffmpeg_path,
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "yuv4mpegpipe",
                "-i",
                "pipe:0",
                "-frames:v",
                "100",
                "-f",
                "framemd5",
                "-y",
                str(output_path),
            ],
            stdin=edge_process.stdout,
            capture_output=True,
            check=False,
            timeout=120,
        )
        edge_process.stdout.close()
        edge_stderr = edge_process.communicate(timeout=30)[1]
        stream_stderr = stream_process.communicate(timeout=30)[1]

        self.assertEqual(ffmpeg_result.returncode, 0, ffmpeg_result.stderr.decode())
        self.assertIn(edge_process.returncode, {0, -signal.SIGPIPE}, edge_stderr.decode())
        self.assertEqual(stream_process.returncode, 0, stream_stderr.decode())
        self.assertEqual(
            hashlib.sha256(output_path.read_bytes()).hexdigest(),
            RAINFOREST_FIRST_100_FRAMEMD5_SHA256,
        )


if __name__ == "__main__":
    unittest.main()
