import json
import unittest

from pathlib import Path

from scripts.create_real_mvc_qualification_segment import (
    SegmentFailure,
    build_evidence,
    build_mkvmerge_command,
    public_track_summary,
    validate_segment_document,
)


def mvc_document(*, stereo_mode: int = 13) -> dict[str, object]:
    return {
        "container": {"properties": {"title": "Private title", "duration": 65_649_000_000}},
        "tracks": [
            {
                "id": 0,
                "type": "video",
                "codec": "AVC/H.264/MPEG-4p10",
                "properties": {
                    "language": "und",
                    "pixel_dimensions": "1920x1080",
                    "stereo_mode": stereo_mode,
                    "track_name": "Private video title",
                },
            },
            {
                "id": 1,
                "type": "audio",
                "codec": "TrueHD Atmos",
                "properties": {"language": "eng", "track_name": "Private audio title"},
            },
            {
                "id": 2,
                "type": "subtitles",
                "codec": "HDMV PGS",
                "properties": {"language": "eng", "track_name": "Private subtitle title"},
            },
        ],
    }


class RealMVCQualificationSegmentTests(unittest.TestCase):
    def test_mkvmerge_command_has_stable_selection_and_private_metadata_stripping(self) -> None:
        command = build_mkvmerge_command(
            "mkvmerge",
            Path("private-source.mkv"),
            Path("segment.mkv"),
            start_seconds=4500,
            duration_seconds=65.648,
            video_track=0,
            audio_track=1,
            subtitle_track=6,
            deterministic_seed="seed-v1",
        )

        self.assertIn("parts:4500s-4565.648s", command)
        self.assertIn("--deterministic", command)
        self.assertIn("--no-date", command)
        self.assertEqual(command.count("--track-name"), 3)
        self.assertEqual(command[-1], "private-source.mkv")

    def test_public_track_summary_omits_titles_and_container_metadata(self) -> None:
        encoded = json.dumps(public_track_summary(mvc_document()))

        self.assertNotIn("Private", encoded)
        self.assertIn("TrueHD Atmos", encoded)
        self.assertIn("HDMV PGS", encoded)

    def test_segment_validation_requires_mvc_both_eyes_laced(self) -> None:
        validate_segment_document(mvc_document())

        with self.assertRaisesRegex(SegmentFailure, "both-eyes-laced"):
            validate_segment_document(mvc_document(stereo_mode=0))

    def test_segment_validation_requires_pgs_subtitles(self) -> None:
        document = mvc_document()
        document["tracks"][2]["codec"] = "SubRip/SRT"

        with self.assertRaisesRegex(SegmentFailure, "HDMV PGS"):
            validate_segment_document(document)

    def test_evidence_is_path_free_and_records_supersession(self) -> None:
        evidence = build_evidence(
            source_sha256="a" * 64,
            source_size_bytes=32_000_000_000,
            source_duration_seconds=5737.76,
            source_track_count=55,
            segment_sha256="b" * 64,
            segment_size_bytes=354_000_000,
            segment_duration_seconds=65.649,
            segment_tracks=public_track_summary(mvc_document()),
            repeat_sha256="b" * 64,
            start_seconds=4500,
            requested_duration_seconds=65.648,
            video_track=0,
            audio_track=1,
            subtitle_track=6,
            deterministic_seed="seed-v1",
            mkvmerge_version="mkvmerge v100.0",
            ffprobe_version="ffprobe version 8.1.2",
        )
        encoded = json.dumps(evidence)

        self.assertTrue(evidence["acceptance"]["passed"])
        self.assertIn("supersedes", evidence)
        self.assertNotIn("/Users/", encoded)
        self.assertNotIn("Private", encoded)


if __name__ == "__main__":
    unittest.main()
