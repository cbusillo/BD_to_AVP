import json
import unittest
from pathlib import Path
from unittest.mock import patch

from bd_to_avp.modules import video
from bd_to_avp.process_runner import ProcessCancelled


class VideoProbeTests(unittest.TestCase):
    def test_fx_upscale_command_uses_integer_quality_factor(self) -> None:
        command = video.fx_upscale_command(Path("movie.mov"), 75)

        self.assertEqual(
            command,
            [video.config.FX_UPSCALE_PATH, "--bitrate-scaling-factor", "0.75", Path("movie.mov")],
        )

    def test_fx_upscale_command_rejects_non_integer_quality(self) -> None:
        for invalid in (-1, 101, 75.0, True):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "integer from 0 through 100"):
                    video.fx_upscale_command(Path("movie.mov"), invalid)  # type: ignore[arg-type]

    def test_upscale_file_uses_shared_command_helper(self) -> None:
        with (
            patch.object(video.config, "upscale_quality", 65),
            patch.object(video, "run_process_capture") as run_process_capture,
        ):
            video.upscale_file(Path("movie.mov"))

        run_process_capture.assert_called_once()
        self.assertEqual(
            run_process_capture.call_args.args[0],
            [video.config.FX_UPSCALE_PATH, "--bitrate-scaling-factor", "0.65", Path("movie.mov")],
        )

    def test_malformed_ffprobe_output_uses_default_color_depth(self) -> None:
        malformed = json.JSONDecodeError("bad metadata", "", 0)

        with patch.object(video, "run_ffprobe", side_effect=malformed):
            color_depth = video.get_video_color_depth(Path("movie.mkv"))

        self.assertEqual(color_depth, video.DiscInfo.color_depth)

    def test_invalid_ffprobe_utf8_uses_default_color_depth(self) -> None:
        with patch.object(video, "run_ffprobe", side_effect=UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")):
            color_depth = video.get_video_color_depth(Path("movie.mkv"))

        self.assertEqual(color_depth, video.DiscInfo.color_depth)

    def test_process_cancellation_does_not_fall_back_to_default_color_depth(self) -> None:
        cancellation = ProcessCancelled("cancelled")
        with (
            patch.object(video, "run_ffprobe", side_effect=cancellation),
            self.assertRaises(ProcessCancelled) as context,
        ):
            video.get_video_color_depth(Path("movie.mkv"))

        self.assertIs(context.exception, cancellation)


if __name__ == "__main__":
    unittest.main()
