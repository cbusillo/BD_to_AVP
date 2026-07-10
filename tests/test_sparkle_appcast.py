import base64
import tempfile
import unittest
import xml.etree.ElementTree as ET

from datetime import datetime, timezone
from pathlib import Path

from scripts import sparkle_appcast


SIGNATURE = base64.b64encode(b"s" * 64).decode("ascii")


def make_empty_feed(path: Path) -> None:
    path.write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"
     xmlns:sparkle="http://www.andymatuschak.org/xml-namespaces/sparkle"
     xmlns:dc="http://purl.org/dc/elements/1.1/">
  <channel>
    <title>3D Blu-ray to Vision Pro Updates</title>
    <link>https://github.com/cbusillo/BD_to_AVP</link>
    <description>Updates.</description>
    <language>en</language>
  </channel>
</rss>
""",
        encoding="utf-8",
    )


def item(build: str, short_version: str, channel: str | None) -> sparkle_appcast.AppcastItem:
    tag = f"v{short_version}"
    return sparkle_appcast.AppcastItem(
        build_version=build,
        short_version=short_version,
        channel=channel,
        download_url=f"https://github.com/cbusillo/BD_to_AVP/releases/download/{tag}/BD_to_AVP-{short_version}.dmg",
        length=12345,
        signature=SIGNATURE,
        release_notes_url=f"https://github.com/cbusillo/BD_to_AVP/releases/tag/{tag}",
        minimum_system_version="11.0",
        published_at=datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc),
    )


class SparkleAppcastTests(unittest.TestCase):
    def test_adds_stable_and_rc_items_in_descending_build_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            empty_feed = root / "empty.xml"
            stable_feed = root / "stable.xml"
            rc_feed = root / "rc.xml"
            make_empty_feed(empty_feed)

            sparkle_appcast.append_item(empty_feed, stable_feed, item("144", "0.2.143", None))
            sparkle_appcast.append_item(stable_feed, rc_feed, item("145", "0.2.144rc1", "rc"))
            sparkle_appcast.validate_appcast(rc_feed)
            _, channel = sparkle_appcast.load_appcast(rc_feed)
            items = channel.findall("item")

        self.assertEqual(
            [entry.findtext(f"{sparkle_appcast.SPARKLE}version") for entry in items],
            ["145", "144"],
        )
        self.assertEqual(items[0].findtext(f"{sparkle_appcast.SPARKLE}channel"), "rc")
        self.assertIsNone(items[1].find(f"{sparkle_appcast.SPARKLE}channel"))
        enclosure = items[0].find("enclosure")
        if enclosure is None:
            self.fail("RC appcast item is missing its enclosure")
        self.assertEqual(
            enclosure.get(f"{sparkle_appcast.SPARKLE}edSignature"),
            SIGNATURE,
        )

    def test_rejects_non_monotonic_build(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            empty_feed = root / "empty.xml"
            stable_feed = root / "stable.xml"
            make_empty_feed(empty_feed)
            sparkle_appcast.append_item(empty_feed, stable_feed, item("144", "0.2.143", None))

            with self.assertRaisesRegex(sparkle_appcast.AppcastError, "newer than published build"):
                sparkle_appcast.append_item(stable_feed, root / "duplicate.xml", item("144", "0.2.144rc1", "rc"))

    def test_rejects_duplicate_short_version_with_new_build(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            empty_feed = root / "empty.xml"
            stable_feed = root / "stable.xml"
            make_empty_feed(empty_feed)
            sparkle_appcast.append_item(empty_feed, stable_feed, item("144", "0.2.143", None))

            with self.assertRaisesRegex(sparkle_appcast.AppcastError, "already published"):
                sparkle_appcast.check_new_release(stable_feed, "145", "0.2.143")

    def test_rejects_noncanonical_build_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            feed = Path(temp_dir) / "appcast.xml"
            make_empty_feed(feed)

            for build_version in ("0", "00", "01"):
                with self.subTest(build_version=build_version):
                    with self.assertRaisesRegex(sparkle_appcast.AppcastError, "canonical numeric"):
                        sparkle_appcast.check_new_build(feed, build_version)

    def test_rejects_stable_item_with_rc_version(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            feed = Path(temp_dir) / "appcast.xml"
            make_empty_feed(feed)

            with self.assertRaisesRegex(sparkle_appcast.AppcastError, "channel and short version disagree"):
                sparkle_appcast.append_item(feed, Path(temp_dir) / "output.xml", item("144", "0.2.144rc1", None))

    def test_rejects_rc_channel_without_rc_version(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            feed = Path(temp_dir) / "appcast.xml"
            make_empty_feed(feed)

            with self.assertRaisesRegex(sparkle_appcast.AppcastError, "channel and short version disagree"):
                sparkle_appcast.append_item(feed, Path(temp_dir) / "output.xml", item("144", "0.2.144", "rc"))

    def test_rejects_delta_enclosures(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            feed = Path(temp_dir) / "appcast.xml"
            make_empty_feed(feed)
            tree = ET.parse(feed)
            channel = tree.getroot().find("channel")
            if channel is None:
                self.fail("Test appcast is missing its channel")
            ET.SubElement(channel, f"{sparkle_appcast.SPARKLE}deltas")
            tree.write(feed, encoding="utf-8", xml_declaration=True)

            with self.assertRaisesRegex(sparkle_appcast.AppcastError, "Delta updates"):
                sparkle_appcast.validate_appcast(feed)

    def test_rejects_non_tag_qualified_download_url(self) -> None:
        invalid_item = sparkle_appcast.AppcastItem(
            **{
                **item("144", "0.2.143", None).__dict__,
                "download_url": "https://github.com/cbusillo/BD_to_AVP/releases/latest/download/app.dmg",
            }
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            feed = Path(temp_dir) / "appcast.xml"
            make_empty_feed(feed)

            with self.assertRaisesRegex(sparkle_appcast.AppcastError, "tag-qualified"):
                sparkle_appcast.append_item(feed, Path(temp_dir) / "output.xml", invalid_item)

    def test_rejects_release_notes_for_different_tag(self) -> None:
        invalid_item = sparkle_appcast.AppcastItem(
            **{
                **item("144", "0.2.143", None).__dict__,
                "release_notes_url": "https://github.com/cbusillo/BD_to_AVP/releases/tag/v0.2.142",
            }
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            feed = Path(temp_dir) / "appcast.xml"
            make_empty_feed(feed)

            with self.assertRaisesRegex(sparkle_appcast.AppcastError, "same release tag"):
                sparkle_appcast.append_item(feed, Path(temp_dir) / "output.xml", invalid_item)

    def test_rejects_urls_not_derived_from_short_version(self) -> None:
        invalid_item = sparkle_appcast.AppcastItem(
            **{
                **item("144", "0.2.143", None).__dict__,
                "download_url": "https://github.com/cbusillo/BD_to_AVP/releases/download/v0.2.142/app.dmg",
                "release_notes_url": "https://github.com/cbusillo/BD_to_AVP/releases/tag/v0.2.142",
            }
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            feed = Path(temp_dir) / "appcast.xml"
            make_empty_feed(feed)

            with self.assertRaisesRegex(sparkle_appcast.AppcastError, "derived from"):
                sparkle_appcast.append_item(feed, Path(temp_dir) / "output.xml", invalid_item)

    def test_rejects_non_dmg_enclosure(self) -> None:
        invalid_item = sparkle_appcast.AppcastItem(
            **{
                **item("144", "0.2.143", None).__dict__,
                "download_url": "https://github.com/cbusillo/BD_to_AVP/releases/download/v0.2.143/app.zip",
            }
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            feed = Path(temp_dir) / "appcast.xml"
            make_empty_feed(feed)

            with self.assertRaisesRegex(sparkle_appcast.AppcastError, "DMG asset"):
                sparkle_appcast.append_item(feed, Path(temp_dir) / "output.xml", invalid_item)

    def test_rejects_invalid_signature_length(self) -> None:
        invalid_item = sparkle_appcast.AppcastItem(
            **{
                **item("144", "0.2.143", None).__dict__,
                "signature": base64.b64encode(b"short").decode("ascii"),
            }
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            feed = Path(temp_dir) / "appcast.xml"
            make_empty_feed(feed)

            with self.assertRaisesRegex(sparkle_appcast.AppcastError, "64-byte"):
                sparkle_appcast.append_item(feed, Path(temp_dir) / "output.xml", invalid_item)


if __name__ == "__main__":
    unittest.main()
