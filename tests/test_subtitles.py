import tempfile
import unittest
from contextlib import chdir
from pathlib import Path
from typing import Any
from unittest.mock import patch

from bd_to_avp.modules import sub
from bd_to_avp.observability import (
    ObservabilityContext,
    ObservabilityEmitter,
    ObservabilityEvent,
    ObservabilityPrivacy,
    ObservabilitySeverity,
    ObservabilityStage,
)
from bd_to_avp.process_runner import ProcessCancelled
from bd_to_avp.runtime import ObservabilityStream, RunContext
from bd_to_avp.vendor.pgsrip.media_path import MediaPath
from bd_to_avp.vendor.pgsrip.mkv import MkvTrack
from bd_to_avp.modules.sub import (
    create_srt_from_mkv,
    extract_subtitle_to_srt,
    get_selected_subtitle_tracks,
    get_languages_in_mkv,
    mark_forced_srt_files,
    subtitle_rip_options,
    subtitle_language_alpha2,
)


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[ObservabilityEvent] = []

    def emit(self, event: ObservabilityEvent) -> None:
        self.events.append(event)


def make_run_context() -> tuple[RunContext, RecordingSink]:
    sink = RecordingSink()
    stream = ObservabilityStream(ObservabilityEmitter.APP, sink)
    return RunContext(observability=stream), sink


def subtitle_terminal_events(sink: RecordingSink) -> list[ObservabilityEvent]:
    terminal_kinds = {
        "subtitle.extract.completed",
        "subtitle.extract.failed",
        "subtitle.extract.cancelled",
    }
    return [event for event in sink.events if event.kind in terminal_kinds]


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

    def test_bibliographic_alias_converts_to_alpha2(self) -> None:
        self.assertEqual(subtitle_language_alpha2("dut"), "nl")


class SubtitleRipOptionsTests(unittest.TestCase):
    def test_remove_extra_languages_limits_pgsrip_to_configured_language(self) -> None:
        with (
            patch.object(sub.config, "remove_extra_languages", True),
            patch.object(sub.config, "language_code", "eng"),
            patch.object(sub.config, "keep_files", False),
        ):
            options = subtitle_rip_options()

        self.assertEqual({str(language) for language in options.languages}, {"en"})

    def test_keep_extra_languages_leaves_pgsrip_unfiltered(self) -> None:
        with (
            patch.object(sub.config, "remove_extra_languages", False),
            patch.object(sub.config, "language_code", "eng"),
            patch.object(sub.config, "keep_files", False),
        ):
            options = subtitle_rip_options()

        self.assertEqual(options.languages, set())

    def test_bibliographic_alias_filters_with_canonical_language(self) -> None:
        with (
            patch.object(sub.config, "remove_extra_languages", True),
            patch.object(sub.config, "language_code", "ger"),
            patch.object(sub.config, "keep_files", False),
        ):
            options = subtitle_rip_options()

        self.assertEqual({language.alpha3t for language in options.languages}, {"deu"})

    def test_alpha3_only_language_filters_without_alpha2_code(self) -> None:
        with (
            patch.object(sub.config, "remove_extra_languages", True),
            patch.object(sub.config, "language_code", "ace"),
            patch.object(sub.config, "keep_files", False),
        ):
            options = subtitle_rip_options()

        self.assertEqual({language.alpha3t for language in options.languages}, {"ace"})


class SubtitleStreamDetectionTests(unittest.TestCase):
    def test_missing_streams_returns_no_subtitles(self) -> None:
        with patch.object(sub, "run_ffprobe", return_value={}):
            tracks = get_languages_in_mkv(Path("movie.mkv"))

        self.assertIsNone(tracks)

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

        with patch.object(sub, "run_ffprobe", return_value=probe):
            tracks = get_languages_in_mkv(Path("movie.mkv"))

        self.assertEqual(tracks, [{"index": 3, "language": "eng", "default": 0, "forced": 1}])

    def test_language_detection_normalizes_bibliographic_metadata(self) -> None:
        probe = {
            "streams": [
                {
                    "index": 3,
                    "codec_type": "subtitle",
                    "codec_name": "hdmv_pgs_subtitle",
                    "tags": {"language": "ger"},
                    "disposition": {"default": 0, "forced": 0},
                }
            ]
        }

        with patch.object(sub, "run_ffprobe", return_value=probe):
            tracks = get_languages_in_mkv(Path("movie.mkv"))

        self.assertEqual(tracks, [{"index": 3, "language": "deu", "default": 0, "forced": 0}])

    def test_missing_empty_and_unknown_language_tags_become_undetermined(self) -> None:
        probe = {
            "streams": [
                {
                    "index": 2,
                    "codec_type": "subtitle",
                    "codec_name": "hdmv_pgs_subtitle",
                    "disposition": {"default": 0, "forced": 0},
                },
                {
                    "index": 3,
                    "codec_type": "subtitle",
                    "codec_name": "hdmv_pgs_subtitle",
                    "tags": {"language": ""},
                    "disposition": {"default": 0, "forced": 0},
                },
                {
                    "index": 4,
                    "codec_type": "subtitle",
                    "codec_name": "hdmv_pgs_subtitle",
                    "tags": {"language": "xyz"},
                    "disposition": {"default": 0, "forced": 0},
                },
            ]
        }

        with patch.object(sub, "run_ffprobe", return_value=probe):
            tracks = get_languages_in_mkv(Path("movie.mkv"))

        self.assertEqual([track["language"] for track in tracks or []], ["und", "und", "und"])

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
        warnings: list[str] = []
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

            extract_subtitle_to_srt(output_path / "movie.mkv", warning_handler=warnings.append)

        self.assertFalse(stale_subtitle.exists())
        rip.assert_not_called()
        self.assertEqual(warnings, ["No PGS subtitle tracks found in source; continuing without subtitles."])

    def test_missing_preferred_language_continues_without_subtitles(self) -> None:
        warnings: list[str] = []
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch("bd_to_avp.modules.sub.get_languages_in_mkv", return_value=[{"index": 3, "language": "eng"}]),
            patch.object(sub.config, "skip_subtitles", False),
            patch.object(sub.config, "remove_extra_languages", True),
            patch.object(sub.config, "language_code", "dut"),
            patch.object(sub.config, "continue_on_error", False),
            patch("bd_to_avp.modules.sub.Mkv"),
            patch("bd_to_avp.modules.sub.get_selected_subtitle_tracks", return_value=[]),
            patch("bd_to_avp.modules.sub.pgsrip.rip") as rip,
        ):
            extract_subtitle_to_srt(Path(temp_dir) / "movie.mkv", warning_handler=warnings.append)

        rip.assert_not_called()
        self.assertEqual(
            warnings,
            ["No PGS subtitle tracks matched the preferred language Dutch (nld); continuing without subtitles."],
        )

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

    def test_skip_subtitles_uses_explicit_output_folder_not_source_folder(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(sub.config, "skip_subtitles", True),
            patch.object(sub.config, "start_stage", sub.Stage.EXTRACT_SUBTITLES),
            patch("bd_to_avp.modules.sub.extract_subtitle_to_srt") as extract,
        ):
            temp_path = Path(temp_dir)
            source_folder = temp_path / "source"
            output_folder = temp_path / "output"
            source_folder.mkdir()
            output_folder.mkdir()
            source_subtitle = source_folder / "movie.en.srt"
            output_subtitle = output_folder / "movie.en.srt"
            source_subtitle.write_text("manual", encoding="utf-8")
            output_subtitle.write_text("stale", encoding="utf-8")

            create_srt_from_mkv(source_folder / "movie.mkv", output_folder)

            source_subtitle_exists = source_subtitle.exists()
            output_subtitle_exists = output_subtitle.exists()

        self.assertTrue(source_subtitle_exists)
        self.assertFalse(output_subtitle_exists)
        extract.assert_not_called()

    def test_subtitle_extraction_aliases_direct_source_into_output_folder(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch("bd_to_avp.modules.sub.get_languages_in_mkv", return_value=[{"index": 3, "language": "eng"}]),
            patch.object(sub.config, "skip_subtitles", False),
            patch.object(sub.config, "continue_on_error", False),
            patch("bd_to_avp.modules.sub.Mkv") as mkv_class,
            patch(
                "bd_to_avp.modules.sub.get_selected_subtitle_tracks",
                return_value=[
                    {
                        "index": 3,
                        "language": "eng",
                        "forced": 0,
                        "srt_path": Path(temp_dir) / "output" / "movie.en.srt",
                    }
                ],
            ),
            patch("bd_to_avp.modules.sub.mark_forced_srt_files") as mark_forced,
        ):
            temp_path = Path(temp_dir)
            source_folder = temp_path / "source"
            output_folder = temp_path / "output"
            source_folder.mkdir()
            output_folder.mkdir()
            source_mkv = source_folder / "movie.mkv"
            source_mkv.write_bytes(b"mkv")

            def write_srt(mkv_file, _options):
                Path(str(mkv_file.media_path)).with_suffix(".en.srt").write_text("subtitle", encoding="utf-8")
                return 1

            with patch("bd_to_avp.modules.sub.pgsrip.rip", side_effect=write_srt) as rip:
                mkv_class.side_effect = lambda path, **_kwargs: type("MkvStub", (), {"media_path": Path(path)})()

                extract_subtitle_to_srt(source_mkv, output_folder)

            created_srt = output_folder / "movie.en.srt"
            self.assertTrue(created_srt.exists())
            self.assertEqual(created_srt.read_text(encoding="utf-8"), "subtitle")
            self.assertFalse((source_folder / "movie.en.srt").exists())
            self.assertFalse((output_folder / "movie.mkv").exists())
            rip.assert_called_once()
            selected_tracks = mark_forced.call_args.args[0]
            self.assertEqual(len(selected_tracks), 1)
            self.assertEqual(selected_tracks[0]["index"], 3)
            mark_forced.assert_called_once_with(selected_tracks, None)

    def test_unreadable_pgs_tracks_warn_and_remove_partial_subtitles(self) -> None:
        run_context, sink = make_run_context()
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch("bd_to_avp.modules.sub.get_languages_in_mkv", return_value=[{"index": 3, "language": "eng"}]),
            patch.object(sub.config, "skip_subtitles", False),
            patch("bd_to_avp.modules.sub.Mkv") as mkv_class,
            patch(
                "bd_to_avp.modules.sub.get_selected_subtitle_tracks",
                return_value=[
                    {
                        "index": 3,
                        "language": "eng",
                        "forced": 0,
                        "srt_path": Path(temp_dir) / "movie.en.srt",
                    }
                ],
            ),
        ):
            output_path = Path(temp_dir)
            source_mkv = output_path / "movie.mkv"
            source_mkv.write_bytes(b"mkv")
            partial_srt = output_path / "movie.en.srt"
            warnings: list[str] = []
            mkv_class.side_effect = lambda path, **_kwargs: type("MkvStub", (), {"media_path": Path(path)})()

            def produce_no_usable_subtitles(_mkv_file, _options):
                partial_srt.write_text("partial", encoding="utf-8")
                return 0

            with patch("bd_to_avp.modules.sub.pgsrip.rip", side_effect=produce_no_usable_subtitles):
                extract_subtitle_to_srt(source_mkv, output_path, warnings.append, run_context=run_context)

            self.assertFalse(partial_srt.exists())
            self.assertEqual(
                warnings,
                ["PGS subtitle extraction did not produce usable subtitle files; continuing without subtitles."],
            )
            self.assertEqual(len(subtitle_terminal_events(sink)), 1)
            failed = subtitle_terminal_events(sink)[0]
            self.assertIsNotNone(failed.data.failure)
            assert failed.data.failure is not None
            self.assertEqual(failed.data.failure.code, "subtitle_rip_no_output")

    def test_pgs_extraction_exception_warns_and_removes_partial_subtitles(self) -> None:
        run_context, sink = make_run_context()
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch("bd_to_avp.modules.sub.get_languages_in_mkv", return_value=[{"index": 3, "language": "eng"}]),
            patch.object(sub.config, "skip_subtitles", False),
            patch("bd_to_avp.modules.sub.Mkv") as mkv_class,
            patch(
                "bd_to_avp.modules.sub.get_selected_subtitle_tracks",
                return_value=[
                    {
                        "index": 3,
                        "language": "eng",
                        "forced": 0,
                        "srt_path": Path(temp_dir) / "movie.en.srt",
                    }
                ],
            ),
        ):
            output_path = Path(temp_dir)
            source_mkv = output_path / "movie.mkv"
            source_mkv.write_bytes(b"mkv")
            partial_srt = output_path / "movie.en.srt"
            partial_srt.write_text("partial", encoding="utf-8")
            warnings: list[str] = []
            mkv_class.side_effect = lambda path, **_kwargs: type("MkvStub", (), {"media_path": Path(path)})()

            with patch("bd_to_avp.modules.sub.pgsrip.rip", side_effect=ValueError("malformed PGS stream")):
                extract_subtitle_to_srt(source_mkv, output_path, warnings.append, run_context=run_context)

            self.assertFalse(partial_srt.exists())
            self.assertEqual(
                warnings,
                ["PGS subtitle extraction failed; continuing without subtitles. (malformed PGS stream)"],
            )
            self.assertEqual(len(subtitle_terminal_events(sink)), 1)
            failed = subtitle_terminal_events(sink)[0]
            self.assertIsNotNone(failed.data.failure)
            assert failed.data.failure is not None
            self.assertEqual(failed.data.failure.code, "subtitle_rip_failed")
            self.assertNotIn("malformed PGS stream", failed.to_json_line())

    def test_subtitle_extraction_preserves_cancellation(self) -> None:
        run_context, sink = make_run_context()
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch("bd_to_avp.modules.sub.get_languages_in_mkv", return_value=[{"index": 3, "language": "eng"}]),
            patch.object(sub.config, "skip_subtitles", False),
            patch("bd_to_avp.modules.sub.Mkv") as mkv_class,
            patch("bd_to_avp.modules.sub.get_selected_subtitle_tracks", return_value=[]),
            patch("bd_to_avp.modules.sub.pgsrip.rip", side_effect=ProcessCancelled("cancelled")),
        ):
            output_path = Path(temp_dir)
            source_mkv = output_path / "movie.mkv"
            source_mkv.write_bytes(b"mkv")
            mkv_class.side_effect = lambda path, **_kwargs: type("MkvStub", (), {"media_path": Path(path)})()

            with self.assertRaisesRegex(ProcessCancelled, "cancelled"):
                extract_subtitle_to_srt(source_mkv, output_path, run_context=run_context)

        cancelled = [event for event in sink.events if event.kind == "subtitle.extract.cancelled"]
        self.assertEqual(len(cancelled), 1)
        self.assertIsNotNone(cancelled[0].data.cancellation)
        assert cancelled[0].data.cancellation is not None
        self.assertIsNone(cancelled[0].data.cancellation.forced)
        self.assertIsNotNone(cancelled[0].data.progress)
        assert cancelled[0].data.progress is not None
        self.assertEqual(cancelled[0].data.progress.completed_units, 0.0)
        self.assertIsNone(cancelled[0].data.progress.total_units)
        self.assertEqual(len(subtitle_terminal_events(sink)), 1)

    def test_subtitle_source_alias_uses_absolute_target_for_relative_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            source_folder = temp_path / "source"
            output_folder = temp_path / "output"
            source_folder.mkdir()
            output_folder.mkdir()
            source_mkv = source_folder / "movie.mkv"
            source_mkv.write_bytes(b"mkv")

            with chdir(temp_path), sub.subtitle_source_alias(Path("source/movie.mkv"), output_folder) as alias_path:
                self.assertTrue(alias_path.is_symlink())
                self.assertEqual(alias_path.resolve(strict=True), source_mkv.resolve(strict=True))

            self.assertFalse(alias_path.is_symlink())

    def test_subtitle_source_alias_reuses_media_already_in_output_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_folder = Path(temp_dir)
            source_mkv = output_folder / "movie.mkv"
            source_mkv.write_bytes(b"mkv")

            with sub.subtitle_source_alias(source_mkv, output_folder.resolve()) as alias_path:
                self.assertEqual(alias_path, source_mkv)
                self.assertFalse(alias_path.is_symlink())

            self.assertTrue(source_mkv.exists())

    def test_subtitle_source_alias_cleans_existing_matching_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            source_folder = temp_path / "source"
            output_folder = temp_path / "output"
            source_folder.mkdir()
            output_folder.mkdir()
            source_mkv = source_folder / "movie.mkv"
            source_mkv.write_bytes(b"mkv")
            stale_alias = output_folder / "movie.mkv"
            stale_alias.symlink_to(source_mkv)

            with sub.subtitle_source_alias(source_mkv, output_folder) as alias_path:
                self.assertEqual(alias_path, stale_alias)
                self.assertTrue(alias_path.is_symlink())

            self.assertFalse(stale_alias.is_symlink())

    def test_subtitle_source_alias_avoids_stale_broken_alias(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            source_folder = temp_path / "source"
            output_folder = temp_path / "output"
            source_folder.mkdir()
            output_folder.mkdir()
            source_mkv = source_folder / "movie.mkv"
            source_mkv.write_bytes(b"mkv")
            stale_alias = output_folder / "movie.mkv"
            stale_alias.symlink_to(output_folder / "missing.mkv")

            with sub.subtitle_source_alias(source_mkv, output_folder) as alias_path:
                self.assertEqual(alias_path, stale_alias)
                self.assertTrue(alias_path.is_symlink())
                self.assertEqual(alias_path.resolve(strict=True), source_mkv.resolve(strict=True))

            self.assertFalse(stale_alias.is_symlink())

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


class SubtitleObservabilityTests(unittest.TestCase):
    def test_keyboard_interrupt_closes_started_event_as_cancelled(self) -> None:
        run_context, sink = make_run_context()
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch("bd_to_avp.modules.sub.get_languages_in_mkv", return_value=[{"index": 3, "language": "eng"}]),
            patch.object(sub.config, "skip_subtitles", False),
            patch("bd_to_avp.modules.sub.Mkv"),
            patch(
                "bd_to_avp.modules.sub.get_selected_subtitle_tracks",
                return_value=[{"index": 3, "language": "eng", "forced": 0, "srt_path": Path(temp_dir) / "m.en.srt"}],
            ),
            patch("bd_to_avp.modules.sub.pgsrip.rip", side_effect=KeyboardInterrupt),
        ):
            with self.assertRaises(KeyboardInterrupt):
                extract_subtitle_to_srt(Path(temp_dir) / "movie.mkv", run_context=run_context)

        terminal = subtitle_terminal_events(sink)
        self.assertEqual(len(terminal), 1)
        self.assertEqual(terminal[0].kind, "subtitle.extract.cancelled")

    def test_started_event_emitted_before_rip(self) -> None:
        run_context, sink = make_run_context()
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch("bd_to_avp.modules.sub.get_languages_in_mkv", return_value=[{"index": 3, "language": "eng"}]),
            patch.object(sub.config, "skip_subtitles", False),
            patch.object(sub.config, "continue_on_error", True),
            patch("bd_to_avp.modules.sub.Mkv"),
            patch(
                "bd_to_avp.modules.sub.get_selected_subtitle_tracks",
                return_value=[{"index": 3, "language": "eng", "forced": 0, "srt_path": Path(temp_dir) / "m.en.srt"}],
            ),
            patch("bd_to_avp.modules.sub.pgsrip.rip", return_value=0),
            patch("bd_to_avp.modules.sub.mark_forced_srt_files"),
        ):
            extract_subtitle_to_srt(Path(temp_dir) / "movie.mkv", run_context=run_context)

        kinds = [e.kind for e in sink.events]
        self.assertIn("subtitle.extract.started", kinds)
        started = next(e for e in sink.events if e.kind == "subtitle.extract.started")
        self.assertIsNotNone(started.data.progress)
        assert started.data.progress is not None
        self.assertEqual(started.data.progress.total_units, 1.0)
        self.assertEqual(started.data.progress.unit, "tracks")

    def test_completed_event_emitted_after_successful_rip(self) -> None:
        run_context, sink = make_run_context()
        observability_context = ObservabilityContext(stage=ObservabilityStage("extract_subtitles"))
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch("bd_to_avp.modules.sub.get_languages_in_mkv", return_value=[{"index": 3, "language": "eng"}]),
            patch.object(sub.config, "skip_subtitles", False),
            patch.object(sub.config, "continue_on_error", True),
            patch("bd_to_avp.modules.sub.Mkv"),
            patch(
                "bd_to_avp.modules.sub.get_selected_subtitle_tracks",
                return_value=[{"index": 3, "language": "eng", "forced": 0, "srt_path": Path(temp_dir) / "m.en.srt"}],
            ),
            patch("bd_to_avp.modules.sub.mark_forced_srt_files"),
        ):
            output_path = Path(temp_dir)
            srt_file = output_path / "movie.en.srt"

            def write_srt(*_args: Any) -> int:
                srt_file.write_text("subtitle data", encoding="utf-8")
                return 1

            with patch("bd_to_avp.modules.sub.pgsrip.rip", side_effect=write_srt):
                extract_subtitle_to_srt(
                    output_path / "private-title.mkv",
                    output_path,
                    run_context=run_context,
                    observability_context=observability_context,
                )

        kinds = [e.kind for e in sink.events]
        self.assertIn("subtitle.extract.completed", kinds)
        completed = next(e for e in sink.events if e.kind == "subtitle.extract.completed")
        self.assertIsNotNone(completed.data.progress)
        assert completed.data.progress is not None
        self.assertEqual(completed.data.progress.completed_units, 1.0)
        self.assertEqual(completed.data.progress.total_units, 1.0)
        self.assertEqual(len(subtitle_terminal_events(sink)), 1)
        self.assertEqual(completed.context, observability_context)
        self.assertTrue(all(event.privacy is ObservabilityPrivacy.PUBLIC for event in sink.events))
        serialized_events = "\n".join(event.to_json_line() for event in sink.events)
        self.assertNotIn(temp_dir, serialized_events)
        self.assertNotIn("private-title", serialized_events)

    def test_partial_warning_emitted_when_empty_srts_produced(self) -> None:
        run_context, sink = make_run_context()
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch("bd_to_avp.modules.sub.get_languages_in_mkv", return_value=[{"index": 3, "language": "eng"}]),
            patch.object(sub.config, "skip_subtitles", False),
            patch.object(sub.config, "continue_on_error", True),
            patch("bd_to_avp.modules.sub.Mkv"),
            patch(
                "bd_to_avp.modules.sub.get_selected_subtitle_tracks",
                return_value=[
                    {"index": 3, "language": "eng", "forced": 0, "srt_path": Path(temp_dir) / "m.en.srt"},
                    {"index": 4, "language": "eng", "forced": 0, "srt_path": Path(temp_dir) / "m-1.en.srt"},
                ],
            ),
            patch("bd_to_avp.modules.sub.mark_forced_srt_files"),
        ):
            output_path = Path(temp_dir)
            good_srt = output_path / "movie.en.srt"
            empty_srt = output_path / "movie-1.en.srt"

            def write_srts(*_args: Any) -> int:
                good_srt.write_text("subtitle data", encoding="utf-8")
                empty_srt.write_bytes(b"")
                return 2

            with patch("bd_to_avp.modules.sub.pgsrip.rip", side_effect=write_srts):
                extract_subtitle_to_srt(output_path / "movie.mkv", output_path, run_context=run_context)

        warning_events = [e for e in sink.events if e.kind == "subtitle.extract.partial_output"]
        self.assertEqual(len(warning_events), 1)
        warning = warning_events[0]
        self.assertEqual(warning.severity, ObservabilitySeverity.WARNING)
        self.assertIsNotNone(warning.data.message)
        assert warning.data.message is not None
        self.assertIn("1 of 2", warning.data.message.value)
        self.assertIsNotNone(warning.data.progress)
        assert warning.data.progress is not None
        self.assertEqual(warning.data.progress.completed_units, 1.0)
        self.assertEqual(warning.data.progress.total_units, 2.0)

    def test_partial_warning_emitted_when_ripper_skips_a_selected_track(self) -> None:
        run_context, sink = make_run_context()
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch("bd_to_avp.modules.sub.get_languages_in_mkv", return_value=[{"index": 3, "language": "eng"}]),
            patch.object(sub.config, "skip_subtitles", False),
            patch.object(sub.config, "continue_on_error", True),
            patch("bd_to_avp.modules.sub.Mkv"),
            patch(
                "bd_to_avp.modules.sub.get_selected_subtitle_tracks",
                return_value=[
                    {"index": 3, "language": "eng", "forced": 0, "srt_path": Path(temp_dir) / "m.en.srt"},
                    {"index": 4, "language": "eng", "forced": 0, "srt_path": Path(temp_dir) / "m-1.en.srt"},
                ],
            ),
            patch("bd_to_avp.modules.sub.mark_forced_srt_files"),
        ):
            output_path = Path(temp_dir)

            def write_one_srt(*_args: Any) -> int:
                (output_path / "movie.en.srt").write_text("subtitle data", encoding="utf-8")
                return 1

            with patch("bd_to_avp.modules.sub.pgsrip.rip", side_effect=write_one_srt):
                extract_subtitle_to_srt(output_path / "movie.mkv", output_path, run_context=run_context)

        warning = next(event for event in sink.events if event.kind == "subtitle.extract.partial_output")
        self.assertEqual(warning.severity, ObservabilitySeverity.WARNING)
        self.assertIsNotNone(warning.data.progress)
        assert warning.data.progress is not None
        self.assertEqual(warning.data.progress.completed_units, 1.0)
        self.assertEqual(warning.data.progress.total_units, 2.0)

    def test_empty_output_emits_failure_before_strict_error(self) -> None:
        run_context, sink = make_run_context()
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch("bd_to_avp.modules.sub.get_languages_in_mkv", return_value=[{"index": 3, "language": "eng"}]),
            patch.object(sub.config, "skip_subtitles", False),
            patch.object(sub.config, "continue_on_error", False),
            patch("bd_to_avp.modules.sub.Mkv"),
            patch(
                "bd_to_avp.modules.sub.get_selected_subtitle_tracks",
                return_value=[{"index": 3, "language": "eng", "forced": 0, "srt_path": Path(temp_dir) / "m.en.srt"}],
            ),
        ):
            output_path = Path(temp_dir)

            def write_empty_srt(*_args: Any) -> int:
                (output_path / "movie.en.srt").write_bytes(b"")
                return 1

            with (
                patch("bd_to_avp.modules.sub.pgsrip.rip", side_effect=write_empty_srt),
                self.assertRaises(sub.SRTCreationError),
            ):
                extract_subtitle_to_srt(output_path / "movie.mkv", output_path, run_context=run_context)

        self.assertEqual(len(subtitle_terminal_events(sink)), 1)
        failed = subtitle_terminal_events(sink)[0]
        self.assertIsNotNone(failed.data.failure)
        assert failed.data.failure is not None
        self.assertEqual(failed.data.failure.code, "subtitle_empty_output")

    def test_postprocess_error_closes_started_event_with_failure(self) -> None:
        run_context, sink = make_run_context()
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch("bd_to_avp.modules.sub.get_languages_in_mkv", return_value=[{"index": 3, "language": "eng"}]),
            patch.object(sub.config, "skip_subtitles", False),
            patch.object(sub.config, "continue_on_error", True),
            patch("bd_to_avp.modules.sub.Mkv"),
            patch(
                "bd_to_avp.modules.sub.get_selected_subtitle_tracks",
                return_value=[{"index": 3, "language": "eng", "forced": 1, "srt_path": Path(temp_dir) / "m.en.srt"}],
            ),
            patch("bd_to_avp.modules.sub.mark_forced_srt_files", side_effect=OSError("rename failed")),
        ):
            output_path = Path(temp_dir)

            def write_srt(*_args: Any) -> int:
                (output_path / "movie.en.srt").write_text("subtitle data", encoding="utf-8")
                return 1

            with (
                patch("bd_to_avp.modules.sub.pgsrip.rip", side_effect=write_srt),
                self.assertRaisesRegex(OSError, "rename failed"),
            ):
                extract_subtitle_to_srt(output_path / "movie.mkv", output_path, run_context=run_context)

        self.assertEqual(len(subtitle_terminal_events(sink)), 1)
        failed = subtitle_terminal_events(sink)[0]
        self.assertIsNotNone(failed.data.failure)
        assert failed.data.failure is not None
        self.assertEqual(failed.data.failure.code, "subtitle_postprocess_failed")
        self.assertIsNotNone(failed.data.progress)
        assert failed.data.progress is not None
        self.assertEqual(failed.data.progress.completed_units, 1.0)
        self.assertNotIn("rename failed", failed.to_json_line())

    def test_no_partial_warning_when_all_tracks_succeed(self) -> None:
        run_context, sink = make_run_context()
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch("bd_to_avp.modules.sub.get_languages_in_mkv", return_value=[{"index": 3, "language": "eng"}]),
            patch.object(sub.config, "skip_subtitles", False),
            patch.object(sub.config, "continue_on_error", True),
            patch("bd_to_avp.modules.sub.Mkv"),
            patch(
                "bd_to_avp.modules.sub.get_selected_subtitle_tracks",
                return_value=[{"index": 3, "language": "eng", "forced": 0, "srt_path": Path(temp_dir) / "m.en.srt"}],
            ),
            patch("bd_to_avp.modules.sub.mark_forced_srt_files"),
        ):
            output_path = Path(temp_dir)
            srt_file = output_path / "movie.en.srt"

            def write_srt(*_args: Any) -> int:
                srt_file.write_text("subtitle data", encoding="utf-8")
                return 1

            with patch("bd_to_avp.modules.sub.pgsrip.rip", side_effect=write_srt):
                extract_subtitle_to_srt(output_path / "movie.mkv", output_path, run_context=run_context)

        warning_events = [e for e in sink.events if e.kind == "subtitle.extract.partial_output"]
        self.assertEqual(len(warning_events), 0)

    def test_no_events_emitted_without_run_context(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch("bd_to_avp.modules.sub.get_languages_in_mkv", return_value=[{"index": 3, "language": "eng"}]),
            patch.object(sub.config, "skip_subtitles", False),
            patch.object(sub.config, "continue_on_error", True),
            patch("bd_to_avp.modules.sub.Mkv"),
            patch(
                "bd_to_avp.modules.sub.get_selected_subtitle_tracks",
                return_value=[{"index": 3, "language": "eng", "forced": 0, "srt_path": Path(temp_dir) / "m.en.srt"}],
            ),
            patch("bd_to_avp.modules.sub.mark_forced_srt_files"),
            patch.object(RunContext, "emit") as emit,
        ):
            output_path = Path(temp_dir)

            def write_srt(*_args: Any) -> int:
                (output_path / "movie.en.srt").write_text("subtitle data", encoding="utf-8")
                return 1

            with patch("bd_to_avp.modules.sub.pgsrip.rip", side_effect=write_srt):
                extract_subtitle_to_srt(output_path / "movie.mkv", output_path, run_context=None)

        emit.assert_not_called()


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
