import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ffmpeg

from bd_to_avp.modules import container
from bd_to_avp.modules.audio_mode import AudioMode
from bd_to_avp.modules.config import Stage
from bd_to_avp.modules.video_mode import VideoMode


class AudioExtractionTests(unittest.TestCase):
    def test_subtitle_filter_does_not_limit_extracted_audio_tracks(self) -> None:
        with (
            patch.object(container.config, "remove_extra_languages", True),
            patch.object(container, "get_audio_stream_data", return_value=[]),
            patch.object(container, "run_ffmpeg_print_errors") as run_ffmpeg,
        ):
            container.extract_mvc_and_audio(Path("source.mkv"), None, Path("audio.mov"))

        command = ffmpeg.compile(run_ffmpeg.call_args.args[0])
        self.assertIn("0:a", command)
        self.assertNotIn("0:a:0", command)

    def test_pcm_extraction_preserves_audio_titles_as_handler_names(self) -> None:
        with (
            patch.object(
                container,
                "get_audio_stream_data",
                return_value=[
                    {"tags": {"title": "Main 5.1"}},
                    {"tags": {"name": "Alternate Stereo"}},
                ],
            ),
            patch.object(container, "run_ffmpeg_print_errors") as run_ffmpeg,
        ):
            container.extract_mvc_and_audio(Path("source.mkv"), None, Path("audio.mov"))

        command = ffmpeg.compile(run_ffmpeg.call_args.args[0][0])
        self.assertIn("-metadata:s:a:0", command)
        self.assertIn("handler_name=Main 5.1", command)
        self.assertIn("-metadata:s:a:1", command)
        self.assertIn("handler_name=Alternate Stereo", command)

    def test_direct_pipeline_skips_intermediate_video_and_pcm(self) -> None:
        with (
            patch.object(container.config, "audio_mode", AudioMode.CONVERT_AAC),
            patch.object(container.config, "keep_files", False),
            patch.object(container.config, "start_stage", Stage.CREATE_MKV),
            patch.object(container, "run_ffmpeg_print_errors") as run_ffmpeg,
        ):
            audio_path, video_path = container.create_mvc_and_audio("Movie", Path("source.mkv"), Path("output"))

        self.assertEqual(audio_path, Path("source.mkv"))
        self.assertEqual(video_path, Path("source.mkv"))
        run_ffmpeg.assert_not_called()

    def test_keep_files_preserves_mvc_without_changing_aac_policy(self) -> None:
        with (
            patch.object(container.config, "audio_mode", AudioMode.CONVERT_AAC),
            patch.object(container.config, "keep_files", True),
            patch.object(container.config, "start_stage", Stage.CREATE_MKV),
            patch.object(container, "run_ffmpeg_print_errors") as run_ffmpeg,
        ):
            audio_path, video_path = container.create_mvc_and_audio("Movie", Path("source.mkv"), Path("output"))

        self.assertEqual(audio_path, Path("source.mkv"))
        self.assertEqual(video_path, Path("output/Movie_mvc.h264"))
        command = ffmpeg.compile(run_ffmpeg.call_args.args[0])
        self.assertNotIn("pcm_s24le", command)
        self.assertIn("file:output/Movie_mvc.h264", command)

    def test_pcm_mode_preserves_pcm_boundary(self) -> None:
        with (
            patch.object(container.config, "audio_mode", AudioMode.PCM),
            patch.object(container.config, "keep_files", False),
            patch.object(container.config, "start_stage", Stage.CREATE_MKV),
            patch.object(container, "audio_handler_metadata_options", return_value={}),
            patch.object(container, "run_ffmpeg_print_errors") as run_ffmpeg,
        ):
            audio_path, video_path = container.create_mvc_and_audio("Movie", Path("source.mkv"), Path("output"))

        command = ffmpeg.compile(run_ffmpeg.call_args.args[0])
        self.assertEqual(audio_path, Path("output/Movie_audio_PCM.mov"))
        self.assertEqual(video_path, Path("source.mkv"))
        self.assertIn("pcm_s24le", command)
        self.assertNotIn("file:output/Movie_mvc.h264", command)


class MuxCommandTests(unittest.TestCase):
    def test_final_mux_retains_inputs_until_completed_file_moves(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_folder = Path(temp_dir) / "Movie"
            output_folder.mkdir()
            mv_hevc_path = output_folder / "movie_MV-HEVC.mov"
            audio_path = output_folder / "movie_audio.m4a"
            mv_hevc_path.write_bytes(b"video")
            audio_path.write_bytes(b"audio")

            with (
                patch.object(container.config, "keep_files", False),
                patch.object(container.config, "start_stage", Stage.CREATE_MKV),
                patch.object(container.config, "video_mode", VideoMode.MV_HEVC),
                patch.object(container, "mux_video_audio_subs") as mux,
            ):
                result = container.create_muxed_file(audio_path, mv_hevc_path, output_folder, "Movie")

            self.assertEqual(result, output_folder / "Movie_AVP.mov")
            mux.assert_called_once_with(
                mv_hevc_path,
                audio_path,
                output_folder / "Movie_AVP.mov",
                output_folder,
                run_context=None,
                cancellation_event=None,
                observability_context=None,
            )
            self.assertTrue(mv_hevc_path.exists())
            self.assertTrue(audio_path.exists())

    def test_final_mux_failure_retains_inputs_for_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_folder = Path(temp_dir) / "Movie"
            output_folder.mkdir()
            mv_hevc_path = output_folder / "movie_MV-HEVC.mov"
            audio_path = output_folder / "movie_audio.m4a"
            mv_hevc_path.write_bytes(b"video")
            audio_path.write_bytes(b"audio")

            with (
                patch.object(container.config, "keep_files", False),
                patch.object(container.config, "start_stage", Stage.CREATE_MKV),
                patch.object(container.config, "video_mode", VideoMode.MV_HEVC),
                patch.object(container, "mux_video_audio_subs", side_effect=RuntimeError("mux failed")),
                self.assertRaisesRegex(RuntimeError, "mux failed"),
            ):
                container.create_muxed_file(audio_path, mv_hevc_path, output_folder, "Movie")

            self.assertTrue(mv_hevc_path.exists())
            self.assertTrue(audio_path.exists())

    def test_final_mux_forces_video_sync_samples_for_quicktime_seeking(self) -> None:
        with (
            patch.object(container.config, "MP4BOX_PATH", Path("/tools/MP4Box")),
            patch.object(container.config, "video_mode", VideoMode.MV_HEVC),
            patch.object(
                container,
                "get_audio_stream_data",
                return_value=[{"index": 0, "tags": {"language": "eng"}, "channel_layout": "7.1"}],
            ),
            patch.object(container, "sorted_files_by_creation_filtered_on_suffix", return_value=[]),
            patch.object(container, "run_process_capture") as run_command,
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

    def test_av1_final_mux_preserves_existing_sync_samples(self) -> None:
        with (
            patch.object(container.config, "MP4BOX_PATH", Path("/tools/MP4Box")),
            patch.object(container.config, "video_mode", VideoMode.AV1_SBS),
            patch.object(container, "get_audio_stream_data", return_value=[]),
            patch.object(container, "sorted_files_by_creation_filtered_on_suffix", return_value=[]),
            patch.object(container, "run_process_capture") as run_command,
        ):
            container.mux_video_audio_subs(
                Path("movie_AV1-Stereo.mp4"),
                Path("audio_PCM.mov"),
                Path("movie_AV1_Stereo.mov"),
                Path("."),
            )

        command = run_command.call_args.args[0]
        self.assertEqual(command[:4], [Path("/tools/MP4Box"), "-new", "-add", Path("movie_AV1-Stereo.mp4")])

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
            patch.object(container, "run_process_capture") as run_command,
        ):
            container.mux_video_audio_subs(
                Path("movie_MV-HEVC.mov"),
                Path("Movie_audio_AAC.m4a"),
                Path("movie_AVP.mov"),
                Path("."),
            )

        command = run_command.call_args.args[0]
        self.assertIn("Movie_audio_AAC.m4a#1:lang=eng:group=1:alternate_group=1", command)
        self.assertIn("Movie_audio_AAC.m4a#2:lang=fra:group=1:alternate_group=1:disable", command)

    def test_final_mux_preserves_audio_title_and_default_disposition(self) -> None:
        with (
            patch.object(container.config, "MP4BOX_PATH", Path("/tools/MP4Box")),
            patch.object(
                container,
                "get_audio_stream_data",
                return_value=[
                    {
                        "index": 0,
                        "tags": {"language": "eng", "title": "Commentary"},
                        "channel_layout": "stereo",
                        "disposition": {"default": 0},
                    },
                    {
                        "index": 1,
                        "tags": {"language": "jpn", "title": "Main Japanese"},
                        "channel_layout": "5.1",
                        "disposition": {"default": 1},
                    },
                ],
            ),
            patch.object(container, "sorted_files_by_creation_filtered_on_suffix", return_value=[]),
            patch.object(container, "run_process_capture") as run_command,
        ):
            container.mux_video_audio_subs(
                Path("movie_MV-HEVC.mov"),
                Path("Movie_audio_AAC.m4a"),
                Path("movie_AVP.mov"),
                Path("."),
            )

        command = run_command.call_args.args[0]
        self.assertIn("Movie_audio_AAC.m4a#1:lang=eng:group=1:alternate_group=1:disable", command)
        self.assertIn("2:type=name:str='Commentary'", command)
        self.assertIn("Movie_audio_AAC.m4a#2:lang=jpn:group=1:alternate_group=1:enabled", command)
        self.assertIn("3:type=name:str='Main Japanese'", command)

    def test_final_mux_uses_m4a_name_tag_as_audio_title(self) -> None:
        with (
            patch.object(container.config, "MP4BOX_PATH", Path("/tools/MP4Box")),
            patch.object(
                container,
                "get_audio_stream_data",
                return_value=[
                    {
                        "index": 0,
                        "tags": {"language": "eng", "name": "Director Commentary"},
                        "channel_layout": "stereo",
                    }
                ],
            ),
            patch.object(container, "sorted_files_by_creation_filtered_on_suffix", return_value=[]),
            patch.object(container, "run_process_capture") as run_command,
        ):
            container.mux_video_audio_subs(
                Path("movie_MV-HEVC.mov"),
                Path("Movie_audio_AAC.m4a"),
                Path("movie_AVP.mov"),
                Path("."),
            )

        command = run_command.call_args.args[0]
        self.assertIn("2:type=name:str='Director Commentary'", command)

    def test_final_mux_uses_preserved_handler_name_as_audio_title(self) -> None:
        with (
            patch.object(container.config, "MP4BOX_PATH", Path("/tools/MP4Box")),
            patch.object(
                container,
                "get_audio_stream_data",
                return_value=[
                    {
                        "index": 0,
                        "tags": {"language": "eng", "handler_name": "Main 5.1"},
                        "channels": 6,
                    }
                ],
            ),
            patch.object(container, "sorted_files_by_creation_filtered_on_suffix", return_value=[]),
            patch.object(container, "run_process_capture") as run_command,
        ):
            container.mux_video_audio_subs(
                Path("movie_MV-HEVC.mov"),
                Path("Movie_audio_AAC.m4a"),
                Path("movie_AVP.mov"),
                Path("."),
            )

        command = run_command.call_args.args[0]
        self.assertIn("2:type=name:str='Main 5.1'", command)

    def test_final_mux_uses_channel_count_when_layout_and_title_are_missing(self) -> None:
        with (
            patch.object(container.config, "MP4BOX_PATH", Path("/tools/MP4Box")),
            patch.object(
                container,
                "get_audio_stream_data",
                return_value=[
                    {
                        "index": 0,
                        "tags": {"language": "eng", "handler_name": "SoundHandler"},
                        "channels": 6,
                    }
                ],
            ),
            patch.object(container, "sorted_files_by_creation_filtered_on_suffix", return_value=[]),
            patch.object(container, "run_process_capture") as run_command,
        ):
            container.mux_video_audio_subs(
                Path("movie_MV-HEVC.mov"),
                Path("Movie_audio_AAC.m4a"),
                Path("movie_AVP.mov"),
                Path("."),
            )

        command = run_command.call_args.args[0]
        self.assertIn("2:type=name:str='English 6-channel Audio'", command)

    def test_final_mux_normalizes_bibliographic_audio_language(self) -> None:
        with (
            patch.object(container.config, "MP4BOX_PATH", Path("/tools/MP4Box")),
            patch.object(
                container,
                "get_audio_stream_data",
                return_value=[{"index": 0, "tags": {"language": "ger"}, "channel_layout": "5.1"}],
            ),
            patch.object(container, "sorted_files_by_creation_filtered_on_suffix", return_value=[]),
            patch.object(container, "run_process_capture") as run_command,
        ):
            container.mux_video_audio_subs(
                Path("movie_MV-HEVC.mov"),
                Path("Movie_audio_AAC.m4a"),
                Path("movie_AVP.mov"),
                Path("."),
            )

        command = run_command.call_args.args[0]
        self.assertIn("Movie_audio_AAC.m4a#1:lang=deu:group=1:alternate_group=1", command)
        self.assertIn("2:type=name:str='German 5.1 Audio'", command)

    def test_final_mux_uses_unknown_for_invalid_audio_language(self) -> None:
        with (
            patch.object(container.config, "MP4BOX_PATH", Path("/tools/MP4Box")),
            patch.object(
                container,
                "get_audio_stream_data",
                return_value=[{"index": 0, "tags": {"language": "xxx"}, "channel_layout": "stereo"}],
            ),
            patch.object(container, "sorted_files_by_creation_filtered_on_suffix", return_value=[]),
            patch.object(container, "run_process_capture") as run_command,
        ):
            container.mux_video_audio_subs(
                Path("movie_MV-HEVC.mov"),
                Path("Movie_audio_AAC.m4a"),
                Path("movie_AVP.mov"),
                Path("."),
            )

        command = run_command.call_args.args[0]
        self.assertIn("Movie_audio_AAC.m4a#1:lang=und:group=1:alternate_group=1", command)
        self.assertIn("2:type=name:str='Unknown stereo Audio'", command)

    def test_final_mux_uses_unknown_when_audio_language_is_missing(self) -> None:
        with (
            patch.object(container.config, "MP4BOX_PATH", Path("/tools/MP4Box")),
            patch.object(
                container,
                "get_audio_stream_data",
                return_value=[{"index": 0, "channel_layout": "stereo"}],
            ),
            patch.object(container, "sorted_files_by_creation_filtered_on_suffix", return_value=[]),
            patch.object(container, "run_process_capture") as run_command,
        ):
            container.mux_video_audio_subs(
                Path("movie_MV-HEVC.mov"),
                Path("Movie_audio_AAC.m4a"),
                Path("movie_AVP.mov"),
                Path("."),
            )

        command = run_command.call_args.args[0]
        self.assertIn("Movie_audio_AAC.m4a#1:lang=und:group=1:alternate_group=1", command)
        self.assertIn("2:type=name:str='Unknown stereo Audio'", command)

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
            patch.object(container, "run_process_capture") as run_command,
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

    def test_final_mux_supports_alpha3_only_subtitle_filenames(self) -> None:
        with (
            patch.object(container.config, "MP4BOX_PATH", Path("/tools/MP4Box")),
            patch.object(container, "get_audio_stream_data", return_value=[]),
            patch.object(
                container,
                "sorted_files_by_creation_filtered_on_suffix",
                return_value=[Path("movie.ace.srt")],
            ),
            patch.object(container, "run_process_capture") as run_command,
        ):
            container.mux_video_audio_subs(
                Path("movie_MV-HEVC.mov"),
                Path("audio_PCM.mov"),
                Path("movie_AVP.mov"),
                Path("."),
            )

        command = run_command.call_args.args[0]
        self.assertIn("movie.ace.srt#1:hdlr=sbtl:lang=ace:group=2:name=Achinese Subtitles:tx3g", command)


if __name__ == "__main__":
    unittest.main()
