from __future__ import annotations

import argparse
import json
import os
import plistlib
import subprocess
import sys
import tempfile

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from scripts.native_app import (
    NATIVE_APP_NAME,
    NATIVE_EXECUTABLE_NAME,
    NATIVE_PRODUCT_NAME,
    smoke_packaged_worker,
    verify_layout,
)
from scripts.sparkle_bundle import SparkleBundleMetadata, inspect_app_bundle
from scripts.verify_app_tools import REQUIRED_TOOLS, verify_tool

RELEASE_VOLUME_NAME = NATIVE_PRODUCT_NAME


class MacOSReleaseError(RuntimeError):
    pass


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=True, text=True)


def verify_release_app(
    app_path: Path,
    *,
    verify_signatures: bool = False,
    verify_distribution: bool = False,
    smoke_app: bool = False,
    smoke_tools: bool = False,
    smoke_worker: bool = False,
) -> SparkleBundleMetadata:
    if app_path.name != NATIVE_APP_NAME:
        raise MacOSReleaseError(f"Release app must be named {NATIVE_APP_NAME}.")
    verify_layout(app_path)
    metadata = inspect_app_bundle(app_path, verify_signatures=verify_signatures)
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
    with tempfile.TemporaryDirectory(prefix="macos-release-startup-") as temporary_directory:
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
            raise MacOSReleaseError("macOS app startup smoke did not exit within 20 seconds.") from error
    if completed.returncode != 0:
        details = (completed.stderr or completed.stdout).strip()
        raise MacOSReleaseError(f"macOS app startup smoke exited with status {completed.returncode}: {details}")


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
        raise MacOSReleaseError(f"macOS bundled-tool smoke failed: {error}") from error


def create_release_dmg(app_path: Path, output_path: Path) -> Path:
    if output_path.suffix.lower() != ".dmg":
        raise MacOSReleaseError("Release output must use the .dmg extension.")
    if output_path.exists():
        raise MacOSReleaseError(f"Refusing to replace existing release DMG: {output_path}")
    verify_release_app(app_path, verify_signatures=True)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="macos-release-dmg-", dir=output_path.parent) as temporary_directory:
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
                RELEASE_VOLUME_NAME,
                str(staging_root),
                str(output_path),
            ]
        )

    if not output_path.is_file():
        raise MacOSReleaseError(f"Release DMG was not created: {output_path}")
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
        raise MacOSReleaseError("Notary service did not return valid JSON.") from error
    if completed.returncode != 0 or payload.get("status") != "Accepted":
        status = payload.get("status", "unknown")
        message = payload.get("message", "Notary submission was not accepted.")
        raise MacOSReleaseError(f"Notary submission failed with status {status!r}: {message}")

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
        raise MacOSReleaseError(f"Expected one mounted DMG volume; found {len(mount_points)}.")
    try:
        yield mount_points[0]
    finally:
        subprocess.run(
            ["hdiutil", "detach", str(mount_points[0])],
            capture_output=True,
            check=True,
            text=True,
        )


def verify_release_dmg(
    dmg_path: Path,
    *,
    verify_signatures: bool = False,
    verify_distribution: bool = False,
    smoke_app: bool = False,
    smoke_tools: bool = False,
    smoke_worker: bool = False,
) -> SparkleBundleMetadata:
    if not dmg_path.is_file():
        raise MacOSReleaseError(f"Release DMG is missing: {dmg_path}")
    if verify_signatures:
        verify_dmg_signature(dmg_path)
    if verify_distribution:
        verify_dmg_distribution(dmg_path)

    with mounted_dmg(dmg_path) as mount_point:
        app_paths = list(mount_point.glob("*.app"))
        if len(app_paths) != 1 or app_paths[0].name != NATIVE_APP_NAME:
            raise MacOSReleaseError(f"Expected one {NATIVE_APP_NAME} bundle in the release DMG.")
        applications_link = mount_point / "Applications"
        if not applications_link.is_symlink() or os.readlink(applications_link) != "/Applications":
            raise MacOSReleaseError("Release DMG must contain an Applications symlink.")
        return verify_release_app(
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
    parser = argparse.ArgumentParser(description="Assemble and validate the macOS release artifact.")
    commands = parser.add_subparsers(dest="command", required=True)

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
    if args.command == "verify-app":
        metadata = verify_release_app(
            args.app,
            verify_signatures=args.verify_signatures,
            verify_distribution=args.verify_distribution,
            smoke_app=args.smoke_app,
            smoke_tools=args.smoke_tools,
            smoke_worker=args.smoke_worker,
        )
        print(json.dumps(asdict(metadata), sort_keys=True))
    elif args.command == "create-dmg":
        print(create_release_dmg(args.app, args.output))
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
        metadata = verify_release_dmg(
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
