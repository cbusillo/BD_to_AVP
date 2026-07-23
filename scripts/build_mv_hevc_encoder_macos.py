#!/usr/bin/env python3

import argparse
import os
import platform
import shutil
import subprocess
import tempfile

from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_PATH = REPOSITORY_ROOT / "native/mv_hevc_encoder/MVHEVCEncoder.swift"
MINIMUM_MACOS = "26.0"
COMMAND_TIMEOUT_SECONDS = 180


def sdk_path() -> Path:
    xcrun = shutil.which("xcrun")
    if xcrun is None:
        raise RuntimeError("xcrun is required to build the MV-HEVC encoder")
    value = subprocess.check_output(
        [xcrun, "--sdk", "macosx", "--show-sdk-path"],
        text=True,
        timeout=COMMAND_TIMEOUT_SECONDS,
    ).strip()
    path = Path(value)
    if not path.is_dir():
        raise RuntimeError("xcrun returned an invalid macOS SDK path")
    return path


def swift_compiler() -> str:
    xcrun = shutil.which("xcrun")
    if xcrun is None:
        raise RuntimeError("xcrun is required to build the MV-HEVC encoder")
    value = subprocess.check_output(
        [xcrun, "--find", "swiftc"],
        text=True,
        timeout=COMMAND_TIMEOUT_SECONDS,
    ).strip()
    if not value:
        raise RuntimeError("swiftc is required to build the MV-HEVC encoder")
    return value


def build_command(compiler: str, source_path: Path, output_path: Path, sdk: Path) -> list[str]:
    return [
        compiler,
        "-swift-version",
        "6",
        "-parse-as-library",
        "-O",
        "-whole-module-optimization",
        "-warnings-as-errors",
        "-target",
        f"arm64-apple-macosx{MINIMUM_MACOS}",
        "-sdk",
        str(sdk),
        str(source_path),
        "-o",
        str(output_path),
        "-framework",
        "AVFoundation",
        "-framework",
        "CoreMedia",
        "-framework",
        "CoreVideo",
        "-framework",
        "VideoToolbox",
    ]


def build_encoder(output_path: Path) -> None:
    compiler = swift_compiler()
    sdk = sdk_path()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="mv-hevc-encoder-build-") as temporary_directory:
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        subprocess.run(
            build_command(compiler, SOURCE_PATH, output_path, sdk),
            check=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
            env=environment,
        )

    architecture = subprocess.check_output(
        ["file", str(output_path)],
        text=True,
        timeout=COMMAND_TIMEOUT_SECONDS,
    )
    if "arm64" not in architecture:
        raise RuntimeError("MV-HEVC encoder is not an arm64 executable")
    build_version = subprocess.check_output(
        ["vtool", "-show-build", str(output_path)],
        text=True,
        timeout=COMMAND_TIMEOUT_SECONDS,
    )
    if f"minos {MINIMUM_MACOS}" not in build_version:
        raise RuntimeError(f"MV-HEVC encoder minimum macOS version is not {MINIMUM_MACOS}")
    linked_libraries = subprocess.check_output(
        ["otool", "-L", str(output_path)],
        text=True,
        timeout=COMMAND_TIMEOUT_SECONDS,
    )
    for framework in ("AVFoundation", "CoreMedia", "CoreVideo", "VideoToolbox"):
        if f"/{framework}.framework/" not in linked_libraries:
            raise RuntimeError(f"MV-HEVC encoder is not linked to {framework}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the native direct MV-HEVC prototype encoder.")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("build/mv-hevc-encoder/mv-hevc-encoder"),
        help="Destination for the prototype executable.",
    )
    args = parser.parse_args()

    if platform.system() != "Darwin" or platform.machine() != "arm64":
        parser.error("this build script requires macOS arm64")

    output_path = args.output.resolve()
    build_encoder(output_path)
    print(f"Wrote {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
