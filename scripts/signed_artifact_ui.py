from __future__ import annotations

import argparse
import hashlib
import json
import shutil

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.installed_ui_qualification import (
    InstalledUIQualificationConfig,
    InstalledUIQualificationError,
    run as run_installed_ui_qualification,
)
from scripts.release_receipt import ReleaseReceiptError, validate_receipt as validate_release_receipt
from scripts.tier3_clean_machine import RELEASES_URL, CleanMachineError, MacOSOperations


REPO_ROOT = Path(__file__).resolve().parents[1]


class SignedArtifactUIError(RuntimeError):
    pass


@dataclass(frozen=True)
class SignedArtifactUIConfig:
    repo: Path
    release_receipt: Path
    dmg: Path
    qualification_root: Path
    evidence_directory: Path
    release_notes_url: str
    workflow_run_id: int
    workflow_run_attempt: int
    release_receipt_file_sha256: str
    failure_diagnostics_directory: Path | None = None


def _mapping(value: object, description: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SignedArtifactUIError(f"{description} must be a JSON object.")
    return value


def _load_json(path: Path, description: str) -> Mapping[str, Any]:
    try:
        return _mapping(json.loads(path.read_text(encoding="utf-8")), description)
    except OSError as error:
        raise SignedArtifactUIError(f"Unable to read {description} at {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise SignedArtifactUIError(f"Invalid JSON in {description} at {path}: {error}") from error


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact(receipt: Mapping[str, Any], kind: str) -> Mapping[str, Any]:
    artifacts = [
        _mapping(item, "release receipt artifact")
        for item in receipt.get("artifacts", [])
        if _mapping(item, "release receipt artifact").get("kind") == kind
    ]
    if len(artifacts) != 1:
        raise SignedArtifactUIError(f"Release receipt must contain exactly one {kind} artifact.")
    return artifacts[0]


def _run(config: SignedArtifactUIConfig, operations: MacOSOperations) -> Mapping[str, Any]:
    if config.evidence_directory.exists() or config.qualification_root.exists():
        raise SignedArtifactUIError("Signed artifact UI outputs and workspace must not already exist.")
    if operations.app_running():
        raise SignedArtifactUIError("The production app must not be running before signed artifact UI qualification.")
    if _file_sha256(config.release_receipt) != config.release_receipt_file_sha256:
        raise SignedArtifactUIError("Release receipt file digest does not match the exact build-receipt output.")
    release_receipt = _load_json(config.release_receipt, "release receipt")
    try:
        validate_release_receipt(release_receipt)
    except ReleaseReceiptError as error:
        raise SignedArtifactUIError(f"Release receipt is invalid: {error}") from error
    workflow = _mapping(release_receipt.get("workflow"), "release receipt workflow")
    if workflow.get("run_id") != config.workflow_run_id or workflow.get("run_attempt") != config.workflow_run_attempt:
        raise SignedArtifactUIError("Release receipt workflow binding does not match this run attempt.")

    dmg = _artifact(release_receipt, "dmg")
    if config.dmg.name != dmg.get("name"):
        raise SignedArtifactUIError("Downloaded DMG name does not match the release receipt.")
    if config.dmg.stat().st_size != dmg.get("size_bytes") or _file_sha256(config.dmg) != dmg.get("sha256"):
        raise SignedArtifactUIError("Downloaded DMG size or digest does not match the release receipt.")

    return run_installed_ui_qualification(
        InstalledUIQualificationConfig(
            repo=config.repo,
            dmg=config.dmg,
            qualification_root=config.qualification_root,
            evidence_directory=config.evidence_directory,
            release_notes_url=config.release_notes_url,
            expected_app_tree_sha256=str(release_receipt.get("signed_app_tree_sha256")),
            owner="bd-to-avp-signed-artifact-ui",
            failure_diagnostics_directory=config.failure_diagnostics_directory,
        ),
        operations,
    )


def run(config: SignedArtifactUIConfig, operations: MacOSOperations | None = None) -> Mapping[str, Any]:
    operations = operations or MacOSOperations()
    try:
        return _run(config, operations)
    except BaseException:
        if config.evidence_directory.exists():
            shutil.rmtree(config.evidence_directory)
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run signed artifact UI qualification against an exact DMG.")
    parser.add_argument("--repo", type=Path, default=REPO_ROOT)
    parser.add_argument("--release-receipt", type=Path, required=True)
    parser.add_argument("--dmg", type=Path, required=True)
    parser.add_argument("--qualification-root", type=Path, required=True)
    parser.add_argument("--evidence-directory", type=Path, required=True)
    parser.add_argument("--failure-diagnostics-directory", type=Path)
    parser.add_argument("--release-notes-url", default=RELEASES_URL)
    parser.add_argument("--workflow-run-id", type=int, required=True)
    parser.add_argument("--workflow-run-attempt", type=int, required=True)
    parser.add_argument("--release-receipt-file-sha256", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = SignedArtifactUIConfig(
        repo=args.repo,
        release_receipt=args.release_receipt,
        dmg=args.dmg,
        qualification_root=args.qualification_root,
        evidence_directory=args.evidence_directory,
        release_notes_url=args.release_notes_url,
        workflow_run_id=args.workflow_run_id,
        workflow_run_attempt=args.workflow_run_attempt,
        release_receipt_file_sha256=args.release_receipt_file_sha256,
        failure_diagnostics_directory=args.failure_diagnostics_directory,
    )
    try:
        run(config)
    except (
        CleanMachineError,
        InstalledUIQualificationError,
        SignedArtifactUIError,
    ) as error:
        build_parser().error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
