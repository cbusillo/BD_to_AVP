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
