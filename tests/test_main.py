import sys
import unittest
from unittest.mock import patch

from bd_to_avp import __main__
from bd_to_avp.modules.audio_mode import AudioMode
from bd_to_avp.modules.config import Config


class MainSmokeTests(unittest.TestCase):
    def test_audio_mode_defaults_and_legacy_false_remain_pcm(self) -> None:
        config = Config()

        self.assertEqual(config.audio_mode, AudioMode.PCM)
        config.transcode_audio = False
        self.assertEqual(config.audio_mode, AudioMode.PCM)

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


if __name__ == "__main__":
    unittest.main()
