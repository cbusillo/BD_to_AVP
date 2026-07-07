from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
WHEELHOUSE = REPO_ROOT / ".briefcase-wheelhouse"
WHEELHOUSE_REQUIREMENTS = ["pysrt==1.1.2"]
VENDOR_FFMPEG_COMMANDS = {"create", "build", "run"}
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
VENDORED_TOOLS = ["ffmpeg", "ffprobe"]


def run(command: list[str]) -> None:
    subprocess.run(command, check=True, cwd=REPO_ROOT)


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
            shutil.copy2(source_path, APP_RESOURCE_BIN / tool_name)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Briefcase with repo-local packaging fixes.")
    parser.add_argument("briefcase_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    if not args.briefcase_args:
        parser.error("provide a Briefcase command, for example: create --no-input")

    build_wheelhouse()
    if should_vendor_ffmpeg(args.briefcase_args):
        vendor_ffmpeg()
    run(
        [
            sys.executable,
            "-m",
            "briefcase",
            *args.briefcase_args,
            "-C",
            briefcase_config_override(),
        ]
    )


if __name__ == "__main__":
    main()
