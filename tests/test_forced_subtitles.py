"""Forced-only subtitle tracks for Blu-ray sources read without MakeMKV (#1126).

MakeMKV adds a forced-only track after each PG stream whose captions flag forced_on_flag, and
drops it when the stream has none. A direct read rebuilds those tracks from the same streams.
"""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import numpy as np

from bd_to_avp.modules import sub
from bd_to_avp.modules.config import Stage, config
from bd_to_avp.vendor.pgsrip import mkv
from bd_to_avp.vendor.pgsrip.media import PgsSubtitleItem
from bd_to_avp.vendor.pgsrip.media_path import MediaPath
from bd_to_avp.vendor.pgsrip.ocr import AppleVisionOcr, OcrWord
from bd_to_avp.vendor.pgsrip.pgs import PgsReader, contains_forced_objects, forced_only_pgs
from tests.fixtures import forced_pgs

FIXTURE = forced_pgs.FIXTURE.read_bytes()
MEDIA_PATH = MediaPath("movie.en.sup")


def caption_shape(*captions: tuple[str, int, int]) -> tuple[int, int]:
    """The bitmap pgsrip decodes for captions shown together: their rows stacked."""
    return sum(height for _, _, height in captions), captions[0][1]


def regular_only_stream() -> bytes:
    """The fixture's first caption, which is not forced."""
    display_sets = list(PgsReader.decode(FIXTURE, MEDIA_PATH))[:2]
    return b"".join(segment.bytes for display_set in display_sets for segment in display_set.segments)


def items(data: bytes) -> list[tuple[str, str, tuple[int, int]]]:
    return [
        (str(item.start), str(item.end), item.image.shape)
        for item in PgsSubtitleItem.create_items(MEDIA_PATH, PgsReader.decode(data, MEDIA_PATH))
    ]


class ForcedOnlyStreamTests(unittest.TestCase):
    def test_keeps_only_forced_objects_for_as_long_as_they_show(self) -> None:
        self.assertEqual(
            items(forced_only_pgs(FIXTURE, MEDIA_PATH)),
            [
                ("00:00:03,000", "00:00:04,000", caption_shape(forced_pgs.FORCED_ALONE)),
                # Shown beside a regular caption; it leaves when the next composition drops it.
                ("00:00:05,000", "00:00:06,000", caption_shape(forced_pgs.MIXED_FORCED)),
                # Defined in an earlier display set that showed only the regular caption.
                ("00:00:09,000", "00:00:10,000", caption_shape(forced_pgs.EARLY_FORCED)),
                # Replaced by another forced caption without a new epoch; pgsrip reads one picture
                # per epoch, so each picture starts its own.
                ("00:00:11,000", "00:00:12,000", caption_shape(forced_pgs.FORCED_FIRST)),
                ("00:00:12,000", "00:00:13,000", caption_shape(forced_pgs.FORCED_NEXT)),
            ],
        )

    def test_full_stream_keeps_every_caption(self) -> None:
        # Including the one whose palette update carries no window definition.
        self.assertEqual(
            [shape for _, _, shape in items(FIXTURE)],
            [
                caption_shape(forced_pgs.REGULAR),
                caption_shape(forced_pgs.FORCED_ALONE),
                caption_shape(forced_pgs.MIXED_REGULAR, forced_pgs.MIXED_FORCED),
                caption_shape(forced_pgs.EARLY_REGULAR, forced_pgs.EARLY_FORCED),
                caption_shape(forced_pgs.FORCED_FIRST),
            ],
        )

    def test_stream_without_forced_captions_has_no_forced_only_stream(self) -> None:
        self.assertTrue(contains_forced_objects(FIXTURE, MEDIA_PATH))
        self.assertFalse(contains_forced_objects(regular_only_stream(), MEDIA_PATH))
        self.assertEqual(forced_only_pgs(regular_only_stream(), MEDIA_PATH), b"")


def pgs_stream(index: int, language: str) -> dict[str, Any]:
    return {
        "index": index,
        "codec_type": "subtitle",
        "codec_name": "hdmv_pgs_subtitle",
        "tags": {"language": language},
        "disposition": {"default": 0, "forced": 0},
    }


class ForcedOnlyTrackSelectionTests(unittest.TestCase):
    @staticmethod
    def selected(*, forced_only_tracks: bool, languages: set[Any] | None = None) -> tuple[list[Any], list[int]]:
        streams = {2: FIXTURE, 3: regular_only_stream()}
        reads: list[int] = []

        def read_data(media_path: object, track_id: int, temp_folder: str, **_kwargs: object) -> bytes:
            del media_path
            reads.append(track_id)
            (Path(temp_folder) / f"track-{track_id}.sup").write_bytes(streams[track_id])
            return streams[track_id]

        metadata = {"streams": [{"index": 0, "codec_type": "video"}, pgs_stream(2, "eng"), pgs_stream(3, "fra")]}
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.object(mkv, "run_ffprobe", return_value=metadata),
                patch.object(mkv.MkvPgs, "read_data", side_effect=read_data),
            ):
                movie = mkv.Mkv(f"{temp_dir}/movie_audio_subtitles.ts", forced_only_tracks=forced_only_tracks)
                options = mkv.Options(overwrite=True, one_per_lang=False, languages=languages or set())
                selected = [
                    (track.id, str(language), number, track.forced, track.forced_only)
                    for track, language, number in movie.get_selected_pgs_tracks(options)
                ]
                # The rip reads each stream from the copy made while selecting.
                medias = list(movie.get_selected_pgs_medias(options))
                for _, pgs in medias:
                    _ = pgs.items
                    shutil.rmtree(pgs.temp_folder)
                movie.close()
        return selected, reads

    def test_stream_with_forced_captions_gains_a_forced_only_track_after_the_full_tracks(self) -> None:
        selected, reads = self.selected(forced_only_tracks=True)

        # The order and numbering pgsrip gives a MakeMKV file with the same streams.
        self.assertEqual(
            selected,
            [(2, "en", 0, False, False), (3, "fr", 0, False, False), (2, "en", 1, True, True)],
        )
        self.assertEqual(sorted(reads), [2, 3])

    def test_mkv_from_makemkv_gets_no_derived_tracks(self) -> None:
        selected, reads = self.selected(forced_only_tracks=False)

        self.assertEqual(selected, [(2, "en", 0, False, False), (3, "fr", 0, False, False)])
        self.assertEqual(reads, [2, 3])

    def test_streams_in_other_languages_are_not_read_for_forced_captions(self) -> None:
        selected, reads = self.selected(forced_only_tracks=True, languages={mkv.Language("fra")})

        self.assertEqual(selected, [(3, "fr", 0, False, False)])
        self.assertEqual(reads, [3])


CAPTION_TEXT = {
    caption_shape(*captions): " ".join(name for name, _, _ in captions)
    for captions in (
        (forced_pgs.REGULAR,),
        (forced_pgs.FORCED_ALONE,),
        (forced_pgs.MIXED_FORCED,),
        (forced_pgs.EARLY_FORCED,),
        (forced_pgs.FORCED_FIRST,),
        (forced_pgs.FORCED_NEXT,),
        (forced_pgs.MIXED_REGULAR, forced_pgs.MIXED_FORCED),
        (forced_pgs.EARLY_REGULAR, forced_pgs.EARLY_FORCED),
    )
}


def recognize(image: np.ndarray, _language: object = None) -> dict[str, list[Any]]:
    """OCR that names a fixture caption from its decoded bitmap."""
    height, width = image.shape[:2]
    text = CAPTION_TEXT[(height, width)]
    return AppleVisionOcr._tsv_data_from_words([OcrWord(text, 100, 0, 0, width, height, 1, 1)])


def run_ffmpeg(*arguments: object) -> None:
    subprocess.run(
        [str(config.FFMPEG_PATH), "-hide_banner", "-loglevel", "error", "-nostdin", *map(str, arguments)],
        check=True,
        capture_output=True,
        timeout=60,
    )


def srt_cues(path: Path) -> list[tuple[str, str]]:
    blocks = [block.splitlines() for block in path.read_text(encoding="utf-8").strip().split("\n\n")]
    return [(block[1], " ".join(block[2:]).upper()) for block in blocks]


@unittest.skipUnless(config.FFMPEG_PATH.is_file() and config.FFPROBE_PATH.is_file(), "needs ffmpeg and ffprobe")
class ForcedOnlyRipTests(unittest.TestCase):
    def rip(self, folder: Path, source: Path, *, forced_only_tracks: bool) -> dict[str, list[tuple[str, str]]]:
        with (
            patch.object(config, "skip_subtitles", False),
            patch.object(config, "remove_extra_languages", False),
            patch.object(config, "start_stage", Stage.CREATE_MKV),
            patch.object(AppleVisionOcr, "image_to_data", side_effect=recognize),
        ):
            warnings: list[str] = []
            sub.extract_subtitle_to_srt(source, folder, warnings.append, forced_only_tracks=forced_only_tracks)
        self.assertEqual(warnings, [])
        return {path.name.removeprefix(source.stem): srt_cues(path) for path in sorted(folder.glob("*.srt"))}

    def test_streams_read_without_makemkv_rip_like_the_makemkv_file_of_the_same_streams(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            english, french, english_forced = root / "en.sup", root / "fr.sup", root / "en-forced.sup"
            english.write_bytes(FIXTURE)
            french.write_bytes(regular_only_stream())
            english_forced.write_bytes(forced_only_pgs(FIXTURE, MEDIA_PATH))
            # -copyts keeps each caption's time; FFmpeg would otherwise start every .sup input at zero.
            video = ("-copyts", "-f", "lavfi", "-i", "testsrc2=size=64x64:rate=24")

            # The disc's streams as the reader passes them on: each PG stream once, nothing forced.
            # (FFmpeg cannot write PG streams to MPEG-TS, so this uses Matroska like MakeMKV's file;
            # the reader's sidecar is read through the same FFmpeg demuxing.)
            direct = root / "direct"
            direct.mkdir()
            sidecar = direct / "Movie_audio_subtitles.mkv"
            run_ffmpeg(
                *video,
                *("-i", english, "-i", french),
                *("-map", "0:v", "-map", "1", "-map", "2", "-c:v", "mpeg4", "-c:s", "copy", "-t", "14"),
                *("-metadata:s:s:0", "language=eng", "-metadata:s:s:1", "language=fra", "-y", sidecar),
            )
            # MakeMKV's file: each full track, followed by its forced-only track when it has one.
            makemkv = root / "makemkv"
            makemkv.mkdir()
            title = makemkv / "Movie.mkv"
            run_ffmpeg(
                *video,
                *("-i", english, "-i", english_forced, "-i", french),
                *("-map", "0:v", "-map", "1", "-map", "2", "-map", "3", "-c:v", "mpeg4", "-c:s", "copy", "-t", "14"),
                *("-metadata:s:s:0", "language=eng", "-metadata:s:s:1", "language=eng"),
                *("-metadata:s:s:2", "language=fra", "-disposition:s:1", "forced", "-y", title),
            )

            direct_srt = self.rip(direct, sidecar, forced_only_tracks=True)
            makemkv_srt = self.rip(makemkv, title, forced_only_tracks=False)

        self.assertEqual(direct_srt, makemkv_srt)
        self.assertEqual(
            direct_srt["-1.forced.en.srt"],
            [
                ("00:00:03,000 --> 00:00:04,000", "FORCED ALONE"),
                ("00:00:05,000 --> 00:00:06,000", "MIXED FORCED"),
                ("00:00:09,000 --> 00:00:10,000", "EARLY FORCED"),
                ("00:00:11,000 --> 00:00:12,000", "FORCED FIRST"),
                ("00:00:12,000 --> 00:00:13,000", "FORCED NEXT"),
            ],
        )
        self.assertEqual(
            [text for _, text in direct_srt[".en.srt"]],
            ["REGULAR", "FORCED ALONE", "MIXED REGULAR MIXED FORCED", "EARLY REGULAR EARLY FORCED", "FORCED FIRST"],
        )
        self.assertEqual(sorted(direct_srt), ["-1.forced.en.srt", ".en.srt", ".fr.srt"])


if __name__ == "__main__":
    unittest.main()
