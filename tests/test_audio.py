import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import ffmpeg

from bd_to_avp.modules import audio
from bd_to_avp.modules.aac_layout_policy import (
    AAC_LAYOUT_POLICIES,
    AacLayoutAction,
    AacLayoutPolicyError,
)
from bd_to_avp.modules.audio_mode import AudioMode
from bd_to_avp.modules.audio_selection import load_audio_selection_manifest, select_audio_streams
from bd_to_avp.modules.config import Stage, config


class AudioPreparationTests(unittest.TestCase):
    def test_automatic_qualifies_only_retained_preferred_language_tracks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            source_path = temp_path / "source.mkv"
            output_folder = temp_path / "Movie"
            source_path.write_bytes(b"source")
            output_folder.mkdir()
            streams = [
                qualified_stream(index=2, language="eng"),
                qualified_stream(index=5, codec_name="ac3", language="jpn"),
            ]

            def copy_audio(_input_path: Path, output_path: Path, **_kwargs: object) -> None:
                output_path.write_bytes(b"selected-aac")

            with (
                patch.object(audio.config, "audio_mode", AudioMode.AUTOMATIC),
                patch.object(audio.config, "audio_preferred_language", "eng"),
                patch.object(audio.config, "start_stage", Stage.CREATE_MKV),
                patch.object(audio, "get_audio_stream_data", return_value=streams),
                patch.object(audio, "copy_audio", side_effect=copy_audio) as copy,
                patch.object(audio, "transcode_audio") as transcode,
            ):
                result = audio.create_prepared_audio_file(source_path, output_folder)

            self.assertEqual(result.read_bytes(), b"selected-aac")
            transcode.assert_not_called()
            selection = copy.call_args.kwargs["selection"]
            self.assertEqual([stream.stream_index for stream in selection.streams], [2])
            self.assertEqual([stream.audio_position for stream in selection.streams], [0])
            manifest = load_audio_selection_manifest(result)
            self.assertIsNotNone(manifest)
            assert manifest is not None
            self.assertEqual(manifest.source_stream_count, 2)
            self.assertEqual(manifest.selected_stream_count, 1)

    def test_automatic_copies_all_qualified_aac_to_owned_m4a(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            source_path = temp_path / "source.mkv"
            output_folder = temp_path / "Movie"
            source_path.write_bytes(b"source")
            output_folder.mkdir()

            def copy_audio(_input_path: Path, output_path: Path, **_kwargs: object) -> None:
                output_path.write_bytes(b"copied-aac")

            with (
                patch.object(audio.config, "audio_mode", AudioMode.AUTOMATIC),
                patch.object(audio.config, "keep_files", False),
                patch.object(audio.config, "source_path", source_path),
                patch.object(audio.config, "start_stage", Stage.CREATE_MKV),
                patch.object(audio, "qualify_selected_audio_streams", return_value=[audio_qualification()]) as qualify,
                patch.object(audio, "copy_audio", side_effect=copy_audio) as copy,
                patch.object(audio, "transcode_audio") as transcode,
            ):
                result = audio.create_prepared_audio_file(source_path, output_folder)

            final_path = output_folder / "Movie_audio_AAC.m4a"
            temporary_path = output_folder / "Movie_audio_AAC.part.m4a"
            self.assertEqual(result, final_path)
            self.assertEqual(final_path.read_bytes(), b"copied-aac")
            self.assertFalse(temporary_path.exists())
            self.assertTrue(source_path.exists())
            qualify.assert_called_once_with(
                source_path,
                selection=None,
                run_context=None,
                cancellation_event=None,
                observability_context=None,
            )
            copy.assert_called_once_with(
                source_path,
                temporary_path,
                selection=None,
                run_context=None,
                cancellation_event=None,
                observability_context=None,
            )
            transcode.assert_not_called()

    def test_automatic_mixed_codecs_transcodes_whole_set_and_warns(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            source_path = temp_path / "source.mkv"
            output_folder = temp_path / "Movie"
            source_path.write_bytes(b"source")
            output_folder.mkdir()
            activity = Mock()

            def write_audio(_input_path: Path, output_path: Path, _bitrate: int, **_kwargs: object) -> None:
                output_path.write_bytes(b"aac")

            qualifications = [
                audio_qualification(index=0, codec_name="aac"),
                audio_qualification(index=1, codec_name="ac3", qualified=False, reason="codec_not_allowed"),
            ]
            with (
                patch.object(audio.config, "audio_mode", AudioMode.AUTOMATIC),
                patch.object(audio.config, "keep_files", False),
                patch.object(audio.config, "source_path", source_path),
                patch.object(audio.config, "start_stage", Stage.CREATE_MKV),
                patch.object(audio.config, "audio_bitrate", 512),
                patch.object(audio, "qualify_selected_audio_streams", return_value=qualifications) as qualify,
                patch.object(audio, "copy_audio") as copy,
                patch.object(audio, "transcode_audio", side_effect=write_audio) as transcode,
            ):
                result = audio.create_prepared_audio_file(source_path, output_folder, activity)

            self.assertEqual(result, output_folder / "Movie_audio_AAC.m4a")
            qualify.assert_called_once_with(
                source_path,
                selection=None,
                run_context=None,
                cancellation_event=None,
                observability_context=None,
            )
            copy.assert_not_called()
            transcode.assert_called_once_with(
                source_path,
                output_folder / "Movie_audio_AAC.part.m4a",
                512,
                selection=None,
                activity=activity,
                run_context=None,
                cancellation_event=None,
                observability_context=None,
            )
            activity.warning.assert_called_once()
            _, kwargs = activity.warning.call_args
            self.assertEqual(kwargs["code"], "audio_automatic_fallback_to_aac")
            self.assertEqual(kwargs["action"], "convert_aac")
            self.assertEqual(kwargs["source_codecs"], ["aac", "ac3"])
            self.assertEqual(kwargs["unqualified_streams"][0]["reason"], "codec_not_allowed")

    def test_convert_aac_transcodes_without_qualification(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            source_path = temp_path / "source.mkv"
            output_folder = temp_path / "Movie"
            source_path.write_bytes(b"source")
            output_folder.mkdir()

            def write_audio(_input_path: Path, output_path: Path, _bitrate: int, **_kwargs: object) -> None:
                output_path.write_bytes(b"aac")

            with (
                patch.object(audio.config, "audio_mode", AudioMode.CONVERT_AAC),
                patch.object(audio.config, "keep_files", False),
                patch.object(audio.config, "source_path", source_path),
                patch.object(audio.config, "start_stage", Stage.CREATE_MKV),
                patch.object(audio.config, "audio_bitrate", 384),
                patch.object(audio, "qualify_selected_audio_streams") as qualify,
                patch.object(audio, "copy_audio") as copy,
                patch.object(audio, "transcode_audio", side_effect=write_audio) as transcode,
            ):
                audio.create_prepared_audio_file(source_path, output_folder)

            qualify.assert_not_called()
            copy.assert_not_called()
            transcode.assert_called_once_with(
                source_path,
                output_folder / "Movie_audio_AAC.part.m4a",
                384,
                selection=None,
                activity=None,
                run_context=None,
                cancellation_event=None,
                observability_context=None,
            )

    def test_failed_preparation_removes_partial_output_but_preserves_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            source_path = temp_path / "source.mkv"
            output_folder = temp_path / "Movie"
            source_path.write_bytes(b"source")
            output_folder.mkdir()

            def fail_after_partial_write(
                _input_path: Path,
                output_path: Path,
                _bitrate: int,
                **_kwargs: object,
            ) -> None:
                output_path.write_bytes(b"partial")
                raise RuntimeError("transcode failed")

            with (
                patch.object(audio.config, "audio_mode", AudioMode.CONVERT_AAC),
                patch.object(audio.config, "keep_files", False),
                patch.object(audio.config, "source_path", source_path),
                patch.object(audio.config, "start_stage", Stage.CREATE_MKV),
                patch.object(audio, "transcode_audio", side_effect=fail_after_partial_write),
                self.assertRaisesRegex(RuntimeError, "transcode failed"),
            ):
                audio.create_prepared_audio_file(source_path, output_folder)

            self.assertTrue(source_path.exists())
            self.assertFalse((output_folder / "Movie_audio_AAC.part.m4a").exists())
            self.assertFalse((output_folder / "Movie_audio_AAC.m4a").exists())

    def test_keep_files_does_not_change_automatic_copy_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            retained_source_copy = temp_path / "Movie" / "source.mkv"
            output_folder = retained_source_copy.parent
            output_folder.mkdir()
            retained_source_copy.write_bytes(b"source")

            def copy_audio(_input_path: Path, output_path: Path, **_kwargs: object) -> None:
                output_path.write_bytes(b"copied-aac")

            with (
                patch.object(audio.config, "audio_mode", AudioMode.AUTOMATIC),
                patch.object(audio.config, "keep_files", True),
                patch.object(audio.config, "source_path", retained_source_copy),
                patch.object(audio.config, "start_stage", Stage.CREATE_MKV),
                patch.object(audio, "qualify_selected_audio_streams", return_value=[audio_qualification()]),
                patch.object(audio, "copy_audio", side_effect=copy_audio) as copy,
                patch.object(audio, "transcode_audio") as transcode,
            ):
                audio.create_prepared_audio_file(retained_source_copy, output_folder)

            self.assertTrue(retained_source_copy.exists())
            copy.assert_called_once()
            transcode.assert_not_called()

    def test_prepared_audio_retains_owned_source_until_final_move(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            source_path = temp_path / "source.iso"
            output_folder = temp_path / "Movie"
            owned_mkv_path = output_folder / "Movie.mkv"
            source_path.write_bytes(b"disc-image")
            output_folder.mkdir()
            owned_mkv_path.write_bytes(b"owned-mkv")

            def copy_audio(_input_path: Path, output_path: Path, **_kwargs: object) -> None:
                output_path.write_bytes(b"copied-aac")

            with (
                patch.object(audio.config, "audio_mode", AudioMode.AUTOMATIC),
                patch.object(audio.config, "keep_files", False),
                patch.object(audio.config, "source_path", source_path),
                patch.object(audio.config, "start_stage", Stage.CREATE_MKV),
                patch.object(audio, "qualify_selected_audio_streams", return_value=[audio_qualification()]),
                patch.object(audio, "copy_audio", side_effect=copy_audio),
            ):
                audio.create_prepared_audio_file(owned_mkv_path, output_folder)

            self.assertTrue(owned_mkv_path.exists())

    def test_pcm_returns_existing_generated_pcm(self) -> None:
        original_audio_path = Path("Movie_audio_PCM.mov")
        with (
            patch.object(audio.config, "audio_mode", AudioMode.PCM),
            patch.object(audio.config, "start_stage", Stage.TRANSCODE_AUDIO),
            patch.object(audio, "transcode_audio") as transcode,
            patch.object(audio, "copy_audio") as copy,
        ):
            result = audio.create_prepared_audio_file(original_audio_path, Path("Movie"))

        self.assertEqual(result, original_audio_path)
        transcode.assert_not_called()
        copy.assert_not_called()

    def test_final_mux_resume_uses_existing_prepared_m4a(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            source_path = temp_path / "source.mkv"
            output_folder = temp_path / "Movie"
            source_path.write_bytes(b"source")
            output_folder.mkdir()
            aac_path = output_folder / "Movie_audio_AAC.m4a"
            aac_path.write_bytes(b"aac")

            with (
                patch.object(audio.config, "audio_mode", AudioMode.AUTOMATIC),
                patch.object(audio.config, "start_stage", Stage.CREATE_FINAL_FILE),
                patch.object(
                    audio,
                    "qualify_selected_audio_streams",
                    return_value=[audio_qualification()],
                ) as qualify,
                patch.object(audio, "transcode_audio") as transcode,
                patch.object(audio, "copy_audio") as copy,
            ):
                result = audio.create_prepared_audio_file(source_path, output_folder)

            self.assertEqual(result, aac_path)
            qualify.assert_called_once_with(
                aac_path,
                run_context=None,
                cancellation_event=None,
                observability_context=None,
            )
            transcode.assert_not_called()
            copy.assert_not_called()

    def test_final_mux_resume_rejects_stale_unqualified_aac(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            source_path = temp_path / "source.mkv"
            output_folder = temp_path / "Movie"
            source_path.write_bytes(b"source")
            output_folder.mkdir()
            aac_path = output_folder / "Movie_audio_AAC.m4a"
            aac_path.write_bytes(b"stale-aac")
            activity = Mock()

            with (
                patch.object(audio.config, "audio_mode", AudioMode.AUTOMATIC),
                patch.object(audio.config, "start_stage", Stage.CREATE_FINAL_FILE),
                patch.object(
                    audio,
                    "qualify_selected_audio_streams",
                    return_value=[
                        audio_qualification(
                            channels=8,
                            channel_layout="7.1",
                            qualified=False,
                            reason="channel_layout_requires_downmix",
                        )
                    ],
                ),
                self.assertRaisesRegex(AacLayoutPolicyError, "Restart from Prepare Audio"),
            ):
                audio.create_prepared_audio_file(source_path, output_folder, activity)

            activity.warning.assert_called_once()
            _, fields = activity.warning.call_args
            self.assertEqual(fields["code"], "audio_layout_policy_rejected")
            self.assertEqual(fields["action"], "restart_prepare_audio")
            self.assertEqual(fields["unqualified_streams"][0]["channel_layout"], "7.1")
            self.assertEqual(fields["unqualified_streams"][0]["reason"], "channel_layout_requires_downmix")

    def test_final_mux_resume_requires_owned_prepared_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            source_path = temp_path / "source.mkv"
            output_folder = temp_path / "Movie"
            source_path.write_bytes(b"source")
            output_folder.mkdir()

            with (
                patch.object(audio.config, "audio_mode", AudioMode.AUTOMATIC),
                patch.object(audio.config, "start_stage", Stage.CREATE_FINAL_FILE),
                self.assertRaisesRegex(FileNotFoundError, "Prepared audio artifact not found"),
            ):
                audio.create_prepared_audio_file(source_path, output_folder)

    def test_final_mux_resume_rejects_legacy_aac_mov_with_recovery_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            source_path = temp_path / "source.mkv"
            output_folder = temp_path / "Movie"
            source_path.write_bytes(b"source")
            output_folder.mkdir()
            legacy_aac_path = output_folder / "Movie_audio_AAC.mov"
            legacy_aac_path.write_bytes(b"legacy-aac")

            with (
                patch.object(audio.config, "audio_mode", AudioMode.AUTOMATIC),
                patch.object(audio.config, "start_stage", Stage.CREATE_FINAL_FILE),
                self.assertRaisesRegex(FileNotFoundError, "Restart from Prepare Audio"),
            ):
                audio.create_prepared_audio_file(source_path, output_folder)

            self.assertTrue(legacy_aac_path.exists())

    def test_qualification_is_conservative_and_excludes_ac3_eac3(self) -> None:
        streams = [
            qualified_stream(index=0),
            qualified_stream(index=1, codec_name="ac3"),
            qualified_stream(index=2, codec_name="eac3"),
            qualified_stream(index=3, codec_name="dts"),
            qualified_stream(index=4, profile="Main"),
            qualified_stream(index=5, sample_rate="96000"),
            qualified_stream(index=6, channels=6, channel_layout="stereo"),
            qualified_stream(index=7, channels=4, channel_layout="4.0"),
            {"index": 8, "codec_name": "aac", "profile": "LC"},
            qualified_stream(index=9, channels=6, channel_layout="5.1(side)"),
            qualified_stream(index=10, channels=8, channel_layout="7.1"),
            qualified_stream(index=11, channels=4, channel_layout="unknown"),
        ]

        results = [audio.qualify_audio_stream(stream) for stream in streams]

        self.assertTrue(results[0].qualified)
        self.assertEqual(results[1].reason, "codec_not_allowed")
        self.assertEqual(results[2].reason, "codec_not_allowed")
        self.assertEqual(results[3].reason, "codec_not_aac")
        self.assertEqual(results[4].reason, "aac_profile_not_qualified")
        self.assertEqual(results[5].reason, "sample_rate_not_qualified")
        self.assertEqual(results[6].reason, "channel_layout_mismatch")
        self.assertTrue(results[7].qualified)
        self.assertEqual(results[8].reason, "sample_rate_missing")
        self.assertEqual(results[9].reason, "channel_layout_requires_remap")
        self.assertEqual(results[10].reason, "channel_layout_requires_downmix")
        self.assertEqual(results[11].reason, "channel_layout_not_qualified")

    def test_layout_matrix_drives_automatic_copy_qualification(self) -> None:
        qualified_copy_layouts = {
            policy.source_layout: len(policy.source_channels)
            for policy in AAC_LAYOUT_POLICIES
            if policy.action is AacLayoutAction.PRESERVE
        }

        self.assertEqual(audio.AAC_COPY_LAYOUT_CHANNELS, qualified_copy_layouts)
        self.assertEqual(
            qualified_copy_layouts,
            {"mono": 1, "stereo": 2, "3.0": 3, "4.0": 4, "5.0": 5, "5.1": 6},
        )
        for index, policy in enumerate(AAC_LAYOUT_POLICIES):
            with self.subTest(layout=policy.source_layout):
                result = audio.qualify_audio_stream(
                    qualified_stream(
                        index=index,
                        channels=len(policy.source_channels),
                        channel_layout=policy.source_layout,
                    )
                )
                self.assertEqual(result.qualified, policy.action is AacLayoutAction.PRESERVE)
                if policy.action is not AacLayoutAction.PRESERVE:
                    self.assertEqual(result.reason, f"channel_layout_requires_{policy.action.value}")

    def test_transcode_options_match_every_qualified_policy_row(self) -> None:
        for index, policy in enumerate(AAC_LAYOUT_POLICIES):
            with self.subTest(layout=policy.source_layout):
                layout_plan = audio.plan_aac_layouts(
                    [
                        qualified_stream(
                            index=index,
                            channels=len(policy.source_channels),
                            channel_layout=policy.source_layout,
                        )
                    ]
                )
                options = audio.aac_layout_options(layout_plan)

                self.assertEqual(layout_plan[0].decision.policy, policy)
                self.assertEqual(options["channel_layout:a:0"], policy.target_layout)
                if policy.pan_filter is None:
                    self.assertNotIn("filter:a:0", options)
                else:
                    self.assertEqual(options["filter:a:0"], policy.pan_filter)

    def test_7_1_copy_is_rejected_and_downmixed_to_5_1(self) -> None:
        policy = next(policy for policy in AAC_LAYOUT_POLICIES if policy.source_layout == "7.1")
        qualification = audio.qualify_audio_stream(qualified_stream(index=0, channels=8, channel_layout="7.1"))

        self.assertNotIn("7.1", audio.AAC_COPY_LAYOUT_CHANNELS)
        self.assertEqual(policy.action, AacLayoutAction.DOWNMIX)
        self.assertEqual(policy.target_layout, "5.1")
        self.assertFalse(qualification.qualified)
        self.assertEqual(qualification.reason, "channel_layout_requires_downmix")

    def test_transcode_audio_maps_requested_stream_selector(self) -> None:
        streams = [qualified_stream(index=0)]
        with (
            patch.object(audio, "audio_streams_for_selector", return_value=streams),
            patch.object(audio, "audio_handler_metadata_options", return_value={}),
            patch.object(audio, "run_ffmpeg_print_errors") as run_ffmpeg,
        ):
            audio.transcode_audio(Path("source.mkv"), Path("audio.m4a"), 384, "a:0")

        command = ffmpeg.compile(run_ffmpeg.call_args.args[0])
        self.assertIn("0:a:0", command)
        self.assertIn("file:audio.m4a", command)
        self.assertIn("-map_metadata", command)

    def test_copy_audio_maps_all_audio_tracks_without_encoder(self) -> None:
        with (
            patch.object(audio, "audio_handler_metadata_options", return_value={}),
            patch.object(audio, "run_ffmpeg_print_errors") as run_ffmpeg,
        ):
            audio.copy_audio(Path("source.mkv"), Path("audio.m4a"))

        command = ffmpeg.compile(run_ffmpeg.call_args.args[0])
        self.assertIn("0:a", command)
        self.assertIn("copy", command)
        self.assertIn("file:audio.m4a", command)

    def test_copy_audio_explicitly_maps_non_contiguous_preferred_tracks(self) -> None:
        streams = [
            qualified_stream(index=1, language="eng", title="English 5.1"),
            qualified_stream(index=4, language="jpn", title="Japanese 5.1"),
            qualified_stream(index=8, language="en-US", title="English Commentary"),
        ]
        selection = select_audio_streams(streams, "eng")

        with (
            patch.object(audio, "audio_handler_metadata_options", return_value={}) as metadata_options,
            patch.object(audio, "run_ffmpeg_print_errors") as run_ffmpeg,
        ):
            audio.copy_audio(Path("source.mkv"), Path("audio.m4a"), selection=selection)

        command = ffmpeg.compile(run_ffmpeg.call_args.args[0])
        self.assertIn("0:a:0", command)
        self.assertIn("0:a:2", command)
        self.assertNotIn("0:a:1", command)
        self.assertEqual(command[command.index("-map_metadata:s:a:0") + 1], "0:s:a:0")
        self.assertEqual(command[command.index("-map_metadata:s:a:1") + 1], "0:s:a:2")
        metadata_options.assert_called_once_with(
            Path("source.mkv"),
            selected_streams=[streams[0], streams[2]],
            run_context=None,
            cancellation_event=None,
            observability_context=None,
        )

    def test_aac_transcode_explicitly_maps_the_same_preferred_track_set(self) -> None:
        streams = [
            qualified_stream(index=1, language="eng", title="English Main", is_default=True),
            qualified_stream(index=4, language="jpn"),
            qualified_stream(index=8, language="en-US", title="English Commentary"),
        ]
        selection = select_audio_streams(streams, "eng")

        with patch.object(audio, "run_ffmpeg_print_errors") as run_ffmpeg:
            audio.transcode_audio(Path("source.mkv"), Path("audio.m4a"), 384, selection=selection)

        command = ffmpeg.compile(run_ffmpeg.call_args.args[0])
        self.assertIn("0:a:0", command)
        self.assertIn("0:a:2", command)
        self.assertNotIn("0:a:1", command)
        self.assertIn("aac", command)
        self.assertEqual(command[command.index("-map_metadata:s:a:0") + 1], "0:s:a:0")
        self.assertEqual(command[command.index("-map_metadata:s:a:1") + 1], "0:s:a:2")
        self.assertEqual(command[command.index("-metadata:s:a:0") + 1], "handler_name=English Main")
        self.assertEqual(command[command.index("-metadata:s:a:1") + 1], "handler_name=English Commentary")
        self.assertEqual(command[command.index("-disposition:a:0") + 1], "default")
        self.assertEqual(command[command.index("-disposition:a:1") + 1], "0")

    def test_aac_transcode_normalizes_side_surround_for_apple_playback(self) -> None:
        streams = [
            qualified_stream(index=1, language="eng", channel_layout="5.1(side)", channels=6),
            qualified_stream(index=4, language="eng", channel_layout="stereo", channels=2),
        ]
        selection = select_audio_streams(streams, "eng")
        activity = Mock()

        with (
            patch.object(audio, "audio_handler_metadata_options", return_value={}),
            patch.object(audio, "run_ffmpeg_print_errors") as run_ffmpeg,
        ):
            audio.transcode_audio(
                Path("source.mkv"),
                Path("audio.m4a"),
                384,
                selection=selection,
                activity=activity,
            )

        command = ffmpeg.compile(run_ffmpeg.call_args.args[0])
        self.assertEqual(command[command.index("-channel_layout:a:0") + 1], "5.1")
        self.assertEqual(command[command.index("-channel_layout:a:1") + 1], "stereo")
        self.assertEqual(
            command[command.index("-filter:a:0") + 1],
            "pan=5.1|FL=FL|FR=FR|FC=FC|LFE=LFE|BL=SL|BR=SR",
        )
        self.assertNotIn("-filter:a:1", command)
        activity.warning.assert_called_once()
        _, fields = activity.warning.call_args
        self.assertEqual(fields["code"], "audio_layout_policy_applied")
        self.assertEqual(
            [decision["action"] for decision in fields["layout_decisions"]],
            ["remap", "preserve"],
        )

    def test_aac_transcode_applies_exact_filters_to_mixed_layouts(self) -> None:
        streams = [
            qualified_stream(index=1, channels=5, channel_layout="5.0(side)"),
            qualified_stream(index=4, channels=8, channel_layout="7.1(wide-side)"),
            qualified_stream(index=8, channels=2, channel_layout="stereo"),
        ]
        selection = select_audio_streams(streams, None)
        activity = Mock()

        with (
            patch.object(audio, "audio_handler_metadata_options", return_value={}),
            patch.object(audio, "run_ffmpeg_print_errors") as run_ffmpeg,
        ):
            audio.transcode_audio(
                Path("source.mkv"),
                Path("audio.m4a"),
                512,
                selection=selection,
                activity=activity,
            )

        command = ffmpeg.compile(run_ffmpeg.call_args.args[0])
        self.assertEqual(
            command[command.index("-filter:a:0") + 1],
            "pan=5.0|FL=FL|FR=FR|FC=FC|BL=SL|BR=SR",
        )
        self.assertEqual(
            command[command.index("-filter:a:1") + 1],
            "pan=5.1|FL<FL+0.707*FLC|FR<FR+0.707*FRC|FC=FC|LFE=LFE|BL=SL|BR=SR",
        )
        self.assertNotIn("-filter:a:2", command)
        self.assertEqual(command[command.index("-channel_layout:a:0") + 1], "5.0")
        self.assertEqual(command[command.index("-channel_layout:a:1") + 1], "5.1")
        self.assertEqual(command[command.index("-channel_layout:a:2") + 1], "stereo")
        _, fields = activity.warning.call_args
        self.assertEqual(
            [decision["action"] for decision in fields["layout_decisions"]],
            ["remap", "downmix", "preserve"],
        )

    def test_aac_transcode_rejects_unknown_layout_before_ffmpeg(self) -> None:
        streams = [qualified_stream(index=3, channels=8, channel_layout="unknown")]
        activity = Mock()

        with (
            patch.object(audio, "audio_streams_for_selector", return_value=streams),
            patch.object(audio, "audio_handler_metadata_options") as metadata_options,
            patch.object(audio, "run_ffmpeg_print_errors") as run_ffmpeg,
            self.assertRaisesRegex(AacLayoutPolicyError, "channel_layout_not_qualified"),
        ):
            audio.transcode_audio(
                Path("source.mkv"),
                Path("audio.m4a"),
                384,
                activity=activity,
            )

        metadata_options.assert_not_called()
        run_ffmpeg.assert_not_called()
        activity.warning.assert_called_once()
        _, fields = activity.warning.call_args
        self.assertEqual(fields["code"], "audio_layout_policy_rejected")
        self.assertEqual(fields["action"], "stop_conversion")
        self.assertEqual(fields["layout_decisions"][0]["action"], "fail")
        self.assertEqual(fields["layout_decisions"][0]["reason"], "channel_layout_not_qualified")

    @unittest.skipUnless(
        config.FFMPEG_PATH.is_file() and config.FFPROBE_PATH.is_file(),
        "FFmpeg integration tools are unavailable",
    )
    def test_real_ffmpeg_remap_produces_canonical_aac_and_preserves_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            source_path = temp_path / "source.mov"
            output_path = temp_path / "output.m4a"
            subprocess.run(
                [
                    config.FFMPEG_PATH,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "anullsrc=channel_layout=5.1(side):sample_rate=48000",
                    "-t",
                    "0.1",
                    "-c:a",
                    "pcm_s16le",
                    "-metadata:s:a:0",
                    "language=eng",
                    "-metadata:s:a:0",
                    "handler_name=English Side",
                    "-disposition:a:0",
                    "default",
                    "-y",
                    source_path,
                ],
                check=True,
            )
            activity = Mock()

            with patch.object(audio.config, "audio_mode", AudioMode.CONVERT_AAC):
                audio.transcode_audio(source_path, output_path, 384, activity=activity)

            probe = subprocess.run(
                [
                    config.FFPROBE_PATH,
                    "-v",
                    "error",
                    "-show_entries",
                    (
                        "stream=codec_name,profile,sample_rate,channels,channel_layout:"
                        "stream_tags=language,handler_name:stream_disposition=default"
                    ),
                    "-select_streams",
                    "a",
                    "-of",
                    "json",
                    output_path,
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            stream = json.loads(probe.stdout)["streams"][0]

            self.assertEqual(stream["codec_name"], "aac")
            self.assertEqual(stream["profile"], "LC")
            self.assertEqual(stream["sample_rate"], "48000")
            self.assertEqual(stream["channels"], 6)
            self.assertEqual(stream["channel_layout"], "5.1")
            self.assertEqual(stream["tags"]["language"], "eng")
            self.assertEqual(stream["tags"]["handler_name"], "English Side")
            self.assertEqual(stream["disposition"]["default"], 1)
            _, fields = activity.warning.call_args
            self.assertEqual(fields["code"], "audio_layout_policy_applied")
            self.assertEqual(fields["layout_decisions"][0]["action"], "remap")

    def test_audio_preparation_preserves_stream_titles_as_handler_names(self) -> None:
        streams = [
            qualified_stream(index=0, title="Main 5.1"),
            qualified_stream(index=1, title="Alternate Stereo", channels=2, channel_layout="stereo"),
        ]
        with (
            patch.object(audio, "audio_streams_for_selector", return_value=streams),
            patch.object(audio, "audio_handler_metadata_options") as metadata_options,
            patch.object(audio, "run_ffmpeg_print_errors") as run_ffmpeg,
        ):
            metadata_options.return_value = {
                "metadata:s:a:0": "handler_name=Main 5.1",
                "metadata:s:a:1": "handler_name=Alternate Stereo",
            }
            audio.transcode_audio(Path("source.mkv"), Path("audio.m4a"), 384)

        command = ffmpeg.compile(run_ffmpeg.call_args.args[0])
        self.assertIn("-metadata:s:a:0", command)
        self.assertIn("handler_name=Main 5.1", command)
        self.assertIn("-metadata:s:a:1", command)
        self.assertIn("handler_name=Alternate Stereo", command)
        self.assertNotIn("-metadata:s:a:2", command)
        metadata_options.assert_called_once_with(
            Path("source.mkv"),
            "a",
            selected_streams=streams,
            run_context=None,
            cancellation_event=None,
            observability_context=None,
        )


def audio_qualification(
    *,
    index: int = 0,
    codec_name: str = "aac",
    profile: str | None = "lc",
    qualified: bool = True,
    reason: str | None = None,
    sample_rate: int | None = 48_000,
    channels: int | None = 2,
    channel_layout: str | None = "stereo",
) -> audio.AudioStreamQualification:
    return audio.AudioStreamQualification(
        index,
        codec_name,
        profile,
        qualified,
        reason,
        sample_rate,
        channels,
        channel_layout,
    )


def qualified_stream(
    *,
    index: int,
    codec_name: str = "aac",
    profile: str = "LC",
    sample_rate: str = "48000",
    channels: int = 2,
    channel_layout: str = "stereo",
    language: str = "eng",
    title: str | None = None,
    is_default: bool = False,
) -> dict[str, object]:
    tags: dict[str, object] = {"language": language}
    if title is not None:
        tags["title"] = title
    return {
        "index": index,
        "codec_name": codec_name,
        "profile": profile,
        "sample_rate": sample_rate,
        "channels": channels,
        "channel_layout": channel_layout,
        "tags": tags,
        "disposition": {"default": int(is_default)},
    }


if __name__ == "__main__":
    unittest.main()
