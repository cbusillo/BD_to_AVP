import hashlib
import tempfile
import tomllib
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

    def test_render_manifest_escapes_control_characters(self) -> None:
        manifest = update_ffmpeg_manifest.UpdatedManifest(
            version="8.1.3\nrc",
            base_url="https://example.invalid/8.1.3",
            license_mode="GPLv3\tverified",
            build='quoted "build"',
            assets=[],
        )

        rendered = update_ffmpeg_manifest.render_manifest(manifest)

        self.assertEqual(tomllib.loads(rendered)["version"], "8.1.3\nrc")
        self.assertEqual(tomllib.loads(rendered)["license_mode"], "GPLv3\tverified")
        self.assertEqual(tomllib.loads(rendered)["build"], 'quoted "build"')

    def test_extract_asset_binary_never_uses_archive_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            archive_path = temp_path / "ffmpeg.zip"
            output_dir = temp_path / "output"
            output_dir.mkdir()
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("../../ffmpeg", b"safe bytes")

            binary_path = update_ffmpeg_manifest.extract_asset_binary("ffmpeg", archive_path, output_dir)

            self.assertEqual(binary_path, output_dir / "ffmpeg")
            self.assertEqual(binary_path.read_bytes(), b"safe bytes")
            self.assertFalse((temp_path.parent / "ffmpeg").exists())

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

            with (
                patch("scripts.vendor_ffmpeg_macos.download", side_effect=copy_archive),
                patch("scripts.update_ffmpeg_manifest.validate_base_url"),
            ):
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

    def test_partial_manifest_update_preserves_unselected_assets(self) -> None:
        old_manifest = vendor_ffmpeg_macos.VendorManifest(
            version="8.1.2",
            base_url="https://example.invalid/old",
            license_mode="GPLv3",
            build="old build",
            assets=[
                vendor_ffmpeg_macos.BinaryAsset(
                    name="ffmpeg",
                    url="https://example.invalid/old/ffmpeg.zip",
                    zip_sha256="0" * 64,
                    binary_sha256="1" * 64,
                ),
                vendor_ffmpeg_macos.BinaryAsset(
                    name="ffprobe",
                    url="https://example.invalid/old/ffprobe.zip",
                    zip_sha256="2" * 64,
                    binary_sha256="3" * 64,
                ),
            ],
        )
        new_manifest = update_ffmpeg_manifest.UpdatedManifest(
            version="8.1.3",
            base_url="https://example.invalid/old",
            license_mode="GPLv3",
            build="new build",
            assets=[
                update_ffmpeg_manifest.UpdatedAsset(
                    name="ffmpeg",
                    zip_sha256="4" * 64,
                    binary_sha256="5" * 64,
                )
            ],
        )

        merged_manifest = update_ffmpeg_manifest.merge_manifest_assets(old_manifest, new_manifest)

        self.assertEqual([asset.name for asset in merged_manifest.assets], ["ffmpeg", "ffprobe"])
        self.assertEqual(merged_manifest.assets[0].zip_sha256, "4" * 64)
        self.assertEqual(merged_manifest.assets[0].binary_sha256, "5" * 64)
        self.assertEqual(merged_manifest.assets[1].zip_sha256, "2" * 64)
        self.assertEqual(merged_manifest.assets[1].binary_sha256, "3" * 64)

    def test_validate_base_url_accepts_approved_host(self) -> None:
        update_ffmpeg_manifest.validate_base_url("https://ffmpeg.martin-riedl.de/8.1.3")
        update_ffmpeg_manifest.validate_base_url("https://ffmpeg.martin-riedl.de/8.1.3/")

    def test_validate_base_url_rejects_http(self) -> None:
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            update_ffmpeg_manifest.validate_base_url("http://ffmpeg.martin-riedl.de/8.1.3")

    def test_validate_base_url_rejects_unapproved_host(self) -> None:
        with self.assertRaisesRegex(ValueError, "ffmpeg.martin-riedl.de"):
            update_ffmpeg_manifest.validate_base_url("https://evil.example.com/8.1.3")

    def test_validate_base_url_rejects_subdomain_bypass(self) -> None:
        with self.assertRaisesRegex(ValueError, "ffmpeg.martin-riedl.de"):
            update_ffmpeg_manifest.validate_base_url("https://evil.ffmpeg.martin-riedl.de/8.1.3")

    def test_build_candidate_manifest_rejects_unapproved_host(self) -> None:
        with self.assertRaisesRegex(ValueError, "ffmpeg.martin-riedl.de"):
            update_ffmpeg_manifest.build_candidate_manifest(
                version="8.1.3",
                base_url="https://evil.example.com/build",
                license_mode="GPLv3",
                build="test build",
                asset_names=["ffmpeg"],
            )

    def test_partial_manifest_update_rejects_base_url_change(self) -> None:
        old_manifest = vendor_ffmpeg_macos.VendorManifest(
            version="8.1.2",
            base_url="https://example.invalid/old",
            license_mode="GPLv3",
            build="old build",
            assets=[
                vendor_ffmpeg_macos.BinaryAsset(
                    name="ffmpeg",
                    url="https://example.invalid/old/ffmpeg.zip",
                    zip_sha256="0" * 64,
                    binary_sha256="1" * 64,
                ),
                vendor_ffmpeg_macos.BinaryAsset(
                    name="ffprobe",
                    url="https://example.invalid/old/ffprobe.zip",
                    zip_sha256="2" * 64,
                    binary_sha256="3" * 64,
                ),
            ],
        )
        new_manifest = update_ffmpeg_manifest.UpdatedManifest(
            version="8.1.3",
            base_url="https://example.invalid/new",
            license_mode="GPLv3",
            build="new build",
            assets=[
                update_ffmpeg_manifest.UpdatedAsset(
                    name="ffmpeg",
                    zip_sha256="4" * 64,
                    binary_sha256="5" * 64,
                )
            ],
        )

        with self.assertRaisesRegex(ValueError, "Partial FFmpeg manifest updates"):
            update_ffmpeg_manifest.merge_manifest_assets(old_manifest, new_manifest)


if __name__ == "__main__":
    unittest.main()
