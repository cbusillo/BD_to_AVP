from __future__ import annotations

import argparse
import hashlib
import shutil
import stat
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CACHE_DIR = REPO_ROOT / ".vendor" / "ffmpeg"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "bd_to_avp" / "bin"
DOWNLOAD_BASE_URL = "https://ffmpeg.martin-riedl.de/download/macos/arm64/1783011502_8.1.2"
FFMPEG_VERSION = "8.1.2"
USER_AGENT = "BD_to_AVP ffmpeg vendor script"
DOWNLOAD_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class BinaryAsset:
    name: str
    zip_sha256: str
    binary_sha256: str

    @property
    def url(self) -> str:
        return f"{DOWNLOAD_BASE_URL}/{self.name}.zip"

    @property
    def archive_name(self) -> str:
        return f"{self.name}.zip"


ASSETS = [
    BinaryAsset(
        name="ffmpeg",
        zip_sha256="ef1aa60006c7b77ce170c1608c08d8e4ba1c30c5746f2ac986ded932d0ac2c3c",
        binary_sha256="eaf91238e104dd0e262bc6510e25061855cc99a6955a721b0ac99660d58c473d",
    ),
    BinaryAsset(
        name="ffprobe",
        zip_sha256="c39787f4af7a3932502d2d48db6f6feaaa836b48a73ef78c32cc3285df61dfaf",
        binary_sha256="ed9dc5871914b466b96b402c9ec0ba68ce4f836e72faa464b1b4e279835bd4a6",
    ),
]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response, destination.open("wb") as file:
        shutil.copyfileobj(response, file)


def verify_archive(path: Path, expected_sha256: str) -> None:
    actual_sha256 = sha256(path)
    if actual_sha256 != expected_sha256:
        raise ValueError(f"Checksum mismatch for {path.name}: expected {expected_sha256}, got {actual_sha256}")


def extract_binary(asset: BinaryAsset, archive_path: Path, output_dir: Path) -> Path:
    with zipfile.ZipFile(archive_path) as archive:
        names = [name for name in archive.namelist() if Path(name).name == asset.name]
        if len(names) != 1:
            raise ValueError(f"Expected one {asset.name} binary in {archive_path.name}, found {names}")
        extracted_path = Path(archive.extract(names[0], output_dir))

    output_path = output_dir / asset.name
    if extracted_path != output_path:
        output_path.unlink(missing_ok=True)
        extracted_path.replace(output_path)
        for parent in reversed(extracted_path.parents):
            if parent == output_dir or not parent.exists():
                break
            try:
                parent.rmdir()
            except OSError:
                break

    current_mode = output_path.stat().st_mode
    output_path.chmod(current_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return output_path


def verify_binary(path: Path, expected_sha256: str) -> None:
    actual_sha256 = sha256(path)
    if actual_sha256 != expected_sha256:
        raise ValueError(
            f"Checksum mismatch for extracted {path.name}: expected {expected_sha256}, got {actual_sha256}"
        )


def vendor_asset(asset: BinaryAsset, cache_dir: Path, output_dir: Path, refresh: bool) -> Path:
    archive_path = cache_dir / asset.archive_name
    if refresh or not archive_path.exists():
        download(asset.url, archive_path)
    verify_archive(archive_path, asset.zip_sha256)
    output_path = extract_binary(asset, archive_path, output_dir)
    verify_binary(output_path, asset.binary_sha256)
    return output_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Vendor static macOS arm64 FFmpeg tools into bd_to_avp/bin.")
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--refresh", action="store_true", help="Download archives even when cached copies exist.")
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    for asset in ASSETS:
        output_path = vendor_asset(asset, args.cache_dir, args.output_dir, args.refresh)
        print(f"Vendored {asset.name} {FFMPEG_VERSION}: {output_path} ({sha256(output_path)})")


if __name__ == "__main__":
    main()
