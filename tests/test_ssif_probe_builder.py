import json
import tempfile
import unittest

from pathlib import Path

from scripts import build_ssif_probe_macos


class SsifProbeBuilderTests(unittest.TestCase):
    def test_manifest_records_bundled_shared_library_contract(self) -> None:
        manifest = build_ssif_probe_macos.load_manifest(build_ssif_probe_macos.MANIFEST_PATH)

        self.assertEqual(manifest.schema_version, 3)
        self.assertEqual(manifest.platform, "macOS arm64")
        self.assertEqual(manifest.architecture, "arm64")
        self.assertEqual(manifest.minimum_macos, "14.0")
        self.assertEqual(manifest.linkage, "private-shared")
        self.assertEqual(manifest.rpath, "@loader_path/../lib")
        self.assertEqual(manifest.meson_version, "1.12.0")
        self.assertEqual(manifest.ninja_version, "1.13.2.git.kitware.jobserver-pipe-1")
        self.assertIn("-arch", manifest.probe_compile_flags)
        self.assertEqual(manifest.libbluray.version, "1.4.1")
        self.assertEqual(manifest.libbluray.source, "libbluray-1.4.1.tar.xz")
        self.assertEqual(manifest.libudfread.version, "1.2.0")
        self.assertEqual(manifest.libudfread.source, "libudfread-1.2.0.tar.xz")
        self.assertEqual(manifest.libbluray.install_name, "@rpath/libbluray.3.dylib")
        self.assertEqual(manifest.libudfread.install_name, "@rpath/libudfread.3.dylib")
        self.assertEqual(manifest.filenames["probe"], "ssif_probe")
        self.assertFalse(manifest.build_options.embed_udfread)
        self.assertEqual(manifest.build_options.default_library, "shared")
        self.assertEqual(manifest.build_options.bdj_jar, "disabled")
        self.assertEqual(manifest.build_options.freetype, "disabled")
        self.assertEqual(manifest.build_options.fontconfig, "disabled")
        self.assertEqual(manifest.build_options.libxml2, "disabled")
        self.assertEqual(
            set(manifest.unsigned_checksums),
            {
                "bd_to_avp/bin/ssif_probe",
                "bd_to_avp/lib/libbluray.3.dylib",
                "bd_to_avp/lib/libudfread.3.dylib",
            },
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
        self.assertIn("-Ddefault_library=shared", command)
        self.assertIn("-Dembed_udfread=false", command)
        self.assertIn("-Denable_docs=false", command)
        self.assertIn("-Denable_tools=false", command)
        self.assertIn("-Denable_examples=false", command)
        self.assertIn("-Dbdj_jar=disabled", command)
        self.assertIn("-Dfreetype=disabled", command)
        self.assertIn("-Dfontconfig=disabled", command)
        self.assertIn("-Dlibxml2=disabled", command)

    def test_artifact_paths_are_the_packaged_private_layout(self) -> None:
        manifest = build_ssif_probe_macos.load_manifest(build_ssif_probe_macos.MANIFEST_PATH)

        self.assertEqual(
            build_ssif_probe_macos.artifact_paths(manifest),
            {
                "bd_to_avp/bin/ssif_probe": build_ssif_probe_macos.BIN_PATH,
                "bd_to_avp/lib/libbluray.3.dylib": build_ssif_probe_macos.LIBRARY_DIRECTORY / "libbluray.3.dylib",
                "bd_to_avp/lib/libudfread.3.dylib": build_ssif_probe_macos.LIBRARY_DIRECTORY / "libudfread.3.dylib",
            },
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
        self.assertIn("-arch arm64", environment["CFLAGS"])
        self.assertIn("-mmacosx-version-min=14.0", environment["CFLAGS"])
        self.assertIn("-headerpad_max_install_names", environment["LDFLAGS"])
        self.assertEqual(environment["CPATH"], "")
        self.assertEqual(environment["LIBRARY_PATH"], "")


if __name__ == "__main__":
    unittest.main()
