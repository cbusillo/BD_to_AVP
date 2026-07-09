import sys
import unittest
from unittest.mock import patch

from bd_to_avp import __main__


class MainSmokeTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
