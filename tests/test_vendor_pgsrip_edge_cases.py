import unittest
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock, patch

import numpy as np

from bd_to_avp.vendor.pgsrip.media_path import MediaPath
from bd_to_avp.vendor.pgsrip.media import PgsSubtitleItem
from bd_to_avp.vendor.pgsrip.mkv import Mkv, MkvTrack
from bd_to_avp.vendor.pgsrip.options import Options
from bd_to_avp.vendor.pgsrip.pgs import WindowDefinitionSegment
from bd_to_avp.vendor.pgsrip.ripper import PgsToSrtRipper
from bd_to_avp.vendor.pgsrip.utils import from_hex, to_time


class MkvTrackOrderingTests(unittest.TestCase):
    def test_pgs_media_order_treats_missing_forced_flag_as_unforced(self) -> None:
        track_without_forced = MkvTrack(
            {
                "id": 2,
                "type": "subtitles",
                "codec": "HDMV PGS",
                "properties": {"language": "eng", "enabled_track": True},
            }
        )
        forced_track = MkvTrack(
            {
                "id": 1,
                "type": "subtitles",
                "codec": "HDMV PGS",
                "properties": {"language": "eng", "enabled_track": True, "forced_track": True},
            }
        )
        mkv = Mkv.__new__(Mkv)
        mkv.tracks = [forced_track, track_without_forced]
        mkv.media_path = Mock()

        with (
            patch(
                "bd_to_avp.vendor.pgsrip.mkv.MkvPgs.expected_srt_path",
                return_value=Mock(exists=Mock(return_value=False)),
            ),
            patch("bd_to_avp.vendor.pgsrip.mkv.MkvPgs") as mkv_pgs,
        ):
            mkv_pgs.return_value.matches.return_value = True
            list(mkv.get_pgs_medias(Options(one_per_lang=False, overwrite=True)))

        selected_track_ids = [call.args[1] for call in mkv_pgs.call_args_list]
        self.assertEqual(selected_track_ids, [2, 1])


class PgsSubtitleItemTimestampTests(unittest.TestCase):
    def test_zero_valued_timestamp_converts_to_subrip_zero(self) -> None:
        timestamp = to_time(0)

        self.assertIsNotNone(timestamp)
        self.assertEqual(str(timestamp), "00:00:00,000")

    def test_zero_valued_next_start_is_available_for_end_repair(self) -> None:
        item = _subtitle_item(start=-5000, end=-5000)
        next_item = _subtitle_item(start=0, end=12000)

        self.assertTrue(item.auto_fix(next_item))

        self.assertEqual(item.end, -1)


class PgsSubtitleItemWindowTests(unittest.TestCase):
    def test_offsets_ignore_display_sets_without_windows(self) -> None:
        item = PgsSubtitleItem(0, MediaPath("fake.sup"), [_display_set(0, None, None), _display_set(1, 12, 34)])

        self.assertEqual(item.x_offset, 12)
        self.assertEqual(item.y_offset, 34)

    def test_window_definition_segment_allows_zero_windows(self) -> None:
        segment = WindowDefinitionSegment(_pgs_segment_bytes(b"\x00"))

        self.assertEqual(segment.num_windows, 0)
        self.assertIsNone(segment.window_id)

    def test_empty_hex_converts_to_none(self) -> None:
        self.assertIsNone(from_hex(b""))


class PgsRipperEmptyTrackTests(unittest.TestCase):
    def test_empty_subtitle_items_do_not_crash_ripper_initialization(self) -> None:
        pgs = Mock()
        pgs.items = []

        ripper = PgsToSrtRipper(pgs, Options())

        self.assertEqual(ripper.gap, (30, 100))

    def test_empty_subtitle_items_rip_to_empty_srt(self) -> None:
        pgs = Mock()
        pgs.items = []
        pgs.media_path = Mock()
        pgs.media_path.translate.return_value = Path("empty.srt")

        ripper = PgsToSrtRipper(pgs, Options())

        with patch("bd_to_avp.vendor.pgsrip.ripper.SubRipFile") as subrip_file:
            subs = Mock()
            subrip_file.return_value = subs

            self.assertIs(ripper.rip(lambda text: text), subs)

        subs.clean_indexes.assert_called_once()
        subs.append.assert_not_called()

    def test_ripper_uses_injected_ocr_backend_to_create_srt_item(self) -> None:
        item = _subtitle_item(start=1000, end=2000)
        image = Mock()
        image.data = np.full((10, 30), 255, dtype=np.uint8)
        image.shape = image.data.shape
        cast(Any, item).image = image
        pgs = Mock()
        pgs.items = [item]
        pgs.language = None
        pgs.temp_folder = ""
        pgs.media_path = Mock()
        pgs.media_path.translate.return_value = Path("movie.srt")
        ocr_backend = Mock()
        ocr_backend.image_to_data.return_value = {
            "level": [5],
            "page_num": [1],
            "block_num": [1],
            "par_num": [1],
            "line_num": [1],
            "word_num": [1],
            "left": [1],
            "top": [1],
            "width": [30],
            "height": [10],
            "conf": [99],
            "text": ["Hello"],
        }

        ripper = PgsToSrtRipper(pgs, Options(ocr_backend=ocr_backend))

        with patch("bd_to_avp.vendor.pgsrip.ripper.SubRipFile") as subrip_file:
            subs = Mock()
            subrip_file.return_value = subs

            ripper.rip(lambda text: text)

        ocr_backend.image_to_data.assert_called()
        subs.append.assert_called_once()
        self.assertEqual(subs.append.call_args.args[0].text, "Hello")


def _subtitle_item(start: int, end: int) -> PgsSubtitleItem:
    item = PgsSubtitleItem.__new__(PgsSubtitleItem)
    item.media_path = MediaPath("fake.sup")
    item.start = start
    item.end = end
    item.image = Mock()
    item.x_offset = 0
    item.y_offset = 0
    return item


def _display_set(num_windows: int, x_offset: int | None, y_offset: int | None) -> Mock:
    display_set = Mock()
    display_set.pcs.presentation_timestamp = 0
    display_set.wds.num_windows = num_windows
    display_set.wds.x_offset = x_offset
    display_set.wds.y_offset = y_offset
    display_set.pcs.is_start.return_value = False
    return display_set


def _pgs_segment_bytes(data: bytes) -> bytes:
    size = len(data).to_bytes(2, byteorder="big")
    return b"PG" + b"\x00" * 8 + b"\x17" + size + data


if __name__ == "__main__":
    unittest.main()
