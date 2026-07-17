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
    NATIVE_PRERELEASE_VERSION,
    NATIVE_PRODUCT_NAME,
    NATIVE_SHORT_VERSION,
)
from scripts.native_preview_release import (
    PREVIEW_RELEASE_METADATA,
    NativePreviewReleaseError,
    create_preview_release_metadata,
    create_preview_dmg,
    inspect_preview_info,
    main,
    mounted_dmg,
    notarize_and_staple,
    parse_args,
    smoke_native_app_startup,
    smoke_packaged_tools,
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
    def test_preview_release_identity_is_derived_and_non_production(self) -> None:
        self.assertEqual(PREVIEW_RELEASE_METADATA.prerelease_version, NATIVE_PRERELEASE_VERSION)
        self.assertEqual(PREVIEW_RELEASE_METADATA.release_tag, f"v{NATIVE_PRERELEASE_VERSION}")
        self.assertRegex(
            PREVIEW_RELEASE_METADATA.release_tag,
            r"^v\d+\.\d+\.\d+-(?:alpha|beta|rc)\.[1-9]\d*$",
        )
        self.assertEqual(
            PREVIEW_RELEASE_METADATA.release_name,
            f"v{NATIVE_PRERELEASE_VERSION}",
        )
        self.assertTrue(PREVIEW_RELEASE_METADATA.release_name.startswith(f"v{NATIVE_SHORT_VERSION}-"))
        self.assertEqual(
            PREVIEW_RELEASE_METADATA.dmg_name,
            f"3D-Blu-ray-to-Vision-Pro-Native-Preview-{NATIVE_PRERELEASE_VERSION}.dmg",
        )
        self.assertEqual(PREVIEW_RELEASE_METADATA.app_name, NATIVE_APP_NAME)
        self.assertNotEqual(NATIVE_BUNDLE_IDENTIFIER, "com.shinycomputers.bd-to-avp")

    def test_metadata_command_writes_github_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_path = Path(temporary_directory) / "github-output"
            with patch("builtins.print"):
                main(["metadata", "--github-output", str(output_path)])

            outputs = dict(line.split("=", maxsplit=1) for line in output_path.read_text(encoding="utf-8").splitlines())

        self.assertEqual(outputs, PREVIEW_RELEASE_METADATA.github_outputs())

    def test_release_metadata_rejects_invalid_version_or_build(self) -> None:
        with self.assertRaisesRegex(NativePreviewReleaseError, "three numeric components"):
            create_preview_release_metadata(short_version="0.3")
        with self.assertRaisesRegex(NativePreviewReleaseError, "positive integer"):
            create_preview_release_metadata(build_version="0")
        with self.assertRaisesRegex(NativePreviewReleaseError, "prerelease version"):
            create_preview_release_metadata(prerelease_version="0.3.0")
        with self.assertRaisesRegex(NativePreviewReleaseError, "short version"):
            create_preview_release_metadata(prerelease_version="0.4.0-beta.1")

    def test_verify_dmg_accepts_native_and_tool_smoke_flags(self) -> None:
        args = parse_args(
            [
                "verify-dmg",
                "--dmg",
                "/tmp/preview.dmg",
                "--smoke-app",
                "--smoke-tools",
                "--smoke-worker",
            ]
        )

        self.assertTrue(args.smoke_app)
        self.assertTrue(args.smoke_tools)
        self.assertTrue(args.smoke_worker)

    def test_native_startup_smoke_uses_explicit_exit_argument(self) -> None:
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with tempfile.TemporaryDirectory() as temporary_directory:
            app_path = Path(temporary_directory) / NATIVE_APP_NAME
            executable = app_path / "Contents" / "MacOS" / NATIVE_EXECUTABLE_NAME
            executable.parent.mkdir(parents=True)
            executable.touch()
            with patch("scripts.native_preview_release.subprocess.run", return_value=completed) as run_mock:
                smoke_native_app_startup(app_path)

        command = run_mock.call_args.args[0]
        self.assertEqual(command, [str(executable), "--startup-smoke"])
        self.assertEqual(run_mock.call_args.kwargs["timeout"], 20)

    def test_packaged_tool_smoke_probes_release_tool_set(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            app_path = Path(temporary_directory) / NATIVE_APP_NAME
            with patch("scripts.native_preview_release.verify_tool") as verify_tool_mock:
                smoke_packaged_tools(app_path)

        self.assertGreaterEqual(verify_tool_mock.call_count, 6)

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
            output_path = temporary_path / PREVIEW_RELEASE_METADATA.dmg_name
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
        compatibility = workflow["jobs"]["compatibility"]

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
        self.assertIn("semantic prerelease identifier", str(prepare))
        self.assertNotIn("outside the production semver namespace", str(prepare))
        self.assertIn("Xcode 27", str(package))
        self.assertIn("XCODEGEN_VERSION", str(workflow))
        self.assertIn("sw_vers", str(package))
        self.assertIn("must not override", str(package))
        self.assertNotIn("head -1", str(package))
        self.assertNotIn("xcodebuild -version |", str(package))
        self.assertNotIn("actions/setup-python", str(package))
        self.assertIn("uv python install 3.12", str(package))
        self.assertEqual(workflow["name"], "Publish Native UI Preview")
        self.assertIn("python -m scripts.native_preview_release metadata", str(prepare))
        self.assertIn("needs.prepare.outputs.app_name", str(package))
        self.assertIn("needs.prepare.outputs.dmg_name", str(package))
        self.assertEqual(compatibility["runs-on"], "macos-26")
        self.assertEqual(compatibility["needs"], ["package"])
        self.assertIn("actions/setup-python", str(compatibility))
        self.assertIn("verify-dmg", str(compatibility))
        self.assertIn("--smoke-app", str(compatibility))
        self.assertIn("--smoke-tools", str(compatibility))
        self.assertIn("--smoke-worker", str(compatibility))
        self.assertIn("sw_vers", str(compatibility))

    def test_workflow_isolated_from_production_channels(self) -> None:
        workflow = load_workflow()
        workflow_text = str(workflow).lower()
        workflow_source = (REPO_ROOT / ".github" / "workflows" / "native-ui-preview.yml").read_text(encoding="utf-8")

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
        self.assertEqual(
            workflow_source.count('releases/latest" \\\n            --jq .tag_name)'),
            2,
        )
        self.assertIn('"$total_count" = "2"', workflow_text)

    def test_workflow_restores_failed_publication_to_resumable_draft(self) -> None:
        workflow = load_workflow()
        publish_steps = {step["name"]: step for step in workflow["jobs"]["publish"]["steps"]}
        restore = publish_steps["Restore failed preview publication to draft"]
        restore_text = str(restore).lower()

        self.assertEqual(restore["if"], "failure() && steps.draft_release.outputs.release_id != ''")
        self.assertIn("steps.draft_release.outputs.release_id", restore_text)
        self.assertIn("draft: true", restore_text)
        self.assertIn("target_commitish", restore_text)
        self.assertEqual(restore_text.count("for attempt in {1..5}"), 2)
        self.assertIn("could not be inspected for draft restoration", restore_text)
        self.assertIn("refusing to modify it", restore_text)
        self.assertIn("could not be restored to draft", restore_text)

    def test_workflow_attests_and_revalidates_exact_assets(self) -> None:
        workflow = load_workflow()
        package = workflow["jobs"]["package"]
        compatibility = workflow["jobs"]["compatibility"]
        publish = workflow["jobs"]["publish"]
        config = json.loads((REPO_ROOT / ".github" / "github.json").read_text(encoding="utf-8"))

        self.assertNotIn("attest-package", workflow["jobs"])
        self.assertEqual(package["permissions"]["attestations"], "write")
        self.assertEqual(package["permissions"]["id-token"], "write")
        self.assertIn("actions/attest@", str(package))
        self.assertEqual(publish["permissions"]["contents"], "write")
        self.assertEqual(set(publish["needs"]), {"prepare", "package", "compatibility"})
        self.assertIn("native-preview-package", str(compatibility))
        self.assertIn("gh attestation verify", str(publish))
        self.assertIn("native-ui-preview.yml", str(publish))
        self.assertIn("SHA256SUMS", str(publish))
        self.assertIn("PREVIEW_TAG", str(publish))
        self.assertIn("needs.prepare.outputs.release_name", str(publish))
        self.assertIn("needs.prepare.outputs.release_tag", str(publish))
        self.assertIn("docs/native-ui-preview.md", str(publish))
        self.assertNotIn("Native UI Preview 1", str(workflow))
        self.assertNotIn(PREVIEW_RELEASE_METADATA.app_name, str(workflow))
        self.assertNotIn(PREVIEW_RELEASE_METADATA.dmg_name, str(workflow))
        self.assertNotIn(PREVIEW_RELEASE_METADATA.release_name, str(workflow))
        self.assertNotIn(PREVIEW_RELEASE_METADATA.release_tag, str(workflow))
        self.assertIn(".github/workflows/native-ui-preview.yml", config["docs"]["releaseWorkflows"])
        self.assertIn("Publish Native UI Preview", config["importantWorkflows"])


if __name__ == "__main__":
    unittest.main()
