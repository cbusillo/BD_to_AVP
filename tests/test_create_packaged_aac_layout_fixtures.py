import json
import subprocess
import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from scripts.create_packaged_aac_layout_fixtures import (
    MAX_SOURCE_DURATION_SECONDS,
    MAX_SOURCE_SIZE_BYTES,
    FixtureBuildFailure,
    _audio_encoding_command,
    _create_identity_source,
    _mux_command,
    _run,
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

        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "source.mkv"
            source.write_bytes(b"video")
            with patch("scripts.create_packaged_aac_layout_fixtures._probe_document", return_value=document):
                with self.assertRaisesRegex(FixtureBuildFailure, "H.264"):
                    _source_video_summary(source)

    def test_source_video_rejects_oversized_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "source.mkv"
            with source.open("wb") as file:
                file.truncate(MAX_SOURCE_SIZE_BYTES + 1)

            with self.assertRaisesRegex(FixtureBuildFailure, "size limit"):
                _source_video_summary(source)

    def test_source_video_rejects_overlong_input(self) -> None:
        document = {
            "streams": [{"codec_type": "video", "codec_name": "h264"}],
            "format": {"duration": str(MAX_SOURCE_DURATION_SECONDS + 1)},
        }

        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "source.mkv"
            source.write_bytes(b"mvc")
            with patch("scripts.create_packaged_aac_layout_fixtures._probe_document", return_value=document):
                with self.assertRaisesRegex(FixtureBuildFailure, "duration limit"):
                    _source_video_summary(source)

    def test_source_video_rejects_non_finite_duration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "source.mkv"
            source.write_bytes(b"mvc")
            for duration in ("nan", "inf", "-inf"):
                document = {
                    "streams": [{"codec_type": "video", "codec_name": "h264"}],
                    "format": {"duration": duration},
                }
                with (
                    self.subTest(duration=duration),
                    patch("scripts.create_packaged_aac_layout_fixtures._probe_document", return_value=document),
                ):
                    with self.assertRaisesRegex(FixtureBuildFailure, "finite"):
                        _source_video_summary(source)

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

        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "source.mkv"
            source.write_bytes(b"video")
            with patch("scripts.create_packaged_aac_layout_fixtures._probe_document", return_value=document):
                summary = _source_video_summary(source)

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

    def test_command_failure_redacts_private_paths(self) -> None:
        private_source = Path("/Users/example/Private Movie/source.mkv")
        error = subprocess.CalledProcessError(
            1,
            ["ffprobe", private_source.as_posix()],
            stderr=f"{private_source}: Invalid data found",
        )

        with patch("scripts.create_packaged_aac_layout_fixtures.subprocess.run", side_effect=error):
            with self.assertRaises(FixtureBuildFailure) as context:
                _run([Path("/private/tools/ffprobe"), private_source])

        self.assertNotIn(private_source.as_posix(), str(context.exception))
        self.assertIn("<redacted-path>", str(context.exception))

    def test_interrupted_generation_removes_private_staging_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source.mkv"
            output = root / "fixtures"
            source.write_bytes(b"mvc")
            with (
                patch(
                    "scripts.create_packaged_aac_layout_fixtures._source_video_summary",
                    return_value={"duration_seconds": 4.213, "direct_mvc_route_proven": False},
                ),
                patch("scripts.create_packaged_aac_layout_fixtures._hash_file", return_value="a" * 64),
                patch("scripts.create_packaged_aac_layout_fixtures._version_line", return_value="tool version"),
                patch(
                    "scripts.create_packaged_aac_layout_fixtures._create_identity_source",
                    side_effect=KeyboardInterrupt,
                ),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    create_packaged_aac_layout_fixtures(source, output)

            self.assertFalse(output.exists())
            self.assertEqual(list(root.glob(".fixtures.*")), [])

    def test_success_receipt_versions_and_hashes_generator_inputs(self) -> None:
        def create_identity(_case: object, _track: object, output: Path) -> None:
            output.write_bytes(b"identity")

        def run_command(command: list[str | Path]) -> subprocess.CompletedProcess[str]:
            output = command[-1]
            if isinstance(output, Path) and output.suffix in {".mka", ".mkv"}:
                output.write_bytes(output.name.encode("utf-8"))
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source.mkv"
            output = root / "fixtures"
            source.write_bytes(b"mvc")
            with (
                patch(
                    "scripts.create_packaged_aac_layout_fixtures._source_video_summary",
                    return_value={"duration_seconds": 4.213, "direct_mvc_route_proven": False},
                ),
                patch("scripts.create_packaged_aac_layout_fixtures._hash_file", return_value="a" * 64),
                patch("scripts.create_packaged_aac_layout_fixtures._version_line", return_value="tool version"),
                patch(
                    "scripts.create_packaged_aac_layout_fixtures._create_identity_source", side_effect=create_identity
                ),
                patch("scripts.create_packaged_aac_layout_fixtures._run", side_effect=run_command),
                patch("scripts.create_packaged_aac_layout_fixtures._probe_document", return_value={"streams": []}),
                patch("scripts.create_packaged_aac_layout_fixtures._audio_streams", return_value=[]),
                patch("scripts.create_packaged_aac_layout_fixtures._validate_streams"),
            ):
                receipt = create_packaged_aac_layout_fixtures(source, output)

            persisted = json.loads((output / "fixture-receipt.json").read_text(encoding="utf-8"))
            self.assertEqual(receipt, persisted)
            self.assertEqual(receipt["schema_version"], 2)
            self.assertEqual(receipt["tools"]["ffmpeg"]["sha256"], "a" * 64)
            self.assertEqual(receipt["tools"]["ffprobe"]["version"], "tool version")
            self.assertFalse((output / ".work").exists())
