import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bd_to_avp import install
from bd_to_avp.modules.config import Stage


class DependencyPreflightTests(unittest.TestCase):
    def test_missing_required_dependency_binaries_raise_clear_error(self) -> None:
        with (
            patch.object(install.config, "FFMPEG_PATH", Path(__file__)),
            patch.object(install.config, "FFPROBE_PATH", Path(__file__)),
            patch.object(install.config, "MAKEMKVCON_PATH", Path("/missing/makemkvcon")),
            patch.object(install.config, "MKVEXTRACT_PATH", Path(__file__)),
            patch.object(install.config, "MKVMERGE_PATH", Path(__file__)),
            patch.object(install.config, "MP4BOX_PATH", Path("/missing/MP4Box")),
            patch.object(install.config, "TESSERACT_PATH", Path(__file__)),
            patch.object(install.config, "EDGE264_TEST_PATH", Path(__file__)),
            self.assertRaisesRegex(install.DependencyPreflightError, "MakeMKV"),
        ):
            install.verify_runtime_ready()

    def test_missing_native_mvc_helper_raises_clear_error(self) -> None:
        with (
            patch.object(install.config, "FFMPEG_PATH", Path(__file__)),
            patch.object(install.config, "FFPROBE_PATH", Path(__file__)),
            patch.object(install.config, "MAKEMKVCON_PATH", Path(__file__)),
            patch.object(install.config, "MKVEXTRACT_PATH", Path(__file__)),
            patch.object(install.config, "MKVMERGE_PATH", Path(__file__)),
            patch.object(install.config, "MP4BOX_PATH", Path(__file__)),
            patch.object(install.config, "TESSERACT_PATH", Path(__file__)),
            patch.object(install.config, "EDGE264_TEST_PATH", Path("/missing/edge264_test")),
            self.assertRaisesRegex(install.DependencyPreflightError, "edge264_test"),
        ):
            install.verify_runtime_ready()

    def test_missing_native_mvc_helper_is_listed_once(self) -> None:
        with patch.object(install.config, "EDGE264_TEST_PATH", Path("/missing/edge264_test")):
            missing_binaries = install.get_missing_dependency_binaries_for_current_job()

        self.assertEqual(missing_binaries.count(Path("/missing/edge264_test")), 1)

    def test_gui_missing_makemkv_message_is_plain(self) -> None:
        with (
            patch.object(install.config.app, "is_gui", True),
            patch.object(install.config, "MAKEMKVCON_PATH", Path("/missing/makemkvcon")),
        ):
            message = install.build_missing_dependency_message([Path("/missing/makemkvcon")])

        self.assertIn("MakeMKV", message)
        self.assertIn("Install MakeMKV for macOS", message)
        self.assertNotIn("/missing", message)

    def test_gui_missing_bundled_tool_message_recommends_reinstall(self) -> None:
        with (
            patch.object(install.config.app, "is_gui", True),
            patch.object(install.config, "MP4BOX_PATH", Path("/missing/MP4Box")),
        ):
            message = install.build_missing_dependency_message([Path("/missing/MP4Box")])

        self.assertIn("MP4Box", message)
        self.assertIn("Reinstall the app", message)
        self.assertNotIn("/missing", message)

    def test_runtime_ready_does_not_import_subprocess(self) -> None:
        with (
            patch.object(install.config, "FFMPEG_PATH", Path(__file__)),
            patch.object(install.config, "FFPROBE_PATH", Path(__file__)),
            patch.object(install.config, "MAKEMKVCON_PATH", Path(__file__)),
            patch.object(install.config, "MKVEXTRACT_PATH", Path(__file__)),
            patch.object(install.config, "MKVMERGE_PATH", Path(__file__)),
            patch.object(install.config, "MP4BOX_PATH", Path(__file__)),
            patch.object(install.config, "TESSERACT_PATH", Path(__file__)),
            patch.object(install.config, "EDGE264_TEST_PATH", Path(__file__)),
        ):
            install.verify_runtime_ready()

        self.assertFalse(hasattr(install, "subprocess"))

    def test_mts_sources_do_not_require_makemkv(self) -> None:
        with (
            patch.object(install.config, "source_path", Path("movie.m2ts")),
            patch.object(install.config, "start_stage", Stage.CREATE_MKV),
        ):
            required_paths = install.get_required_dependency_binaries_for_current_job()

        self.assertNotIn(install.config.MAKEMKVCON_PATH, required_paths)

    def test_disc_sources_require_makemkv(self) -> None:
        with (
            patch.object(install.config, "source_path", None),
            patch.object(install.config, "source_str", "disc:0"),
            patch.object(install.config, "start_stage", Stage.CREATE_MKV),
        ):
            required_paths = install.get_required_dependency_binaries_for_current_job()

        self.assertIn(install.config.MAKEMKVCON_PATH, required_paths)

    def test_skip_subtitles_does_not_require_subtitle_tools(self) -> None:
        with (
            patch.object(install.config, "skip_subtitles", True),
            patch.object(install.config, "start_stage", Stage.EXTRACT_SUBTITLES),
        ):
            required_paths = install.get_required_dependency_binaries_for_current_job()

        self.assertNotIn(install.config.MKVEXTRACT_PATH, required_paths)
        self.assertNotIn(install.config.MKVMERGE_PATH, required_paths)
        self.assertNotIn(install.config.TESSERACT_PATH, required_paths)

    def test_fx_upscale_tool_is_required_only_when_enabled(self) -> None:
        with (
            patch.object(install.config, "fx_upscale", False),
            patch.object(install.config, "start_stage", Stage.UPSCALE_VIDEO),
        ):
            required_paths = install.get_required_dependency_binaries_for_current_job()

        self.assertNotIn(install.config.FX_UPSCALE_PATH, required_paths)

        with (
            patch.object(install.config, "fx_upscale", True),
            patch.object(install.config, "start_stage", Stage.UPSCALE_VIDEO),
        ):
            required_paths = install.get_required_dependency_binaries_for_current_job()

        self.assertIn(install.config.FX_UPSCALE_PATH, required_paths)

    def test_native_mvc_helper_repairs_missing_execute_bit(self) -> None:
        with tempfile.NamedTemporaryFile() as helper_file:
            helper_path = Path(helper_file.name)
            helper_path.chmod(0o644)

            with patch.object(install.config, "EDGE264_TEST_PATH", helper_path):
                self.assertTrue(install.ensure_native_mvc_splitter_executable())

            self.assertTrue(helper_path.stat().st_mode & 0o111)


class InstallVersionTests(unittest.TestCase):
    def test_check_install_version_matches_saved_version(self) -> None:
        with (
            patch.object(install.config.app, "load_version_from_file", return_value="1.2.3"),
            patch.object(
                type(install.config.app), "code_version", new_callable=lambda: property(lambda _self: "1.2.3")
            ),
        ):
            self.assertTrue(install.check_install_version())


if __name__ == "__main__":
    unittest.main()
