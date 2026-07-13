import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bd_to_avp.modules import disc
from bd_to_avp.modules.config import Stage


class DiscStageArtifactTests(unittest.TestCase):
    def test_disc_info_uses_iso_source_prefix_for_image(self) -> None:
        makemkv_output = "\n".join(
            [
                'CINFO:2,0,"Feature 3D"',
                'TINFO:0,9,0,"0:45:00"',
                'SINFO:0,1,6,0,"Mpeg4-MVC-3D"',
                'SINFO:0,1,19,0,"1920x1080"',
                'SINFO:0,1,21,0,"24000/1001"',
            ]
        )
        with (
            patch.object(disc.config, "source_path", Path("/Movies/Feature.iso")),
            patch.object(disc.config, "source_str", "disc:0"),
            patch.object(disc.config, "IMAGE_EXTENSIONS", [".iso"]),
            patch.object(disc.config, "MAKEMKVCON_PATH", Path("/Applications/MakeMKV/makemkvcon")),
            patch.object(disc, "run_command", return_value=makemkv_output) as run_command,
        ):
            result = disc.get_disc_and_mvc_video_info()

        self.assertEqual(result.name, "Feature 3D")
        self.assertEqual(result.main_title_number, 0)
        self.assertIn("iso:/Movies/Feature.iso", run_command.call_args.args[0])

    def test_selected_bluray_folder_overrides_stale_disc_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = Path(temp_dir) / "Feature"
            source_path.mkdir()
            with (
                patch.object(disc.config, "source_path", source_path),
                patch.object(disc.config, "source_str", "disc:0"),
            ):
                source = disc.get_makemkv_source()

            self.assertEqual(source, f"file:{source_path}")

    def test_disc_info_uses_explicit_file_source_for_bluray_folder(self) -> None:
        makemkv_output = "\n".join(
            [
                'CINFO:2,0,"Feature 3D"',
                'TINFO:0,9,0,"0:45:00"',
                'SINFO:0,1,6,0,"Mpeg4-MVC-3D"',
            ]
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = Path(temp_dir) / "Feature"
            source_path.mkdir()
            with (
                patch.object(disc.config, "source_path", source_path),
                patch.object(disc.config, "source_str", f"file:{source_path}"),
                patch.object(disc.config, "MAKEMKVCON_PATH", Path("/Applications/MakeMKV/makemkvcon")),
                patch.object(disc, "run_command", return_value=makemkv_output) as run_command,
            ):
                result = disc.get_disc_and_mvc_video_info()

            self.assertEqual(result.name, "Feature 3D")
            self.assertEqual(
                run_command.call_args.args[0],
                [
                    Path("/Applications/MakeMKV/makemkvcon"),
                    "--robot",
                    "--noscan",
                    "info",
                    f"file:{source_path}",
                ],
            )

    def test_device_sources_do_not_disable_drive_scanning(self) -> None:
        self.assertFalse(disc.makemkv_source_supports_noscan("disc:0"))
        self.assertFalse(disc.makemkv_source_supports_noscan("dev:/dev/rdisk4"))
        self.assertTrue(disc.makemkv_source_supports_noscan("file:/Movies/Feature"))

    def test_disc_info_passes_device_source_to_makemkv_without_noscan(self) -> None:
        makemkv_output = "\n".join(
            [
                'CINFO:2,0,"Physical 3D"',
                'TINFO:0,9,0,"0:45:00"',
                'SINFO:0,1,6,0,"Mpeg4-MVC-3D"',
            ]
        )
        with (
            patch.object(disc.config, "source_path", None),
            patch.object(disc.config, "source_str", "dev:/dev/disk9"),
            patch.object(disc.config, "MAKEMKVCON_PATH", Path("/Applications/MakeMKV/makemkvcon")),
            patch.object(disc, "run_command", return_value=makemkv_output) as run_command,
        ):
            result = disc.get_disc_and_mvc_video_info()

        self.assertEqual(result.name, "Physical 3D")
        self.assertEqual(
            run_command.call_args.args[0],
            [
                Path("/Applications/MakeMKV/makemkvcon"),
                "--robot",
                None,
                "info",
                "dev:/dev/disk9",
            ],
        )

    def test_keep_files_copies_mkv_source_to_output_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            source_path = temp_path / "source.mkv"
            output_folder = temp_path / "output"
            output_folder.mkdir()
            source_path.write_bytes(b"source")

            with (
                patch.object(disc.config, "source_path", source_path),
                patch.object(disc.config, "keep_files", True),
                patch.object(disc.config, "MTS_EXTENSIONS", [".mts", ".m2ts"]),
            ):
                result = disc.create_mkv_file(output_folder, disc.DiscInfo(name="Movie"), "eng")

            copied_path = output_folder / source_path.name
            self.assertEqual(result, copied_path)
            self.assertEqual(copied_path.read_bytes(), b"source")
            self.assertTrue(source_path.exists())

    def test_direct_mkv_source_reuses_source_path_without_copying(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            source_path = temp_path / "source.mkv"
            output_folder = temp_path / "output"
            output_folder.mkdir()
            source_path.write_bytes(b"source")

            with (
                patch.object(disc.config, "source_path", source_path),
                patch.object(disc.config, "keep_files", False),
                patch.object(disc.config, "MTS_EXTENSIONS", [".mts", ".m2ts"]),
            ):
                result = disc.create_mkv_file(output_folder, disc.DiscInfo(name="Movie"), "eng")

            self.assertEqual(result, source_path)
            self.assertFalse((output_folder / source_path.name).exists())
            self.assertTrue(source_path.exists())

    def test_direct_m2ts_source_reuses_source_path_without_copying(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            source_path = temp_path / "source.m2ts"
            output_folder = temp_path / "output"
            output_folder.mkdir()
            source_path.write_bytes(b"source")

            with (
                patch.object(disc.config, "source_path", source_path),
                patch.object(disc.config, "keep_files", False),
                patch.object(disc.config, "MTS_EXTENSIONS", [".mts", ".m2ts"]),
            ):
                result = disc.create_mkv_file(output_folder, disc.DiscInfo(name="Movie"), "eng")

            self.assertEqual(result, source_path)
            self.assertFalse((output_folder / source_path.name).exists())
            self.assertTrue(source_path.exists())

    def test_direct_source_must_exist_before_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            source_path = temp_path / "missing.mkv"
            output_folder = temp_path / "output"
            output_folder.mkdir()

            with (
                patch.object(disc.config, "source_path", source_path),
                patch.object(disc.config, "keep_files", False),
                patch.object(disc.config, "MTS_EXTENSIONS", [".mts", ".m2ts"]),
                self.assertRaisesRegex(FileNotFoundError, "Source file not found"),
            ):
                disc.create_mkv_file(output_folder, disc.DiscInfo(name="Movie"), "eng")

    def test_keep_files_resume_uses_source_already_in_output_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_folder = Path(temp_dir)
            source_path = output_folder / "movie.mkv"
            source_path.write_bytes(b"mkv")

            with (
                patch.object(disc.config, "source_path", source_path),
                patch.object(disc.config, "keep_files", True),
                patch.object(disc.config, "start_stage", Stage.EXTRACT_MVC_AND_AUDIO),
                patch.object(disc.config, "MTS_EXTENSIONS", [".mts", ".m2ts"]),
                patch.object(disc.shutil, "copy2") as copy_source,
            ):
                result = disc.create_mkv_file(output_folder, disc.DiscInfo(name="Movie"), "eng")

            self.assertEqual(result, source_path)
            copy_source.assert_not_called()

    def test_keep_files_resume_uses_existing_copy_without_recopying_external_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            source_path = temp_path / "source" / "movie.mkv"
            output_folder = temp_path / "output"
            source_path.parent.mkdir()
            output_folder.mkdir()
            source_path.write_bytes(b"source")
            existing_copy = output_folder / source_path.name
            existing_copy.write_bytes(b"copy")

            with (
                patch.object(disc.config, "source_path", source_path),
                patch.object(disc.config, "keep_files", True),
                patch.object(disc.config, "start_stage", Stage.EXTRACT_MVC_AND_AUDIO),
                patch.object(disc.config, "MTS_EXTENSIONS", [".mts", ".m2ts"]),
                patch.object(disc.shutil, "copy2") as copy_source,
            ):
                result = disc.create_mkv_file(output_folder, disc.DiscInfo(name="Movie"), "eng")

            self.assertEqual(result, existing_copy)
            copy_source.assert_not_called()

    def test_resume_from_existing_mkv_still_uses_output_folder_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_folder = Path(temp_dir)
            existing_mkv = output_folder / "movie.mkv"
            existing_mkv.write_bytes(b"mkv")

            with (
                patch.object(disc.config, "source_path", Path("movie.iso")),
                patch.object(disc.config, "IMAGE_EXTENSIONS", [".iso"]),
                patch.object(disc.config, "start_stage", Stage.EXTRACT_MVC_AND_AUDIO),
                patch.object(disc, "rip_disc_to_mkv") as rip_disc_to_mkv,
            ):
                result = disc.create_mkv_file(output_folder, disc.DiscInfo(name="Movie"), "eng")

            self.assertEqual(result, existing_mkv)
            rip_disc_to_mkv.assert_not_called()


if __name__ == "__main__":
    unittest.main()
