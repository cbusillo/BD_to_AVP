import base64
import json
import platform
import plistlib
import shutil
import subprocess
import tempfile
import unittest

from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock, patch

from scripts import sparkle_appcast
from scripts.artifact_identity import app_tree_sha256
from scripts.release_receipt import build_receipt as build_release_receipt
from scripts.release_receipt import write_receipt
from scripts.tier3_clean_machine import (
    APP_NAME,
    BUNDLE_IDENTIFIER,
    CleanMachineError,
    EnvironmentFacts,
    MacOSOperations,
    QualificationConfig,
    QualificationOperations,
    RELEASES_URL,
    ReleaseArtifact,
    SparkleUpdateFailure,
    SparkleUpdateState,
    SPARKLE_INSTALL_ACTIONS,
    UpdateObservation,
    file_sha256,
    parse_feed_candidate,
    preflight_report,
    run_qualification,
    validate_release_order,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
CANDIDATE_SHA = "b" * 40
PRIOR_SHA = "a" * 40
SIGNATURE = base64.b64encode(b"s" * 64).decode("ascii")


class FakeOperations(QualificationOperations):
    def __init__(
        self,
        app_sources: dict[Path, Path],
        feed_bytes: bytes,
        candidate_dmg: Path,
        *,
        macos_version: str = "26.0",
        update_failure: bool = False,
        retryable_failures: int = 0,
        post_press_failure: bool = False,
        terminal_failure: bool = False,
        cancelled: bool = False,
        tamper_prior_before_update: bool = False,
        wrong_running_path: bool = False,
        staged_repeats_after_press: int = 0,
        press_state_changed: bool = False,
        oscillate_non_actionable: bool = False,
        sentinel_failure: bool = False,
        tamper_marker: bool = False,
        install_action: str = "Install and Relaunch",
    ) -> None:
        self.app_sources = app_sources
        self.feed_bytes = feed_bytes
        self.macos_version = macos_version
        self.update_failure = update_failure
        self.retryable_failures = retryable_failures
        self.post_press_failure = post_press_failure
        self.terminal_failure = terminal_failure
        self.cancelled = cancelled
        self.tamper_prior_before_update = tamper_prior_before_update
        self.wrong_running_path = wrong_running_path
        self.staged_repeats_after_press = staged_repeats_after_press
        self.press_state_changed = press_state_changed
        self.oscillate_non_actionable = oscillate_non_actionable
        self.sentinel_failure = sentinel_failure
        self.tamper_marker = tamper_marker
        self.install_action = install_action
        self.preferences: dict[str, str] = {}
        self.running = False
        self.candidate_source = app_sources[candidate_dmg.resolve()]
        self.pressed_actions: list[UpdateObservation] = []
        self.update_attempts = 0
        self.intent_seen_before_press = False
        self.current_app_path: Path | None = None
        self.current_synthetic_home: Path | None = None
        self.launch_calls = 0
        self.quit_calls = 0
        self.running_app_path: Path | None = None
        self.post_staged_observations = 0
        self.observation_calls = 0

    def inspect_environment(self, qualification_root: Path, environment_class: str) -> EnvironmentFacts:
        return EnvironmentFacts(
            environment_class=environment_class,
            architecture="arm64",
            macos_version=self.macos_version,
            macos_build="25A123",
            free_bytes=20 * 1024 * 1024 * 1024,
            accessibility_enabled=True,
            homebrew_present=True,
            app_running=self.running,
            required_tools=(),
        )

    def fetch_live_feed(self) -> bytes:
        return self.feed_bytes

    def install_app(self, dmg_path: Path, destination: Path) -> None:
        shutil.copytree(self.app_sources[dmg_path], destination, symlinks=True)

    def smoke_app(self, app_path: Path, synthetic_home: Path, log_path: Path) -> str:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("smoke passed\n", encoding="utf-8")
        return file_sha256(log_path)

    def write_preferences(self, synthetic_home: Path, route: str) -> None:
        self.preferences = {
            "BDToAVPUpdateChannel": route,
            "BDToAVPTier3Sentinel": "changed" if self.sentinel_failure else "tier3-preserve",
            "SUEnableAutomaticChecks": "0",
        }

    def read_preference(self, synthetic_home: Path, key: str) -> str:
        return self.preferences[key]

    def launch_app(self, app_path: Path, synthetic_home: Path) -> None:
        self.launch_calls += 1
        self.running = True
        self.running_app_path = app_path

    def open_updater(self, app_path: Path, synthetic_home: Path) -> None:
        self.launch_app(app_path, synthetic_home)
        self.current_app_path = app_path
        self.current_synthetic_home = synthetic_home
        self.update_attempts += 1
        if self.retryable_failures > 0:
            self.retryable_failures -= 1
            raise SparkleUpdateFailure(
                "simulated application process timeout",
                reason_code="application-process-timeout",
                state=SparkleUpdateState.WAITING_FOR_WINDOW,
                action_pressed=False,
            )
        if self.update_failure:
            raise SparkleUpdateFailure(
                "simulated Sparkle failure",
                reason_code="updater-script-failure",
                state=SparkleUpdateState.UNKNOWN,
                action_pressed=False,
            )

    def observe_updater(self) -> UpdateObservation:
        self.observation_calls += 1
        if self.oscillate_non_actionable:
            state = SparkleUpdateState.DOWNLOADING if self.observation_calls % 2 else SparkleUpdateState.INSTALLING
            return UpdateObservation(
                state=state,
                window_match="identifier",
                action_identifier="SPUUserUpdateChoiceInstall",
                action_title="Install Update",
            )
        if self.post_press_failure and self.pressed_actions:
            raise SparkleUpdateFailure(
                "simulated post-press failure",
                reason_code="updater-script-failure",
                state=SparkleUpdateState.INSTALLING,
                action_pressed=True,
            )
        if self.terminal_failure:
            return UpdateObservation(
                state=SparkleUpdateState.TERMINAL_FAILURE,
                window_match="owned-dialog",
                action_identifier="",
                action_title="",
            )
        if self.cancelled:
            return UpdateObservation(
                state=SparkleUpdateState.CANCELLED,
                window_match="none",
                action_identifier="",
                action_title="",
            )
        action_states = {
            "Install and Relaunch": (
                SparkleUpdateState.READY_INSTALL_RELAUNCH,
                "",
                "Install and Relaunch",
            ),
            "Install on Quit": (
                SparkleUpdateState.READY_INSTALL_ON_QUIT,
                "",
                "Install on Quit",
            ),
            "SPUUserUpdateChoiceInstall": (
                SparkleUpdateState.READY_INSTALL_RELAUNCH,
                "SPUUserUpdateChoiceInstall",
                "Install and Relaunch",
            ),
        }
        if self.install_action == "Install Update":
            staged_observation = UpdateObservation(
                state=SparkleUpdateState.STAGED_INSTALL,
                window_match="identifier",
                action_identifier="SPUUserUpdateChoiceInstall",
                action_title="Install Update",
            )
            if not self.pressed_actions:
                observation = staged_observation
            elif self.post_staged_observations < self.staged_repeats_after_press:
                self.post_staged_observations += 1
                observation = staged_observation
            else:
                observation = UpdateObservation(
                    state=SparkleUpdateState.READY_INSTALL_RELAUNCH,
                    window_match="identifier",
                    action_identifier="SPUUserUpdateChoiceInstall",
                    action_title="Install and Relaunch",
                )
        elif self.install_action in action_states:
            state, identifier, title = action_states[self.install_action]
            observation = UpdateObservation(
                state=state,
                window_match="identifier" if identifier else "button-identifier",
                action_identifier=identifier,
                action_title=title,
            )
        else:
            observation = UpdateObservation(
                state=SparkleUpdateState.UNKNOWN,
                window_match="identifier",
                action_identifier="",
                action_title=self.install_action,
            )
        return observation

    def press_updater_action(self, observation: UpdateObservation) -> None:
        if self.current_app_path is None or self.current_synthetic_home is None:
            raise AssertionError("fake updater was pressed before it was opened")
        checkpoint_path = self.current_synthetic_home.parent / ".bd-to-avp-sparkle-update.json"
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        transitions = checkpoint["transitions"]
        self.intent_seen_before_press = bool(transitions and transitions[-1]["phase"] == "intent")
        if self.press_state_changed:
            raise SparkleUpdateFailure(
                "simulated updater state change",
                reason_code="updater-state-changed",
                state=observation.state,
                action_pressed=False,
            )
        self.pressed_actions.append(observation)
        if observation.state == SparkleUpdateState.STAGED_INSTALL:
            return
        if self.tamper_marker:
            marker_path = self.current_synthetic_home.parent / ".bd-to-avp-tier3-owned.json"
            marker_path.write_text('{"owner":"changed"}\n', encoding="utf-8")
        app_path = self.current_app_path
        shutil.rmtree(app_path)
        shutil.copytree(self.candidate_source, app_path, symlinks=True)
        self.running = True
        self.running_app_path = self.candidate_source if self.wrong_running_path else app_path

    def collect_ui_evidence(
        self,
        *,
        repo: Path,
        phase: str,
        app_path: Path,
        synthetic_home: Path,
        output_directory: Path,
        release_notes_url: str,
    ) -> None:
        if phase == "updater" and self.tamper_prior_before_update:
            (app_path / "tampered-before-updater").write_text("changed\n", encoding="utf-8")
        del repo, app_path, synthetic_home
        output_directory.mkdir(parents=True, exist_ok=True)
        if phase == "updater":
            (output_directory / "updater-ui.json").write_text(
                json.dumps(
                    {
                        "install_action": self.install_action,
                        "release_notes_url": release_notes_url,
                        "release_notes_url_observed": True,
                        "schema_version": 1,
                        "status": "passed",
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            return
        if phase != "candidate":
            raise AssertionError(f"unexpected UI phase: {phase}")
        (output_directory / "candidate-ui.json").write_text(
            json.dumps(
                {
                    "main_window_ready": True,
                    "profile_document_version": 6,
                    "profile_save_accessible": True,
                    "profile_save_succeeded": True,
                    "profiles_after": 1,
                    "release_page_url": RELEASES_URL,
                    "release_page_url_observed": True,
                    "schema_version": 1,
                    "status": "passed",
                    "updater_controls_accessible": True,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        records = [
            {
                "actions": ["AXPress"],
                "enabled": True,
                "help": "Opens a form to name and save these settings as a reusable profile",
                "identifier": "save-profile-action",
                "label": "Save current settings as new profile",
                "role": "AXButton",
            },
            {
                "actions": ["AXPress"],
                "enabled": True,
                "help": "",
                "identifier": "update-action",
                "label": "Check for Updates…",
                "role": "AXButton",
            },
            {
                "actions": ["AXPress"],
                "enabled": True,
                "help": "",
                "identifier": "update-route-picker",
                "label": "",
                "role": "AXPopUpButton",
            },
            {
                "actions": ["AXPress"],
                "enabled": True,
                "help": "",
                "identifier": "all-releases-link",
                "label": "View All Releases…",
                "role": "AXLink",
                "url": RELEASES_URL,
            },
        ]
        (output_directory / "accessibility-tree.json").write_text(
            json.dumps({"elements": records, "schema_version": 1}, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        screenshot = b"\x89PNG\r\n\x1a\n" + (b"x" * 64)
        (output_directory / "screenshot-light.png").write_bytes(screenshot)
        (output_directory / "screenshot-dark.png").write_bytes(screenshot)

    def app_running(self, app_path: Path | None = None) -> bool:
        if not self.running:
            return False
        return app_path is None or self.running_app_path == app_path

    def quit_app(self) -> None:
        self.quit_calls += 1
        self.running = False
        self.running_app_path = None


class Tier3CleanMachineTests(unittest.TestCase):
    def test_extract_ui_attachments_accepts_xcresult_generated_names(self) -> None:
        operations = MacOSOperations()
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            result_bundle = root / "InstalledUI-candidate.xcresult"
            result_bundle.mkdir()
            output_directory = root / "Evidence"
            output_directory.mkdir()

            def run(command: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
                del kwargs
                attachments_directory = Path(command[-1])
                attachments_directory.mkdir()
                records = []
                for index, expected_name in enumerate(
                    ("candidate-ui.json", "screenshot-light.png", "screenshot-dark.png")
                ):
                    expected_path = Path(expected_name)
                    exported_name = f"attachment-{index}{expected_path.suffix}"
                    (attachments_directory / exported_name).write_bytes(expected_name.encode())
                    records.append(
                        {
                            "exportedFileName": exported_name,
                            "suggestedHumanReadableName": (
                                f"{expected_path.stem}_0_00000000-0000-0000-0000-00000000000{index}"
                                f"{expected_path.suffix}"
                            ),
                        }
                    )
                (attachments_directory / "manifest.json").write_text(
                    json.dumps([{"attachments": records}]),
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch.object(MacOSOperations, "_run", side_effect=run):
                operations._extract_ui_attachments(
                    result_bundle=result_bundle,
                    output_directory=output_directory,
                    expected_names=("candidate-ui.json", "screenshot-light.png", "screenshot-dark.png"),
                )

            for expected_name in ("candidate-ui.json", "screenshot-light.png", "screenshot-dark.png"):
                self.assertEqual((output_directory / expected_name).read_bytes(), expected_name.encode())

    def test_collect_ui_evidence_forwards_exact_scheme_build_settings(self) -> None:
        operations = MacOSOperations()
        release_notes_url = "https://example.test/releases/v1?name=encoded%20space&route=stable"
        expected_tests = {
            "candidate": ("testCandidateMainWindowProfileAndSettings", "candidate-ui.json"),
            "updater": ("testPriorUpdaterControlsAndReleaseLinks", "updater-ui.json"),
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repo = root / "Repo With Spaces"
            (repo / "macos").mkdir(parents=True)
            app_path = root / "Applications" / APP_NAME
            synthetic_home = root / "Synthetic Home"
            for phase, (test_name, evidence_name) in expected_tests.items():
                with self.subTest(phase=phase):
                    output_directory = root / f"Evidence {phase}"
                    captured_commands: list[list[str]] = []

                    def run(
                        command: Any,
                        captured_commands: list[list[str]] = captured_commands,
                        **kwargs: Any,
                    ) -> subprocess.CompletedProcess[str]:
                        captured_commands.append(list(command))
                        self.assertNotIn("env", kwargs)
                        return subprocess.CompletedProcess(command, 0, "", "")

                    def extract_attachments(
                        *,
                        output_directory: Path = output_directory,
                        evidence_name: str = evidence_name,
                        phase: str = phase,
                        **kwargs: Any,
                    ) -> None:
                        del kwargs
                        output_directory.mkdir(parents=True, exist_ok=True)
                        if phase == "candidate":
                            (output_directory / evidence_name).write_text("{}\n", encoding="utf-8")
                            (output_directory / "accessibility-tree.json").write_text(
                                json.dumps(
                                    {
                                        "elements": [
                                            {
                                                "identifier": "all-releases-link",
                                                "url": RELEASES_URL,
                                            }
                                        ],
                                        "schema_version": 1,
                                    }
                                )
                                + "\n",
                                encoding="utf-8",
                            )
                        else:
                            (output_directory / evidence_name).write_text(
                                json.dumps(
                                    {
                                        "install_action": "Install and Relaunch",
                                        "release_notes_url": release_notes_url,
                                        "release_notes_url_observed": False,
                                        "schema_version": 1,
                                        "status": "passed",
                                    }
                                )
                                + "\n",
                                encoding="utf-8",
                            )
                            (output_directory / "updater-accessibility.json").write_text(
                                json.dumps(
                                    {
                                        "release_notes_url": release_notes_url,
                                        "release_notes_url_observed": True,
                                        "schema_version": 1,
                                    }
                                )
                                + "\n",
                                encoding="utf-8",
                            )

                    collector = Mock()
                    with (
                        patch.object(MacOSOperations, "_run", side_effect=run),
                        patch.object(MacOSOperations, "_start_accessibility_collector", return_value=collector),
                        patch.object(MacOSOperations, "_finish_accessibility_collector") as finish_collector,
                        patch.object(MacOSOperations, "_extract_ui_attachments", side_effect=extract_attachments),
                    ):
                        operations.collect_ui_evidence(
                            repo=repo,
                            phase=phase,
                            app_path=app_path,
                            synthetic_home=synthetic_home,
                            output_directory=output_directory,
                            release_notes_url=release_notes_url,
                        )

                    self.assertEqual(len(captured_commands), 1)
                    finish_collector.assert_called_once_with(collector)
                    if phase == "updater":
                        updater_evidence = json.loads((output_directory / evidence_name).read_text(encoding="utf-8"))
                        self.assertIs(updater_evidence["release_notes_url_observed"], True)
                    command = captured_commands[0]
                    expected_settings = {
                        "BD_TO_AVP_UI_APP_PATH": str(app_path),
                        "BD_TO_AVP_UI_BUNDLE_IDENTIFIER": BUNDLE_IDENTIFIER,
                        "BD_TO_AVP_UI_HOME": str(synthetic_home),
                        "BD_TO_AVP_UI_OUTPUT_DIRECTORY": str(output_directory),
                        "BD_TO_AVP_UI_PHASE": phase,
                        "BD_TO_AVP_UI_RELEASE_NOTES_URL": release_notes_url,
                        "BD_TO_AVP_UI_RELEASES_URL": RELEASES_URL,
                    }
                    observed_settings = {
                        argument.split("=", maxsplit=1)[0]: argument.split("=", maxsplit=1)[1]
                        for argument in command
                        if argument.split("=", maxsplit=1)[0] in expected_settings
                    }
                    self.assertEqual(observed_settings, expected_settings)
                    self.assertEqual(command[-1], "test")
                    self.assertIn(
                        f"-only-testing:BluRayToVisionProUITests/InstalledUIAcceptanceTests/{test_name}",
                        command,
                    )
                    for key, value in expected_settings.items():
                        argument = f"{key}={value}"
                        self.assertEqual(command.count(argument), 1)
                        self.assertLess(command.index(argument), len(command) - 1)

    def test_collect_ui_evidence_rejects_skipped_test_without_phase_output(self) -> None:
        operations = MacOSOperations()
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with (
                patch.object(
                    MacOSOperations,
                    "_run",
                    return_value=subprocess.CompletedProcess([], 0, "", ""),
                ),
                patch.object(
                    MacOSOperations,
                    "_start_accessibility_collector",
                    return_value=Mock(),
                ),
                patch.object(MacOSOperations, "_finish_accessibility_collector"),
                patch.object(MacOSOperations, "_extract_ui_attachments"),
            ):
                with self.assertRaisesRegex(
                    CleanMachineError,
                    "candidate test completed without required evidence: candidate-ui.json",
                ):
                    operations.collect_ui_evidence(
                        repo=root,
                        phase="candidate",
                        app_path=root / APP_NAME,
                        synthetic_home=root / "Home",
                        output_directory=root / "Evidence",
                        release_notes_url=RELEASES_URL,
                    )

    def test_stop_accessibility_collector_reaps_an_already_exited_process(self) -> None:
        collector = Mock()
        collector.poll.return_value = 2
        collector.communicate.return_value = ("collector stdout", "collector stderr")

        self.assertEqual(
            MacOSOperations._stop_accessibility_collector(collector),
            ("collector stdout", "collector stderr"),
        )
        collector.terminate.assert_not_called()
        collector.kill.assert_not_called()
        collector.communicate.assert_called_once_with()

    @staticmethod
    def make_app(root: Path, package_version: str, build_version: str, payload: str) -> Path:
        app = root / f"app-{package_version}" / APP_NAME
        contents = app / "Contents"
        contents.mkdir(parents=True)
        (contents / "Info.plist").write_bytes(
            plistlib.dumps(
                {
                    "BDToAVPDistributionChannel": "direct",
                    "CFBundleIdentifier": BUNDLE_IDENTIFIER,
                    "CFBundleShortVersionString": package_version,
                    "CFBundleVersion": build_version,
                    "SUFeedURL": "https://cbusillo.github.io/BD_to_AVP/appcast.xml",
                }
            )
        )
        executable = contents / "MacOS" / "app"
        executable.parent.mkdir(parents=True)
        executable.write_text(payload, encoding="utf-8")
        executable.chmod(0o755)
        return app

    @staticmethod
    def release_facts(
        *,
        source_sha: str,
        package_version: str,
        public_version: str,
        build_version: str,
        release_tag: str,
        release_id: int,
        dmg_path: Path,
        app_path: Path,
    ) -> dict[str, object]:
        return {
            "release_route": "prerelease",
            "source_sha": source_sha,
            "workflow_actor": "shiny-code-bot",
            "workflow_run_id": release_id + 1000,
            "workflow_run_attempt": 1,
            "package_version": package_version,
            "public_version": public_version,
            "build_version": build_version,
            "release_tag": release_tag,
            "release_name": release_tag,
            "release_id": release_id,
            "release_created_at": "2026-08-05T12:00:00Z",
            "prerelease": True,
            "make_latest": False,
            "signed_app_tree_sha256": app_tree_sha256(app_path),
            "artifacts": [
                {
                    "kind": "dmg",
                    "name": dmg_path.name,
                    "sha256": file_sha256(dmg_path),
                    "size_bytes": dmg_path.stat().st_size,
                    "asset_id": release_id + 1,
                },
                {
                    "kind": "checksum",
                    "name": "SHA256SUMS",
                    "sha256": "c" * 64,
                    "size_bytes": 100,
                    "asset_id": release_id + 2,
                },
                {
                    "kind": "appcast",
                    "name": "appcast.xml",
                    "sha256": "d" * 64,
                    "size_bytes": 500,
                    "asset_id": release_id + 3,
                },
            ],
        }

    @staticmethod
    def make_feed(root: Path, candidate_dmg: Path, *, minimum_system_version: str = "26.0") -> bytes:
        root.mkdir(parents=True, exist_ok=True)
        empty_feed = root / "empty.xml"
        empty_feed.write_text(
            f"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"
     xmlns:sparkle="{sparkle_appcast.SPARKLE_NAMESPACE}"
     xmlns:dc="{sparkle_appcast.DC_NAMESPACE}">
  <channel>
    <title>3D Blu-ray to Vision Pro Updates</title>
    <link>https://github.com/cbusillo/BD_to_AVP</link>
    <description>Updates.</description>
    <language>en</language>
  </channel>
</rss>
""",
            encoding="utf-8",
        )
        feed = root / "appcast.xml"
        sparkle_appcast.append_item(
            empty_feed,
            feed,
            sparkle_appcast.AppcastItem(
                build_version="160",
                short_version="0.3.0rc3",
                channel="rc",
                download_url=(
                    f"https://github.com/cbusillo/BD_to_AVP/releases/download/v0.3.0-rc.3/{candidate_dmg.name}"
                ),
                length=candidate_dmg.stat().st_size,
                signature=SIGNATURE,
                release_notes_markdown="RC 3 qualification fixture.",
                full_release_notes_url="https://github.com/cbusillo/BD_to_AVP/releases/tag/v0.3.0-rc.3",
                minimum_system_version=minimum_system_version,
                published_at=datetime(2026, 8, 5, 12, tzinfo=timezone.utc),
            ),
        )
        return feed.read_bytes()

    def fixture(self, root: Path) -> tuple[QualificationConfig, FakeOperations]:
        repo = root / "repo"
        repo.mkdir()
        policy_path = repo / "docs" / "qualification" / "release-qualification-policy-v1.json"
        policy_path.parent.mkdir(parents=True)
        shutil.copy2(REPO_ROOT / "docs" / "qualification" / "release-qualification-policy-v1.json", policy_path)

        prior_app = self.make_app(root, "0.3.0rc2", "159", "prior")
        candidate_app = self.make_app(root, "0.3.0rc3", "160", "candidate")
        prior_dmg = root / "3D-Blu-ray-to-Vision-Pro-0.3.0-rc.2.dmg"
        candidate_dmg = root / "3D-Blu-ray-to-Vision-Pro-0.3.0-rc.3.dmg"
        prior_dmg.write_bytes(b"prior-dmg")
        candidate_dmg.write_bytes(b"candidate-dmg")

        prior_receipt = repo / "docs" / "release-evidence" / "prior" / "release-receipt.json"
        candidate_receipt = repo / "docs" / "release-evidence" / "candidate" / "release-receipt.json"
        prior_receipt.parent.mkdir(parents=True)
        candidate_receipt.parent.mkdir(parents=True)
        write_receipt(
            build_release_receipt(
                self.release_facts(
                    source_sha=PRIOR_SHA,
                    package_version="0.3.0rc2",
                    public_version="0.3.0-rc.2",
                    build_version="159",
                    release_tag="v0.3.0-rc.2",
                    release_id=100,
                    dmg_path=prior_dmg,
                    app_path=prior_app,
                )
            ),
            prior_receipt,
        )
        write_receipt(
            build_release_receipt(
                self.release_facts(
                    source_sha=CANDIDATE_SHA,
                    package_version="0.3.0rc3",
                    public_version="0.3.0-rc.3",
                    build_version="160",
                    release_tag="v0.3.0-rc.3",
                    release_id=200,
                    dmg_path=candidate_dmg,
                    app_path=candidate_app,
                )
            ),
            candidate_receipt,
        )
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repo, check=True)

        config = QualificationConfig(
            repo=repo,
            candidate_receipt=candidate_receipt,
            candidate_dmg=candidate_dmg,
            prior_receipt=prior_receipt,
            prior_dmg=prior_dmg,
            qualification_root=Path.home() / f"tier3-test-{root.name}",
            route="rc",
            environment_class="restorable-location",
            output_receipt=root / "tier3-receipt.json",
            ui_output_receipt=root / "tier3-ui-receipt.json",
            evidence_directory=root / "evidence",
        )
        operations = FakeOperations(
            {prior_dmg.resolve(): prior_app, candidate_dmg.resolve(): candidate_app},
            self.make_feed(root, candidate_dmg),
            candidate_dmg,
        )
        return config, operations

    def test_preflight_binds_checked_artifacts_environment_and_live_feed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            config, operations = self.fixture(Path(temporary_directory))
            report = preflight_report(config, operations)

        self.assertEqual(report["preflight"], "passed")
        self.assertEqual(report["environment"]["environment_class"], "restorable-location")
        self.assertEqual(report["candidate"]["build_version"], "160")
        self.assertEqual(report["prior"]["build_version"], "159")
        self.assertEqual(report["feed"]["route"], "rc")
        self.assertNotIn("hostname", json.dumps(report).lower())

    def test_run_emits_valid_receipt_and_disposes_owned_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config, operations = self.fixture(root)
            receipts = run_qualification(config, operations)

            self.assertFalse(config.qualification_root.exists())
            self.assertTrue(config.output_receipt.exists())
            self.assertTrue(config.ui_output_receipt.exists())
            self.assertEqual(
                {path.name for path in config.evidence_directory.iterdir()},
                {
                    "accessibility-tree.json",
                    "cleanup.json",
                    "install-log.json",
                    "package-smoke.json",
                    "profile-snapshot.json",
                    "screenshot-dark.png",
                    "screenshot-light.png",
                    "sparkle-update.json",
                    "ui-result.json",
                },
            )
            for receipt in receipts.values():
                self.assertEqual(receipt["result"]["status"], "passed")
                self.assertEqual(receipt["cleanup"]["status"], "disposed")
                self.assertEqual(receipt["release_identity"]["source_sha"], CANDIDATE_SHA)
            update_evidence = json.loads(
                (config.evidence_directory / "sparkle-update.json").read_text(encoding="utf-8")
            )
            self.assertEqual(update_evidence["attempt"], 1)
            self.assertEqual(update_evidence["outcome"], "install-and-relaunch")
            self.assertEqual(update_evidence["window_match"], "button-identifier")
            self.assertIn("ready-install-relaunch", update_evidence["states_observed"])
            self.assertRegex(update_evidence["intent_checkpoint_sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(update_evidence["journal_sha256"], r"^[0-9a-f]{64}$")
            self.assertTrue(operations.intent_seen_before_press)
            self.assertFalse(operations.app_running())

    def test_run_accepts_supported_sparkle_install_actions(self) -> None:
        for install_action in SPARKLE_INSTALL_ACTIONS:
            with self.subTest(install_action=install_action), tempfile.TemporaryDirectory() as temporary_directory:
                root = Path(temporary_directory)
                config, operations = self.fixture(root)
                operations.install_action = install_action

                receipts = run_qualification(config, operations)

                self.assertEqual(
                    receipts["installed-ui-accessibility"]["result"]["status"],
                    "passed",
                )

    def test_run_rejects_unknown_sparkle_install_action_with_observed_value(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config, operations = self.fixture(root)
            operations.install_action = "Learn More…"

            with self.assertRaisesRegex(
                CleanMachineError,
                "unsupported install action: 'Learn More…'",
            ):
                run_qualification(config, operations)

            self.assertFalse(config.qualification_root.exists())
            self.assertFalse(config.evidence_directory.exists())

    def test_run_cleans_workspace_after_update_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config, operations = self.fixture(root)
            operations.update_failure = True

            with self.assertRaisesRegex(CleanMachineError, "simulated Sparkle failure"):
                run_qualification(config, operations)

            self.assertFalse(config.qualification_root.exists())
            self.assertFalse(operations.app_running())
            self.assertFalse(config.output_receipt.exists())
            self.assertFalse(config.ui_output_receipt.exists())
            self.assertFalse(config.evidence_directory.exists())
            self.assertEqual(operations.update_attempts, 1)

    def test_run_install_on_quit_installs_then_launches_exact_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config, operations = self.fixture(root)
            operations.install_action = "Install on Quit"

            run_qualification(config, operations)

            update_evidence = json.loads(
                (config.evidence_directory / "sparkle-update.json").read_text(encoding="utf-8")
            )
            self.assertEqual(update_evidence["outcome"], "install-on-quit")
            self.assertIn("ready-install-on-quit", update_evidence["states_observed"])
            self.assertEqual(operations.launch_calls, 2)
            self.assertTrue(operations.intent_seen_before_press)

    def test_run_records_staged_install_before_final_relaunch_action(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config, operations = self.fixture(root)
            operations.install_action = "Install Update"

            run_qualification(config, operations)

            update_evidence = json.loads(
                (config.evidence_directory / "sparkle-update.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                [observation.state for observation in operations.pressed_actions],
                [SparkleUpdateState.STAGED_INSTALL, SparkleUpdateState.READY_INSTALL_RELAUNCH],
            )
            self.assertEqual(update_evidence["outcome"], "install-and-relaunch")
            self.assertEqual(
                update_evidence["states_observed"],
                ["staged-install", "installing", "ready-install-relaunch"],
            )

    def test_run_does_not_press_a_repeated_staged_action(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config, operations = self.fixture(root)
            operations.install_action = "Install Update"
            operations.staged_repeats_after_press = 2

            run_qualification(config, operations)

            self.assertEqual(
                [observation.state for observation in operations.pressed_actions],
                [SparkleUpdateState.STAGED_INSTALL, SparkleUpdateState.READY_INSTALL_RELAUNCH],
            )

    def test_run_retries_one_clean_attempt_for_pre_press_environmental_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config, operations = self.fixture(root)
            operations.retryable_failures = 1

            run_qualification(config, operations)

            update_evidence = json.loads(
                (config.evidence_directory / "sparkle-update.json").read_text(encoding="utf-8")
            )
            self.assertEqual(operations.update_attempts, 2)
            self.assertEqual(update_evidence["attempt"], 2)
            self.assertEqual(update_evidence["retry_reason_code"], "application-process-timeout")

    def test_run_does_not_retry_after_an_action_was_pressed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config, operations = self.fixture(root)
            operations.install_action = "Install Update"
            operations.post_press_failure = True

            with self.assertRaisesRegex(CleanMachineError, "simulated post-press failure"):
                run_qualification(config, operations)

            self.assertEqual(operations.update_attempts, 1)
            self.assertEqual(len(operations.pressed_actions), 1)
            self.assertFalse(config.evidence_directory.exists())

    def test_run_fails_closed_when_updater_changes_after_intent_recording(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config, operations = self.fixture(root)
            operations.press_state_changed = True

            with self.assertRaisesRegex(CleanMachineError, "changed after intent"):
                run_qualification(config, operations)

            self.assertEqual(operations.update_attempts, 1)
            self.assertTrue(operations.intent_seen_before_press)
            self.assertFalse(operations.pressed_actions)
            self.assertFalse(config.evidence_directory.exists())

    def test_run_classifies_terminal_failure_without_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config, operations = self.fixture(root)
            operations.terminal_failure = True

            with self.assertRaisesRegex(CleanMachineError, "terminal updater failure"):
                run_qualification(config, operations)

            self.assertEqual(operations.update_attempts, 1)
            self.assertFalse(operations.pressed_actions)

    def test_run_classifies_cancellation_without_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config, operations = self.fixture(root)
            operations.cancelled = True

            with self.assertRaisesRegex(CleanMachineError, "was cancelled"):
                run_qualification(config, operations)

            self.assertEqual(operations.update_attempts, 1)
            self.assertFalse(operations.pressed_actions)

    def test_run_bounds_non_actionable_state_transitions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config, operations = self.fixture(root)
            operations.oscillate_non_actionable = True

            with patch("scripts.tier3_clean_machine.UPDATE_POLL_SECONDS", 0):
                with self.assertRaisesRegex(CleanMachineError, "bounded transition count"):
                    run_qualification(config, operations)

            self.assertEqual(operations.update_attempts, 1)
            self.assertFalse(operations.pressed_actions)
            self.assertFalse(config.evidence_directory.exists())

    def test_run_revalidates_prior_identity_immediately_before_updater_interaction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config, operations = self.fixture(root)
            operations.tamper_prior_before_update = True

            with self.assertRaisesRegex(CleanMachineError, "exact signed release receipt"):
                run_qualification(config, operations)

            self.assertEqual(operations.update_attempts, 0)
            self.assertFalse(operations.pressed_actions)

    def test_run_rejects_candidate_process_from_a_different_app_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config, operations = self.fixture(root)
            operations.wrong_running_path = True

            with patch("scripts.tier3_clean_machine.UPDATE_TIMEOUT_SECONDS", 0.01):
                with self.assertRaisesRegex(CleanMachineError, "did not install and relaunch"):
                    run_qualification(config, operations)

            self.assertEqual(operations.update_attempts, 1)
            self.assertFalse(config.evidence_directory.exists())

    def test_preflight_rejects_wrong_macos_major(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            config, operations = self.fixture(Path(temporary_directory))
            operations.macos_version = "27.0"

            with self.assertRaisesRegex(CleanMachineError, "requires macOS"):
                preflight_report(config, operations)

    def test_preflight_rejects_candidate_not_newer_than_prior(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            config, operations = self.fixture(Path(temporary_directory))
            report = preflight_report(config, operations)
            self.assertEqual(report["candidate"]["build_version"], "160")
            candidate = json.loads(config.candidate_receipt.read_text(encoding="utf-8"))
            prior = json.loads(config.prior_receipt.read_text(encoding="utf-8"))
            candidate_release = self.release_artifact(config.candidate_receipt, config.candidate_dmg, candidate)
            prior_release = self.release_artifact(config.prior_receipt, config.prior_dmg, prior)
            candidate_release = replace(candidate_release, build_version=prior_release.build_version)

            with self.assertRaisesRegex(CleanMachineError, "must be newer"):
                validate_release_order(prior_release, candidate_release)

    def test_preflight_rejects_root_outside_home(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config, operations = self.fixture(root)
            config = replace(config, qualification_root=root / "outside-home")

            with self.assertRaisesRegex(CleanMachineError, "inside the current home"):
                preflight_report(config, operations)

    def test_preflight_rejects_candidate_minimum_system_above_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config, operations = self.fixture(root)
            operations.feed_bytes = self.make_feed(
                root / "new-feed", config.candidate_dmg, minimum_system_version="26.1"
            )

            with self.assertRaisesRegex(CleanMachineError, "older than the candidate"):
                preflight_report(config, operations)

    def test_preflight_rejects_preference_sentinel_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config, operations = self.fixture(root)
            operations.sentinel_failure = True

            with self.assertRaisesRegex(CleanMachineError, "Preference sentinel"):
                run_qualification(config, operations)

            self.assertFalse(config.qualification_root.exists())
            self.assertFalse(config.evidence_directory.exists())

    def test_marker_tampering_retains_root_for_investigation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config, operations = self.fixture(root)
            operations.tamper_marker = True

            with self.assertRaisesRegex(CleanMachineError, "ownership marker changed"):
                run_qualification(config, operations)

            self.assertTrue(config.qualification_root.exists())
            self.assertFalse(config.evidence_directory.exists())
            shutil.rmtree(config.qualification_root)

    def test_feed_rejects_route_that_excludes_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config, operations = self.fixture(root)
            report = preflight_report(config, operations)
            self.assertEqual(report["feed"]["route"], "rc")
            candidate = json.loads(config.candidate_receipt.read_text(encoding="utf-8"))
            release_artifact = self.release_artifact(config.candidate_receipt, config.candidate_dmg, candidate)

            with self.assertRaisesRegex(CleanMachineError, "not eligible"):
                parse_feed_candidate(operations.feed_bytes, release_artifact, "stable")

    @staticmethod
    def release_artifact(receipt_path: Path, dmg_path: Path, receipt: dict[str, Any]) -> ReleaseArtifact:
        release = cast(dict[str, Any], receipt["release"])
        versions = cast(dict[str, Any], receipt["versions"])
        artifacts = cast(list[dict[str, Any]], receipt["artifacts"])
        artifact = next(item for item in artifacts if item["kind"] == "dmg")
        return ReleaseArtifact(
            receipt=receipt,
            receipt_path=receipt_path,
            receipt_reference="docs/release-evidence/test/release-receipt.json",
            receipt_file_sha256=file_sha256(receipt_path),
            dmg_path=dmg_path,
            dmg_name=artifact["name"],
            dmg_sha256=artifact["sha256"],
            dmg_size=artifact["size_bytes"],
            source_sha=receipt["source_sha"],
            release_tag=release["tag"],
            package_version=versions["package"],
            public_version=versions["public"],
            build_version=versions["build"],
            signed_app_tree_sha256=receipt["signed_app_tree_sha256"],
        )

    @unittest.skipUnless(platform.system() == "Darwin", "AppleScript compilation requires macOS")
    def test_updater_applescript_compiles(self) -> None:
        operations = MacOSOperations()
        scripts = {
            "open": operations._updater_open_script(),
            "observe": operations._updater_observe_script(),
            "press": operations._updater_press_script(),
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            for name, script in scripts.items():
                with self.subTest(name=name):
                    source = root / f"{name}.applescript"
                    output = root / f"{name}.scpt"
                    source.write_text(script, encoding="utf-8")
                    result = subprocess.run(
                        ["osacompile", "-o", str(output), str(source)],
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_updater_waits_for_identified_enabled_install_button(self) -> None:
        observe_script = MacOSOperations()._updater_observe_script()
        press_script = MacOSOperations()._updater_press_script()

        self.assertIn('attribute "AXIdentifier"', observe_script)
        self.assertIn('"SUUpdateAlert"', observe_script)
        self.assertIn('"SPUUserUpdateChoiceInstall"', observe_script)
        self.assertIn("enabled of selectedButton", observe_script)
        self.assertIn('selectedTitle is "Install Update"', observe_script)
        self.assertIn('selectedTitle is "Install on Quit"', observe_script)
        self.assertIn('"ready-install-relaunch"', observe_script)
        self.assertIn('perform action "AXPress" of selectedButton', press_script)
        self.assertIn("actualWindowMatch is not expectedWindowMatch", press_script)

    @unittest.skipUnless(platform.system() == "Darwin", "DMG mount integration requires macOS")
    def test_synthetic_dmg_mount_and_detach(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source"
            source.mkdir()
            (source / "fixture.txt").write_text("fixture\n", encoding="utf-8")
            dmg = root / "fixture.dmg"
            create = subprocess.run(
                ["hdiutil", "create", "-quiet", "-ov", "-format", "UDZO", "-srcfolder", str(source), str(dmg)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(create.returncode, 0, create.stderr)
            mount_point = root / "mount"
            operations = MacOSOperations()
            operations._mount_dmg(dmg, mount_point)
            self.assertEqual((mount_point / "fixture.txt").read_text(encoding="utf-8"), "fixture\n")
            operations._detach_mounts([mount_point])
            self.assertFalse(mount_point.exists())


if __name__ == "__main__":
    unittest.main()
