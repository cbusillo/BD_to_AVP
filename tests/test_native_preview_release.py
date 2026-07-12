import importlib
import json
import plistlib
import subprocess
import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from scripts.native_app import (
    NATIVE_APP_NAME,
    NATIVE_BUILD_VERSION,
    NATIVE_BUNDLE_IDENTIFIER,
    NATIVE_EXECUTABLE_NAME,
    NATIVE_MINIMUM_SYSTEM_VERSION,
    NATIVE_PRODUCT_NAME,
    NATIVE_SHORT_VERSION,
)
from scripts.native_preview_release import (
    PREVIEW_DMG_NAME,
    PREVIEW_RELEASE_NAME,
    PREVIEW_TAG,
    NativePreviewReleaseError,
    create_preview_dmg,
    inspect_preview_info,
    mounted_dmg,
    notarize_and_staple,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
yaml = importlib.import_module("yaml")


def preview_info(**overrides: object) -> dict[str, object]:
    info: dict[str, object] = {
        "BluRayToVisionProEngineBundled": True,
        "CFBundleDisplayName": NATIVE_PRODUCT_NAME,
        "CFBundleExecutable": NATIVE_EXECUTABLE_NAME,
        "CFBundleIdentifier": NATIVE_BUNDLE_IDENTIFIER,
        "CFBundleName": NATIVE_PRODUCT_NAME,
        "CFBundleShortVersionString": NATIVE_SHORT_VERSION,
        "CFBundleVersion": NATIVE_BUILD_VERSION,
        "LSMinimumSystemVersion": NATIVE_MINIMUM_SYSTEM_VERSION,
        "MainModule": "bd_to_avp.worker",
    }
    info.update(overrides)
    return info


def write_preview_app(root: Path, **overrides: object) -> Path:
    app_path = root / NATIVE_APP_NAME
    info_path = app_path / "Contents" / "Info.plist"
    info_path.parent.mkdir(parents=True)
    with info_path.open("wb") as info_file:
        plistlib.dump(preview_info(**overrides), info_file)
    return app_path


def load_workflow() -> dict:
    with (REPO_ROOT / ".github" / "workflows" / "native-ui-preview.yml").open(encoding="utf-8") as handle:
        return yaml.load(handle, Loader=yaml.BaseLoader)


class NativePreviewIdentityTests(unittest.TestCase):
    def test_preview_release_identity_is_fixed_and_non_production(self) -> None:
        self.assertEqual(PREVIEW_TAG, "native-ui-preview-1")
        self.assertEqual(PREVIEW_RELEASE_NAME, "Native UI Preview 1")
        self.assertEqual(PREVIEW_DMG_NAME, "3D-Blu-ray-to-Vision-Pro-Native-Preview-0.3.0-1.dmg")
        self.assertNotEqual(NATIVE_BUNDLE_IDENTIFIER, "com.shinycomputers.bd-to-avp")

    def test_accepts_exact_preview_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            app_path = write_preview_app(Path(temporary_directory))

            metadata = inspect_preview_info(app_path)

        self.assertEqual(metadata.bundle_identifier, NATIVE_BUNDLE_IDENTIFIER)
        self.assertEqual(metadata.short_version, NATIVE_SHORT_VERSION)
        self.assertEqual(metadata.build_version, NATIVE_BUILD_VERSION)
        self.assertEqual(metadata.minimum_system_version, NATIVE_MINIMUM_SYSTEM_VERSION)

    def test_rejects_wrong_preview_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            app_path = write_preview_app(Path(temporary_directory), CFBundleShortVersionString="0.2.143")

            with self.assertRaisesRegex(NativePreviewReleaseError, "CFBundleShortVersionString"):
                inspect_preview_info(app_path)

    def test_rejects_production_update_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            app_path = write_preview_app(Path(temporary_directory), SUFeedURL="https://example.invalid/appcast.xml")

            with self.assertRaisesRegex(NativePreviewReleaseError, "production update metadata"):
                inspect_preview_info(app_path)

    def test_refuses_to_replace_an_existing_dmg(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            output_path = temporary_path / PREVIEW_DMG_NAME
            output_path.touch()

            with self.assertRaisesRegex(NativePreviewReleaseError, "Refusing to replace"):
                create_preview_dmg(temporary_path / NATIVE_APP_NAME, output_path)

    def test_notarization_requires_accepted_status_before_stapling(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["xcrun"],
            returncode=0,
            stdout='{"id":"submission","status":"Accepted"}',
            stderr="",
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            with patch("scripts.native_preview_release.subprocess.run", return_value=completed) as run_mock:
                payload = notarize_and_staple(
                    temporary_path / "submission.zip",
                    temporary_path / NATIVE_APP_NAME,
                    keychain_profile="preview-profile",
                    keychain_path=temporary_path / "build.keychain-db",
                    log_path=temporary_path / "notary.json",
                )

        self.assertEqual(payload["status"], "Accepted")
        self.assertEqual(run_mock.call_count, 3)

    def test_notarization_rejects_invalid_status_without_stapling(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["xcrun"],
            returncode=1,
            stdout='{"id":"submission","status":"Invalid","message":"signature rejected"}',
            stderr="",
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            with (
                patch("scripts.native_preview_release.subprocess.run", return_value=completed) as run_mock,
                self.assertRaisesRegex(NativePreviewReleaseError, "signature rejected"),
            ):
                notarize_and_staple(
                    temporary_path / "submission.zip",
                    temporary_path / NATIVE_APP_NAME,
                    keychain_profile="preview-profile",
                    keychain_path=temporary_path / "build.keychain-db",
                    log_path=temporary_path / "notary.json",
                )

        self.assertEqual(run_mock.call_count, 1)

    def test_malformed_dmg_attach_detaches_every_discovered_volume(self) -> None:
        attach_result = subprocess.CompletedProcess(
            args=["hdiutil"],
            returncode=0,
            stdout=plistlib.dumps(
                {
                    "system-entities": [
                        {"mount-point": "/Volumes/Preview One"},
                        {"mount-point": "/Volumes/Preview Two"},
                    ]
                }
            ),
            stderr=b"",
        )
        detach_result = subprocess.CompletedProcess(args=["hdiutil"], returncode=0, stdout="", stderr="")

        with (
            patch(
                "scripts.native_preview_release.subprocess.run",
                side_effect=[attach_result, detach_result, detach_result],
            ) as run_mock,
            self.assertRaisesRegex(NativePreviewReleaseError, "Expected one mounted DMG volume"),
        ):
            with mounted_dmg(Path("preview.dmg")):
                self.fail("Malformed DMG should not yield a mount point.")

        detach_commands = [call.args[0] for call in run_mock.call_args_list[1:]]
        self.assertEqual(
            detach_commands,
            [
                ["hdiutil", "detach", "/Volumes/Preview Two"],
                ["hdiutil", "detach", "/Volumes/Preview One"],
            ],
        )


class NativePreviewWorkflowTests(unittest.TestCase):
    def test_workflow_is_manual_main_only_and_uses_release_runner(self) -> None:
        workflow = load_workflow()
        prepare = workflow["jobs"]["prepare"]
        package = workflow["jobs"]["package"]

        self.assertEqual(set(workflow["on"]), {"workflow_dispatch"})
        self.assertEqual(workflow["concurrency"]["group"], "release")
        self.assertEqual(workflow["concurrency"]["cancel-in-progress"], "false")
        self.assertEqual(
            set(package["runs-on"]),
            {"self-hosted", "macOS", "ARM64", "bd-to-avp-release"},
        )
        self.assertEqual(package["environment"], "macos-signing")
        self.assertEqual(package["permissions"]["contents"], "read")
        self.assertIn("refs/heads/main", str(prepare))
        self.assertIn("refs/remotes/origin/main", str(prepare))
        self.assertIn("Xcode 27", str(package))
        self.assertIn("XCODEGEN_VERSION", str(workflow))
        self.assertIn("sw_vers", str(package))
        self.assertIn("must not override", str(package))

    def test_workflow_isolated_from_production_channels(self) -> None:
        workflow = load_workflow()
        workflow_text = str(workflow).lower()

        self.assertNotIn("appcast", workflow_text)
        self.assertNotIn("sparkle", workflow_text)
        self.assertNotIn("pypi", workflow_text)
        self.assertNotIn("pages", workflow_text)
        self.assertNotIn("scripts.release", workflow_text)
        self.assertNotIn("certificate_installer", workflow_text)
        self.assertNotIn("default-keychain", workflow_text)
        self.assertNotIn(" -a -t", workflow_text)
        self.assertNotIn("--deny-self-hosted-runners", workflow_text)
        self.assertIn("--prerelease", workflow_text)
        self.assertIn("--latest=false", workflow_text)
        self.assertIn("--sign-keychain", workflow_text)
        self.assertIn('make_latest: "false"', workflow_text)
        self.assertIn("$runner_temp/native-preview-app", workflow_text)
        self.assertIn("dist/notary/*.json", workflow_text)
        self.assertIn("unpublish.json", workflow_text)
        self.assertIn("critical: latest-release validation failed", workflow_text)
        self.assertNotIn("--input unpublish.json >/dev/null || true", workflow_text)
        self.assertIn('"$total_count" = "2"', workflow_text)

    def test_workflow_attests_and_revalidates_exact_assets(self) -> None:
        workflow = load_workflow()
        package = workflow["jobs"]["package"]
        publish = workflow["jobs"]["publish"]
        config = json.loads((REPO_ROOT / ".github" / "github.json").read_text(encoding="utf-8"))

        self.assertNotIn("attest-package", workflow["jobs"])
        self.assertEqual(package["permissions"]["attestations"], "write")
        self.assertEqual(package["permissions"]["id-token"], "write")
        self.assertIn("actions/attest@", str(package))
        self.assertEqual(publish["permissions"]["contents"], "write")
        self.assertIn("gh attestation verify", str(publish))
        self.assertIn("native-ui-preview.yml", str(publish))
        self.assertIn("SHA256SUMS", str(publish))
        self.assertIn("PREVIEW_TAG", str(publish))
        self.assertIn(".github/workflows/native-ui-preview.yml", config["docs"]["releaseWorkflows"])
        self.assertIn("Publish Native UI Preview 1", config["importantWorkflows"])


if __name__ == "__main__":
    unittest.main()
