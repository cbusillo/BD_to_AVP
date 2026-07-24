import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from bd_to_avp.modules import process
from bd_to_avp.modules.audio_mode import AudioMode
from bd_to_avp.modules.config import Stage
from bd_to_avp.modules.disc import DiscInfo
from bd_to_avp.modules.preview_range import PreviewRange
from bd_to_avp.modules.video_mode import VideoMode
from bd_to_avp.modules.video_route import ResolvedVideoRoute, VideoRouteKind
from bd_to_avp.observability import ObservabilityEmitter
from bd_to_avp.runtime import ObservabilityStream, RunContext
from bd_to_avp.worker.protocol import VideoRouteIntent


class ProcessAudioWiringTests(unittest.TestCase):
    def test_process_each_threads_run_context_and_stage_context_to_every_tool_stage(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            source_path = temp_path / "source.mkv"
            source_path.write_bytes(b"source")
            output_folder = temp_path / "Movie"
            output_folder.mkdir()
            preview_range = PreviewRange(0, 60, 120)
            run_context = RunContext(ObservabilityStream(ObservabilityEmitter.WORKER))
            stage_mocks: list[tuple[object, str]] = []

            with ExitStack() as stack:
                stack.enter_context(patch.object(process.config, "source_path", source_path))
                stack.enter_context(patch.object(process.config, "source_str", None))
                stack.enter_context(patch.object(process.config, "output_root_path", temp_path))
                stack.enter_context(patch.object(process.config, "overwrite", True))
                stack.enter_context(patch.object(process.config, "keep_files", True))
                stack.enter_context(patch.object(process.config, "audio_mode", AudioMode.CONVERT_AAC))
                stack.enter_context(patch.object(process.config, "video_mode", VideoMode.MV_HEVC))
                stack.enter_context(patch.object(process.config, "fx_upscale", True))
                stack.enter_context(patch.object(process.config, "skip_subtitles", False))
                stack.enter_context(patch.object(process.config, "start_stage", Stage.CREATE_MKV))
                stack.enter_context(patch.object(process.config, "preview_range", preview_range))
                stack.enter_context(patch.object(process.config, "remove_original", False))
                stage_mocks.append(
                    (stack.enter_context(patch.object(process.preflight, "verify_runtime_ready")), "preflight")
                )
                stage_mocks.append(
                    (
                        stack.enter_context(
                            patch.object(
                                process,
                                "get_disc_and_mvc_video_info",
                                return_value=DiscInfo(name="Movie"),
                            )
                        ),
                        "inspect_source",
                    )
                )
                stack.enter_context(
                    patch.object(process, "prepare_output_folder_for_source", return_value=output_folder)
                )
                stack.enter_context(patch.object(process, "file_exists_normalized", return_value=False))
                stage_mocks.append(
                    (
                        stack.enter_context(patch.object(process, "create_mkv_file", return_value=source_path)),
                        "create_mkv",
                    )
                )
                stage_mocks.append(
                    (
                        stack.enter_context(
                            patch.object(
                                process,
                                "create_bounded_preview_source",
                                return_value=(source_path, preview_range),
                            )
                        ),
                        "prepare_preview_range",
                    )
                )
                stage_mocks.append(
                    (stack.enter_context(patch.object(process, "get_video_color_depth", return_value=8)), "probe_color")
                )
                stage_mocks.append(
                    (
                        stack.enter_context(patch.object(process, "detect_crop_parameters", return_value="")),
                        "detect_crop",
                    )
                )
                audio_path = output_folder / "audio.mov"
                mvc_path = output_folder / "mvc.h264"
                stage_mocks.append(
                    (
                        stack.enter_context(
                            patch.object(process, "create_mvc_and_audio", return_value=(audio_path, mvc_path))
                        ),
                        "extract_mvc_and_audio",
                    )
                )
                subtitle_stage = stack.enter_context(patch.object(process, "create_srt_from_mkv"))
                stage_mocks.append((subtitle_stage, "extract_subtitles"))
                left_path = output_folder / "left.mov"
                right_path = output_folder / "right.mov"
                stage_mocks.append(
                    (
                        stack.enter_context(
                            patch.object(process, "create_left_right_files", return_value=(left_path, right_path))
                        ),
                        "create_left_right_files",
                    )
                )
                mv_hevc_path = output_folder / "mv-hevc.mov"
                stage_mocks.append(
                    (
                        stack.enter_context(patch.object(process, "create_mv_hevc_file", return_value=mv_hevc_path)),
                        "combine_to_mv_hevc",
                    )
                )
                upscaled_path = output_folder / "upscaled.mov"
                stage_mocks.append(
                    (
                        stack.enter_context(patch.object(process, "create_upscaled_file", return_value=upscaled_path)),
                        "upscale_video",
                    )
                )
                prepared_audio_path = output_folder / "audio.m4a"
                stage_mocks.append(
                    (
                        stack.enter_context(
                            patch.object(
                                process,
                                "create_transcoded_audio_file",
                                return_value=prepared_audio_path,
                            )
                        ),
                        "transcode_audio",
                    )
                )
                muxed_path = output_folder / "Movie_AVP.mov"
                stage_mocks.append(
                    (
                        stack.enter_context(patch.object(process, "create_muxed_file", return_value=muxed_path)),
                        "create_final_file",
                    )
                )
                stack.enter_context(
                    patch.object(process, "move_completed_conversion", return_value=temp_path / "Movie_AVP.mov")
                )
                stack.enter_context(patch.dict(process.os.environ, {}, clear=False))

                process.process_each(run_context=run_context)

            for stage_mock, stage_id in stage_mocks:
                call = stage_mock.call_args
                self.assertIs(call.kwargs["run_context"], run_context)
                self.assertIs(call.kwargs["cancellation_event"], run_context.cancellation.event)
                self.assertEqual(call.kwargs["observability_context"].stage.id, stage_id)
            self.assertIsNone(subtitle_stage.call_args.args[2])

    def test_move_files_resume_skips_prior_processing_stages(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            source_path = temp_path / "missing-source.iso"
            output_folder = temp_path / "Movie"
            muxed_path = output_folder / "Movie_AVP.mov"
            final_path = temp_path / "Movie_AVP.mov"
            output_folder.mkdir()
            muxed_path.write_bytes(b"final")

            with ExitStack() as stack:
                stack.enter_context(patch.object(process.config, "source_path", source_path))
                stack.enter_context(patch.object(process.config, "output_root_path", temp_path))
                stack.enter_context(patch.object(process.config, "overwrite", True))
                stack.enter_context(patch.object(process.config, "keep_files", False))
                stack.enter_context(patch.object(process.config, "start_stage", Stage.MOVE_FILES))
                stack.enter_context(patch.object(process.config, "remove_original", False))
                preflight = stack.enter_context(patch.object(process.preflight, "verify_runtime_ready"))
                inspect_source = stack.enter_context(patch.object(process, "get_disc_and_mvc_video_info"))
                prepare_output = stack.enter_context(patch.object(process, "prepare_output_folder_for_source"))
                create_mkv = stack.enter_context(patch.object(process, "create_mkv_file"))

                result = process.process_each()

            self.assertEqual(result, final_path)
            self.assertEqual(final_path.read_bytes(), b"final")
            self.assertFalse(output_folder.exists())
            preflight.assert_not_called()
            inspect_source.assert_not_called()
            prepare_output.assert_not_called()
            create_mkv.assert_not_called()

    def test_move_files_resume_selects_matching_direct_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            selected_folder = temp_path / "Selected"
            other_folder = temp_path / "Other"
            selected_folder.mkdir()
            other_folder.mkdir()
            (selected_folder / "Selected_AVP.mov").write_bytes(b"selected")
            (other_folder / "Other_AVP.mov").write_bytes(b"other")

            with (
                patch.object(process.config, "source_path", temp_path / "Selected.mkv"),
                patch.object(process.config, "output_root_path", temp_path),
                patch.object(process.config, "overwrite", True),
                patch.object(process.config, "keep_files", False),
                patch.object(process.config, "start_stage", Stage.MOVE_FILES),
                patch.object(process.config, "remove_original", False),
            ):
                result = process.process_each()

            self.assertEqual(result, temp_path / "Selected_AVP.mov")
            self.assertEqual(result.read_bytes(), b"selected")
            self.assertTrue((other_folder / "Other_AVP.mov").exists())

    def test_move_files_resume_rejects_ambiguous_completed_movies(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            for name in ("First", "Second"):
                output_folder = temp_path / name
                output_folder.mkdir()
                (output_folder / f"{name}_AVP.mov").write_bytes(name.encode())

            with (
                patch.object(process.config, "source_path", temp_path / "unavailable.iso"),
                patch.object(process.config, "output_root_path", temp_path),
                patch.object(process.config, "start_stage", Stage.MOVE_FILES),
                self.assertRaisesRegex(RuntimeError, "Multiple completed movies are ready to move"),
            ):
                process.process_each()

    def test_move_files_stage_plan_is_filesystem_only(self) -> None:
        with patch.object(process.config, "start_stage", Stage.MOVE_FILES):
            self.assertEqual(process.conversion_stage_plan(), ("configure", "move_files"))

    def test_direct_audio_source_is_replaced_by_aac_and_removed_after_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            source_path = temp_path / "source.mkv"
            output_folder = temp_path / "Movie"
            video_path = output_folder / "Movie_mvc.h264"
            left_path = output_folder / "Movie_left.mov"
            right_path = output_folder / "Movie_right.mov"
            mv_hevc_path = output_folder / "Movie_MV-HEVC.mov"
            aac_path = output_folder / "Movie_audio_AAC.m4a"
            final_path = output_folder / "Movie_AVP.mov"
            source_path.write_bytes(b"source")
            output_folder.mkdir()

            with ExitStack() as stack:
                stack.enter_context(patch.object(process.config, "source_path", source_path))
                stack.enter_context(patch.object(process.config, "output_root_path", temp_path))
                stack.enter_context(patch.object(process.config, "overwrite", True))
                stack.enter_context(patch.object(process.config, "keep_files", False))
                stack.enter_context(patch.object(process.config, "audio_mode", AudioMode.AUTOMATIC))
                stack.enter_context(patch.object(process.config, "video_mode", VideoMode.MV_HEVC))
                stack.enter_context(patch.object(process.config, "start_stage", Stage.CREATE_MKV))
                stack.enter_context(patch.object(process.config, "remove_original", True))
                stack.enter_context(patch.object(process.config, "language_code", "eng"))
                stack.enter_context(patch.object(process.preflight, "verify_runtime_ready"))
                stack.enter_context(
                    patch.object(process, "get_disc_and_mvc_video_info", return_value=DiscInfo(name="Movie"))
                )
                stack.enter_context(
                    patch.object(process, "prepare_output_folder_for_source", return_value=output_folder)
                )
                stack.enter_context(patch.object(process, "file_exists_normalized", return_value=False))
                stack.enter_context(patch.object(process, "create_mkv_file", return_value=source_path))
                stack.enter_context(patch.object(process, "get_video_color_depth", return_value=8))
                stack.enter_context(patch.object(process, "detect_crop_parameters", return_value=None))
                stack.enter_context(
                    patch.object(process, "create_mvc_and_audio", return_value=(source_path, video_path))
                )
                stack.enter_context(patch.object(process, "create_srt_from_mkv"))
                stack.enter_context(
                    patch.object(process, "create_left_right_files", return_value=(left_path, right_path))
                )
                stack.enter_context(patch.object(process, "create_mv_hevc_file", return_value=mv_hevc_path))
                stack.enter_context(patch.object(process, "create_upscaled_file", return_value=mv_hevc_path))
                prepare_audio = stack.enter_context(
                    patch.object(process, "create_transcoded_audio_file", return_value=aac_path)
                )
                mux = stack.enter_context(patch.object(process, "create_muxed_file", return_value=final_path))
                stack.enter_context(patch.object(process, "move_file_to_output_root_folder"))
                stack.enter_context(patch.dict(process.os.environ, {}, clear=False))
                process.process_each()

                self.assertEqual(prepare_audio.call_args.args[:2], (source_path, output_folder))
                self.assertEqual(mux.call_args.args, (aac_path, mv_hevc_path, output_folder, "Movie"))
                self.assertEqual(mux.call_args.kwargs["observability_context"].stage.id, "create_final_file")
                self.assertFalse(source_path.exists())

    def test_prepare_audio_stage_preserves_stage_id_with_new_message(self) -> None:
        class StopAfterAudio(Exception):
            pass

        class Activity:
            def __init__(self) -> None:
                self.started: list[tuple[str, str]] = []

            def stage_started(self, stage: str, message: str) -> None:
                self.started.append((stage, message))

            def log(self, *_args: object, **_kwargs: object) -> None:
                pass

            def warning(self, *_args: object, **_kwargs: object) -> None:
                pass

            def stage_progress(self, *_args: object, **_kwargs: object) -> None:
                pass

            def set_stage_plan(self, *_args: object, **_kwargs: object) -> None:
                pass

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            source_path = temp_path / "source.mkv"
            output_folder = temp_path / "Movie"
            video_path = output_folder / "Movie_mvc.h264"
            left_path = output_folder / "Movie_left.mov"
            right_path = output_folder / "Movie_right.mov"
            mv_hevc_path = output_folder / "Movie_MV-HEVC.mov"
            aac_path = output_folder / "Movie_audio_AAC.m4a"
            source_path.write_bytes(b"source")
            output_folder.mkdir()
            activity = Activity()

            with ExitStack() as stack:
                stack.enter_context(patch.object(process.config, "source_path", source_path))
                stack.enter_context(patch.object(process.config, "output_root_path", temp_path))
                stack.enter_context(patch.object(process.config, "overwrite", True))
                stack.enter_context(patch.object(process.config, "keep_files", False))
                stack.enter_context(patch.object(process.config, "audio_mode", AudioMode.AUTOMATIC))
                stack.enter_context(patch.object(process.config, "video_mode", VideoMode.MV_HEVC))
                stack.enter_context(patch.object(process.config, "start_stage", Stage.CREATE_MKV))
                stack.enter_context(patch.object(process.config, "remove_original", False))
                stack.enter_context(patch.object(process.preflight, "verify_runtime_ready"))
                stack.enter_context(
                    patch.object(process, "get_disc_and_mvc_video_info", return_value=DiscInfo(name="Movie"))
                )
                stack.enter_context(
                    patch.object(process, "prepare_output_folder_for_source", return_value=output_folder)
                )
                stack.enter_context(patch.object(process, "file_exists_normalized", return_value=False))
                stack.enter_context(patch.object(process, "create_mkv_file", return_value=source_path))
                stack.enter_context(patch.object(process, "get_video_color_depth", return_value=8))
                stack.enter_context(patch.object(process, "detect_crop_parameters", return_value=None))
                stack.enter_context(
                    patch.object(process, "create_mvc_and_audio", return_value=(source_path, video_path))
                )
                stack.enter_context(patch.object(process, "create_srt_from_mkv"))
                stack.enter_context(
                    patch.object(process, "create_left_right_files", return_value=(left_path, right_path))
                )
                stack.enter_context(patch.object(process, "create_mv_hevc_file", return_value=mv_hevc_path))
                stack.enter_context(patch.object(process, "create_upscaled_file", return_value=mv_hevc_path))
                stack.enter_context(patch.object(process, "create_transcoded_audio_file", return_value=aac_path))
                stack.enter_context(patch.object(process, "create_muxed_file", side_effect=StopAfterAudio))
                stack.enter_context(self.assertRaises(StopAfterAudio))
                process.process_each(activity=activity)

            self.assertIn(("transcode_audio", "Prepare Audio"), activity.started)

    def test_av1_mode_uses_packed_encode_and_preserves_final_mux_wiring(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            source_path = temp_path / "source.mkv"
            output_folder = temp_path / "Movie"
            mvc_path = output_folder / "Movie_mvc.h264"
            unmarked_path = output_folder / "Movie_AV1-SBS-unmarked.mp4"
            stereo_path = output_folder / "Movie_AV1-Stereo.mp4"
            audio_path = output_folder / "Movie_audio_PCM.mov"
            final_path = output_folder / "Movie_AV1_Stereo.mov"
            source_path.write_bytes(b"source")
            output_folder.mkdir()

            with ExitStack() as stack:
                stack.enter_context(patch.object(process.config, "source_path", source_path))
                stack.enter_context(patch.object(process.config, "output_root_path", temp_path))
                stack.enter_context(patch.object(process.config, "overwrite", True))
                stack.enter_context(patch.object(process.config, "keep_files", False))
                stack.enter_context(patch.object(process.config, "audio_mode", AudioMode.PCM))
                stack.enter_context(patch.object(process.config, "video_mode", VideoMode.AV1_SBS))
                stack.enter_context(patch.object(process.config, "fx_upscale", False))
                stack.enter_context(patch.object(process.config, "start_stage", Stage.CREATE_MKV))
                stack.enter_context(patch.object(process.config, "remove_original", False))
                stack.enter_context(patch.object(process.preflight, "verify_runtime_ready"))
                stack.enter_context(
                    patch.object(process, "get_disc_and_mvc_video_info", return_value=DiscInfo(name="Movie"))
                )
                stack.enter_context(
                    patch.object(process, "prepare_output_folder_for_source", return_value=output_folder)
                )
                stack.enter_context(patch.object(process, "file_exists_normalized", return_value=False))
                stack.enter_context(patch.object(process, "create_mkv_file", return_value=source_path))
                stack.enter_context(patch.object(process, "get_video_color_depth", return_value=8))
                stack.enter_context(patch.object(process, "detect_crop_parameters", return_value=None))
                stack.enter_context(patch.object(process, "create_mvc_and_audio", return_value=(audio_path, mvc_path)))
                stack.enter_context(patch.object(process, "create_srt_from_mkv"))
                create_sbs = stack.enter_context(
                    patch.object(process, "create_av1_sbs_file", return_value=unmarked_path)
                )
                finalize = stack.enter_context(
                    patch.object(process, "create_av1_stereo_file", return_value=stereo_path)
                )
                create_left_right = stack.enter_context(patch.object(process, "create_left_right_files"))
                create_mv_hevc = stack.enter_context(patch.object(process, "create_mv_hevc_file"))
                stack.enter_context(patch.object(process, "create_upscaled_file", return_value=stereo_path))
                stack.enter_context(patch.object(process, "create_transcoded_audio_file", return_value=audio_path))
                mux = stack.enter_context(patch.object(process, "create_muxed_file", return_value=final_path))
                stack.enter_context(patch.object(process, "move_file_to_output_root_folder"))
                stack.enter_context(patch.dict(process.os.environ, {}, clear=False))

                process.process_each()

            self.assertEqual(
                create_sbs.call_args.args,
                (DiscInfo(name="Movie", color_depth=8), output_folder, mvc_path, None),
            )
            self.assertEqual(create_sbs.call_args.kwargs["observability_context"].stage.id, "encode_av1_stereo")
            self.assertEqual(
                finalize.call_args.args,
                (unmarked_path, output_folder, DiscInfo(name="Movie", color_depth=8)),
            )
            self.assertEqual(finalize.call_args.kwargs["observability_context"].stage.id, "finalize_av1_stereo")
            create_left_right.assert_not_called()
            create_mv_hevc.assert_not_called()
            self.assertEqual(mux.call_args.args, (audio_path, stereo_path, output_folder, "Movie"))
            self.assertEqual(mux.call_args.kwargs["observability_context"].stage.id, "create_final_file")

    def test_direct_mv_hevc_route_writes_stage_four_boundary_and_skips_generated_stages(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            source_path = temp_path / "source.mkv"
            output_folder = temp_path / "Movie"
            mvc_path = output_folder / "Movie_mvc.h264"
            direct_path = output_folder / "Movie_MV-HEVC.mov"
            audio_path = output_folder / "Movie_audio_PCM.mov"
            final_path = output_folder / "Movie_AVP.mov"
            source_path.write_bytes(b"source")
            output_folder.mkdir()
            route = ResolvedVideoRoute(
                intent=VideoRouteIntent.AUTOMATIC,
                selected=VideoRouteKind.DIRECT_MV_HEVC,
                reason="direct_eligible",
                output_mode=VideoMode.MV_HEVC,
                direct_bitrate_mbps=20,
            )

            with ExitStack() as stack:
                stack.enter_context(patch.object(process.config, "source_path", source_path))
                stack.enter_context(patch.object(process.config, "output_root_path", temp_path))
                stack.enter_context(patch.object(process.config, "overwrite", True))
                stack.enter_context(patch.object(process.config, "keep_files", False))
                stack.enter_context(patch.object(process.config, "audio_mode", AudioMode.PCM))
                stack.enter_context(patch.object(process.config, "video_mode", VideoMode.MV_HEVC))
                stack.enter_context(patch.object(process.config, "fx_upscale", False))
                stack.enter_context(patch.object(process.config, "skip_subtitles", True))
                stack.enter_context(patch.object(process.config, "start_stage", Stage.CREATE_MKV))
                stack.enter_context(patch.object(process.config, "remove_original", False))
                preflight = stack.enter_context(patch.object(process.preflight, "verify_runtime_ready"))
                stack.enter_context(
                    patch.object(process, "get_disc_and_mvc_video_info", return_value=DiscInfo(name="Movie"))
                )
                stack.enter_context(
                    patch.object(process, "prepare_output_folder_for_source", return_value=output_folder)
                )
                stack.enter_context(patch.object(process, "file_exists_normalized", return_value=False))
                stack.enter_context(patch.object(process, "create_mkv_file", return_value=source_path))
                stack.enter_context(patch.object(process, "get_video_color_depth", return_value=8))
                stack.enter_context(patch.object(process, "detect_crop_parameters", return_value=""))
                stack.enter_context(patch.object(process, "create_mvc_and_audio", return_value=(audio_path, mvc_path)))
                direct = stack.enter_context(
                    patch.object(process, "create_direct_mv_hevc_file", return_value=direct_path)
                )
                create_left_right = stack.enter_context(patch.object(process, "create_left_right_files"))
                create_mv_hevc = stack.enter_context(patch.object(process, "create_mv_hevc_file"))
                stack.enter_context(patch.object(process, "create_upscaled_file", return_value=direct_path))
                stack.enter_context(patch.object(process, "create_transcoded_audio_file", return_value=audio_path))
                mux = stack.enter_context(patch.object(process, "create_muxed_file", return_value=final_path))
                stack.enter_context(patch.object(process, "move_file_to_output_root_folder", return_value=final_path))
                stack.enter_context(patch.dict(process.os.environ, {}, clear=False))

                process.process_each(video_route=route)

            self.assertIs(preflight.call_args.kwargs["video_route"], route)
            self.assertEqual(
                direct.call_args.args, (DiscInfo(name="Movie", color_depth=8), output_folder, mvc_path, "", 20, None)
            )
            self.assertEqual(direct.call_args.kwargs["observability_context"].stage.id, "create_left_right_files")
            create_left_right.assert_not_called()
            create_mv_hevc.assert_not_called()
            self.assertEqual(mux.call_args.args, (audio_path, direct_path, output_folder, "Movie"))


if __name__ == "__main__":
    unittest.main()
