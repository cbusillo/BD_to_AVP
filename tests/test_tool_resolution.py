import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from bd_to_avp.modules.config import config, resolve_tool_path, tool_env_var
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

    def test_path_wins_over_homebrew_fallback(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("bd_to_avp.modules.config.shutil.which", return_value="/usr/bin/mkvmerge"),
        ):
            self.assertEqual(resolve_tool_path("mkvmerge"), Path("/usr/bin/mkvmerge"))

    def test_homebrew_path_is_last_resort(self) -> None:
        with patch.dict(os.environ, {}, clear=True), patch("bd_to_avp.modules.config.shutil.which", return_value=None):
            self.assertEqual(resolve_tool_path("tesseract"), Path("/opt/homebrew/bin/tesseract"))

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
                patch.object(config, "MKVEXTRACT_PATH", custom_bin / "mkvextract"),
                patch.object(config, "MKVMERGE_PATH", custom_bin / "mkvmerge"),
                patch.object(config, "MP4BOX_PATH", custom_bin / "MP4Box"),
                patch.object(config, "TESSERACT_PATH", custom_bin / "tesseract"),
                patch.dict(os.environ, {"PATH": "/usr/bin"}, clear=True),
                patch.object(config, "configure_tesseract_command"),
            ):
                config.configure_tool_environment()
                path_dirs = os.environ["PATH"].split(os.pathsep)

        self.assertEqual(path_dirs[:3], [custom_bin.as_posix(), app_bin.as_posix(), "/usr/bin"])

    def test_configure_tesseract_command_sets_pytesseract_binary(self) -> None:
        fake_pytesseract = types.SimpleNamespace(pytesseract=types.SimpleNamespace(tesseract_cmd="tesseract"))

        with (
            patch.object(config, "TESSERACT_PATH", Path("/tools/tesseract")),
            patch("bd_to_avp.modules.config.importlib.import_module", return_value=fake_pytesseract),
        ):
            config.configure_tesseract_command()

        self.assertEqual(fake_pytesseract.pytesseract.tesseract_cmd, "/tools/tesseract")


class MkvToolPathTests(unittest.TestCase):
    def test_mkv_metadata_uses_configured_mkvmerge_path(self) -> None:
        metadata = b'{"tracks": []}'
        with (
            patch.object(mkv.config, "MKVMERGE_PATH", Path("/tools/mkvmerge")),
            patch("bd_to_avp.vendor.pgsrip.mkv.check_output", return_value=metadata) as check_output,
        ):
            mkv.Mkv("movie.mkv")

        check_output.assert_called_once_with([Path("/tools/mkvmerge"), "-i", "-F", "json", "movie.mkv"])

    def test_pgs_extraction_uses_configured_mkvextract_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            sup_path = temp_path / "3.en.sup"

            def write_sup(_command: list[object]) -> bytes:
                sup_path.write_bytes(b"pgs")
                return b""

            media_path = mkv.MediaPath("movie.mkv").translate(language=mkv.Language("eng"))

            with (
                patch.object(mkv.config, "MKVEXTRACT_PATH", Path("/tools/mkvextract")),
                patch("bd_to_avp.vendor.pgsrip.mkv.check_output", side_effect=write_sup) as check_output,
            ):
                data = mkv.MkvPgs.read_data(media_path, 3, temp_dir)

        self.assertEqual(data, b"pgs")
        check_output.assert_called_once_with([Path("/tools/mkvextract"), str(media_path), "tracks", f"3:{sup_path}"])


if __name__ == "__main__":
    unittest.main()
