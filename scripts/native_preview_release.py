from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import subprocess
import sys
import tempfile

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from scripts.native_app import (
    NATIVE_APP_NAME,
    NATIVE_BUILD_VERSION,
    NATIVE_BUNDLE_IDENTIFIER,
    NATIVE_EXECUTABLE_NAME,
    NATIVE_MINIMUM_SYSTEM_VERSION,
    NATIVE_PRERELEASE_VERSION,
    NATIVE_PRODUCT_NAME,
    NATIVE_SHORT_VERSION,
    smoke_packaged_worker,
    verify_codesign,
    verify_layout,
)
from scripts.verify_app_tools import REQUIRED_TOOLS, verify_tool

PREVIEW_VOLUME_NAME = NATIVE_PRODUCT_NAME
INFO_PLIST_RELATIVE_PATH = Path("Contents/Info.plist")


class NativePreviewReleaseError(RuntimeError):
    pass


@dataclass(frozen=True)
class NativePreviewReleaseMetadata:
    app_name: str
    build_version: str
    dmg_name: str
    prerelease_version: str
    release_name: str
    release_tag: str
    short_version: str

    def github_outputs(self) -> dict[str, str]:
        return {key: str(value) for key, value in asdict(self).items()}


@dataclass(frozen=True)
class NativePreviewMetadata:
    app_path: str
    build_version: str
    bundle_identifier: str
    minimum_system_version: str
    product_name: str
    short_version: str


def create_preview_release_metadata(
    *,
    app_name: str = NATIVE_APP_NAME,
    build_version: str = NATIVE_BUILD_VERSION,
    prerelease_version: str = NATIVE_PRERELEASE_VERSION,
    product_name: str = NATIVE_PRODUCT_NAME,
    short_version: str = NATIVE_SHORT_VERSION,
) -> NativePreviewReleaseMetadata:
    if re.fullmatch(r"\d+\.\d+\.\d+", short_version) is None:
        raise NativePreviewReleaseError("Native preview short version must contain three numeric components.")
    if re.fullmatch(r"[1-9]\d*", build_version) is None:
        raise NativePreviewReleaseError("Native preview build version must be a positive integer.")
    if (
        re.fullmatch(
            rf"{re.escape(short_version)}-(alpha|beta|rc)\.([1-9]\d*)",
            prerelease_version,
        )
        is None
    ):
        raise NativePreviewReleaseError(
            "Native preview prerelease version must match the short version followed by -alpha.N, -beta.N, or -rc.N."
        )

    file_stem = product_name.replace(" ", "-")
    return NativePreviewReleaseMetadata(
        app_name=app_name,
        build_version=build_version,
        dmg_name=f"{file_stem}-{prerelease_version}.dmg",
        prerelease_version=prerelease_version,
        release_name=f"v{prerelease_version}",
        release_tag=f"v{prerelease_version}",
        short_version=short_version,
    )


PREVIEW_RELEASE_METADATA = create_preview_release_metadata()


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=True, text=True)


def inspect_preview_info(app_path: Path) -> NativePreviewMetadata:
    if app_path.name != NATIVE_APP_NAME:
        raise NativePreviewReleaseError(f"Preview app must be named {NATIVE_APP_NAME}.")

    info_path = app_path / INFO_PLIST_RELATIVE_PATH
    if not info_path.is_file():
        raise NativePreviewReleaseError(f"Preview Info.plist is missing: {info_path}")
    with info_path.open("rb") as info_file:
        info = plistlib.load(info_file)

    expected_values = {
        "BluRayToVisionProEngineBundled": True,
        "CFBundleDisplayName": NATIVE_PRODUCT_NAME,
        "CFBundleExecutable": NATIVE_EXECUTABLE_NAME,
        "CFBundleIdentifier": NATIVE_BUNDLE_IDENTIFIER,
        "CFBundleName": NATIVE_PRODUCT_NAME,
        "CFBundleShortVersionString": NATIVE_SHORT_VERSION,
        "CFBundleVersion": NATIVE_BUILD_VERSION,
        "LSMinimumSystemVersion": NATIVE_MINIMUM_SYSTEM_VERSION,
        "MainModule": "bd_to_avp.worker",
    }
    mismatches = [
        f"{key}: expected {expected!r}, found {info.get(key)!r}"
        for key, expected in expected_values.items()
        if info.get(key) != expected
    ]
    update_keys = sorted(key for key in info if key.startswith("SU") or key == "BDToAVPDistributionChannel")
    if update_keys:
        mismatches.append("production update metadata is present: " + ", ".join(update_keys))
    if mismatches:
        raise NativePreviewReleaseError("Native preview identity validation failed:\n" + "\n".join(mismatches))

    return NativePreviewMetadata(
        app_path=app_path.as_posix(),
        build_version=str(info["CFBundleVersion"]),
        bundle_identifier=str(info["CFBundleIdentifier"]),
        minimum_system_version=str(info["LSMinimumSystemVersion"]),
        product_name=str(info["CFBundleName"]),
        short_version=str(info["CFBundleShortVersionString"]),
    )


def verify_preview_app(
    app_path: Path,
    *,
    verify_signatures: bool = False,
    verify_distribution: bool = False,
    smoke_app: bool = False,
    smoke_tools: bool = False,
    smoke_worker: bool = False,
) -> NativePreviewMetadata:
    metadata = inspect_preview_info(app_path)
    verify_layout(app_path)
    if verify_signatures:
        verify_codesign(app_path)
    if verify_distribution:
        run(["xcrun", "stapler", "validate", str(app_path)])
        run(["spctl", "--assess", "--type", "execute", "--verbose=4", str(app_path)])
    if smoke_app:
        smoke_native_app_startup(app_path)
    if smoke_tools:
        smoke_packaged_tools(app_path)
    if smoke_worker:
        smoke_packaged_worker(app_path)
    return metadata


def smoke_native_app_startup(app_path: Path) -> None:
    executable = app_path / "Contents" / "MacOS" / NATIVE_EXECUTABLE_NAME
    with tempfile.TemporaryDirectory(prefix="native-preview-startup-") as temporary_directory:
        environment = os.environ.copy()
        environment["HOME"] = temporary_directory
        environment["TMPDIR"] = temporary_directory
        try:
            completed = subprocess.run(
                [str(executable), "--startup-smoke"],
                cwd=temporary_directory,
                env=environment,
                capture_output=True,
                text=True,
                timeout=20,
            )
        except subprocess.TimeoutExpired as error:
            raise NativePreviewReleaseError("Native preview startup smoke did not exit within 20 seconds.") from error
    if completed.returncode != 0:
        details = (completed.stderr or completed.stdout).strip()
        raise NativePreviewReleaseError(
            f"Native preview startup smoke exited with status {completed.returncode}: {details}"
        )


def smoke_packaged_tools(app_path: Path) -> None:
    tool_directory = app_path / "Contents" / "Resources" / "app" / "bd_to_avp" / "bin"
    try:
        for tool_name, probe_args in REQUIRED_TOOLS.items():
            verify_tool(tool_directory / tool_name, probe_args)
    except (
        FileNotFoundError,
        PermissionError,
        RuntimeError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ) as error:
        raise NativePreviewReleaseError(f"Native preview bundled-tool smoke failed: {error}") from error


def create_preview_dmg(app_path: Path, output_path: Path) -> Path:
    if output_path.suffix.lower() != ".dmg":
        raise NativePreviewReleaseError("Native preview output must use the .dmg extension.")
    if output_path.exists():
        raise NativePreviewReleaseError(f"Refusing to replace existing preview DMG: {output_path}")
    verify_preview_app(app_path, verify_signatures=True)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="native-preview-dmg-", dir=output_path.parent) as temporary_directory:
        staging_root = Path(temporary_directory) / "volume"
        staging_root.mkdir()
        staged_app = staging_root / NATIVE_APP_NAME
        run(["ditto", str(app_path), str(staged_app)])
        (staging_root / "Applications").symlink_to("/Applications")
        run(
            [
                "diskutil",
                "image",
                "create",
                "from",
                "--verbose",
                "--format",
                "UDZO",
                "--volumeName",
                PREVIEW_VOLUME_NAME,
                str(staging_root),
                str(output_path),
            ]
        )

    if not output_path.is_file():
        raise NativePreviewReleaseError(f"Native preview DMG was not created: {output_path}")
    return output_path


def notarize_and_staple(
    submission_path: Path,
    staple_path: Path,
    *,
    keychain_profile: str,
    keychain_path: Path,
    log_path: Path,
) -> Mapping[str, object]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [
            "xcrun",
            "notarytool",
            "submit",
            str(submission_path),
            "--keychain-profile",
            keychain_profile,
            "--keychain",
            str(keychain_path),
            "--wait",
            "--timeout",
            "45m",
            "--output-format",
            "json",
            "--no-progress",
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    log_path.write_text(completed.stdout, encoding="utf-8")
    if completed.stderr:
        print(completed.stderr, file=sys.stderr, end="")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise NativePreviewReleaseError("Notary service did not return valid JSON.") from error
    if completed.returncode != 0 or payload.get("status") != "Accepted":
        status = payload.get("status", "unknown")
        message = payload.get("message", "Notary submission was not accepted.")
        raise NativePreviewReleaseError(f"Notary submission failed with status {status!r}: {message}")

    run(["xcrun", "stapler", "staple", str(staple_path)])
    run(["xcrun", "stapler", "validate", str(staple_path)])
    return payload


def verify_dmg_signature(dmg_path: Path) -> None:
    run(["codesign", "--verify", "--strict", "--verbose=2", str(dmg_path)])


def verify_dmg_distribution(dmg_path: Path) -> None:
    run(["xcrun", "stapler", "validate", str(dmg_path)])
    run(
        [
            "spctl",
            "--assess",
            "--type",
            "open",
            "--context",
            "context:primary-signature",
            "--verbose=4",
            str(dmg_path),
        ]
    )


@contextmanager
def mounted_dmg(dmg_path: Path) -> Iterator[Path]:
    completed = subprocess.run(
        ["hdiutil", "attach", "-readonly", "-nobrowse", "-plist", str(dmg_path)],
        capture_output=True,
        check=True,
    )
    payload = plistlib.loads(completed.stdout)
    mount_points = [
        Path(entity["mount-point"]) for entity in payload.get("system-entities", []) if entity.get("mount-point")
    ]
    if len(mount_points) != 1:
        for mount_point in reversed(mount_points):
            subprocess.run(
                ["hdiutil", "detach", str(mount_point)],
                capture_output=True,
                check=False,
                text=True,
            )
        raise NativePreviewReleaseError(f"Expected one mounted DMG volume; found {len(mount_points)}.")
    try:
        yield mount_points[0]
    finally:
        subprocess.run(
            ["hdiutil", "detach", str(mount_points[0])],
            capture_output=True,
            check=True,
            text=True,
        )


def verify_preview_dmg(
    dmg_path: Path,
    *,
    verify_signatures: bool = False,
    verify_distribution: bool = False,
    smoke_app: bool = False,
    smoke_tools: bool = False,
    smoke_worker: bool = False,
) -> NativePreviewMetadata:
    if not dmg_path.is_file():
        raise NativePreviewReleaseError(f"Native preview DMG is missing: {dmg_path}")
    if verify_signatures:
        verify_dmg_signature(dmg_path)
    if verify_distribution:
        verify_dmg_distribution(dmg_path)

    with mounted_dmg(dmg_path) as mount_point:
        app_paths = list(mount_point.glob("*.app"))
        if len(app_paths) != 1 or app_paths[0].name != NATIVE_APP_NAME:
            raise NativePreviewReleaseError(f"Expected one {NATIVE_APP_NAME} bundle in the preview DMG.")
        applications_link = mount_point / "Applications"
        if not applications_link.is_symlink() or os.readlink(applications_link) != "/Applications":
            raise NativePreviewReleaseError("Preview DMG must contain an Applications symlink.")
        return verify_preview_app(
            app_paths[0],
            verify_signatures=verify_signatures,
            verify_distribution=verify_distribution,
            smoke_app=smoke_app,
            smoke_tools=smoke_tools,
            smoke_worker=smoke_worker,
        )


def add_verification_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--verify-signatures", action="store_true")
    parser.add_argument("--verify-distribution", action="store_true")
    parser.add_argument("--smoke-app", action="store_true")
    parser.add_argument("--smoke-tools", action="store_true")
    parser.add_argument("--smoke-worker", action="store_true")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Assemble and validate the Native UI Preview release artifact.")
    commands = parser.add_subparsers(dest="command", required=True)

    metadata_parser = commands.add_parser("metadata")
    metadata_parser.add_argument("--github-output", type=Path)

    verify_app_parser = commands.add_parser("verify-app")
    verify_app_parser.add_argument("--app", type=Path, required=True)
    add_verification_flags(verify_app_parser)

    create_dmg_parser = commands.add_parser("create-dmg")
    create_dmg_parser.add_argument("--app", type=Path, required=True)
    create_dmg_parser.add_argument("--output", type=Path, required=True)

    notarize_parser = commands.add_parser("notarize")
    notarize_parser.add_argument("--submission", type=Path, required=True)
    notarize_parser.add_argument("--staple", type=Path, required=True)
    notarize_parser.add_argument("--keychain-profile", required=True)
    notarize_parser.add_argument("--keychain", type=Path, required=True)
    notarize_parser.add_argument("--log", type=Path, required=True)

    verify_dmg_parser = commands.add_parser("verify-dmg")
    verify_dmg_parser.add_argument("--dmg", type=Path, required=True)
    add_verification_flags(verify_dmg_parser)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    if args.command == "metadata":
        if args.github_output:
            with args.github_output.open("a", encoding="utf-8") as handle:
                for key, value in PREVIEW_RELEASE_METADATA.github_outputs().items():
                    handle.write(f"{key}={value}\n")
        print(json.dumps(asdict(PREVIEW_RELEASE_METADATA), sort_keys=True))
    elif args.command == "verify-app":
        metadata = verify_preview_app(
            args.app,
            verify_signatures=args.verify_signatures,
            verify_distribution=args.verify_distribution,
            smoke_app=args.smoke_app,
            smoke_tools=args.smoke_tools,
            smoke_worker=args.smoke_worker,
        )
        print(json.dumps(asdict(metadata), sort_keys=True))
    elif args.command == "create-dmg":
        print(create_preview_dmg(args.app, args.output))
    elif args.command == "notarize":
        payload = notarize_and_staple(
            args.submission,
            args.staple,
            keychain_profile=args.keychain_profile,
            keychain_path=args.keychain,
            log_path=args.log,
        )
        print(json.dumps(payload, sort_keys=True))
    elif args.command == "verify-dmg":
        metadata = verify_preview_dmg(
            args.dmg,
            verify_signatures=args.verify_signatures,
            verify_distribution=args.verify_distribution,
            smoke_app=args.smoke_app,
            smoke_tools=args.smoke_tools,
            smoke_worker=args.smoke_worker,
        )
        print(json.dumps(asdict(metadata), sort_keys=True))


if __name__ == "__main__":
    main()
