import io
import importlib
import json
import plistlib
import subprocess
import sys
import tempfile
import unittest

from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts.macos_release import (
    MacOSReleaseError,
    create_release_dmg,
    main,
    mounted_dmg,
    notarize_and_staple,
    parse_args,
    run,
    smoke_packaged_tools,
    verify_release_app,
)
from scripts.native_app import NATIVE_APP_NAME
from scripts.sparkle_bundle import SparkleBundleMetadata
from scripts.verify_app_tools import REQUIRED_TOOLS

REPO_ROOT = Path(__file__).resolve().parents[1]
yaml = importlib.import_module("yaml")


def release_metadata(app_path: Path) -> SparkleBundleMetadata:
    return SparkleBundleMetadata(
        app_path=app_path.as_posix(),
        bundle_identifier="com.shinycomputers.bd-to-avp",
        build_version="146",
        short_version="0.2.143",
        distribution_channel="direct",
        support_diagnostics_endpoint="https://support.example",
        feed_url="https://cbusillo.github.io/BD_to_AVP/appcast.xml",
        minimum_system_version="26.0",
        public_key="test-key",
    )


class MacOSReleaseArtifactTests(unittest.TestCase):
    def test_release_tool_stdout_is_routed_away_from_cli_metadata(self) -> None:
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout=None, stderr=None)

        with patch("scripts.macos_release.subprocess.run", return_value=completed) as run_mock:
            result = run(["release-tool", "--probe"])

        self.assertIs(result, completed)
        run_mock.assert_called_once_with(
            ["release-tool", "--probe"],
            check=True,
            text=True,
            stdout=sys.stderr,
        )

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

    def test_verify_dmg_command_emits_only_metadata_on_stdout(self) -> None:
        dmg_path = Path("/tmp/release.dmg")
        metadata = release_metadata(Path("/Volumes/Release") / NATIVE_APP_NAME)
        stdout = io.StringIO()
        stderr = io.StringIO()

        def verify_dmg(*_args: object, **_kwargs: object) -> SparkleBundleMetadata:
            print("Processing: release.dmg", file=sys.stderr)
            return metadata

        with (
            patch("scripts.macos_release.verify_release_dmg", side_effect=verify_dmg),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            main(["verify-dmg", "--dmg", str(dmg_path), "--verify-distribution"])

        emitted = json.loads(stdout.getvalue())
        self.assertEqual(emitted["short_version"], metadata.short_version)
        self.assertNotIn("Processing:", stdout.getvalue())
        self.assertIn("Processing: release.dmg", stderr.getvalue())

    def test_verify_release_app_runs_only_the_requested_smokes(self) -> None:
        app_path = Path("/tmp") / NATIVE_APP_NAME
        metadata = release_metadata(app_path)
        smoke_flags = {
            "smoke_app": "smoke_native_app",
            "smoke_tools": "smoke_packaged_tools",
            "smoke_worker": "smoke_packaged_worker",
        }

        for requested in (None, *smoke_flags):
            with (
                self.subTest(requested=requested),
                patch("scripts.macos_release.verify_layout"),
                patch("scripts.macos_release.inspect_app_bundle", return_value=metadata),
                patch("scripts.macos_release.smoke_native_app") as smoke_native,
                patch("scripts.macos_release.smoke_packaged_tools") as smoke_tools,
                patch("scripts.macos_release.smoke_packaged_worker") as smoke_worker,
            ):
                smokes = {
                    "smoke_native_app": smoke_native,
                    "smoke_packaged_tools": smoke_tools,
                    "smoke_packaged_worker": smoke_worker,
                }
                flags = {flag: flag == requested for flag in smoke_flags}

                verify_release_app(app_path, **flags)

                ran = {name for name, smoke in smokes.items() if smoke.called}
                self.assertEqual(ran, {smoke_flags[requested]} if requested else set())

    def test_verify_release_app_without_signatures_keeps_layout_validation(self) -> None:
        app_path = Path("/tmp") / NATIVE_APP_NAME
        metadata = release_metadata(app_path)

        with (
            patch("scripts.macos_release.verify_layout") as verify_layout,
            patch("scripts.macos_release.inspect_app_bundle", return_value=metadata) as inspect_bundle,
        ):
            result = verify_release_app(app_path, verify_signatures=False)

        self.assertEqual(result, metadata)
        verify_layout.assert_called_once_with(app_path)
        inspect_bundle.assert_called_once_with(app_path, verify_signatures=False)

    def test_create_release_dmg_allows_explicit_unsigned_pre_signing_package(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            app_path = root / NATIVE_APP_NAME
            output_path = root / "release.dmg"
            metadata = release_metadata(app_path)

            def release_tool(command: list[str]) -> subprocess.CompletedProcess[str]:
                if command[:4] == ["diskutil", "image", "create", "from"]:
                    Path(command[-1]).write_bytes(b"dmg")
                return subprocess.CompletedProcess(command, 0, "", "")

            with (
                patch("scripts.macos_release.verify_layout") as verify_layout,
                patch("scripts.macos_release.inspect_app_bundle", return_value=metadata) as inspect_bundle,
                patch("scripts.macos_release.run", side_effect=release_tool),
            ):
                result = create_release_dmg(app_path, output_path, verify_signatures=False)

        self.assertEqual(result, output_path)
        verify_layout.assert_called_once_with(app_path)
        inspect_bundle.assert_called_once_with(app_path, verify_signatures=False)

    def test_packaged_tool_smoke_probes_release_tool_set(self) -> None:
        app_path = Path("/tmp") / NATIVE_APP_NAME
        with patch("scripts.macos_release.verify_tool") as verify_tool:
            smoke_packaged_tools(app_path)

        probed = {call.args[0].name: call.args[1] for call in verify_tool.call_args_list}
        self.assertEqual(probed, REQUIRED_TOOLS)

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
    def test_a_draft_release_waits_for_the_compatibility_check(self) -> None:
        workflow_path = REPO_ROOT / ".github" / "workflows" / "release-engine.yml"
        workflow = yaml.load(workflow_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        self.assertIn("compatibility", workflow["jobs"]["create-draft"]["needs"])


if __name__ == "__main__":
    unittest.main()
