import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from scripts import build_ssif_probe_macos


class SsifProbeBuilderTests(unittest.TestCase):
    def test_manifest_pins_development_dependencies(self) -> None:
        manifest = build_ssif_probe_macos.load_manifest(build_ssif_probe_macos.MANIFEST_PATH)

        self.assertEqual(manifest.schema_version, 1)
        self.assertEqual(manifest.platform, "macOS arm64")
        self.assertEqual(manifest.minimum_macos, "14.0")
        self.assertEqual(manifest.linkage, "dynamic-development-only")
        self.assertEqual(manifest.libbluray.version, "1.4.1")
        self.assertEqual(manifest.libudfread.version, "1.2.0")
        self.assertEqual(manifest.libbluray.license, "LGPL-2.1-or-later")
        self.assertEqual(manifest.libudfread.license, "LGPL-2.1-or-later")

    def test_manifest_rejects_unknown_fields(self) -> None:
        manifest = """
schema_version = 1
platform = "macOS arm64"
minimum_macos = "14.0"
linkage = "dynamic-development-only"
unexpected = true

[libbluray]
version = "1.4.1"
pkg_config = "libbluray"
source_url = "https://example.com/libbluray.tar.xz"
source_sha256 = "bluray"
license = "LGPL-2.1-or-later"

[libudfread]
version = "1.2.0"
pkg_config = "libudfread"
source_url = "https://example.com/libudfread.tar.xz"
source_sha256 = "udfread"
license = "LGPL-2.1-or-later"
"""
        with tempfile.TemporaryDirectory() as temporary_directory:
            manifest_path = Path(temporary_directory) / "manifest.toml"
            manifest_path.write_text(manifest)

            with self.assertRaisesRegex(RuntimeError, "unexpected SSIF probe manifest fields"):
                build_ssif_probe_macos.load_manifest(manifest_path)

    @patch("scripts.build_ssif_probe_macos.pkg_config")
    def test_build_command_uses_dynamic_pkg_config_linkage(self, pkg_config_mock) -> None:
        pkg_config_mock.side_effect = ["-I/native/include", "-L/native/lib -lbluray"]
        manifest = build_ssif_probe_macos.SsifProbeManifest(
            schema_version=1,
            platform="macOS arm64",
            minimum_macos="14.0",
            linkage="dynamic-development-only",
            libbluray=build_ssif_probe_macos.NativeDependency(
                version="1.4.1",
                pkg_config="libbluray",
                source_url="https://example.com/libbluray.tar.xz",
                source_sha256="bluray",
                license="LGPL-2.1-or-later",
            ),
            libudfread=build_ssif_probe_macos.NativeDependency(
                version="1.2.0",
                pkg_config="libudfread",
                source_url="https://example.com/libudfread.tar.xz",
                source_sha256="udfread",
                license="LGPL-2.1-or-later",
            ),
        )

        command = build_ssif_probe_macos.build_command(
            "clang",
            Path("probe.c"),
            Path("probe"),
            manifest,
        )

        self.assertEqual(
            command,
            [
                "clang",
                "-std=c11",
                "-O2",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-mmacosx-version-min=14.0",
                "-I/native/include",
                "probe.c",
                "-o",
                "probe",
                "-L/native/lib",
                "-lbluray",
            ],
        )


if __name__ == "__main__":
    unittest.main()
