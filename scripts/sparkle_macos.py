from __future__ import annotations

import argparse
import hashlib
import os
import plistlib
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
import urllib.parse
import urllib.request

from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPO_ROOT / "vendor" / "sparkle-macos.toml"
CACHE_ROOT = REPO_ROOT / ".vendor" / "sparkle"
APP_PATH = REPO_ROOT / "build" / "bd-to-avp" / "macos" / "app" / "3D Blu-ray to Vision Pro.app"
FRAMEWORK_RELATIVE_PATH = Path("Contents/Frameworks/Sparkle.framework")
REQUIRED_FRAMEWORK_PATHS = (
    Path("Versions/B/Sparkle"),
    Path("Versions/B/Updater.app"),
    Path("Versions/B/XPCServices/Downloader.xpc"),
    Path("Versions/B/XPCServices/Installer.xpc"),
)


class SparkleBuildError(RuntimeError):
    pass


@dataclass(frozen=True)
class SparkleRelease:
    version: str
    archive_url: str
    archive_sha256: str

    @property
    def archive_name(self) -> str:
        return Path(urllib.parse.urlparse(self.archive_url).path).name


def load_release(manifest_path: Path = MANIFEST_PATH) -> SparkleRelease:
    with manifest_path.open("rb") as handle:
        data = tomllib.load(handle)
    release = SparkleRelease(
        version=str(data["version"]),
        archive_url=str(data["archive_url"]),
        archive_sha256=str(data["archive_sha256"]),
    )
    if urllib.parse.urlparse(release.archive_url).scheme != "https":
        raise SparkleBuildError("Sparkle archive URL must use HTTPS.")
    if len(release.archive_sha256) != 64:
        raise SparkleBuildError("Sparkle archive SHA-256 must contain 64 hexadecimal characters.")
    try:
        int(release.archive_sha256, 16)
    except ValueError as error:
        raise SparkleBuildError("Sparkle archive SHA-256 is not hexadecimal.") from error
    return release


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_archive(path: Path, release: SparkleRelease) -> None:
    actual_sha256 = sha256_file(path)
    if actual_sha256 != release.archive_sha256:
        raise SparkleBuildError(
            f"Sparkle archive digest mismatch: expected {release.archive_sha256}, got {actual_sha256}."
        )


def download_archive(release: SparkleRelease, cache_root: Path = CACHE_ROOT) -> Path:
    cache_root.mkdir(parents=True, exist_ok=True)
    archive_path = cache_root / release.archive_name
    if archive_path.exists():
        try:
            verify_archive(archive_path, release)
            return archive_path
        except SparkleBuildError:
            archive_path.unlink()

    temporary_path = archive_path.with_suffix(f"{archive_path.suffix}.tmp")
    temporary_path.unlink(missing_ok=True)
    try:
        with urllib.request.urlopen(release.archive_url, timeout=60) as response, temporary_path.open("wb") as output:
            shutil.copyfileobj(response, output)
        verify_archive(temporary_path, release)
        os.replace(temporary_path, archive_path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return archive_path


def _validate_archive_members(archive: tarfile.TarFile, destination: Path) -> None:
    destination = destination.resolve()
    for member in archive.getmembers():
        member_path = Path(member.name)
        if member_path.is_absolute():
            raise SparkleBuildError(f"Sparkle archive contains an absolute path: {member.name}")
        extracted_path = (destination / member_path).resolve()
        if not extracted_path.is_relative_to(destination):
            raise SparkleBuildError(f"Sparkle archive path escapes the extraction root: {member.name}")
        if member.issym() or member.islnk():
            link_path = Path(member.linkname)
            if link_path.is_absolute():
                raise SparkleBuildError(f"Sparkle archive contains an absolute link: {member.name}")
            link_target = extracted_path.parent / link_path if member.issym() else destination / link_path
            if not link_target.resolve().is_relative_to(destination):
                raise SparkleBuildError(f"Sparkle archive link escapes the extraction root: {member.name}")


def extract_archive(
    archive_path: Path,
    release: SparkleRelease,
    cache_root: Path = CACHE_ROOT,
    *,
    force: bool = False,
) -> Path:
    extract_root = cache_root / release.version
    marker_path = extract_root / ".archive-sha256"
    framework_path = extract_root / "Sparkle.framework"
    if not force and marker_path.exists() and marker_path.read_text(encoding="utf-8").strip() == release.archive_sha256:
        try:
            verify_framework_layout(framework_path, expected_version=release.version)
            return framework_path
        except SparkleBuildError:
            shutil.rmtree(extract_root, ignore_errors=True)

    cache_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=cache_root, prefix="sparkle-extract-") as temporary_dir:
        temporary_root = Path(temporary_dir)
        with tarfile.open(archive_path, mode="r:xz") as archive:
            _validate_archive_members(archive, temporary_root)
            archive.extractall(temporary_root, filter="data")
        temporary_framework = temporary_root / "Sparkle.framework"
        verify_framework_layout(temporary_framework, expected_version=release.version)
        (temporary_root / ".archive-sha256").write_text(release.archive_sha256 + "\n", encoding="utf-8")
        shutil.rmtree(extract_root, ignore_errors=True)
        shutil.move(temporary_root.as_posix(), extract_root.as_posix())

    verify_framework_layout(framework_path, expected_version=release.version)
    return framework_path


def verify_framework_layout(framework_path: Path, *, expected_version: str | None = None) -> None:
    if not framework_path.is_dir():
        raise SparkleBuildError(f"Sparkle framework not found: {framework_path}")
    for required_path in REQUIRED_FRAMEWORK_PATHS:
        candidate = framework_path / required_path
        if not candidate.exists():
            raise SparkleBuildError(f"Sparkle framework is missing {required_path}.")
    info_path = framework_path / "Versions/B/Resources/Info.plist"
    if not info_path.is_file():
        raise SparkleBuildError("Sparkle framework is missing its Info.plist.")
    with info_path.open("rb") as handle:
        info = plistlib.load(handle)
    if info.get("CFBundleIdentifier") != "org.sparkle-project.Sparkle":
        raise SparkleBuildError("Sparkle framework has an unexpected bundle identifier.")
    if expected_version is not None and info.get("CFBundleShortVersionString") != expected_version:
        raise SparkleBuildError(
            f"Sparkle framework version must be {expected_version}; found {info.get('CFBundleShortVersionString')!r}."
        )


def verify_framework_architecture(framework_path: Path) -> None:
    executable_path = framework_path / "Versions/B/Sparkle"
    result = subprocess.run(
        ["lipo", "-archs", executable_path],
        check=True,
        capture_output=True,
        text=True,
    )
    if "arm64" not in result.stdout.split():
        raise SparkleBuildError("Sparkle framework does not contain an arm64 slice.")


def embed_sparkle(
    app_path: Path = APP_PATH,
    *,
    release: SparkleRelease | None = None,
    cache_root: Path = CACHE_ROOT,
    verify_architecture: bool = True,
    force_extract: bool = False,
) -> Path:
    if not app_path.is_dir():
        raise SparkleBuildError(f"Briefcase app bundle not found: {app_path}")
    release = release or load_release()
    archive_path = download_archive(release, cache_root)
    source_framework = extract_archive(archive_path, release, cache_root, force=force_extract)
    if verify_architecture:
        verify_framework_architecture(source_framework)

    destination = app_path / FRAMEWORK_RELATIVE_PATH
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(destination, ignore_errors=True)
    shutil.copytree(source_framework, destination, symlinks=True, copy_function=shutil.copy2)
    verify_framework_layout(destination, expected_version=release.version)
    return destination


def sparkle_tool_path(
    tool_name: str,
    *,
    release: SparkleRelease | None = None,
    cache_root: Path = CACHE_ROOT,
) -> Path:
    release = release or load_release()
    archive_path = download_archive(release, cache_root)
    framework_path = extract_archive(archive_path, release, cache_root, force=True)
    tool_path = framework_path.parent / "bin" / tool_name
    if not tool_path.is_file() or not os.access(tool_path, os.X_OK):
        raise SparkleBuildError(f"Sparkle tool is missing or not executable: {tool_path}")
    return tool_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Embed the pinned Sparkle framework into the Briefcase app bundle.")
    parser.add_argument("--app", type=Path, default=APP_PATH, help="Path to the Briefcase .app bundle.")
    parser.add_argument("--cache", type=Path, default=CACHE_ROOT, help="Sparkle download and extraction cache.")
    parser.add_argument("--tool", help="Prepare Sparkle and print the path to a bundled command-line tool.")
    args = parser.parse_args()

    if args.tool:
        print(sparkle_tool_path(args.tool, cache_root=args.cache))
        return 0
    destination = embed_sparkle(args.app, cache_root=args.cache)
    print(f"Embedded Sparkle {load_release().version}: {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
