import stat
import tempfile
import unittest
import zipfile
import hashlib
from pathlib import Path
from unittest.mock import patch

from scripts import briefcase_app, vendor_ffmpeg_macos


FAKE_BINARY_SHA256 = hashlib.sha256(b"binary").hexdigest()


class VendorFfmpegTests(unittest.TestCase):
    def test_extract_binary_sets_executable_bit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            archive_path = temp_path / "ffmpeg.zip"
            output_dir = temp_path / "bin"
            output_dir.mkdir()

            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("ffmpeg", b"binary")

            output_path = vendor_ffmpeg_macos.extract_binary(
                vendor_ffmpeg_macos.BinaryAsset(
                    name="ffmpeg",
                    url="https://example.invalid/ffmpeg.zip",
                    zip_sha256=vendor_ffmpeg_macos.sha256(archive_path),
                    binary_sha256=FAKE_BINARY_SHA256,
                ),
                archive_path,
                output_dir,
            )

            self.assertEqual(output_path.name, "ffmpeg")
            self.assertTrue(output_path.stat().st_mode & stat.S_IXUSR)

    def test_vendor_asset_uses_cached_verified_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            cache_dir = temp_path / "cache"
            output_dir = temp_path / "bin"
            cache_dir.mkdir()
            archive_path = cache_dir / "ffprobe.zip"

            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("ffprobe", b"binary")

            asset = vendor_ffmpeg_macos.BinaryAsset(
                name="ffprobe",
                url="https://example.invalid/ffprobe.zip",
                zip_sha256=vendor_ffmpeg_macos.sha256(archive_path),
                binary_sha256=FAKE_BINARY_SHA256,
            )

            with patch("scripts.vendor_ffmpeg_macos.download") as download:
                output_path = vendor_ffmpeg_macos.vendor_asset(asset, cache_dir, output_dir, refresh=False)

            download.assert_not_called()
            self.assertEqual(output_path, output_dir / "ffprobe")
            self.assertTrue(output_path.exists())

    def test_load_manifest_builds_asset_urls(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest_path = Path(temp_dir) / "ffmpeg.toml"
            manifest_path.write_text(
                "\n".join(
                    [
                        'version = "8.1.2"',
                        'base_url = "https://example.invalid/ffmpeg"',
                        'license_mode = "GPLv3"',
                        'build = "test build"',
                        "",
                        "[[assets]]",
                        'name = "ffmpeg"',
                        f'zip_sha256 = "{"0" * 64}"',
                        f'binary_sha256 = "{"1" * 64}"',
                    ]
                )
            )

            manifest = vendor_ffmpeg_macos.load_manifest(manifest_path)

        self.assertEqual(manifest.version, "8.1.2")
        self.assertEqual(manifest.license_mode, "GPLv3")
        self.assertEqual(manifest.assets[0].url, "https://example.invalid/ffmpeg/ffmpeg.zip")

    def test_archive_checksum_mismatch_raises(self) -> None:
        with tempfile.NamedTemporaryFile() as archive_file:
            archive_path = Path(archive_file.name)
            archive_path.write_bytes(b"unexpected")

            with self.assertRaisesRegex(ValueError, "Checksum mismatch"):
                vendor_ffmpeg_macos.verify_archive(archive_path, "0" * 64)

    def test_download_rejects_non_https_urls(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "ffmpeg.zip"

            with self.assertRaisesRegex(ValueError, "HTTPS"):
                vendor_ffmpeg_macos.download("http://example.invalid/ffmpeg.zip", destination)

    def test_binary_checksum_mismatch_raises(self) -> None:
        with tempfile.NamedTemporaryFile() as binary_file:
            binary_path = Path(binary_file.name)
            binary_path.write_bytes(b"unexpected")

            with self.assertRaisesRegex(ValueError, "extracted"):
                vendor_ffmpeg_macos.verify_binary(binary_path, "0" * 64)


class BriefcaseVendorHookTests(unittest.TestCase):
    def test_vendor_ffmpeg_for_app_build_commands(self) -> None:
        self.assertTrue(briefcase_app.should_vendor_ffmpeg(["create", "--no-input"]))
        self.assertTrue(briefcase_app.should_vendor_ffmpeg(["build"]))
        self.assertFalse(briefcase_app.should_vendor_ffmpeg(["package", "-i", "Developer ID"]))

    def test_sync_vendored_tools_for_package_commands(self) -> None:
        self.assertTrue(briefcase_app.should_sync_vendored_tools(["create", "--no-input"]))
        self.assertTrue(briefcase_app.should_sync_vendored_tools(["build"]))
        self.assertFalse(briefcase_app.should_sync_vendored_tools(["package", "-i", "Developer ID"]))
        self.assertFalse(briefcase_app.should_sync_vendored_tools(["--help"]))

    def test_do_not_vendor_ffmpeg_for_non_app_commands(self) -> None:
        self.assertFalse(briefcase_app.should_vendor_ffmpeg(["--help"]))

    def test_sync_vendored_tools_to_existing_app(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo_root = temp_path / "repo"
            source_bin = repo_root / "bd_to_avp" / "bin"
            app_bin = temp_path / "app" / "bd_to_avp" / "bin"
            source_bin.mkdir(parents=True)
            app_bin.mkdir(parents=True)
            for tool_name in briefcase_app.VENDORED_TOOLS:
                (source_bin / tool_name).write_text(tool_name)
                (source_bin / tool_name).chmod(0o644)

            with (
                patch.object(briefcase_app, "REPO_ROOT", repo_root),
                patch.object(briefcase_app, "APP_RESOURCE_BIN", app_bin),
            ):
                briefcase_app.sync_vendored_tools_to_existing_app()

            for tool_name in briefcase_app.VENDORED_TOOLS:
                self.assertEqual((app_bin / tool_name).read_text(), tool_name)
                self.assertTrue((app_bin / tool_name).stat().st_mode & 0o111)

    def test_briefcase_package_does_not_resync_signed_tools(self) -> None:
        with (
            patch.object(briefcase_app, "run") as run,
            patch.object(briefcase_app, "build_wheelhouse") as build_wheelhouse,
            patch.object(briefcase_app, "sync_vendored_tools_to_existing_app") as sync_tools,
        ):
            briefcase_app.main_with_args(["package", "--no-input"])

        build_wheelhouse.assert_called_once()
        run.assert_called_once()
        sync_tools.assert_not_called()


if __name__ == "__main__":
    unittest.main()
