import subprocess
import tempfile
import unittest

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
                patch.object(verify_apple_media.shutil, "which", return_value="/usr/bin/avconvert"),
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
            with (
                patch.object(verify_apple_media.shutil, "which", return_value="/usr/bin/avconvert"),
                patch.object(verify_apple_media, "run") as run,
            ):
                verify_apple_media.verify_apple_media_compatible(Path(media_file.name))

        command = run.call_args.args[0]
        self.assertEqual(command[0], "/usr/bin/avconvert")
        self.assertIn("PresetPassthrough", command)
        self.assertIn("--disableFastStart", command)


if __name__ == "__main__":
    unittest.main()
