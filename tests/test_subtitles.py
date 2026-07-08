import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bd_to_avp.modules import sub
from bd_to_avp.vendor.pgsrip.media_path import MediaPath
from bd_to_avp.vendor.pgsrip.mkv import MkvTrack
from bd_to_avp.modules.sub import (
    create_srt_from_mkv,
    extract_subtitle_to_srt,
    get_selected_subtitle_tracks,
    get_languages_in_mkv,
    mark_forced_srt_files,
    subtitle_language_alpha2,
)


class ForcedSubtitleNamingTests(unittest.TestCase):
    def test_marks_second_same_language_track_as_forced(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir)
            full_srt = output_path / "movie.en.srt"
            forced_srt = output_path / "movie-1.en.srt"
            full_srt.write_text("full", encoding="utf-8")
            forced_srt.write_text("forced", encoding="utf-8")
            tracks = [
                {"index": 4, "language": "eng", "default": 1, "forced": 0, "srt_path": full_srt},
                {"index": 5, "language": "eng", "default": 0, "forced": 1, "srt_path": forced_srt},
            ]

            mark_forced_srt_files(tracks)

            self.assertTrue(full_srt.exists())
            self.assertFalse(forced_srt.exists())
            self.assertEqual((output_path / "movie-1.forced.en.srt").read_text(encoding="utf-8"), "forced")

    def test_marks_only_forced_language_track_as_forced(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir)
            forced_srt = output_path / "movie.en.srt"
            forced_srt.write_text("forced", encoding="utf-8")
            tracks = [{"index": 3, "language": "eng", "default": 0, "forced": 1, "srt_path": forced_srt}]

            mark_forced_srt_files(tracks)

            self.assertFalse(forced_srt.exists())
            self.assertEqual((output_path / "movie.forced.en.srt").read_text(encoding="utf-8"), "forced")

    def test_marks_forced_path_without_special_casing_language(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir)
            subtitle = output_path / "movie.und.srt"
            subtitle.write_text("forced", encoding="utf-8")
            tracks = [{"index": 3, "language": "und", "default": 0, "forced": 1, "srt_path": subtitle}]

            mark_forced_srt_files(tracks)

            self.assertFalse(subtitle.exists())
            self.assertEqual((output_path / "movie.forced.und.srt").read_text(encoding="utf-8"), "forced")

    def test_marks_forced_track_by_selected_srt_path_not_raw_stream_position(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir)
            forced_srt = output_path / "movie.en.srt"
            shifted_srt = output_path / "movie-1.en.srt"
            forced_srt.write_text("forced", encoding="utf-8")
            shifted_srt.write_text("other", encoding="utf-8")
            tracks = [{"index": 5, "language": "eng", "default": 0, "forced": 1, "srt_path": forced_srt}]

            mark_forced_srt_files(tracks)

            self.assertFalse(forced_srt.exists())
            self.assertTrue(shifted_srt.exists())
            self.assertEqual((output_path / "movie.forced.en.srt").read_text(encoding="utf-8"), "forced")

    def test_marks_digit_ended_basename_without_confusing_numbered_sibling(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir)
            forced_srt = output_path / "Movie-2024.en.srt"
            sibling_srt = output_path / "Movie-2024-1.en.srt"
            forced_srt.write_text("forced", encoding="utf-8")
            sibling_srt.write_text("full", encoding="utf-8")
            tracks = [{"index": 7, "language": "eng", "default": 0, "forced": 1, "srt_path": forced_srt}]

            mark_forced_srt_files(tracks)

            self.assertFalse(forced_srt.exists())
            self.assertTrue(sibling_srt.exists())
            self.assertEqual((output_path / "Movie-2024.forced.en.srt").read_text(encoding="utf-8"), "forced")


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

    def test_no_subtitle_tracks_remove_stale_srt_files(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch("bd_to_avp.modules.sub.get_languages_in_mkv", return_value=None),
            patch("bd_to_avp.modules.sub.pgsrip.rip") as rip,
            patch.object(sub.config, "skip_subtitles", False),
            patch.object(sub.config, "continue_on_error", False),
        ):
            output_path = Path(temp_dir)
            stale_subtitle = output_path / "movie.en.srt"
            stale_subtitle.write_text("stale", encoding="utf-8")

            extract_subtitle_to_srt(output_path / "movie.mkv")

        self.assertFalse(stale_subtitle.exists())
        rip.assert_not_called()

    def test_skip_subtitles_remove_stale_srt_files_when_stage_runs(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(sub.config, "skip_subtitles", True),
            patch.object(sub.config, "start_stage", sub.Stage.EXTRACT_SUBTITLES),
            patch("bd_to_avp.modules.sub.extract_subtitle_to_srt") as extract,
        ):
            output_path = Path(temp_dir)
            stale_subtitle = output_path / "movie.en.srt"
            stale_subtitle.write_text("stale", encoding="utf-8")

            create_srt_from_mkv(output_path / "movie.mkv")

        self.assertFalse(stale_subtitle.exists())
        extract.assert_not_called()

    def test_skip_subtitles_preserves_srt_files_when_subtitle_stage_is_skipped(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(sub.config, "skip_subtitles", True),
            patch.object(sub.config, "start_stage", sub.Stage.CREATE_FINAL_FILE),
            patch("bd_to_avp.modules.sub.extract_subtitle_to_srt") as extract,
        ):
            output_path = Path(temp_dir)
            staged_subtitle = output_path / "movie.en.srt"
            staged_subtitle.write_text("manual", encoding="utf-8")

            create_srt_from_mkv(output_path / "movie.mkv")
            subtitle_still_exists = staged_subtitle.exists()

        self.assertTrue(subtitle_still_exists)
        extract.assert_not_called()


class SelectedSubtitleTrackTests(unittest.TestCase):
    def test_selected_tracks_use_pgsrip_output_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir)
            media_path = output_path / "Movie-2024.mkv"
            media_path.touch()
            mkv_file = sub.Mkv.__new__(sub.Mkv)
            mkv_file.media_path = MediaPath(media_path.as_posix())
            mkv_file.tracks = [
                make_track(2, enabled=False, forced=False),
                make_track(3, enabled=True, forced=False),
                make_track(4, enabled=True, forced=True),
            ]

            tracks = get_selected_subtitle_tracks(mkv_file, sub.Options(overwrite=True, one_per_lang=False))

        self.assertEqual(
            tracks,
            [
                {
                    "index": 3,
                    "language": "en",
                    "forced": 0,
                    "srt_path": output_path / "Movie-2024.en.srt",
                },
                {
                    "index": 4,
                    "language": "en",
                    "forced": 1,
                    "srt_path": output_path / "Movie-2024-1.en.srt",
                },
            ],
        )

    def test_selected_track_metadata_does_not_allocate_pgs_temp_folders(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir)
            media_path = output_path / "Movie.mkv"
            media_path.touch()
            mkv_file = sub.Mkv.__new__(sub.Mkv)
            mkv_file.media_path = MediaPath(media_path.as_posix())
            mkv_file.tracks = [make_track(3, enabled=True, forced=True)]

            with patch.object(MediaPath, "create_temp_folder") as create_temp_folder:
                tracks = get_selected_subtitle_tracks(mkv_file, sub.Options(overwrite=True, one_per_lang=False))

        self.assertEqual(tracks[0]["srt_path"], output_path / "Movie.en.srt")
        create_temp_folder.assert_not_called()

    def test_selected_pgs_medias_still_allocate_for_real_rip_objects(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir)
            media_path = output_path / "Movie.mkv"
            media_path.touch()
            mkv_file = sub.Mkv.__new__(sub.Mkv)
            mkv_file.media_path = MediaPath(media_path.as_posix())
            mkv_file.tracks = [make_track(3, enabled=True, forced=True)]

            with patch.object(MediaPath, "create_temp_folder", return_value=temp_dir) as create_temp_folder:
                medias = list(mkv_file.get_selected_pgs_medias(sub.Options(overwrite=True, one_per_lang=False)))

        self.assertEqual(len(medias), 1)
        create_temp_folder.assert_called_once()

    def test_existing_first_srt_does_not_skip_later_numbered_track(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir)
            media_path = output_path / "Movie.mkv"
            media_path.touch()
            (output_path / "Movie.en.srt").write_text("existing", encoding="utf-8")
            mkv_file = sub.Mkv.__new__(sub.Mkv)
            mkv_file.media_path = MediaPath(media_path.as_posix())
            mkv_file.tracks = [
                make_track(3, enabled=True, forced=False),
                make_track(4, enabled=True, forced=True),
            ]

            tracks = get_selected_subtitle_tracks(mkv_file, sub.Options(overwrite=False, one_per_lang=False))

        self.assertEqual(
            tracks,
            [
                {
                    "index": 4,
                    "language": "en",
                    "forced": 1,
                    "srt_path": output_path / "Movie-1.en.srt",
                }
            ],
        )

    def test_existing_numbered_srt_skips_later_track_without_temp_allocation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir)
            media_path = output_path / "Movie.mkv"
            media_path.touch()
            (output_path / "Movie-1.en.srt").write_text("existing", encoding="utf-8")
            mkv_file = sub.Mkv.__new__(sub.Mkv)
            mkv_file.media_path = MediaPath(media_path.as_posix())
            mkv_file.tracks = [
                make_track(3, enabled=True, forced=False),
                make_track(4, enabled=True, forced=True),
            ]

            with patch.object(MediaPath, "create_temp_folder") as create_temp_folder:
                tracks = get_selected_subtitle_tracks(mkv_file, sub.Options(overwrite=False, one_per_lang=False))

        self.assertEqual(
            tracks, [{"index": 3, "language": "en", "forced": 0, "srt_path": output_path / "Movie.en.srt"}]
        )
        create_temp_folder.assert_not_called()

    def test_pgs_srt_path_preserves_selected_track_number(self) -> None:
        media_path = MediaPath("Movie.mkv")
        pgs = sub.MkvPgs.__new__(sub.MkvPgs)
        pgs.media_path = media_path.translate(language=sub.Language("eng"), number=1)

        self.assertEqual(Path(str(pgs.srt_path)), Path("Movie-1.en.srt"))


def make_track(track_id: int, *, enabled: bool, forced: bool, language: str = "eng") -> MkvTrack:
    return MkvTrack(
        {
            "id": track_id,
            "type": "subtitles",
            "codec": "HDMV PGS",
            "properties": {
                "enabled_track": enabled,
                "forced_track": forced,
                "language_ietf": language,
            },
        }
    )


if __name__ == "__main__":
    unittest.main()
