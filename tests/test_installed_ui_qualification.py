import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from scripts.installed_ui_qualification import (
    InstalledUIQualificationConfig,
    InstalledUIQualificationError,
    run,
    validate_installed_ui_output_paths,
)
from scripts.tier3_clean_machine import CleanMachineError


class FailingOperations:
    def install_app(self, _dmg_path: Path, _destination: Path, _mount_point: Path) -> None:
        raise CleanMachineError("primary UI failure")

    def collect_ui_evidence(self, **_kwargs: object) -> None:
        raise AssertionError("evidence collection must not run after installation fails")

    def quit_app(self) -> None:
        raise RuntimeError("cleanup quit failure")

    def app_running(self) -> bool:
        return False


class InstalledUIQualificationTests(unittest.TestCase):
    @staticmethod
    def config(root: Path) -> InstalledUIQualificationConfig:
        return InstalledUIQualificationConfig(
            repo=root,
            dmg=root / "candidate.dmg",
            qualification_root=root / "qualification",
            evidence_directory=root / "evidence",
            release_notes_url="https://example.com/releases",
            expected_app_tree_sha256="a" * 64,
            owner="test-installed-ui",
            failure_diagnostics_directory=root / "diagnostics",
        )

    def test_rejects_overlapping_output_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = self.config(root)
            overlapping = InstalledUIQualificationConfig(
                **{
                    **config.__dict__,
                    "failure_diagnostics_directory": config.qualification_root / "diagnostics",
                }
            )

            with self.assertRaisesRegex(InstalledUIQualificationError, "must not overlap"):
                validate_installed_ui_output_paths(overlapping)

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


if __name__ == "__main__":
    unittest.main()
