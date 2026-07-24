import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import ffmpeg

from bd_to_avp.modules import audio
from bd_to_avp.modules.audio_mode import AudioMode
from bd_to_avp.modules.audio_selection import load_audio_selection_manifest, select_audio_streams
from bd_to_avp.modules.config import Stage


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
                patch.object(audio, "transcode_audio") as transcode,
                patch.object(audio, "copy_audio") as copy,
            ):
                result = audio.create_prepared_audio_file(source_path, output_folder)

            self.assertEqual(result, aac_path)
            transcode.assert_not_called()
            copy.assert_not_called()

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
            qualified_stream(index=7, channel_layout="4.0"),
            {"index": 8, "codec_name": "aac", "profile": "LC"},
            qualified_stream(index=9, channel_layout="5.1(side)"),
        ]

        results = [audio.qualify_audio_stream(stream) for stream in streams]

        self.assertTrue(results[0].qualified)
        self.assertEqual(results[1].reason, "codec_not_allowed")
        self.assertEqual(results[2].reason, "codec_not_allowed")
        self.assertEqual(results[3].reason, "codec_not_aac")
        self.assertEqual(results[4].reason, "aac_profile_not_qualified")
        self.assertEqual(results[5].reason, "sample_rate_not_qualified")
        self.assertEqual(results[6].reason, "channel_layout_mismatch")
        self.assertEqual(results[7].reason, "channel_layout_not_qualified")
        self.assertEqual(results[8].reason, "sample_rate_missing")
        self.assertEqual(results[9].reason, "channel_layout_not_qualified")

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
        metadata_options.assert_called_once_with(
            Path("source.mkv"),
            selected_streams=[streams[0], streams[2]],
            run_context=None,
            cancellation_event=None,
            observability_context=None,
        )

    def test_aac_transcode_explicitly_maps_the_same_preferred_track_set(self) -> None:
        streams = [
            qualified_stream(index=1, language="eng"),
            qualified_stream(index=4, language="jpn"),
            qualified_stream(index=8, language="eng"),
        ]
        selection = select_audio_streams(streams, "eng")

        with (
            patch.object(audio, "audio_handler_metadata_options", return_value={}),
            patch.object(audio, "run_ffmpeg_print_errors") as run_ffmpeg,
        ):
            audio.transcode_audio(Path("source.mkv"), Path("audio.m4a"), 384, selection=selection)

        command = ffmpeg.compile(run_ffmpeg.call_args.args[0])
        self.assertIn("0:a:0", command)
        self.assertIn("0:a:2", command)
        self.assertNotIn("0:a:1", command)
        self.assertIn("aac", command)

    def test_aac_transcode_normalizes_side_surround_for_apple_playback(self) -> None:
        streams = [
            qualified_stream(index=1, language="eng", channel_layout="5.1(side)", channels=6),
            qualified_stream(index=4, language="eng", channel_layout="stereo", channels=2),
        ]
        selection = select_audio_streams(streams, "eng")

        with (
            patch.object(audio, "audio_handler_metadata_options", return_value={}),
            patch.object(audio, "run_ffmpeg_print_errors") as run_ffmpeg,
        ):
            audio.transcode_audio(Path("source.mkv"), Path("audio.m4a"), 384, selection=selection)

        command = ffmpeg.compile(run_ffmpeg.call_args.args[0])
        self.assertIn("-channel_layout:a:0", command)
        self.assertIn("5.1", command)
        self.assertNotIn("-channel_layout:a:1", command)

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
        "disposition": {"default": 0},
    }


if __name__ == "__main__":
    unittest.main()
