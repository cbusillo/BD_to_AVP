import json
import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from scripts.create_event_hls_mv_hevc_fixture import (
    DIRECT_FIXTURE_GENERATOR,
    INIT_FILENAME,
    MPD_FILENAME,
    PLAYLIST_FILENAME,
    FixtureGenerationError,
    _playlist_segments,
    _publish_fragments,
    _validate_media_tracks,
    hls_packaging_command,
    source_generator_command,
    validate_fixture,
)


class EventHlsFixtureTests(unittest.TestCase):
    def test_source_generator_reuses_direct_mv_hevc_script(self) -> None:
        command = source_generator_command(Path("source.mov"), Path("encoder"))

        self.assertEqual(command, [DIRECT_FIXTURE_GENERATOR, Path("source.mov"), Path("encoder")])

    def test_hls_command_preserves_stereo_video_and_audio(self) -> None:
        command = hls_packaging_command(Path("MP4Box"), Path("source.mov"), Path("fixture"))

        self.assertEqual(command[command.index("-dash") + 1], "2000")
        self.assertEqual(command[command.index("-frag") + 1], "2000")
        self.assertIn("-rap", command)
        self.assertIn("segment-", command)
        self.assertIn(Path("fixture") / "fragmentation.mpd", command)

    def test_playlist_parser_accepts_bounded_event_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture_directory = Path(temporary_directory)
            (fixture_directory / INIT_FILENAME).write_bytes(b"init")
            for index in range(2):
                (fixture_directory / f"segment-{index:03d}.m4s").write_bytes(b"segment")
            (fixture_directory / PLAYLIST_FILENAME).write_text(
                "\n".join(
                    [
                        "#EXTM3U",
                        "#EXT-X-VERSION:7",
                        "#EXT-X-PLAYLIST-TYPE:EVENT",
                        "#EXT-X-TARGETDURATION:2",
                        "#EXT-X-MEDIA-SEQUENCE:0",
                        '#EXT-X-MAP:URI="init.mp4"',
                        "#EXT-X-INDEPENDENT-SEGMENTS",
                        "#EXTINF:2.000,",
                        "segment-000.m4s",
                        "#EXTINF:2.000,",
                        "segment-001.m4s",
                        "#EXT-X-ENDLIST",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                _playlist_segments(fixture_directory / PLAYLIST_FILENAME, fixture_directory),
                [("segment-000.m4s", 2.0), ("segment-001.m4s", 2.0)],
            )

    def test_playlist_parser_rejects_vod_and_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture_directory = Path(temporary_directory)
            (fixture_directory / INIT_FILENAME).write_bytes(b"init")
            (fixture_directory / PLAYLIST_FILENAME).write_text(
                "\n".join(
                    [
                        "#EXTM3U",
                        "#EXT-X-PLAYLIST-TYPE:VOD",
                        "#EXT-X-TARGETDURATION:2",
                        '#EXT-X-MAP:URI="init.mp4"',
                        "#EXT-X-INDEPENDENT-SEGMENTS",
                        "#EXTINF:2.000,",
                        "../escape.m4s",
                    ]
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(FixtureGenerationError, "EVENT"):
                _playlist_segments(fixture_directory / PLAYLIST_FILENAME, fixture_directory)

    def test_fragment_publisher_renames_bounded_mp4box_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture_directory = Path(temporary_directory)
            (fixture_directory / "segment-init.mp4").write_bytes(b"init")
            (fixture_directory / "segment-1.m4s").write_bytes(b"one")
            (fixture_directory / "segment-2.m4s").write_bytes(b"two")
            (fixture_directory / MPD_FILENAME).write_text(
                '<MPD><Period><AdaptationSet><SegmentTemplate timescale="1000" duration="2000" />'
                "</AdaptationSet></Period></MPD>",
                encoding="utf-8",
            )

            self.assertEqual(_publish_fragments(fixture_directory), [2.0, 2.0])
            self.assertTrue((fixture_directory / INIT_FILENAME).is_file())
            self.assertTrue((fixture_directory / "segment-000.m4s").is_file())
            self.assertTrue((fixture_directory / "segment-001.m4s").is_file())
            self.assertFalse((fixture_directory / MPD_FILENAME).exists())

    def test_media_track_validation_requires_hevc_and_aac(self) -> None:
        document = {
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "hevc",
                    "width": 3840,
                    "height": 2160,
                    "duration": "4.0",
                    "start_time": "0.0",
                },
                {
                    "codec_type": "audio",
                    "codec_name": "aac",
                    "duration": "4.0",
                    "start_time": "0.0",
                },
            ]
        }

        _validate_media_tracks(document, require_audio=True)

        document["streams"].pop()
        with self.assertRaisesRegex(FixtureGenerationError, "AAC"):
            _validate_media_tracks(document, require_audio=True)

    def test_validate_fixture_checks_boxes_and_tracks_without_encoding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture_directory = Path(temporary_directory)
            (fixture_directory / INIT_FILENAME).write_bytes(b"init")
            for index in range(2):
                (fixture_directory / f"segment-{index:03d}.m4s").write_bytes(b"segment")
            (fixture_directory / PLAYLIST_FILENAME).write_text(
                "\n".join(
                    [
                        "#EXTM3U",
                        "#EXT-X-VERSION:7",
                        "#EXT-X-PLAYLIST-TYPE:EVENT",
                        "#EXT-X-TARGETDURATION:2",
                        '#EXT-X-MAP:URI="init.mp4"',
                        "#EXT-X-INDEPENDENT-SEGMENTS",
                        "#EXTINF:2.000,",
                        "segment-000.m4s",
                        "#EXTINF:2.000,",
                        "segment-001.m4s",
                        "#EXT-X-ENDLIST",
                    ]
                ),
                encoding="utf-8",
            )
            probe = {
                "streams": [
                    {"codec_type": "video", "codec_name": "hevc", "width": 3840, "height": 2160, "duration": "4"},
                    {"codec_type": "audio", "codec_name": "aac", "duration": "4"},
                ]
            }
            boxes = '\n'.join(f'Type="{box}"' for box in ("hvcC", "lhvC", "eyes", "vexu"))

            def fake_run(command, **_kwargs):
                if "ffprobe" in str(command[0]):
                    return type("Result", (), {"stdout": json.dumps(probe), "stderr": ""})()
                return type("Result", (), {"stdout": boxes, "stderr": ""})()

            with patch("scripts.create_event_hls_mv_hevc_fixture._run", side_effect=fake_run):
                validate_fixture(fixture_directory, ffprobe_path=Path("ffprobe"), mp4box_path=Path("MP4Box"))
