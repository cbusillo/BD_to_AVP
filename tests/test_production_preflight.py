import json
import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from scripts.production_preflight import (
    MAIN_REMOTE_REF,
    ProductionPreflightError,
    finalize_success,
    validate_source,
)


SOURCE_SHA = "1" * 40
WORKFLOW_REF = "cbusillo/BD_to_AVP/.github/workflows/production-preflight.yml@refs/heads/main"


def valid_environment() -> dict[str, str]:
    return {
        "GITHUB_ACTOR": "cbusillo",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_REPOSITORY": "cbusillo/BD_to_AVP",
        "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_RUN_ID": "123456789",
        "GITHUB_SHA": SOURCE_SHA,
        "GITHUB_TRIGGERING_ACTOR": "cbusillo",
        "GITHUB_WORKFLOW_REF": WORKFLOW_REF,
        "GITHUB_WORKFLOW_SHA": SOURCE_SHA,
    }


class ProductionPreflightTests(unittest.TestCase):
    @patch("scripts.production_preflight._git_output")
    def test_validate_source_binds_exact_current_protected_main(self, git_output) -> None:
        git_output.side_effect = lambda _repo, *arguments: (
            SOURCE_SHA if arguments in (("rev-parse", "HEAD"), ("rev-parse", MAIN_REMOTE_REF)) else ""
        )

        report = validate_source(SOURCE_SHA, environment=valid_environment())

        self.assertEqual(report["source_sha"], SOURCE_SHA)
        self.assertEqual(report["checkout_sha"], SOURCE_SHA)
        self.assertEqual(report["protected_main_sha"], SOURCE_SHA)
        self.assertEqual(
            report["implementation"],
            {"path": ".github/workflows/production-preflight-engine.yml", "sha": SOURCE_SHA},
        )
        self.assertEqual(report["workflow"]["ref"], WORKFLOW_REF)
        self.assertEqual(report["workflow"]["path"], ".github/workflows/production-preflight.yml")
        self.assertEqual(report["workflow"]["run_id"], 123456789)
        self.assertEqual(report["workflow"]["run_attempt"], 2)

    @patch("scripts.production_preflight._git_output")
    def test_validate_source_accepts_guarded_prerelease_caller(self, git_output) -> None:
        git_output.return_value = SOURCE_SHA
        environment = valid_environment()
        environment["GITHUB_WORKFLOW_REF"] = "cbusillo/BD_to_AVP/.github/workflows/prerelease.yml@refs/heads/main"

        report = validate_source(SOURCE_SHA, environment=environment)

        self.assertEqual(report["workflow"]["path"], ".github/workflows/prerelease.yml")

    def test_validate_source_rejects_abbreviated_sha(self) -> None:
        with self.assertRaisesRegex(ProductionPreflightError, "full lowercase Git SHA"):
            validate_source(SOURCE_SHA[:12], environment=valid_environment())

    def test_validate_source_rejects_non_main_ref(self) -> None:
        environment = valid_environment()
        environment["GITHUB_REF"] = "refs/heads/task"

        with self.assertRaisesRegex(ProductionPreflightError, "protected main"):
            validate_source(SOURCE_SHA, environment=environment)

    def test_validate_source_rejects_unapproved_main_workflow(self) -> None:
        environment = valid_environment()
        environment["GITHUB_WORKFLOW_REF"] = "cbusillo/BD_to_AVP/.github/workflows/unapproved.yml@refs/heads/main"

        with self.assertRaisesRegex(ProductionPreflightError, "caller workflow is not approved"):
            validate_source(SOURCE_SHA, environment=environment)

    @patch("scripts.production_preflight._git_output")
    def test_validate_source_rejects_stale_main_sha(self, git_output) -> None:
        git_output.side_effect = [SOURCE_SHA, "2" * 40]

        with self.assertRaisesRegex(ProductionPreflightError, "no longer current protected main"):
            validate_source(SOURCE_SHA, environment=valid_environment())

    @patch("scripts.production_preflight._git_output")
    def test_validate_source_rejects_mismatched_dispatch_sha(self, git_output) -> None:
        environment = valid_environment()
        environment["GITHUB_SHA"] = "2" * 40

        with self.assertRaisesRegex(ProductionPreflightError, "input SHA does not match"):
            validate_source(SOURCE_SHA, environment=environment)

        git_output.assert_not_called()

    @patch("scripts.production_preflight._git_output")
    def test_validate_source_rejects_workflow_definition_from_another_commit(self, git_output) -> None:
        environment = valid_environment()
        environment["GITHUB_WORKFLOW_SHA"] = "2" * 40

        with self.assertRaisesRegex(ProductionPreflightError, "workflow definition does not match"):
            validate_source(SOURCE_SHA, environment=environment)

        git_output.assert_not_called()

    def test_finalize_success_binds_smoke_and_installed_ui_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_report = root / "source.json"
            smoke_log = root / "smoke.log"
            installed_ui_report = root / "installed-ui.json"
            installed_ui_evidence = root / "evidence"
            installed_ui_evidence.mkdir()
            (installed_ui_evidence / "candidate-ui.json").write_text('{"passed": true}\n', encoding="utf-8")
            source_report.write_text(
                json.dumps(
                    {
                        "source_sha": SOURCE_SHA,
                        "implementation": {
                            "path": ".github/workflows/production-preflight-engine.yml",
                            "sha": SOURCE_SHA,
                        },
                        "workflow": {"run_id": 123456789, "run_attempt": 2, "ref": WORKFLOW_REF},
                    }
                ),
                encoding="utf-8",
            )
            smoke_log.write_text(
                "Package version metadata passed: 0.3.2b5\nRelease app smoke passed\n", encoding="utf-8"
            )
            installed_ui_report.write_text(
                json.dumps(
                    {
                        "app_tree_sha256": "a" * 64,
                        "dmg": {"name": "preventive.dmg", "sha256": "b" * 64, "size_bytes": 42},
                        "source_sha": SOURCE_SHA,
                        "workflow": {"run_id": 123456789, "run_attempt": 2},
                    }
                ),
                encoding="utf-8",
            )

            report = finalize_success(
                source_report=source_report,
                smoke_log=smoke_log,
                installed_ui_report=installed_ui_report,
                installed_ui_evidence=installed_ui_evidence,
            )

        self.assertEqual(report["source_sha"], SOURCE_SHA)
        self.assertEqual(report["implementation"]["sha"], SOURCE_SHA)
        self.assertEqual(report["package_version"], "0.3.2b5")
        self.assertEqual(report["app_tree_sha256"], "a" * 64)
        self.assertEqual(report["preventive_dmg"]["sha256"], "b" * 64)
        self.assertEqual(report["production_package_smoke"]["size_bytes"], 66)
        self.assertEqual(report["installed_ui_evidence"]["file_count"], 1)

    def test_finalize_success_rejects_source_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_report = root / "source.json"
            smoke_log = root / "smoke.log"
            installed_ui_report = root / "installed-ui.json"
            installed_ui_evidence = root / "evidence"
            installed_ui_evidence.mkdir()
            (installed_ui_evidence / "candidate-ui.json").write_text("{}\n", encoding="utf-8")
            source_report.write_text(
                json.dumps(
                    {
                        "source_sha": SOURCE_SHA,
                        "implementation": {
                            "path": ".github/workflows/production-preflight-engine.yml",
                            "sha": SOURCE_SHA,
                        },
                        "workflow": {"run_id": 1, "run_attempt": 1},
                    }
                ),
                encoding="utf-8",
            )
            smoke_log.write_text("Package version metadata passed: 1.2.3\n", encoding="utf-8")
            installed_ui_report.write_text(
                json.dumps(
                    {
                        "app_tree_sha256": "a" * 64,
                        "dmg": {},
                        "source_sha": "2" * 40,
                        "workflow": {"run_id": 1, "run_attempt": 1},
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ProductionPreflightError, "source SHA does not match"):
                finalize_success(
                    source_report=source_report,
                    smoke_log=smoke_log,
                    installed_ui_report=installed_ui_report,
                    installed_ui_evidence=installed_ui_evidence,
                )
