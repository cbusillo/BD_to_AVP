from __future__ import annotations

import argparse
import base64
import binascii
import re
import xml.etree.ElementTree as ET

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse


SPARKLE_NAMESPACE = "http://www.andymatuschak.org/xml-namespaces/sparkle"
DC_NAMESPACE = "http://purl.org/dc/elements/1.1/"
REPOSITORY_PATH = "/cbusillo/BD_to_AVP"
SPARKLE = f"{{{SPARKLE_NAMESPACE}}}"
SHORT_VERSION_PATTERN = re.compile(r"^[0-9]+(?:\.[0-9]+){2}(?:rc[0-9]+)?$")
ET.register_namespace("sparkle", SPARKLE_NAMESPACE)
ET.register_namespace("dc", DC_NAMESPACE)


class AppcastError(RuntimeError):
    pass


@dataclass(frozen=True)
class AppcastItem:
    build_version: str
    short_version: str
    channel: str | None
    download_url: str
    length: int
    signature: str
    release_notes_url: str
    minimum_system_version: str
    published_at: datetime


def load_appcast(path: Path) -> tuple[Any, ET.Element]:
    try:
        tree = ET.parse(path)
    except (ET.ParseError, OSError) as error:
        raise AppcastError(f"Unable to parse appcast {path}: {error}") from error
    root = tree.getroot()
    if root.tag != "rss" or root.get("version") != "2.0":
        raise AppcastError("Appcast root must be RSS 2.0.")
    channel = root.find("channel")
    if channel is None:
        raise AppcastError("Appcast is missing its channel element.")
    return tree, channel


def _text(element: ET.Element, tag: str) -> str:
    child = element.find(tag)
    return (child.text or "").strip() if child is not None else ""


def _parse_build_number(value: str, label: str) -> int:
    if not value.isdigit():
        raise AppcastError(f"{label} must be a canonical numeric build greater than 1.")
    number = int(value)
    if number <= 1 or str(number) != value:
        raise AppcastError(f"{label} must be a canonical numeric build greater than 1.")
    return number


def _build_number(item: ET.Element) -> int:
    return _parse_build_number(_text(item, f"{SPARKLE}version"), "sparkle:version")


def _release_channel(short_version: str) -> str | None:
    if SHORT_VERSION_PATTERN.fullmatch(short_version) is None:
        raise AppcastError(f"Invalid Sparkle short version: {short_version!r}")
    return "rc" if re.search(r"rc[0-9]+$", short_version, flags=re.IGNORECASE) else None


def maximum_build_version(channel: ET.Element) -> int | None:
    builds = [_build_number(item) for item in channel.findall("item")]
    return max(builds) if builds else None


def check_new_build(feed_path: Path, build_version: str) -> None:
    candidate_build = _parse_build_number(build_version, "Candidate build version")
    _, channel = load_appcast(feed_path)
    validate_appcast_channel(channel)
    maximum = maximum_build_version(channel)
    if maximum is not None and candidate_build <= maximum:
        raise AppcastError(f"Candidate build {build_version} must be newer than published build {maximum}.")


def check_new_release(feed_path: Path, build_version: str, short_version: str) -> None:
    check_new_build(feed_path, build_version)
    _release_channel(short_version)
    _, channel = load_appcast(feed_path)
    if any(_text(item, f"{SPARKLE}shortVersionString") == short_version for item in channel.findall("item")):
        raise AppcastError(f"Sparkle short version is already published: {short_version}")


def _validate_https_url(value: str, label: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise AppcastError(f"{label} must be an absolute HTTPS URL: {value}")


def _validate_release_url(value: str, label: str, *, download: bool) -> tuple[str, str | None]:
    _validate_https_url(value, label)
    parsed = urlparse(value)
    if parsed.netloc != "github.com":
        raise AppcastError(f"{label} must use github.com: {value}")
    if parsed.query or parsed.fragment:
        raise AppcastError(f"{label} must not contain a query string or fragment: {value}")
    expected_marker = f"{REPOSITORY_PATH}/releases/download/" if download else f"{REPOSITORY_PATH}/releases/tag/"
    decoded_path = unquote(parsed.path)
    if not decoded_path.startswith(expected_marker):
        raise AppcastError(f"{label} must be tag-qualified for cbusillo/BD_to_AVP: {value}")
    suffix = decoded_path[len(expected_marker) :]
    if download:
        tag, separator, asset_name = suffix.partition("/")
        if not separator or not tag or not asset_name or "/" in asset_name:
            raise AppcastError(f"{label} must identify one release tag and asset: {value}")
        if not asset_name.lower().endswith(".dmg"):
            raise AppcastError(f"{label} must identify a DMG asset: {value}")
        return tag, asset_name
    if not suffix or "/" in suffix:
        raise AppcastError(f"{label} must identify exactly one release tag: {value}")
    return suffix, None


def _validate_signature(value: str) -> None:
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as error:
        raise AppcastError("sparkle:edSignature is not valid base64.") from error
    if len(decoded) != 64:
        raise AppcastError("sparkle:edSignature must decode to a 64-byte Ed25519 signature.")


def validate_appcast_channel(channel: ET.Element) -> None:
    if channel.find("title") is None or channel.find("description") is None:
        raise AppcastError("Appcast channel requires title and description elements.")
    if channel.findall(f".//{SPARKLE}deltas"):
        raise AppcastError("Delta updates are not allowed in the Sparkle appcast.")

    build_versions: list[int] = []
    short_versions: set[str] = set()
    download_urls: set[str] = set()
    for item in channel.findall("item"):
        build_version = _build_number(item)
        short_version = _text(item, f"{SPARKLE}shortVersionString")
        expected_channel = _release_channel(short_version)
        item_channel = _text(item, f"{SPARKLE}channel") or None
        if item_channel not in {None, "rc"}:
            raise AppcastError(f"Unsupported Sparkle channel: {item_channel}")
        if item_channel != expected_channel:
            raise AppcastError(f"Release candidate channel and short version disagree for {short_version!r}.")

        enclosures = item.findall("enclosure")
        if len(enclosures) != 1:
            raise AppcastError("Every appcast item requires exactly one full-update enclosure.")
        enclosure = enclosures[0]
        download_url = enclosure.get("url", "")
        download_tag, _ = _validate_release_url(download_url, "Enclosure URL", download=True)
        length = enclosure.get("length", "")
        if not length.isdigit() or int(length) <= 0:
            raise AppcastError("Enclosure length must be a positive integer.")
        if enclosure.get("type") != "application/octet-stream":
            raise AppcastError("Enclosure type must be application/octet-stream.")
        signature = enclosure.get(f"{SPARKLE}edSignature", "")
        _validate_signature(signature)

        release_notes_url = _text(item, f"{SPARKLE}releaseNotesLink")
        release_notes_tag, _ = _validate_release_url(release_notes_url, "Release notes URL", download=False)
        if release_notes_tag != download_tag:
            raise AppcastError("Release notes and enclosure URLs must use the same release tag.")
        if download_tag != f"v{short_version}":
            raise AppcastError("Appcast URLs must use the tag derived from sparkle:shortVersionString.")
        if _text(item, "link") != release_notes_url:
            raise AppcastError("Appcast item link must match sparkle:releaseNotesLink.")
        minimum_system_version = _text(item, f"{SPARKLE}minimumSystemVersion")
        if not minimum_system_version:
            raise AppcastError("Every appcast item requires sparkle:minimumSystemVersion.")
        try:
            parsedate_to_datetime(_text(item, "pubDate"))
        except (TypeError, ValueError) as error:
            raise AppcastError("Every appcast item requires a valid RFC 2822 pubDate.") from error

        if build_version in build_versions:
            raise AppcastError(f"Duplicate Sparkle build version: {build_version}")
        if short_version in short_versions:
            raise AppcastError(f"Duplicate Sparkle short version: {short_version}")
        if download_url in download_urls:
            raise AppcastError(f"Duplicate Sparkle download URL: {download_url}")
        build_versions.append(build_version)
        short_versions.add(short_version)
        download_urls.add(download_url)

    if build_versions != sorted(build_versions, reverse=True):
        raise AppcastError("Appcast items must be ordered from newest to oldest build version.")


def validate_appcast(path: Path) -> None:
    _, channel = load_appcast(path)
    validate_appcast_channel(channel)


def validate_empty_appcast(path: Path) -> None:
    validate_appcast(path)
    _, channel = load_appcast(path)
    if channel.findall("item"):
        raise AppcastError("Emergency feed must be a valid empty appcast.")


def validate_release_snapshot(path: Path, short_version: str) -> None:
    validate_appcast(path)
    _release_channel(short_version)
    _, channel = load_appcast(path)
    items = channel.findall("item")
    if not items or _text(items[0], f"{SPARKLE}shortVersionString") != short_version:
        raise AppcastError(f"Appcast snapshot must start with release {short_version}.")


def verify_release_item(
    feed_path: Path,
    *,
    build_version: str,
    short_version: str,
    download_url: str,
    length: int,
) -> None:
    validate_appcast(feed_path)
    _, channel = load_appcast(feed_path)
    matches = [item for item in channel.findall("item") if _text(item, f"{SPARKLE}shortVersionString") == short_version]
    if len(matches) != 1:
        raise AppcastError(f"Expected exactly one appcast item for {short_version}; found {len(matches)}.")
    item = matches[0]
    if _text(item, f"{SPARKLE}version") != build_version:
        raise AppcastError(f"Appcast build for {short_version} does not match {build_version}.")
    enclosure = item.find("enclosure")
    if enclosure is None:
        raise AppcastError(f"Appcast item for {short_version} is missing its enclosure.")
    if enclosure.get("url") != download_url:
        raise AppcastError(f"Appcast enclosure URL for {short_version} does not match the release asset.")
    if enclosure.get("length") != str(length):
        raise AppcastError(f"Appcast enclosure length for {short_version} does not match the release asset.")


def append_item(feed_path: Path, output_path: Path, item: AppcastItem) -> None:
    check_new_release(feed_path, item.build_version, item.short_version)
    if item.channel not in {None, "rc"}:
        raise AppcastError(f"Unsupported Sparkle channel: {item.channel}")
    expected_channel = _release_channel(item.short_version)
    if item.channel != expected_channel:
        raise AppcastError("Sparkle channel and short version disagree.")
    download_tag, _ = _validate_release_url(item.download_url, "Enclosure URL", download=True)
    release_notes_tag, _ = _validate_release_url(item.release_notes_url, "Release notes URL", download=False)
    if release_notes_tag != download_tag:
        raise AppcastError("Release notes and enclosure URLs must use the same release tag.")
    if download_tag != f"v{item.short_version}":
        raise AppcastError("Appcast URLs must use the tag derived from sparkle:shortVersionString.")
    _validate_signature(item.signature)
    if item.length <= 0:
        raise AppcastError("Enclosure length must be positive.")

    tree, channel = load_appcast(feed_path)
    item_element = ET.Element("item")
    ET.SubElement(item_element, "title").text = f"Version {item.short_version}"
    ET.SubElement(item_element, "link").text = item.release_notes_url
    ET.SubElement(item_element, f"{SPARKLE}version").text = item.build_version
    ET.SubElement(item_element, f"{SPARKLE}shortVersionString").text = item.short_version
    if item.channel:
        ET.SubElement(item_element, f"{SPARKLE}channel").text = item.channel
    ET.SubElement(item_element, f"{SPARKLE}releaseNotesLink").text = item.release_notes_url
    ET.SubElement(item_element, "pubDate").text = format_datetime(item.published_at.astimezone(timezone.utc))
    ET.SubElement(
        item_element,
        "enclosure",
        {
            "url": item.download_url,
            "length": str(item.length),
            "type": "application/octet-stream",
            f"{SPARKLE}edSignature": item.signature,
        },
    )
    ET.SubElement(item_element, f"{SPARKLE}minimumSystemVersion").text = item.minimum_system_version

    first_item_index = next(
        (index for index, child in enumerate(channel) if child.tag == "item"),
        len(channel),
    )
    channel.insert(first_item_index, item_element)
    ET.indent(tree, space="  ")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tree.write(output_path, encoding="utf-8", xml_declaration=True)
    validate_appcast(output_path)


def _channel_argument(value: str) -> str | None:
    return None if value == "stable" else value


def main() -> int:
    parser = argparse.ArgumentParser(description="Create and validate the BD_to_AVP Sparkle appcast.")
    commands = parser.add_subparsers(dest="command", required=True)

    add = commands.add_parser("add", help="Add one signed full-DMG item to an appcast.")
    add.add_argument("--feed", type=Path, required=True)
    add.add_argument("--output", type=Path, required=True)
    add.add_argument("--build-version", required=True)
    add.add_argument("--short-version", required=True)
    add.add_argument("--channel", choices=("stable", "rc"), required=True)
    add.add_argument("--download-url", required=True)
    add.add_argument("--length", type=int, required=True)
    add.add_argument("--signature", required=True)
    add.add_argument("--release-notes-url", required=True)
    add.add_argument("--minimum-system-version", required=True)
    add.add_argument("--published-at", help="RFC 3339 publication time; defaults to now in UTC.")

    validate = commands.add_parser("validate", help="Validate all appcast entries.")
    validate.add_argument("--feed", type=Path, required=True)

    validate_empty = commands.add_parser("validate-empty", help="Validate an emergency empty appcast.")
    validate_empty.add_argument("--feed", type=Path, required=True)

    validate_snapshot = commands.add_parser(
        "validate-snapshot",
        help="Validate a cumulative snapshot whose newest item matches a release.",
    )
    validate_snapshot.add_argument("--feed", type=Path, required=True)
    validate_snapshot.add_argument("--short-version", required=True)

    check = commands.add_parser("check-build", help="Require a build number newer than the appcast.")
    check.add_argument("--feed", type=Path, required=True)
    check.add_argument("--build-version", required=True)

    check_release = commands.add_parser("check-release", help="Require a new build and short version.")
    check_release.add_argument("--feed", type=Path, required=True)
    check_release.add_argument("--build-version", required=True)
    check_release.add_argument("--short-version", required=True)

    verify_release = commands.add_parser(
        "verify-release",
        help="Verify one release item against an exact GitHub Release asset.",
    )
    verify_release.add_argument("--feed", type=Path, required=True)
    verify_release.add_argument("--build-version", required=True)
    verify_release.add_argument("--short-version", required=True)
    verify_release.add_argument("--download-url", required=True)
    verify_release.add_argument("--length", type=int, required=True)

    args = parser.parse_args()
    if args.command == "validate":
        validate_appcast(args.feed)
    elif args.command == "validate-empty":
        validate_empty_appcast(args.feed)
    elif args.command == "validate-snapshot":
        validate_release_snapshot(args.feed, args.short_version)
    elif args.command == "check-build":
        check_new_build(args.feed, args.build_version)
    elif args.command == "check-release":
        check_new_release(args.feed, args.build_version, args.short_version)
    elif args.command == "verify-release":
        verify_release_item(
            args.feed,
            build_version=args.build_version,
            short_version=args.short_version,
            download_url=args.download_url,
            length=args.length,
        )
    else:
        published_at = datetime.fromisoformat(args.published_at) if args.published_at else datetime.now(timezone.utc)
        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=timezone.utc)
        append_item(
            args.feed,
            args.output,
            AppcastItem(
                build_version=args.build_version,
                short_version=args.short_version,
                channel=_channel_argument(args.channel),
                download_url=args.download_url,
                length=args.length,
                signature=args.signature,
                release_notes_url=args.release_notes_url,
                minimum_system_version=args.minimum_system_version,
                published_at=published_at,
            ),
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
