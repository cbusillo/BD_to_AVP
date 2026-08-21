from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from scripts.artifact_identity import app_tree_sha256
from scripts.installed_ui_qualification import (
    InstalledUIQualificationConfig,
    InstalledUIQualificationError,
    run as run_installed_ui_qualification,
    validate_installed_ui_output_paths,
)
from scripts.macos_release import MacOSReleaseError, create_release_dmg
from scripts.tier3_clean_machine import CleanMachineError, RELEASES_URL


REPO_ROOT = Path(__file__).resolve().parents[1]


class PreSigningUIError(RuntimeError):
    pass


@dataclass(frozen=True)
class PreSigningUIConfig:
    repo: Path
    app: Path
    dmg: Path
    qualification_root: Path
    evidence_directory: Path
    output_report: Path
    failure_diagnostics_directory: Path | None
    release_notes_url: str
    source_sha: str
    workflow_run_id: int
    workflow_run_attempt: int


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _path_is_within(path: Path, directory: Path) -> bool:
    resolved_path = path.resolve()
    resolved_directory = directory.resolve()
    return resolved_path == resolved_directory or resolved_directory in resolved_path.parents


def _write_report(path: Path, report: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(report, temporary, indent=2, sort_keys=True)
            temporary.write("\n")
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def _remove_owned_output(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def run(config: PreSigningUIConfig) -> Mapping[str, object]:
    for path in (config.dmg, config.output_report):
        if path.exists() or path.is_symlink():
            raise PreSigningUIError(f"Pre-signing UI output already exists: {path}")
    if config.dmg.resolve() == config.output_report.resolve():
        raise PreSigningUIError("Pre-signing DMG and report paths must be different.")
    if not config.app.is_dir():
        raise PreSigningUIError(f"Pre-signing packaged app is missing: {config.app}")
    output_directories = [config.qualification_root, config.evidence_directory]
    if config.failure_diagnostics_directory is not None:
        output_directories.append(config.failure_diagnostics_directory)
    for output_path in (config.dmg, config.output_report):
        if any(_path_is_within(output_path, directory) for directory in output_directories):
            raise PreSigningUIError("Pre-signing DMG and report paths must be outside UI output directories.")

    expected_app_tree_sha256 = app_tree_sha256(config.app)
    installed_ui_config = InstalledUIQualificationConfig(
        repo=config.repo,
        dmg=config.dmg,
        qualification_root=config.qualification_root,
        evidence_directory=config.evidence_directory,
        release_notes_url=config.release_notes_url,
        expected_app_tree_sha256=expected_app_tree_sha256,
        owner="bd-to-avp-pre-signing-ui",
        failure_diagnostics_directory=config.failure_diagnostics_directory,
    )
    try:
        validate_installed_ui_output_paths(installed_ui_config)
    except InstalledUIQualificationError as error:
        raise PreSigningUIError(str(error)) from error
    try:
        create_release_dmg(config.app, config.dmg, verify_signatures=False)
        evidence = run_installed_ui_qualification(installed_ui_config)
        report = {
            "app_tree_sha256": expected_app_tree_sha256,
            "dmg": {
                "name": config.dmg.name,
                "sha256": _file_sha256(config.dmg),
                "size_bytes": config.dmg.stat().st_size,
            },
            "evidence": dict(evidence),
            "schema_version": 1,
            "source_sha": config.source_sha,
            "workflow": {
                "run_attempt": config.workflow_run_attempt,
                "run_id": config.workflow_run_id,
            },
        }
        _write_report(config.output_report, report)
        return report
    except BaseException as error:
        for path in (config.output_report, config.evidence_directory, config.dmg):
            try:
                _remove_owned_output(path)
            except BaseException as cleanup_error:
                error.add_note(
                    f"Pre-signing output cleanup failed for {path}: {type(cleanup_error).__name__}: {cleanup_error}"
                )
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run installed UI qualification before production signing.")
    parser.add_argument("--repo", type=Path, default=REPO_ROOT)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--dmg", type=Path, required=True)
    parser.add_argument("--qualification-root", type=Path, required=True)
    parser.add_argument("--evidence-directory", type=Path, required=True)
    parser.add_argument("--output-report", type=Path, required=True)
    parser.add_argument("--failure-diagnostics-directory", type=Path)
    parser.add_argument("--release-notes-url", default=RELEASES_URL)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--workflow-run-id", type=int, required=True)
    parser.add_argument("--workflow-run-attempt", type=int, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = PreSigningUIConfig(
        repo=args.repo,
        app=args.app,
        dmg=args.dmg,
        qualification_root=args.qualification_root,
        evidence_directory=args.evidence_directory,
        output_report=args.output_report,
        failure_diagnostics_directory=args.failure_diagnostics_directory,
        release_notes_url=args.release_notes_url,
        source_sha=args.source_sha,
        workflow_run_id=args.workflow_run_id,
        workflow_run_attempt=args.workflow_run_attempt,
    )
    try:
        run(config)
    except (CleanMachineError, InstalledUIQualificationError, MacOSReleaseError, PreSigningUIError) as error:
        build_parser().error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
