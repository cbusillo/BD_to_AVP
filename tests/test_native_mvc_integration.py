"""Opt-in real decoder coverage; see docs/native-mvc-integration.md."""

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bd_to_avp.modules import video
from bd_to_avp.modules.disc import DiscInfo


FIXTURE_ENV = "BD_TO_AVP_MVC_FIXTURE_MANIFEST"
ROOT = Path(__file__).resolve().parents[1]
PROCESS_TIMEOUT = 30


def lossless_command(command: list[str]) -> list[str]:
    """Keep the production graph/maps; replace only lossy output encoding."""
    result = []
    index = 0
    while index < len(command):
        arg = command[index]
        if arg in {"-b:v", "-bufsize", "-tag", "-vprofile"}:
            index += 2
            continue
        if arg == "-vcodec":
            result.extend([arg, "rawvideo"])
            index += 2
            continue
        if arg.startswith("file:"):
            result.extend(["-pix_fmt", "yuv420p", "-f", "rawvideo"])
        result.append(arg)
        index += 1
    return result


@unittest.skipUnless(os.environ.get(FIXTURE_ENV), f"Set {FIXTURE_ENV} to run real MVC tests")
class NativeMvcIntegrationTests(unittest.TestCase):
    def test_real_stream_preserves_frames_and_eye_order(self) -> None:
        self.check_stream("clean")

    def test_corrupt_right_view_finishes_with_skip_warning(self) -> None:
        self.check_stream("damaged")

    def check_stream(self, case: str) -> None:
        manifest_path = Path(os.environ[FIXTURE_ENV]).resolve()
        fixture = json.loads(manifest_path.read_text())[case]
        source = manifest_path.parent / fixture["stream"]
        self.assertEqual(source.suffix, ".264", "edge264 requires the .264 extension")
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), fixture["stream_sha256"])
        width, height = fixture["width"], fixture["height"]
        self.assertGreater(fixture["frames"], 0)
        self.assertEqual(width % 2, 0)
        self.assertEqual(height % 2, 0)
        if case == "clean":
            self.assertNotEqual(fixture["left_sha256"], fixture["right_sha256"], "Use distinguishable eyes")
        disc_info = DiscInfo(
            name="MVC integration", resolution=f"{width}x{height}", frame_rate=fixture["frame_rate"], color_depth=8
        )

        for single_threaded in (True, False):
            for swap_eyes in (False, True) if case == "clean" else (False,):
                with (
                    self.subTest(single_threaded=single_threaded, swap_eyes=swap_eyes),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    directory_path = Path(directory)
                    packed_path = directory_path / "packed.y4m"
                    left_path, right_path = directory_path / "left.yuv", directory_path / "right.yuv"
                    with patch.object(video.config, "EDGE264_TEST_PATH", ROOT / "bd_to_avp/bin/edge264_test"):
                        decoder_command = video.generate_native_mvc_splitter_command(
                            source, single_threaded=single_threaded
                        )
                    # Drain to a file so a full stderr/stdout pipe cannot mask a hang.
                    with packed_path.open("wb") as packed:
                        decoder = subprocess.run(
                            decoder_command, stdout=packed, stderr=subprocess.PIPE, timeout=PROCESS_TIMEOUT, check=False
                        )
                    stderr = decoder.stderr.decode(errors="replace")
                    self.assertEqual(decoder.returncode, 0, stderr)
                    damage = video.Edge264DamageCollector()
                    for line in stderr.splitlines():
                        damage.handle_line(None, line)
                    if case == "damaged":
                        self.assertGreater(damage.skipped_count, 0, stderr)
                        self.assertTrue(damage.frame_indexes, "Missing damage position")
                        self.assertIsNotNone(video.build_damaged_source_warning(damage, fixture["frame_rate"]))
                    else:
                        self.assertEqual(damage.skipped_count, 0, stderr)

                    with (
                        patch.object(video.config, "resolution", ""),
                        patch.object(video.config, "frame_rate", ""),
                        patch.object(video.config, "swap_eyes", swap_eyes),
                        patch.object(video.config, "software_encoder", True),
                    ):
                        command = video.generate_native_mvc_ffmpeg_command(left_path, right_path, disc_info, "")
                    with packed_path.open("rb") as packed:
                        encoded = subprocess.run(
                            lossless_command(command),
                            stdin=packed,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE,
                            timeout=PROCESS_TIMEOUT,
                            check=False,
                        )
                    self.assertEqual(encoded.returncode, 0, encoded.stderr.decode(errors="replace"))
                    frame_bytes = width * height * 3 // 2
                    left, right = left_path.read_bytes(), right_path.read_bytes()
                    self.assertEqual(len(left), fixture["frames"] * frame_bytes, "Left frames dropped or added")
                    self.assertEqual(len(right), len(left), "Unequal eye frame counts")
                    if case == "clean":
                        expected = (fixture["left_sha256"], fixture["right_sha256"])
                        if swap_eyes:
                            expected = expected[::-1]
                        self.assertEqual(hashlib.sha256(left).hexdigest(), expected[0], "Left eye pixels/order changed")
                        self.assertEqual(
                            hashlib.sha256(right).hexdigest(), expected[1], "Right eye pixels/order changed"
                        )


if __name__ == "__main__":
    unittest.main()
