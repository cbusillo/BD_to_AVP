import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import ffmpeg

from bd_to_avp.modules import audio
from bd_to_avp.modules.audio_mode import AudioMode
from bd_to_avp.modules.config import Stage


class AudioPreparationTests(unittest.TestCase):
    def test_automatic_copies_all_qualified_aac_to_owned_m4a(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            source_path = temp_path / "source.mkv"
            output_folder = temp_path / "Movie"
            source_path.write_bytes(b"source")
            output_folder.mkdir()

            def copy_audio(_input_path: Path, output_path: Path) -> None:
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
            qualify.assert_called_once_with(source_path)
            copy.assert_called_once_with(source_path, temporary_path)
            transcode.assert_not_called()

    def test_automatic_mixed_codecs_transcodes_whole_set_and_warns(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            source_path = temp_path / "source.mkv"
            output_folder = temp_path / "Movie"
            source_path.write_bytes(b"source")
            output_folder.mkdir()
            activity = Mock()

            def write_audio(_input_path: Path, output_path: Path, _bitrate: int) -> None:
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
            qualify.assert_called_once_with(source_path)
            copy.assert_not_called()
            transcode.assert_called_once_with(source_path, output_folder / "Movie_audio_AAC.part.m4a", 512)
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

            def write_audio(_input_path: Path, output_path: Path, _bitrate: int) -> None:
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
            transcode.assert_called_once_with(source_path, output_folder / "Movie_audio_AAC.part.m4a", 384)

    def test_failed_preparation_removes_partial_output_but_preserves_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            source_path = temp_path / "source.mkv"
            output_folder = temp_path / "Movie"
            source_path.write_bytes(b"source")
            output_folder.mkdir()

            def fail_after_partial_write(_input_path: Path, output_path: Path, _bitrate: int) -> None:
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

            def copy_audio(_input_path: Path, output_path: Path) -> None:
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

            def copy_audio(_input_path: Path, output_path: Path) -> None:
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

    def test_transcode_audio_maps_requested_stream_selector(self) -> None:
        with patch.object(audio, "run_ffmpeg_print_errors") as run_ffmpeg:
            audio.transcode_audio(Path("source.mkv"), Path("audio.m4a"), 384, "a:0")

        command = ffmpeg.compile(run_ffmpeg.call_args.args[0])
        self.assertIn("0:a:0", command)
        self.assertIn("file:audio.m4a", command)
        self.assertIn("-map_metadata", command)

    def test_copy_audio_maps_all_audio_tracks_without_encoder(self) -> None:
        with patch.object(audio, "run_ffmpeg_print_errors") as run_ffmpeg:
            audio.copy_audio(Path("source.mkv"), Path("audio.m4a"))

        command = ffmpeg.compile(run_ffmpeg.call_args.args[0])
        self.assertIn("0:a", command)
        self.assertIn("copy", command)
        self.assertIn("file:audio.m4a", command)


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
) -> dict[str, object]:
    return {
        "index": index,
        "codec_name": codec_name,
        "profile": profile,
        "sample_rate": sample_rate,
        "channels": channels,
        "channel_layout": channel_layout,
    }


if __name__ == "__main__":
    unittest.main()
