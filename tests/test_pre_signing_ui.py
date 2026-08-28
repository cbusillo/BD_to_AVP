import json
import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from scripts.pre_signing_ui import PreSigningUIConfig, PreSigningUIError, run
from scripts.tier3_clean_machine import CleanMachineError


class PreSigningUITests(unittest.TestCase):
    @staticmethod
    def config(root: Path) -> PreSigningUIConfig:
        app = root / "3D Blu-ray to Vision Pro.app"
        app.mkdir(parents=True)
        return PreSigningUIConfig(
            repo=root,
            app=app,
            dmg=root / "candidate.dmg",
            qualification_root=root / "qualification",
            evidence_directory=root / "evidence",
            output_report=root / "report.json",
            failure_diagnostics_directory=root / "diagnostics",
            release_notes_url="https://example.com/releases",
            source_sha="a" * 40,
            workflow_run_id=123,
            workflow_run_attempt=2,
        )

    def test_builds_dmg_runs_shared_fixture_and_writes_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            app = root / "3D Blu-ray to Vision Pro.app"
            app.mkdir()
            dmg = root / "candidate.dmg"
            report_path = root / "report.json"
            config = PreSigningUIConfig(
                repo=root,
                app=app,
                dmg=dmg,
                qualification_root=root / "qualification",
                evidence_directory=root / "evidence",
                output_report=report_path,
                failure_diagnostics_directory=root / "diagnostics",
                release_notes_url="https://example.com/releases",
                source_sha="a" * 40,
                workflow_run_id=123,
                workflow_run_attempt=2,
            )

            def create_dmg(_app: Path, output: Path, *, verify_signatures: bool) -> Path:
                self.assertFalse(verify_signatures)
                output.write_bytes(b"dmg")
                return output

            with (
                patch("scripts.pre_signing_ui.app_tree_sha256", return_value="b" * 64),
                patch("scripts.pre_signing_ui.create_release_dmg", side_effect=create_dmg) as create_mock,
                patch(
                    "scripts.pre_signing_ui.run_installed_ui_qualification",
                    return_value={"candidate-ui.json": "c" * 64},
                ) as ui_mock,
            ):
                report = run(config)

            saved = json.loads(report_path.read_text(encoding="utf-8"))

        create_mock.assert_called_once()
        ui_config = ui_mock.call_args.args[0]
        self.assertEqual(ui_config.dmg, dmg)
        self.assertEqual(ui_config.expected_app_tree_sha256, "b" * 64)
        self.assertEqual(ui_config.owner, "bd-to-avp-pre-signing-ui")
        self.assertEqual(report, saved)
        self.assertEqual(saved["schema_version"], 1)
        self.assertEqual(saved["source_sha"], "a" * 40)
        self.assertEqual(saved["workflow"], {"run_attempt": 2, "run_id": 123})
        self.assertEqual(saved["dmg"]["size_bytes"], 3)
        self.assertEqual(saved["evidence"], {"candidate-ui.json": "c" * 64})

    def test_rejects_existing_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            app = root / "3D Blu-ray to Vision Pro.app"
            app.mkdir()
            report_path = root / "report.json"
            report_path.write_text("{}\n", encoding="utf-8")
            config = PreSigningUIConfig(
                repo=root,
                app=app,
                dmg=root / "candidate.dmg",
                qualification_root=root / "qualification",
                evidence_directory=root / "evidence",
                output_report=report_path,
                failure_diagnostics_directory=None,
                release_notes_url="https://example.com/releases",
                source_sha="a" * 40,
                workflow_run_id=123,
                workflow_run_attempt=1,
            )

            with self.assertRaisesRegex(PreSigningUIError, "output already exists"):
                run(config)

    def test_rejects_stale_shared_outputs_before_creating_dmg(self) -> None:
        for output_name in ("qualification_root", "evidence_directory", "failure_diagnostics_directory"):
            with self.subTest(output_name=output_name), tempfile.TemporaryDirectory() as temporary_directory:
                root = Path(temporary_directory)
                config = self.config(root)
                output_path = getattr(config, output_name)
                if not isinstance(output_path, Path):
                    raise AssertionError("shared output path must be configured")
                output_path.mkdir(parents=True)

                with (
                    patch("scripts.pre_signing_ui.app_tree_sha256", return_value="b" * 64),
                    patch("scripts.pre_signing_ui.create_release_dmg") as create_mock,
                    self.assertRaisesRegex(PreSigningUIError, "must not already exist"),
                ):
                    run(config)

                create_mock.assert_not_called()

    def test_removes_owned_outputs_when_installed_ui_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = self.config(root)

            def create_dmg(_app: Path, output: Path, *, verify_signatures: bool) -> Path:
                self.assertFalse(verify_signatures)
                output.write_bytes(b"dmg")
                return output

            def fail_installed_ui(_config: object) -> object:
                config.evidence_directory.mkdir()
                if config.failure_diagnostics_directory is None:
                    raise AssertionError("diagnostics path must be configured")
                config.failure_diagnostics_directory.mkdir()
                (config.failure_diagnostics_directory / "failure.txt").write_text(
                    "simulated failure\n",
                    encoding="utf-8",
                )
                raise CleanMachineError("simulated UI failure")

            with (
                patch("scripts.pre_signing_ui.app_tree_sha256", return_value="b" * 64),
                patch("scripts.pre_signing_ui.create_release_dmg", side_effect=create_dmg),
                patch("scripts.pre_signing_ui.run_installed_ui_qualification", side_effect=fail_installed_ui),
                self.assertRaisesRegex(CleanMachineError, "simulated UI failure"),
            ):
                run(config)

            self.assertFalse(config.dmg.exists())
            self.assertFalse(config.evidence_directory.exists())
            self.assertFalse(config.output_report.exists())
            self.assertTrue((config.failure_diagnostics_directory / "failure.txt").is_file())

    def test_removes_owned_outputs_when_report_write_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = self.config(root)

            def create_dmg(_app: Path, output: Path, *, verify_signatures: bool) -> Path:
                self.assertFalse(verify_signatures)
                output.write_bytes(b"dmg")
                return output

            def collect_evidence(_config: object) -> dict[str, str]:
                config.evidence_directory.mkdir()
                return {"candidate-ui.json": "c" * 64}

            with (
                patch("scripts.pre_signing_ui.app_tree_sha256", return_value="b" * 64),
                patch("scripts.pre_signing_ui.create_release_dmg", side_effect=create_dmg),
                patch("scripts.pre_signing_ui.run_installed_ui_qualification", side_effect=collect_evidence),
                patch("scripts.pre_signing_ui._write_report", side_effect=OSError("report failure")),
                self.assertRaisesRegex(OSError, "report failure"),
            ):
                run(config)

            self.assertFalse(config.dmg.exists())
            self.assertFalse(config.evidence_directory.exists())
            self.assertFalse(config.output_report.exists())


if __name__ == "__main__":
    unittest.main()
