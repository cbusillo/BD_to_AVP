from __future__ import annotations

import hashlib
import io
import json
import re
import stat
import subprocess
import zipfile

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, cast

from scripts.qualify_release_scope import (
    QualificationScopeError,
    git_checked_reference_content,
    validate_policy,
)
from scripts.release_evidence import ReleaseEvidenceError, _merge_unique_record, effective_successful_workflow_run_id
from scripts.release_receipt import ReleaseReceiptError, validate_receipt as validate_release_receipt
from scripts.signed_artifact_receipt import (
    PROFILE_CASE_ID,
    SignedArtifactReceiptError,
    release_expectation_from_receipt,
    validate_receipt as validate_signed_ui_receipt,
)
from scripts.tier3_receipt import Tier3ReceiptError, load_validated_receipt_bytes


PLAN_TYPE = "bd_to_avp.release_qualification_reconciliation_plan"
SCHEMA_VERSION = 1
REPOSITORY = "cbusillo/BD_to_AVP"
EVIDENCE_INDEX_PATH = "docs/qualification/release-evidence-v1.json"
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_TOTAL_UNCOMPRESSED_BYTES = 48 * 1024 * 1024
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_PNG_BYTES = 8 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 24
MAX_COMPRESSION_RATIO = 200
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
ARCHIVE_PATH_PATTERN = re.compile(r"^[a-z0-9][a-z0-9./-]*$")
CASE_IDS = ("clean-machine-signed-update", "installed-ui-accessibility")
SPARKLE_CASE_ID = "sparkle-update-route"
SPARKLE_ROUTES = {"alpha", "beta", "rc", "stable"}
TOP_LEVEL_FILES = frozenset(
    {
        "clean-machine-signed-update.json",
        "installed-ui-accessibility.json",
        "qualification-run.json",
        "signed-artifact-ui-receipt.json",
    }
)
EVIDENCE_FILENAMES = {
    "accessibility-tree": "accessibility-tree.json",
    "cleanup": "cleanup.json",
    "install-log": "install-log.json",
    "package-smoke": "package-smoke.json",
    "profile-snapshot": "profile-snapshot.json",
    "screenshot-dark": "screenshot-dark.png",
    "screenshot-light": "screenshot-light.png",
    "sparkle-update": "sparkle-update.json",
    "ui-result": "ui-result.json",
}
EVIDENCE_PREFIX = "clean-machine-signed-update-evidence/"
EXPECTED_ARCHIVE_FILES = TOP_LEVEL_FILES | frozenset(
    EVIDENCE_PREFIX + filename for filename in EVIDENCE_FILENAMES.values()
)


class QualificationArtifactError(RuntimeError):
    pass


class QualificationArtifactSafetyError(QualificationArtifactError):
    pass


@dataclass(frozen=True)
class ReconciliationFile:
    path: str
    content: bytes


@dataclass(frozen=True)
class ReconciliationBundle:
    plan: dict[str, object]
    files: tuple[ReconciliationFile, ...]


class QualificationIdentity(Protocol):
    release_tag: str
    candidate_sha: str
    manifest_sha256: str
    runner_sha: str
    main_sha: str
    evidence_ref: str
    evidence_sha: str
    release_receipt_file_sha256: str
    signed_ui_artifact_id: int
    signed_ui_artifact_sha256: str

    @property
    def digest(self) -> str:
        raise NotImplementedError


class GitHubArtifactAPI(Protocol):
    def get_json(self, endpoint: str, *, active_auth: bool = False) -> object:
        raise NotImplementedError

    def get_bytes(
        self,
        endpoint: str,
        *,
        active_auth: bool = False,
        max_bytes: int,
        timeout_seconds: float,
    ) -> bytes:
        raise NotImplementedError


def _mapping(value: object, description: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise QualificationArtifactError(f"{description} must be a JSON object.")
    return cast(Mapping[str, Any], value)


def _sequence(value: object, description: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise QualificationArtifactError(f"{description} must be a JSON array.")
    return value


def _string(value: object, description: str) -> str:
    if not isinstance(value, str) or not value:
        raise QualificationArtifactError(f"{description} must be a non-empty string.")
    return value


def _integer(value: object, description: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise QualificationArtifactError(f"{description} must be a positive integer.")
    return value


def _sha(value: object, description: str) -> str:
    text = _string(value, description)
    if SHA_PATTERN.fullmatch(text) is None:
        raise QualificationArtifactError(f"{description} must be a full lowercase Git SHA.")
    return text


def _sha256(value: object, description: str) -> str:
    text = _string(value, description)
    if SHA256_PATTERN.fullmatch(text) is None:
        raise QualificationArtifactError(f"{description} must be a lowercase SHA-256 digest.")
    return text


def _exact_keys(value: Mapping[str, Any], expected: set[str], description: str) -> None:
    actual = set(value)
    if actual != expected:
        raise QualificationArtifactSafetyError(
            f"{description} keys changed; missing={sorted(expected - actual)!r}, extra={sorted(actual - expected)!r}."
        )


def _canonical_json_bytes(payload: Mapping[str, object]) -> bytes:
    return (json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n").encode()


def _pretty_json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()


def _load_json_bytes(content: bytes, description: str, *, max_bytes: int = MAX_JSON_BYTES) -> Mapping[str, Any]:
    if len(content) > max_bytes:
        raise QualificationArtifactSafetyError(f"{description} exceeds the {max_bytes}-byte limit.")
    try:
        return _mapping(json.loads(content), description)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise QualificationArtifactSafetyError(f"{description} is not valid UTF-8 JSON.") from error


def _checked_file(repo_root: Path, relative_path: str) -> bytes | None:
    path = PurePosixPath(relative_path)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != relative_path:
        raise QualificationArtifactSafetyError("Checked evidence path is not canonical.")
    listing = subprocess.run(
        ["git", "ls-tree", "--name-only", "HEAD", "--", relative_path],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if listing.returncode != 0:
        raise QualificationArtifactError("Unable to inspect checked evidence paths.")
    if listing.stdout.strip() != relative_path:
        return None
    content = subprocess.run(
        ["git", "show", f"HEAD:{relative_path}"],
        cwd=repo_root,
        capture_output=True,
        check=False,
    )
    if content.returncode != 0:
        raise QualificationArtifactError("Unable to read checked evidence content.")
    return content.stdout


def _json_at_revision(repo_root: Path, revision: str, relative_path: str, description: str) -> Mapping[str, Any]:
    result = subprocess.run(
        ["git", "show", f"{revision}:{relative_path}"],
        cwd=repo_root,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise QualificationArtifactSafetyError(f"Unable to read runner-bound {description}.")
    return _load_json_bytes(result.stdout, f"runner-bound {description}")


def _parse_timestamp(value: object, description: str) -> datetime:
    text = _string(value, description)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise QualificationArtifactError(f"{description} is invalid.") from error
    if parsed.tzinfo is None:
        raise QualificationArtifactError(f"{description} must include a timezone.")
    return parsed.astimezone(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _archive_entry_limit(name: str) -> int:
    return MAX_PNG_BYTES if name.endswith(".png") else MAX_JSON_BYTES


def _admit_archive(archive_bytes: bytes) -> dict[str, bytes]:
    if not archive_bytes:
        raise QualificationArtifactError("Milestone Qualification artifact download was empty.")
    if len(archive_bytes) > MAX_ARCHIVE_BYTES:
        raise QualificationArtifactSafetyError(
            f"Milestone Qualification artifact exceeds the {MAX_ARCHIVE_BYTES}-byte archive limit."
        )
    try:
        archive = zipfile.ZipFile(io.BytesIO(archive_bytes))
    except zipfile.BadZipFile as error:
        raise QualificationArtifactSafetyError(
            "Milestone Qualification artifact is not a valid ZIP archive."
        ) from error
    with archive:
        if archive.comment:
            raise QualificationArtifactSafetyError("Milestone Qualification artifact ZIP comment is not allowed.")
        members = archive.infolist()
        if len(members) > MAX_ARCHIVE_ENTRIES:
            raise QualificationArtifactSafetyError("Milestone Qualification artifact contains too many ZIP entries.")
        names = [member.filename for member in members]
        if len(names) != len(set(names)):
            raise QualificationArtifactSafetyError("Milestone Qualification artifact contains duplicate ZIP entries.")
        admitted: dict[str, bytes] = {}
        total_size = 0
        for member in members:
            name = member.filename
            path = PurePosixPath(name.rstrip("/"))
            if (
                not name
                or "\\" in name
                or name.startswith("/")
                or "//" in name
                or any(part in {"", ".", ".."} for part in path.parts)
                or ARCHIVE_PATH_PATTERN.fullmatch(name) is None
            ):
                raise QualificationArtifactSafetyError("Milestone Qualification artifact contains an unsafe ZIP path.")
            mode = (member.external_attr >> 16) & 0o177777
            file_type = stat.S_IFMT(mode)
            if stat.S_ISLNK(mode) or file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
                raise QualificationArtifactSafetyError(
                    "Milestone Qualification artifact contains a non-regular ZIP entry."
                )
            if member.flag_bits & 0x1:
                raise QualificationArtifactSafetyError(
                    "Milestone Qualification artifact contains an encrypted ZIP entry."
                )
            if member.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                raise QualificationArtifactSafetyError(
                    "Milestone Qualification artifact uses an unsupported compression type."
                )
            if member.is_dir():
                if member.file_size:
                    raise QualificationArtifactSafetyError(
                        "Milestone Qualification artifact directory entry contains data."
                    )
                if name != EVIDENCE_PREFIX:
                    raise QualificationArtifactSafetyError(
                        "Milestone Qualification artifact contains an unexpected directory entry."
                    )
                continue
            if name not in EXPECTED_ARCHIVE_FILES:
                raise QualificationArtifactSafetyError(
                    f"Milestone Qualification artifact contains unexpected file {name!r}."
                )
            limit = _archive_entry_limit(name)
            if member.file_size > limit:
                raise QualificationArtifactSafetyError(
                    f"Milestone Qualification artifact entry {name!r} exceeds its size limit."
                )
            if member.file_size / max(member.compress_size, 1) > MAX_COMPRESSION_RATIO:
                raise QualificationArtifactSafetyError(
                    f"Milestone Qualification artifact entry {name!r} exceeds the compression-ratio limit."
                )
            total_size += member.file_size
            if total_size > MAX_TOTAL_UNCOMPRESSED_BYTES:
                raise QualificationArtifactSafetyError(
                    "Milestone Qualification artifact exceeds the total uncompressed size limit."
                )
            try:
                with archive.open(member) as source:
                    content = source.read(limit + 1)
            except (RuntimeError, zipfile.BadZipFile) as error:
                raise QualificationArtifactSafetyError(
                    f"Unable to read Milestone Qualification artifact entry {name!r}."
                ) from error
            if len(content) != member.file_size or len(content) > limit:
                raise QualificationArtifactSafetyError(
                    f"Milestone Qualification artifact entry {name!r} size changed during admission."
                )
            admitted[name] = content
        missing = sorted(EXPECTED_ARCHIVE_FILES - set(admitted))
        if missing:
            raise QualificationArtifactSafetyError(
                f"Milestone Qualification artifact is missing required files: {missing!r}."
            )
        return admitted


def _validate_qualification_run(
    payload: Mapping[str, Any],
    *,
    identity: QualificationIdentity,
    run: Mapping[str, Any],
    manifest: Mapping[str, Any],
    receipt_digests: Mapping[str, str],
) -> None:
    _exact_keys(
        payload,
        {
            "candidate_tag",
            "evidence_ref",
            "evidence_sha",
            "manifest_sha256",
            "prior_tag",
            "route",
            "signed_ui_artifact",
            "tier3_receipts",
        },
        "qualification-run.json",
    )
    prior = _mapping(manifest.get("prior"), "manifest prior")
    release = _mapping(manifest.get("release"), "manifest release")
    if payload.get("candidate_tag") != identity.release_tag:
        raise QualificationArtifactSafetyError("Qualification artifact release tag conflicts with resume identity.")
    if payload.get("evidence_ref") != identity.evidence_ref or payload.get("evidence_sha") != identity.evidence_sha:
        raise QualificationArtifactSafetyError("Qualification artifact evidence branch identity conflicts.")
    if payload.get("manifest_sha256") != identity.manifest_sha256:
        raise QualificationArtifactSafetyError("Qualification artifact manifest digest conflicts.")
    if payload.get("prior_tag") != prior.get("release_tag") or payload.get("route") != release.get("sparkle_route"):
        raise QualificationArtifactSafetyError("Qualification artifact release route identity conflicts.")
    signed_ui = _mapping(payload.get("signed_ui_artifact"), "qualification-run signed UI artifact")
    _exact_keys(signed_ui, {"digest", "id", "receipt_sha256"}, "qualification-run signed UI artifact")
    if signed_ui.get("id") != identity.signed_ui_artifact_id:
        raise QualificationArtifactSafetyError("Qualification artifact signed UI artifact ID conflicts.")
    if signed_ui.get("digest") != f"sha256:{identity.signed_ui_artifact_sha256}":
        raise QualificationArtifactSafetyError("Qualification artifact signed UI artifact digest conflicts.")
    manifest_signed_ui = _mapping(manifest.get("signed_ui_artifact"), "manifest signed UI artifact")
    if signed_ui.get("receipt_sha256") != manifest_signed_ui.get("receipt_sha256"):
        raise QualificationArtifactSafetyError("Qualification artifact signed UI receipt digest conflicts.")
    raw_receipts = _sequence(payload.get("tier3_receipts"), "qualification-run Tier 3 receipts")
    if len(raw_receipts) != len(CASE_IDS):
        raise QualificationArtifactSafetyError("Qualification artifact Tier 3 receipt set changed.")
    observed: dict[str, str] = {}
    for index, raw_receipt in enumerate(raw_receipts):
        receipt = _mapping(raw_receipt, f"qualification-run Tier 3 receipt {index}")
        _exact_keys(receipt, {"case_id", "receipt_sha256"}, f"qualification-run Tier 3 receipt {index}")
        case_id = _string(receipt.get("case_id"), f"qualification-run Tier 3 receipt {index} case ID")
        if case_id in observed:
            raise QualificationArtifactSafetyError("Qualification artifact repeats a Tier 3 case ID.")
        observed[case_id] = _sha256(
            receipt.get("receipt_sha256"),
            f"qualification-run Tier 3 receipt {index} digest",
        )
    if observed != dict(receipt_digests):
        raise QualificationArtifactSafetyError("Qualification artifact Tier 3 receipt digests conflict.")
    _integer(run.get("id"), "workflow run ID")
    _integer(run.get("run_attempt"), "workflow run attempt")


def _validate_receipts(
    entries: Mapping[str, bytes],
    *,
    repo_root: Path,
    identity: QualificationIdentity,
    manifest: Mapping[str, Any],
) -> tuple[list[dict[str, object]], dict[str, Mapping[str, Any]], dict[str, str]]:
    paths = _mapping(manifest.get("paths"), "manifest paths")
    policy_path = _string(paths.get("policy"), "manifest policy path")
    try:
        policy = validate_policy(
            _json_at_revision(repo_root, identity.runner_sha, policy_path, "release qualification policy")
        )
    except QualificationScopeError as error:
        raise QualificationArtifactSafetyError(str(error)) from error
    policy_id = _string(policy.get("policy_id"), "release qualification policy ID")
    cases = {
        _string(case.get("id"), "qualification case ID"): case
        for case in (
            _mapping(item, "release qualification case")
            for item in _sequence(policy.get("cases"), "release qualification cases")
        )
    }
    reference_content = git_checked_reference_content(repo_root)
    validated: dict[str, Mapping[str, Any]] = {}
    digests: dict[str, str] = {}
    summaries: list[dict[str, object]] = []
    for case_id in CASE_IDS:
        case = cases.get(case_id)
        if case is None:
            raise QualificationArtifactSafetyError(f"Runner policy is missing qualification case {case_id!r}.")
        content = entries[f"{case_id}.json"]
        if content != _pretty_json_bytes(_load_json_bytes(content, f"{case_id} receipt")):
            raise QualificationArtifactSafetyError(f"Qualification receipt {case_id!r} is not canonical JSON.")
        try:
            receipt = load_validated_receipt_bytes(
                content,
                policy_id=policy_id,
                case=case,
                reference_content=reference_content,
            )
        except (QualificationScopeError, Tier3ReceiptError) as error:
            raise QualificationArtifactSafetyError(f"Qualification receipt {case_id!r} is invalid: {error}") from error
        result = _mapping(receipt.get("result"), f"{case_id} result")
        if result.get("status") != "passed":
            raise QualificationArtifactSafetyError(f"Qualification receipt {case_id!r} did not pass.")
        release_identity = _mapping(receipt.get("release_identity"), f"{case_id} release identity")
        release_receipt = _mapping(release_identity.get("release_receipt"), f"{case_id} release receipt")
        if release_identity.get("source_sha") != identity.candidate_sha:
            raise QualificationArtifactSafetyError(f"Qualification receipt {case_id!r} source SHA conflicts.")
        if release_identity.get("release_tag") != identity.release_tag:
            raise QualificationArtifactSafetyError(f"Qualification receipt {case_id!r} release tag conflicts.")
        if release_receipt.get("file_sha256") != identity.release_receipt_file_sha256:
            raise QualificationArtifactSafetyError(f"Qualification receipt {case_id!r} release receipt conflicts.")
        receipt_digest = _sha256(receipt.get("receipt_sha256"), f"{case_id} receipt self digest")
        evidence_summaries: list[dict[str, str]] = []
        for raw_evidence in _sequence(receipt.get("evidence"), f"{case_id} evidence"):
            evidence = _mapping(raw_evidence, f"{case_id} evidence record")
            evidence_summaries.append(
                {
                    "kind": _string(evidence.get("kind"), f"{case_id} evidence kind"),
                    "sha256": _sha256(evidence.get("sha256"), f"{case_id} evidence digest"),
                }
            )
        timestamps = _mapping(receipt.get("timestamps"), f"{case_id} timestamps")
        validated[case_id] = receipt
        digests[case_id] = receipt_digest
        summaries.append(
            {
                "case_id": case_id,
                "completed_at": _string(timestamps.get("completed_at"), f"{case_id} completed_at"),
                "evidence_digests": sorted(evidence_summaries, key=lambda item: item["kind"]),
                "receipt_sha256": receipt_digest,
            }
        )
    return summaries, validated, digests


def _validate_evidence_files(entries: Mapping[str, bytes], receipts: Mapping[str, Mapping[str, Any]]) -> None:
    referenced: dict[str, str] = {}
    for case_id in CASE_IDS:
        for raw_evidence in _sequence(receipts[case_id].get("evidence"), f"{case_id} evidence"):
            evidence = _mapping(raw_evidence, f"{case_id} evidence record")
            kind = _string(evidence.get("kind"), f"{case_id} evidence kind")
            digest = _sha256(evidence.get("sha256"), f"{case_id} evidence digest")
            if kind in referenced and referenced[kind] != digest:
                raise QualificationArtifactSafetyError("Qualification receipts disagree about an evidence digest.")
            referenced[kind] = digest
    if set(referenced) != set(EVIDENCE_FILENAMES):
        raise QualificationArtifactSafetyError("Qualification artifact evidence set changed.")
    for kind, filename in EVIDENCE_FILENAMES.items():
        observed = hashlib.sha256(entries[EVIDENCE_PREFIX + filename]).hexdigest()
        if observed != referenced[kind]:
            raise QualificationArtifactSafetyError(f"Qualification artifact evidence digest conflicts for {kind!r}.")
    clean_cleanup = _mapping(receipts["clean-machine-signed-update"].get("cleanup"), "clean-machine cleanup")
    if clean_cleanup.get("evidence_sha256") != referenced["cleanup"]:
        raise QualificationArtifactSafetyError("Clean-machine cleanup evidence digest conflicts.")


def _plan_file_operation(repo_root: Path, path: str, content: bytes) -> dict[str, object]:
    existing = _checked_file(repo_root, path)
    if existing is None:
        state = "create"
    elif existing == content:
        state = "identical"
    else:
        raise QualificationArtifactSafetyError(f"Checked qualification receipt {path!r} conflicts.")
    return {
        "kind": "write_file",
        "path": path,
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
        "state": state,
    }


def _validated_release_receipt(
    repo_root: Path,
    *,
    identity: QualificationIdentity,
    manifest: Mapping[str, Any],
) -> Mapping[str, Any]:
    manifest_receipt = _mapping(manifest.get("release_receipt"), "manifest release receipt")
    expected_path = f"docs/release-evidence/{identity.release_tag}/release-receipt.json"
    receipt_path = _string(manifest_receipt.get("path"), "manifest release receipt path")
    if receipt_path != expected_path:
        raise QualificationArtifactSafetyError("Manifest release receipt path conflicts with the candidate tag.")
    content = _checked_file(repo_root, receipt_path)
    if content is None:
        raise QualificationArtifactSafetyError("Checked release receipt is missing.")
    digest = hashlib.sha256(content).hexdigest()
    if digest != identity.release_receipt_file_sha256 or digest != manifest_receipt.get("file_sha256"):
        raise QualificationArtifactSafetyError("Checked release receipt file digest conflicts.")
    receipt = _load_json_bytes(content, "checked release receipt")
    try:
        validate_release_receipt(receipt)
    except ReleaseReceiptError as error:
        raise QualificationArtifactSafetyError(f"Checked release receipt is invalid: {error}") from error
    release = _mapping(receipt.get("release"), "checked release receipt release")
    manifest_release = _mapping(manifest.get("release"), "manifest release")
    if (
        receipt.get("source_sha") != identity.candidate_sha
        or release.get("id") != manifest_release.get("id")
        or release.get("tag") != identity.release_tag
        or receipt.get("receipt_sha256") != manifest_receipt.get("receipt_sha256")
    ):
        raise QualificationArtifactSafetyError("Checked release receipt identity conflicts.")
    return receipt


def _validated_signed_ui_receipt(
    content: bytes,
    *,
    identity: QualificationIdentity,
    manifest: Mapping[str, Any],
    release_receipt: Mapping[str, Any],
    policy_id: str,
) -> Mapping[str, Any]:
    if content != _pretty_json_bytes(_load_json_bytes(content, "signed UI receipt")):
        raise QualificationArtifactSafetyError("Qualification artifact signed UI receipt is not canonical JSON.")
    receipt = _load_json_bytes(content, "signed UI receipt")
    manifest_workflow = _mapping(manifest.get("workflow"), "manifest workflow")
    manifest_release_receipt = _mapping(manifest.get("release_receipt"), "manifest release receipt")
    try:
        expectation = release_expectation_from_receipt(
            release_receipt,
            policy_id=policy_id,
            case_id=PROFILE_CASE_ID,
            workflow_run_id=_integer(manifest_workflow.get("run_id"), "manifest workflow run ID"),
            workflow_run_attempt=_integer(manifest_workflow.get("run_attempt"), "manifest workflow run attempt"),
            release_receipt_asset_id=_integer(
                manifest_release_receipt.get("asset_id"),
                "manifest release receipt asset ID",
            ),
            release_receipt_file_sha256=identity.release_receipt_file_sha256,
        )
        validate_signed_ui_receipt(receipt, expectation)
    except SignedArtifactReceiptError as error:
        raise QualificationArtifactSafetyError(
            f"Qualification artifact signed UI receipt is invalid: {error}"
        ) from error
    return receipt


def _single_release_artifact(release_receipt: Mapping[str, Any], kind: str) -> Mapping[str, Any]:
    artifacts = [
        _mapping(item, "release receipt artifact")
        for item in _sequence(release_receipt.get("artifacts"), "release receipt artifacts")
    ]
    matches = [artifact for artifact in artifacts if artifact.get("kind") == kind]
    if len(matches) != 1:
        raise QualificationArtifactSafetyError(f"Checked release receipt requires exactly one {kind} artifact.")
    return matches[0]


def _validate_live_publication_record(
    repo_root: Path,
    *,
    identity: QualificationIdentity,
    release_receipt: Mapping[str, Any],
    accepted_at: str,
) -> None:
    publication_path = f"docs/release-evidence/{identity.release_tag}/publication-record.json"
    content = _checked_file(repo_root, publication_path)
    if content is None:
        raise QualificationArtifactSafetyError("Checked live-publication record is missing.")
    publication = _load_json_bytes(content, "checked live-publication record")
    release = _mapping(release_receipt.get("release"), "release receipt release")
    workflow = _mapping(release_receipt.get("workflow"), "release receipt workflow")
    expected = {
        "schema_version": 1,
        "release_tag": identity.release_tag,
        "release_id": release["id"],
        "source_sha": identity.candidate_sha,
        "workflow_run_id": workflow["run_id"],
        "receipt_file_sha256": identity.release_receipt_file_sha256,
    }
    if any(publication.get(field) != value for field, value in expected.items()):
        raise QualificationArtifactSafetyError("Checked live-publication record identity conflicts.")
    if effective_successful_workflow_run_id(publication) is None:
        raise QualificationArtifactSafetyError("Checked live-publication record does not prove a successful release.")
    appcast = _single_release_artifact(release_receipt, "appcast")
    live_pages = _mapping(publication.get("live_pages"), "checked live-publication live_pages")
    if (
        live_pages.get("state") != "verified"
        or live_pages.get("sha256") != appcast.get("sha256")
        or live_pages.get("url") != "https://cbusillo.github.io/BD_to_AVP/appcast.xml"
    ):
        raise QualificationArtifactSafetyError("Checked live-publication record does not match the live appcast.")
    published_at = _parse_timestamp(publication.get("published_at"), "publication published_at")
    if _parse_timestamp(accepted_at, "qualification accepted_at") < published_at:
        raise QualificationArtifactSafetyError("Milestone qualification completed before live publication.")


def _build_live_qualification(
    repo_root: Path,
    *,
    identity: QualificationIdentity,
    run: Mapping[str, Any],
    artifact: Mapping[str, Any],
    manifest: Mapping[str, Any],
    entries: Mapping[str, bytes],
    release_receipt: Mapping[str, Any],
    qualification_run: Mapping[str, Any],
    accepted_at: str,
) -> bytes:
    _validate_live_publication_record(
        repo_root,
        identity=identity,
        release_receipt=release_receipt,
        accepted_at=accepted_at,
    )
    route = _string(qualification_run.get("route"), "qualification Sparkle route")
    if route not in SPARKLE_ROUTES:
        raise QualificationArtifactSafetyError("Qualification artifact Sparkle route is unsupported.")
    sparkle_update = _load_json_bytes(
        entries[EVIDENCE_PREFIX + EVIDENCE_FILENAMES["sparkle-update"]],
        "Sparkle update evidence",
    )
    profile_snapshot = _load_json_bytes(
        entries[EVIDENCE_PREFIX + EVIDENCE_FILENAMES["profile-snapshot"]],
        "profile snapshot evidence",
    )
    release = _mapping(release_receipt.get("release"), "release receipt release")
    workflow = _mapping(release_receipt.get("workflow"), "release receipt workflow")
    versions = _mapping(release_receipt.get("versions"), "release receipt versions")
    appcast = _single_release_artifact(release_receipt, "appcast")
    dmg = _single_release_artifact(release_receipt, "dmg")
    signed_app_tree = _sha256(release_receipt.get("signed_app_tree_sha256"), "signed app tree digest")
    sparkle_candidate = _mapping(sparkle_update.get("candidate"), "Sparkle update candidate")
    if (
        sparkle_update.get("status") != "passed"
        or sparkle_update.get("outcome") != "install-and-relaunch"
        or sparkle_update.get("button") != "Install and Relaunch"
        or sparkle_update.get("route") != route
        or sparkle_update.get("feed_sha256") != appcast.get("sha256")
        or sparkle_candidate.get("build_version") != versions.get("build")
        or sparkle_candidate.get("package_version") != versions.get("package")
        or sparkle_candidate.get("signed_app_tree_sha256") != signed_app_tree
    ):
        raise QualificationArtifactSafetyError("Sparkle update evidence does not match the published candidate.")
    required_profile_flags = (
        "profile_encoding_options_preserved",
        "profile_identity_preserved",
        "profile_migration_matched",
        "profile_safe_pipeline_defaults_preserved",
        "route_preserved",
        "sentinel_preserved",
        "unsafe_legacy_run_defaults_removed",
    )
    if (
        any(profile_snapshot.get(flag) is not True for flag in required_profile_flags)
        or profile_snapshot.get("route_after") != route
        or profile_snapshot.get("profile_before_sha256") != profile_snapshot.get("profile_after_sha256")
        or profile_snapshot.get("profile_before_semantic_sha256")
        != profile_snapshot.get("profile_after_semantic_sha256")
    ):
        raise QualificationArtifactSafetyError("Profile snapshot does not prove preservation across the update.")
    clean_receipt_path = f"docs/qualification/{identity.release_tag}-clean-machine-signed-update-v1.json"
    clean_receipt_content = entries["clean-machine-signed-update.json"]
    artifact_digest = _string(artifact.get("digest"), "Milestone Qualification artifact digest")
    if not artifact_digest.startswith("sha256:"):
        raise QualificationArtifactError("Milestone Qualification artifact digest is invalid.")
    qualification_channel = route if release.get("prerelease") is True else "stable"
    document: dict[str, Any] = {
        "candidate": {
            "appcast_sha256": _sha256(appcast.get("sha256"), "appcast digest"),
            "build": _string(versions.get("build"), "candidate build"),
            "dmg_sha256": _sha256(dmg.get("sha256"), "DMG digest"),
            "package_version": _string(versions.get("package"), "candidate package version"),
            "public_version": _string(versions.get("public"), "candidate public version"),
            "release_id": _integer(release.get("id"), "release ID"),
            "release_run_id": _integer(workflow.get("run_id"), "release workflow run ID"),
            "release_tag": identity.release_tag,
            "signed_app_tree_sha256": signed_app_tree,
            "source_sha": identity.candidate_sha,
        },
        "cases": [
            {
                "id": SPARKLE_CASE_ID,
                "observations": {
                    f"candidate_offered_on_{route}_route": True,
                    "install_action": "Install and Relaunch",
                    "post_update_build": _string(versions.get("build"), "candidate build"),
                    "post_update_package_version": _string(
                        versions.get("package"),
                        "candidate package version",
                    ),
                    "post_update_signed_app_tree_sha256": signed_app_tree,
                    "prior_release_tag": _string(qualification_run.get("prior_tag"), "prior release tag"),
                    "profile_preserved": True,
                    "qualification_artifact_digest": artifact_digest,
                    "qualification_artifact_id": _integer(
                        artifact.get("id"),
                        "Milestone Qualification artifact ID",
                    ),
                    "qualification_evidence_sha": identity.evidence_sha,
                    "qualification_manifest_sha256": identity.manifest_sha256,
                    "qualification_receipt_reference": clean_receipt_path,
                    "qualification_receipt_sha256": hashlib.sha256(clean_receipt_content).hexdigest(),
                    "qualification_workflow_run_id": _integer(run.get("id"), "workflow run ID"),
                    "route": route,
                },
                "result": "passed",
            }
        ],
        "qualification_id": f"{qualification_channel}-live-qualification-v1",
        "qualified_at": accepted_at,
        "schema_version": 1,
    }
    return _pretty_json_bytes(document)


def _append_index_record(
    receipts: list[dict[str, object]],
    operations: list[dict[str, object]],
    record: dict[str, object],
) -> None:
    before = len(receipts)
    try:
        _merge_unique_record(receipts, record, "receipt_id")
    except ReleaseEvidenceError as error:
        raise QualificationArtifactSafetyError(str(error)) from error
    operations.append(
        {
            "kind": "append_evidence_record",
            "path": EVIDENCE_INDEX_PATH,
            "receipt_id": record["receipt_id"],
            "record": record,
            "state": "append" if len(receipts) > before else "present",
        }
    )


def _plan_index_operations(
    repo_root: Path,
    *,
    identity: QualificationIdentity,
    run: Mapping[str, Any],
    receipt_contents: Mapping[str, bytes],
    signed_ui_path: str,
    signed_ui_content: bytes,
    signed_ui_receipt: Mapping[str, Any],
    live_path: str,
    live_content: bytes,
    accepted_at: str,
) -> tuple[list[dict[str, object]], bytes]:
    index_content = _checked_file(repo_root, EVIDENCE_INDEX_PATH)
    if index_content is None:
        raise QualificationArtifactSafetyError("Checked release qualification evidence index is missing.")
    index = dict(_load_json_bytes(index_content, "release qualification evidence index"))
    if index.get("schema_version") != 1:
        raise QualificationArtifactSafetyError("Release qualification evidence index schema version changed.")
    receipts = [
        dict(_mapping(item, "release qualification evidence record"))
        for item in _sequence(index.get("receipts"), "release qualification evidence records")
    ]
    run_id = _integer(run.get("id"), "workflow run ID")
    operations: list[dict[str, object]] = []
    for case_id in CASE_IDS:
        destination = f"docs/qualification/{identity.release_tag}-{case_id}-v1.json"
        content = receipt_contents[case_id]
        record: dict[str, object] = {
            "accepted_at": accepted_at,
            "case_id": case_id,
            "receipt_id": f"{identity.release_tag}:{case_id}:{run_id}",
            "reference": destination,
            "sha256": hashlib.sha256(content).hexdigest(),
            "source": "tier3_automation_receipt",
            "source_sha": identity.candidate_sha,
            "status": "accepted",
        }
        _append_index_record(receipts, operations, record)
    signed_ui_workflow = _mapping(signed_ui_receipt.get("workflow"), "signed UI receipt workflow")
    _append_index_record(
        receipts,
        operations,
        {
            "accepted_at": accepted_at,
            "case_id": PROFILE_CASE_ID,
            "receipt_id": (
                f"{identity.release_tag}:{PROFILE_CASE_ID}:"
                f"{_integer(signed_ui_workflow.get('run_id'), 'signed UI workflow run ID')}"
            ),
            "reference": signed_ui_path,
            "sha256": hashlib.sha256(signed_ui_content).hexdigest(),
            "source": "signed_artifact_receipt",
            "source_sha": identity.candidate_sha,
            "status": "accepted",
        },
    )
    _append_index_record(
        receipts,
        operations,
        {
            "accepted_at": accepted_at,
            "case_id": SPARKLE_CASE_ID,
            "receipt_id": f"{identity.release_tag}:{SPARKLE_CASE_ID}:{run_id}",
            "reference": live_path,
            "sha256": hashlib.sha256(live_content).hexdigest(),
            "source": "signed_artifact_receipt",
            "source_sha": identity.candidate_sha,
            "status": "accepted",
        },
    )
    index["receipts"] = receipts
    return operations, _pretty_json_bytes(index)


def plan_reconciliation_bundle(
    *,
    repo_root: Path,
    identity: QualificationIdentity,
    run: Mapping[str, Any],
    artifact: Mapping[str, Any],
    manifest: Mapping[str, Any],
    archive_bytes: bytes,
) -> ReconciliationBundle:
    repo_root = repo_root.resolve()
    entries = _admit_archive(archive_bytes)
    receipt_summaries, receipts, receipt_digests = _validate_receipts(
        entries,
        repo_root=repo_root,
        identity=identity,
        manifest=manifest,
    )
    accepted_at = _format_timestamp(
        max(
            _parse_timestamp(summary["completed_at"], f"{summary['case_id']} receipt completed_at")
            for summary in receipt_summaries
        )
    )
    qualification_run = _load_json_bytes(entries["qualification-run.json"], "qualification-run.json")
    _validate_qualification_run(
        qualification_run,
        identity=identity,
        run=run,
        manifest=manifest,
        receipt_digests=receipt_digests,
    )
    _validate_evidence_files(entries, receipts)
    release_receipt = _validated_release_receipt(
        repo_root,
        identity=identity,
        manifest=manifest,
    )
    manifest_signed_ui = _mapping(manifest.get("signed_ui_artifact"), "manifest signed UI artifact")
    signed_ui_path = _string(manifest_signed_ui.get("receipt_path"), "manifest signed UI receipt path")
    expected_signed_ui_path = f"docs/release-evidence/{identity.release_tag}/signed-artifact-ui-receipt.json"
    if signed_ui_path != expected_signed_ui_path:
        raise QualificationArtifactSafetyError("Manifest signed UI receipt path conflicts with the candidate tag.")
    signed_ui_content = entries["signed-artifact-ui-receipt.json"]
    if hashlib.sha256(signed_ui_content).hexdigest() != manifest_signed_ui.get("receipt_file_sha256"):
        raise QualificationArtifactSafetyError("Qualification artifact signed UI receipt file digest conflicts.")
    checked_signed_ui = _checked_file(repo_root, signed_ui_path)
    if checked_signed_ui is None or checked_signed_ui != signed_ui_content:
        raise QualificationArtifactSafetyError(
            "Qualification artifact signed UI receipt conflicts with checked evidence."
        )
    signed_ui_receipt = _validated_signed_ui_receipt(
        signed_ui_content,
        identity=identity,
        manifest=manifest,
        release_receipt=release_receipt,
        policy_id=_string(receipts[CASE_IDS[0]].get("policy_id"), "qualification policy ID"),
    )
    live_path = f"docs/qualification/{identity.release_tag}-live-qualification-v1.json"
    live_content = _build_live_qualification(
        repo_root,
        identity=identity,
        run=run,
        artifact=artifact,
        manifest=manifest,
        entries=entries,
        release_receipt=release_receipt,
        qualification_run=qualification_run,
        accepted_at=accepted_at,
    )
    operations: list[dict[str, object]] = []
    receipt_contents = {case_id: entries[f"{case_id}.json"] for case_id in CASE_IDS}
    for case_id in CASE_IDS:
        operations.append(
            _plan_file_operation(
                repo_root,
                f"docs/qualification/{identity.release_tag}-{case_id}-v1.json",
                receipt_contents[case_id],
            )
        )
    operations.append(
        _plan_file_operation(
            repo_root,
            live_path,
            live_content,
        )
    )
    operations.append(
        {
            "kind": "verify_file",
            "path": signed_ui_path,
            "sha256": hashlib.sha256(signed_ui_content).hexdigest(),
            "size_bytes": len(signed_ui_content),
            "state": "identical",
        }
    )
    index_operations, index_content = _plan_index_operations(
        repo_root,
        identity=identity,
        run=run,
        receipt_contents=receipt_contents,
        signed_ui_path=signed_ui_path,
        signed_ui_content=signed_ui_content,
        signed_ui_receipt=signed_ui_receipt,
        live_path=live_path,
        live_content=live_content,
        accepted_at=accepted_at,
    )
    operations.extend(index_operations)
    sorted_operations = sorted(
        operations,
        key=lambda item: (
            cast(str, item["kind"]),
            cast(str, item["path"]),
            cast(str, item.get("receipt_id", "")),
        ),
    )
    artifact_digest = _string(artifact.get("digest"), "Milestone Qualification artifact digest")
    if not artifact_digest.startswith("sha256:"):
        raise QualificationArtifactError("Milestone Qualification artifact digest is invalid.")
    materialized_files = {
        **{
            f"docs/qualification/{identity.release_tag}-{case_id}-v1.json": receipt_contents[case_id]
            for case_id in CASE_IDS
        },
        live_path: live_content,
        EVIDENCE_INDEX_PATH: index_content,
    }
    file_states = {
        cast(str, operation["path"]): cast(str, operation["state"])
        for operation in sorted_operations
        if operation["kind"] == "write_file"
    }
    file_states[EVIDENCE_INDEX_PATH] = (
        "append" if any(operation["state"] == "append" for operation in index_operations) else "identical"
    )
    plan: dict[str, object] = {
        "artifact": {
            "id": _integer(artifact.get("id"), "Milestone Qualification artifact ID"),
            "name": _string(artifact.get("name"), "Milestone Qualification artifact name"),
            "run_attempt": _integer(run.get("run_attempt"), "workflow run attempt"),
            "run_id": _integer(run.get("id"), "workflow run ID"),
            "sha256": _sha256(artifact_digest.removeprefix("sha256:"), "Milestone Qualification artifact digest"),
        },
        "candidate_sha": _sha(identity.candidate_sha, "candidate SHA"),
        "cases": sorted(receipt_summaries, key=lambda item: cast(str, item["case_id"])),
        "evidence_ref": identity.evidence_ref,
        "evidence_sha": _sha(identity.evidence_sha, "evidence SHA"),
        "files": [
            {
                "path": path,
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
                "state": file_states[path],
            }
            for path, content in sorted(materialized_files.items())
        ],
        "identity_sha256": _sha256(identity.digest, "resume identity digest"),
        "main_sha": _sha(identity.main_sha, "main SHA"),
        "manifest_sha256": _sha256(identity.manifest_sha256, "manifest digest"),
        "operations": sorted_operations,
        "plan_type": PLAN_TYPE,
        "release_tag": identity.release_tag,
        "requires_changes": any(item["state"] in {"append", "create"} for item in sorted_operations),
        "schema_version": SCHEMA_VERSION,
    }
    plan["plan_sha256"] = hashlib.sha256(_canonical_json_bytes(plan)).hexdigest()
    return ReconciliationBundle(
        plan=plan,
        files=tuple(
            ReconciliationFile(path=path, content=content) for path, content in sorted(materialized_files.items())
        ),
    )


def plan_reconciliation(
    *,
    repo_root: Path,
    identity: QualificationIdentity,
    run: Mapping[str, Any],
    artifact: Mapping[str, Any],
    manifest: Mapping[str, Any],
    archive_bytes: bytes,
) -> dict[str, object]:
    return plan_reconciliation_bundle(
        repo_root=repo_root,
        identity=identity,
        run=run,
        artifact=artifact,
        manifest=manifest,
        archive_bytes=archive_bytes,
    ).plan


def download_reconciliation_bundle(
    *,
    client: GitHubArtifactAPI,
    repo_root: Path,
    identity: QualificationIdentity,
    run: Mapping[str, Any],
    artifact: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> ReconciliationBundle:
    artifact_id = _integer(artifact.get("id"), "Milestone Qualification artifact ID")
    run_id = _integer(run.get("id"), "workflow run ID")
    run_attempt = _integer(run.get("run_attempt"), "workflow run attempt")
    expected_name = f"milestone-qualification-{identity.release_tag}-{run_attempt}"
    metadata = _mapping(
        client.get_json(f"repos/{REPOSITORY}/actions/artifacts/{artifact_id}", active_auth=True),
        "Milestone Qualification artifact metadata",
    )
    workflow_run = _mapping(metadata.get("workflow_run"), "Milestone Qualification artifact workflow run")
    if (
        metadata.get("id") != artifact_id
        or metadata.get("name") != expected_name
        or metadata.get("digest") != artifact.get("digest")
        or metadata.get("expired") is not False
        or workflow_run.get("id") != run_id
        or workflow_run.get("head_branch") != "main"
        or workflow_run.get("head_sha") != identity.runner_sha
    ):
        raise QualificationArtifactSafetyError("Milestone Qualification artifact metadata changed before download.")
    size_in_bytes = _integer(metadata.get("size_in_bytes"), "Milestone Qualification artifact size")
    if size_in_bytes > MAX_ARCHIVE_BYTES:
        raise QualificationArtifactSafetyError(
            f"Milestone Qualification artifact exceeds the {MAX_ARCHIVE_BYTES}-byte archive limit."
        )
    archive_bytes = client.get_bytes(
        f"repos/{REPOSITORY}/actions/artifacts/{artifact_id}/zip",
        active_auth=True,
        max_bytes=MAX_ARCHIVE_BYTES,
        timeout_seconds=300.0,
    )
    expected_digest = _string(artifact.get("digest"), "Milestone Qualification artifact digest")
    if hashlib.sha256(archive_bytes).hexdigest() != expected_digest.removeprefix("sha256:"):
        raise QualificationArtifactSafetyError("Milestone Qualification artifact archive digest conflicts.")
    return plan_reconciliation_bundle(
        repo_root=repo_root,
        identity=identity,
        run=run,
        artifact=artifact,
        manifest=manifest,
        archive_bytes=archive_bytes,
    )


def download_and_plan_reconciliation(
    *,
    client: GitHubArtifactAPI,
    repo_root: Path,
    identity: QualificationIdentity,
    run: Mapping[str, Any],
    artifact: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> dict[str, object]:
    return download_reconciliation_bundle(
        client=client,
        repo_root=repo_root,
        identity=identity,
        run=run,
        artifact=artifact,
        manifest=manifest,
    ).plan


__all__ = [
    "MAX_ARCHIVE_BYTES",
    "PLAN_TYPE",
    "QualificationArtifactError",
    "QualificationArtifactSafetyError",
    "ReconciliationBundle",
    "ReconciliationFile",
    "download_and_plan_reconciliation",
    "download_reconciliation_bundle",
    "plan_reconciliation",
    "plan_reconciliation_bundle",
]
