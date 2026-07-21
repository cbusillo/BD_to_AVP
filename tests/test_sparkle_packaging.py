import concurrent.futures
import io
import plistlib
import tarfile
import tempfile
import unittest

from pathlib import Path
from unittest.mock import Mock, patch

from scripts import briefcase_macos_signing, sparkle_bundle, sparkle_macos
from scripts.native_app import SUPPORT_DIAGNOSTICS_ENDPOINT_ENV, SUPPORT_DIAGNOSTICS_ENDPOINT_INFO_KEY
from scripts.production_identity import (
    PRODUCTION_BUNDLE_IDENTIFIER,
    PRODUCTION_DEVELOPER_IDENTITY,
    PRODUCTION_DISTRIBUTION_CHANNEL,
    PRODUCTION_FEED_URL,
    PRODUCTION_SPARKLE_PUBLIC_KEY,
    PRODUCTION_TEAM_ID,
)


def make_framework(root: Path, *, version: str = "2.9.4") -> Path:
    framework = root / "Sparkle.framework"
    for required_path in sparkle_macos.REQUIRED_FRAMEWORK_PATHS:
        path = framework / required_path
        if required_path.suffix in {".app", ".xpc"}:
            path.mkdir(parents=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"binary")
    info_path = framework / "Versions/B/Resources/Info.plist"
    info_path.parent.mkdir(parents=True, exist_ok=True)
    with info_path.open("wb") as handle:
        plistlib.dump(
            {
                "CFBundleIdentifier": "org.sparkle-project.Sparkle",
                "CFBundleShortVersionString": version,
            },
            handle,
        )
    (framework / "Versions/Current").symlink_to("B")
    (framework / "Sparkle").symlink_to("Versions/Current/Sparkle")
    return framework


def make_app(root: Path, info: dict[str, object]) -> Path:
    app_path = root / "Test.app"
    info_path = app_path / "Contents/Info.plist"
    info_path.parent.mkdir(parents=True)
    with info_path.open("wb") as handle:
        plistlib.dump(info, handle)
    framework = make_framework(app_path / "Contents/Frameworks")
    self_contained_framework = app_path / sparkle_macos.FRAMEWORK_RELATIVE_PATH
    if framework != self_contained_framework:
        self_contained_framework.parent.mkdir(parents=True, exist_ok=True)
        framework.rename(self_contained_framework)
    return app_path


class SparkleMacOSTests(unittest.TestCase):
    def test_manifest_pins_expected_release(self) -> None:
        release = sparkle_macos.load_release()

        self.assertEqual(release.version, "2.9.4")
        self.assertEqual(
            release.archive_sha256,
            "ce89daf967db1e1893ed3ebd67575ed82d3902563e3191ca92aaec9164fbdef9",
        )
        self.assertTrue(release.archive_url.startswith("https://"))

    def test_manifest_rejects_non_https_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest_path = Path(temp_dir) / "sparkle.toml"
            manifest_path.write_text(
                'version = "2.9.4"\narchive_url = "http://example.invalid/Sparkle.tar.xz"\n'
                f'archive_sha256 = "{"0" * 64}"\n',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(sparkle_macos.SparkleBuildError, "HTTPS"):
                sparkle_macos.load_release(manifest_path)

    def test_extract_archive_rejects_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            archive_path = root / "Sparkle.tar.xz"
            with tarfile.open(archive_path, "w:xz") as archive:
                member = tarfile.TarInfo("../outside")
                member.size = 1
                archive.addfile(member, io.BytesIO(b"x"))
            release = sparkle_macos.SparkleRelease(
                version="test",
                archive_url="https://example.invalid/Sparkle.tar.xz",
                archive_sha256=sparkle_macos.sha256_file(archive_path),
            )

            with self.assertRaisesRegex(sparkle_macos.SparkleBuildError, "escapes"):
                sparkle_macos.extract_archive(archive_path, release, root / "cache")

    def test_extract_archive_rebuilds_corrupt_cached_framework(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            archive_root = root / "archive-root"
            framework = make_framework(archive_root, version="test")
            archive_path = root / "Sparkle.tar.xz"
            with tarfile.open(archive_path, "w:xz") as archive:
                archive.add(framework, arcname="Sparkle.framework")
            release = sparkle_macos.SparkleRelease(
                version="test",
                archive_url="https://example.invalid/Sparkle.tar.xz",
                archive_sha256=sparkle_macos.sha256_file(archive_path),
            )
            cache_root = root / "cache"
            cached_root = cache_root / release.version
            cached_root.mkdir(parents=True)
            (cached_root / ".archive-sha256").write_text(release.archive_sha256, encoding="utf-8")

            extracted_framework = sparkle_macos.extract_archive(archive_path, release, cache_root)

            sparkle_macos.verify_framework_layout(extracted_framework)

    def test_embed_preserves_framework_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_path = root / "App.app"
            app_path.mkdir()
            source_framework = make_framework(root / "source", version="test")
            release = sparkle_macos.SparkleRelease(
                version="test",
                archive_url="https://example.invalid/Sparkle.tar.xz",
                archive_sha256="0" * 64,
            )

            with (
                patch.object(sparkle_macos, "download_archive", return_value=root / "archive"),
                patch.object(sparkle_macos, "extract_archive", return_value=source_framework),
            ):
                destination = sparkle_macos.embed_sparkle(
                    app_path,
                    release=release,
                    cache_root=root / "cache",
                    verify_architecture=False,
                )

            self.assertTrue((destination / "Versions/Current").is_symlink())
            self.assertEqual((destination / "Versions/Current").readlink(), Path("B"))
            sparkle_macos.verify_framework_layout(destination)

    def test_framework_version_must_match_pin(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            framework = make_framework(Path(temp_dir), version="2.9.3")

            with self.assertRaisesRegex(sparkle_macos.SparkleBuildError, "version must be 2.9.4"):
                sparkle_macos.verify_framework_layout(framework, expected_version="2.9.4")


class BriefcaseSigningTests(unittest.TestCase):
    def test_collect_sign_targets_includes_xpc_bundles(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app_path = Path(temp_dir) / "App.app"
            downloader = app_path / "Contents/Frameworks/Sparkle.framework/Versions/B/XPCServices/Downloader.xpc"
            installer = app_path / "Contents/Frameworks/Sparkle.framework/Versions/B/XPCServices/Installer.xpc"
            updater = app_path / "Contents/Frameworks/Sparkle.framework/Versions/B/Updater.app"
            framework = app_path / "Contents/Frameworks/Sparkle.framework"
            for path in (downloader, installer, updater):
                path.mkdir(parents=True)
            (app_path / "Contents/Resources").mkdir(parents=True)

            class Command:
                @staticmethod
                def package_path(_app):
                    return app_path

            targets = briefcase_macos_signing.collect_sign_targets(Command(), object())

        self.assertIn(downloader, targets)
        self.assertIn(installer, targets)
        self.assertIn(updater, targets)
        self.assertIn(framework, targets)
        self.assertEqual(targets[-1], app_path)

    def test_sparkle_targets_do_not_receive_app_entitlements(self) -> None:
        app_path = Path("/tmp/App.app")
        sparkle_path = app_path / "Contents/Frameworks/Sparkle.framework/Versions/B/Updater.app"
        other_path = app_path / "Contents/Frameworks/Other.framework"

        self.assertTrue(briefcase_macos_signing.is_sparkle_target(sparkle_path, app_path))
        self.assertFalse(briefcase_macos_signing.is_sparkle_target(other_path, app_path))

    def test_signing_patch_rejects_unexpected_briefcase_version(self) -> None:
        with patch.object(briefcase_macos_signing.briefcase, "__version__", "0.0.0"):
            with self.assertRaises(RuntimeError) as error:
                briefcase_macos_signing.install_patch()
        self.assertIn(
            f"requires Briefcase {briefcase_macos_signing.EXPECTED_BRIEFCASE_VERSION}",
            str(error.exception),
        )

    def test_signing_patch_installs_for_expected_briefcase_version(self) -> None:
        original_sign_app = briefcase_macos_signing.macOSSigningMixin.sign_app
        self.addCleanup(
            setattr,
            briefcase_macos_signing.macOSSigningMixin,
            "sign_app",
            original_sign_app,
        )

        briefcase_macos_signing.install_patch()

        self.assertIs(
            briefcase_macos_signing.macOSSigningMixin.sign_app,
            briefcase_macos_signing.sign_app_with_xpc,
        )

    def test_sign_app_uses_inside_out_order_and_no_host_entitlements_for_sparkle(self) -> None:
        app_path = Path("/tmp/App.app")
        downloader = app_path / "Contents/Frameworks/Sparkle.framework/Versions/B/XPCServices/Downloader.xpc"
        updater_app = app_path / "Contents/Frameworks/Sparkle.framework/Versions/B/Updater.app"
        framework = app_path / "Contents/Frameworks/Sparkle.framework"
        other_framework = app_path / "Contents/Frameworks/Other.framework"
        groups = [[downloader], [updater_app], [framework, other_framework], [app_path]]
        signed: list[tuple[Path, object]] = []

        class ProgressBar:
            def add_task(self, _label: str, *, total: int) -> int:
                self.total = total
                return 1

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def update(self, _task_id: int, *, advance: int) -> None:
                self.advance = advance

        class Command:
            console = Mock(is_deep_debug=False)
            tools = Mock()

            @staticmethod
            def package_path(_app):
                return app_path

            @staticmethod
            def entitlements_path(_app):
                return Path("host.entitlements")

            @staticmethod
            def sign_file(path, *, entitlements, identity):
                signed.append((path, entitlements))

        command = Command()
        command.console.progress_bar.return_value = ProgressBar()
        command.tools.file.sorted_depth_first_groups.return_value = (iter(group) for group in groups)
        real_executor = concurrent.futures.ThreadPoolExecutor
        executor_workers: list[int | None] = []

        def create_executor(*, max_workers=None):
            executor_workers.append(max_workers)
            return real_executor(max_workers=max_workers)

        with (
            patch.object(
                briefcase_macos_signing,
                "collect_sign_targets",
                return_value=[path for group in groups for path in group],
            ),
            patch.object(
                briefcase_macos_signing.concurrent.futures,
                "ThreadPoolExecutor",
                side_effect=create_executor,
            ),
        ):
            briefcase_macos_signing.sign_app_with_xpc(command, object(), "Developer ID")

        self.assertEqual([path for path, _ in signed], [path for group in groups for path in group])
        self.assertEqual(executor_workers, [1, 1, 1, None])
        entitlements_by_path = dict(signed)
        self.assertIsNone(entitlements_by_path[downloader])
        self.assertIsNone(entitlements_by_path[updater_app])
        self.assertIsNone(entitlements_by_path[framework])
        self.assertEqual(entitlements_by_path[other_framework], Path("host.entitlements"))
        self.assertEqual(entitlements_by_path[app_path], Path("host.entitlements"))


class SparkleBundleTests(unittest.TestCase):
    def expected_info(self) -> dict[str, object]:
        return {
            "CFBundleVersion": "144",
            "BDToAVPDistributionChannel": PRODUCTION_DISTRIBUTION_CHANNEL,
            "SUFeedURL": PRODUCTION_FEED_URL,
            "SUPublicEDKey": PRODUCTION_SPARKLE_PUBLIC_KEY,
            "SUAllowsAutomaticUpdates": False,
            "SUVerifyUpdateBeforeExtraction": True,
        }

    @patch("scripts.sparkle_bundle.subprocess.run")
    def test_codesign_identity_requires_exact_production_authority_and_team(self, run_mock: Mock) -> None:
        run_mock.return_value = Mock(
            stdout="",
            stderr=(
                f"Authority={PRODUCTION_DEVELOPER_IDENTITY}\n"
                "Authority=Developer ID Certification Authority\n"
                f"TeamIdentifier={PRODUCTION_TEAM_ID}\n"
            ),
        )

        sparkle_bundle._verify_codesign_identity(Path("Signed.app"), "Test app")

        invalid_metadata = (
            f"Authority=Developer ID Application: Other Company ({PRODUCTION_TEAM_ID})\n"
            f"TeamIdentifier={PRODUCTION_TEAM_ID}\n"
        )
        run_mock.return_value = Mock(stdout="", stderr=invalid_metadata)
        with self.assertRaisesRegex(sparkle_bundle.SparkleBundleError, "signing authority must be"):
            sparkle_bundle._verify_codesign_identity(Path("Signed.app"), "Test app")

        run_mock.return_value = Mock(
            stdout="",
            stderr=f"Authority={PRODUCTION_DEVELOPER_IDENTITY}\nTeamIdentifier=ZZZZZ12345\n",
        )
        with self.assertRaisesRegex(sparkle_bundle.SparkleBundleError, "TeamIdentifier must be"):
            sparkle_bundle._verify_codesign_identity(Path("Signed.app"), "Test app")

    def test_inspect_app_bundle_validates_metadata_and_layout(self) -> None:
        expected = self.expected_info()
        support_endpoint = "https://support.example"
        info = {
            **expected,
            "CFBundleIdentifier": PRODUCTION_BUNDLE_IDENTIFIER,
            "CFBundleShortVersionString": "1.2.3rc1",
            "LSMinimumSystemVersion": "11.0",
            SUPPORT_DIAGNOSTICS_ENDPOINT_INFO_KEY: support_endpoint,
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            app_path = make_app(Path(temp_dir), info)

            metadata = sparkle_bundle.inspect_app_bundle(
                app_path,
                expected_info=expected,
                environment={SUPPORT_DIAGNOSTICS_ENDPOINT_ENV: support_endpoint},
            )

        self.assertEqual(metadata.build_version, "144")
        self.assertEqual(metadata.short_version, "1.2.3rc1")
        self.assertEqual(metadata.distribution_channel, "direct")
        self.assertEqual(metadata.support_diagnostics_endpoint, support_endpoint)
        self.assertEqual(metadata.minimum_system_version, "11.0")

    def test_inspect_app_bundle_accepts_canonical_pep440_short_versions(self) -> None:
        expected = self.expected_info()
        for short_version in ("0.3.0", "0.3.0a3", "0.3.0b3", "0.3.0rc3"):
            with self.subTest(short_version=short_version), tempfile.TemporaryDirectory() as temp_dir:
                info = {
                    **expected,
                    "CFBundleIdentifier": PRODUCTION_BUNDLE_IDENTIFIER,
                    "CFBundleShortVersionString": short_version,
                    "LSMinimumSystemVersion": "11.0",
                    SUPPORT_DIAGNOSTICS_ENDPOINT_INFO_KEY: "https://support.example",
                }
                app_path = make_app(Path(temp_dir), info)

                metadata = sparkle_bundle.inspect_app_bundle(app_path, expected_info=expected, environment={})

                self.assertEqual(metadata.short_version, short_version)

    def test_inspect_app_bundle_rejects_default_build_number(self) -> None:
        expected = self.expected_info()
        expected["CFBundleVersion"] = "1"
        info = {
            **expected,
            "CFBundleIdentifier": PRODUCTION_BUNDLE_IDENTIFIER,
            "CFBundleShortVersionString": "1.2.3",
            "LSMinimumSystemVersion": "11.0",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            app_path = make_app(Path(temp_dir), info)

            with self.assertRaisesRegex(sparkle_bundle.SparkleBundleError, "greater than 1"):
                sparkle_bundle.inspect_app_bundle(app_path, expected_info=expected)

    def test_inspect_app_bundle_rejects_noncanonical_build_numbers(self) -> None:
        for build_version in ("0", "00", "01"):
            with self.subTest(build_version=build_version), tempfile.TemporaryDirectory() as temp_dir:
                expected = self.expected_info()
                expected["CFBundleVersion"] = build_version
                info = {
                    **expected,
                    "CFBundleIdentifier": PRODUCTION_BUNDLE_IDENTIFIER,
                    "CFBundleShortVersionString": "1.2.3",
                    "LSMinimumSystemVersion": "11.0",
                }
                app_path = make_app(Path(temp_dir), info)

                with self.assertRaisesRegex(sparkle_bundle.SparkleBundleError, "canonical numeric"):
                    sparkle_bundle.inspect_app_bundle(app_path, expected_info=expected)

    def test_release_artifact_accepts_newer_numeric_build(self) -> None:
        expected = self.expected_info()
        info = {
            **expected,
            "CFBundleVersion": "145",
            "CFBundleIdentifier": PRODUCTION_BUNDLE_IDENTIFIER,
            "CFBundleShortVersionString": "1.2.4rc1",
            "LSMinimumSystemVersion": "11.0",
            SUPPORT_DIAGNOSTICS_ENDPOINT_INFO_KEY: "https://support.example",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            app_path = make_app(Path(temp_dir), info)

            metadata = sparkle_bundle.inspect_app_bundle(
                app_path,
                expected_info=expected,
                require_repository_build=False,
                environment={},
            )

        self.assertEqual(metadata.build_version, "145")

    def test_inspect_app_bundle_rejects_malformed_short_versions(self) -> None:
        expected = self.expected_info()
        malformed_versions = ("0.3", "0.3.0-beta.3", "0.3.0b0", "00.3.0", "0.3.0b03", "1.2.3\nforged=value")
        for short_version in malformed_versions:
            with self.subTest(short_version=short_version), tempfile.TemporaryDirectory() as temp_dir:
                info = {
                    **expected,
                    "CFBundleIdentifier": PRODUCTION_BUNDLE_IDENTIFIER,
                    "CFBundleShortVersionString": short_version,
                    "LSMinimumSystemVersion": "11.0",
                    SUPPORT_DIAGNOSTICS_ENDPOINT_INFO_KEY: "https://support.example",
                }
                app_path = make_app(Path(temp_dir), info)

                with self.assertRaisesRegex(sparkle_bundle.SparkleBundleError, "canonical three-part PEP 440"):
                    sparkle_bundle.inspect_app_bundle(app_path, expected_info=expected, environment={})

    def test_inspect_app_bundle_rejects_retired_preview_short_versions(self) -> None:
        expected = self.expected_info()
        for short_version in ("0.3.0b1", "0.3.0b2"):
            with self.subTest(short_version=short_version), tempfile.TemporaryDirectory() as temp_dir:
                info = {
                    **expected,
                    "CFBundleIdentifier": PRODUCTION_BUNDLE_IDENTIFIER,
                    "CFBundleShortVersionString": short_version,
                    "LSMinimumSystemVersion": "11.0",
                    SUPPORT_DIAGNOSTICS_ENDPOINT_INFO_KEY: "https://support.example",
                }
                app_path = make_app(Path(temp_dir), info)

                with self.assertRaisesRegex(sparkle_bundle.SparkleBundleError, "retired preview identity"):
                    sparkle_bundle.inspect_app_bundle(app_path, expected_info=expected, environment={})

    def test_inspect_app_bundle_rejects_missing_support_diagnostics_endpoint(self) -> None:
        expected = self.expected_info()
        info = {
            **expected,
            "CFBundleIdentifier": PRODUCTION_BUNDLE_IDENTIFIER,
            "CFBundleShortVersionString": "0.3.0b3",
            "LSMinimumSystemVersion": "11.0",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            app_path = make_app(Path(temp_dir), info)

            with self.assertRaisesRegex(sparkle_bundle.SparkleBundleError, "must be a non-empty valid HTTPS endpoint"):
                sparkle_bundle.inspect_app_bundle(app_path, expected_info=expected, environment={})

    def test_inspect_app_bundle_rejects_invalid_support_diagnostics_endpoint(self) -> None:
        expected = self.expected_info()
        invalid_endpoints = (
            "https://user:secret@support.example",
            "https://support.example/diagnostics",
            "https://support.example?token=secret",
            "https://support.example#fragment",
        )
        for endpoint in invalid_endpoints:
            with self.subTest(endpoint=endpoint), tempfile.TemporaryDirectory() as temp_dir:
                info = {
                    **expected,
                    "CFBundleIdentifier": PRODUCTION_BUNDLE_IDENTIFIER,
                    "CFBundleShortVersionString": "0.3.0b3",
                    "LSMinimumSystemVersion": "11.0",
                    SUPPORT_DIAGNOSTICS_ENDPOINT_INFO_KEY: endpoint,
                }
                app_path = make_app(Path(temp_dir), info)

                with self.assertRaisesRegex(
                    sparkle_bundle.SparkleBundleError,
                    "must be a non-empty valid HTTPS endpoint",
                ):
                    sparkle_bundle.inspect_app_bundle(app_path, expected_info=expected, environment={})

    def test_inspect_app_bundle_rejects_mismatched_support_diagnostics_endpoint(self) -> None:
        expected = self.expected_info()
        info = {
            **expected,
            "CFBundleIdentifier": PRODUCTION_BUNDLE_IDENTIFIER,
            "CFBundleShortVersionString": "0.3.0b3",
            "LSMinimumSystemVersion": "11.0",
            SUPPORT_DIAGNOSTICS_ENDPOINT_INFO_KEY: "https://support.example",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            app_path = make_app(Path(temp_dir), info)

            with self.assertRaisesRegex(sparkle_bundle.SparkleBundleError, "must exactly match the approved"):
                sparkle_bundle.inspect_app_bundle(
                    app_path,
                    expected_info=expected,
                    environment={SUPPORT_DIAGNOSTICS_ENDPOINT_ENV: "https://other-support.example"},
                )

    def test_repository_public_key_matches_briefcase_metadata(self) -> None:
        info = sparkle_bundle.load_expected_info()

        self.assertEqual(info["CFBundleVersion"], "149")
        self.assertEqual(info["SUPublicEDKey"], sparkle_bundle.PUBLIC_KEY_PATH.read_text().strip())


if __name__ == "__main__":
    unittest.main()
