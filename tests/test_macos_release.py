import importlib
import json
import plistlib
import subprocess
import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from scripts.macos_release import (
    MacOSReleaseError,
    create_release_dmg,
    main,
    mounted_dmg,
    notarize_and_staple,
    parse_args,
    smoke_native_app_startup,
    smoke_packaged_tools,
    verify_release_app,
)
from scripts.native_app import NATIVE_APP_NAME, NATIVE_EXECUTABLE_NAME
from scripts.sparkle_bundle import SparkleBundleMetadata

REPO_ROOT = Path(__file__).resolve().parents[1]
yaml = importlib.import_module("yaml")


def release_metadata(app_path: Path) -> SparkleBundleMetadata:
    return SparkleBundleMetadata(
        app_path=app_path.as_posix(),
        bundle_identifier="com.shinycomputers.bd-to-avp",
        build_version="146",
        short_version="0.2.143",
        distribution_channel="direct",
        feed_url="https://cbusillo.github.io/BD_to_AVP/appcast.xml",
        minimum_system_version="26.0",
        public_key="test-key",
    )


class MacOSReleaseArtifactTests(unittest.TestCase):
    def test_verify_dmg_accepts_app_and_tool_smoke_flags(self) -> None:
        args = parse_args(
            [
                "verify-dmg",
                "--dmg",
                "/tmp/release.dmg",
                "--smoke-app",
                "--smoke-tools",
                "--smoke-worker",
            ]
        )

        self.assertTrue(args.smoke_app)
        self.assertTrue(args.smoke_tools)
        self.assertTrue(args.smoke_worker)

    def test_verify_release_app_delegates_identity_and_smoke_checks(self) -> None:
        app_path = Path("/tmp") / NATIVE_APP_NAME
        metadata = release_metadata(app_path)

        with (
            patch("scripts.macos_release.verify_layout") as verify_layout,
            patch("scripts.macos_release.inspect_app_bundle", return_value=metadata) as inspect_bundle,
            patch("scripts.macos_release.smoke_native_app_startup") as smoke_app,
            patch("scripts.macos_release.smoke_packaged_tools") as smoke_tools,
            patch("scripts.macos_release.smoke_packaged_worker") as smoke_worker,
        ):
            result = verify_release_app(
                app_path,
                verify_signatures=True,
                smoke_app=True,
                smoke_tools=True,
                smoke_worker=True,
            )

        self.assertEqual(result, metadata)
        verify_layout.assert_called_once_with(app_path)
        inspect_bundle.assert_called_once_with(app_path, verify_signatures=True)
        smoke_app.assert_called_once_with(app_path)
        smoke_tools.assert_called_once_with(app_path)
        smoke_worker.assert_called_once_with(app_path)

    def test_native_startup_smoke_uses_explicit_exit_argument(self) -> None:
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        app_path = Path("/tmp") / NATIVE_APP_NAME
        with patch("scripts.macos_release.subprocess.run", return_value=completed) as run_mock:
            smoke_native_app_startup(app_path)

        command = run_mock.call_args.args[0]
        self.assertEqual(command, [str(app_path / "Contents" / "MacOS" / NATIVE_EXECUTABLE_NAME), "--startup-smoke"])

    def test_packaged_tool_smoke_probes_release_tool_set(self) -> None:
        app_path = Path("/tmp") / NATIVE_APP_NAME
        with patch("scripts.macos_release.verify_tool") as verify_tool:
            smoke_packaged_tools(app_path)

        tool_names = {call.args[0].name for call in verify_tool.call_args_list}
        self.assertEqual(
            tool_names,
            {"MP4Box", "edge264_test", "ffmpeg", "ffprobe", "fx-upscale", "spatial-media-kit-tool"},
        )

    def test_refuses_to_replace_an_existing_dmg(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            app_path = root / NATIVE_APP_NAME
            output_path = root / "release.dmg"
            output_path.write_bytes(b"existing")

            with self.assertRaisesRegex(MacOSReleaseError, "Refusing to replace"):
                create_release_dmg(app_path, output_path)

    def test_notarization_requires_accepted_status_before_stapling(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({"status": "Accepted", "id": "submission"}),
            stderr="",
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            log_path = root / "notary.json"
            with (
                patch("scripts.macos_release.subprocess.run", return_value=completed),
                patch("scripts.macos_release.run") as run_mock,
            ):
                payload = notarize_and_staple(
                    root / "release.dmg",
                    root / "release.dmg",
                    keychain_profile="release-profile",
                    keychain_path=root / "release.keychain-db",
                    log_path=log_path,
                )
            log_contents = log_path.read_text(encoding="utf-8")

        self.assertEqual(payload["status"], "Accepted")
        self.assertTrue(log_contents)
        self.assertEqual(run_mock.call_count, 2)

    def test_notarization_rejects_invalid_status_without_stapling(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout=json.dumps({"status": "Invalid", "message": "signature failure"}),
            stderr="",
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with (
                patch("scripts.macos_release.subprocess.run", return_value=completed),
                patch("scripts.macos_release.run") as run_mock,
                self.assertRaisesRegex(MacOSReleaseError, "signature failure"),
            ):
                notarize_and_staple(
                    root / "release.dmg",
                    root / "release.dmg",
                    keychain_profile="release-profile",
                    keychain_path=root / "release.keychain-db",
                    log_path=root / "notary.json",
                )

        run_mock.assert_not_called()

    def test_malformed_dmg_attach_detaches_every_discovered_volume(self) -> None:
        payload = plistlib.dumps(
            {
                "system-entities": [
                    {"mount-point": "/Volumes/Release One"},
                    {"mount-point": "/Volumes/Release Two"},
                ]
            }
        )
        attach = subprocess.CompletedProcess(args=[], returncode=0, stdout=payload, stderr=b"")
        detach = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        with (
            patch("scripts.macos_release.subprocess.run", side_effect=[attach, detach, detach]) as run_mock,
            self.assertRaisesRegex(MacOSReleaseError, "found 2"),
        ):
            with mounted_dmg(Path("release.dmg")):
                self.fail("Malformed mounts must not be yielded.")

        detach_commands = [call.args[0] for call in run_mock.call_args_list[1:]]
        self.assertEqual(
            detach_commands,
            [
                ["hdiutil", "detach", "/Volumes/Release Two"],
                ["hdiutil", "detach", "/Volumes/Release One"],
            ],
        )

    def test_verify_app_command_emits_metadata(self) -> None:
        app_path = Path("/tmp") / NATIVE_APP_NAME
        metadata = release_metadata(app_path)
        with (
            patch("scripts.macos_release.verify_release_app", return_value=metadata),
            patch("builtins.print") as print_mock,
        ):
            main(["verify-app", "--app", str(app_path)])

        emitted = json.loads(print_mock.call_args.args[0])
        self.assertEqual(emitted["bundle_identifier"], "com.shinycomputers.bd-to-avp")


class MacOSReleaseWorkflowTests(unittest.TestCase):
    def test_production_workflow_owns_macos_packaging_and_compatibility(self) -> None:
        workflow_path = REPO_ROOT / ".github" / "workflows" / "briefcase.yml"
        workflow = yaml.load(workflow_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        package = workflow["jobs"]["package"]
        compatibility = workflow["jobs"]["compatibility"]
        create_draft = workflow["jobs"]["create-draft"]

        self.assertEqual(package["runs-on"], ["self-hosted", "macOS", "ARM64", "bd-to-avp-release"])
        self.assertIn("python scripts/native_app.py package", str(package))
        self.assertIn("python -m scripts.macos_release", str(package))
        self.assertNotIn("python -m scripts.briefcase_app package", str(package))
        self.assertEqual(compatibility["runs-on"], "macos-26")
        self.assertIn("--smoke-app", str(compatibility))
        self.assertIn("--smoke-tools", str(compatibility))
        self.assertIn("--smoke-worker", str(compatibility))
        self.assertIn("compatibility", create_draft["needs"])
        self.assertFalse((REPO_ROOT / ".github" / "workflows" / "native-ui-preview.yml").exists())


if __name__ == "__main__":
    unittest.main()
