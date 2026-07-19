import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bd_to_avp.modules import disc
from bd_to_avp.modules.config import Stage
from bd_to_avp.process_runner import ProcessOutputSnapshot, ProcessResult


def process_result(output: str = "", stderr: str = "") -> ProcessResult:
    payload = output.encode("utf-8")
    stderr_payload = stderr.encode("utf-8")
    return ProcessResult(
        tool_run_id="run-id",
        returncode=0,
        elapsed_ms=1,
        stdout=ProcessOutputSnapshot(payload, b"", len(payload), len(payload), 0, 0),
        stderr=ProcessOutputSnapshot(
            stderr_payload,
            b"",
            len(stderr_payload),
            len(stderr_payload),
            0,
            0,
        ),
    )


class DiscStageArtifactTests(unittest.TestCase):
    def test_custom_profile_preserves_source_track_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(disc.config, "remove_extra_languages", True):
            profile_path = Path(temp_dir) / "custom_profile.mmcp.xml"

            disc.create_custom_makemkv_profile(profile_path)

            profile = profile_path.read_text(encoding="utf-8")

        self.assertIn("+sel:all", profile)
        self.assertIn("+sel:mvcvideo", profile)
        self.assertNotIn("{language_code}", profile)
        self.assertNotIn("-5:", profile)
        self.assertNotIn("-10:", profile)

    def test_makemkv_progress_uses_total_and_clamps_to_maximum(self) -> None:
        self.assertEqual(disc.parse_makemkv_progress("PRGV:5,25,100"), (25, 100))
        self.assertEqual(disc.parse_makemkv_progress("PRGV:5,125,100"), (100, 100))
        self.assertIsNone(disc.parse_makemkv_progress("PRGV:5,25,0"))
        self.assertIsNone(disc.parse_makemkv_progress("MSG:1005,0,1,Finished"))

        structured = disc.parse_makemkv_observability_progress("PRGV:5,25,100")
        self.assertEqual(structured.fraction, 0.25)
        self.assertEqual(structured.completed_units, 25)
        self.assertEqual(structured.total_units, 100)

    def test_rip_requests_progress_and_reports_robot_updates(self) -> None:
        progress_updates: list[tuple[float, float]] = []

        def run_process_capture(
            command: list[object],
            _name: str,
            *,
            line_handler: object,
            **kwargs: object,
        ) -> ProcessResult:
            assert callable(line_handler)
            line_handler("PRGV:5,25,100")
            line_handler("MSG:1005,0,1,Finished")
            line_handler("PRGV:5,100,100")
            self.assertIn("--robot", command)
            self.assertIn("--progress=-same", command)
            self.assertEqual(kwargs["tool_id"], "makemkvcon")
            self.assertFalse(kwargs["merge_stderr"])
            self.assertTrue(callable(kwargs["progress_parser"]))
            self.assertEqual(len(kwargs["artifacts"]), 1)
            self.assertEqual(kwargs["capture_overflow"], disc.CaptureOverflowPolicy.FAIL)
            return process_result()

        with tempfile.TemporaryDirectory() as temp_dir:
            output_folder = Path(temp_dir)
            with (
                patch.object(disc.config, "source_path", Path("/Movies/Feature.iso")),
                patch.object(disc.config, "source_str", None),
                patch.object(disc.config, "IMAGE_EXTENSIONS", [".iso"]),
                patch.object(disc.config, "MAKEMKVCON_PATH", Path("/Applications/MakeMKV/makemkvcon")),
                patch.object(disc, "create_custom_makemkv_profile"),
                patch.object(disc, "run_process_capture", side_effect=run_process_capture),
            ):
                disc.rip_disc_to_mkv(
                    output_folder,
                    disc.DiscInfo(name="Feature", main_title_number=0),
                    lambda completed, total: progress_updates.append((completed, total)),
                )

        self.assertEqual(progress_updates, [(25, 100), (100, 100)])

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
            patch.object(disc, "run_process_capture", return_value=process_result(makemkv_output)) as run_command,
        ):
            result = disc.get_disc_and_mvc_video_info()

        self.assertEqual(result.name, "Feature 3D")
        self.assertEqual(result.main_title_number, 0)
        self.assertEqual(result.duration_seconds, 2700)
        self.assertEqual(result.titles[0].id, "makemkv:0")
        self.assertTrue(result.titles[0].main_feature)
        self.assertIn("--minlength=0", run_command.call_args.args[0])
        self.assertIn("iso:/Movies/Feature.iso", run_command.call_args.args[0])
        self.assertFalse(run_command.call_args.kwargs["merge_stderr"])

    def test_disc_info_exposes_all_mvc_titles_and_selects_requested_video(self) -> None:
        makemkv_output = "\n".join(
            [
                'CINFO:2,0,"Feature 3D"',
                'TINFO:4,9,0,"1:40:00"',
                'TINFO:2,9,0,"0:12:00"',
                'TINFO:7,9,0,"0:05:00"',
                'SINFO:2,1,6,0,"Mpeg4-MVC-3D"',
                'SINFO:4,1,6,0,"Mpeg4-MVC-3D"',
                'SINFO:7,1,6,0,"Mpeg4-AVC"',
                'SINFO:2,1,19,0,"1920x1080"',
                'SINFO:2,1,21,0,"24000/1001"',
            ]
        )
        with (
            patch.object(disc.config, "source_path", Path("/Movies/Feature.iso")),
            patch.object(disc.config, "source_str", None),
            patch.object(disc.config, "IMAGE_EXTENSIONS", [".iso"]),
            patch.object(disc.config, "MAKEMKVCON_PATH", Path("/Applications/MakeMKV/makemkvcon")),
            patch.object(disc, "run_process_capture", return_value=process_result(makemkv_output)),
        ):
            result = disc.get_disc_and_mvc_video_info("makemkv:2")

        self.assertEqual([title.id for title in result.titles], ["makemkv:4", "makemkv:2"])
        self.assertEqual(result.main_title_number, 2)
        self.assertEqual(result.name, "Feature 3D - 3D Video 1")
        self.assertEqual(result.duration_seconds, 720)
        self.assertEqual(result.resolution, "1920x1080")

    def test_disc_info_rejects_stale_title_selection(self) -> None:
        titles = disc.build_disc_title_catalog(
            "Feature 3D",
            [disc.TitleInfo(index=0, duration=120, has_mvc=True)],
        )

        with self.assertRaises(disc.DiscTitleSelectionError):
            disc.select_disc_title(titles, "makemkv:9")

    def test_disc_info_maps_missing_mvc_catalog_to_stale_title_selection(self) -> None:
        makemkv_output = "\n".join(
            [
                'CINFO:2,0,"Replacement Disc"',
                'TINFO:0,9,0,"0:10:00"',
                'SINFO:0,1,6,0,"Mpeg4-AVC"',
            ]
        )
        with (
            patch.object(disc.config, "source_path", Path("/Movies/Replacement.iso")),
            patch.object(disc.config, "source_str", None),
            patch.object(disc.config, "IMAGE_EXTENSIONS", [".iso"]),
            patch.object(disc.config, "MAKEMKVCON_PATH", Path("/Applications/MakeMKV/makemkvcon")),
            patch.object(disc, "run_process_capture", return_value=process_result(makemkv_output)),
            self.assertRaises(disc.DiscTitleSelectionError),
        ):
            disc.get_disc_and_mvc_video_info("makemkv:0")

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
                patch.object(disc, "run_process_capture", return_value=process_result(makemkv_output)) as run_command,
            ):
                result = disc.get_disc_and_mvc_video_info()

            self.assertEqual(result.name, "Feature 3D")
            self.assertEqual(
                run_command.call_args.args[0],
                [
                    Path("/Applications/MakeMKV/makemkvcon"),
                    "--robot",
                    "--minlength=0",
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
            patch.object(disc, "run_process_capture", return_value=process_result(makemkv_output)) as run_command,
        ):
            result = disc.get_disc_and_mvc_video_info()

        self.assertEqual(result.name, "Physical 3D")
        command = run_command.call_args.args[0]
        self.assertNotIn("--noscan", command)
        self.assertIn("info", command)
        self.assertIn("dev:/dev/disk9", command)

    def test_disc_rip_disables_makemkv_minimum_title_length(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_folder = Path(temp_dir)
            with (
                patch.object(disc.config, "source_path", Path("/Movies/Feature.iso")),
                patch.object(disc.config, "source_str", None),
                patch.object(disc.config, "IMAGE_EXTENSIONS", [".iso"]),
                patch.object(disc.config, "MAKEMKVCON_PATH", Path("/Applications/MakeMKV/makemkvcon")),
                patch.object(disc, "run_process_capture", return_value=process_result()) as run_command,
            ):
                disc.rip_disc_to_mkv(output_folder, disc.DiscInfo(main_title_number=2))

        command = run_command.call_args.args[0]
        self.assertIn("--minlength=0", command)
        self.assertIn(2, command)

    def test_disc_rip_detects_makemkv_error_from_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_folder = Path(temp_dir)
            with (
                patch.object(disc.config, "source_path", Path("/Movies/Feature.iso")),
                patch.object(disc.config, "source_str", None),
                patch.object(disc.config, "IMAGE_EXTENSIONS", [".iso"]),
                patch.object(disc.config, "MAKEMKVCON_PATH", Path("/Applications/MakeMKV/makemkvcon")),
                patch.object(disc.config, "continue_on_error", False),
                patch.object(disc.config, "MKV_ERROR_CODES", ["FAILED"]),
                patch.object(disc.config, "MKV_ERROR_FILTERS", []),
                patch.object(disc, "create_custom_makemkv_profile"),
                patch.object(
                    disc,
                    "run_process_capture",
                    return_value=process_result(stderr="FAILED to save title\n"),
                ) as run_command,
                self.assertRaises(disc.MKVCreationError),
            ):
                disc.rip_disc_to_mkv(output_folder, disc.DiscInfo(main_title_number=2))

        self.assertFalse(run_command.call_args.kwargs["merge_stderr"])

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
                result = disc.create_mkv_file(output_folder, disc.DiscInfo(name="Movie"))

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
                result = disc.create_mkv_file(output_folder, disc.DiscInfo(name="Movie"))

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
                result = disc.create_mkv_file(output_folder, disc.DiscInfo(name="Movie"))

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
                disc.create_mkv_file(output_folder, disc.DiscInfo(name="Movie"))

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
                result = disc.create_mkv_file(output_folder, disc.DiscInfo(name="Movie"))

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
                result = disc.create_mkv_file(output_folder, disc.DiscInfo(name="Movie"))

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
                result = disc.create_mkv_file(output_folder, disc.DiscInfo(name="Movie"))

            self.assertEqual(result, existing_mkv)
            rip_disc_to_mkv.assert_not_called()


if __name__ == "__main__":
    unittest.main()
