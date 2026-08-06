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
    UpdateInteraction,
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
        sentinel_failure: bool = False,
        tamper_marker: bool = False,
    ) -> None:
        self.app_sources = app_sources
        self.feed_bytes = feed_bytes
        self.macos_version = macos_version
        self.update_failure = update_failure
        self.sentinel_failure = sentinel_failure
        self.tamper_marker = tamper_marker
        self.preferences: dict[str, str] = {}
        self.running = False
        self.candidate_source = app_sources[candidate_dmg.resolve()]

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

    def perform_update(self, app_path: Path, synthetic_home: Path) -> UpdateInteraction:
        if self.update_failure:
            raise CleanMachineError("simulated Sparkle failure")
        if self.tamper_marker:
            marker_path = synthetic_home.parent / ".bd-to-avp-tier3-owned.json"
            marker_path.write_text('{"owner":"changed"}\n', encoding="utf-8")
        shutil.rmtree(app_path)
        shutil.copytree(self.candidate_source, app_path, symlinks=True)
        self.running = True
        return UpdateInteraction(clicked_button="Install and Relaunch")

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
        del repo, app_path, synthetic_home
        output_directory.mkdir(parents=True, exist_ok=True)
        if phase == "updater":
            (output_directory / "updater-ui.json").write_text(
                json.dumps(
                    {
                        "install_action": "Install and Relaunch",
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
                    "profile_document_version": 5,
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
                "actions": [],
                "enabled": True,
                "help": "",
                "identifier": "main-status",
                "label": "Status: Ready",
                "role": "AXStaticText",
            },
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
                "label": "Update route",
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

    def app_running(self) -> bool:
        return self.running

    def quit_app(self) -> None:
        self.running = False


class Tier3CleanMachineTests(unittest.TestCase):
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
            self.assertFalse(operations.app_running())

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
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "updater.applescript"
            output = Path(temporary_directory) / "updater.scpt"
            source.write_text(MacOSOperations()._updater_script(), encoding="utf-8")
            result = subprocess.run(
                ["osacompile", "-o", str(output), str(source)],
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 0, result.stderr)

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
