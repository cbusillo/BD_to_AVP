import hashlib
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from scripts import update_ffmpeg_manifest, vendor_ffmpeg_macos


class UpdateFfmpegManifestTests(unittest.TestCase):
    def test_render_manifest_is_deterministic(self) -> None:
        manifest = update_ffmpeg_manifest.UpdatedManifest(
            version="8.1.3",
            base_url="https://example.invalid/8.1.3",
            license_mode="GPLv3",
            build="test build",
            assets=[
                update_ffmpeg_manifest.UpdatedAsset(
                    name="ffmpeg",
                    zip_sha256="0" * 64,
                    binary_sha256="1" * 64,
                )
            ],
        )

        self.assertEqual(
            update_ffmpeg_manifest.render_manifest(manifest),
            "\n".join(
                [
                    'version = "8.1.3"',
                    'base_url = "https://example.invalid/8.1.3"',
                    'license_mode = "GPLv3"',
                    'build = "test build"',
                    "",
                    "[[assets]]",
                    'name = "ffmpeg"',
                    f'zip_sha256 = "{"0" * 64}"',
                    f'binary_sha256 = "{"1" * 64}"',
                    "",
                ]
            ),
        )

    def test_build_candidate_manifest_downloads_and_hashes_assets(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source_dir = Path(temp_dir) / "source"
            source_dir.mkdir()
            archive_path = source_dir / "ffmpeg.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("nested/ffmpeg", b"binary")
            expected_zip_sha256 = hashlib.sha256(archive_path.read_bytes()).hexdigest()

            def copy_archive(_url: str, destination: Path) -> None:
                destination.write_bytes(archive_path.read_bytes())

            with patch("scripts.vendor_ffmpeg_macos.download", side_effect=copy_archive):
                manifest = update_ffmpeg_manifest.build_candidate_manifest(
                    version="8.1.3",
                    base_url="https://example.invalid/build",
                    license_mode="GPLv3",
                    build="test build",
                    asset_names=["ffmpeg"],
                )

        self.assertEqual(manifest.version, "8.1.3")
        self.assertEqual(manifest.assets[0].zip_sha256, expected_zip_sha256)
        self.assertEqual(manifest.assets[0].binary_sha256, hashlib.sha256(b"binary").hexdigest())

    def test_written_manifest_loads_in_vendor_script(self) -> None:
        manifest = update_ffmpeg_manifest.UpdatedManifest(
            version="8.1.3",
            base_url="https://example.invalid/8.1.3",
            license_mode="GPLv3",
            build="test build",
            assets=[
                update_ffmpeg_manifest.UpdatedAsset(
                    name="ffmpeg",
                    zip_sha256="0" * 64,
                    binary_sha256="1" * 64,
                )
            ],
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            manifest_path = Path(temp_dir) / "ffmpeg.toml"
            update_ffmpeg_manifest.write_manifest(manifest_path, manifest)
            loaded_manifest = vendor_ffmpeg_macos.load_manifest(manifest_path)

        self.assertEqual(loaded_manifest.version, "8.1.3")
        self.assertEqual(loaded_manifest.assets[0].name, "ffmpeg")
        self.assertEqual(loaded_manifest.assets[0].binary_sha256, "1" * 64)

    def test_written_manifest_escapes_toml_strings(self) -> None:
        manifest = update_ffmpeg_manifest.UpdatedManifest(
            version='8.1.3 "quoted"',
            base_url="https://example.invalid/build\\path",
            license_mode="GPLv3",
            build='build with "quotes" and \\slashes',
            assets=[
                update_ffmpeg_manifest.UpdatedAsset(
                    name="ffmpeg",
                    zip_sha256="0" * 64,
                    binary_sha256="1" * 64,
                )
            ],
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            manifest_path = Path(temp_dir) / "ffmpeg.toml"
            update_ffmpeg_manifest.write_manifest(manifest_path, manifest)
            loaded_manifest = vendor_ffmpeg_macos.load_manifest(manifest_path)

        self.assertEqual(loaded_manifest.version, '8.1.3 "quoted"')
        self.assertEqual(loaded_manifest.base_url, "https://example.invalid/build\\path")
        self.assertEqual(loaded_manifest.build, 'build with "quotes" and \\slashes')


if __name__ == "__main__":
    unittest.main()
