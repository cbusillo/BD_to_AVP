import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bd_to_avp import preflight
from bd_to_avp.modules import process
from bd_to_avp.modules.config import is_direct_pipeline_source_reused, Stage
from bd_to_avp.modules.file import move_file_to_output_root_folder, prepare_output_folder_for_source


class ProcessPreflightTests(unittest.TestCase):
    def test_batch_processing_aborts_on_dependency_preflight_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source_folder = Path(temp_dir)
            (source_folder / "movie.m2ts").touch()

            with (
                patch.object(process.config, "source_folder_path", source_folder),
                patch.object(process, "process_each", side_effect=preflight.DependencyPreflightError("missing tool")),
                self.assertRaisesRegex(preflight.DependencyPreflightError, "missing tool"),
            ):
                process.process(Stage.CREATE_MKV)

    def test_direct_pipeline_reused_file_source_is_not_cleanup_owned(self) -> None:
        with (
            patch.object(process.config, "direct_pipeline", True),
            patch.object(process.config, "source_path", Path("movie.mkv")),
            patch.object(process.config, "MTS_EXTENSIONS", [".mts", ".m2ts"]),
        ):
            self.assertTrue(is_direct_pipeline_source_reused())

    def test_default_pipeline_file_source_remains_cleanup_owned(self) -> None:
        with (
            patch.object(process.config, "direct_pipeline", False),
            patch.object(process.config, "source_path", Path("movie.mkv")),
            patch.object(process.config, "MTS_EXTENSIONS", [".mts", ".m2ts"]),
        ):
            self.assertFalse(is_direct_pipeline_source_reused())

    def test_direct_pipeline_iso_source_remains_cleanup_owned(self) -> None:
        with (
            patch.object(process.config, "direct_pipeline", True),
            patch.object(process.config, "source_path", Path("movie.iso")),
            patch.object(process.config, "MTS_EXTENSIONS", [".mts", ".m2ts"]),
        ):
            self.assertFalse(is_direct_pipeline_source_reused())

    def test_create_mkv_start_refuses_to_clear_folder_containing_direct_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_root = Path(temp_dir)
            output_folder = output_root / "Movie"
            output_folder.mkdir()
            source_path = output_folder / "Movie.mkv"
            source_path.write_bytes(b"source")

            with (
                patch.object(process.config, "direct_pipeline", True),
                patch.object(process.config, "source_path", source_path),
                patch.object(process.config, "output_root_path", output_root),
                patch.object(process.config, "start_stage", Stage.CREATE_MKV),
                patch.object(process.config, "MTS_EXTENSIONS", [".mts", ".m2ts"]),
                self.assertRaisesRegex(ValueError, "direct source media"),
            ):
                prepare_output_folder_for_source("Movie")

            self.assertTrue(source_path.exists())

    def test_output_move_preserves_folder_containing_direct_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_root = Path(temp_dir)
            output_folder = output_root / "Movie"
            output_folder.mkdir()
            source_path = output_folder / "Movie.mkv"
            muxed_path = output_folder / "Movie_AVP.mov"
            source_path.write_bytes(b"source")
            muxed_path.write_bytes(b"final")

            with (
                patch.object(process.config, "direct_pipeline", True),
                patch.object(process.config, "source_path", source_path),
                patch.object(process.config, "output_root_path", output_root),
                patch.object(process.config, "keep_files", False),
                patch.object(process.config, "MTS_EXTENSIONS", [".mts", ".m2ts"]),
            ):
                move_file_to_output_root_folder(muxed_path)

            self.assertTrue(source_path.exists())
            self.assertTrue(output_folder.exists())
            self.assertEqual((output_root / "Movie_AVP.mov").read_bytes(), b"final")

    def test_create_mkv_start_preserves_symlink_source_inside_output_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            output_root = temp_path / "output"
            output_folder = output_root / "Movie"
            external_folder = temp_path / "source"
            output_folder.mkdir(parents=True)
            external_folder.mkdir()
            external_source = external_folder / "Movie.mkv"
            source_path = output_folder / "Movie.mkv"
            external_source.write_bytes(b"source")
            source_path.symlink_to(external_source)

            with (
                patch.object(process.config, "direct_pipeline", True),
                patch.object(process.config, "source_path", source_path),
                patch.object(process.config, "output_root_path", output_root),
                patch.object(process.config, "start_stage", Stage.CREATE_MKV),
                patch.object(process.config, "MTS_EXTENSIONS", [".mts", ".m2ts"]),
                self.assertRaisesRegex(ValueError, "direct source media"),
            ):
                prepare_output_folder_for_source("Movie")

            self.assertTrue(source_path.is_symlink())
            self.assertEqual(source_path.resolve(strict=True), external_source.resolve(strict=True))


if __name__ == "__main__":
    unittest.main()
