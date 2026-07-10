from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from scripts.sparkle_macos import APP_PATH, FRAMEWORK_RELATIVE_PATH, embed_sparkle, verify_framework_layout


REPO_ROOT = Path(__file__).resolve().parents[1]
WHEELHOUSE = REPO_ROOT / ".briefcase-wheelhouse"
WHEELHOUSE_REQUIREMENTS = ["pysrt==1.1.2"]
VENDOR_FFMPEG_COMMANDS = {"create", "build", "run"}
SYNC_TOOL_COMMANDS = {"create", "build", "run"}
POST_SYNC_TOOL_COMMANDS = {"create"}
SPARKLE_COMMANDS = {"create", "build", "run", "package"}
POST_EMBED_SPARKLE_COMMANDS = {"create"}
FORCE_EXTRACT_SPARKLE_COMMANDS = {"package"}
APP_RESOURCE_BIN = (
    REPO_ROOT
    / "build"
    / "bd-to-avp"
    / "macos"
    / "app"
    / "3D Blu-ray to Vision Pro.app"
    / "Contents"
    / "Resources"
    / "app"
    / "bd_to_avp"
    / "bin"
)
VENDORED_TOOLS = ["ffmpeg", "ffprobe", "MP4Box"]


def run(command: list[str]) -> None:
    subprocess.run(command, check=True, cwd=REPO_ROOT)


def run_briefcase(briefcase_args: list[str]) -> None:
    run(
        [
            sys.executable,
            "-m",
            "scripts.briefcase_cli",
            *briefcase_args,
            "-C",
            briefcase_config_override(),
        ]
    )


def build_wheelhouse() -> None:
    WHEELHOUSE.mkdir(exist_ok=True)
    run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--wheel-dir",
            str(WHEELHOUSE),
            *WHEELHOUSE_REQUIREMENTS,
        ]
    )


def briefcase_config_override() -> str:
    wheelhouse_path = WHEELHOUSE.resolve().as_posix()
    return f'requirement_installer_args=["--find-links", "{wheelhouse_path}"]'


def should_vendor_ffmpeg(briefcase_args: list[str]) -> bool:
    commands = {arg for arg in briefcase_args if not arg.startswith("-")}
    return bool(commands & VENDOR_FFMPEG_COMMANDS)


def vendor_ffmpeg() -> None:
    run([sys.executable, "scripts/vendor_ffmpeg_macos.py"])
    sync_vendored_tools_to_existing_app()


def sync_vendored_tools_to_existing_app() -> None:
    if not APP_RESOURCE_BIN.is_dir():
        return

    for tool_name in VENDORED_TOOLS:
        source_path = REPO_ROOT / "bd_to_avp" / "bin" / tool_name
        if source_path.exists():
            output_path = APP_RESOURCE_BIN / tool_name
            shutil.copy2(source_path, output_path)
            output_path.chmod(output_path.stat().st_mode | 0o111)


def should_sync_vendored_tools(briefcase_args: list[str]) -> bool:
    commands = {arg for arg in briefcase_args if not arg.startswith("-")}
    return bool(commands & SYNC_TOOL_COMMANDS)


def should_sync_vendored_tools_after(briefcase_args: list[str]) -> bool:
    commands = {arg for arg in briefcase_args if not arg.startswith("-")}
    return bool(commands & POST_SYNC_TOOL_COMMANDS)


def should_embed_sparkle(briefcase_args: list[str]) -> bool:
    commands = {arg for arg in briefcase_args if not arg.startswith("-")}
    return bool(commands & SPARKLE_COMMANDS)


def should_embed_sparkle_after(briefcase_args: list[str]) -> bool:
    commands = {arg for arg in briefcase_args if not arg.startswith("-")}
    return bool(commands & POST_EMBED_SPARKLE_COMMANDS)


def should_force_extract_sparkle(briefcase_args: list[str]) -> bool:
    commands = {arg for arg in briefcase_args if not arg.startswith("-")}
    return bool(commands & FORCE_EXTRACT_SPARKLE_COMMANDS)


def sync_sparkle_to_existing_app(*, force_extract: bool = False) -> None:
    if APP_PATH.is_dir():
        embed_sparkle(APP_PATH, force_extract=force_extract)


def main_with_args(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run Briefcase with repo-local packaging fixes.")
    parser.add_argument("briefcase_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    if not args.briefcase_args:
        parser.error("provide a Briefcase command, for example: create --no-input")

    build_wheelhouse()
    if should_vendor_ffmpeg(args.briefcase_args):
        vendor_ffmpeg()
    elif should_sync_vendored_tools(args.briefcase_args):
        sync_vendored_tools_to_existing_app()
    if should_embed_sparkle(args.briefcase_args):
        sync_sparkle_to_existing_app(force_extract=should_force_extract_sparkle(args.briefcase_args))
    run_briefcase(args.briefcase_args)
    if should_sync_vendored_tools_after(args.briefcase_args):
        sync_vendored_tools_to_existing_app()
    if should_embed_sparkle(args.briefcase_args):
        if should_embed_sparkle_after(args.briefcase_args) and APP_PATH.is_dir():
            embed_sparkle(APP_PATH)
        if not APP_PATH.is_dir():
            return
        verify_framework_layout(APP_PATH / FRAMEWORK_RELATIVE_PATH)


def main() -> None:
    main_with_args()


if __name__ == "__main__":
    main()
