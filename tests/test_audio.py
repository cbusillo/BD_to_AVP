import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ffmpeg

from bd_to_avp.modules import audio
from bd_to_avp.modules.config import Stage


class DirectAudioTranscodeTests(unittest.TestCase):
    def test_direct_transcode_reads_source_and_atomically_writes_aac(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            source_path = temp_path / "source.mkv"
            output_folder = temp_path / "Movie"
            source_path.write_bytes(b"source")
            output_folder.mkdir()

            def write_audio(_input_path: Path, output_path: Path, _bitrate: int, _selector: str) -> None:
                output_path.write_bytes(b"aac")

            with (
                patch.object(audio.config, "direct_pipeline", True),
                patch.object(audio.config, "transcode_audio", True),
                patch.object(audio.config, "keep_files", False),
                patch.object(audio.config, "remove_extra_languages", False),
                patch.object(audio.config, "start_stage", Stage.CREATE_MKV),
                patch.object(audio.config, "audio_bitrate", 384),
                patch.object(audio, "transcode_audio", side_effect=write_audio) as transcode,
            ):
                result = audio.create_transcoded_audio_file(source_path, output_folder)

            final_path = output_folder / "Movie_audio_AAC.mov"
            temporary_path = output_folder / "Movie_audio_AAC.part.mov"
            self.assertEqual(result, final_path)
            self.assertEqual(final_path.read_bytes(), b"aac")
            self.assertFalse(temporary_path.exists())
            self.assertFalse((output_folder / "Movie_audio_PCM.mov").exists())
            self.assertTrue(source_path.exists())
            transcode.assert_called_once_with(source_path, temporary_path, 384, "a")

    def test_direct_transcode_selects_first_audio_track_when_removing_languages(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            source_path = temp_path / "source.mkv"
            output_folder = temp_path / "Movie"
            source_path.write_bytes(b"source")
            output_folder.mkdir()

            def write_audio(_input_path: Path, output_path: Path, _bitrate: int, _selector: str) -> None:
                output_path.write_bytes(b"aac")

            with (
                patch.object(audio.config, "direct_pipeline", True),
                patch.object(audio.config, "transcode_audio", True),
                patch.object(audio.config, "keep_files", False),
                patch.object(audio.config, "remove_extra_languages", True),
                patch.object(audio.config, "start_stage", Stage.CREATE_MKV),
                patch.object(audio.config, "audio_bitrate", 384),
                patch.object(audio, "transcode_audio", side_effect=write_audio) as transcode,
            ):
                audio.create_transcoded_audio_file(source_path, output_folder)

            self.assertEqual(transcode.call_args.args[3], "a:0")

    def test_failed_direct_transcode_removes_partial_output_but_preserves_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            source_path = temp_path / "source.mkv"
            output_folder = temp_path / "Movie"
            source_path.write_bytes(b"source")
            output_folder.mkdir()

            def fail_after_partial_write(_input_path: Path, output_path: Path, _bitrate: int, _selector: str) -> None:
                output_path.write_bytes(b"partial")
                raise RuntimeError("transcode failed")

            with (
                patch.object(audio.config, "direct_pipeline", True),
                patch.object(audio.config, "transcode_audio", True),
                patch.object(audio.config, "keep_files", False),
                patch.object(audio.config, "remove_extra_languages", False),
                patch.object(audio.config, "start_stage", Stage.CREATE_MKV),
                patch.object(audio, "transcode_audio", side_effect=fail_after_partial_write),
                self.assertRaisesRegex(RuntimeError, "transcode failed"),
            ):
                audio.create_transcoded_audio_file(source_path, output_folder)

            self.assertTrue(source_path.exists())
            self.assertFalse((output_folder / "Movie_audio_AAC.part.mov").exists())
            self.assertFalse((output_folder / "Movie_audio_AAC.mov").exists())

    def test_keep_files_uses_durable_pcm_input(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_folder = Path(temp_dir) / "Movie"
            output_folder.mkdir()
            pcm_path = output_folder / "Movie_audio_PCM.mov"
            pcm_path.write_bytes(b"pcm")

            def write_audio(_input_path: Path, output_path: Path, _bitrate: int, _selector: str) -> None:
                output_path.write_bytes(b"aac")

            with (
                patch.object(audio.config, "direct_pipeline", True),
                patch.object(audio.config, "transcode_audio", True),
                patch.object(audio.config, "keep_files", True),
                patch.object(audio.config, "remove_extra_languages", True),
                patch.object(audio.config, "start_stage", Stage.CREATE_MKV),
                patch.object(audio, "transcode_audio", side_effect=write_audio) as transcode,
            ):
                audio.create_transcoded_audio_file(pcm_path, output_folder)

            self.assertTrue(pcm_path.exists())
            self.assertEqual(transcode.call_args.args[0], pcm_path)
            self.assertEqual(transcode.call_args.args[3], "a")

    def test_final_mux_resume_uses_existing_direct_aac(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            source_path = temp_path / "source.mkv"
            output_folder = temp_path / "Movie"
            source_path.write_bytes(b"source")
            output_folder.mkdir()
            aac_path = output_folder / "Movie_audio_AAC.mov"
            aac_path.write_bytes(b"aac")

            with (
                patch.object(audio.config, "direct_pipeline", True),
                patch.object(audio.config, "transcode_audio", True),
                patch.object(audio.config, "keep_files", False),
                patch.object(audio.config, "remove_extra_languages", False),
                patch.object(audio.config, "start_stage", Stage.CREATE_FINAL_FILE),
                patch.object(audio.config, "audio_bitrate", 384),
                patch.object(audio, "transcode_audio") as transcode,
            ):
                result = audio.create_transcoded_audio_file(source_path, output_folder)

            self.assertEqual(result, aac_path)
            transcode.assert_not_called()

    def test_final_mux_resume_requires_existing_direct_aac(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            source_path = temp_path / "source.mkv"
            output_folder = temp_path / "Movie"
            source_path.write_bytes(b"source")
            output_folder.mkdir()

            with (
                patch.object(audio.config, "direct_pipeline", True),
                patch.object(audio.config, "transcode_audio", True),
                patch.object(audio.config, "keep_files", False),
                patch.object(audio.config, "remove_extra_languages", False),
                patch.object(audio.config, "start_stage", Stage.CREATE_FINAL_FILE),
                patch.object(audio.config, "audio_bitrate", 384),
                self.assertRaisesRegex(FileNotFoundError, "Direct AAC audio artifact not found"),
            ):
                audio.create_transcoded_audio_file(source_path, output_folder)

    def test_transcode_disabled_returns_original_audio(self) -> None:
        original_audio_path = Path("Movie_audio_PCM.mov")
        with (
            patch.object(audio.config, "direct_pipeline", True),
            patch.object(audio.config, "transcode_audio", False),
            patch.object(audio.config, "keep_files", False),
            patch.object(audio.config, "start_stage", Stage.TRANSCODE_AUDIO),
            patch.object(audio, "transcode_audio") as transcode,
        ):
            result = audio.create_transcoded_audio_file(original_audio_path, Path("Movie"))

        self.assertEqual(result, original_audio_path)
        transcode.assert_not_called()

    def test_transcode_audio_maps_requested_stream_selector(self) -> None:
        with patch.object(audio, "run_ffmpeg_print_errors") as run_ffmpeg:
            audio.transcode_audio(Path("source.mkv"), Path("audio.mov"), 384, "a:0")

        command = ffmpeg.compile(run_ffmpeg.call_args.args[0])
        self.assertIn("0:a:0", command)
        self.assertIn("file:audio.mov", command)


if __name__ == "__main__":
    unittest.main()
