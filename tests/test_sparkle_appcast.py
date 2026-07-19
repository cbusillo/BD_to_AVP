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


def item(
    build: str,
    short_version: str,
    channel: str | None,
    *,
    release_tag: str | None = None,
    asset_name: str | None = None,
) -> sparkle_appcast.AppcastItem:
    tag = release_tag or sparkle_appcast._release_public_tag(short_version)
    dmg_name = asset_name or sparkle_appcast._release_asset_name(short_version)
    return sparkle_appcast.AppcastItem(
        build_version=build,
        short_version=short_version,
        channel=channel,
        download_url=f"https://github.com/cbusillo/BD_to_AVP/releases/download/{tag}/{dmg_name}",
        length=12345,
        signature=SIGNATURE,
        release_notes_markdown=f"Version {short_version} improves conversion reliability.",
        full_release_notes_url=f"https://github.com/cbusillo/BD_to_AVP/releases/tag/{tag}",
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
        self.assertEqual(items[0].findtext("title"), "Version 0.2.144-rc.1")
        self.assertIsNone(items[1].find(f"{sparkle_appcast.SPARKLE}channel"))
        description = items[0].find("description")
        if description is None:
            self.fail("RC appcast item is missing embedded release notes")
        self.assertEqual(description.get(f"{sparkle_appcast.SPARKLE}format"), "markdown")
        self.assertIn("Version 0.2.144rc1 improves conversion reliability.", description.text or "")
        self.assertIn(sparkle_appcast.FULL_RELEASE_LINK_LABEL, description.text or "")
        self.assertIsNone(items[0].find(f"{sparkle_appcast.SPARKLE}releaseNotesLink"))
        self.assertEqual(
            items[0].findtext(f"{sparkle_appcast.SPARKLE}fullReleaseNotesLink"),
            "https://github.com/cbusillo/BD_to_AVP/releases/tag/v0.2.144-rc.1",
        )
        enclosure = items[0].find("enclosure")
        if enclosure is None:
            self.fail("RC appcast item is missing its enclosure")
        self.assertEqual(
            enclosure.get(f"{sparkle_appcast.SPARKLE}edSignature"),
            SIGNATURE,
        )

    def test_adds_all_routes_with_exact_channels_and_global_build_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            feeds = [root / f"feed-{index}.xml" for index in range(6)]
            make_empty_feed(feeds[0])
            releases = (
                ("144", "1.2.2", None),
                ("145", "1.2.3a1", "alpha"),
                ("146", "1.2.3b1", "beta"),
                ("147", "1.2.3rc1", "rc"),
                ("148", "1.2.3", None),
            )
            for index, release_item in enumerate(releases, start=1):
                sparkle_appcast.append_item(feeds[index - 1], feeds[index], item(*release_item))

            sparkle_appcast.validate_appcast(feeds[-1])
            _, channel = sparkle_appcast.load_appcast(feeds[-1])
            items = channel.findall("item")

        self.assertEqual(
            [entry.findtext(f"{sparkle_appcast.SPARKLE}version") for entry in items],
            ["148", "147", "146", "145", "144"],
        )
        self.assertEqual(
            [entry.findtext(f"{sparkle_appcast.SPARKLE}channel") for entry in items],
            [None, "rc", "beta", "alpha", None],
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

    def test_rejects_semantically_older_release_with_new_build(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            empty_feed = root / "empty.xml"
            stable_feed = root / "stable.xml"
            make_empty_feed(empty_feed)
            sparkle_appcast.append_item(empty_feed, stable_feed, item("144", "0.2.143", None))

            with self.assertRaisesRegex(sparkle_appcast.AppcastError, "newer than published version"):
                sparkle_appcast.check_new_release(stable_feed, "145", "0.2.143rc4")

    def test_accepts_rc_to_rc_to_stable_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            empty_feed = root / "empty.xml"
            rc1_feed = root / "rc1.xml"
            rc2_feed = root / "rc2.xml"
            stable_feed = root / "stable.xml"
            make_empty_feed(empty_feed)

            sparkle_appcast.append_item(empty_feed, rc1_feed, item("144", "0.2.143rc4", "rc"))
            sparkle_appcast.append_item(rc1_feed, rc2_feed, item("145", "0.2.143rc5", "rc"))
            sparkle_appcast.append_item(rc2_feed, stable_feed, item("146", "0.2.143", None))

            sparkle_appcast.validate_appcast(stable_feed)

    def test_rejects_noncanonical_short_versions(self) -> None:
        for short_version in (
            "01.2.3",
            "1.02.3",
            "1.2.03",
            "1.2.3a0",
            "1.2.3b01",
            "1.2.3rc0",
            "1.2.3rc01",
        ):
            with (
                self.subTest(short_version=short_version),
                self.assertRaisesRegex(
                    sparkle_appcast.AppcastError,
                    "Invalid Sparkle short version",
                ),
            ):
                sparkle_appcast._release_channel(short_version)

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

    def test_rejects_alpha_beta_and_unknown_channel_mismatches(self) -> None:
        cases = (
            ("1.2.3a1", "beta"),
            ("1.2.3b1", "alpha"),
            ("1.2.3rc1", "beta"),
            ("1.2.3", "nightly"),
        )
        for short_version, channel in cases:
            with self.subTest(short_version=short_version, channel=channel), tempfile.TemporaryDirectory() as temp_dir:
                feed = Path(temp_dir) / "appcast.xml"
                make_empty_feed(feed)

                with self.assertRaises(sparkle_appcast.AppcastError):
                    sparkle_appcast.append_item(
                        feed,
                        Path(temp_dir) / "output.xml",
                        item("144", short_version, channel),
                    )

    def test_rejects_empty_or_duplicate_channel_elements(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            empty_feed = root / "empty.xml"
            feed = root / "appcast.xml"
            make_empty_feed(empty_feed)
            sparkle_appcast.append_item(empty_feed, feed, item("144", "1.2.3", None))

            tree = ET.parse(feed)
            appcast_item = tree.getroot().find("channel/item")
            if appcast_item is None:
                self.fail("Test appcast is missing its item")
            ET.SubElement(appcast_item, f"{sparkle_appcast.SPARKLE}channel")
            tree.write(feed, encoding="utf-8", xml_declaration=True)
            with self.assertRaisesRegex(sparkle_appcast.AppcastError, "Unsupported Sparkle channel"):
                sparkle_appcast.validate_appcast(feed)

            ET.SubElement(appcast_item, f"{sparkle_appcast.SPARKLE}channel").text = "rc"
            tree.write(feed, encoding="utf-8", xml_declaration=True)
            with self.assertRaisesRegex(sparkle_appcast.AppcastError, "at most one"):
                sparkle_appcast.validate_appcast(feed)

    def test_rejects_retired_beta_releases_before_appcast_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            feed = Path(temp_dir) / "appcast.xml"
            make_empty_feed(feed)

            for short_version in ("0.3.0b1", "0.3.0b2"):
                with (
                    self.subTest(short_version=short_version),
                    self.assertRaisesRegex(
                        sparkle_appcast.AppcastError,
                        "retired preview identity",
                    ),
                ):
                    sparkle_appcast.append_item(
                        feed,
                        Path(temp_dir) / "output.xml",
                        item("148", short_version, "beta"),
                    )

    def test_rejects_retired_tag_before_short_version_parsing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            empty_feed = root / "empty.xml"
            feed = root / "appcast.xml"
            make_empty_feed(empty_feed)
            sparkle_appcast.append_item(empty_feed, feed, item("148", "1.2.3", None))
            tree = ET.parse(feed)
            appcast_item = tree.getroot().find("channel/item")
            if appcast_item is None:
                self.fail("Test appcast is missing its item")
            short_version = appcast_item.find(f"{sparkle_appcast.SPARKLE}shortVersionString")
            enclosure = appcast_item.find("enclosure")
            if short_version is None or enclosure is None:
                self.fail("Test appcast is missing version metadata")
            short_version.text = "not-a-version"
            enclosure.set(
                "url",
                "https://github.com/cbusillo/BD_to_AVP/releases/download/"
                "v0.3.0-beta.1/3D-Blu-ray-to-Vision-Pro-0.3.0-beta.1.dmg",
            )
            tree.write(feed, encoding="utf-8", xml_declaration=True)

            with self.assertRaisesRegex(sparkle_appcast.AppcastError, "retired preview identity"):
                sparkle_appcast.validate_appcast(feed)

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
                "full_release_notes_url": "https://github.com/cbusillo/BD_to_AVP/releases/tag/v0.2.142",
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
                "full_release_notes_url": "https://github.com/cbusillo/BD_to_AVP/releases/tag/v0.2.142",
            }
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            feed = Path(temp_dir) / "appcast.xml"
            make_empty_feed(feed)

            with self.assertRaisesRegex(sparkle_appcast.AppcastError, "public tag mapped"):
                sparkle_appcast.append_item(feed, Path(temp_dir) / "output.xml", invalid_item)

    def test_rejects_dmg_name_using_internal_prerelease_syntax(self) -> None:
        invalid_item = item(
            "148",
            "1.2.3b1",
            "beta",
            asset_name="3D-Blu-ray-to-Vision-Pro-1.2.3b1.dmg",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            feed = Path(temp_dir) / "appcast.xml"
            make_empty_feed(feed)

            with self.assertRaisesRegex(sparkle_appcast.AppcastError, "public DMG name mapped"):
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

    def test_verifies_release_item_against_exact_asset(self) -> None:
        expected = item("144", "0.2.143", None)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            empty_feed = root / "empty.xml"
            feed = root / "appcast.xml"
            make_empty_feed(empty_feed)
            sparkle_appcast.append_item(empty_feed, feed, expected)

            sparkle_appcast.verify_release_item(
                feed,
                build_version=expected.build_version,
                short_version=expected.short_version,
                download_url=expected.download_url,
                length=expected.length,
                release_notes_markdown=expected.release_notes_markdown,
                full_release_notes_url=expected.full_release_notes_url,
            )

    def test_rejects_release_item_asset_mismatch(self) -> None:
        expected = item("144", "0.2.143", None)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            empty_feed = root / "empty.xml"
            feed = root / "appcast.xml"
            make_empty_feed(empty_feed)
            sparkle_appcast.append_item(empty_feed, feed, expected)

            with self.assertRaisesRegex(sparkle_appcast.AppcastError, "length"):
                sparkle_appcast.verify_release_item(
                    feed,
                    build_version=expected.build_version,
                    short_version=expected.short_version,
                    download_url=expected.download_url,
                    length=expected.length + 1,
                    release_notes_markdown=expected.release_notes_markdown,
                    full_release_notes_url=expected.full_release_notes_url,
                )

    def test_preserves_legacy_release_note_links_in_cumulative_feed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            empty_feed = root / "empty.xml"
            stable_feed = root / "stable.xml"
            rc_feed = root / "rc.xml"
            make_empty_feed(empty_feed)
            sparkle_appcast.append_item(empty_feed, stable_feed, item("144", "0.2.143", None))

            tree = ET.parse(stable_feed)
            appcast_item = tree.getroot().find("channel/item")
            if appcast_item is None:
                self.fail("Test appcast is missing its item")
            description = appcast_item.find("description")
            full_release_notes_link = appcast_item.find(f"{sparkle_appcast.SPARKLE}fullReleaseNotesLink")
            if description is None or full_release_notes_link is None:
                self.fail("Test appcast is missing embedded release-note metadata")
            release_url = full_release_notes_link.text
            appcast_item.remove(description)
            appcast_item.remove(full_release_notes_link)
            ET.SubElement(appcast_item, f"{sparkle_appcast.SPARKLE}releaseNotesLink").text = release_url
            tree.write(stable_feed, encoding="utf-8", xml_declaration=True)

            sparkle_appcast.validate_appcast(stable_feed)
            sparkle_appcast.append_item(stable_feed, rc_feed, item("145", "0.2.144rc1", "rc"))
            sparkle_appcast.validate_appcast(rc_feed)

    def test_accepts_immutable_compact_rc_history_without_emitting_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            empty_feed = root / "empty.xml"
            feed = root / "appcast.xml"
            make_empty_feed(empty_feed)
            sparkle_appcast.append_item(empty_feed, feed, item("145", "0.2.143rc5", "rc"))
            feed_text = feed.read_text(encoding="utf-8")
            feed_text = feed_text.replace(
                "3D-Blu-ray-to-Vision-Pro-0.2.143-rc.5.dmg",
                "3D-Blu-ray-to-Vision-Pro-0.2.143rc5.dmg",
            ).replace("v0.2.143-rc.5", "v0.2.143rc5")
            feed.write_text(feed_text, encoding="utf-8")

            sparkle_appcast.validate_appcast(feed)
            sparkle_appcast.validate_release_tag_snapshot(feed, "v0.2.143rc5")

            with self.assertRaisesRegex(sparkle_appcast.AppcastError, "public tag mapped"):
                sparkle_appcast.append_item(
                    empty_feed,
                    root / "new.xml",
                    item(
                        "145",
                        "0.2.143rc5",
                        "rc",
                        release_tag="v0.2.143rc5",
                        asset_name="3D-Blu-ray-to-Vision-Pro-0.2.143rc5.dmg",
                    ),
                )

    def test_rejects_mixed_canonical_and_legacy_rc_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            empty_feed = root / "empty.xml"
            feed = root / "appcast.xml"
            make_empty_feed(empty_feed)
            sparkle_appcast.append_item(empty_feed, feed, item("145", "0.2.143rc5", "rc"))
            feed.write_text(
                feed.read_text(encoding="utf-8").replace(
                    "3D-Blu-ray-to-Vision-Pro-0.2.143-rc.5.dmg",
                    "3D-Blu-ray-to-Vision-Pro-0.2.143rc5.dmg",
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(sparkle_appcast.AppcastError, "public DMG name mapped"):
                sparkle_appcast.validate_appcast(feed)

    def test_rejects_ambiguous_embedded_and_external_release_notes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            empty_feed = root / "empty.xml"
            feed = root / "appcast.xml"
            make_empty_feed(empty_feed)
            sparkle_appcast.append_item(empty_feed, feed, item("144", "0.2.143", None))
            tree = ET.parse(feed)
            appcast_item = tree.getroot().find("channel/item")
            if appcast_item is None:
                self.fail("Test appcast is missing its item")
            ET.SubElement(appcast_item, f"{sparkle_appcast.SPARKLE}releaseNotesLink").text = appcast_item.findtext(
                f"{sparkle_appcast.SPARKLE}fullReleaseNotesLink"
            )
            tree.write(feed, encoding="utf-8", xml_declaration=True)

            with self.assertRaisesRegex(sparkle_appcast.AppcastError, "must not also use"):
                sparkle_appcast.validate_appcast(feed)

    def test_rejects_invalid_or_oversized_embedded_markdown(self) -> None:
        release_url = "https://github.com/cbusillo/BD_to_AVP/releases/tag/v0.2.143"
        with self.assertRaisesRegex(sparkle_appcast.AppcastError, "must not be empty"):
            sparkle_appcast.render_release_notes(" \n", release_url)
        with self.assertRaisesRegex(sparkle_appcast.AppcastError, "invalid in XML"):
            sparkle_appcast.render_release_notes("bad\x00notes", release_url)
        with self.assertRaisesRegex(sparkle_appcast.AppcastError, "must not exceed"):
            sparkle_appcast.render_release_notes("x" * sparkle_appcast.MAX_RELEASE_NOTES_BYTES, release_url)

    def test_verify_release_rejects_changed_draft_body(self) -> None:
        expected = item("144", "0.2.143", None)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            empty_feed = root / "empty.xml"
            feed = root / "appcast.xml"
            make_empty_feed(empty_feed)
            sparkle_appcast.append_item(empty_feed, feed, expected)

            with self.assertRaisesRegex(sparkle_appcast.AppcastError, "do not match the draft release body"):
                sparkle_appcast.verify_release_item(
                    feed,
                    build_version=expected.build_version,
                    short_version=expected.short_version,
                    download_url=expected.download_url,
                    length=expected.length,
                    release_notes_markdown="Changed after appcast construction.",
                    full_release_notes_url=expected.full_release_notes_url,
                )

    def test_validates_empty_emergency_feed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            feed = Path(temp_dir) / "appcast.xml"
            make_empty_feed(feed)

            sparkle_appcast.validate_empty_appcast(feed)

    def test_snapshot_requires_matching_newest_release(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            empty_feed = root / "empty.xml"
            stable_feed = root / "stable.xml"
            rc_feed = root / "rc.xml"
            make_empty_feed(empty_feed)
            sparkle_appcast.append_item(empty_feed, stable_feed, item("144", "0.2.143", None))
            sparkle_appcast.append_item(stable_feed, rc_feed, item("145", "0.2.144rc1", "rc"))

            sparkle_appcast.validate_release_snapshot(rc_feed, "0.2.144rc1")
            sparkle_appcast.validate_release_tag_snapshot(rc_feed, "v0.2.144-rc.1")
            with self.assertRaisesRegex(sparkle_appcast.AppcastError, "must start"):
                sparkle_appcast.validate_release_snapshot(rc_feed, "0.2.143")
            with self.assertRaisesRegex(sparkle_appcast.AppcastError, "must start"):
                sparkle_appcast.validate_release_tag_snapshot(rc_feed, "v0.2.143")


if __name__ == "__main__":
    unittest.main()
