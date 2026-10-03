import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bd_to_avp.modules import disc, process
from bd_to_avp.modules.config import Config


class CLISourceTests(unittest.TestCase):
    def parse(self, *arguments: str) -> Config:
        with patch.object(Config, "App"), patch.object(sys, "argv", ["bd-to-avp", *arguments]):
            config = Config()
            config.parse_args()
        return config

    def test_disc_reaches_makemkv_source_dispatch(self) -> None:
        config = self.parse("--source", "disc:0")
        with patch.object(disc, "config", config):
            self.assertEqual(disc.get_makemkv_source(), "disc:0")
        self.assertIsNone(config.source_path)
        self.assertIsNone(config.source_folder_path)

    def test_folder_dispatches_nested_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "nested" / "movie.mkv"
            source.parent.mkdir()
            source.touch()
            config = self.parse("--source-folder", str(root))
            observed = []
            with (
                patch.object(process, "config", config),
                patch.object(
                    process, "process_each", side_effect=lambda *args, **kwargs: observed.append(config.source_path)
                ),
            ):
                process.process(config.start_stage)
            self.assertEqual(observed, [source])
            self.assertIsNone(config.source_str)

    def test_file_and_image_sources_reach_makemkv(self) -> None:
        for name, prefix in (("movie.mkv", ""), ("disc.iso", "iso:")):
            with self.subTest(name=name):
                config = self.parse("--source", f"~/Movies/{name}")
                source = Path.home() / "Movies" / name
                with patch.object(disc, "config", config):
                    expected = f"{prefix}{source}"
                    self.assertEqual(disc.get_makemkv_source(), expected)
                self.assertEqual(config.source_path, source)
                self.assertIsNone(config.source_str)
                self.assertIsNone(config.source_folder_path)

    def test_folder_expands_home(self) -> None:
        config = self.parse("-f", "~/Movies")
        self.assertEqual(config.source_folder_path, Path.home() / "Movies")

    def test_source_is_required_and_mutually_exclusive(self) -> None:
        for arguments in ((), ("--source", "disc:0", "--source-folder", "/Movies")):
            with self.subTest(arguments=arguments), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    self.parse(*arguments)
                self.assertEqual(error.exception.code, 2)

    def test_output_and_sleep_defaults_and_overrides(self) -> None:
        defaults = self.parse("-s", "disc:0")
        self.assertEqual(defaults.output_root_path, Path.home() / "Movies")
        self.assertTrue(defaults.keep_awake)
        overrides = self.parse("-s", "disc:0", "-o", "~/converted", "--no-keep-awake")
        self.assertEqual(overrides.output_root_path, Path.home() / "converted")
        self.assertFalse(overrides.keep_awake)


class CLIBatchTests(unittest.TestCase):
    parse = CLISourceTests.parse

    def run_folder(self, root: Path, convert: object, *, keep_awake: bool = False) -> tuple[object, str]:
        from bd_to_avp import __main__ as cli

        config = self.parse("--source-folder", str(root), "--no-keep-awake")
        config.keep_awake = keep_awake
        config.app.is_gui = False
        output = io.StringIO()
        status: object = 0
        with (
            patch.object(cli, "config", config),
            patch.object(process, "config", config),
            patch.object(config, "configure_tool_environment"),
            patch.object(config, "parse_args"),
            patch.object(process.keep, "running", return_value=contextlib.nullcontext()),
            patch.object(process, "process_each", side_effect=convert),
            contextlib.redirect_stdout(output),
        ):
            try:
                cli.main()
            except SystemExit as error:
                status = error.code
        self.assertIsNone(config.source_path)
        return status, output.getvalue()

    def test_failed_sources_are_reported_and_later_sources_are_attempted(self) -> None:
        import subprocess

        from bd_to_avp.modules.disc import MKVCreationError
        from bd_to_avp.modules.sub import SRTCreationError

        failures = (
            RuntimeError("encoder failed"),
            ValueError("invalid video"),
            subprocess.CalledProcessError(1, "ffmpeg"),
            MKVCreationError("disc extraction failed"),
            SRTCreationError("subtitle extraction failed"),
        )
        for failure in failures:
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                first, second = root / "a.mkv", root / "b.mkv"
                first.touch()
                second.touch()
                attempts = []

                def convert(
                    *args: object, attempts=attempts, first=first, failure=failure, root=root, **kwargs: object
                ) -> Path:
                    attempts.append(process.config.source_path)
                    if process.config.source_path == first:
                        raise failure
                    return root / "converted.mov"

                status, output = self.run_folder(root, convert)
                self.assertNotEqual(status, 0)
                self.assertIn(str(first), output)
                self.assertIn(str(failure), output)
                self.assertEqual(attempts, [first, second])

    def test_failure_status_survives_keep_awake_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "movie.mkv").touch()
            status, output = self.run_folder(root, RuntimeError("encoder failed"), keep_awake=True)
            self.assertNotEqual(status, 0)
            self.assertIn("encoder failed", output)

    def test_all_failed_sources_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = [root / "a.mkv", root / "b.M2TS"]
            for source in sources:
                source.touch()
            status, output = self.run_folder(root, RuntimeError("conversion failed"))
            self.assertNotEqual(status, 0)
            for source in sources:
                self.assertIn(str(source), output)

    def test_invalid_or_empty_source_folder_has_actionable_failure(self) -> None:
        from unittest.mock import Mock

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            file = root / "movie.mkv"
            file.touch()
            empty = root / "unsupported"
            empty.mkdir()
            (empty / "notes.txt").touch()
            for folder in (root / "missing", file, empty):
                with self.subTest(folder=folder):
                    convert = Mock()
                    status, _ = self.run_folder(folder, convert)
                    self.assertNotEqual(status, 0)
                    self.assertIn(str(folder), str(status))
                    convert.assert_not_called()

    def test_success_and_existing_output_skips_return_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "movie.mkv"
            source.touch()
            status, _ = self.run_folder(root, lambda *args, **kwargs: root / "converted.mov")
            self.assertEqual(status, 0)
            status, output = self.run_folder(root, FileExistsError("output already exists"))
            self.assertEqual(status, 0)
            self.assertIn(str(source), output)
            self.assertIn("output already exists", output)
