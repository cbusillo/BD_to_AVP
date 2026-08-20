import hashlib
import io
import stat
import tarfile
import tempfile
import unittest
import zipfile

from pathlib import Path
from unittest.mock import patch

from scripts import embedded_python


class EmbeddedPythonTests(unittest.TestCase):
    def test_repository_manifest_is_digest_pinned(self) -> None:
        manifest = embedded_python.load_manifest()

        self.assertEqual(manifest.python_version, "3.12.10")
        self.assertEqual(manifest.python_abi, "3.12")
        self.assertEqual(manifest.architecture, "arm64")
        self.assertRegex(manifest.support.sha256, r"^[0-9a-f]{64}$")
        self.assertRegex(manifest.launcher.sha256, r"^[0-9a-f]{64}$")

    def test_verify_archive_rejects_digest_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            archive_path = Path(temporary_directory) / "runtime.tar.gz"
            archive_path.write_bytes(b"unexpected")
            asset = embedded_python.RuntimeAsset("https://example.invalid/runtime.tar.gz", "0" * 64)

            with self.assertRaisesRegex(embedded_python.EmbeddedPythonError, "digest mismatch"):
                embedded_python.verify_archive(archive_path, asset)

    def test_validate_tar_members_rejects_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            archive_path = Path(temporary_directory) / "runtime.tar.gz"
            with tarfile.open(archive_path, "w:gz") as archive:
                member = tarfile.TarInfo("../escape")
                payload = b"escape"
                member.size = len(payload)
                archive.addfile(member, io.BytesIO(payload))

            with tarfile.open(archive_path, "r:gz") as archive:
                with self.assertRaisesRegex(embedded_python.EmbeddedPythonError, "escapes"):
                    embedded_python.validate_tar_members(archive, Path(temporary_directory) / "output")

    def test_extract_launcher_thins_to_arm64_and_sets_executable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            archive_path = root / "launcher.zip"
            output_path = root / "BluRayToVisionProEngine"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("Stub", b"universal launcher")
            manifest = embedded_python.RuntimeManifest(
                python_version="3.12.13",
                python_abi="3.12",
                architecture="arm64",
                minimum_macos_version="11.0",
                framework_source_path=Path("Python.framework"),
                support=embedded_python.RuntimeAsset("https://example.invalid/support.tar.gz", "0" * 64),
                launcher=embedded_python.RuntimeAsset("https://example.invalid/launcher.zip", "1" * 64),
                launcher_member="Stub",
            )

            def fake_lipo(command: list[object], **_kwargs: object) -> None:
                Path(command[-1]).write_bytes(Path(command[1]).read_bytes())

            with patch("scripts.embedded_python.subprocess.run", side_effect=fake_lipo) as run:
                embedded_python.extract_launcher(archive_path, manifest, output_path)

            run.assert_called_once()
            self.assertEqual(output_path.read_bytes(), b"universal launcher")
            self.assertTrue(output_path.stat().st_mode & stat.S_IXUSR)

    def test_install_uses_lock_hashes_and_binary_wheels_except_pysrt(self) -> None:
        manifest = embedded_python.load_manifest()
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            requirements = root / "requirements.txt"
            requirements.write_text("pysrt==1.1.2 --hash=sha256:" + hashlib.sha256(b"pysrt").hexdigest())
            destination = root / "packages"

            with patch("scripts.embedded_python.subprocess.run") as run:
                embedded_python.install_locked_dependencies(
                    destination,
                    manifest,
                    requirements_path=requirements,
                    build_constraints_path=root / "build-constraints.txt",
                )

            command = run.call_args.args[0]
            self.assertIn("--require-hashes", command)
            self.assertIn("--build-constraints", command)
            self.assertIn(":all:", command)
            self.assertIn("pysrt", command)
            self.assertIn("aarch64-apple-darwin", command)

    def test_copy_application_source_includes_vendored_tools(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "app"
            embedded_python.copy_application_source(destination)

            self.assertTrue((destination / "bd_to_avp" / "worker" / "__main__.py").is_file())
            self.assertTrue((destination / "bd_to_avp" / "bin" / "MP4Box").is_file())


if __name__ == "__main__":
    unittest.main()
