import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from bd_to_avp.vendor.pgsrip.media_path import MediaPath
from bd_to_avp.vendor.pgsrip.media import PgsSubtitleItem
from bd_to_avp.vendor.pgsrip.mkv import Mkv, MkvTrack
from bd_to_avp.vendor.pgsrip.options import Options
from bd_to_avp.vendor.pgsrip.ripper import PgsToSrtRipper


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

        with patch("bd_to_avp.vendor.pgsrip.mkv.MkvPgs") as mkv_pgs:
            mkv_pgs.return_value.matches.return_value = True
            list(mkv.get_pgs_medias(Options(one_per_lang=False)))

        selected_track_ids = [call.args[1] for call in mkv_pgs.call_args_list]
        self.assertEqual(selected_track_ids, [2, 1])


class PgsSubtitleItemTimestampTests(unittest.TestCase):
    def test_zero_valued_next_start_is_available_for_end_repair(self) -> None:
        item = _subtitle_item(start=-5000, end=-5000)
        next_item = _subtitle_item(start=0, end=12000)

        self.assertTrue(item.auto_fix(next_item))

        self.assertEqual(item.end, -1)


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


def _subtitle_item(start: int, end: int) -> PgsSubtitleItem:
    item = PgsSubtitleItem.__new__(PgsSubtitleItem)
    item.media_path = MediaPath("fake.sup")
    item.start = start
    item.end = end
    item.image = Mock()
    item.x_offset = 0
    item.y_offset = 0
    return item


if __name__ == "__main__":
    unittest.main()
