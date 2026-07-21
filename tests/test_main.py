import builtins
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from bd_to_avp import __main__
from bd_to_avp.modules.audio_mode import AudioMode
from bd_to_avp.modules.config import Config


class MainSmokeTests(unittest.TestCase):
    def test_gui_start_is_lazy(self) -> None:
        with (
            patch.object(__main__.config.app, "is_gui", True),
            patch("bd_to_avp.__main__._start_gui") as start_gui,
            patch("bd_to_avp.__main__.start_process") as start_process,
            patch("bd_to_avp.__main__.signal.signal"),
        ):
            __main__.main()

        start_gui.assert_called_once_with()
        start_process.assert_not_called()

    def test_missing_gui_extra_exits_with_install_guidance(self) -> None:
        real_import = builtins.__import__

        def import_without_pyside(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "bd_to_avp.app":
                raise ModuleNotFoundError("No module named 'PySide6'", name="PySide6")
            return real_import(name, globals, locals, fromlist, level)

        with (
            patch("builtins.__import__", side_effect=import_without_pyside),
            self.assertRaisesRegex(SystemExit, r"bd_to_avp\[gui\].*release DMG"),
        ):
            __main__._start_gui()

    def test_audio_mode_defaults_to_automatic_and_legacy_false_remains_pcm(self) -> None:
        config = Config()

        self.assertEqual(config.audio_mode, AudioMode.AUTOMATIC)
        config.transcode_audio = False
        self.assertEqual(config.audio_mode, AudioMode.PCM)

    def test_cli_without_audio_mode_uses_automatic(self) -> None:
        config = Config()

        with patch.object(sys, "argv", ["bd-to-avp", "--source", "/tmp/movie.mkv"]):
            config.parse_args()

        self.assertEqual(config.audio_mode, AudioMode.AUTOMATIC)

    def test_saved_audio_choices_survive_automatic_default(self) -> None:
        cases = [
            ("transcode_audio = False", AudioMode.PCM),
            ("audio_mode = pcm", AudioMode.PCM),
            ("audio_mode = convert_aac", AudioMode.CONVERT_AAC),
        ]

        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.ini"
            for saved_option, expected_mode in cases:
                with self.subTest(saved_option=saved_option):
                    config_path.write_text(f"[Options]\n{saved_option}\n")
                    config = Config()
                    config.app.config_file = config_path
                    config.load_config_from_file()

                    self.assertEqual(config.audio_mode, expected_mode)

    def test_apple_vision_smoke_flag_runs_without_source(self) -> None:
        with (
            patch.object(sys, "argv", ["bd-to-avp", "--smoke-apple-vision-ocr"]),
            patch("bd_to_avp.__main__.AppleVisionOcr._load_frameworks") as load_frameworks,
            patch("bd_to_avp.__main__.start_process") as start_process,
            patch("builtins.print") as print_mock,
        ):
            __main__.main()

        load_frameworks.assert_called_once()
        start_process.assert_not_called()
        print_mock.assert_called_once_with("Apple Vision OCR import smoke passed")

    def test_legacy_transcode_audio_cli_maps_to_convert_aac_mode(self) -> None:
        config = Config()

        with patch.object(
            sys,
            "argv",
            ["bd-to-avp", "--source", "/tmp/movie.mkv", "--transcode-audio"],
        ):
            config.parse_args()

        self.assertEqual(config.audio_mode, AudioMode.CONVERT_AAC)

    def test_audio_mode_cli_selects_pcm_without_legacy_boolean(self) -> None:
        config = Config()

        with patch.object(
            sys,
            "argv",
            ["bd-to-avp", "--source", "/tmp/movie.mkv", "--audio-mode", "pcm"],
        ):
            config.parse_args()

        self.assertEqual(config.audio_mode, AudioMode.PCM)
        self.assertFalse(config.transcode_audio)

    def test_audio_preferred_language_cli_normalizes_alias(self) -> None:
        config = Config()

        with patch.object(
            sys,
            "argv",
            ["bd-to-avp", "--source", "/tmp/movie.mkv", "--audio-preferred-language", "en-US"],
        ):
            config.parse_args()

        self.assertEqual(config.audio_preferred_language, "eng")

    def test_audio_preferred_language_cli_rejects_invalid_code(self) -> None:
        config = Config()

        with (
            patch.object(
                sys,
                "argv",
                ["bd-to-avp", "--source", "/tmp/movie.mkv", "--audio-preferred-language", "invalid"],
            ),
            self.assertRaises(SystemExit),
        ):
            config.parse_args()


if __name__ == "__main__":
    unittest.main()
