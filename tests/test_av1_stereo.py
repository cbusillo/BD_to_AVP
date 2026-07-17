import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bd_to_avp.modules import video
from bd_to_avp.modules.config import config
from bd_to_avp.modules.disc import DiscInfo
from bd_to_avp.modules.video_mode import VideoMode


class Av1StereoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.disc_info = DiscInfo(
            name="Sample",
            frame_rate="24000/1001",
            resolution="1920x1080",
            color_depth=8,
        )

    def test_native_av1_command_encodes_one_full_side_by_side_stream(self) -> None:
        with (
            patch.object(video.config, "av1_crf", 30),
            patch.object(video.config, "swap_eyes", False),
            patch.object(video.config, "frame_rate", ""),
            patch.object(video.config, "resolution", ""),
        ):
            command = video.generate_native_mvc_av1_command(Path("stereo.mp4"), self.disc_info, "")

        filter_graph = command[command.index("-filter_complex") + 1]
        self.assertIn("split=2", filter_graph)
        self.assertIn("crop=1920:1080:0:0", filter_graph)
        self.assertIn("crop=1920:1080:1920:0", filter_graph)
        self.assertIn("hstack=inputs=2", filter_graph)
        self.assertEqual(command[command.index("-vcodec") + 1], "libsvtav1")
        self.assertEqual(
            command[command.index("-bsf:v") + 1],
            "av1_metadata=color_primaries=1:transfer_characteristics=1:matrix_coefficients=1:color_range=tv",
        )
        self.assertEqual(command[command.index("-crf") + 1], "30")
        self.assertEqual(command[command.index("-preset") + 1], "9")
        self.assertEqual(command[command.index("-color_primaries") + 1], "bt709")
        self.assertEqual(command[command.index("-color_trc") + 1], "bt709")
        self.assertEqual(command[command.index("-colorspace") + 1], "bt709")
        self.assertEqual(command[command.index("-color_range") + 1], "tv")
        self.assertNotIn("-svtav1-params", command)
        self.assertNotIn("-svtav1_params", command)
        self.assertIn("file:stereo.mp4", command)

    def test_native_av1_command_preserves_crop_per_eye(self) -> None:
        with (
            patch.object(video.config, "av1_crf", 32),
            patch.object(video.config, "swap_eyes", True),
            patch.object(video.config, "frame_rate", ""),
            patch.object(video.config, "resolution", ""),
        ):
            command = video.generate_native_mvc_av1_command(
                Path("stereo.mp4"),
                self.disc_info,
                "1880:1040:20:20",
            )

        filter_graph = command[command.index("-filter_complex") + 1]
        self.assertEqual(filter_graph.count("crop=1880:1040:20:20"), 2)
        self.assertIn("hstack=inputs=2", filter_graph)

    def test_av1_stereo_patch_contains_current_apple_eye_and_packing_boxes(self) -> None:
        patch_xml = video.av1_stereo_patch_xml().lower()

        self.assertIn("av01.av1c+", patch_xml)
        self.assertIn('fcc="vexu"', patch_xml)
        self.assertIn("73747269", patch_xml)
        self.assertIn("7061636b", patch_xml)
        self.assertIn("706b696e", patch_xml)
        self.assertIn("73696465", patch_xml)

    def test_add_av1_stereo_metadata_removes_temporary_patch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            patch_path = Path(temp_dir) / "stereo.xml"
            file_descriptor = os.open(patch_path, os.O_CREAT | os.O_RDWR)
            input_path = Path(temp_dir) / "input.mp4"
            output_path = Path(temp_dir) / "output.mp4"

            with (
                patch.object(video.tempfile, "mkstemp", return_value=(file_descriptor, str(patch_path))),
                patch.object(video, "run_command") as run_command,
                patch.object(video.config, "MP4BOX_PATH", Path("/tools/MP4Box")),
            ):
                video.add_av1_stereo_metadata(input_path, output_path)

            run_command.assert_called_once_with(
                [Path("/tools/MP4Box"), "-patch", patch_path, input_path, "-out", output_path],
                "add Apple stereo packing metadata to AV1 video.",
            )
            self.assertFalse(patch_path.exists())

    def test_final_file_tag_distinguishes_av1_from_mv_hevc(self) -> None:
        with patch.object(config, "video_mode", VideoMode.MV_HEVC):
            self.assertEqual(config.final_file_tag, "_AVP")
        with patch.object(config, "video_mode", VideoMode.AV1_SBS):
            self.assertEqual(config.final_file_tag, "_AV1_Stereo")
