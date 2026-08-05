import unittest
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, Mock, patch

import numpy as np

from bd_to_avp.process_runner import ProcessCancelled
from bd_to_avp.vendor.pgsrip.core import rip_pgs
from bd_to_avp.vendor.pgsrip.media_path import MediaPath
from bd_to_avp.vendor.pgsrip.media import PgsSubtitleItem
from bd_to_avp.vendor.pgsrip.mkv import Mkv, MkvTrack
from bd_to_avp.vendor.pgsrip.options import Options
from bd_to_avp.vendor.pgsrip.pgs import Palette, PgsImage, PgsReader, WindowDefinitionSegment
from bd_to_avp.vendor.pgsrip.ripper import PgsToSrtRipper
from bd_to_avp.vendor.pgsrip.utils import from_hex, to_time


# ---------------------------------------------------------------------------
# Helpers for building minimal RLE-encoded PGS image data
# ---------------------------------------------------------------------------
def _rle_single_pixel(color_index: int) -> bytes:
    """Encode a single pixel with the given palette index (non-zero color)."""
    return bytes([color_index])


def _rle_run(length: int, color_index: int) -> bytes:
    """Encode a run of `length` pixels of a given color.

    Uses the 4-byte escape form when color_index is non-zero and length > 1,
    and the 3-byte transparent-run form when color_index is 0.
    """
    if color_index == 0:
        if length < 64:
            return bytes([0x00, length])
        return bytes([0x00, 0x40 | (length >> 8), length & 0xFF])
    # non-zero color, multi-pixel run: 0x00, 0xC0 | (len >> 8), len & 0xFF, color
    if length <= 0x3FFF:
        return bytes([0x00, 0xC0 | (length >> 8), length & 0xFF, color_index])
    raise ValueError(f"Run length {length} too large for single RLE sequence")


def _rle_end_of_line() -> bytes:
    """Encode an end-of-line marker (0x00 0x00)."""
    return bytes([0x00, 0x00])


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


class PgsCancellationTests(unittest.TestCase):
    def test_process_cancellation_escapes_vendor_rip_error_handling(self) -> None:
        cancellation = ProcessCancelled("cancelled")
        pgs = MagicMock()
        pgs.__enter__.side_effect = cancellation

        with self.assertRaises(ProcessCancelled) as context:
            rip_pgs(pgs, Options())

        self.assertIs(context.exception, cancellation)


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


class PgsImageRenderTests(unittest.TestCase):
    def test_binary_render_composes_visible_palette_entries_for_ocr(self) -> None:
        palettes = [Palette(0, 0, 0, 0)] * 256
        palettes[1] = Palette(17, 128, 128, 255)
        palettes[2] = Palette(126, 128, 128, 255)

        self.assertEqual(PgsImage.get_color(palettes[0], binary=True), [255])
        self.assertEqual(PgsImage.get_color(palettes[1], binary=True), [17])
        self.assertEqual(PgsImage.get_color(palettes[2], binary=True), [126])


class PgsReaderTests(unittest.TestCase):
    def test_reads_many_segments_without_tail_slicing(self) -> None:
        data = b"".join(_pgs_segment_bytes(b"") for _ in range(20_000))

        segments = list(PgsReader.read_segments(data, MediaPath("many.sup")))

        self.assertEqual(len(segments), 20_000)

    def test_skips_unknown_segment_and_continues(self) -> None:
        data = _pgs_segment_bytes(b"") + _raw_pgs_segment_bytes(0x99, b"") + _pgs_segment_bytes(b"")

        segments = list(PgsReader.read_segments(data, MediaPath("unknown.sup")))

        self.assertEqual(len(segments), 2)

    def test_truncated_segment_is_ignored(self) -> None:
        data = _raw_pgs_segment_bytes(0x17, b"payload", declared_size=20)

        self.assertEqual(list(PgsReader.read_segments(data, MediaPath("truncated.sup"))), [])


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


class PgsRleBoundsTests(unittest.TestCase):
    """Phase 2 — RLE expansion must not exceed ODS-declared pixel count."""

    def _make_palettes(self) -> list[Palette]:
        palettes = [Palette(0, 0, 0, 0)] * 256
        palettes[1] = Palette(200, 128, 128, 255)
        return palettes

    def test_rle_within_declared_bounds_decodes_successfully(self) -> None:
        """Valid 2×2 image (4 pixels) must decode without error."""
        palettes = self._make_palettes()
        # 2 pixels on row 1, end-of-line, 2 pixels on row 2, end-of-line
        rle = _rle_run(2, 1) + _rle_end_of_line() + _rle_run(2, 1) + _rle_end_of_line()
        image = PgsImage(rle, palettes, width=2, height=2)
        data = image.data  # must not raise
        self.assertEqual(data.shape, (2, 2))

    def test_rle_exceeding_declared_bounds_raises_value_error(self) -> None:
        """ODS declares 2×2 (4 pixels) but RLE expands to 5+ — must raise ValueError."""
        palettes = self._make_palettes()
        # 5 pixels on one row, then end-of-line
        rle = _rle_run(5, 1) + _rle_end_of_line()
        image = PgsImage(rle, palettes, width=2, height=2)
        with self.assertRaises(ValueError):
            _ = image.data

    def test_rle_with_no_declared_bounds_decodes_unbounded(self) -> None:
        """When width/height are not provided, decoding must not raise even for large runs."""
        palettes = self._make_palettes()
        rle = _rle_run(100, 1) + _rle_end_of_line()
        image = PgsImage(rle, palettes)  # no width/height
        data = image.data  # must not raise
        self.assertEqual(data.shape[1], 100)

    def test_decode_rle_image_max_pixels_parameter_raises_on_overflow(self) -> None:
        """decode_rle_image raises ValueError when max_pixels is exceeded."""
        palettes = self._make_palettes()
        rle = _rle_run(10, 1) + _rle_end_of_line()
        with self.assertRaises(ValueError):
            PgsImage.decode_rle_image(rle, palettes, max_pixels=5)

    def test_decode_rle_image_max_pixels_none_allows_large_expansion(self) -> None:
        """decode_rle_image with max_pixels=None behaves as before."""
        palettes = self._make_palettes()
        rle = _rle_run(50, 1) + _rle_end_of_line()
        result = PgsImage.decode_rle_image(rle, palettes, max_pixels=None)
        self.assertEqual(result.shape[1], 50)


class PgsMalformedItemIsolationTests(unittest.TestCase):
    """Phase 2 — a single malformed display item must not kill the whole track."""

    def _make_good_display_set(self, is_start: bool = True) -> Mock:
        """Return a minimal well-formed display set mock with valid image data."""
        palettes = [Palette(0, 0, 0, 0)] * 256
        palettes[1] = Palette(200, 128, 128, 255)
        pds = Mock()
        pds.palettes = palettes

        ods = Mock()
        # 4 pixels declared, 4 pixels in RLE — valid
        ods.img_data = _rle_run(2, 1) + _rle_end_of_line() + _rle_run(2, 1) + _rle_end_of_line()
        ods.width = 2
        ods.height = 2

        ds = Mock()
        ds.pcs.presentation_timestamp = 1000
        ds.pcs.is_start.return_value = is_start
        ds.pds_segments = [pds]
        ds.ods_segments = [ods]
        ds.wds.num_windows = 1
        ds.wds.x_offset = 10
        ds.wds.y_offset = 20
        return ds

    def _make_malformed_display_set(self) -> Mock:
        """Return a display set mock whose RLE data expands beyond declared dims."""
        palettes = [Palette(0, 0, 0, 0)] * 256
        palettes[1] = Palette(200, 128, 128, 255)
        pds = Mock()
        pds.palettes = palettes

        ods = Mock()
        # Declares 2×2 (4 pixels) but RLE encodes 20 pixels — will raise ValueError
        ods.img_data = _rle_run(20, 1) + _rle_end_of_line()
        ods.width = 2
        ods.height = 2

        ds = Mock()
        ds.pcs.presentation_timestamp = 5000
        ds.pcs.is_start.return_value = True
        ds.pds_segments = [pds]
        ds.ods_segments = [ods]
        ds.wds.num_windows = 1
        ds.wds.x_offset = 10
        ds.wds.y_offset = 20
        return ds

    def test_generate_image_returns_none_for_malformed_rle(self) -> None:
        """generate_image returns None (and logs) when RLE overflows declared bounds."""
        bad_ds = self._make_malformed_display_set()
        result = PgsSubtitleItem.generate_image([bad_ds])
        self.assertIsNone(result)

    def test_generate_image_returns_image_for_valid_rle(self) -> None:
        """generate_image returns a PgsImage for well-formed data."""
        good_ds = self._make_good_display_set(is_start=True)
        result = PgsSubtitleItem.generate_image([good_ds])
        self.assertIsNotNone(result)
        # Accessing .data must not raise
        _ = result.data

    def test_create_items_skips_malformed_item_and_keeps_good_ones(self) -> None:
        """create_items produces items only for display sets whose image decoded OK.

        The malformed item's image is None, which auto_fix rejects; the good
        item must still appear in the result.
        """
        media_path = MediaPath("fake.sup")

        good_start = self._make_good_display_set(is_start=True)
        # Need a second ds that is NOT a start so it's grouped with good_start
        good_end = Mock()
        good_end.pcs.presentation_timestamp = 2000
        good_end.pcs.is_start.return_value = False
        good_end.pds_segments = []
        good_end.ods_segments = []
        good_end.wds.num_windows = 0

        bad_start = self._make_malformed_display_set()
        bad_end = Mock()
        bad_end.pcs.presentation_timestamp = 6000
        bad_end.pcs.is_start.return_value = False
        bad_end.pds_segments = []
        bad_end.ods_segments = []
        bad_end.wds.num_windows = 0

        # Sequence: good subtitle [good_start, good_end], bad subtitle [bad_start, bad_end]
        display_sets = [good_start, good_end, bad_start, bad_end]
        items = PgsSubtitleItem.create_items(media_path, display_sets)

        # The good subtitle item passes auto_fix; the bad one (image=None) fails it
        self.assertEqual(len(items), 1, f"Expected 1 item, got {len(items)}")
        self.assertIsNotNone(items[0].image)


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
    return _raw_pgs_segment_bytes(0x17, data)


def _raw_pgs_segment_bytes(segment_type: int, data: bytes, declared_size: int | None = None) -> bytes:
    size = (len(data) if declared_size is None else declared_size).to_bytes(2, byteorder="big")
    return b"PG" + b"\x00" * 8 + bytes([segment_type]) + size + data


if __name__ == "__main__":
    unittest.main()
