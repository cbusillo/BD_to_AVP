import subprocess
import tempfile
import unittest
import json

from pathlib import Path
from unittest.mock import patch

from scripts import verify_apple_media


class AppleMediaVerifyTests(unittest.TestCase):
    def test_missing_media_file_fails_before_avconvert(self) -> None:
        with self.assertRaisesRegex(verify_apple_media.AppleMediaFailure, "Missing media"):
            verify_apple_media.verify_apple_media_compatible(Path("/missing/movie.mov"))

    def test_missing_avconvert_reports_clear_error(self) -> None:
        with tempfile.NamedTemporaryFile() as media_file:
            with patch.object(verify_apple_media.shutil, "which", return_value=None):
                with self.assertRaisesRegex(verify_apple_media.AppleMediaFailure, "avconvert"):
                    verify_apple_media.verify_apple_media_compatible(Path(media_file.name))

    def test_avconvert_failure_includes_output(self) -> None:
        with tempfile.NamedTemporaryFile() as media_file:
            with (
                patch.object(
                    verify_apple_media.shutil,
                    "which",
                    side_effect=lambda command: f"/usr/bin/{command}",
                ),
                patch.object(
                    verify_apple_media,
                    "run",
                    side_effect=subprocess.CalledProcessError(
                        returncode=1,
                        cmd=["avconvert"],
                        output="Cannot Open",
                    ),
                ),
            ):
                with self.assertRaisesRegex(verify_apple_media.AppleMediaFailure, "Cannot Open"):
                    verify_apple_media.verify_apple_media_compatible(Path(media_file.name))

    def test_avconvert_passthrough_command_is_used(self) -> None:
        with tempfile.NamedTemporaryFile() as media_file:
            probe_output = json.dumps({"streams": []})
            with (
                patch.object(
                    verify_apple_media.shutil,
                    "which",
                    side_effect=lambda command: f"/usr/bin/{command}",
                ),
                patch.object(
                    verify_apple_media,
                    "run",
                    side_effect=[
                        subprocess.CompletedProcess([], 0, stdout=""),
                        subprocess.CompletedProcess([], 0, stdout=probe_output),
                        subprocess.CompletedProcess([], 0, stdout=probe_output),
                    ],
                ) as run,
            ):
                verify_apple_media.verify_apple_media_compatible(Path(media_file.name))

        command = run.call_args_list[0].args[0]
        self.assertEqual(command[0], "/usr/bin/avconvert")
        self.assertIn("PresetPassthrough", command)
        self.assertIn("--disableFastStart", command)

    def test_apple_passthrough_must_preserve_audio_tracks(self) -> None:
        with tempfile.NamedTemporaryFile() as media_file:
            source_probe = json.dumps({"streams": [{"codec_type": "video"}, {"codec_type": "audio"}]})
            passthrough_probe = json.dumps({"streams": [{"codec_type": "video"}]})
            with (
                patch.object(
                    verify_apple_media.shutil,
                    "which",
                    side_effect=lambda command: f"/usr/bin/{command}",
                ),
                patch.object(
                    verify_apple_media,
                    "run",
                    side_effect=[
                        subprocess.CompletedProcess([], 0, stdout=""),
                        subprocess.CompletedProcess([], 0, stdout=source_probe),
                        subprocess.CompletedProcess([], 0, stdout=passthrough_probe),
                    ],
                ),
                self.assertRaisesRegex(verify_apple_media.AppleMediaFailure, "dropped 1 audio track"),
            ):
                verify_apple_media.verify_apple_media_compatible(Path(media_file.name))


if __name__ == "__main__":
    unittest.main()
