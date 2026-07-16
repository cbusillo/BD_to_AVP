#!/usr/bin/env python3

import argparse
import os
import platform
import shlex
import shutil
import subprocess
import tempfile
import tomllib

from dataclasses import dataclass
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPOSITORY_ROOT / "vendor/ssif-probe-macos-arm64.toml"
SOURCE_PATH = REPOSITORY_ROOT / "native/ssif_probe/ssif_probe.c"
COMMAND_TIMEOUT_SECONDS = 120


@dataclass(frozen=True)
class NativeDependency:
    version: str
    pkg_config: str
    source_url: str
    source_sha256: str
    license: str


@dataclass(frozen=True)
class SsifProbeManifest:
    schema_version: int
    platform: str
    minimum_macos: str
    linkage: str
    libbluray: NativeDependency
    libudfread: NativeDependency


def parse_dependency(value: object, name: str) -> NativeDependency:
    if not isinstance(value, dict):
        raise RuntimeError(f"{name} manifest section must be a table")
    expected_fields = {"version", "pkg_config", "source_url", "source_sha256", "license"}
    unexpected_fields = sorted(set(value) - expected_fields)
    if unexpected_fields:
        raise RuntimeError(f"unexpected {name} manifest fields: {', '.join(unexpected_fields)}")
    for field in expected_fields:
        if not isinstance(value.get(field), str) or not value[field]:
            raise RuntimeError(f"{name} manifest field is missing or invalid: {field}")
    return NativeDependency(**{field: value[field] for field in expected_fields})


def load_manifest(path: Path) -> SsifProbeManifest:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    expected_fields = {
        "schema_version",
        "platform",
        "minimum_macos",
        "linkage",
        "libbluray",
        "libudfread",
    }
    unexpected_fields = sorted(set(data) - expected_fields)
    if unexpected_fields:
        raise RuntimeError(f"unexpected SSIF probe manifest fields: {', '.join(unexpected_fields)}")
    if data.get("schema_version") != 1:
        raise RuntimeError("unsupported SSIF probe manifest schema")
    for field in ("platform", "minimum_macos", "linkage"):
        if not isinstance(data.get(field), str) or not data[field]:
            raise RuntimeError(f"SSIF probe manifest field is missing or invalid: {field}")
    return SsifProbeManifest(
        schema_version=data["schema_version"],
        platform=data["platform"],
        minimum_macos=data["minimum_macos"],
        linkage=data["linkage"],
        libbluray=parse_dependency(data.get("libbluray"), "libbluray"),
        libudfread=parse_dependency(data.get("libudfread"), "libudfread"),
    )


def pkg_config(arguments: list[str]) -> str:
    return subprocess.check_output(
        ["pkg-config", *arguments],
        text=True,
        timeout=COMMAND_TIMEOUT_SECONDS,
    ).strip()


def verify_dependency(dependency: NativeDependency) -> None:
    installed_version = pkg_config(["--modversion", dependency.pkg_config])
    if installed_version != dependency.version:
        raise RuntimeError(
            f"{dependency.pkg_config} {dependency.version} is required; found {installed_version or 'nothing'}"
        )


def build_command(
    compiler: str,
    source_path: Path,
    output_path: Path,
    manifest: SsifProbeManifest,
) -> list[str]:
    compile_flags = shlex.split(pkg_config(["--cflags", manifest.libbluray.pkg_config]))
    link_flags = shlex.split(pkg_config(["--libs", manifest.libbluray.pkg_config]))
    return [
        compiler,
        "-std=c11",
        "-O2",
        "-Wall",
        "-Wextra",
        "-Werror",
        f"-mmacosx-version-min={manifest.minimum_macos}",
        *compile_flags,
        str(source_path),
        "-o",
        str(output_path),
        *link_flags,
    ]


def build_ssif_probe(output_path: Path, manifest: SsifProbeManifest) -> None:
    if manifest.linkage != "dynamic-development-only":
        raise RuntimeError(f"unsupported SSIF probe linkage: {manifest.linkage}")
    verify_dependency(manifest.libbluray)
    verify_dependency(manifest.libudfread)
    compiler = os.environ.get("CC") or shutil.which("clang")
    if compiler is None:
        raise RuntimeError("clang is required to build the SSIF probe")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ssif-probe-build-") as temporary_directory:
        build_environment = os.environ.copy()
        build_environment["TMPDIR"] = temporary_directory
        subprocess.run(
            build_command(compiler, SOURCE_PATH, output_path, manifest),
            check=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
            env=build_environment,
        )
    architecture = subprocess.check_output(
        ["file", str(output_path)],
        text=True,
        timeout=COMMAND_TIMEOUT_SECONDS,
    )
    if "arm64" not in architecture:
        raise RuntimeError("SSIF probe is not an arm64 executable")
    build_version = subprocess.check_output(
        ["vtool", "-show-build", str(output_path)],
        text=True,
        timeout=COMMAND_TIMEOUT_SECONDS,
    )
    if f"minos {manifest.minimum_macos}" not in build_version:
        raise RuntimeError(f"SSIF probe minimum macOS version is not {manifest.minimum_macos}")
    linked_libraries = subprocess.check_output(
        ["otool", "-L", str(output_path)],
        text=True,
        timeout=COMMAND_TIMEOUT_SECONDS,
    )
    if "libbluray" not in linked_libraries:
        raise RuntimeError("SSIF probe is not dynamically linked to libbluray")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the development-only direct SSIF probe.")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("build/ssif-probe/ssif_probe"),
        help="Destination for the development-only probe executable.",
    )
    args = parser.parse_args()

    manifest = load_manifest(MANIFEST_PATH)
    if manifest.platform != "macOS arm64":
        raise RuntimeError(f"unsupported SSIF probe platform: {manifest.platform}")
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        parser.error(f"this build script requires {manifest.platform}")

    output_path = args.output.resolve()
    build_ssif_probe(output_path, manifest)
    print(f"Wrote {output_path}")
    print("This dynamically linked binary is for local prototype validation only.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
