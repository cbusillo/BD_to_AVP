import unittest
from pathlib import Path
from unittest.mock import patch

import ffmpeg

from bd_to_avp.modules import container
from bd_to_avp.modules.config import Stage


class AudioExtractionTests(unittest.TestCase):
    def test_direct_audio_transcode_extracts_video_without_pcm(self) -> None:
        with (
            patch.object(container.config, "direct_pipeline", True),
            patch.object(container.config, "transcode_audio", True),
            patch.object(container.config, "keep_files", False),
            patch.object(container.config, "start_stage", Stage.CREATE_MKV),
            patch.object(container, "run_ffmpeg_print_errors") as run_ffmpeg,
        ):
            audio_path, video_path = container.create_mvc_and_audio("Movie", Path("source.mkv"), Path("output"))

        command = ffmpeg.compile(run_ffmpeg.call_args.args[0])
        self.assertEqual(audio_path, Path("source.mkv"))
        self.assertEqual(video_path, Path("output/Movie_mvc.h264"))
        self.assertNotIn("output/Movie_audio_PCM.mov", " ".join(command))
        self.assertNotIn("pcm_s24le", command)

    def test_durable_audio_path_still_extracts_pcm(self) -> None:
        with (
            patch.object(container.config, "direct_pipeline", False),
            patch.object(container.config, "transcode_audio", True),
            patch.object(container.config, "keep_files", False),
            patch.object(container.config, "start_stage", Stage.CREATE_MKV),
            patch.object(container, "run_ffmpeg_print_errors") as run_ffmpeg,
        ):
            audio_path, _video_path = container.create_mvc_and_audio("Movie", Path("source.mkv"), Path("output"))

        command = ffmpeg.compile(run_ffmpeg.call_args.args[0])
        self.assertEqual(audio_path, Path("output/Movie_audio_PCM.mov"))
        self.assertIn("pcm_s24le", command)
        self.assertIn("file:output/Movie_audio_PCM.mov", command)

    def test_keep_files_preserves_pcm_boundary_in_direct_mode(self) -> None:
        with (
            patch.object(container.config, "direct_pipeline", True),
            patch.object(container.config, "transcode_audio", True),
            patch.object(container.config, "keep_files", True),
            patch.object(container.config, "start_stage", Stage.CREATE_MKV),
            patch.object(container, "run_ffmpeg_print_errors") as run_ffmpeg,
        ):
            audio_path, _video_path = container.create_mvc_and_audio("Movie", Path("source.mkv"), Path("output"))

        command = ffmpeg.compile(run_ffmpeg.call_args.args[0])
        self.assertEqual(audio_path, Path("output/Movie_audio_PCM.mov"))
        self.assertIn("pcm_s24le", command)

    def test_direct_mode_without_audio_transcode_preserves_pcm_boundary(self) -> None:
        with (
            patch.object(container.config, "direct_pipeline", True),
            patch.object(container.config, "transcode_audio", False),
            patch.object(container.config, "keep_files", False),
            patch.object(container.config, "start_stage", Stage.CREATE_MKV),
            patch.object(container, "run_ffmpeg_print_errors") as run_ffmpeg,
        ):
            audio_path, _video_path = container.create_mvc_and_audio("Movie", Path("source.mkv"), Path("output"))

        command = ffmpeg.compile(run_ffmpeg.call_args.args[0])
        self.assertEqual(audio_path, Path("output/Movie_audio_PCM.mov"))
        self.assertIn("pcm_s24le", command)


class MuxCommandTests(unittest.TestCase):
    def test_final_mux_forces_video_sync_samples_for_quicktime_seeking(self) -> None:
        with (
            patch.object(container.config, "MP4BOX_PATH", Path("/tools/MP4Box")),
            patch.object(
                container,
                "get_audio_stream_data",
                return_value=[{"index": 0, "tags": {"language": "eng"}, "channel_layout": "7.1"}],
            ),
            patch.object(container, "sorted_files_by_creation_filtered_on_suffix", return_value=[]),
            patch.object(container, "run_command") as run_command,
        ):
            container.mux_video_audio_subs(
                Path("movie_MV-HEVC.mov"),
                Path("audio_PCM.mov"),
                Path("movie_AVP.mov"),
                Path("."),
            )

        command = run_command.call_args.args[0]
        self.assertEqual(command[:4], [Path("/tools/MP4Box"), "-new", "-add", "movie_MV-HEVC.mov:forcesync"])
        self.assertIn("audio_PCM.mov#1:lang=eng:group=1:alternate_group=1", command)
        self.assertEqual(command[-1], Path("movie_AVP.mov"))

    def test_final_mux_preserves_multiple_direct_aac_tracks(self) -> None:
        with (
            patch.object(container.config, "MP4BOX_PATH", Path("/tools/MP4Box")),
            patch.object(
                container,
                "get_audio_stream_data",
                return_value=[
                    {"index": 0, "tags": {"language": "eng"}, "channel_layout": "5.1"},
                    {"index": 1, "tags": {"language": "fra"}, "channel_layout": "stereo"},
                ],
            ),
            patch.object(container, "sorted_files_by_creation_filtered_on_suffix", return_value=[]),
            patch.object(container, "run_command") as run_command,
        ):
            container.mux_video_audio_subs(
                Path("movie_MV-HEVC.mov"),
                Path("Movie_audio_AAC.mov"),
                Path("movie_AVP.mov"),
                Path("."),
            )

        command = run_command.call_args.args[0]
        self.assertIn("Movie_audio_AAC.mov#1:lang=eng:group=1:alternate_group=1", command)
        self.assertIn("Movie_audio_AAC.mov#2:lang=fra:group=1:alternate_group=1:disable", command)

    def test_final_mux_marks_forced_subtitles_for_quicktime(self) -> None:
        with (
            patch.object(container.config, "MP4BOX_PATH", Path("/tools/MP4Box")),
            patch.object(
                container,
                "get_audio_stream_data",
                return_value=[{"index": 0, "tags": {"language": "eng"}, "channel_layout": "7.1"}],
            ),
            patch.object(
                container,
                "sorted_files_by_creation_filtered_on_suffix",
                return_value=[Path("movie.forced.en.srt"), Path("movie.en.srt")],
            ),
            patch.object(container, "run_command") as run_command,
        ):
            container.mux_video_audio_subs(
                Path("movie_MV-HEVC.mov"),
                Path("audio_PCM.mov"),
                Path("movie_AVP.mov"),
                Path("."),
            )

        command = run_command.call_args.args[0]
        self.assertIn(
            "movie.forced.en.srt#1:hdlr=sbtl:lang=eng:group=2:name=English Subtitles:tx3g:txtflags=0xC0000000",
            command,
        )
        self.assertIn("3:type=name:str='English Forced Subtitles'", command)
        self.assertIn("movie.en.srt#1:hdlr=sbtl:lang=eng:group=2:name=English Subtitles:tx3g", command)
        self.assertIn("4:type=name:str='English Subtitles'", command)


if __name__ == "__main__":
    unittest.main()
