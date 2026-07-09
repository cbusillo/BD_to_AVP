import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bd_to_avp import preflight
from bd_to_avp.modules.config import Stage
from bd_to_avp.vendor.pgsrip.ocr import OcrError


class DependencyPreflightTests(unittest.TestCase):
    def test_missing_required_dependency_binaries_raise_clear_error(self) -> None:
        with (
            patch.object(preflight.config, "FFMPEG_PATH", Path(__file__)),
            patch.object(preflight.config, "FFPROBE_PATH", Path(__file__)),
            patch.object(preflight.config, "MAKEMKVCON_PATH", Path("/missing/makemkvcon")),
            patch.object(preflight.config, "MKVEXTRACT_PATH", Path(__file__)),
            patch.object(preflight.config, "MKVMERGE_PATH", Path(__file__)),
            patch.object(preflight.config, "MP4BOX_PATH", Path("/missing/MP4Box")),
            patch.object(preflight.config, "EDGE264_TEST_PATH", Path(__file__)),
            self.assertRaisesRegex(preflight.DependencyPreflightError, "MakeMKV"),
        ):
            preflight.verify_runtime_ready()

    def test_missing_native_mvc_helper_raises_clear_error(self) -> None:
        with (
            patch.object(preflight.config.app, "is_gui", False),
            patch.object(preflight.config, "FFMPEG_PATH", Path(__file__)),
            patch.object(preflight.config, "FFPROBE_PATH", Path(__file__)),
            patch.object(preflight.config, "MAKEMKVCON_PATH", Path(__file__)),
            patch.object(preflight.config, "MKVEXTRACT_PATH", Path(__file__)),
            patch.object(preflight.config, "MKVMERGE_PATH", Path(__file__)),
            patch.object(preflight.config, "MP4BOX_PATH", Path(__file__)),
            patch.object(preflight.config, "EDGE264_TEST_PATH", Path("/missing/edge264_test")),
            self.assertRaisesRegex(preflight.DependencyPreflightError, "edge264_test"),
        ):
            preflight.verify_runtime_ready()

    def test_missing_native_mvc_helper_is_listed_once(self) -> None:
        with patch.object(preflight.config, "EDGE264_TEST_PATH", Path("/missing/edge264_test")):
            missing_binaries = preflight.get_missing_dependency_binaries_for_current_job()

        self.assertEqual(missing_binaries.count(Path("/missing/edge264_test")), 1)

    def test_gui_missing_makemkv_message_is_plain(self) -> None:
        with (
            patch.object(preflight.config.app, "is_gui", True),
            patch.object(preflight.config, "MAKEMKVCON_PATH", Path("/missing/makemkvcon")),
        ):
            message = preflight.build_missing_dependency_message([Path("/missing/makemkvcon")])

        self.assertIn("MakeMKV", message)
        self.assertIn("Install MakeMKV for macOS", message)
        self.assertNotIn("/missing", message)

    def test_gui_missing_bundled_tool_message_recommends_reinstall(self) -> None:
        with (
            patch.object(preflight.config.app, "is_gui", True),
            patch.object(preflight.config, "MP4BOX_PATH", Path("/missing/MP4Box")),
        ):
            message = preflight.build_missing_dependency_message([Path("/missing/MP4Box")])

        self.assertIn("MP4Box", message)
        self.assertIn("Reinstall the app", message)
        self.assertNotIn("/missing", message)

    def test_runtime_ready_does_not_import_subprocess(self) -> None:
        with (
            patch.object(preflight.config.app, "is_gui", False),
            patch.object(preflight.config, "FFMPEG_PATH", Path(__file__)),
            patch.object(preflight.config, "FFPROBE_PATH", Path(__file__)),
            patch.object(preflight.config, "MAKEMKVCON_PATH", Path(__file__)),
            patch.object(preflight.config, "MKVEXTRACT_PATH", Path(__file__)),
            patch.object(preflight.config, "MKVMERGE_PATH", Path(__file__)),
            patch.object(preflight.config, "MP4BOX_PATH", Path(__file__)),
            patch.object(preflight.config, "EDGE264_TEST_PATH", Path(__file__)),
        ):
            preflight.verify_runtime_ready()

        self.assertFalse(hasattr(preflight, "subprocess"))

    def test_mts_sources_do_not_require_makemkv(self) -> None:
        with (
            patch.object(preflight.config, "source_path", Path("movie.m2ts")),
            patch.object(preflight.config, "start_stage", Stage.CREATE_MKV),
        ):
            required_paths = preflight.get_required_dependency_binaries_for_current_job()

        self.assertNotIn(preflight.config.MAKEMKVCON_PATH, required_paths)

    def test_disc_sources_require_makemkv(self) -> None:
        with (
            patch.object(preflight.config, "source_path", None),
            patch.object(preflight.config, "source_str", "disc:0"),
            patch.object(preflight.config, "start_stage", Stage.CREATE_MKV),
        ):
            required_paths = preflight.get_required_dependency_binaries_for_current_job()

        self.assertIn(preflight.config.MAKEMKVCON_PATH, required_paths)

    def test_skip_subtitles_does_not_require_subtitle_tools(self) -> None:
        with (
            patch.object(preflight.config, "skip_subtitles", True),
            patch.object(preflight.config, "start_stage", Stage.EXTRACT_SUBTITLES),
        ):
            required_paths = preflight.get_required_dependency_binaries_for_current_job()

        self.assertNotIn(preflight.config.MKVEXTRACT_PATH, required_paths)
        self.assertNotIn(preflight.config.MKVMERGE_PATH, required_paths)

    def test_subtitle_extraction_no_longer_requires_external_subtitle_tools(self) -> None:
        with (
            patch.object(preflight.config, "skip_subtitles", False),
            patch.object(preflight.config, "start_stage", Stage.EXTRACT_SUBTITLES),
        ):
            required_paths = preflight.get_required_dependency_binaries_for_current_job()

        self.assertNotIn(preflight.config.MKVEXTRACT_PATH, required_paths)
        self.assertNotIn(preflight.config.MKVMERGE_PATH, required_paths)

    def test_subtitle_extraction_requires_apple_vision_frameworks(self) -> None:
        with (
            patch.object(preflight.config, "skip_subtitles", False),
            patch.object(preflight.config, "start_stage", Stage.EXTRACT_SUBTITLES),
            patch("bd_to_avp.preflight.AppleVisionOcr._load_frameworks", side_effect=OcrError("missing")),
        ):
            with self.assertRaisesRegex(preflight.DependencyPreflightError, "Apple Vision OCR"):
                preflight.verify_apple_vision_ocr_ready()

    def test_skipped_subtitle_extraction_does_not_require_apple_vision_frameworks(self) -> None:
        with (
            patch.object(preflight.config, "skip_subtitles", True),
            patch.object(preflight.config, "start_stage", Stage.EXTRACT_SUBTITLES),
            patch("bd_to_avp.preflight.AppleVisionOcr._load_frameworks") as load_frameworks,
        ):
            preflight.verify_apple_vision_ocr_ready()

        load_frameworks.assert_not_called()

    def test_final_mux_always_requires_mp4box_even_before_srt_files_exist(self) -> None:
        with (
            patch.object(preflight.config, "skip_subtitles", False),
            patch.object(preflight.config, "source_path", Path("/movie/source.mkv")),
            patch.object(preflight.config, "start_stage", Stage.CREATE_FINAL_FILE),
        ):
            required_paths = preflight.get_required_dependency_binaries_for_current_job()

        self.assertIn(preflight.config.MP4BOX_PATH, required_paths)

    def test_fx_upscale_tool_is_required_only_when_enabled(self) -> None:
        with (
            patch.object(preflight.config, "fx_upscale", False),
            patch.object(preflight.config, "start_stage", Stage.UPSCALE_VIDEO),
        ):
            required_paths = preflight.get_required_dependency_binaries_for_current_job()

        self.assertNotIn(preflight.config.FX_UPSCALE_PATH, required_paths)

        with (
            patch.object(preflight.config, "fx_upscale", True),
            patch.object(preflight.config, "start_stage", Stage.UPSCALE_VIDEO),
        ):
            required_paths = preflight.get_required_dependency_binaries_for_current_job()

        self.assertIn(preflight.config.FX_UPSCALE_PATH, required_paths)

    def test_native_mvc_helper_repairs_missing_execute_bit(self) -> None:
        with tempfile.NamedTemporaryFile() as helper_file:
            helper_path = Path(helper_file.name)
            helper_path.chmod(0o644)

            with patch.object(preflight.config, "EDGE264_TEST_PATH", helper_path):
                self.assertTrue(preflight.ensure_native_mvc_splitter_executable())

            self.assertTrue(helper_path.stat().st_mode & 0o111)


if __name__ == "__main__":
    unittest.main()
