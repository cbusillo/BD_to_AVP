from __future__ import annotations

import argparse
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from scripts import vendor_ffmpeg_macos


DEFAULT_MANIFEST_PATH = vendor_ffmpeg_macos.DEFAULT_MANIFEST_PATH
DEFAULT_ASSET_NAMES = ["ffmpeg", "ffprobe"]


@dataclass(frozen=True)
class UpdatedAsset:
    name: str
    zip_sha256: str
    binary_sha256: str


@dataclass(frozen=True)
class UpdatedManifest:
    version: str
    base_url: str
    license_mode: str
    build: str
    assets: list[UpdatedAsset]


def updated_asset_from_existing(asset: vendor_ffmpeg_macos.BinaryAsset) -> UpdatedAsset:
    return UpdatedAsset(
        name=asset.name,
        zip_sha256=asset.zip_sha256,
        binary_sha256=asset.binary_sha256,
    )


def build_candidate_manifest(
    *,
    version: str,
    base_url: str,
    license_mode: str,
    build: str,
    asset_names: list[str],
) -> UpdatedManifest:
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        archive_dir = temp_path / "archives"
        output_dir = temp_path / "bin"
        archive_dir.mkdir()
        output_dir.mkdir()
        assets: list[UpdatedAsset] = []

        for asset_name in asset_names:
            url = f"{base_url.rstrip('/')}/{asset_name}.zip"
            archive_path = archive_dir / f"{asset_name}.zip"
            vendor_ffmpeg_macos.download(url, archive_path)
            zip_sha256 = vendor_ffmpeg_macos.sha256(archive_path)
            binary_path = extract_asset_binary(asset_name, archive_path, output_dir)
            binary_sha256 = vendor_ffmpeg_macos.sha256(binary_path)
            assets.append(UpdatedAsset(name=asset_name, zip_sha256=zip_sha256, binary_sha256=binary_sha256))

    return UpdatedManifest(
        version=version,
        base_url=base_url.rstrip("/"),
        license_mode=license_mode,
        build=build,
        assets=assets,
    )


def merge_manifest_assets(
    old_manifest: vendor_ffmpeg_macos.VendorManifest,
    new_manifest: UpdatedManifest,
) -> UpdatedManifest:
    if new_manifest.base_url != old_manifest.base_url and len(new_manifest.assets) < len(old_manifest.assets):
        raise ValueError("Partial FFmpeg manifest updates must keep the existing base URL")

    updated_assets = {asset.name: asset for asset in new_manifest.assets}
    merged_assets = [
        updated_assets.get(asset.name, updated_asset_from_existing(asset)) for asset in old_manifest.assets
    ]
    known_asset_names = {asset.name for asset in old_manifest.assets}
    merged_assets.extend(asset for asset in new_manifest.assets if asset.name not in known_asset_names)

    return UpdatedManifest(
        version=new_manifest.version,
        base_url=new_manifest.base_url,
        license_mode=new_manifest.license_mode,
        build=new_manifest.build,
        assets=merged_assets,
    )


def extract_asset_binary(asset_name: str, archive_path: Path, output_dir: Path) -> Path:
    with zipfile.ZipFile(archive_path) as archive:
        matches = [name for name in archive.namelist() if Path(name).name == asset_name]
        if len(matches) != 1:
            raise ValueError(f"Expected one {asset_name} binary in {archive_path.name}, found {matches}")
        extracted_path = Path(archive.extract(matches[0], output_dir))

    output_path = output_dir / asset_name
    if extracted_path != output_path:
        output_path.unlink(missing_ok=True)
        extracted_path.replace(output_path)
    return output_path


def render_manifest(manifest: UpdatedManifest) -> str:
    lines = [
        toml_string("version", manifest.version),
        toml_string("base_url", manifest.base_url),
        toml_string("license_mode", manifest.license_mode),
        toml_string("build", manifest.build),
        "",
    ]
    for asset in manifest.assets:
        lines.extend(
            [
                "[[assets]]",
                toml_string("name", asset.name),
                toml_string("zip_sha256", asset.zip_sha256),
                toml_string("binary_sha256", asset.binary_sha256),
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def toml_string(key: str, value: str) -> str:
    escaped_value = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'{key} = "{escaped_value}"'


def write_manifest(manifest_path: Path, manifest: UpdatedManifest) -> None:
    manifest_path.write_text(render_manifest(manifest))


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh the app-local macOS arm64 FFmpeg vendor manifest.")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH)
    parser.add_argument("--version", required=True, help="FFmpeg version represented by the candidate build.")
    parser.add_argument("--base-url", required=True, help="Base URL containing ffmpeg.zip and ffprobe.zip.")
    parser.add_argument("--license-mode", default="GPLv3")
    parser.add_argument(
        "--build",
        default="static macOS arm64 build from ffmpeg.martin-riedl.de",
        help="Human-readable build/source description stored in the manifest.",
    )
    parser.add_argument("--asset", dest="asset_names", action="append", choices=DEFAULT_ASSET_NAMES)
    args = parser.parse_args()

    asset_names = args.asset_names or DEFAULT_ASSET_NAMES
    old_manifest = vendor_ffmpeg_macos.load_manifest(args.manifest)
    new_manifest = build_candidate_manifest(
        version=args.version,
        base_url=args.base_url,
        license_mode=args.license_mode,
        build=args.build,
        asset_names=asset_names,
    )
    new_manifest = merge_manifest_assets(old_manifest, new_manifest)
    write_manifest(args.manifest, new_manifest)
    print(f"Updated FFmpeg manifest: {old_manifest.version} -> {new_manifest.version}")
    print(f"Old base URL: {old_manifest.base_url}")
    print(f"New base URL: {new_manifest.base_url}")
    for asset in new_manifest.assets:
        print(f"{asset.name}: zip={asset.zip_sha256} binary={asset.binary_sha256}")


if __name__ == "__main__":
    main()
