import runpy
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from babelfish import Language
from click.testing import CliRunner

from bd_to_avp.vendor.pgsrip.cli import pgsrip
from bd_to_avp.vendor.pgsrip.media import Pgs
from bd_to_avp.vendor.pgsrip.sup import Sup


class PgsripCliTests(unittest.TestCase):
    def test_invalid_config_fails_before_scanning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for config_path in (root / "missing.conf", root):
                with self.subTest(config=config_path.name), patch("bd_to_avp.vendor.pgsrip.cli.api.scan_path") as scan:
                    result = CliRunner().invoke(pgsrip, ["--config", str(config_path), str(root / "movie.sup")])

                    self.assertEqual(result.exit_code, 2, result.output)
                    scan.assert_not_called()

    def test_defaults_scan_multiple_paths_and_rip_collected_subtitles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / name for name in ("first.en.sup", "second.fr.sup")]
            for path in paths:
                path.write_bytes(b"extraction boundary")
            subtitles = [Mock(spec=Pgs), Mock(spec=Pgs)]
            with (
                patch.object(Sup, "get_pgs_medias", side_effect=[[subtitles[0]], [subtitles[1]]]) as collect,
                patch("bd_to_avp.vendor.pgsrip.cli.api.rip_pgs", side_effect=[True, False]) as rip,
            ):
                result = CliRunner().invoke(pgsrip, [str(path) for path in paths])

            self.assertEqual(result.exit_code, 0, result.exception)
            self.assertEqual(collect.call_count, len(paths))
            self.assertEqual([call.args[0] for call in rip.call_args_list], subtitles)
            options = rip.call_args.args[1]
            self.assertEqual(options.languages, set())
            self.assertTrue(options.one_per_lang)
            self.assertFalse(options.overwrite)
            self.assertFalse(options.keep_temp_files)
            self.assertIsNone(options.age)
            self.assertIsNone(options.srt_age)
            self.assertIsNone(options.max_workers)
            self.assertIn("1 PGS subtitle ripped from 2 files", result.output)

    def test_existing_config_reaches_scanning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "cleanit.json"
            config_path.write_text("{}", encoding="utf-8")
            with patch("bd_to_avp.vendor.pgsrip.cli.api.scan_path", return_value=([], [], [])) as scan:
                result = CliRunner().invoke(pgsrip, ["--config", str(config_path), str(Path(directory) / "movie.sup")])

            self.assertEqual(result.exit_code, 0, result.exception)
            scan.assert_called_once()

    def test_age_all_and_repeated_options_reach_extraction_in_each_progress_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "movie.en.sup"
            source.write_bytes(b"extraction boundary")
            for verbosity in ([], ["-vv"], ["--debug"]):
                with self.subTest(verbosity=verbosity):
                    subtitle = Mock(spec=Pgs)
                    with (
                        patch.object(Sup, "get_pgs_medias", return_value=[subtitle]),
                        patch("bd_to_avp.vendor.pgsrip.cli.api.rip_pgs", return_value=True) as rip,
                        patch("bd_to_avp.vendor.pgsrip.cli.logger"),
                    ):
                        result = CliRunner().invoke(
                            pgsrip,
                            [
                                "-a",
                                "1w2d3h",
                                "-A",
                                "12h",
                                "--all",
                                "--force",
                                "--keep-temp-files",
                                "--max-workers",
                                "2",
                                "--encoding",
                                "utf-8",
                                "-l",
                                "en",
                                "-l",
                                "fr",
                                "-t",
                                "default",
                                "-t",
                                "ocr",
                                *verbosity,
                                str(source),
                            ],
                        )

                    self.assertEqual(result.exit_code, 0, result.exception)
                    rip.assert_called_once()
                    self.assertIs(rip.call_args.args[0], subtitle)
                    options = rip.call_args.args[1]
                    self.assertEqual(options.languages, {Language.fromietf("en"), Language.fromietf("fr")})
                    self.assertEqual(options.tags, {"default", "ocr"})
                    self.assertEqual(options.age, timedelta(weeks=1, days=2, hours=3))
                    self.assertEqual(options.srt_age, timedelta(hours=12))
                    self.assertFalse(options.one_per_lang)
                    self.assertTrue(options.overwrite)
                    self.assertTrue(options.keep_temp_files)
                    self.assertEqual(options.max_workers, 2)
                    self.assertEqual(options.encoding, "utf-8")
                    self.assertIn("1 PGS subtitle ripped", result.output)

    def test_invalid_parameters_are_usage_errors_before_scanning(self) -> None:
        for arguments in (
            ["--age", "yesterday"],
            ["--srt-age", "1h2d"],
            ["--language", "not-a-language"],
            ["--max-workers", "0"],
            ["--max-workers", "51"],
        ):
            with self.subTest(arguments=arguments), patch("bd_to_avp.vendor.pgsrip.cli.api.scan_path") as scan:
                result = CliRunner().invoke(pgsrip, [*arguments, "movie.sup"])

                self.assertEqual(result.exit_code, 2, result.output)
                scan.assert_not_called()

    def test_module_entrypoint_dispatches_click_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "movie.en.sup"
            source.write_bytes(b"extraction boundary")
            with (
                patch.object(sys, "argv", ["pgsrip", "--all", str(source)]),
                patch.object(Sup, "get_pgs_medias", return_value=[Mock(spec=Pgs)]),
                patch("bd_to_avp.vendor.pgsrip.cli.api.rip_pgs", return_value=True) as rip,
                self.assertRaises(SystemExit) as exit_result,
            ):
                runpy.run_module("bd_to_avp.vendor.pgsrip", run_name="__main__")

            self.assertEqual(exit_result.exception.code, 0)
            rip.assert_called_once()
            self.assertFalse(rip.call_args.args[1].one_per_lang)


if __name__ == "__main__":
    unittest.main()
