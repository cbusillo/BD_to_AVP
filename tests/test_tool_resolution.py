import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bd_to_avp.modules.config import config, resolve_makemkvcon_path, resolve_tool_path, tool_env_var
from bd_to_avp.vendor.pgsrip import mkv


class ToolResolutionTests(unittest.TestCase):
    def test_tool_env_var_normalizes_names(self) -> None:
        self.assertEqual(tool_env_var("MP4Box"), "BD_TO_AVP_MP4BOX_PATH")
        self.assertEqual(tool_env_var("mkv-extract"), "BD_TO_AVP_MKV_EXTRACT_PATH")

    def test_env_override_wins(self) -> None:
        with patch.dict(os.environ, {"BD_TO_AVP_FFMPEG_PATH": "/custom/ffmpeg"}, clear=False):
            self.assertEqual(resolve_tool_path("ffmpeg"), Path("/custom/ffmpeg"))

    def test_app_local_bin_wins_over_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bin_path = Path(temp_dir)
            bundled_tool = bin_path / "MP4Box"
            bundled_tool.touch()

            with (
                patch.dict(os.environ, {}, clear=True),
                patch("bd_to_avp.modules.config.shutil.which", return_value="/usr/bin/MP4Box"),
            ):
                self.assertEqual(resolve_tool_path("MP4Box", script_bin_path=bin_path), bundled_tool)

    def test_app_local_ffmpeg_wins_over_homebrew_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bin_path = Path(temp_dir)
            bundled_ffmpeg = bin_path / "ffmpeg"
            bundled_ffprobe = bin_path / "ffprobe"
            bundled_ffmpeg.touch()
            bundled_ffprobe.touch()

            with (
                patch.dict(os.environ, {}, clear=True),
                patch("bd_to_avp.modules.config.shutil.which", side_effect=lambda tool: f"/opt/homebrew/bin/{tool}"),
            ):
                self.assertEqual(resolve_tool_path("ffmpeg", script_bin_path=bin_path), bundled_ffmpeg)
                self.assertEqual(resolve_tool_path("ffprobe", script_bin_path=bin_path), bundled_ffprobe)

    def test_path_wins_over_homebrew_fallback(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("bd_to_avp.modules.config.shutil.which", return_value="/usr/bin/example-tool"),
        ):
            self.assertEqual(resolve_tool_path("example-tool"), Path("/usr/bin/example-tool"))

    def test_homebrew_path_is_last_resort(self) -> None:
        with patch.dict(os.environ, {}, clear=True), patch("bd_to_avp.modules.config.shutil.which", return_value=None):
            self.assertEqual(resolve_tool_path("example-tool"), Path("/opt/homebrew/bin/example-tool"))

    def test_makemkv_app_bundle_is_used_before_homebrew_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app_bundle_bin = Path(temp_dir)
            makemkvcon = app_bundle_bin / "makemkvcon"
            makemkvcon.touch()

            with (
                patch.dict(os.environ, {}, clear=True),
                patch("bd_to_avp.modules.config.MAKEMKV_APP_BUNDLE_BIN", app_bundle_bin),
                patch("bd_to_avp.modules.config.shutil.which", return_value=None),
            ):
                self.assertEqual(resolve_makemkvcon_path(), makemkvcon)

    def test_configure_tool_environment_orders_configured_paths_before_bundled_bin(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            custom_bin = temp_path / "custom bin"
            app_bin = temp_path / "app bin"
            for path in [custom_bin, app_bin]:
                path.mkdir()

            with (
                patch.object(config, "SCRIPT_PATH_BIN", app_bin),
                patch.object(config, "FFMPEG_PATH", custom_bin / "ffmpeg"),
                patch.object(config, "FFPROBE_PATH", custom_bin / "ffprobe"),
                patch.object(config, "MAKEMKVCON_PATH", custom_bin / "makemkvcon"),
                patch.object(config, "MP4BOX_PATH", custom_bin / "MP4Box"),
                patch.dict(os.environ, {"PATH": "/usr/bin"}, clear=True),
            ):
                config.configure_tool_environment()
                path_dirs = os.environ["PATH"].split(os.pathsep)

        self.assertEqual(path_dirs[:3], [custom_bin.as_posix(), app_bin.as_posix(), "/usr/bin"])


class MkvToolPathTests(unittest.TestCase):
    def test_mkv_metadata_uses_configured_ffprobe_path(self) -> None:
        metadata: dict[str, object] = {"streams": []}
        with patch("bd_to_avp.vendor.pgsrip.mkv.run_ffprobe", return_value=metadata) as probe:
            mkv.Mkv("movie.mkv")

        probe.assert_called_once_with(
            "movie.mkv",
            run_context=None,
            cancellation_event=None,
            observability_context=None,
        )

    def test_mkv_metadata_maps_ffprobe_pgs_streams_to_existing_track_model(self) -> None:
        metadata = {
            "streams": [
                {"index": 0, "codec_type": "video", "codec_name": "h264"},
                {
                    "index": 4,
                    "codec_type": "subtitle",
                    "codec_name": "hdmv_pgs_subtitle",
                    "tags": {"language": "eng", "title": "English forced"},
                    "disposition": {"default": 1, "forced": 1},
                },
            ]
        }

        with patch("bd_to_avp.vendor.pgsrip.mkv.run_ffprobe", return_value=metadata):
            movie = mkv.Mkv("movie.mkv")

        video_track, subtitle_track = movie.tracks
        self.assertEqual(video_track.type, "video")
        self.assertEqual(subtitle_track.id, 4)
        self.assertEqual(subtitle_track.type, "subtitles")
        self.assertEqual(subtitle_track.codec, "HDMV PGS")
        self.assertEqual(str(subtitle_track.language), "en")
        self.assertTrue(subtitle_track.forced)
        self.assertTrue(subtitle_track.properties["default_track"])

    def test_mkv_metadata_preserves_ffprobe_language_ietf_tag(self) -> None:
        track = mkv.MkvTrack.from_ffprobe_stream(
            {
                "index": 2,
                "codec_type": "subtitle",
                "codec_name": "hdmv_pgs_subtitle",
                "tags": {"language": "por", "language_ietf": "pt-BR"},
                "disposition": {},
            }
        )

        self.assertEqual(track.properties["language"], "por")
        self.assertEqual(track.properties["language_ietf"], "pt-BR")

    def test_pgs_selection_ignores_disabled_ffprobe_subtitle_streams(self) -> None:
        metadata = {
            "streams": [
                {
                    "index": 3,
                    "codec_type": "subtitle",
                    "codec_name": "hdmv_pgs_subtitle",
                    "tags": {"language": "eng"},
                    "disposition": {"disabled": 1},
                },
                {
                    "index": 4,
                    "codec_type": "subtitle",
                    "codec_name": "hdmv_pgs_subtitle",
                    "tags": {"language": "eng"},
                    "disposition": {},
                },
            ]
        }

        with patch("bd_to_avp.vendor.pgsrip.mkv.run_ffprobe", return_value=metadata):
            movie = mkv.Mkv("movie.mkv")

        selected = list(movie.get_selected_pgs_tracks(mkv.Options(overwrite=True, one_per_lang=False)))

        self.assertEqual([track.id for track, _, _ in selected], [4])

    def test_mkv_metadata_wraps_ffprobe_errors_as_called_process_error(self) -> None:
        error = mkv.ffmpeg.Error("ffprobe", b"out", b"err")
        with (
            patch.object(mkv.config, "FFPROBE_PATH", Path("/tools/ffprobe")),
            patch("bd_to_avp.vendor.pgsrip.mkv.run_ffprobe", side_effect=error),
            self.assertRaises(subprocess.CalledProcessError) as raised,
        ):
            mkv.Mkv("movie.mkv")

        self.assertEqual(raised.exception.cmd, [Path("/tools/ffprobe"), "movie.mkv"])
        self.assertEqual(raised.exception.stderr, b"err")

    def test_mkv_metadata_wraps_malformed_ffprobe_output_as_called_process_error(self) -> None:
        errors = [
            json.JSONDecodeError("bad metadata", "", 0),
            UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad metadata"),
        ]
        for error in errors:
            with (
                self.subTest(error=type(error).__name__),
                patch("bd_to_avp.vendor.pgsrip.mkv.run_ffprobe", side_effect=error),
                self.assertRaises(subprocess.CalledProcessError) as raised,
            ):
                mkv.Mkv("movie.mkv")

            self.assertIn(b"bad metadata", raised.exception.stderr)

    def test_pgs_selection_ignores_non_pgs_subtitle_streams(self) -> None:
        metadata = {
            "streams": [
                {
                    "index": 2,
                    "codec_type": "subtitle",
                    "codec_name": "subrip",
                    "tags": {"language": "eng"},
                    "disposition": {"forced": 0},
                },
                {
                    "index": 4,
                    "codec_type": "subtitle",
                    "codec_name": "hdmv_pgs_subtitle",
                    "tags": {"language": "eng"},
                    "disposition": {"forced": 0},
                },
            ]
        }

        with patch("bd_to_avp.vendor.pgsrip.mkv.run_ffprobe", return_value=metadata):
            movie = mkv.Mkv("movie.mkv")

        selected = list(movie.get_selected_pgs_tracks(mkv.Options(overwrite=True, one_per_lang=False)))

        self.assertEqual([track.id for track, _, _ in selected], [4])

    def test_pgs_extraction_uses_configured_ffmpeg_path_and_stream_index(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            media_path = mkv.MediaPath("movie.mkv").translate(language=mkv.Language("eng"))

            with (
                patch.object(mkv.config, "FFMPEG_PATH", Path("/tools/ffmpeg")),
                patch("bd_to_avp.vendor.pgsrip.mkv.run_process_capture") as run,
            ):
                run.side_effect = lambda *_args, **kwargs: kwargs["stdout"].write(b"pgs")
                data = mkv.MkvPgs.read_data(media_path, 3, temp_dir)

        self.assertEqual(data, b"pgs")
        self.assertEqual(
            run.call_args.args[0],
            [
                Path("/tools/ffmpeg"),
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-i",
                str(media_path),
                "-map",
                "0:3",
                "-c:s",
                "copy",
                "-f",
                "sup",
                "pipe:1",
            ],
        )
        self.assertEqual(run.call_args.kwargs["tool_id"], "ffmpeg")
        artifact = run.call_args.kwargs["artifacts"][0]
        self.assertEqual(artifact.role, "subtitle_payload")
        self.assertEqual(artifact.path, Path(temp_dir) / "track-3.sup")


if __name__ == "__main__":
    unittest.main()
