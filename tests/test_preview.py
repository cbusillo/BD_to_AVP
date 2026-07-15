import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from bd_to_avp.modules.config import config
from bd_to_avp.modules.preview import (
    PreviewRange,
    create_bounded_preview_source,
    resolve_preview_range,
)


class PreviewRangeTests(unittest.TestCase):
    def test_resolves_beginning_middle_and_end(self) -> None:
        beginning = resolve_preview_range(7200, 60, "beginning")
        middle = resolve_preview_range(7200, 60, "middle")
        ending = resolve_preview_range(7200, 60, "end")

        self.assertEqual(beginning.start_seconds, 0)
        self.assertEqual(middle.start_seconds, 3570)
        self.assertEqual(ending.start_seconds, 7140)
        self.assertEqual(ending.duration_seconds, 60)

    def test_clamps_preview_to_short_source(self) -> None:
        preview_range = resolve_preview_range(45, 60, "end")

        self.assertEqual(preview_range.start_seconds, 0)
        self.assertEqual(preview_range.duration_seconds, 45)

    def test_rejects_unknown_source_duration(self) -> None:
        with self.assertRaisesRegex(ValueError, "duration is not available"):
            resolve_preview_range(0, 60, "middle")


class PreviewSourceTests(unittest.TestCase):
    def test_creates_atomic_bounded_container_with_all_playback_streams(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            source_path = temporary_path / "movie.mkv"
            source_path.write_bytes(b"source")
            output_folder = temporary_path / "output"
            output_folder.mkdir()
            preview_range = PreviewRange(3570, 60, 7200)
            observed_command: list[object] = []

            def run(command: list[object], _name: str) -> str:
                observed_command.extend(command)
                Path(command[-1]).write_bytes(b"preview")
                return ""

            with (
                patch("bd_to_avp.modules.preview.run_command", side_effect=run),
                patch(
                    "bd_to_avp.modules.preview.ffmpeg.probe",
                    side_effect=[
                        {
                            "format": {"start_time": "0", "duration": "7200"},
                            "streams": [{"codec_type": "video", "start_time": "0"}],
                        },
                        {
                            "format": {"start_time": "3569", "duration": "3629"},
                            "streams": [{"codec_type": "video", "start_time": "3569"}],
                        },
                        {
                            "format": {"start_time": "0", "duration": "61"},
                            "streams": [{"codec_type": "video", "start_time": "0"}],
                        },
                    ],
                ),
            ):
                output_path, aligned_range = create_bounded_preview_source(
                    source_path,
                    output_folder,
                    preview_range,
                )

            self.assertEqual(output_path, output_folder / "movie_preview.mkv")
            self.assertTrue(output_path.is_file())
            self.assertEqual(observed_command[0], config.FFMPEG_PATH)
            self.assertEqual(aligned_range.start_seconds, 3569)
            self.assertEqual(aligned_range.duration_seconds, 61)
            self.assertIn("3570", observed_command)
            self.assertIn("3630", observed_command)
            self.assertIn("0:v:0", observed_command)
            self.assertIn("0:a?", observed_command)
            self.assertIn("0:s?", observed_command)
            self.assertFalse(output_path.with_suffix(".part.mkv").exists())


if __name__ == "__main__":
    unittest.main()
