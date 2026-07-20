from __future__ import annotations

import argparse
import json
import plistlib
import re
import subprocess
import tomllib

from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator

from scripts.native_app import verify_support_diagnostics_endpoint
from scripts.production_identity import (
    PRODUCTION_BUNDLE_IDENTIFIER,
    PRODUCTION_DEVELOPER_IDENTITY,
    PRODUCTION_DISTRIBUTION_CHANNEL,
    PRODUCTION_FEED_URL,
    PRODUCTION_SPARKLE_PUBLIC_KEY,
    PRODUCTION_TEAM_ID,
    validate_production_public_key,
)
from scripts.release import RETIRED_RELEASE_TAGS, ReleaseError, parse_release_version
from scripts.sparkle_macos import FRAMEWORK_RELATIVE_PATH, REPO_ROOT, load_release, verify_framework_layout


INFO_PLIST_RELATIVE_PATH = Path("Contents/Info.plist")
PUBLIC_KEY_PATH = REPO_ROOT / "sparkle-public-ed-key.txt"
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"
SYSTEM_VERSION_PATTERN = re.compile(r"^[0-9]+(?:\.[0-9]+){1,2}$")


class SparkleBundleError(RuntimeError):
    pass


@dataclass(frozen=True)
class SparkleBundleMetadata:
    app_path: str
    bundle_identifier: str
    build_version: str
    short_version: str
    distribution_channel: str
    support_diagnostics_endpoint: str
    feed_url: str
    minimum_system_version: str
    public_key: str


def load_expected_info(
    pyproject_path: Path = PYPROJECT_PATH, public_key_path: Path = PUBLIC_KEY_PATH
) -> dict[str, object]:
    with pyproject_path.open("rb") as handle:
        pyproject = tomllib.load(handle)
    info = dict(pyproject["tool"]["briefcase"]["app"]["bd-to-avp"]["macOS"]["info"])
    public_key = public_key_path.read_text(encoding="utf-8").strip()
    try:
        validate_production_public_key(public_key)
    except ValueError as error:
        raise SparkleBundleError(str(error)) from error
    if info.get("SUPublicEDKey") != public_key:
        raise SparkleBundleError("The Briefcase SUPublicEDKey does not match sparkle-public-ed-key.txt.")
    if info.get("SUFeedURL") != PRODUCTION_FEED_URL:
        raise SparkleBundleError("The Briefcase SUFeedURL does not match the production feed identity.")
    if info.get("BDToAVPDistributionChannel") != PRODUCTION_DISTRIBUTION_CHANNEL:
        raise SparkleBundleError("The Briefcase distribution channel does not match the production identity.")
    return info


def _require_equal(info: dict[str, object], key: str, expected: object) -> object:
    actual = info.get(key)
    if actual != expected:
        raise SparkleBundleError(f"Info.plist {key} must be {expected!r}; found {actual!r}.")
    return actual


def _parse_short_version(value: str) -> str:
    try:
        version = parse_release_version(value)
    except ReleaseError as error:
        raise SparkleBundleError(
            "CFBundleShortVersionString must be a canonical three-part PEP 440 Stable, Alpha, Beta, or RC version."
        ) from error
    if version.release_tag in RETIRED_RELEASE_TAGS:
        raise SparkleBundleError("CFBundleShortVersionString belongs to a retired preview identity.")
    return version.text


def _verify_codesign_identity(path: Path, description: str) -> None:
    result = subprocess.run(
        ["codesign", "-dv", "--verbose=4", path],
        check=True,
        capture_output=True,
        text=True,
    )
    metadata = f"{result.stdout}\n{result.stderr}"
    authorities = re.findall(r"^Authority=(.+)$", metadata, re.MULTILINE)
    team_identifiers = re.findall(r"^TeamIdentifier=(.+)$", metadata, re.MULTILINE)
    if authorities[:1] != [PRODUCTION_DEVELOPER_IDENTITY]:
        raise SparkleBundleError(
            f"{description} signing authority must be {PRODUCTION_DEVELOPER_IDENTITY!r}; found {authorities[:1]!r}."
        )
    if team_identifiers != [PRODUCTION_TEAM_ID]:
        raise SparkleBundleError(
            f"{description} TeamIdentifier must be {PRODUCTION_TEAM_ID!r}; found {team_identifiers!r}."
        )


def verify_code_signatures(app_path: Path) -> None:
    framework_path = app_path / FRAMEWORK_RELATIVE_PATH
    targets = [
        framework_path / "Versions/B/Updater.app",
        framework_path / "Versions/B/XPCServices/Downloader.xpc",
        framework_path / "Versions/B/XPCServices/Installer.xpc",
        framework_path,
        app_path,
    ]
    for target in targets:
        subprocess.run(
            ["codesign", "--verify", "--strict", "--verbose=4", target],
            check=True,
            capture_output=True,
            text=True,
        )
    subprocess.run(
        ["codesign", "--verify", "--deep", "--strict", "--verbose=4", app_path],
        check=True,
        capture_output=True,
        text=True,
    )
    _verify_codesign_identity(app_path, "Containing app")


def verify_dmg_distribution(dmg_path: Path) -> None:
    _verify_codesign_identity(dmg_path, "DMG")
    subprocess.run(
        ["xcrun", "stapler", "validate", dmg_path],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            "spctl",
            "--assess",
            "--type",
            "open",
            "--context",
            "context:primary-signature",
            "--verbose=4",
            dmg_path,
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def verify_app_distribution(app_path: Path) -> None:
    subprocess.run(
        ["spctl", "--assess", "--type", "execute", "--verbose=4", app_path],
        check=True,
        capture_output=True,
        text=True,
    )


def inspect_app_bundle(
    app_path: Path,
    *,
    expected_info: dict[str, object] | None = None,
    verify_signatures: bool = False,
    require_repository_build: bool = True,
    environment: Mapping[str, str] | None = None,
) -> SparkleBundleMetadata:
    info_path = app_path / INFO_PLIST_RELATIVE_PATH
    if not info_path.is_file():
        raise SparkleBundleError(f"Info.plist not found: {info_path}")
    with info_path.open("rb") as handle:
        info = plistlib.load(handle)

    expected_info = expected_info or load_expected_info()
    expected_keys = [
        "BDToAVPDistributionChannel",
        "SUFeedURL",
        "SUPublicEDKey",
        "SUAllowsAutomaticUpdates",
        "SUVerifyUpdateBeforeExtraction",
    ]
    if require_repository_build:
        expected_keys.insert(0, "CFBundleVersion")
    for key in expected_keys:
        _require_equal(info, key, expected_info[key])
    _require_equal(info, "CFBundleIdentifier", PRODUCTION_BUNDLE_IDENTIFIER)
    _require_equal(info, "BDToAVPDistributionChannel", PRODUCTION_DISTRIBUTION_CHANNEL)
    _require_equal(info, "SUFeedURL", PRODUCTION_FEED_URL)
    _require_equal(info, "SUPublicEDKey", PRODUCTION_SPARKLE_PUBLIC_KEY)
    if "SUEnableAutomaticChecks" in info:
        raise SparkleBundleError("SUEnableAutomaticChecks must remain unset so Sparkle owns consent prompting.")

    build_version = str(info["CFBundleVersion"])
    if not build_version.isdigit():
        raise SparkleBundleError("CFBundleVersion must be a canonical numeric repository counter greater than 1.")
    build_number = int(build_version)
    if build_number <= 1 or str(build_number) != build_version:
        raise SparkleBundleError("CFBundleVersion must be a canonical numeric repository counter greater than 1.")
    short_version = _parse_short_version(str(info.get("CFBundleShortVersionString", "")))
    minimum_system_version = str(info.get("LSMinimumSystemVersion", "")).strip()
    if SYSTEM_VERSION_PATTERN.fullmatch(minimum_system_version) is None:
        raise SparkleBundleError("LSMinimumSystemVersion must be a numeric dotted version.")
    try:
        support_diagnostics_endpoint = verify_support_diagnostics_endpoint(info, environment=environment)
    except ValueError as error:
        raise SparkleBundleError(str(error)) from error

    framework_path = app_path / FRAMEWORK_RELATIVE_PATH
    verify_framework_layout(framework_path, expected_version=load_release().version)
    if verify_signatures:
        verify_code_signatures(app_path)

    return SparkleBundleMetadata(
        app_path=app_path.as_posix(),
        bundle_identifier=str(info.get("CFBundleIdentifier", "")),
        build_version=build_version,
        short_version=short_version,
        distribution_channel=str(info["BDToAVPDistributionChannel"]),
        support_diagnostics_endpoint=support_diagnostics_endpoint,
        feed_url=str(info["SUFeedURL"]),
        minimum_system_version=minimum_system_version,
        public_key=str(info["SUPublicEDKey"]),
    )


@contextmanager
def mounted_dmg(dmg_path: Path) -> Iterator[Path]:
    result = subprocess.run(
        ["hdiutil", "attach", "-readonly", "-nobrowse", "-plist", dmg_path],
        check=True,
        capture_output=True,
    )
    payload = plistlib.loads(result.stdout)
    mount_points = [
        Path(entity["mount-point"]) for entity in payload.get("system-entities", []) if entity.get("mount-point")
    ]
    if len(mount_points) != 1:
        raise SparkleBundleError(f"Expected one mounted DMG volume; found {len(mount_points)}.")
    mount_point = mount_points[0]
    try:
        yield mount_point
    finally:
        subprocess.run(["hdiutil", "detach", mount_point], check=True, capture_output=True, text=True)


def inspect_dmg(
    dmg_path: Path,
    *,
    verify_signatures: bool = False,
    require_repository_build: bool = True,
    verify_distribution: bool = False,
    environment: Mapping[str, str] | None = None,
) -> SparkleBundleMetadata:
    if verify_distribution:
        verify_dmg_distribution(dmg_path)
    with mounted_dmg(dmg_path) as mount_point:
        app_paths = list(mount_point.glob("*.app"))
        if len(app_paths) != 1:
            raise SparkleBundleError(f"Expected one app bundle in the DMG; found {len(app_paths)}.")
        metadata = inspect_app_bundle(
            app_paths[0],
            verify_signatures=verify_signatures,
            require_repository_build=require_repository_build,
            environment=environment,
        )
        if verify_distribution:
            verify_app_distribution(app_paths[0])
        return metadata


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate Sparkle metadata, layout, and signatures in a macOS app.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--app", type=Path, help="Path to an unpackaged .app bundle.")
    source.add_argument("--dmg", type=Path, help="Path to a packaged DMG.")
    parser.add_argument(
        "--verify-signatures", action="store_true", help="Verify nested and containing code signatures."
    )
    parser.add_argument(
        "--release-artifact",
        action="store_true",
        help="Accept the artifact's numeric build while enforcing the protected release metadata policy.",
    )
    parser.add_argument(
        "--verify-distribution",
        action="store_true",
        help="Validate the DMG ticket and Gatekeeper assessments for the DMG and contained app.",
    )
    args = parser.parse_args()
    if args.verify_distribution and args.app:
        parser.error("--verify-distribution requires --dmg")

    metadata = (
        inspect_app_bundle(
            args.app,
            verify_signatures=args.verify_signatures,
            require_repository_build=not args.release_artifact,
        )
        if args.app
        else inspect_dmg(
            args.dmg,
            verify_signatures=args.verify_signatures,
            require_repository_build=not args.release_artifact,
            verify_distribution=args.verify_distribution,
        )
    )
    print(json.dumps(asdict(metadata), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
