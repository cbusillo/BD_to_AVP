from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast


REPO_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "cbusillo/BD_to_AVP"
MAIN_REF = "refs/heads/main"
MAIN_REMOTE_REF = "refs/remotes/origin/main"
REQUIRED_EVENT = "workflow_dispatch"
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
WORKFLOW_REF_PATTERN = re.compile(rf"^{re.escape(REPOSITORY)}/\.github/workflows/[^@]+@{re.escape(MAIN_REF)}$")
PACKAGE_VERSION_PREFIX = "Package version metadata passed: "
PRODUCTION_PREFLIGHT_WORKFLOW_PATH = ".github/workflows/production-preflight.yml"
PRODUCTION_PREFLIGHT_ENGINE_WORKFLOW_PATH = ".github/workflows/production-preflight-engine.yml"
STABLE_WORKFLOW_PATH = ".github/workflows/briefcase.yml"
PRERELEASE_WORKFLOW_PATH = ".github/workflows/prerelease.yml"
ALLOWED_CALLER_WORKFLOW_PATHS = {
    PRODUCTION_PREFLIGHT_WORKFLOW_PATH,
    STABLE_WORKFLOW_PATH,
    PRERELEASE_WORKFLOW_PATH,
}
MAX_REPORT_BYTES = 1024 * 1024
MAX_EVIDENCE_FILES = 100
MAX_EVIDENCE_FILE_BYTES = 1024 * 1024
MAX_EVIDENCE_TOTAL_BYTES = 16 * 1024 * 1024


class ProductionPreflightError(RuntimeError):
    pass


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "")
    if not value:
        raise ProductionPreflightError(f"Production preflight is missing {name}.")
    return value


def _full_sha(value: str, description: str) -> str:
    if SHA_PATTERN.fullmatch(value) is None:
        raise ProductionPreflightError(f"{description} must be a full lowercase Git SHA.")
    return value


def _positive_int(value: str, description: str) -> int:
    if not value.isdecimal() or int(value) <= 0:
        raise ProductionPreflightError(f"{description} must be a positive integer.")
    return int(value)


def _caller_workflow_path(workflow_ref: str) -> str:
    prefix = f"{REPOSITORY}/"
    suffix = f"@{MAIN_REF}"
    if WORKFLOW_REF_PATTERN.fullmatch(workflow_ref) is None:
        raise ProductionPreflightError("Production preflight workflow must be loaded from protected main.")
    workflow_path = workflow_ref.removeprefix(prefix).removesuffix(suffix)
    if workflow_path not in ALLOWED_CALLER_WORKFLOW_PATHS:
        raise ProductionPreflightError(f"Production preflight caller workflow is not approved: {workflow_path}")
    return workflow_path


def _git_output(repo: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        command = " ".join(arguments)
        raise ProductionPreflightError(f"Could not inspect production preflight Git state: {command}") from error
    return result.stdout.strip()


def validate_source(
    source_sha: str,
    *,
    repo: Path = REPO_ROOT,
    environment: Mapping[str, str] = os.environ,
) -> Mapping[str, object]:
    source_sha = _full_sha(source_sha, "Production preflight source SHA")
    repository = _required(environment, "GITHUB_REPOSITORY")
    if repository != REPOSITORY:
        raise ProductionPreflightError(f"Production preflight repository {repository!r} does not match {REPOSITORY!r}.")
    event_name = _required(environment, "GITHUB_EVENT_NAME")
    if event_name != REQUIRED_EVENT:
        raise ProductionPreflightError("Production preflight must originate from workflow_dispatch.")
    ref = _required(environment, "GITHUB_REF")
    if ref != MAIN_REF:
        raise ProductionPreflightError("Production preflight must originate from protected main.")
    github_sha = _full_sha(_required(environment, "GITHUB_SHA"), "GitHub source SHA")
    if github_sha != source_sha:
        raise ProductionPreflightError("Production preflight input SHA does not match the dispatched main commit.")
    workflow_ref = _required(environment, "GITHUB_WORKFLOW_REF")
    workflow_path = _caller_workflow_path(workflow_ref)
    workflow_sha = _full_sha(_required(environment, "GITHUB_WORKFLOW_SHA"), "Workflow definition SHA")
    if workflow_sha != source_sha:
        raise ProductionPreflightError(
            "Production preflight workflow definition does not match the requested source SHA."
        )

    checkout_sha = _full_sha(_git_output(repo, "rev-parse", "HEAD"), "Checked out source SHA")
    if checkout_sha != source_sha:
        raise ProductionPreflightError("Production preflight checkout does not match the requested source SHA.")
    protected_main_sha = _full_sha(
        _git_output(repo, "rev-parse", MAIN_REMOTE_REF),
        "Protected main SHA",
    )
    if protected_main_sha != source_sha:
        raise ProductionPreflightError("The requested production preflight SHA is no longer current protected main.")

    return {
        "checkout_sha": checkout_sha,
        "event_name": event_name,
        "protected_main_ref": MAIN_REF,
        "protected_main_sha": protected_main_sha,
        "repository": repository,
        "schema_version": 1,
        "source_sha": source_sha,
        "implementation": {
            "path": PRODUCTION_PREFLIGHT_ENGINE_WORKFLOW_PATH,
            "sha": source_sha,
        },
        "workflow": {
            "actor": _required(environment, "GITHUB_ACTOR"),
            "path": workflow_path,
            "ref": workflow_ref,
            "run_attempt": _positive_int(
                _required(environment, "GITHUB_RUN_ATTEMPT"),
                "Workflow run attempt",
            ),
            "run_id": _positive_int(_required(environment, "GITHUB_RUN_ID"), "Workflow run ID"),
            "sha": workflow_sha,
            "triggering_actor": _required(environment, "GITHUB_TRIGGERING_ACTOR"),
        },
    }


def _file_evidence(path: Path, *, maximum_bytes: int = MAX_REPORT_BYTES) -> Mapping[str, object]:
    if not path.is_file():
        raise ProductionPreflightError(f"Production preflight evidence file is missing: {path}")
    size_bytes = path.stat().st_size
    if size_bytes > maximum_bytes:
        raise ProductionPreflightError(f"Production preflight evidence exceeds its {maximum_bytes}-byte bound: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as evidence_file:
        for chunk in iter(lambda: evidence_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "name": path.name,
        "sha256": digest.hexdigest(),
        "size_bytes": size_bytes,
    }


def _directory_evidence(path: Path) -> Mapping[str, object]:
    if not path.is_dir():
        raise ProductionPreflightError(f"Production preflight evidence directory is missing: {path}")
    files = sorted(candidate for candidate in path.rglob("*") if candidate.is_file())
    if not files:
        raise ProductionPreflightError(f"Production preflight evidence directory is empty: {path}")
    if len(files) > MAX_EVIDENCE_FILES:
        raise ProductionPreflightError(
            f"Production preflight evidence exceeds its {MAX_EVIDENCE_FILES}-file bound: {path}"
        )
    entries: list[Mapping[str, object]] = []
    total_size_bytes = 0
    for candidate in files:
        evidence = dict(_file_evidence(candidate, maximum_bytes=MAX_EVIDENCE_FILE_BYTES))
        evidence["name"] = candidate.relative_to(path).as_posix()
        total_size_bytes += cast(int, evidence["size_bytes"])
        entries.append(evidence)
    if total_size_bytes > MAX_EVIDENCE_TOTAL_BYTES:
        raise ProductionPreflightError(
            f"Production preflight evidence exceeds its {MAX_EVIDENCE_TOTAL_BYTES}-byte total bound: {path}"
        )
    return {
        "file_count": len(entries),
        "files": entries,
        "name": path.name,
        "total_size_bytes": total_size_bytes,
    }


def _json_object(path: Path, description: str) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ProductionPreflightError(f"{description} is not readable JSON: {path}") from error
    if not isinstance(value, dict):
        raise ProductionPreflightError(f"{description} must be a JSON object: {path}")
    return cast(Mapping[str, Any], value)


def _package_version(smoke_log: Path) -> str:
    try:
        lines = smoke_log.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise ProductionPreflightError(f"Production package smoke log is unreadable: {smoke_log}") from error
    versions = [line.removeprefix(PACKAGE_VERSION_PREFIX) for line in lines if line.startswith(PACKAGE_VERSION_PREFIX)]
    if len(versions) != 1 or not versions[0]:
        raise ProductionPreflightError("Production package smoke must report exactly one package version.")
    return versions[0]


def finalize_success(
    *,
    source_report: Path,
    smoke_log: Path,
    installed_ui_report: Path,
    installed_ui_evidence: Path,
) -> Mapping[str, object]:
    source = _json_object(source_report, "Production preflight source report")
    installed_ui = _json_object(installed_ui_report, "Installed UI report")
    source_sha = source.get("source_sha")
    if not isinstance(source_sha, str) or SHA_PATTERN.fullmatch(source_sha) is None:
        raise ProductionPreflightError("Production preflight source report has an invalid source SHA.")
    if installed_ui.get("source_sha") != source_sha:
        raise ProductionPreflightError("Installed UI report source SHA does not match production preflight source.")
    source_workflow = source.get("workflow")
    implementation = source.get("implementation")
    installed_workflow = installed_ui.get("workflow")
    if not isinstance(source_workflow, dict) or not isinstance(installed_workflow, dict):
        raise ProductionPreflightError("Production preflight reports are missing workflow identity.")
    if implementation != {"path": PRODUCTION_PREFLIGHT_ENGINE_WORKFLOW_PATH, "sha": source_sha}:
        raise ProductionPreflightError("Production preflight source report has invalid shared implementation identity.")
    for field in ("run_id", "run_attempt"):
        if installed_workflow.get(field) != source_workflow.get(field):
            raise ProductionPreflightError(f"Installed UI report workflow {field} does not match source validation.")

    package_version = _package_version(smoke_log)
    app_tree_sha256 = installed_ui.get("app_tree_sha256")
    dmg = installed_ui.get("dmg")
    if not isinstance(app_tree_sha256, str) or DIGEST_PATTERN.fullmatch(app_tree_sha256) is None:
        raise ProductionPreflightError("Installed UI report has an invalid app tree digest.")
    if not isinstance(dmg, dict):
        raise ProductionPreflightError("Installed UI report is missing preventive DMG evidence.")
    dmg_name = dmg.get("name")
    dmg_sha256 = dmg.get("sha256")
    dmg_size_bytes = dmg.get("size_bytes")
    if not isinstance(dmg_name, str) or not dmg_name.endswith(".dmg"):
        raise ProductionPreflightError("Installed UI report has an invalid preventive DMG name.")
    if not isinstance(dmg_sha256, str) or DIGEST_PATTERN.fullmatch(dmg_sha256) is None:
        raise ProductionPreflightError("Installed UI report has an invalid preventive DMG digest.")
    if not isinstance(dmg_size_bytes, int) or isinstance(dmg_size_bytes, bool) or dmg_size_bytes <= 0:
        raise ProductionPreflightError("Installed UI report has an invalid preventive DMG size.")

    return {
        "app_tree_sha256": app_tree_sha256,
        "package_version": package_version,
        "preventive_dmg": dmg,
        "schema_version": 1,
        "source_sha": source_sha,
        "source_validation": _file_evidence(source_report),
        "production_package_smoke": _file_evidence(smoke_log),
        "installed_ui_qualification": _file_evidence(installed_ui_report),
        "installed_ui_evidence": _directory_evidence(installed_ui_evidence),
        "implementation": implementation,
        "workflow": source_workflow,
    }


def _write_json(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(value, temporary, indent=2, sort_keys=True)
            temporary.write("\n")
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate and bind release-independent production preflight evidence.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate-source")
    validate_parser.add_argument("--repo", type=Path, default=REPO_ROOT)
    validate_parser.add_argument("--source-sha", required=True)
    validate_parser.add_argument("--output", type=Path, required=True)

    finalize_parser = subparsers.add_parser("finalize")
    finalize_parser.add_argument("--source-report", type=Path, required=True)
    finalize_parser.add_argument("--smoke-log", type=Path, required=True)
    finalize_parser.add_argument("--installed-ui-report", type=Path, required=True)
    finalize_parser.add_argument("--installed-ui-evidence", type=Path, required=True)
    finalize_parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None, *, environment: Mapping[str, str] = os.environ) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "validate-source":
            report = validate_source(args.source_sha, repo=args.repo, environment=environment)
        else:
            report = finalize_success(
                source_report=args.source_report,
                smoke_log=args.smoke_log,
                installed_ui_report=args.installed_ui_report,
                installed_ui_evidence=args.installed_ui_evidence,
            )
        _write_json(args.output, report)
    except ProductionPreflightError as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
