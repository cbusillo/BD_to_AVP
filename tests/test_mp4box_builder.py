import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from scripts import build_mp4box_macos


class MP4BoxBuilderTests(unittest.TestCase):
    def test_load_manifest_reads_build_and_validation_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest_path = Path(temp_dir) / "mp4box.toml"
            manifest_path.write_text(
                '\n'.join(
                    [
                        'version = "26.02.0"',
                        'repo_url = "https://example.invalid/gpac.git"',
                        'tag = "v26.02.0"',
                        'license_mode = "LGPL-2.1-or-later"',
                        'binary = "MP4Box"',
                        f'binary_sha256 = "{"1" * 64}"',
                        'build = "static test build"',
                        'configure_flags = ["--static-bin", "--use-ffmpeg=no"]',
                        '',
                        '[validation]',
                        'required_file_substring = "Mach-O 64-bit executable arm64"',
                        'required_version_substring = "MP4Box - GPAC version"',
                        'forbidden_link_prefixes = ["/opt/homebrew", "/usr/local"]',
                        'expected_system_links = ["/usr/lib/libz.1.dylib", "/usr/lib/libSystem.B.dylib"]',
                    ]
                )
            )

            manifest = build_mp4box_macos.load_manifest(manifest_path)

        self.assertEqual(manifest.tag, "v26.02.0")
        self.assertEqual(manifest.binary, "MP4Box")
        self.assertEqual(manifest.binary_sha256, "1" * 64)
        self.assertIn("--static-bin", manifest.configure_flags)
        self.assertEqual(manifest.validation.forbidden_link_prefixes, ("/opt/homebrew", "/usr/local"))
        self.assertIn("/usr/lib/libSystem.B.dylib", manifest.validation.expected_system_links)

    def test_build_env_hides_homebrew_tools(self) -> None:
        env = build_mp4box_macos.build_env()

        self.assertEqual(env["PATH"], "/usr/bin:/bin:/usr/sbin:/sbin")
        self.assertEqual(env["CC"], "/usr/bin/clang")
        self.assertEqual(env["CXX"], "/usr/bin/clang++")
        self.assertEqual(env["PKG_CONFIG"], "/usr/bin/false")

    def test_verify_build_host_rejects_non_arm64_macos(self) -> None:
        with (
            patch.object(build_mp4box_macos.platform, "system", return_value="Darwin"),
            patch.object(build_mp4box_macos.platform, "machine", return_value="x86_64"),
        ):
            with self.assertRaisesRegex(build_mp4box_macos.BuildFailure, "macOS arm64"):
                build_mp4box_macos.verify_build_host()

    def test_verify_macos_binary_rejects_homebrew_linkage(self) -> None:
        def fake_run(command: list[str | Path], **kwargs) -> str:
            if command[0] == "file":
                return "MP4Box: Mach-O 64-bit executable arm64"
            if command[0] == "otool":
                return "MP4Box:\n\t/opt/homebrew/lib/libgpac.dylib\n"
            return "MP4Box - GPAC version 26.02"

        with patch.object(build_mp4box_macos, "run", side_effect=fake_run):
            with self.assertRaisesRegex(build_mp4box_macos.BuildFailure, "/opt/homebrew"):
                build_mp4box_macos.verify_macos_binary(
                    Path("MP4Box"), build_mp4box_macos.load_manifest().validation
                )

    def test_verify_macos_binary_accepts_system_only_linkage(self) -> None:
        def fake_run(command: list[str | Path], **kwargs) -> str:
            if command[0] == "file":
                return "MP4Box: Mach-O 64-bit executable arm64"
            if command[0] == "otool":
                return "MP4Box:\n\t/usr/lib/libz.1.dylib\n\t/usr/lib/libSystem.B.dylib\n"
            return "MP4Box - GPAC version 26.02"

        with patch.object(build_mp4box_macos, "run", side_effect=fake_run):
            build_mp4box_macos.verify_macos_binary(Path("MP4Box"), build_mp4box_macos.load_manifest().validation)

    def test_verify_macos_binary_rejects_unexpected_system_linkage(self) -> None:
        def fake_run(command: list[str | Path], **kwargs) -> str:
            if command[0] == "file":
                return "MP4Box: Mach-O 64-bit executable arm64"
            if command[0] == "otool":
                return "MP4Box:\n\t/usr/lib/libz.1.dylib\n\t/usr/lib/libobjc.A.dylib\n"
            return "MP4Box - GPAC version 26.02"

        with patch.object(build_mp4box_macos, "run", side_effect=fake_run):
            with self.assertRaisesRegex(build_mp4box_macos.BuildFailure, "unexpected"):
                build_mp4box_macos.verify_macos_binary(
                    Path("MP4Box"), build_mp4box_macos.load_manifest().validation
                )

    def test_build_mp4box_skips_distclean_without_makefile(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source_dir = Path(temp_dir)
            binary_path = source_dir / "bin" / "gcc" / "MP4Box"
            binary_path.parent.mkdir(parents=True)
            binary_path.write_text("binary")
            commands: list[list[str | Path]] = []

            def fake_run(command: list[str | Path], **kwargs) -> str:
                commands.append(command)
                return ""

            with patch.object(build_mp4box_macos, "run", side_effect=fake_run):
                build_mp4box_macos.build_mp4box(
                    source_dir, source_dir / "install", build_mp4box_macos.load_manifest()
                )

        self.assertNotIn(["make", "distclean"], commands)
        self.assertIn(["make", f"-j{build_mp4box_macos.os.cpu_count() or 1}", "lib"], commands)

    def test_install_binary_sets_executable_bit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source-MP4Box"
            output_dir = root / "bin"
            source.write_text("binary")
            source.chmod(0o644)

            output_path = build_mp4box_macos.install_binary(source, output_dir)
            self.assertEqual(output_path.name, "MP4Box")
            self.assertTrue(output_path.stat().st_mode & 0o111)


if __name__ == "__main__":
    unittest.main()
