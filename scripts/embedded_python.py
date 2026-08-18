from __future__ import annotations

import argparse
import fcntl
import hashlib
import os
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.parse
import urllib.request
import zipfile

from collections.abc import Iterator
from dataclasses import dataclass
from contextlib import contextmanager
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPO_ROOT / "vendor" / "python-runtime-macos-arm64.toml"
CACHE_ROOT = REPO_ROOT / ".vendor" / "python-runtime"
RUNTIME_ROOT = REPO_ROOT / "build" / "embedded-python" / "macos-arm64"
WORKER_EXECUTABLE_NAME = "BluRayToVisionProEngine"
USER_AGENT = "BD_to_AVP embedded Python runtime builder"
DOWNLOAD_TIMEOUT_SECONDS = 60
PURE_PYTHON_SOURCE_ALLOWLIST = ("pysrt",)


class EmbeddedPythonError(RuntimeError):
    pass


@dataclass(frozen=True)
class RuntimeAsset:
    url: str
    sha256: str

    @property
    def archive_name(self) -> str:
        return Path(urllib.parse.urlparse(self.url).path).name


@dataclass(frozen=True)
class RuntimeManifest:
    python_version: str
    python_abi: str
    architecture: str
    minimum_macos_version: str
    framework_source_path: Path
    support: RuntimeAsset
    launcher: RuntimeAsset
    launcher_member: str


@dataclass(frozen=True)
class RuntimeLayout:
    root: Path
    framework: Path
    launcher: Path
    app: Path
    app_packages: Path


@contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def require_string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise EmbeddedPythonError(f"Embedded Python manifest field must be a non-empty string: {key}")
    return value


def load_manifest(path: Path = MANIFEST_PATH) -> RuntimeManifest:
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    if data.get("schema_version") != 1:
        raise EmbeddedPythonError("Embedded Python manifest schema_version must be 1.")
    support = data.get("support")
    launcher = data.get("launcher")
    if not isinstance(support, dict) or not isinstance(launcher, dict):
        raise EmbeddedPythonError("Embedded Python manifest must define support and launcher tables.")
    manifest = RuntimeManifest(
        python_version=require_string(data, "python_version"),
        python_abi=require_string(data, "python_abi"),
        architecture=require_string(data, "architecture"),
        minimum_macos_version=require_string(data, "minimum_macos_version"),
        framework_source_path=Path(require_string(data, "framework_source_path")),
        support=RuntimeAsset(url=require_string(support, "url"), sha256=require_string(support, "sha256")),
        launcher=RuntimeAsset(url=require_string(launcher, "url"), sha256=require_string(launcher, "sha256")),
        launcher_member=require_string(launcher, "archive_member"),
    )
    if manifest.architecture != "arm64":
        raise EmbeddedPythonError("Embedded Python runtime architecture must be arm64.")
    if manifest.framework_source_path.is_absolute() or ".." in manifest.framework_source_path.parts:
        raise EmbeddedPythonError("Embedded Python framework_source_path must stay within the support archive.")
    for asset in (manifest.support, manifest.launcher):
        if urllib.parse.urlparse(asset.url).scheme != "https":
            raise EmbeddedPythonError(f"Embedded Python downloads must use HTTPS: {asset.url}")
        if re.fullmatch(r"[0-9a-f]{64}", asset.sha256) is None:
            raise EmbeddedPythonError(f"Embedded Python SHA-256 is invalid: {asset.sha256!r}")
    return manifest


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_archive(path: Path, asset: RuntimeAsset) -> None:
    actual_sha256 = sha256_file(path)
    if actual_sha256 != asset.sha256:
        raise EmbeddedPythonError(
            f"Embedded Python archive digest mismatch for {path.name}: expected {asset.sha256}, got {actual_sha256}."
        )


def download_archive(asset: RuntimeAsset, cache_root: Path = CACHE_ROOT) -> Path:
    cache_root.mkdir(parents=True, exist_ok=True)
    archive_path = cache_root / asset.archive_name
    if archive_path.exists():
        try:
            verify_archive(archive_path, asset)
            return archive_path
        except EmbeddedPythonError:
            archive_path.unlink()

    temporary_path = archive_path.with_suffix(f"{archive_path.suffix}.tmp")
    temporary_path.unlink(missing_ok=True)
    request = urllib.request.Request(asset.url, headers={"User-Agent": USER_AGENT})
    try:
        with (
            urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response,
            temporary_path.open("wb") as output,
        ):
            shutil.copyfileobj(response, output)
        verify_archive(temporary_path, asset)
        os.replace(temporary_path, archive_path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return archive_path


def validate_tar_members(archive: tarfile.TarFile, destination: Path) -> None:
    extraction_root = destination.resolve()
    for member in archive.getmembers():
        member_path = Path(member.name)
        if member_path.is_absolute():
            raise EmbeddedPythonError(f"Python support archive contains an absolute path: {member.name}")
        extracted_path = (extraction_root / member_path).resolve()
        if not extracted_path.is_relative_to(extraction_root):
            raise EmbeddedPythonError(f"Python support archive path escapes the extraction root: {member.name}")
        if member.issym() or member.islnk():
            link_path = Path(member.linkname)
            if link_path.is_absolute():
                raise EmbeddedPythonError(f"Python support archive contains an absolute link: {member.name}")
            link_target = extracted_path.parent / link_path if member.issym() else extraction_root / link_path
            if not link_target.resolve().is_relative_to(extraction_root):
                raise EmbeddedPythonError(f"Python support archive link escapes the extraction root: {member.name}")


def verify_framework(framework_path: Path, manifest: RuntimeManifest) -> None:
    executable = framework_path / "Python"
    info_path = framework_path / "Versions" / manifest.python_abi / "Resources" / "Info.plist"
    if not executable.exists() or not info_path.is_file():
        raise EmbeddedPythonError(f"Python framework is incomplete: {framework_path}")
    with info_path.open("rb") as handle:
        info = plistlib.load(handle)
    if info.get("CFBundleVersion") != manifest.python_version:
        raise EmbeddedPythonError(
            f"Python framework version must be {manifest.python_version}; found {info.get('CFBundleVersion')!r}."
        )


def _extract_framework_unlocked(
    archive_path: Path,
    manifest: RuntimeManifest,
    cache_root: Path = CACHE_ROOT,
) -> Path:
    support_root = cache_root / f"python-{manifest.python_version}-{manifest.support.sha256[:12]}"
    framework_path = support_root / "Python.framework"
    marker_path = support_root / ".archive-sha256"
    if (
        marker_path.is_file()
        and marker_path.read_text(encoding="utf-8").strip() == manifest.support.sha256
        and (framework_path / "Python").exists()
    ):
        verify_framework(framework_path, manifest)
        return framework_path

    cache_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=cache_root, prefix="python-support-") as temporary_directory:
        temporary_root = Path(temporary_directory)
        with tarfile.open(archive_path, mode="r:gz") as archive:
            validate_tar_members(archive, temporary_root)
            archive.extractall(temporary_root, filter="data")
        source_framework = temporary_root / manifest.framework_source_path
        verify_framework(source_framework, manifest)
        staged_support_root = temporary_root / "staged-support"
        shutil.copytree(source_framework, staged_support_root / "Python.framework", symlinks=True)
        (staged_support_root / ".archive-sha256").write_text(manifest.support.sha256 + "\n", encoding="utf-8")
        shutil.rmtree(support_root, ignore_errors=True)
        shutil.move(staged_support_root.as_posix(), support_root.as_posix())
    verify_framework(framework_path, manifest)
    return framework_path


def extract_framework(
    archive_path: Path,
    manifest: RuntimeManifest,
    cache_root: Path = CACHE_ROOT,
) -> Path:
    lock_path = cache_root / f".python-{manifest.python_version}-{manifest.support.sha256[:12]}.lock"
    with exclusive_lock(lock_path):
        return _extract_framework_unlocked(archive_path, manifest, cache_root)


def extract_launcher(archive_path: Path, manifest: RuntimeManifest, output_path: Path) -> None:
    with zipfile.ZipFile(archive_path) as archive:
        matching_members = [member for member in archive.infolist() if member.filename == manifest.launcher_member]
        if len(matching_members) != 1:
            raise EmbeddedPythonError(
                f"Python launcher archive must contain exactly one {manifest.launcher_member!r} member."
            )
        member = matching_members[0]
        if member.is_dir() or stat.S_ISLNK(member.external_attr >> 16):
            raise EmbeddedPythonError("Python launcher archive member must be a regular file.")
        universal_launcher = output_path.with_suffix(".universal")
        universal_launcher.write_bytes(archive.read(member))
    try:
        subprocess.run(
            ["lipo", universal_launcher, "-thin", manifest.architecture, "-output", output_path],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as error:
        raise EmbeddedPythonError(f"Unable to extract the arm64 Python launcher: {error.stderr.strip()}") from error
    finally:
        universal_launcher.unlink(missing_ok=True)
    output_path.chmod(output_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def export_locked_requirements(output_path: Path, *, only_group: str | None = None) -> None:
    group_arguments = ["--only-group", only_group] if only_group is not None else ["--no-default-groups"]
    subprocess.run(
        [
            "uv",
            "export",
            "--locked",
            *group_arguments,
            "--no-emit-project",
            "--no-annotate",
            "--no-header",
            "--output-file",
            str(output_path),
        ],
        check=True,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )


def install_locked_dependencies(
    destination: Path,
    manifest: RuntimeManifest,
    *,
    requirements_path: Path,
    build_constraints_path: Path,
) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--target",
            str(destination),
            "--requirements",
            str(requirements_path),
            "--build-constraints",
            str(build_constraints_path),
            "--python-version",
            manifest.python_abi,
            "--python-platform",
            "aarch64-apple-darwin",
            "--only-binary",
            ":all:",
            *[argument for package in PURE_PYTHON_SOURCE_ALLOWLIST for argument in ("--no-binary", package)],
            "--require-hashes",
            "--link-mode",
            "copy",
        ],
        check=True,
        cwd=REPO_ROOT,
    )
    shutil.rmtree(destination / "bin", ignore_errors=True)
    for cache_path in destination.rglob("__pycache__"):
        shutil.rmtree(cache_path, ignore_errors=True)
    forbidden = [path for name in ("PySide6", "shiboken6") if (path := destination / name).exists()]
    if forbidden:
        raise EmbeddedPythonError("GUI dependencies must not be installed in the worker runtime.")


def vendor_macos_tools() -> None:
    subprocess.run([sys.executable, "scripts/vendor_ffmpeg_macos.py"], check=True, cwd=REPO_ROOT)


def copy_application_source(destination: Path) -> None:
    source = REPO_ROOT / "bd_to_avp"
    shutil.copytree(
        source,
        destination / "bd_to_avp",
        symlinks=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
    )


def executable_architectures(path: Path) -> set[str]:
    result = subprocess.run(["lipo", "-archs", path], check=True, capture_output=True, text=True)
    return set(result.stdout.split())


def verify_launcher(path: Path, manifest: RuntimeManifest) -> None:
    if executable_architectures(path) != {manifest.architecture}:
        raise EmbeddedPythonError(f"Embedded Python launcher must be {manifest.architecture}-only: {path}")
    linked = subprocess.run(["otool", "-L", path], check=True, capture_output=True, text=True).stdout
    expected_python = f"@rpath/Python.framework/Versions/{manifest.python_abi}/Python"
    if expected_python not in linked:
        raise EmbeddedPythonError(f"Embedded Python launcher does not link {expected_python}.")
    build = subprocess.run(["vtool", "-show-build", path], check=True, capture_output=True, text=True).stdout
    versions = set(re.findall(r"^\s*minos\s+(\d+(?:\.\d+){1,2})\s*$", build, re.MULTILINE))
    if versions != {manifest.minimum_macos_version}:
        raise EmbeddedPythonError(
            f"Embedded Python launcher must target macOS {manifest.minimum_macos_version}; found {sorted(versions)}."
        )


def runtime_layout(root: Path = RUNTIME_ROOT) -> RuntimeLayout:
    return RuntimeLayout(
        root=root,
        framework=root / "Frameworks" / "Python.framework",
        launcher=root / "MacOS" / WORKER_EXECUTABLE_NAME,
        app=root / "Resources" / "app",
        app_packages=root / "Resources" / "app_packages",
    )


def prepare_runtime(
    *,
    manifest_path: Path = MANIFEST_PATH,
    cache_root: Path = CACHE_ROOT,
    runtime_root: Path = RUNTIME_ROOT,
) -> RuntimeLayout:
    manifest = load_manifest(manifest_path)
    support_archive = download_archive(manifest.support, cache_root)
    launcher_archive = download_archive(manifest.launcher, cache_root)
    cached_framework = extract_framework(support_archive, manifest, cache_root)
    vendor_macos_tools()

    runtime_root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=runtime_root.parent, prefix="embedded-python-") as temporary_directory:
        temporary_root = Path(temporary_directory) / "runtime"
        layout = runtime_layout(temporary_root)
        layout.framework.parent.mkdir(parents=True, exist_ok=True)
        layout.launcher.parent.mkdir(parents=True, exist_ok=True)
        layout.app.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(cached_framework, layout.framework, symlinks=True)
        extract_launcher(launcher_archive, manifest, layout.launcher)
        copy_application_source(layout.app)
        requirements_path = Path(temporary_directory) / "requirements.txt"
        build_constraints_path = Path(temporary_directory) / "build-constraints.txt"
        export_locked_requirements(requirements_path)
        export_locked_requirements(build_constraints_path, only_group="runtime-build")
        install_locked_dependencies(
            layout.app_packages,
            manifest,
            requirements_path=requirements_path,
            build_constraints_path=build_constraints_path,
        )
        verify_launcher(layout.launcher, manifest)
        shutil.rmtree(runtime_root, ignore_errors=True)
        shutil.move(temporary_root.as_posix(), runtime_root.as_posix())

    prepared = runtime_layout(runtime_root)
    if not (prepared.framework / "Python").exists() or not prepared.launcher.is_file():
        raise EmbeddedPythonError(f"Embedded Python runtime is incomplete: {runtime_root}")
    return prepared


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the pinned embedded Python worker runtime.")
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    parser.add_argument("--cache", type=Path, default=CACHE_ROOT)
    parser.add_argument("--output", type=Path, default=RUNTIME_ROOT)
    args = parser.parse_args()
    print(prepare_runtime(manifest_path=args.manifest, cache_root=args.cache, runtime_root=args.output).root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
