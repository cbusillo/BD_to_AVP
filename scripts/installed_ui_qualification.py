from __future__ import annotations

import json
import shutil
import uuid

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.artifact_identity import app_tree_sha256
from scripts.tier3_clean_machine import (
    APP_NAME,
    MacOSOperations,
    normalize_installed_ui_candidate_evidence,
)


class InstalledUIQualificationError(RuntimeError):
    pass


@dataclass(frozen=True)
class InstalledUIQualificationConfig:
    repo: Path
    dmg: Path
    qualification_root: Path
    evidence_directory: Path
    release_notes_url: str
    expected_app_tree_sha256: str
    owner: str
    failure_diagnostics_directory: Path | None = None


def _paths_overlap(left: Path, right: Path) -> bool:
    resolved_left = left.resolve()
    resolved_right = right.resolve()
    return (
        resolved_left == resolved_right
        or resolved_left in resolved_right.parents
        or resolved_right in resolved_left.parents
    )


def validate_installed_ui_output_paths(config: InstalledUIQualificationConfig) -> None:
    directories = [
        ("qualification root", config.qualification_root),
        ("evidence directory", config.evidence_directory),
    ]
    if config.failure_diagnostics_directory is not None:
        directories.append(("failure diagnostics directory", config.failure_diagnostics_directory))
    for index, (left_name, left_path) in enumerate(directories):
        for right_name, right_path in directories[index + 1 :]:
            if _paths_overlap(left_path, right_path):
                raise InstalledUIQualificationError(f"Installed UI {left_name} and {right_name} must not overlap.")
    if any(path.exists() or path.is_symlink() for _, path in directories):
        raise InstalledUIQualificationError("Installed UI outputs and workspace must not already exist.")


def _preserve_failure_diagnostics(
    config: InstalledUIQualificationConfig,
    raw_ui_directory: Path,
    error: BaseException,
) -> None:
    destination = config.failure_diagnostics_directory
    if destination is None:
        return
    destination.mkdir(parents=True)
    (destination / "failure.txt").write_text(f"{type(error).__name__}: {error}\n", encoding="utf-8")
    result_bundle = config.qualification_root / "InstalledUI-candidate.xcresult"
    if result_bundle.is_dir():
        shutil.copytree(result_bundle, destination / result_bundle.name)
    if raw_ui_directory.is_dir():
        shutil.copytree(raw_ui_directory, destination / raw_ui_directory.name)


def _cleanup_qualification_workspace(
    config: InstalledUIQualificationConfig,
    operations: MacOSOperations,
    marker_path: Path,
    marker: Mapping[str, str],
) -> list[Exception]:
    errors: list[Exception] = []
    try:
        operations.quit_app()
        if operations.app_running():
            raise InstalledUIQualificationError("Installed UI qualification left the production app running.")
    except Exception as error:
        errors.append(error)

    if not config.qualification_root.exists():
        return errors
    try:
        observed_marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        errors.append(InstalledUIQualificationError("Installed UI cleanup marker is missing or invalid."))
        return errors
    if observed_marker != marker:
        errors.append(InstalledUIQualificationError("Installed UI cleanup marker changed during the run."))
        return errors
    try:
        shutil.rmtree(config.qualification_root)
    except Exception as error:
        errors.append(error)
    return errors


def _run(
    config: InstalledUIQualificationConfig,
    operations: MacOSOperations,
) -> Mapping[str, Any]:
    validate_installed_ui_output_paths(config)
    if operations.app_running():
        raise InstalledUIQualificationError("The production app must not be running before installed UI qualification.")

    synthetic_home = config.qualification_root / "Home"
    app_path = config.qualification_root / "Applications" / APP_NAME
    mount_point = config.qualification_root / "Mount"
    raw_ui_directory = config.qualification_root / "InstalledUIEvidence"
    marker_path = config.qualification_root / ".bd-to-avp-installed-ui.json"
    marker = {"owner": config.owner, "run_id": str(uuid.uuid4())}
    config.qualification_root.mkdir(parents=True)
    marker_path.write_text(json.dumps(marker, sort_keys=True) + "\n", encoding="utf-8")
    primary_error: Exception | None = None
    try:
        synthetic_home.mkdir(parents=True)
        operations.install_app(config.dmg, app_path, mount_point)
        installed_app_tree_sha256 = app_tree_sha256(app_path)
        if installed_app_tree_sha256 != config.expected_app_tree_sha256:
            raise InstalledUIQualificationError("Installed app tree digest does not match the packaged app.")
        operations.collect_ui_evidence(
            repo=config.repo.resolve(),
            phase="candidate",
            app_path=app_path,
            synthetic_home=synthetic_home,
            output_directory=raw_ui_directory,
            release_notes_url=config.release_notes_url,
        )
        operations.quit_app()
        if operations.app_running():
            raise InstalledUIQualificationError("Installed UI qualification left the production app running.")
        return normalize_installed_ui_candidate_evidence(
            raw_ui_directory,
            config.evidence_directory,
            release_notes_url=config.release_notes_url,
        )
    except Exception as error:
        primary_error = error
        try:
            _preserve_failure_diagnostics(config, raw_ui_directory, error)
        except Exception as diagnostics_error:
            error.add_note(
                "Installed UI failure diagnostics could not be preserved: "
                f"{type(diagnostics_error).__name__}: {diagnostics_error}"
            )
        raise
    finally:
        cleanup_errors = _cleanup_qualification_workspace(config, operations, marker_path, marker)
        if cleanup_errors:
            summary = "; ".join(f"{type(error).__name__}: {error}" for error in cleanup_errors)
            if primary_error is not None:
                primary_error.add_note(f"Installed UI cleanup also failed: {summary}")
            else:
                raise InstalledUIQualificationError(f"Installed UI cleanup failed: {summary}") from cleanup_errors[0]


def run(
    config: InstalledUIQualificationConfig,
    operations: MacOSOperations | None = None,
) -> Mapping[str, Any]:
    operations = operations or MacOSOperations()
    try:
        return _run(config, operations)
    except Exception:
        if config.evidence_directory.exists():
            shutil.rmtree(config.evidence_directory)
        raise
