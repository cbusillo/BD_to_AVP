from __future__ import annotations

import argparse
import hashlib
import json

from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from scripts.release_receipt import (
    EXPECTED_BRANCH,
    EXPECTED_REPOSITORY,
    RECEIPT_ASSET_NAME,
    ROUTES,
    ReleaseReceiptError,
    file_sha256,
    validate_receipt,
)


EVIDENCE_INDEX_PATH = Path("docs/qualification/release-evidence-v1.json")
RELEASE_LEDGER_PATH = Path("docs/release-evidence/index-v1.json")


class ReleaseEvidenceError(RuntimeError):
    pass


def effective_successful_workflow_run_id(
    record: Mapping[str, Any],
    *,
    run_id_field: str = "workflow_run_id",
) -> int | None:
    run_id = record.get(run_id_field)
    if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id <= 0:
        return None
    if record.get("workflow_conclusion") == "success":
        return run_id
    if record.get("workflow_conclusion") != "failure":
        return None
    recovery = record.get("recovery_workflow_run")
    if not isinstance(recovery, Mapping) or recovery.get("operation") != "pypi_recovery":
        return None
    recovery_run_id = recovery.get("workflow_run_id")
    if (
        recovery.get("workflow_conclusion") != "success"
        or isinstance(recovery_run_id, bool)
        or not isinstance(recovery_run_id, int)
        or recovery_run_id <= 0
        or recovery_run_id == run_id
    ):
        return None
    return recovery_run_id


def _mapping(value: object, description: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ReleaseEvidenceError(f"{description} must be a JSON object.")
    return cast(Mapping[str, Any], value)


def _sequence(value: object, description: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise ReleaseEvidenceError(f"{description} must be a JSON array.")
    return value


def _string(value: object, description: str) -> str:
    if not isinstance(value, str) or not value:
        raise ReleaseEvidenceError(f"{description} must be a non-empty string.")
    return value


def _integer(value: object, description: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ReleaseEvidenceError(f"{description} must be a positive integer.")
    return value


def _load_json(path: Path, description: str) -> Mapping[str, Any]:
    try:
        return _mapping(json.loads(path.read_text(encoding="utf-8")), description)
    except OSError as error:
        raise ReleaseEvidenceError(f"Unable to read {description} at {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise ReleaseEvidenceError(f"Invalid JSON in {description} at {path}: {error}") from error


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _parse_timestamp(value: object, description: str) -> str:
    text = _string(value, description)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ReleaseEvidenceError(f"{description} must be an ISO-8601 timestamp.") from error
    if parsed.tzinfo is None:
        raise ReleaseEvidenceError(f"{description} must include a timezone.")
    return text


def _artifact(receipt: Mapping[str, Any], kind: str) -> Mapping[str, Any]:
    matches = [
        _mapping(item, "release receipt artifact")
        for item in _sequence(receipt.get("artifacts"), "release receipt artifacts")
        if _mapping(item, "release receipt artifact").get("kind") == kind
    ]
    if len(matches) != 1:
        raise ReleaseEvidenceError(f"Release receipt must contain one {kind} artifact.")
    return matches[0]


def validate_publication(
    workflow_run: Mapping[str, Any],
    release: Mapping[str, Any],
    receipt: Mapping[str, Any],
    receipt_path: Path,
    live_appcast_path: Path,
    recovery_workflow_run: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        validate_receipt(receipt)
    except ReleaseReceiptError as error:
        raise ReleaseEvidenceError(str(error)) from error

    route = _string(receipt.get("release_route"), "release route")
    expected_workflow_name, expected_workflow_path = ROUTES[route]
    workflow = _mapping(receipt.get("workflow"), "receipt workflow")
    source_sha = _string(receipt.get("source_sha"), "receipt source_sha")
    if workflow_run.get("name") != expected_workflow_name or workflow_run.get("path") != expected_workflow_path:
        raise ReleaseEvidenceError("Completed workflow identity does not match the release route.")
    if workflow_run.get("event") != "workflow_dispatch":
        raise ReleaseEvidenceError("Evidence reconciliation requires a workflow_dispatch release run.")
    if workflow_run.get("status") != "completed":
        raise ReleaseEvidenceError("Evidence reconciliation requires a completed release run.")
    workflow_conclusion = workflow_run.get("conclusion")
    recovery_workflow_run_id: int | None = None
    if workflow_conclusion != "success":
        if workflow_conclusion != "failure" or recovery_workflow_run is None:
            raise ReleaseEvidenceError("Evidence reconciliation requires a successful release or checked recovery run.")
        from scripts.stable_pypi_recovery import StablePyPIRecoveryError, load_evidence

        try:
            recovery_evidence = load_evidence()
        except StablePyPIRecoveryError as error:
            raise ReleaseEvidenceError(str(error)) from error
        expected_failed_run = _mapping(recovery_evidence.get("failed_run"), "Stable PyPI recovery failed run")
        expected_release = _mapping(recovery_evidence.get("release"), "Stable PyPI recovery release")
        if (
            route != "stable"
            or workflow_run.get("id") != expected_failed_run.get("id")
            or workflow_run.get("head_sha") != expected_release.get("source_sha")
            or _mapping(receipt.get("release"), "receipt release").get("tag") != expected_release.get("tag")
        ):
            raise ReleaseEvidenceError("Failed release run does not match the reviewed Stable PyPI recovery evidence.")
        expected_recovery_identity = {
            "name": "Stable PyPI recovery",
            "path": ".github/workflows/briefcase.yml",
            "event": "workflow_dispatch",
            "status": "completed",
            "conclusion": "success",
            "head_branch": EXPECTED_BRANCH,
            "display_title": "Stable PyPI recovery",
        }
        for field, expected in expected_recovery_identity.items():
            if recovery_workflow_run.get(field) != expected:
                raise ReleaseEvidenceError(f"Checked PyPI recovery run has unexpected {field}.")
        for actor_field in ("actor", "triggering_actor"):
            actor = _mapping(recovery_workflow_run.get(actor_field), f"recovery workflow run {actor_field}")
            if actor.get("login") != "shiny-code-bot":
                raise ReleaseEvidenceError(f"Recovery workflow run {actor_field} is not the approved release actor.")
        recovery_repository = _mapping(recovery_workflow_run.get("repository"), "recovery workflow repository")
        if recovery_repository.get("full_name") != EXPECTED_REPOSITORY:
            raise ReleaseEvidenceError("Recovery workflow run repository is not canonical.")
        recovery_workflow_run_id = _integer(recovery_workflow_run.get("id"), "recovery workflow run ID")
        if recovery_workflow_run_id == workflow_run.get("id"):
            raise ReleaseEvidenceError("Recovery workflow run must differ from the failed release run.")
    elif recovery_workflow_run is not None:
        raise ReleaseEvidenceError("A successful release run must not be reconciled through recovery mode.")
    if workflow_run.get("head_branch") != EXPECTED_BRANCH or workflow_run.get("head_sha") != source_sha:
        raise ReleaseEvidenceError("Completed release run is not bound to the receipt's protected-main SHA.")
    if workflow_run.get("id") != workflow.get("run_id") or workflow_run.get("run_attempt") != workflow.get(
        "run_attempt"
    ):
        raise ReleaseEvidenceError("Completed release run ID or attempt does not match the receipt.")
    for actor_field in ("actor", "triggering_actor"):
        actor = _mapping(workflow_run.get(actor_field), f"workflow run {actor_field}")
        if actor.get("login") != "shiny-code-bot":
            raise ReleaseEvidenceError(f"Workflow run {actor_field} is not the approved release actor.")
    repository = _mapping(workflow_run.get("repository"), "workflow run repository")
    if repository.get("full_name") != EXPECTED_REPOSITORY:
        raise ReleaseEvidenceError("Workflow run repository is not canonical.")

    receipt_release = _mapping(receipt.get("release"), "receipt release")
    release_id = _integer(receipt_release.get("id"), "receipt release id")
    if release.get("id") != release_id:
        raise ReleaseEvidenceError("Published release ID does not match the receipt.")
    if release.get("tag_name") != receipt_release.get("tag") or release.get("name") != receipt_release.get("name"):
        raise ReleaseEvidenceError("Published release tag or name does not match the receipt.")
    if release.get("target_commitish") != source_sha:
        raise ReleaseEvidenceError("Published release target does not match the receipt source SHA.")
    if release.get("immutable") is not True:
        raise ReleaseEvidenceError("Published release is not protected by GitHub release immutability.")
    if release.get("draft") is not False or release.get("prerelease") is not receipt_release.get("prerelease"):
        raise ReleaseEvidenceError("Release is mutable or its prerelease state conflicts with the receipt.")
    published_at = _parse_timestamp(release.get("published_at"), "release published_at")

    release_assets = _sequence(release.get("assets"), "release assets")
    expected_names = {
        _string(_artifact(receipt, "dmg").get("name"), "DMG name"),
        _string(_artifact(receipt, "checksum").get("name"), "checksum name"),
        _string(_artifact(receipt, "appcast").get("name"), "appcast name"),
        RECEIPT_ASSET_NAME,
    }
    names = [_string(_mapping(asset, "release asset").get("name"), "release asset name") for asset in release_assets]
    if len(names) != len(expected_names) or set(names) != expected_names:
        raise ReleaseEvidenceError("Published release asset set is incomplete or conflicting.")
    for kind in ("dmg", "checksum", "appcast"):
        recorded = _artifact(receipt, kind)
        matches = [
            _mapping(asset, "release asset")
            for asset in release_assets
            if _mapping(asset, "release asset").get("name") == recorded.get("name")
        ]
        if len(matches) != 1:
            raise ReleaseEvidenceError(f"Published release does not contain exactly one {kind} asset.")
        actual = matches[0]
        if actual.get("id") != recorded.get("asset_id") or actual.get("size") != recorded.get("size_bytes"):
            raise ReleaseEvidenceError(f"Published {kind} asset identity differs from the receipt.")
        if actual.get("digest") != f"sha256:{recorded.get('sha256')}":
            raise ReleaseEvidenceError(f"Published {kind} asset digest differs from the receipt.")

    receipt_assets = [
        _mapping(asset, "release receipt asset")
        for asset in release_assets
        if _mapping(asset, "release asset").get("name") == RECEIPT_ASSET_NAME
    ]
    receipt_file_digest = file_sha256(receipt_path)
    if len(receipt_assets) != 1 or receipt_assets[0].get("size") != receipt_path.stat().st_size:
        raise ReleaseEvidenceError("Published receipt asset size does not match the downloaded receipt.")
    if receipt_assets[0].get("digest") != f"sha256:{receipt_file_digest}":
        raise ReleaseEvidenceError("Published receipt asset digest does not match the downloaded receipt.")
    live_appcast_sha256 = hashlib.sha256(live_appcast_path.read_bytes()).hexdigest()
    appcast = _mapping(receipt.get("appcast"), "receipt appcast")
    if live_appcast_sha256 != appcast.get("live_pages_sha256"):
        raise ReleaseEvidenceError("Live Pages appcast does not match the verified release appcast.")

    return {
        "published_at": published_at,
        "receipt_asset_id": _integer(receipt_assets[0].get("id"), "receipt asset id"),
        "receipt_file_sha256": receipt_file_digest,
        "live_appcast_sha256": live_appcast_sha256,
        "original_workflow_conclusion": workflow_conclusion,
        "recovery_workflow_run_id": recovery_workflow_run_id,
    }


def _merge_unique_record(records: list[dict[str, Any]], record: dict[str, Any], key: str) -> None:
    matches = [existing for existing in records if existing.get(key) == record[key]]
    if not matches:
        records.append(record)
        return
    if len(matches) != 1 or matches[0] != record:
        raise ReleaseEvidenceError(f"Existing immutable record {record[key]!r} conflicts with this publication.")


def _update_qualification(repo_root: Path, receipt: Mapping[str, Any]) -> Path:
    release = _mapping(receipt.get("release"), "receipt release")
    tag = _string(release.get("tag"), "release tag")
    candidates: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted((repo_root / "docs" / "qualification").glob("*-signed-qualification-v1.json")):
        document = dict(_load_json(path, "signed qualification"))
        candidate = document.get("candidate")
        if isinstance(candidate, Mapping) and candidate.get("release_tag") == tag:
            candidates.append((path, document))
    if len(candidates) != 1:
        raise ReleaseEvidenceError(
            f"Expected exactly one signed qualification record for {tag}; found {len(candidates)}."
        )
    path, document = candidates[0]
    candidate = dict(_mapping(document.get("candidate"), "qualification candidate"))
    updates = {
        "appcast_sha256": _artifact(receipt, "appcast")["sha256"],
        "dmg_sha256": _artifact(receipt, "dmg")["sha256"],
        "release_id": release["id"],
        "release_run_id": _mapping(receipt.get("workflow"), "receipt workflow")["run_id"],
        "signed_app_tree_sha256": receipt["signed_app_tree_sha256"],
        "source_git_sha": receipt["source_sha"],
    }
    for field, value in updates.items():
        if field not in candidate:
            continue
        if candidate[field] not in (None, value):
            raise ReleaseEvidenceError(f"Qualification field candidate.{field} conflicts with immutable receipt data.")
        candidate[field] = value
    document["candidate"] = candidate
    _write_json(path, document)
    return path


def _update_cut_packet(repo_root: Path, receipt: Mapping[str, Any], publication: Mapping[str, Any]) -> Path:
    versions = _mapping(receipt.get("versions"), "receipt versions")
    path = repo_root / "docs" / f"{_string(versions.get('public'), 'public version')}-cut-packet.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ReleaseEvidenceError(f"Unable to read release cut packet at {path}: {error}") from error
    pending = "**Prepared metadata; publication pending.**"
    recovery_pending = "**Published and immutable; PyPI recovery pending.**"
    published = "**Published and immutable.**"
    if pending in text:
        text = text.replace(pending, published, 1)
    elif recovery_pending in text:
        text = text.replace(recovery_pending, published, 1)
    elif published not in text:
        raise ReleaseEvidenceError("Release cut packet has no recognized publication state.")
    marker = "<!-- release-evidence-receipt-v1 -->"
    release = _mapping(receipt.get("release"), "receipt release")
    workflow = _mapping(receipt.get("workflow"), "receipt workflow")
    block = (
        f"\n\n{marker}\n"
        "## Immutable Publication Receipt\n\n"
        f"- Source SHA: `{receipt['source_sha']}`\n"
        f"- Release workflow run: `{workflow['run_id']}` attempt `{workflow['run_attempt']}`\n"
        f"- GitHub release ID: `{release['id']}`\n"
        f"- Published at: `{publication['published_at']}`\n"
        f"- DMG SHA-256: `{_artifact(receipt, 'dmg')['sha256']}`\n"
        f"- Signed app-tree SHA-256: `{receipt['signed_app_tree_sha256']}`\n"
        f"- Appcast SHA-256: `{_artifact(receipt, 'appcast')['sha256']}`\n"
        f"- Receipt file SHA-256: `{publication['receipt_file_sha256']}`\n"
    )
    if marker in text:
        prefix = text.split(marker, 1)[0].rstrip()
        text = prefix + block
    else:
        text = text.rstrip() + block
    path.write_text(text.rstrip() + "\n", encoding="utf-8")
    return path


def reconcile(
    repo_root: Path,
    workflow_run: Mapping[str, Any],
    release: Mapping[str, Any],
    receipt: Mapping[str, Any],
    receipt_path: Path,
    live_appcast_path: Path,
    recovery_workflow_run: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    publication = validate_publication(
        workflow_run,
        release,
        receipt,
        receipt_path,
        live_appcast_path,
        recovery_workflow_run,
    )
    release_data = _mapping(receipt.get("release"), "receipt release")
    workflow = _mapping(receipt.get("workflow"), "receipt workflow")
    tag = _string(release_data.get("tag"), "release tag")
    evidence_directory = repo_root / "docs" / "release-evidence" / tag
    checked_receipt = evidence_directory / RECEIPT_ASSET_NAME
    if checked_receipt.exists() and checked_receipt.read_bytes() != receipt_path.read_bytes():
        raise ReleaseEvidenceError(f"Checked receipt for immutable release {tag} conflicts with the published asset.")
    checked_receipt.parent.mkdir(parents=True, exist_ok=True)
    checked_receipt.write_bytes(receipt_path.read_bytes())
    recovery_workflow_run = (
        {
            "operation": "pypi_recovery",
            "workflow_conclusion": "success",
            "workflow_run_id": publication["recovery_workflow_run_id"],
        }
        if publication["recovery_workflow_run_id"] is not None
        else None
    )

    publication_record = {
        "live_pages": {
            "sha256": publication["live_appcast_sha256"],
            "state": "verified",
            "url": _mapping(receipt.get("appcast"), "receipt appcast")["live_pages_url"],
        },
        "published_at": publication["published_at"],
        "receipt_asset_id": publication["receipt_asset_id"],
        "receipt_file_sha256": publication["receipt_file_sha256"],
        "release_id": release_data["id"],
        "release_tag": tag,
        "schema_version": 1,
        "source_sha": receipt["source_sha"],
        "workflow_conclusion": publication["original_workflow_conclusion"],
        "workflow_run_id": workflow["run_id"],
    }
    if recovery_workflow_run is not None:
        publication_record["recovery_workflow_run"] = recovery_workflow_run
    publication_path = evidence_directory / "publication-record.json"
    if publication_path.exists() and _load_json(publication_path, "publication record") != publication_record:
        raise ReleaseEvidenceError(f"Checked publication record for immutable release {tag} conflicts.")
    _write_json(publication_path, publication_record)

    ledger_path = repo_root / RELEASE_LEDGER_PATH
    ledger = (
        dict(_load_json(ledger_path, "release evidence ledger"))
        if ledger_path.exists()
        else {"schema_version": 1, "releases": []}
    )
    if ledger.get("schema_version") != 1:
        raise ReleaseEvidenceError("Release evidence ledger schema_version must be 1.")
    releases = [
        dict(_mapping(item, "release ledger record"))
        for item in _sequence(ledger.get("releases"), "release ledger releases")
    ]
    ledger_record = {
        "publication_record": publication_path.relative_to(repo_root).as_posix(),
        "published_at": publication["published_at"],
        "receipt": checked_receipt.relative_to(repo_root).as_posix(),
        "receipt_file_sha256": publication["receipt_file_sha256"],
        "release_id": release_data["id"],
        "source_sha": receipt["source_sha"],
        "tag": tag,
        "workflow_run_id": workflow["run_id"],
    }
    if recovery_workflow_run is not None:
        ledger_record["recovery_workflow_run"] = recovery_workflow_run
    _merge_unique_record(releases, ledger_record, "tag")
    ledger["releases"] = sorted(releases, key=lambda item: cast(str, item["tag"]))
    _write_json(ledger_path, ledger)

    evidence_path = repo_root / EVIDENCE_INDEX_PATH
    evidence = (
        dict(_load_json(evidence_path, "release qualification evidence"))
        if evidence_path.exists()
        else {"schema_version": 1, "receipts": []}
    )
    if evidence.get("schema_version") != 1:
        raise ReleaseEvidenceError("Release qualification evidence schema_version must be 1.")
    receipts = [
        dict(_mapping(item, "qualification evidence receipt"))
        for item in _sequence(evidence.get("receipts"), "qualification evidence receipts")
    ]
    reference = checked_receipt.relative_to(repo_root).as_posix()
    for case_id in _sequence(receipt.get("tier1_case_references"), "Tier 1 case references"):
        case = _string(case_id, "Tier 1 case ID")
        evidence_record = {
            "accepted_at": publication["published_at"],
            "case_id": case,
            "receipt_id": f"{tag}:{case}",
            "reference": reference,
            "release_run_id": workflow["run_id"],
            "sha256": publication["receipt_file_sha256"],
            "source": "release_run_receipt",
            "source_sha": receipt["source_sha"],
            "status": "accepted",
            "workflow_conclusion": publication["original_workflow_conclusion"],
        }
        if recovery_workflow_run is not None:
            evidence_record["recovery_workflow_run"] = recovery_workflow_run
        _merge_unique_record(receipts, evidence_record, "receipt_id")
    evidence["receipts"] = sorted(receipts, key=lambda item: cast(str, item["receipt_id"]))
    _write_json(evidence_path, evidence)

    qualification_path = _update_qualification(repo_root, receipt)
    cut_packet_path = _update_cut_packet(repo_root, receipt, publication)
    return {
        "cut_packet": cut_packet_path.relative_to(repo_root).as_posix(),
        "evidence_index": EVIDENCE_INDEX_PATH.as_posix(),
        "publication_record": publication_path.relative_to(repo_root).as_posix(),
        "qualification": qualification_path.relative_to(repo_root).as_posix(),
        "receipt": reference,
        "receipt_file_sha256": publication["receipt_file_sha256"],
        "release_ledger": RELEASE_LEDGER_PATH.as_posix(),
        "release_run_id": workflow["run_id"],
        "release_tag": tag,
        "source_sha": receipt["source_sha"],
    }


def _write_github_output(path: Path, outputs: Mapping[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for key, value in outputs.items():
            handle.write(f"{key}={value}\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a published release receipt and prepare checked evidence.")
    parser.add_argument("--workflow-run", type=Path, required=True)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--live-appcast", type=Path, required=True)
    parser.add_argument("--recovery-workflow-run", type=Path)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args(argv)
    try:
        outputs = reconcile(
            args.repo_root.resolve(),
            _load_json(args.workflow_run, "workflow run"),
            _load_json(args.release, "published release"),
            _load_json(args.receipt, "release receipt"),
            args.receipt,
            args.live_appcast,
            (
                _load_json(args.recovery_workflow_run, "recovery workflow run")
                if args.recovery_workflow_run is not None
                else None
            ),
        )
        if args.github_output is not None:
            _write_github_output(args.github_output, outputs)
        else:
            print(json.dumps(outputs, sort_keys=True))
    except ReleaseEvidenceError as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
