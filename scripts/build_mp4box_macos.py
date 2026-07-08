from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shutil
import stat
import subprocess
import tomllib

from dataclasses import dataclass
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BUILD_ROOT = REPO_ROOT / ".vendor" / "gpac"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "bd_to_avp" / "bin"
DEFAULT_MANIFEST_PATH = REPO_ROOT / "vendor" / "mp4box-macos-arm64.toml"


class BuildFailure(RuntimeError):
    pass


def verify_build_host() -> None:
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise BuildFailure("MP4Box vendored build requires macOS arm64")


@dataclass(frozen=True)
class ValidationConfig:
    required_file_substring: str
    required_version_substring: str
    forbidden_link_prefixes: tuple[str, ...]
    expected_system_links: tuple[str, ...]


@dataclass(frozen=True)
class BuildManifest:
    version: str
    repo_url: str
    tag: str
    license_mode: str
    binary: str
    binary_sha256: str
    build: str
    configure_flags: list[str]
    validation: ValidationConfig


def load_manifest(manifest_path: Path = DEFAULT_MANIFEST_PATH) -> BuildManifest:
    data = tomllib.loads(manifest_path.read_text())
    validation = data.get("validation")
    if not isinstance(validation, dict):
        raise ValueError("MP4Box manifest must define a [validation] table")

    return BuildManifest(
        version=require_string(data, "version"),
        repo_url=require_string(data, "repo_url"),
        tag=require_string(data, "tag"),
        license_mode=require_string(data, "license_mode"),
        binary=require_string(data, "binary"),
        binary_sha256=require_string(data, "binary_sha256"),
        build=require_string(data, "build"),
        configure_flags=require_string_list(data, "configure_flags"),
        validation=ValidationConfig(
            required_file_substring=require_string(validation, "required_file_substring"),
            required_version_substring=require_string(validation, "required_version_substring"),
            forbidden_link_prefixes=tuple(require_string_list(validation, "forbidden_link_prefixes")),
            expected_system_links=tuple(require_string_list(validation, "expected_system_links")),
        ),
    )


def require_string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"MP4Box manifest field must be a non-empty string: {key}")
    return value


def require_string_list(data: dict[str, Any], key: str) -> list[str]:
    value = data.get(key)
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"MP4Box manifest field must be a non-empty string list: {key}")
    return value


def run(command: list[str | Path], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> str:
    completed = subprocess.run(
        [str(item) for item in command],
        check=True,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return completed.stdout


def build_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
    env["CC"] = "/usr/bin/clang"
    env["CXX"] = "/usr/bin/clang++"
    env["PKG_CONFIG"] = "/usr/bin/false"
    return env


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def clone_or_update_source(source_dir: Path, manifest: BuildManifest, *, refresh: bool) -> None:
    source_dir.parent.mkdir(parents=True, exist_ok=True)
    if refresh and source_dir.exists():
        shutil.rmtree(source_dir)
    if not source_dir.exists():
        run(["git", "clone", "--depth", "1", "--branch", manifest.tag, manifest.repo_url, source_dir])
        return

    try:
        run(["git", "rev-parse", "--verify", f"{manifest.tag}^{{commit}}"], cwd=source_dir)
    except subprocess.CalledProcessError:
        run(["git", "fetch", "--depth", "1", "origin", "tag", manifest.tag], cwd=source_dir)
    run(["git", "checkout", manifest.tag], cwd=source_dir)


def build_mp4box(source_dir: Path, install_dir: Path, manifest: BuildManifest) -> Path:
    env = build_env()
    if (source_dir / "Makefile").exists():
        run(["make", "distclean"], cwd=source_dir, env=env)
    run(
        ["./configure", f"--prefix={install_dir}", *manifest.configure_flags],
        cwd=source_dir,
        env=env,
    )
    jobs = os.cpu_count() or 1
    run(["make", f"-j{jobs}", "lib"], cwd=source_dir, env=env)
    run(["make", f"-j{jobs}", "apps"], cwd=source_dir, env=env)
    mp4box_path = source_dir / "bin" / "gcc" / manifest.binary
    if not mp4box_path.is_file():
        raise BuildFailure(f"MP4Box build did not produce {mp4box_path}")
    return mp4box_path


def verify_macos_binary(binary_path: Path, validation: ValidationConfig) -> None:
    file_output = run(["file", binary_path])
    if validation.required_file_substring not in file_output:
        raise BuildFailure(f"MP4Box is not an arm64 Mach-O executable:\n{file_output}")

    links = run(["otool", "-L", binary_path])
    linked_paths = [line.strip().split(" ", 1)[0] for line in links.splitlines()[1:] if line.strip()]
    forbidden_links = [path for path in linked_paths if path.startswith(validation.forbidden_link_prefixes)]
    if forbidden_links:
        raise BuildFailure("MP4Box links to non-system libraries:\n" + "\n".join(forbidden_links))

    unexpected_links = sorted(set(linked_paths) - set(validation.expected_system_links))
    if unexpected_links:
        raise BuildFailure("MP4Box links to unexpected libraries:\n" + "\n".join(unexpected_links))

    version_output = run([binary_path, "-version"], env=build_env())
    if validation.required_version_substring not in version_output:
        raise BuildFailure(f"MP4Box version probe did not look valid:\n{version_output}")


def install_binary(binary_path: Path, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "MP4Box"
    shutil.copy2(binary_path, output_path)
    output_path.chmod(output_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return output_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a static arm64 macOS MP4Box for the app bundle.")
    parser.add_argument("--build-root", type=Path, default=DEFAULT_BUILD_ROOT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH)
    parser.add_argument("--refresh", action="store_true", help="Delete and reclone the GPAC source directory first.")
    args = parser.parse_args()

    source_dir = args.build_root / "gpac-src"
    install_dir = args.build_root / "install"
    try:
        verify_build_host()
        manifest = load_manifest(args.manifest)
        clone_or_update_source(source_dir, manifest, refresh=args.refresh)
        mp4box_path = build_mp4box(source_dir, install_dir, manifest)
        verify_macos_binary(mp4box_path, manifest.validation)
        output_path = install_binary(mp4box_path, args.output_dir)
        print(f"Built MP4Box {manifest.tag}: {output_path} ({sha256(output_path)})")
    except (BuildFailure, subprocess.CalledProcessError) as error:
        print(f"MP4Box build failed: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
