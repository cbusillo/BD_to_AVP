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
                    "ffmpeg",
                    vendor_ffmpeg_macos.sha256(archive_path),
                    FAKE_BINARY_SHA256,
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
                "ffprobe", vendor_ffmpeg_macos.sha256(archive_path), FAKE_BINARY_SHA256
            )

            with patch("scripts.vendor_ffmpeg_macos.download") as download:
                output_path = vendor_ffmpeg_macos.vendor_asset(asset, cache_dir, output_dir, refresh=False)

            download.assert_not_called()
            self.assertEqual(output_path, output_dir / "ffprobe")
            self.assertTrue(output_path.exists())

    def test_archive_checksum_mismatch_raises(self) -> None:
        with tempfile.NamedTemporaryFile() as archive_file:
            archive_path = Path(archive_file.name)
            archive_path.write_bytes(b"unexpected")

            with self.assertRaisesRegex(ValueError, "Checksum mismatch"):
                vendor_ffmpeg_macos.verify_archive(archive_path, "0" * 64)

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
            (source_bin / "ffmpeg").write_text("ffmpeg")
            (source_bin / "ffprobe").write_text("ffprobe")

            with (
                patch.object(briefcase_app, "REPO_ROOT", repo_root),
                patch.object(briefcase_app, "APP_RESOURCE_BIN", app_bin),
            ):
                briefcase_app.sync_vendored_tools_to_existing_app()

            self.assertEqual((app_bin / "ffmpeg").read_text(), "ffmpeg")
            self.assertEqual((app_bin / "ffprobe").read_text(), "ffprobe")


if __name__ == "__main__":
    unittest.main()
