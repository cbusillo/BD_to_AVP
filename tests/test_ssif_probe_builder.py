import json
from dataclasses import asdict
import sys
import tempfile
import unittest

from pathlib import Path

from scripts import build_ssif_probe_macos


class SsifProbeBuilderTests(unittest.TestCase):
    def test_installed_build_tools_match_the_artifact_toolchain(self) -> None:
        manifest = build_ssif_probe_macos.load_manifest(build_ssif_probe_macos.MANIFEST_PATH)
        tool_directory = Path(sys.executable).parent
        for tool, version in (("meson", manifest.meson_version), ("ninja", manifest.ninja_version)):
            with self.subTest(tool=tool):
                self.assertEqual(
                    build_ssif_probe_macos.command_output([str(tool_directory / tool), "--version"]).strip(),
                    version,
                )

    def test_manifest_rejects_unknown_fields(self) -> None:
        manifest_text = build_ssif_probe_macos.MANIFEST_PATH.read_text(encoding="utf-8")
        manifest_text = manifest_text.replace(
            "\n[build_options]",
            "\nunexpected = true\n\n[build_options]",
            1,
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            manifest_path = Path(temporary_directory) / "manifest.toml"
            manifest_path.write_text(manifest_text, encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "unexpected SSIF probe manifest fields"):
                build_ssif_probe_macos.load_manifest(manifest_path)

    def test_provenance_records_library_flags_and_source_preparation(self) -> None:
        manifest = build_ssif_probe_macos.load_manifest(build_ssif_probe_macos.MANIFEST_PATH)
        provenance = json.loads(build_ssif_probe_macos.PROVENANCE_PATH.read_text(encoding="utf-8"))

        self.assertEqual(
            provenance["library_compile_flags"],
            list(build_ssif_probe_macos.library_compile_flags(manifest)),
        )
        self.assertEqual(
            provenance["library_link_flags"],
            list(build_ssif_probe_macos.library_link_flags(manifest)),
        )
        self.assertEqual(
            provenance["source_preparation"],
            build_ssif_probe_macos.source_preparation_record(manifest),
        )

    def test_meson_setup_forces_the_exact_libudfread_fallback(self) -> None:
        manifest = build_ssif_probe_macos.load_manifest(build_ssif_probe_macos.MANIFEST_PATH)
        command = build_ssif_probe_macos.meson_setup_command(
            "/toolchain/meson",
            Path("libbluray"),
            Path("build"),
            Path("prefix"),
            manifest,
        )

        self.assertEqual(command[:4], ["/toolchain/meson", "setup", "build", "libbluray"])
        self.assertIn("--wrap-mode", command)
        self.assertEqual(command[command.index("--wrap-mode") + 1], "forcefallback")
        for name, value in asdict(manifest.build_options).items():
            rendered = str(value).lower() if isinstance(value, bool) else value
            self.assertIn(f"-D{name}={rendered}", command)

    def test_artifact_paths_are_the_packaged_private_layout(self) -> None:
        manifest = build_ssif_probe_macos.load_manifest(build_ssif_probe_macos.MANIFEST_PATH)

        self.assertEqual(
            build_ssif_probe_macos.artifact_paths(manifest),
            {path: build_ssif_probe_macos.REPOSITORY_ROOT / path for path in manifest.unsigned_checksums},
        )

    def test_sanitized_build_environment_blocks_host_search_paths(self) -> None:
        manifest = build_ssif_probe_macos.load_manifest(build_ssif_probe_macos.MANIFEST_PATH)
        environment = build_ssif_probe_macos.build_environment(
            Path("temporary"),
            "/opt/homebrew/bin/meson",
            "/opt/homebrew/bin/ninja",
            manifest,
        )

        self.assertEqual(environment["PATH"], "/usr/bin:/bin:/usr/sbin:/sbin")
        self.assertEqual(environment["CC"], "/usr/bin/clang")
        self.assertEqual(environment["PKG_CONFIG"], "/usr/bin/false")
        self.assertEqual(environment["PKG_CONFIG_PATH"], "")
        self.assertEqual(environment["PKG_CONFIG_LIBDIR"], "")
        self.assertIn(f"-arch {manifest.architecture}", environment["CFLAGS"])
        self.assertIn(f"-mmacosx-version-min={manifest.minimum_macos}", environment["CFLAGS"])
        self.assertIn("-headerpad_max_install_names", environment["LDFLAGS"])
        self.assertEqual(environment["CPATH"], "")
        self.assertEqual(environment["LIBRARY_PATH"], "")


if __name__ == "__main__":
    unittest.main()
