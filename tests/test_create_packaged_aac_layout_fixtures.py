import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from scripts.create_packaged_aac_layout_fixtures import (
    FixtureBuildFailure,
    _audio_encoding_command,
    _create_identity_source,
    _mux_command,
    _source_video_summary,
    create_packaged_aac_layout_fixtures,
)
from scripts.verify_packaged_aac_layouts import load_fixture_manifest


class PackagedAacFixtureCommandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cases = {case.case_id: case for case in load_fixture_manifest().cases}

    def test_missing_layout_encoding_strips_channel_positions(self) -> None:
        track = self.cases["fail-missing-layout"].tracks[0]

        command = _audio_encoding_command(track, Path("source.wav"), Path("output.mka"))

        self.assertIn("pcm_s24le", command)
        self.assertEqual(command[command.index("-ch_layout") + 1], "6c")

    def test_pce_fixture_uses_side_layout_identity_source(self) -> None:
        case = self.cases["fail-pce-layout"]
        track = case.tracks[0]

        with patch("scripts.create_packaged_aac_layout_fixtures.create_channel_identity_audio") as create:
            _create_identity_source(case, track, Path("source.wav"))

        self.assertEqual(create.call_args.args[1], "5.1(side)")

    def test_mux_command_preserves_track_metadata_and_default(self) -> None:
        case = self.cases["preserve-multilingual"]

        command = _mux_command(
            Path("source.mkv"),
            case,
            [Path("english.mka"), Path("japanese.mka")],
            Path("fixture.mkv"),
        )

        self.assertIn("language=eng", command)
        self.assertIn("title=English Stereo", command)
        self.assertIn("language=jpn", command)
        self.assertIn("title=Japanese 5.1", command)
        self.assertEqual(command[command.index("-disposition:a:0") + 1], "default")
        self.assertEqual(command[command.index("-disposition:a:1") + 1], "0")

    def test_source_video_requires_h264_mkv_long_enough(self) -> None:
        document = {
            "streams": [{"codec_type": "video", "codec_name": "hevc"}],
            "format": {"duration": "4.0"},
        }

        with patch("scripts.create_packaged_aac_layout_fixtures._probe_document", return_value=document):
            with self.assertRaisesRegex(FixtureBuildFailure, "H.264"):
                _source_video_summary(Path("source.mkv"))

    def test_source_video_receipt_defers_direct_route_proof(self) -> None:
        document = {
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "profile": "High",
                    "width": 1920,
                    "height": 1080,
                }
            ],
            "format": {"duration": "4.213"},
        }

        with patch("scripts.create_packaged_aac_layout_fixtures._probe_document", return_value=document):
            summary = _source_video_summary(Path("source.mkv"))

        self.assertFalse(summary["direct_mvc_route_proven"])

    def test_output_directory_must_be_new(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source.mkv"
            output = root / "fixtures"
            source.touch()
            output.mkdir()

            with self.assertRaisesRegex(FixtureBuildFailure, "must not already exist"):
                create_packaged_aac_layout_fixtures(source, output)
