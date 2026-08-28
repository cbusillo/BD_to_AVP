import hashlib
import json
import shutil
import tempfile
import unittest

from dataclasses import replace
from itertools import combinations
from pathlib import Path
from typing import Any
from unittest.mock import patch

from scripts.artifact_identity import app_tree_sha256
from scripts.installed_ui_qualification import (
    InstalledUIOperations,
    InstalledUIQualificationConfig,
    InstalledUIQualificationError,
    run,
    validate_installed_ui_operations_contract,
    validate_installed_ui_output_paths,
)
from scripts.tier3_clean_machine import APP_NAME, RELEASES_URL, CleanMachineError, MacOSOperations


def write_candidate_evidence(output_directory: Path, *, profiles_before: int) -> None:
    output_directory.mkdir(parents=True)
    (output_directory / "candidate-ui.json").write_text(
        json.dumps(
            {
                "main_window_ready": True,
                "profile_document_version": 6,
                "profile_save_accessible": True,
                "profile_save_succeeded": True,
                "profiles_after": profiles_before + 1,
                "profiles_before": profiles_before,
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
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (output_directory / "screenshot-light.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"light" * 12)
    (output_directory / "screenshot-dark.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"dark" * 14)


class RecordingOperations:
    def __init__(
        self,
        app_source: Path,
        *,
        profiles_before: int = 0,
        install_error: Exception | None = None,
        collect_error: BaseException | None = None,
        failing_quit_call: int | None = None,
    ) -> None:
        self.app_source = app_source
        self.profiles_before = profiles_before
        self.install_error = install_error
        self.collect_error = collect_error
        self.failing_quit_call = failing_quit_call
        self.install_arguments: tuple[Path, Path, Path] | None = None
        self.collect_arguments: dict[str, Any] | None = None
        self.quit_calls = 0
        self.running = False

    def install_app(self, dmg_path: Path, destination: Path, mount_point: Path) -> None:
        self.install_arguments = (dmg_path, destination, mount_point)
        if self.install_error is not None:
            raise self.install_error
        destination.parent.mkdir(parents=True, exist_ok=True)
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
        self.collect_arguments = {
            "app_path": app_path,
            "output_directory": output_directory,
            "phase": phase,
            "release_notes_url": release_notes_url,
            "repo": repo,
            "synthetic_home": synthetic_home,
        }
        self.running = True
        write_candidate_evidence(output_directory, profiles_before=self.profiles_before)
        if self.collect_error is not None:
            raise self.collect_error

    def app_running(self, app_path: Path | None = None) -> bool:
        del app_path
        return self.running

    def quit_app(self) -> None:
        self.quit_calls += 1
        if self.quit_calls == self.failing_quit_call:
            raise RuntimeError("cleanup quit failure")
        self.running = False


class FailingOperations:
    def __init__(self) -> None:
        self.install_attempted = False
        self.collect_attempted = False
        self.running = False

    def install_app(self, dmg_path: Path, destination: Path, mount_point: Path) -> None:
        del dmg_path, destination, mount_point
        self.install_attempted = True
        raise CleanMachineError("primary UI failure")

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
        del repo, phase, app_path, synthetic_home, output_directory, release_notes_url
        self.collect_attempted = True
        raise AssertionError("evidence collection must not run after installation fails")

    def quit_app(self) -> None:
        self.running = False
        raise RuntimeError("cleanup quit failure")

    def app_running(self, app_path: Path | None = None) -> bool:
        del app_path
        return self.running


class StaleInstallOperations:
    def __init__(self, app_source: Path) -> None:
        self.app_source = app_source
        self.running = False
        self.install_arguments: tuple[Path, Path] | None = None

    def install_app(self, dmg_path: Path, destination: Path) -> None:
        self.install_arguments = (dmg_path, destination)

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
        self.collect_arguments = (
            repo,
            phase,
            app_path,
            synthetic_home,
            output_directory,
            release_notes_url,
        )

    def app_running(self, app_path: Path | None = None) -> bool:
        self.running_app_path = app_path
        return self.running

    def quit_app(self) -> None:
        self.running = False


class InstalledUIQualificationTests(unittest.TestCase):
    @staticmethod
    def config(root: Path, *, expected_app_tree_sha256: str = "a" * 64) -> InstalledUIQualificationConfig:
        return InstalledUIQualificationConfig(
            repo=root,
            dmg=root / "candidate.dmg",
            qualification_root=root / "qualification",
            evidence_directory=root / "evidence",
            release_notes_url=RELEASES_URL,
            expected_app_tree_sha256=expected_app_tree_sha256,
            owner="test-installed-ui",
            failure_diagnostics_directory=root / "diagnostics",
        )

    @staticmethod
    def make_app(root: Path) -> Path:
        app = root / APP_NAME
        executable = app / "Contents" / "MacOS" / "BD_to_AVP"
        executable.parent.mkdir(parents=True)
        executable.write_text("candidate\n", encoding="utf-8")
        resources = app / "Contents" / "Resources"
        resources.mkdir()
        (resources / "worker").symlink_to("../MacOS/BD_to_AVP")
        return app

    def test_production_operations_satisfy_checked_contract(self) -> None:
        operations = validate_installed_ui_operations_contract(MacOSOperations())

        self.assertIsInstance(operations, InstalledUIOperations)

    def test_rejects_stale_operations_signature(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            operations = StaleInstallOperations(self.make_app(Path(temporary_directory)))

            with self.assertRaisesRegex(InstalledUIQualificationError, "install_app.*production contract"):
                validate_installed_ui_operations_contract(operations)

    def test_success_installs_exact_app_normalizes_evidence_terminates_and_cleans_workspace(self) -> None:
        for profiles_before in (0, 2):
            with self.subTest(profiles_before=profiles_before), tempfile.TemporaryDirectory() as temporary_directory:
                root = Path(temporary_directory)
                app_source = self.make_app(root / "source")
                config = self.config(root, expected_app_tree_sha256=app_tree_sha256(app_source))
                config.dmg.write_bytes(b"candidate-dmg")
                operations = RecordingOperations(app_source, profiles_before=profiles_before)

                evidence = run(config, operations)

                expected_app = config.qualification_root / "Applications" / APP_NAME
                self.assertEqual(
                    operations.install_arguments,
                    (config.dmg, expected_app, config.qualification_root / "Mount"),
                )
                self.assertEqual(
                    operations.collect_arguments,
                    {
                        "app_path": expected_app,
                        "output_directory": config.qualification_root / "InstalledUIEvidence",
                        "phase": "candidate",
                        "release_notes_url": RELEASES_URL,
                        "repo": root.resolve(),
                        "synthetic_home": config.qualification_root / "Home",
                    },
                )
                self.assertEqual(
                    evidence,
                    {
                        "accessibility-tree": hashlib.sha256(
                            (config.evidence_directory / "accessibility-tree.json").read_bytes()
                        ).hexdigest(),
                        "screenshot-dark": hashlib.sha256(
                            (config.evidence_directory / "screenshot-dark.png").read_bytes()
                        ).hexdigest(),
                        "screenshot-light": hashlib.sha256(
                            (config.evidence_directory / "screenshot-light.png").read_bytes()
                        ).hexdigest(),
                        "ui-result": hashlib.sha256(
                            (config.evidence_directory / "ui-result.json").read_bytes()
                        ).hexdigest(),
                    },
                )
                ui_result = json.loads((config.evidence_directory / "ui-result.json").read_text(encoding="utf-8"))
                self.assertEqual(ui_result["profiles_before"], profiles_before)
                self.assertEqual(ui_result["profiles_after"], profiles_before + 1)
                self.assertEqual(operations.quit_calls, 2)
                self.assertFalse(operations.running)
                self.assertFalse(config.qualification_root.exists())
                self.assertFalse(config.failure_diagnostics_directory.exists())

    def test_rejects_installed_app_tree_mismatch_before_collecting_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            operations = RecordingOperations(self.make_app(root / "source"))
            config = self.config(root, expected_app_tree_sha256="0" * 64)

            with self.assertRaisesRegex(InstalledUIQualificationError, "tree digest"):
                run(config, operations)

            self.assertIsNone(operations.collect_arguments)
            self.assertFalse(config.qualification_root.exists())
            self.assertFalse(config.evidence_directory.exists())

    def test_rejects_every_output_path_overlap(self) -> None:
        path_fields = ("qualification_root", "evidence_directory", "failure_diagnostics_directory")
        for left_field, right_field in combinations(path_fields, 2):
            for relationship in ("equal", "left-parent", "right-parent"):
                with (
                    self.subTest(left=left_field, right=right_field, relationship=relationship),
                    tempfile.TemporaryDirectory() as temporary_directory,
                ):
                    root = Path(temporary_directory)
                    config = self.config(root)
                    shared = root / "shared"
                    if relationship == "equal":
                        left_path = right_path = shared
                    elif relationship == "left-parent":
                        left_path, right_path = shared, shared / "nested"
                    else:
                        left_path, right_path = shared / "nested", shared
                    overlapping = replace(config, **{left_field: left_path, right_field: right_path})

                    with self.assertRaisesRegex(InstalledUIQualificationError, "must not overlap"):
                        validate_installed_ui_output_paths(overlapping)

    def test_rejects_stale_output_files_directories_and_symlinks(self) -> None:
        for field in ("qualification_root", "evidence_directory", "failure_diagnostics_directory"):
            for path_kind in ("file", "directory", "symlink"):
                with (
                    self.subTest(field=field, path_kind=path_kind),
                    tempfile.TemporaryDirectory() as temporary_directory,
                ):
                    root = Path(temporary_directory)
                    config = self.config(root)
                    path = getattr(config, field)
                    if path is None:
                        raise AssertionError("test output path must be configured")
                    path.parent.mkdir(parents=True, exist_ok=True)
                    if path_kind == "file":
                        path.write_text("stale\n", encoding="utf-8")
                    elif path_kind == "directory":
                        path.mkdir()
                    else:
                        path.symlink_to(root / "missing-output", target_is_directory=True)

                    with self.assertRaisesRegex(InstalledUIQualificationError, "must not already exist"):
                        validate_installed_ui_output_paths(config)

    def test_primary_failure_survives_diagnostics_and_cleanup_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = self.config(root)

            with (
                patch(
                    "scripts.installed_ui_qualification._preserve_failure_diagnostics",
                    side_effect=OSError("diagnostics failure"),
                ),
                self.assertRaisesRegex(CleanMachineError, "primary UI failure") as raised,
            ):
                run(config, FailingOperations())

            notes = getattr(raised.exception, "__notes__", [])
            self.assertTrue(any("diagnostics failure" in note for note in notes))
            self.assertTrue(any("cleanup quit failure" in note for note in notes))
            self.assertFalse(config.qualification_root.exists())

    def test_preserves_bounded_diagnostics_for_evidence_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            app_source = self.make_app(root / "source")
            config = self.config(root, expected_app_tree_sha256=app_tree_sha256(app_source))
            operations = RecordingOperations(app_source, collect_error=CleanMachineError("evidence failure"))

            with self.assertRaisesRegex(CleanMachineError, "evidence failure"):
                run(config, operations)

            failure_text = (config.failure_diagnostics_directory / "failure.txt").read_text()
            candidate_evidence = config.failure_diagnostics_directory / "InstalledUIEvidence" / "candidate-ui.json"
            self.assertIn("CleanMachineError: evidence failure", failure_text)
            self.assertTrue(candidate_evidence.is_file())
            self.assertFalse(config.qualification_root.exists())
            self.assertFalse(config.evidence_directory.exists())

    def test_cleanup_failure_removes_normalized_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            app_source = self.make_app(root / "source")
            config = self.config(root, expected_app_tree_sha256=app_tree_sha256(app_source))
            operations = RecordingOperations(app_source, failing_quit_call=2)

            with self.assertRaisesRegex(InstalledUIQualificationError, "cleanup failed.*cleanup quit failure"):
                run(config, operations)

            self.assertFalse(config.qualification_root.exists())
            self.assertFalse(config.evidence_directory.exists())

    def test_interrupt_remains_primary_when_cleanup_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            app_source = self.make_app(root / "source")
            config = self.config(root, expected_app_tree_sha256=app_tree_sha256(app_source))
            operations = RecordingOperations(
                app_source,
                collect_error=KeyboardInterrupt("operator interrupt"),
                failing_quit_call=1,
            )

            with self.assertRaisesRegex(KeyboardInterrupt, "operator interrupt") as raised:
                run(config, operations)

            notes = getattr(raised.exception, "__notes__", [])
            self.assertTrue(any("cleanup quit failure" in note for note in notes))
            self.assertFalse(config.qualification_root.exists())

    def test_interrupt_during_normalization_removes_partial_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            app_source = self.make_app(root / "source")
            config = self.config(root, expected_app_tree_sha256=app_tree_sha256(app_source))
            operations = RecordingOperations(app_source)

            def interrupt_normalization(
                _raw_directory: Path,
                evidence_directory: Path,
                *,
                release_notes_url: str,
            ) -> None:
                del release_notes_url
                evidence_directory.mkdir()
                (evidence_directory / "partial.json").write_text("{}\n", encoding="utf-8")
                raise KeyboardInterrupt("normalization interrupt")

            with (
                patch(
                    "scripts.installed_ui_qualification.normalize_installed_ui_candidate_evidence",
                    side_effect=interrupt_normalization,
                ),
                self.assertRaisesRegex(KeyboardInterrupt, "normalization interrupt"),
            ):
                run(config, operations)

            self.assertFalse(config.qualification_root.exists())
            self.assertFalse(config.evidence_directory.exists())

    def test_primary_failure_survives_evidence_cleanup_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = self.config(root)
            primary_error = CleanMachineError("primary UI failure")

            def fail_run(_config: InstalledUIQualificationConfig, _operations: InstalledUIOperations) -> None:
                config.evidence_directory.mkdir()
                raise primary_error

            with (
                patch("scripts.installed_ui_qualification._run", side_effect=fail_run),
                patch(
                    "scripts.installed_ui_qualification.shutil.rmtree",
                    side_effect=OSError("evidence cleanup failure"),
                ),
                self.assertRaisesRegex(CleanMachineError, "primary UI failure") as raised,
            ):
                run(config, FailingOperations())

            notes = getattr(raised.exception, "__notes__", [])
            self.assertTrue(any("evidence cleanup failure" in note for note in notes))


if __name__ == "__main__":
    unittest.main()
