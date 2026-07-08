import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bd_to_avp.modules import sub
from bd_to_avp.modules.sub import (
    extract_subtitle_to_srt,
    get_languages_in_mkv,
    mark_forced_srt_files,
    subtitle_language_alpha2,
)


class ForcedSubtitleNamingTests(unittest.TestCase):
    def test_marks_second_same_language_track_as_forced(self) -> None:
        tracks = [
            {"index": 4, "language": "eng", "default": 1, "forced": 0},
            {"index": 5, "language": "eng", "default": 0, "forced": 1},
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir)
            full_srt = output_path / "movie.en.srt"
            forced_srt = output_path / "movie-1.en.srt"
            full_srt.write_text("full", encoding="utf-8")
            forced_srt.write_text("forced", encoding="utf-8")

            mark_forced_srt_files(output_path, tracks)

            self.assertTrue(full_srt.exists())
            self.assertFalse(forced_srt.exists())
            self.assertEqual((output_path / "movie-1.forced.en.srt").read_text(encoding="utf-8"), "forced")

    def test_marks_only_forced_language_track_as_forced(self) -> None:
        tracks = [{"index": 3, "language": "eng", "default": 0, "forced": 1}]

        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir)
            forced_srt = output_path / "movie.en.srt"
            forced_srt.write_text("forced", encoding="utf-8")

            mark_forced_srt_files(output_path, tracks)

            self.assertFalse(forced_srt.exists())
            self.assertEqual((output_path / "movie.forced.en.srt").read_text(encoding="utf-8"), "forced")

    def test_ignores_unknown_language_for_forced_rename(self) -> None:
        tracks = [{"index": 3, "language": "und", "default": 0, "forced": 1}]

        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir)
            subtitle = output_path / "movie.und.srt"
            subtitle.write_text("forced", encoding="utf-8")

            mark_forced_srt_files(output_path, tracks)

            self.assertTrue(subtitle.exists())


class SubtitleLanguageTests(unittest.TestCase):
    def test_iso_639_2_language_converts_to_alpha2(self) -> None:
        self.assertEqual(subtitle_language_alpha2("eng"), "en")

    def test_undefined_language_returns_none(self) -> None:
        self.assertIsNone(subtitle_language_alpha2("und"))

    def test_invalid_language_returns_none(self) -> None:
        self.assertIsNone(subtitle_language_alpha2("xxx"))


class SubtitleStreamDetectionTests(unittest.TestCase):
    def test_language_detection_uses_only_pgs_streams(self) -> None:
        probe = {
            "streams": [
                {
                    "index": 2,
                    "codec_type": "subtitle",
                    "codec_name": "subrip",
                    "tags": {"language": "eng"},
                    "disposition": {"default": 1, "forced": 0},
                },
                {
                    "index": 3,
                    "codec_type": "subtitle",
                    "codec_name": "hdmv_pgs_subtitle",
                    "tags": {"language": "eng"},
                    "disposition": {"default": 0, "forced": 1},
                },
            ]
        }

        with patch.object(sub.ffmpeg, "probe", return_value=probe):
            tracks = get_languages_in_mkv(Path("movie.mkv"))

        self.assertEqual(tracks, [{"index": 3, "language": "eng", "default": 0, "forced": 1}])

    def test_no_subtitle_tracks_continue_without_pgsrip(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch("bd_to_avp.modules.sub.get_languages_in_mkv", return_value=None),
            patch("bd_to_avp.modules.sub.pgsrip.rip") as rip,
            patch.object(sub.config, "skip_subtitles", False),
            patch.object(sub.config, "continue_on_error", False),
        ):
            extract_subtitle_to_srt(Path(temp_dir) / "movie.mkv")

        rip.assert_not_called()


if __name__ == "__main__":
    unittest.main()
