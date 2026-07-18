import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import add_spatial_video_metadata


class SpatialVideoMetadataTests(unittest.TestCase):
    def test_vexu_content_matches_apple_spatial_box_structure(self) -> None:
        content = add_spatial_video_metadata.spatial_vexu_content(64, 0.02).hex().upper()

        self.assertEqual(
            content,
            "00000045657965730000000D7374726900000000030000001863616D73"
            "00000010626C696E000000000000FA0000000018636D6679000000106461646A"
            "00000000000000C80000001870726F6A0000001070726A690000000072656374",
        )

    def test_patch_replaces_incomplete_vexu_after_layered_hevc_configuration(self) -> None:
        patch_xml = add_spatial_video_metadata.spatial_metadata_patch_xml(64, 0)

        self.assertIn('path="trak.mdia.minf.stbl.stsd.hvc1.vexu"', patch_xml)
        self.assertIn('path="trak.mdia.minf.stbl.stsd.hvc1.lhvC+"', patch_xml)
        self.assertIn('fcc="vexu"', patch_xml)
        self.assertIn("626C696E000000000000FA00", patch_xml)
        self.assertIn("70726A690000000072656374", patch_xml)

    def test_spatial_metadata_rejects_invalid_geometry(self) -> None:
        with self.assertRaisesRegex(ValueError, "baseline_mm"):
            add_spatial_video_metadata.spatial_vexu_content(0, 0)
        with self.assertRaisesRegex(ValueError, "disparity_adjustment"):
            add_spatial_video_metadata.spatial_vexu_content(64, 1.1)

    def test_add_spatial_metadata_removes_temporary_patch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            input_path = root / "input.mov"
            output_path = root / "output.mov"
            input_path.touch()

            with patch.object(add_spatial_video_metadata.subprocess, "run") as run:
                add_spatial_video_metadata.add_spatial_video_metadata(
                    input_path,
                    output_path,
                    64,
                    0,
                    Path("/tools/MP4Box"),
                )

            command = run.call_args.args[0]
            patch_path = Path(command[2])
            self.assertEqual(command[0], "/tools/MP4Box")
            self.assertEqual(command[1], "-patch")
            self.assertEqual(command[3:], [str(input_path), "-out", str(output_path)])
            self.assertFalse(patch_path.exists())
            self.assertTrue(run.call_args.kwargs["check"])


if __name__ == "__main__":
    unittest.main()
