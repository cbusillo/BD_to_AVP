import hashlib
import json
import os
import signal
import shutil
import subprocess
import struct
import sys
import tempfile
import unittest

from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
RAINFOREST_ISO_ENV = "BD_TO_AVP_RAINFOREST_ISO"
RAINFOREST_PLAYLIST = "1005"
RAINFOREST_CLIP = "00007"
RAINFOREST_FIRST_100_FRAME_LINES_SHA256 = "186a33d0a66c39619b94d354f77fbe364ee24c2e441ee25d90a8902b75199166"
SERVICE_FRAME_HEADER = struct.Struct(">4sBBHQQQII")


def framemd5_frame_lines_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for line in handle:
            if not line.startswith(b"#"):
                digest.update(line)
    return digest.hexdigest()


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
        installed_libbluray_version = subprocess.check_output(
            ["pkg-config", "--modversion", "libbluray"],
            text=True,
        ).strip()
        self.assertEqual(inspection["libbluray_version"], installed_libbluray_version)
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
        self.assertEqual(framemd5_frame_lines_sha256(output_path), RAINFOREST_FIRST_100_FRAME_LINES_SHA256)

    def test_bounded_live_source_emits_selected_audio_and_replay_boundaries(self) -> None:
        process = subprocess.Popen(
            [
                str(self.helper_path),
                "stream-service",
                str(self.source_path),
                RAINFOREST_PLAYLIST,
                str(0x1101),
                "116",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert process.stdout is not None
        expected_sequence = 0
        video_dts: list[int] = []
        audio_dts: list[int] = []
        replay_boundaries = 0
        completed = False
        while True:
            header = process.stdout.read(SERVICE_FRAME_HEADER.size)
            if not header:
                break
            self.assertEqual(len(header), SERVICE_FRAME_HEADER.size)
            magic, version, kind, flags, sequence, _, dts, primary_length, secondary_length = (
                SERVICE_FRAME_HEADER.unpack(header)
            )
            self.assertEqual(magic, b"SSFS")
            self.assertEqual(version, 1)
            self.assertEqual(sequence, expected_sequence)
            expected_sequence += 1
            remaining = primary_length + secondary_length
            while remaining > 0:
                chunk = process.stdout.read(min(remaining, 1_048_576))
                self.assertTrue(chunk)
                remaining -= len(chunk)
            if kind == 1:
                video_dts.append(dts)
                replay_boundaries += int(bool(flags & 1))
            elif kind == 2:
                audio_dts.append(dts)
            elif kind == 3:
                completed = True
            else:
                self.fail(f"Unexpected live-source record kind: {kind}")
        stderr = process.communicate(timeout=30)[1]

        self.assertEqual(process.returncode, 0, stderr.decode())
        self.assertTrue(completed)
        self.assertEqual(len(video_dts), 116)
        self.assertGreater(len(audio_dts), 0)
        self.assertGreater(replay_boundaries, 0)
        self.assertEqual(video_dts, sorted(video_dts))
        self.assertEqual(audio_dts, sorted(audio_dts))
        status = json.loads(stderr)
        self.assertEqual(status["pairs"], 116)
        self.assertEqual(status["audio_samples"], len(audio_dts))


if __name__ == "__main__":
    unittest.main()
