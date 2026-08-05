import json
import tempfile
import unittest

from pathlib import Path

from scripts.qualify_release_notes_links import (
    ReleaseNotesLinkQualificationError,
    qualify_release_notes_links,
)


FULL_RELEASE_URL = "https://github.com/cbusillo/BD_to_AVP/releases/tag/v0.3.0-rc.3"


class ReleaseNotesLinkQualificationTests(unittest.TestCase):
    def write_fixture(
        self,
        root: Path,
        *,
        notes: str,
        links: list[dict[str, object]],
        appcast_notes: str | None = None,
    ) -> tuple[Path, Path, Path]:
        notes_path = root / "release-notes.md"
        appcast_path = root / "appcast.xml"
        observations_path = root / "observations.json"
        notes_path.write_text(notes, encoding="utf-8")
        appcast_body = appcast_notes if appcast_notes is not None else notes
        rendered_appcast_body = (
            f"{appcast_body.rstrip()}\n\n[View the complete release and downloads on GitHub]({FULL_RELEASE_URL})"
        )
        appcast_path.write_text(
            """<?xml version="1.0" encoding="utf-8"?>
<rss xmlns:sparkle="http://www.andymatuschak.org/xml-namespaces/sparkle" version="2.0">
  <channel>
    <item>
      <sparkle:shortVersionString>0.3.0rc3</sparkle:shortVersionString>
      <description sparkle:format="markdown"><![CDATA[{notes}]]></description>
      <sparkle:fullReleaseNotesLink>{full_release_url}</sparkle:fullReleaseNotesLink>
      <enclosure url="https://github.com/cbusillo/BD_to_AVP/releases/download/v0.3.0-rc.3/app.dmg" />
    </item>
  </channel>
</rss>
""".format(notes=rendered_appcast_body, full_release_url=FULL_RELEASE_URL),
            encoding="utf-8",
        )
        observations_path.write_text(
            json.dumps({"schema_version": 1, "release_tag": "v0.3.0-rc.3", "links": links}),
            encoding="utf-8",
        )
        return notes_path, appcast_path, observations_path

    def observation(self, url: str, **overrides: object) -> dict[str, object]:
        observation: dict[str, object] = {
            "url": url,
            "opened_url": url,
            "role": "AXLink",
            "route": "external",
            "accessible": True,
            "activated": True,
        }
        observation.update(overrides)
        return observation

    def qualify(self, notes: str, links: list[dict[str, object]], *, appcast_notes: str | None = None) -> dict:
        with tempfile.TemporaryDirectory() as temporary_directory:
            paths = self.write_fixture(
                Path(temporary_directory),
                notes=notes,
                links=links,
                appcast_notes=appcast_notes,
            )
            return qualify_release_notes_links(
                *paths,
                short_version="0.3.0rc3",
                release_tag="v0.3.0-rc.3",
            )

    def test_absent_issue_link_is_not_applicable(self) -> None:
        pull_request = "https://github.com/cbusillo/BD_to_AVP/pull/466"
        comparison = "https://github.com/cbusillo/BD_to_AVP/compare/v0.3.0-rc.2...v0.3.0-rc.3"
        report = self.qualify(
            f"PR {pull_request}\n\nComparison {comparison}",
            [
                self.observation(pull_request),
                self.observation(comparison),
                self.observation(FULL_RELEASE_URL),
            ],
        )

        self.assertTrue(report["passed"])
        self.assertEqual(
            report["categories"]["issue"],
            {
                "expected": 0,
                "observed": 0,
                "result": "not_applicable",
                "reason": "The immutable RC3 notes contain no issue URL.",
            },
        )

    def test_all_present_link_categories_pass(self) -> None:
        issue = "https://github.com/cbusillo/BD_to_AVP/issues/458"
        pull_request = "https://github.com/cbusillo/BD_to_AVP/pull/466"
        comparison = "https://github.com/cbusillo/BD_to_AVP/compare/v0.3.0-rc.2...v0.3.0-rc.3"
        report = self.qualify(
            "\n".join((issue, pull_request, comparison)),
            [
                self.observation(issue),
                self.observation(pull_request),
                self.observation(comparison),
                self.observation(FULL_RELEASE_URL),
            ],
        )

        self.assertTrue(report["passed"])
        self.assertTrue(
            all(
                report["categories"][category]["result"] == "passed"
                for category in (
                    "issue",
                    "pull_request",
                    "comparison",
                    "full_release",
                )
            )
        )

    def test_missing_expected_link_fails(self) -> None:
        issue = "https://github.com/cbusillo/BD_to_AVP/issues/458"
        report = self.qualify(issue, [self.observation(FULL_RELEASE_URL)])

        self.assertFalse(report["passed"])
        self.assertEqual(report["failures"][0]["type"], "missing")

    def test_altered_link_fails(self) -> None:
        expected = "https://github.com/cbusillo/BD_to_AVP/pull/466"
        altered = "https://github.com/cbusillo/BD_to_AVP/pull/467"
        report = self.qualify(
            expected,
            [self.observation(altered), self.observation(FULL_RELEASE_URL)],
        )

        self.assertFalse(report["passed"])
        self.assertIn("altered", {failure["type"] for failure in report["failures"]})

    def test_inaccessible_link_fails(self) -> None:
        pull_request = "https://github.com/cbusillo/BD_to_AVP/pull/466"
        report = self.qualify(
            pull_request,
            [self.observation(pull_request, accessible=False), self.observation(FULL_RELEASE_URL)],
        )

        self.assertFalse(report["passed"])
        self.assertIn("inaccessible", {failure["type"] for failure in report["failures"]})

    def test_unexpected_duplicate_fails(self) -> None:
        pull_request = "https://github.com/cbusillo/BD_to_AVP/pull/466"
        report = self.qualify(
            pull_request,
            [
                self.observation(pull_request),
                self.observation(pull_request),
                self.observation(FULL_RELEASE_URL),
            ],
        )

        self.assertFalse(report["passed"])
        self.assertIn("duplicated", {failure["type"] for failure in report["failures"]})

    def test_source_multiplicity_requires_matching_observations(self) -> None:
        pull_request = "https://github.com/cbusillo/BD_to_AVP/pull/466"
        report = self.qualify(
            f"{pull_request}\n{pull_request}",
            [
                self.observation(pull_request),
                self.observation(pull_request),
                self.observation(FULL_RELEASE_URL),
            ],
        )

        self.assertTrue(report["passed"])

    def test_incorrect_route_fails(self) -> None:
        pull_request = "https://github.com/cbusillo/BD_to_AVP/pull/466"
        report = self.qualify(
            pull_request,
            [self.observation(pull_request, route="embedded"), self.observation(FULL_RELEASE_URL)],
        )

        self.assertFalse(report["passed"])
        self.assertIn("incorrect_route", {failure["type"] for failure in report["failures"]})

    def test_opened_url_must_match_source_url(self) -> None:
        pull_request = "https://github.com/cbusillo/BD_to_AVP/pull/466"
        report = self.qualify(
            pull_request,
            [
                self.observation(
                    pull_request,
                    opened_url="https://github.com/cbusillo/BD_to_AVP/issues/466",
                ),
                self.observation(FULL_RELEASE_URL),
            ],
        )

        self.assertFalse(report["passed"])
        self.assertIn("altered", {failure["type"] for failure in report["failures"]})

    def test_release_notes_must_match_appcast_markdown(self) -> None:
        with self.assertRaisesRegex(
            ReleaseNotesLinkQualificationError,
            "do not match the embedded Markdown",
        ):
            self.qualify("release body", [self.observation(FULL_RELEASE_URL)], appcast_notes="altered body")


if __name__ == "__main__":
    unittest.main()
