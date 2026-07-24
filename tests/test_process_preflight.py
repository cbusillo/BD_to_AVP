import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from bd_to_avp import preflight
from bd_to_avp.modules import process
from bd_to_avp.modules.audio_mode import AudioMode
from bd_to_avp.modules.config import is_direct_pipeline_source_reused, Stage
from bd_to_avp.modules.disc import MKVCreationError
from bd_to_avp.modules.video_mode import VideoMode
from bd_to_avp.modules.video_route import ResolvedVideoRoute, VideoRouteKind
from bd_to_avp.modules.file import (
    move_file_to_output_root_folder,
    prepare_output_folder_for_source,
    remove_output_folder_if_safe,
)
from bd_to_avp.observability import ObservabilityEmitter
from bd_to_avp.runtime import CancellationToken, ObservabilityStream, RunContext
from bd_to_avp.worker.protocol import VideoRouteIntent


class ProcessPreflightTests(unittest.TestCase):
    def test_direct_route_uses_stage_four_and_omits_stage_five(self) -> None:
        route = ResolvedVideoRoute(
            intent=VideoRouteIntent.AUTOMATIC,
            selected=VideoRouteKind.DIRECT_MV_HEVC,
            reason="direct_eligible",
            output_mode=VideoMode.MV_HEVC,
            direct_bitrate_mbps=20,
        )
        with (
            patch.object(process.config, "preview_range", None),
            patch.object(process.config, "skip_subtitles", True),
            patch.object(process.config, "fx_upscale", False),
            patch.object(process.config, "audio_mode", AudioMode.PCM),
            patch.object(process.config, "video_mode", VideoMode.MV_HEVC),
            patch.object(process.config, "start_stage", Stage.CREATE_MKV),
        ):
            plan = process.conversion_stage_plan(route)

        self.assertIn("create_left_right_files", plan)
        self.assertNotIn("combine_to_mv_hevc", plan)

    def test_conversion_stage_plan_tracks_optional_work(self) -> None:
        with (
            patch.object(process.config, "preview_range", None),
            patch.object(process.config, "skip_subtitles", False),
            patch.object(process.config, "fx_upscale", False),
            patch.object(process.config, "audio_mode", AudioMode.CONVERT_AAC),
            patch.object(process.config, "video_mode", VideoMode.MV_HEVC),
            patch.object(process.config, "start_stage", Stage.CREATE_MKV),
        ):
            default_plan = process.conversion_stage_plan()

        self.assertEqual(
            default_plan,
            (
                "configure",
                "preflight",
                "inspect_source",
                "create_mkv",
                "probe_color",
                "detect_crop",
                "extract_mvc_and_audio",
                "extract_subtitles",
                "create_left_right_files",
                "combine_to_mv_hevc",
                "transcode_audio",
                "create_final_file",
                "move_files",
            ),
        )

        with (
            patch.object(process.config, "preview_range", Mock()),
            patch.object(process.config, "skip_subtitles", True),
            patch.object(process.config, "fx_upscale", True),
            patch.object(process.config, "audio_mode", AudioMode.PCM),
            patch.object(process.config, "video_mode", VideoMode.MV_HEVC),
            patch.object(process.config, "start_stage", Stage.CREATE_MKV),
        ):
            optional_plan = process.conversion_stage_plan()

        self.assertIn("prepare_preview_range", optional_plan)
        self.assertIn("upscale_video", optional_plan)
        self.assertNotIn("extract_subtitles", optional_plan)
        self.assertNotIn("transcode_audio", optional_plan)

        with (
            patch.object(process.config, "preview_range", None),
            patch.object(process.config, "skip_subtitles", False),
            patch.object(process.config, "fx_upscale", False),
            patch.object(process.config, "audio_mode", AudioMode.PCM),
            patch.object(process.config, "video_mode", VideoMode.AV1_SBS),
            patch.object(process.config, "start_stage", Stage.CREATE_MKV),
        ):
            av1_plan = process.conversion_stage_plan()

        self.assertIn("encode_av1_stereo", av1_plan)
        self.assertIn("finalize_av1_stereo", av1_plan)
        self.assertNotIn("create_left_right_files", av1_plan)
        self.assertNotIn("combine_to_mv_hevc", av1_plan)

    def test_conversion_stage_plan_excludes_completed_recovery_stages(self) -> None:
        with (
            patch.object(process.config, "preview_range", None),
            patch.object(process.config, "skip_subtitles", False),
            patch.object(process.config, "fx_upscale", False),
            patch.object(process.config, "audio_mode", AudioMode.AUTOMATIC),
            patch.object(process.config, "video_mode", VideoMode.MV_HEVC),
            patch.object(process.config, "start_stage", Stage.EXTRACT_MVC_AND_AUDIO),
        ):
            mkv_recovery_plan = process.conversion_stage_plan()

        self.assertNotIn("create_mkv", mkv_recovery_plan)
        self.assertIn("extract_mvc_and_audio", mkv_recovery_plan)
        self.assertIn("extract_subtitles", mkv_recovery_plan)

        with (
            patch.object(process.config, "preview_range", None),
            patch.object(process.config, "skip_subtitles", True),
            patch.object(process.config, "fx_upscale", False),
            patch.object(process.config, "audio_mode", AudioMode.CONVERT_AAC),
            patch.object(process.config, "video_mode", VideoMode.MV_HEVC),
            patch.object(process.config, "start_stage", Stage.EXTRACT_SUBTITLES),
        ):
            subtitle_recovery_plan = process.conversion_stage_plan()

        self.assertNotIn("create_mkv", subtitle_recovery_plan)
        self.assertNotIn("extract_mvc_and_audio", subtitle_recovery_plan)
        self.assertNotIn("extract_subtitles", subtitle_recovery_plan)
        self.assertIn("create_left_right_files", subtitle_recovery_plan)

    def test_batch_processing_aborts_on_dependency_preflight_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source_folder = Path(temp_dir)
            (source_folder / "movie.m2ts").touch()

            with (
                patch.object(process.config, "source_folder_path", source_folder),
                patch.object(process, "process_each", side_effect=preflight.DependencyPreflightError("missing tool")),
                self.assertRaisesRegex(preflight.DependencyPreflightError, "missing tool"),
            ):
                process.process(Stage.CREATE_MKV)

    def test_batch_processing_stops_after_cancellation(self) -> None:
        cancellation_event = threading.Event()
        processed_sources: list[Path | None] = []

        def cancel_after_first_source(
            _cancellation_event: threading.Event | None = None,
            **_kwargs: object,
        ) -> None:
            processed_sources.append(process.config.source_path)
            cancellation_event.set()

        with tempfile.TemporaryDirectory() as temp_dir:
            source_folder = Path(temp_dir)
            (source_folder / "first.m2ts").touch()
            (source_folder / "second.m2ts").touch()

            with (
                patch.object(process.config, "source_folder_path", source_folder),
                patch.object(process, "process_each", side_effect=cancel_after_first_source),
                self.assertRaises(process.ProcessingCancelled),
            ):
                process.process(Stage.CREATE_MKV, cancellation_event)

        self.assertEqual(len(processed_sources), 1)

    def test_batch_processing_propagates_run_context_cancellation(self) -> None:
        cancellation = CancellationToken()
        run_context = RunContext(ObservabilityStream(ObservabilityEmitter.WORKER), cancellation)

        def cancel_from_context(*_args: object, **_kwargs: object) -> None:
            cancellation.cancel()
            raise process.ProcessCancelled("stop")

        with tempfile.TemporaryDirectory() as temp_dir:
            source_folder = Path(temp_dir)
            (source_folder / "first.m2ts").touch()
            (source_folder / "second.m2ts").touch()

            with (
                patch.object(process.config, "source_folder_path", source_folder),
                patch.object(process, "process_each", side_effect=cancel_from_context),
                self.assertRaises(process.ProcessingCancelled),
            ):
                process.process(Stage.CREATE_MKV, run_context=run_context)

    def test_batch_resume_stage_only_applies_to_failed_source(self) -> None:
        processed_sources: list[tuple[Path | None, Stage]] = []

        def record_source(
            _cancellation_event: threading.Event | None = None,
            **_kwargs: object,
        ) -> None:
            processed_sources.append((process.config.source_path, process.config.start_stage))

        with tempfile.TemporaryDirectory() as temp_dir:
            source_folder = Path(temp_dir)
            first_source = source_folder / "first.m2ts"
            failed_source = source_folder / "failed.m2ts"
            next_source = source_folder / "next.m2ts"
            for source in (first_source, failed_source, next_source):
                source.touch()
            batch_folder = Mock()
            batch_folder.rglob.return_value = [next_source, failed_source, first_source]
            batch_sources = (first_source, failed_source, next_source)

            with (
                patch.object(process.config, "source_folder_path", batch_folder),
                patch.object(process, "process_each", side_effect=record_source),
            ):
                process.process(
                    Stage.EXTRACT_MVC_AND_AUDIO,
                    resume_source_path=failed_source,
                    batch_start_stage=Stage.CREATE_MKV,
                    batch_sources=batch_sources,
                )

        self.assertEqual(
            processed_sources,
            [
                (failed_source, Stage.EXTRACT_MVC_AND_AUDIO),
                (next_source, Stage.CREATE_MKV),
            ],
        )

    def test_batch_source_manifest_is_sorted_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source_folder = Path(temp_dir)
            first_source = source_folder / "a.m2ts"
            second_source = source_folder / "b.mkv"
            third_source = source_folder / "c.iso"
            for source in (first_source, second_source, third_source):
                source.touch()
            batch_folder = Mock()
            batch_folder.rglob.return_value = [third_source, first_source, second_source]

            batch_sources = process.find_batch_sources(batch_folder)

        self.assertEqual(batch_sources, (first_source, second_source, third_source))

    def test_batch_error_records_failed_source_for_continuation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "failed.m2ts"
            source.touch()
            batch_folder = Mock()
            batch_folder.rglob.return_value = [source]
            error = MKVCreationError("failed")

            with (
                patch.object(process.config, "source_folder_path", batch_folder),
                patch.object(process, "process_each", side_effect=error),
                self.assertRaises(process.BatchProcessingError) as raised,
            ):
                process.process(Stage.CREATE_MKV)

        self.assertEqual(raised.exception.source_path, source)
        self.assertIs(raised.exception.error, error)
        self.assertEqual(raised.exception.batch_sources, (source,))

    def test_direct_pipeline_reused_file_source_is_not_cleanup_owned(self) -> None:
        with (
            patch.object(process.config, "keep_files", False),
            patch.object(process.config, "source_path", Path("movie.mkv")),
            patch.object(process.config, "MTS_EXTENSIONS", [".mts", ".m2ts"]),
        ):
            self.assertTrue(is_direct_pipeline_source_reused())

    def test_keep_files_source_remains_cleanup_owned(self) -> None:
        with (
            patch.object(process.config, "keep_files", True),
            patch.object(process.config, "source_path", Path("movie.mkv")),
            patch.object(process.config, "MTS_EXTENSIONS", [".mts", ".m2ts"]),
        ):
            self.assertFalse(is_direct_pipeline_source_reused())

    def test_direct_pipeline_iso_source_remains_cleanup_owned(self) -> None:
        with (
            patch.object(process.config, "keep_files", False),
            patch.object(process.config, "source_path", Path("movie.iso")),
            patch.object(process.config, "MTS_EXTENSIONS", [".mts", ".m2ts"]),
        ):
            self.assertFalse(is_direct_pipeline_source_reused())

    def test_create_mkv_start_refuses_to_clear_folder_containing_direct_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_root = Path(temp_dir)
            output_folder = output_root / "Movie"
            output_folder.mkdir()
            source_path = output_folder / "Movie.mkv"
            source_path.write_bytes(b"source")

            with (
                patch.object(process.config, "keep_files", False),
                patch.object(process.config, "source_path", source_path),
                patch.object(process.config, "output_root_path", output_root),
                patch.object(process.config, "start_stage", Stage.CREATE_MKV),
                patch.object(process.config, "MTS_EXTENSIONS", [".mts", ".m2ts"]),
                self.assertRaisesRegex(ValueError, "source media"),
            ):
                prepare_output_folder_for_source("Movie")

            self.assertTrue(source_path.exists())

    def test_keep_files_create_mkv_start_refuses_to_clear_folder_containing_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_root = Path(temp_dir)
            output_folder = output_root / "Movie"
            output_folder.mkdir()
            source_path = output_folder / "Movie.mkv"
            source_path.write_bytes(b"source")

            with (
                patch.object(process.config, "keep_files", True),
                patch.object(process.config, "source_path", source_path),
                patch.object(process.config, "output_root_path", output_root),
                patch.object(process.config, "start_stage", Stage.CREATE_MKV),
                self.assertRaisesRegex(ValueError, "source media"),
            ):
                prepare_output_folder_for_source("Movie")

            self.assertTrue(source_path.exists())

    def test_temp_cleanup_preserves_source_inside_temp_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_folder = Path(temp_dir) / "temp_files"
            temp_folder.mkdir()
            source_path = temp_folder / "Movie.mkv"
            source_path.write_bytes(b"source")

            with patch.object(process.config, "source_path", source_path):
                removed = remove_output_folder_if_safe(temp_folder)

            self.assertFalse(removed)
            self.assertTrue(source_path.exists())

    def test_remove_original_refuses_source_directory_containing_final_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source_folder = Path(temp_dir) / "source"
            final_path = source_folder / "output" / "Movie_AVP.mov"
            final_path.parent.mkdir(parents=True)
            final_path.write_bytes(b"final")

            with patch.object(process.config, "source_path", source_folder):
                removed = process.remove_original_source(final_path)

            self.assertFalse(removed)
            self.assertEqual(final_path.read_bytes(), b"final")

    def test_remove_original_file_preserves_sibling_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source_folder = Path(temp_dir) / "source"
            source_folder.mkdir()
            source_path = source_folder / "Movie.mkv"
            sibling_path = source_folder / "notes.txt"
            final_path = Path(temp_dir) / "output" / "Movie_AVP.mov"
            source_path.write_bytes(b"source")
            sibling_path.write_bytes(b"notes")

            with patch.object(process.config, "source_path", source_path):
                removed = process.remove_original_source(final_path)

            self.assertTrue(removed)
            self.assertFalse(source_path.exists())
            self.assertEqual(sibling_path.read_bytes(), b"notes")

    def test_move_completed_conversion_reports_refused_source_removal(self) -> None:
        activity = Mock()
        final_path = Path("/output/Movie_AVP.mov")
        with (
            patch.object(process.config, "keep_files", True),
            patch.object(process.config, "remove_original", True),
            patch.object(process, "move_file_to_output_root_folder", return_value=final_path),
            patch.object(process, "remove_original_source", return_value=False),
        ):
            result = process.move_completed_conversion(
                Path("/temporary/Movie_AVP.mov"),
                Path("/temporary"),
                None,
                activity,
            )

        self.assertEqual(result, final_path)
        activity.warning.assert_called_once_with(
            "The original source was kept because it could not be removed safely.",
            stage="move_files",
            code="source_removal_refused",
        )

    def test_output_move_preserves_folder_containing_direct_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_root = Path(temp_dir)
            output_folder = output_root / "Movie"
            output_folder.mkdir()
            source_path = output_folder / "Movie.mkv"
            muxed_path = output_folder / "Movie_AVP.mov"
            source_path.write_bytes(b"source")
            muxed_path.write_bytes(b"final")

            with (
                patch.object(process.config, "source_path", source_path),
                patch.object(process.config, "output_root_path", output_root),
                patch.object(process.config, "keep_files", False),
                patch.object(process.config, "MTS_EXTENSIONS", [".mts", ".m2ts"]),
            ):
                move_file_to_output_root_folder(muxed_path)

            self.assertTrue(source_path.exists())
            self.assertTrue(output_folder.exists())
            self.assertEqual((output_root / "Movie_AVP.mov").read_bytes(), b"final")

    def test_output_move_returns_final_output_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_root = Path(temp_dir)
            output_folder = output_root / "Movie"
            output_folder.mkdir()
            muxed_path = output_folder / "Movie_AVP.mov"
            muxed_path.write_bytes(b"final")

            with (
                patch.object(process.config, "output_root_path", output_root),
                patch.object(process.config, "keep_files", True),
            ):
                final_path = move_file_to_output_root_folder(muxed_path)

            self.assertEqual(final_path, output_root / "Movie_AVP.mov")
            self.assertEqual(final_path.read_bytes(), b"final")

    def test_create_mkv_start_preserves_symlink_source_inside_output_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            output_root = temp_path / "output"
            output_folder = output_root / "Movie"
            external_folder = temp_path / "source"
            output_folder.mkdir(parents=True)
            external_folder.mkdir()
            external_source = external_folder / "Movie.mkv"
            source_path = output_folder / "Movie.mkv"
            external_source.write_bytes(b"source")
            source_path.symlink_to(external_source)

            with (
                patch.object(process.config, "keep_files", False),
                patch.object(process.config, "source_path", source_path),
                patch.object(process.config, "output_root_path", output_root),
                patch.object(process.config, "start_stage", Stage.CREATE_MKV),
                patch.object(process.config, "MTS_EXTENSIONS", [".mts", ".m2ts"]),
                self.assertRaisesRegex(ValueError, "source media"),
            ):
                prepare_output_folder_for_source("Movie")

            self.assertTrue(source_path.is_symlink())
            self.assertEqual(source_path.resolve(strict=True), external_source.resolve(strict=True))


if __name__ == "__main__":
    unittest.main()
