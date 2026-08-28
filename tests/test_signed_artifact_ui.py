import json
import tempfile
import unittest

from pathlib import Path

from scripts.artifact_identity import app_tree_sha256
from scripts.release_receipt import build_receipt, file_sha256, write_receipt
from scripts.signed_artifact_ui import SignedArtifactUIConfig, SignedArtifactUIError, run
from scripts.tier3_clean_machine import APP_NAME, RELEASES_URL, CleanMachineError


CANDIDATE_SHA = "b" * 40
DMG_SHA256 = "c" * 64
CHECKSUM_SHA256 = "d" * 64
APPCAST_SHA256 = "e" * 64


class FakeOperations:
    def __init__(self, app_source: Path) -> None:
        self.app_source = app_source
        self.profiles_after = 1
        self.profiles_before = 0
        self.running = False
        self.install_mount_point: Path | None = None

    def install_app(self, dmg_path: Path, destination: Path, mount_point: Path) -> None:
        del dmg_path
        self.install_mount_point = mount_point
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise AssertionError("test destination should start empty")
        import shutil

        shutil.copytree(self.app_source, destination, symlinks=True)

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
        del repo, app_path, synthetic_home, release_notes_url
        self.running = True
        if phase != "candidate":
            raise AssertionError("signed artifact UI runner must use the candidate lane")
        output_directory.mkdir(parents=True)
        (output_directory / "candidate-ui.json").write_text(
            json.dumps(
                {
                    "main_window_ready": True,
                    "profile_document_version": 6,
                    "profile_save_accessible": True,
                    "profile_save_succeeded": True,
                    "profiles_after": self.profiles_after,
                    "profiles_before": self.profiles_before,
                    "release_page_url": RELEASES_URL,
                    "release_page_url_observed": True,
                    "schema_version": 1,
                    "status": "passed",
                    "updater_controls_accessible": True,
                }
            ),
            encoding="utf-8",
        )
        (output_directory / "accessibility-tree.json").write_text(
            json.dumps(
                {
                    "elements": [
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
                            "help": "Checks for updates",
                            "identifier": "update-action",
                            "label": "Check for Updates",
                            "role": "AXButton",
                        },
                        {
                            "actions": ["AXPress"],
                            "enabled": True,
                            "help": "Update route",
                            "identifier": "update-route-picker",
                            "label": "",
                            "role": "AXPopUpButton",
                        },
                        {
                            "actions": ["AXPress"],
                            "enabled": True,
                            "help": "Opens all releases",
                            "identifier": "all-releases-link",
                            "label": "All Releases",
                            "role": "AXLink",
                            "url": RELEASES_URL,
                        },
                    ],
                    "schema_version": 1,
                }
            ),
            encoding="utf-8",
        )
        png = b"\x89PNG\r\n\x1a\n" + (b"0" * 64)
        (output_directory / "screenshot-light.png").write_bytes(png)
        (output_directory / "screenshot-dark.png").write_bytes(png)

    def quit_app(self) -> None:
        self.running = False

    def app_running(self, app_path: Path | None = None) -> bool:
        del app_path
        return self.running


def make_app(root: Path) -> Path:
    app = root / APP_NAME
    contents = app / "Contents"
    contents.mkdir(parents=True)
    (contents / "Info.plist").write_text("plist", encoding="utf-8")
    (contents / "MacOS").mkdir()
    (contents / "MacOS" / "BD_to_AVP").write_text("binary", encoding="utf-8")
    return app


class SignedArtifactUITests(unittest.TestCase):
    @staticmethod
    def _make_release_receipt(root: Path, app: Path, dmg: Path) -> tuple[Path, str]:
        facts = {
            "release_route": "prerelease",
            "source_sha": CANDIDATE_SHA,
            "workflow_actor": "shiny-code-bot",
            "workflow_run_id": 12345,
            "workflow_run_attempt": 2,
            "package_version": "0.3.0rc3",
            "public_version": "0.3.0-rc.3",
            "build_version": "160",
            "release_tag": "v0.3.0-rc.3",
            "release_name": "v0.3.0-rc.3",
            "release_id": 67890,
            "release_created_at": "2026-08-05T12:00:00Z",
            "prerelease": True,
            "make_latest": False,
            "signed_app_tree_sha256": app_tree_sha256(app),
            "artifacts": [
                {
                    "kind": "dmg",
                    "name": dmg.name,
                    "sha256": file_sha256(dmg),
                    "size_bytes": dmg.stat().st_size,
                    "asset_id": 1,
                },
                {
                    "kind": "checksum",
                    "name": "SHA256SUMS",
                    "sha256": CHECKSUM_SHA256,
                    "size_bytes": 100,
                    "asset_id": 2,
                },
                {
                    "kind": "appcast",
                    "name": "appcast.xml",
                    "sha256": APPCAST_SHA256,
                    "size_bytes": 500,
                    "asset_id": 3,
                },
            ],
        }
        release_receipt = root / "release-receipt.json"
        write_receipt(build_receipt(facts), release_receipt)
        return release_receipt, file_sha256(release_receipt)

    def test_runner_rejects_preexisting_production_app_process(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            operations = FakeOperations(root)
            operations.running = True
            config = SignedArtifactUIConfig(
                repo=Path.cwd(),
                policy=Path.cwd() / "docs/qualification/release-qualification-policy-v1.json",
                release_receipt=root / "release-receipt.json",
                dmg=root / "candidate.dmg",
                qualification_root=root / "workspace",
                evidence_directory=root / "evidence",
                output_receipt=root / "signed-artifact-ui-receipt.json",
                case_id="profile-save-action-accessibility",
                release_notes_url=RELEASES_URL,
                workflow_run_id=12345,
                workflow_run_attempt=2,
                release_receipt_asset_id=4,
                release_receipt_file_sha256="0" * 64,
            )

            with self.assertRaisesRegex(SignedArtifactUIError, "must not be running"):
                run(config, operations)

            self.assertFalse(config.qualification_root.exists())

    def test_runner_preserves_bounded_failure_diagnostics(self) -> None:
        class FailingOperations(FakeOperations):
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
                del repo, phase, app_path, synthetic_home, release_notes_url
                output_directory.mkdir(parents=True)
                (output_directory / "partial.json").write_text("{}\n", encoding="utf-8")
                result_bundle = output_directory.parent / "InstalledUI-candidate.xcresult"
                result_bundle.mkdir()
                (result_bundle / "Info.plist").write_text("partial", encoding="utf-8")
                raise CleanMachineError("simulated UI failure")

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            app = make_app(root / "source")
            dmg = root / "3D-Blu-ray-to-Vision-Pro-0.3.0-rc.3.dmg"
            dmg.write_bytes(b"candidate-dmg")
            release_receipt, release_receipt_sha256 = self._make_release_receipt(root, app, dmg)
            diagnostics = root / "diagnostics"
            config = SignedArtifactUIConfig(
                repo=Path.cwd(),
                policy=Path.cwd() / "docs/qualification/release-qualification-policy-v1.json",
                release_receipt=release_receipt,
                dmg=dmg,
                qualification_root=root / "workspace",
                evidence_directory=root / "evidence",
                output_receipt=root / "signed-artifact-ui-receipt.json",
                case_id="profile-save-action-accessibility",
                release_notes_url=RELEASES_URL,
                workflow_run_id=12345,
                workflow_run_attempt=2,
                release_receipt_asset_id=4,
                release_receipt_file_sha256=release_receipt_sha256,
                failure_diagnostics_directory=diagnostics,
            )

            with self.assertRaisesRegex(CleanMachineError, "simulated UI failure"):
                run(config, FailingOperations(app))

            self.assertFalse(config.qualification_root.exists())
            self.assertTrue((diagnostics / "failure.txt").is_file())
            self.assertTrue((diagnostics / "InstalledUI-candidate.xcresult" / "Info.plist").is_file())
            self.assertTrue((diagnostics / "InstalledUIEvidence" / "partial.json").is_file())

    def test_runner_installs_exact_dmg_and_writes_bound_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            app = make_app(root / "source")
            dmg = root / "3D-Blu-ray-to-Vision-Pro-0.3.0-rc.3.dmg"
            dmg.write_bytes(b"candidate-dmg")
            release_receipt, release_receipt_sha256 = self._make_release_receipt(root, app, dmg)
            output_receipt = root / "signed-artifact-ui-receipt.json"
            config = SignedArtifactUIConfig(
                repo=Path.cwd(),
                policy=Path.cwd() / "docs/qualification/release-qualification-policy-v1.json",
                release_receipt=release_receipt,
                dmg=dmg,
                qualification_root=root / "workspace",
                evidence_directory=root / "evidence",
                output_receipt=output_receipt,
                case_id="profile-save-action-accessibility",
                release_notes_url=RELEASES_URL,
                workflow_run_id=12345,
                workflow_run_attempt=2,
                release_receipt_asset_id=4,
                release_receipt_file_sha256=release_receipt_sha256,
            )

            operations = FakeOperations(app)
            receipt = run(config, operations)

            self.assertTrue(output_receipt.exists())
            self.assertFalse(config.qualification_root.exists())
            self.assertEqual(operations.install_mount_point, config.qualification_root / "Mount")
            self.assertEqual(receipt["case_id"], "profile-save-action-accessibility")
            self.assertEqual(receipt["release_receipt"]["asset_id"], 4)
            self.assertEqual(receipt["dmg"]["name"], dmg.name)

    def test_runner_rejects_profile_save_without_exactly_one_new_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            app = make_app(root / "source")
            dmg = root / "3D-Blu-ray-to-Vision-Pro-0.3.0-rc.3.dmg"
            dmg.write_bytes(b"candidate-dmg")
            release_receipt, release_receipt_sha256 = self._make_release_receipt(root, app, dmg)
            config = SignedArtifactUIConfig(
                repo=Path.cwd(),
                policy=Path.cwd() / "docs/qualification/release-qualification-policy-v1.json",
                release_receipt=release_receipt,
                dmg=dmg,
                qualification_root=root / "workspace",
                evidence_directory=root / "evidence",
                output_receipt=root / "signed-artifact-ui-receipt.json",
                case_id="profile-save-action-accessibility",
                release_notes_url=RELEASES_URL,
                workflow_run_id=12345,
                workflow_run_attempt=2,
                release_receipt_asset_id=4,
                release_receipt_file_sha256=release_receipt_sha256,
            )
            operations = FakeOperations(app)
            operations.profiles_after = 2

            with self.assertRaisesRegex(CleanMachineError, "did not add exactly one profile"):
                run(config, operations)

            self.assertFalse(config.output_receipt.exists())
            self.assertFalse(config.evidence_directory.exists())

    def test_runner_removes_partial_outputs_on_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            release_receipt = root / "release-receipt.json"
            release_receipt.write_text("{}", encoding="utf-8")
            output_receipt = root / "signed-artifact-ui-receipt.json"
            evidence = root / "evidence"
            config = SignedArtifactUIConfig(
                repo=Path.cwd(),
                policy=Path.cwd() / "docs/qualification/release-qualification-policy-v1.json",
                release_receipt=release_receipt,
                dmg=root / "missing.dmg",
                qualification_root=root / "workspace",
                evidence_directory=evidence,
                output_receipt=output_receipt,
                case_id="profile-save-action-accessibility",
                release_notes_url=RELEASES_URL,
                workflow_run_id=12345,
                workflow_run_attempt=2,
                release_receipt_asset_id=4,
                release_receipt_file_sha256="0" * 64,
            )

            with self.assertRaises(SignedArtifactUIError):
                run(config, FakeOperations(root))

            self.assertFalse(output_receipt.exists())
            self.assertFalse(evidence.exists())


if __name__ == "__main__":
    unittest.main()
