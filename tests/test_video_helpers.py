import json
import unittest
from pathlib import Path
from unittest.mock import patch

from bd_to_avp.modules import video


class VideoProbeTests(unittest.TestCase):
    def test_malformed_ffprobe_output_uses_default_color_depth(self) -> None:
        malformed = json.JSONDecodeError("bad metadata", "", 0)

        with patch.object(video, "run_ffprobe", side_effect=malformed):
            color_depth = video.get_video_color_depth(Path("movie.mkv"))

        self.assertEqual(color_depth, video.DiscInfo.color_depth)

    def test_invalid_ffprobe_utf8_uses_default_color_depth(self) -> None:
        with patch.object(video, "run_ffprobe", side_effect=UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")):
            color_depth = video.get_video_color_depth(Path("movie.mkv"))

        self.assertEqual(color_depth, video.DiscInfo.color_depth)


if __name__ == "__main__":
    unittest.main()
