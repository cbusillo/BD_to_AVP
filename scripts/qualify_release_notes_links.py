from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import sys
import xml.etree.ElementTree as ET

from collections import Counter, defaultdict, deque
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit


SPARKLE_NAMESPACE = "http://www.andymatuschak.org/xml-namespaces/sparkle"
SPARKLE = f"{{{SPARKLE_NAMESPACE}}}"
REPOSITORY_HOST = "github.com"
REPOSITORY_PATH = "/cbusillo/BD_to_AVP"
LINK_CATEGORIES = ("issue", "pull_request", "comparison", "full_release", "other")
URL_PATTERN = re.compile(r"https://[^\s<>\"']+")
ISSUE_PATTERN = re.compile(rf"^{re.escape(REPOSITORY_PATH)}/issues/[1-9][0-9]*/?$")
PULL_REQUEST_PATTERN = re.compile(rf"^{re.escape(REPOSITORY_PATH)}/pull/[1-9][0-9]*/?$")
COMPARISON_PATTERN = re.compile(rf"^{re.escape(REPOSITORY_PATH)}/compare/[^/?#]+$")
FULL_RELEASE_PATTERN = re.compile(rf"^{re.escape(REPOSITORY_PATH)}/releases/tag/[^/?#]+$")


class ReleaseNotesLinkQualificationError(RuntimeError):
    pass


def _mapping(value: object, description: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ReleaseNotesLinkQualificationError(f"{description} must be a JSON object.")
    return cast(Mapping[str, Any], value)


def _sequence(value: object, description: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise ReleaseNotesLinkQualificationError(f"{description} must be a JSON array.")
    return value


def _string(value: object, description: str) -> str:
    if not isinstance(value, str) or not value:
        raise ReleaseNotesLinkQualificationError(f"{description} must be a non-empty string.")
    return value


def _load_json(path: Path, description: str) -> Mapping[str, Any]:
    try:
        return _mapping(json.loads(path.read_text(encoding="utf-8")), description)
    except OSError as error:
        raise ReleaseNotesLinkQualificationError(f"Unable to read {description} at {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise ReleaseNotesLinkQualificationError(f"Invalid JSON in {description} at {path}: {error}") from error


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _appcast_bytes(path: Path) -> bytes:
    data = path.read_bytes()
    if path.suffix == ".base64":
        try:
            return base64.b64decode(b"".join(data.split()), validate=True)
        except ValueError as error:
            raise ReleaseNotesLinkQualificationError(f"Invalid base64 appcast snapshot at {path}.") from error
    return data


def _normalized_markdown(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")


def _clean_url(value: str) -> str:
    return value.rstrip(".,;:!?)]}")


def extract_markdown_urls(markdown: str) -> list[str]:
    return [_clean_url(match.group(0)) for match in URL_PATTERN.finditer(markdown)]


def link_category(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc.lower() != REPOSITORY_HOST:
        return "other"
    if ISSUE_PATTERN.fullmatch(parsed.path):
        return "issue"
    if PULL_REQUEST_PATTERN.fullmatch(parsed.path):
        return "pull_request"
    if COMPARISON_PATTERN.fullmatch(parsed.path):
        return "comparison"
    if FULL_RELEASE_PATTERN.fullmatch(parsed.path):
        return "full_release"
    return "other"


def _appcast_item(appcast_path: Path, short_version: str, release_tag: str) -> tuple[str, str]:
    try:
        root = ET.fromstring(_appcast_bytes(appcast_path))
    except (OSError, ET.ParseError) as error:
        raise ReleaseNotesLinkQualificationError(f"Unable to read appcast at {appcast_path}: {error}") from error

    matches: list[ET.Element] = []
    for item in root.findall("./channel/item"):
        if item.findtext(f"{SPARKLE}shortVersionString") == short_version:
            matches.append(item)
    if len(matches) != 1:
        raise ReleaseNotesLinkQualificationError(
            f"Expected one appcast item for short version {short_version!r}; found {len(matches)}."
        )

    item = matches[0]
    description = item.find("description")
    if description is None or description.get(f"{SPARKLE}format") != "markdown" or description.text is None:
        raise ReleaseNotesLinkQualificationError("Matching appcast item requires embedded Markdown release notes.")
    full_release_url = _string(
        item.findtext(f"{SPARKLE}fullReleaseNotesLink"),
        "appcast full release notes URL",
    )
    expected_release_url = f"https://github.com/cbusillo/BD_to_AVP/releases/tag/{release_tag}"
    if full_release_url != expected_release_url:
        raise ReleaseNotesLinkQualificationError(
            f"Appcast full release notes URL must be {expected_release_url!r}, not {full_release_url!r}."
        )
    enclosure = item.find("enclosure")
    if enclosure is None or f"/releases/download/{release_tag}/" not in enclosure.get("url", ""):
        raise ReleaseNotesLinkQualificationError(
            "Matching appcast enclosure is not bound to the requested release tag."
        )
    return description.text, full_release_url


def _expected_links(release_markdown: str, full_release_url: str) -> list[dict[str, str]]:
    links = [
        {"url": url, "category": link_category(url), "source": "release_notes"}
        for url in extract_markdown_urls(release_markdown)
    ]
    links.append(
        {
            "url": full_release_url,
            "category": "full_release",
            "source": "appcast_full_release_link",
        }
    )
    return links


def _observed_links(observations: Mapping[str, Any], release_tag: str) -> list[dict[str, Any]]:
    if observations.get("schema_version") != 1:
        raise ReleaseNotesLinkQualificationError("Release-note link observations schema_version must be 1.")
    if observations.get("release_tag") != release_tag:
        raise ReleaseNotesLinkQualificationError("Release-note link observations target the wrong release tag.")

    links: list[dict[str, Any]] = []
    for index, raw_link in enumerate(_sequence(observations.get("links"), "observed release-note links")):
        link = _mapping(raw_link, f"observed release-note link {index}")
        url = _string(link.get("url"), f"observed release-note link {index} URL")
        opened_url = _string(link.get("opened_url"), f"observed release-note link {index} opened URL")
        role = _string(link.get("role"), f"observed release-note link {index} role")
        route = _string(link.get("route"), f"observed release-note link {index} route")
        for field in ("accessible", "activated"):
            if not isinstance(link.get(field), bool):
                raise ReleaseNotesLinkQualificationError(
                    f"Observed release-note link {index} {field} must be a boolean."
                )
        links.append(
            {
                "url": url,
                "opened_url": opened_url,
                "role": role,
                "route": route,
                "accessible": link["accessible"],
                "activated": link["activated"],
                "category": link_category(url),
            }
        )
    return links


def _pair_altered_links(
    missing: Counter[str],
    extra: Counter[str],
    failures: list[dict[str, Any]],
) -> None:
    missing_by_category: dict[str, deque[str]] = defaultdict(deque)
    for url, count in missing.items():
        missing_by_category[link_category(url)].extend([url] * count)

    for observed_url in list(extra):
        category = link_category(observed_url)
        while extra[observed_url] > 0 and missing_by_category[category]:
            expected_url = missing_by_category[category].popleft()
            failures.append(
                {
                    "type": "altered",
                    "category": category,
                    "expected_url": expected_url,
                    "observed_url": observed_url,
                }
            )
            missing[expected_url] -= 1
            extra[observed_url] -= 1


def qualify_release_notes_links(
    release_notes_path: Path,
    appcast_path: Path,
    observations_path: Path,
    *,
    short_version: str,
    release_tag: str,
) -> dict[str, Any]:
    try:
        release_markdown = release_notes_path.read_text(encoding="utf-8")
    except OSError as error:
        raise ReleaseNotesLinkQualificationError(
            f"Unable to read immutable release notes at {release_notes_path}: {error}"
        ) from error

    appcast_markdown, full_release_url = _appcast_item(appcast_path, short_version, release_tag)
    rendered_markdown = (
        f"{_normalized_markdown(release_markdown)}\n\n"
        f"[View the complete release and downloads on GitHub]({full_release_url})"
    )
    if _normalized_markdown(appcast_markdown) != rendered_markdown:
        raise ReleaseNotesLinkQualificationError(
            "Immutable release notes do not match the embedded Markdown in the appcast item."
        )

    expected = _expected_links(release_markdown, full_release_url)
    observed = _observed_links(_load_json(observations_path, "release-note link observations"), release_tag)
    expected_counts = Counter(link["url"] for link in expected)
    observed_counts = Counter(link["url"] for link in observed)
    missing = expected_counts - observed_counts
    extra = observed_counts - expected_counts
    failures: list[dict[str, Any]] = []

    _pair_altered_links(missing, extra, failures)
    for url, count in sorted(missing.items()):
        if count > 0:
            failures.append(
                {
                    "type": "missing",
                    "category": link_category(url),
                    "url": url,
                    "count": count,
                }
            )
    for url, count in sorted(extra.items()):
        if count <= 0:
            continue
        failure_type = "duplicated" if url in expected_counts else "altered"
        failures.append(
            {
                "type": failure_type,
                "category": link_category(url),
                "url": url,
                "count": count,
            }
        )

    for index, link in enumerate(observed):
        if link["role"] != "AXLink" or not link["accessible"]:
            failures.append(
                {
                    "type": "inaccessible",
                    "category": link["category"],
                    "url": link["url"],
                    "observation_index": index,
                }
            )
        if link["route"] != "external":
            failures.append(
                {
                    "type": "incorrect_route",
                    "category": link["category"],
                    "url": link["url"],
                    "route": link["route"],
                    "observation_index": index,
                }
            )
        if not link["activated"]:
            failures.append(
                {
                    "type": "not_activated",
                    "category": link["category"],
                    "url": link["url"],
                    "observation_index": index,
                }
            )
        if link["opened_url"] != link["url"]:
            failures.append(
                {
                    "type": "altered",
                    "category": link["category"],
                    "expected_url": link["url"],
                    "observed_url": link["opened_url"],
                    "observation_index": index,
                }
            )

    expected_categories = Counter(link["category"] for link in expected)
    observed_categories = Counter(link["category"] for link in observed)
    failed_categories = {failure.get("category") for failure in failures}
    category_results: dict[str, Any] = {}
    for category in LINK_CATEGORIES:
        expected_count = expected_categories[category]
        observed_count = observed_categories[category]
        if expected_count == 0 and observed_count == 0:
            reason = f"The immutable source notes contain no {category.replace('_', ' ')} URL."
            if category == "issue":
                reason = "The immutable RC3 notes contain no issue URL."
            category_results[category] = {
                "expected": 0,
                "observed": 0,
                "result": "not_applicable",
                "reason": reason,
            }
        else:
            category_results[category] = {
                "expected": expected_count,
                "observed": observed_count,
                "result": "failed" if category in failed_categories else "passed",
            }

    return {
        "schema_version": 1,
        "qualification_type": "native_sparkle_release_notes_links",
        "release_tag": release_tag,
        "short_version": short_version,
        "inputs": {
            "release_notes": release_notes_path.as_posix(),
            "release_notes_sha256": _sha256(release_notes_path),
            "appcast": appcast_path.as_posix(),
            "appcast_snapshot_sha256": _sha256(appcast_path),
            "appcast_sha256": hashlib.sha256(_appcast_bytes(appcast_path)).hexdigest(),
            "observations": observations_path.as_posix(),
            "observations_sha256": _sha256(observations_path),
        },
        "expected_links": expected,
        "observed_links": observed,
        "categories": category_results,
        "failures": failures,
        "passed": not failures,
    }


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Qualify native Sparkle release-note links against immutable source notes."
    )
    parser.add_argument("--release-notes", type=Path, required=True)
    parser.add_argument("--appcast", type=Path, required=True)
    parser.add_argument("--observations", type=Path, required=True)
    parser.add_argument("--short-version", required=True)
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    try:
        report = qualify_release_notes_links(
            args.release_notes,
            args.appcast,
            args.observations,
            short_version=args.short_version,
            release_tag=args.release_tag,
        )
    except ReleaseNotesLinkQualificationError as error:
        print(f"Release-note link qualification failed: {error}", file=sys.stderr)
        return 1

    if args.output is not None:
        _write_json(args.output, report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
