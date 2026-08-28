import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from scripts import build_ssif_probe_macos


class SsifProbeBuilderTests(unittest.TestCase):
    def test_manifest_records_known_good_source_provenance(self) -> None:
        manifest = build_ssif_probe_macos.load_manifest(build_ssif_probe_macos.MANIFEST_PATH)

        self.assertEqual(manifest.schema_version, 2)
        self.assertEqual(manifest.platform, "macOS arm64")
        self.assertEqual(manifest.minimum_macos, "14.0")
        self.assertEqual(manifest.linkage, "dynamic-development-only")
        self.assertEqual(manifest.libbluray.known_good_source.version, "1.4.1")
        self.assertEqual(manifest.libudfread.known_good_source.version, "1.2.0")
        self.assertIn("libbluray-1.4.1", manifest.libbluray.known_good_source.url)
        self.assertIn("libudfread-1.2.0", manifest.libudfread.known_good_source.url)
        self.assertEqual(manifest.libbluray.license, "LGPL-2.1-or-later")
        self.assertEqual(manifest.libudfread.license, "LGPL-2.1-or-later")

    def test_manifest_rejects_unknown_fields(self) -> None:
        manifest = """
schema_version = 2
platform = "macOS arm64"
minimum_macos = "14.0"
linkage = "dynamic-development-only"
unexpected = true

[libbluray]
pkg_config = "libbluray"
license = "LGPL-2.1-or-later"

[libbluray.known_good_source]
version = "1.4.1"
url = "https://example.com/libbluray.tar.xz"
sha256 = "bluray"

[libudfread]
pkg_config = "libudfread"
license = "LGPL-2.1-or-later"

[libudfread.known_good_source]
version = "1.2.0"
url = "https://example.com/libudfread.tar.xz"
sha256 = "udfread"
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
            schema_version=2,
            platform="macOS arm64",
            minimum_macos="14.0",
            linkage="dynamic-development-only",
            libbluray=build_ssif_probe_macos.NativeDependency(
                pkg_config="libbluray",
                license="LGPL-2.1-or-later",
                known_good_source=build_ssif_probe_macos.SourceProvenance(
                    version="1.4.1",
                    url="https://example.com/libbluray.tar.xz",
                    sha256="bluray",
                ),
            ),
            libudfread=build_ssif_probe_macos.NativeDependency(
                pkg_config="libudfread",
                license="LGPL-2.1-or-later",
                known_good_source=build_ssif_probe_macos.SourceProvenance(
                    version="1.2.0",
                    url="https://example.com/libudfread.tar.xz",
                    sha256="udfread",
                ),
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

    @patch("scripts.build_ssif_probe_macos.pkg_config", return_value="1.5.0")
    def test_dependency_verification_accepts_host_version(self, pkg_config_mock) -> None:
        dependency = build_ssif_probe_macos.NativeDependency(
            pkg_config="libbluray",
            license="LGPL-2.1-or-later",
            known_good_source=build_ssif_probe_macos.SourceProvenance(
                version="1.4.1",
                url="https://example.com/libbluray.tar.xz",
                sha256="bluray",
            ),
        )

        self.assertEqual(build_ssif_probe_macos.verify_dependency(dependency), "1.5.0")
        pkg_config_mock.assert_called_once_with(["--modversion", "libbluray"])

    @patch("scripts.build_ssif_probe_macos.pkg_config", return_value="")
    def test_dependency_verification_rejects_missing_version(self, _pkg_config_mock) -> None:
        dependency = build_ssif_probe_macos.NativeDependency(
            pkg_config="libbluray",
            license="LGPL-2.1-or-later",
            known_good_source=build_ssif_probe_macos.SourceProvenance(
                version="1.4.1",
                url="https://example.com/libbluray.tar.xz",
                sha256="bluray",
            ),
        )

        with self.assertRaisesRegex(RuntimeError, "did not report an installed version"):
            build_ssif_probe_macos.verify_dependency(dependency)


if __name__ == "__main__":
    unittest.main()
