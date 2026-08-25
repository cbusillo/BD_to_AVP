from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import subprocess
import sys
import tempfile
import zipfile

from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Literal, cast

from scripts.release_receipt import (
    EXPECTED_REPOSITORY,
    ReleaseReceiptError,
    load_validated_checked_receipt,
)
from scripts.release_evidence import (
    ReleaseEvidenceError,
    validate_qualification_record,
)
from scripts.release_milestone_context import (
    ReleaseMilestoneContextError,
    validate_failed_post_publication_qualification_record,
)
from scripts.release_qualification_manifest import (
    ReleaseQualificationManifestError,
    load_validated_manifest,
    manifest_sha256,
)
from scripts.release_workflow_policy import REQUIRED_ACTOR
from scripts.signed_artifact_receipt import (
    MAX_RECEIPT_BYTES,
    PROFILE_CASE_ID,
    SignedArtifactReceiptError,
    load_validated_receipt,
    release_expectation_from_receipt,
    validate_policy_case,
)
from scripts.tier3_receipt import Tier3ReceiptError, load_validated_receipt_bytes


REPO_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_ROOT = PurePosixPath("docs/release-evidence")
CAPTURE_NAME = "capture-v2.json"
QUALIFICATION_NAME = "qualification-v2.json"
DISPOSITION_NAME = "disposition-v2.json"
RECEIPT_NAME = "release-receipt.json"
SIGNED_UI_ARCHIVE_NAME = "signed-artifact-ui.zip"
SIGNED_UI_RECEIPT_NAME = "signed-artifact-ui-receipt.json"
QUALIFICATION_RECORD_NAME = "qualification-record.json"
QUALIFICATION_MANIFEST_NAME = "qualification-manifest.json"
LIVE_QUALIFICATION_NAME = "live-qualification-v1.json"
SCHEMA_VERSION = 2
EXPECTED_MILESTONE_ACTOR = EXPECTED_REPOSITORY.partition("/")[0]
EVIDENCE_WORKFLOW_PATH = ".github/workflows/release-evidence.yml"
MILESTONE_WORKFLOW_PATH = ".github/workflows/milestone-qualification.yml"
SOURCE_INPUT_PATHS = {
    "policy": "docs/qualification/release-qualification-policy-v1.json",
    "route_table": "docs/qualification/video-quality-route-table-v2.json",
    "runner": MILESTONE_WORKFLOW_PATH,
}
REQUIRED_CASE_IDS = frozenset(
    {
        "sparkle-update-route",
        "clean-machine-signed-update",
        "installed-ui-accessibility",
        "profile-save-action-accessibility",
    }
)
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
TAG_PATTERN = re.compile(r"^v[0-9A-Za-z][0-9A-Za-z.-]*$")
TIMESTAMP_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")
IDENTIFIER_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
CAPTURE_KEYS = frozenset(
    {
        "capture_sha256",
        "capture_workflow",
        "captured_at",
        "live_appcast",
        "qualification_record",
        "receipt",
        "record_type",
        "release_tag",
        "release_workflow",
        "repository",
        "schema_version",
        "signed_ui",
        "source_inputs",
        "source_sha",
        "state",
    }
)
QUALIFICATION_KEYS = frozenset(
    {
        "accepted_case_receipts",
        "artifact",
        "capture",
        "profile_preservation",
        "qualification_manifest",
        "qualification_record",
        "qualification_sha256",
        "qualified_at",
        "record_type",
        "release_tag",
        "schema_version",
        "source_sha",
        "state",
        "successful_milestone",
        "updater_route",
    }
)
DISPOSITION_KEYS = frozenset(
    {
        "capture",
        "disposition_sha256",
        "failed_at",
        "failure",
        "failure_workflow",
        "preservation",
        "record_type",
        "release_tag",
        "schema_version",
        "source_sha",
        "state",
    }
)


class ReleaseEvidenceV2Error(RuntimeError):
    pass


@dataclass(frozen=True)
class DigestBinding:
    path: str
    sha256: str


@dataclass(frozen=True)
class ReceiptBinding:
    asset_id: int
    file_sha256: str
    path: str
    receipt_sha256: str


@dataclass(frozen=True)
class FileReceiptBinding:
    file_sha256: str
    path: str
    receipt_sha256: str


@dataclass(frozen=True)
class WorkflowBinding:
    actor: str
    path: str
    run_attempt: int
    run_id: int


@dataclass(frozen=True)
class SignedUIBinding:
    archive: DigestBinding
    artifact_id: int
    receipt: FileReceiptBinding


@dataclass(frozen=True)
class CaptureV2:
    capture_sha256: str
    captured_at: datetime
    receipt: ReceiptBinding
    release_tag: str
    release_workflow: WorkflowBinding
    qualification_record: DigestBinding
    signed_ui: SignedUIBinding
    source_sha: str


@dataclass(frozen=True)
class QualificationV2:
    capture: DigestBinding
    qualification_sha256: str
    qualified_at: datetime
    release_tag: str
    source_sha: str


@dataclass(frozen=True)
class DispositionV2:
    capture: DigestBinding
    disposition_sha256: str
    failed_at: datetime
    release_tag: str
    source_sha: str


@dataclass(frozen=True)
class _BundleReader:
    repo_root: Path
    revision: str | None
    worktree: bool

    def read(self, relative_path: str, description: str) -> bytes:
        path = _path(relative_path, description)
        if self.worktree:
            target, _ = _repo_path(self.repo_root, path, description)
            try:
                return target.read_bytes()
            except OSError as error:
                raise ReleaseEvidenceV2Error(f"Unable to read {description} at {target}: {error}") from error
        if self.revision is None:
            raise ReleaseEvidenceV2Error("A verification revision is required unless --worktree is explicit.")
        return _git_run(self.repo_root, ["show", f"{self.revision}:{path}"], f"read {description}")

    def exists(self, relative_path: str) -> bool:
        path = _path(relative_path, "evidence path")
        if self.worktree:
            target, _ = _repo_path(self.repo_root, path, "evidence path")
            return target.is_file()
        if self.revision is None:
            return False
        result = subprocess.run(
            ["git", "cat-file", "-e", f"{self.revision}:{path}"],
            cwd=self.repo_root,
            capture_output=True,
            check=False,
        )
        return result.returncode == 0


def _mapping(value: object, description: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ReleaseEvidenceV2Error(f"{description} must be a JSON object.")
    return cast(Mapping[str, Any], value)


def _string(value: object, description: str) -> str:
    if not isinstance(value, str) or not value:
        raise ReleaseEvidenceV2Error(f"{description} must be a non-empty string.")
    return value


def _positive_integer(value: object, description: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ReleaseEvidenceV2Error(f"{description} must be a positive integer.")
    return value


def _boolean(value: object, description: str) -> bool:
    if not isinstance(value, bool):
        raise ReleaseEvidenceV2Error(f"{description} must be a boolean.")
    return value


def _exact_keys(value: Mapping[str, Any], expected: frozenset[str], description: str) -> None:
    actual = frozenset(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ReleaseEvidenceV2Error(f"{description} keys changed; missing={missing}, extra={extra}.")


def _sha(value: object, description: str) -> str:
    text = _string(value, description)
    if SHA_PATTERN.fullmatch(text) is None:
        raise ReleaseEvidenceV2Error(f"{description} must be a full lowercase Git SHA.")
    return text


def _sha256(value: object, description: str) -> str:
    text = _string(value, description)
    if SHA256_PATTERN.fullmatch(text) is None:
        raise ReleaseEvidenceV2Error(f"{description} must be a lowercase SHA-256 digest.")
    return text


def _timestamp(value: object, description: str) -> datetime:
    text = _string(value, description)
    if TIMESTAMP_PATTERN.fullmatch(text) is None:
        raise ReleaseEvidenceV2Error(f"{description} must be a UTC ISO-8601 timestamp ending in Z.")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ReleaseEvidenceV2Error(f"{description} must be a valid ISO-8601 timestamp.") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ReleaseEvidenceV2Error(f"{description} must use UTC.")
    return parsed


def _tag(value: object, description: str) -> str:
    text = _string(value, description)
    if TAG_PATTERN.fullmatch(text) is None:
        raise ReleaseEvidenceV2Error(f"{description} is not a canonical release tag.")
    return text


def _identifier(value: object, description: str) -> str:
    text = _string(value, description)
    if IDENTIFIER_PATTERN.fullmatch(text) is None:
        raise ReleaseEvidenceV2Error(f"{description} must be a lowercase identifier.")
    return text


def _path(value: object, description: str) -> str:
    text = _string(value, description)
    path = PurePosixPath(text)
    if path.is_absolute() or text.startswith(("./", "~/")) or path.as_posix() != text:
        raise ReleaseEvidenceV2Error(f"{description} must be a normalized repository-relative path.")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ReleaseEvidenceV2Error(f"{description} must be a normalized repository-relative path.")
    return text


def _repo_path(repo_root: Path, value: object, description: str) -> tuple[Path, str]:
    relative = _path(value, description)
    resolved_root = repo_root.resolve()
    candidate = (resolved_root / relative).resolve()
    try:
        candidate.relative_to(resolved_root)
    except ValueError as error:
        raise ReleaseEvidenceV2Error(f"{description} escapes the repository root.") from error
    return candidate, relative


def _git_run(repo_root: Path, arguments: Sequence[str], description: str) -> bytes:
    result = subprocess.run(["git", *arguments], cwd=repo_root, capture_output=True, check=False)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ReleaseEvidenceV2Error(f"Unable to {description}: {detail or 'git read failed'}.")
    return result.stdout


def _resolve_revision(repo_root: Path, revision: str) -> str:
    candidate = _sha(revision, "verification revision")
    return (
        _git_run(repo_root, ["rev-parse", "--verify", f"{candidate}^{{commit}}"], "resolve verification revision")
        .decode()
        .strip()
    )


def _verification_reader(
    repo_root: Path,
    *,
    verification_revision: str | None,
    worktree: bool,
) -> _BundleReader:
    if worktree and verification_revision is not None:
        raise ReleaseEvidenceV2Error("Worktree verification cannot be combined with a verification revision.")
    if worktree:
        return _BundleReader(repo_root=repo_root, revision=None, worktree=True)
    revision = _resolve_revision(
        repo_root, verification_revision or _git_run(repo_root, ["rev-parse", "HEAD"], "resolve HEAD").decode().strip()
    )
    return _BundleReader(repo_root=repo_root, revision=revision, worktree=False)


@contextmanager
def _materialized_revision(repo_root: Path, revision: str) -> Iterator[Path]:
    with tempfile.TemporaryDirectory() as temporary_directory:
        worktree = Path(temporary_directory) / "revision"
        _git_run(
            repo_root,
            ["worktree", "add", "--detach", "--force", worktree.as_posix(), revision],
            "materialize historical revision",
        )
        cleanup_error: ReleaseEvidenceV2Error | None = None
        try:
            yield worktree
        finally:
            body_failed = sys.exc_info()[0] is not None
            try:
                _git_run(
                    repo_root,
                    ["worktree", "remove", "--force", worktree.as_posix()],
                    "remove materialized historical revision",
                )
            except ReleaseEvidenceV2Error as error:
                cleanup_error = error
            if cleanup_error is not None and not body_failed:
                raise cleanup_error


def canonical_payload_bytes(record: Mapping[str, Any]) -> bytes:
    payload = dict(record)
    record_type = _string(payload.get("record_type"), "record type")
    digest_field = {
        "capture": "capture_sha256",
        "qualification": "qualification_sha256",
        "disposition": "disposition_sha256",
    }.get(record_type)
    if digest_field is None:
        raise ReleaseEvidenceV2Error("Record type must be capture, qualification, or disposition.")
    payload.pop(digest_field, None)
    return (json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


def record_sha256(record: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_payload_bytes(record)).hexdigest()


def canonical_record_bytes(record: Mapping[str, Any]) -> bytes:
    return (json.dumps(record, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()


def with_self_digest(record: Mapping[str, Any]) -> dict[str, Any]:
    output = dict(record)
    digest_field = {
        "capture": "capture_sha256",
        "qualification": "qualification_sha256",
        "disposition": "disposition_sha256",
    }.get(_string(output.get("record_type"), "record type"))
    if digest_field is None:
        raise ReleaseEvidenceV2Error("Record type must be capture, qualification, or disposition.")
    output[digest_field] = record_sha256(output)
    return output


def write_record(path: Path, record: Mapping[str, Any]) -> None:
    expected = canonical_record_bytes(record)
    try:
        with path.open("xb") as handle:
            handle.write(expected)
    except FileExistsError:
        try:
            current = path.read_bytes()
        except OSError as error:
            raise ReleaseEvidenceV2Error(f"Unable to read existing write-once record at {path}: {error}") from error
        if current != expected:
            raise ReleaseEvidenceV2Error(
                f"Write-once release evidence already exists with different bytes: {path}."
            ) from None
    except OSError as error:
        raise ReleaseEvidenceV2Error(f"Unable to create write-once record at {path}: {error}") from error


def _load_record(reader: _BundleReader, relative_path: str, description: str) -> Mapping[str, Any]:
    raw = reader.read(relative_path, description)
    try:
        record = _mapping(json.loads(raw), description)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReleaseEvidenceV2Error(f"Invalid JSON in {description}: {error}") from error
    if raw != canonical_record_bytes(record):
        raise ReleaseEvidenceV2Error(f"{description.capitalize()} must use canonical JSON serialization.")
    return record


def _digest_binding(value: object, description: str, *, expected_path: str | None = None) -> DigestBinding:
    binding = _mapping(value, description)
    _exact_keys(binding, frozenset({"path", "sha256"}), description)
    path = _path(binding.get("path"), f"{description} path")
    if expected_path is not None and path != expected_path:
        raise ReleaseEvidenceV2Error(f"{description} path conflicts with the release tag.")
    return DigestBinding(path=path, sha256=_sha256(binding.get("sha256"), f"{description} SHA-256"))


def _receipt_binding(value: object, description: str, *, expected_path: str) -> ReceiptBinding:
    binding = _mapping(value, description)
    _exact_keys(binding, frozenset({"asset_id", "file_sha256", "path", "receipt_sha256"}), description)
    path = _path(binding.get("path"), f"{description} path")
    if path != expected_path:
        raise ReleaseEvidenceV2Error(f"{description} path conflicts with the release tag.")
    return ReceiptBinding(
        asset_id=_positive_integer(binding.get("asset_id"), f"{description} asset ID"),
        file_sha256=_sha256(binding.get("file_sha256"), f"{description} file SHA-256"),
        path=path,
        receipt_sha256=_sha256(binding.get("receipt_sha256"), f"{description} self SHA-256"),
    )


def _file_receipt_binding(value: object, description: str, *, expected_path: str) -> FileReceiptBinding:
    binding = _mapping(value, description)
    _exact_keys(binding, frozenset({"file_sha256", "path", "receipt_sha256"}), description)
    path = _path(binding.get("path"), f"{description} path")
    if path != expected_path:
        raise ReleaseEvidenceV2Error(f"{description} path conflicts with the release tag.")
    return FileReceiptBinding(
        file_sha256=_sha256(binding.get("file_sha256"), f"{description} file SHA-256"),
        path=path,
        receipt_sha256=_sha256(binding.get("receipt_sha256"), f"{description} self SHA-256"),
    )


def _workflow_binding(value: object, description: str, *, expected_path: str | None = None) -> WorkflowBinding:
    binding = _mapping(value, description)
    _exact_keys(binding, frozenset({"actor", "path", "run_attempt", "run_id"}), description)
    path = _path(binding.get("path"), f"{description} path")
    if expected_path is not None and path != expected_path:
        raise ReleaseEvidenceV2Error(f"{description} path is not canonical.")
    return WorkflowBinding(
        actor=_string(binding.get("actor"), f"{description} actor"),
        path=path,
        run_attempt=_positive_integer(binding.get("run_attempt"), f"{description} run attempt"),
        run_id=_positive_integer(binding.get("run_id"), f"{description} run ID"),
    )


def _verify_record_digest(record: Mapping[str, Any], field: str, description: str) -> str:
    recorded = _sha256(record.get(field), f"{description} self SHA-256")
    if recorded != record_sha256(record):
        raise ReleaseEvidenceV2Error(f"{description.capitalize()} self-digest mismatch.")
    return recorded


def _digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _require_digest(reader: _BundleReader, binding: DigestBinding, description: str) -> bytes:
    data = reader.read(binding.path, description)
    if _digest_bytes(data) != binding.sha256:
        raise ReleaseEvidenceV2Error(f"{description.capitalize()} digest conflicts with its archived bytes.")
    return data


def _load_json_bytes(data: bytes, description: str) -> Mapping[str, Any]:
    try:
        return _mapping(json.loads(data), description)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReleaseEvidenceV2Error(f"Invalid JSON in {description}: {error}") from error


def _validate_receipt_bytes(data: bytes, binding: ReceiptBinding) -> Mapping[str, Any]:
    if _digest_bytes(data) != binding.file_sha256:
        raise ReleaseEvidenceV2Error("Release receipt digest conflicts with its archived bytes.")
    with tempfile.NamedTemporaryFile("wb", suffix=".json") as temporary:
        temporary.write(data)
        temporary.flush()
        try:
            receipt, file_sha256 = load_validated_checked_receipt(Path(temporary.name))
        except ReleaseReceiptError as error:
            raise ReleaseEvidenceV2Error(f"Archived release receipt is invalid: {error}") from error
    if file_sha256 != binding.file_sha256 or receipt.get("receipt_sha256") != binding.receipt_sha256:
        raise ReleaseEvidenceV2Error("Release receipt binding does not match its archived receipt.")
    return receipt


def _validate_qualification_snapshot(
    reader: _BundleReader,
    binding: DigestBinding,
    *,
    source_sha: str,
    source_inputs: Mapping[str, DigestBinding],
    release_receipt: Mapping[str, Any],
) -> None:
    qualification_bytes = _require_digest(reader, binding, "archived qualification record")
    qualification = _load_json_bytes(qualification_bytes, "archived qualification record")
    if qualification_bytes != canonical_record_bytes(qualification):
        raise ReleaseEvidenceV2Error("Archived qualification record must use canonical JSON serialization.")
    policy_bytes = _git_run(
        reader.repo_root,
        ["show", f"{source_sha}:{source_inputs['policy'].path}"],
        "read historical qualification policy",
    )
    route_table_bytes = _git_run(
        reader.repo_root,
        ["show", f"{source_sha}:{source_inputs['route_table'].path}"],
        "read historical qualification route table",
    )
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_root = Path(temporary_directory)
        qualification_path = temporary_root / "qualification-record.json"
        policy_path = temporary_root / "release-qualification-policy-v1.json"
        route_table_path = temporary_root / "video-quality-route-table-v2.json"
        qualification_path.write_bytes(qualification_bytes)
        policy_path.write_bytes(policy_bytes)
        route_table_path.write_bytes(route_table_bytes)
        try:
            validate_qualification_record(
                qualification_path,
                release_receipt,
                policy_path=policy_path,
                route_table_path=route_table_path,
            )
        except ReleaseEvidenceError as error:
            raise ReleaseEvidenceV2Error(f"Archived qualification record is invalid: {error}") from error


def qualification_template_path(release_tag: str, release_route: str) -> str:
    suffix = "-stable" if release_route == "stable" else ""
    return f"docs/qualification/{release_tag}{suffix}-signed-qualification-v1.json"


def _validate_source_inputs(
    reader: _BundleReader,
    source_sha: str,
    release_tag: str,
    release_route: str,
    value: object,
) -> Mapping[str, DigestBinding]:
    inputs = _mapping(value, "capture-v2 source inputs")
    expected_paths = {
        **SOURCE_INPUT_PATHS,
        "qualification_template": qualification_template_path(release_tag, release_route),
    }
    _exact_keys(inputs, frozenset(expected_paths), "capture-v2 source inputs")
    validated: dict[str, DigestBinding] = {}
    for name, expected_path in expected_paths.items():
        binding = _digest_binding(inputs.get(name), f"capture-v2 source input {name}", expected_path=expected_path)
        source_bytes = _git_run(reader.repo_root, ["show", f"{source_sha}:{binding.path}"], f"read source input {name}")
        if _digest_bytes(source_bytes) != binding.sha256:
            raise ReleaseEvidenceV2Error(f"Capture-v2 source input {name} digest does not match {source_sha}.")
        validated[name] = binding
    return validated


def _verify_source_ancestry(reader: _BundleReader, source_sha: str) -> None:
    if reader.worktree:
        revision = _git_run(reader.repo_root, ["rev-parse", "HEAD"], "resolve worktree HEAD").decode().strip()
    else:
        if reader.revision is None:
            raise ReleaseEvidenceV2Error("Verification revision is missing.")
        revision = reader.revision
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", source_sha, revision],
        cwd=reader.repo_root,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise ReleaseEvidenceV2Error("Capture source_sha is not an ancestor of the verification revision.")


def _receipt_artifact_digest(receipt: Mapping[str, Any], kind: str) -> str:
    artifacts = receipt.get("artifacts")
    if not isinstance(artifacts, list):
        raise ReleaseEvidenceV2Error("Validated release receipt artifacts are unavailable.")
    matches = [item for item in artifacts if isinstance(item, Mapping) and item.get("kind") == kind]
    if len(matches) != 1 or not isinstance(matches[0].get("sha256"), str):
        raise ReleaseEvidenceV2Error(f"Validated release receipt must contain one {kind} artifact.")
    return cast(str, matches[0]["sha256"])


def _validate_signed_ui_archive(archive_bytes: bytes, receipt_bytes: bytes) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            entries = archive.infolist()
            for entry in entries:
                name = entry.filename
                normalized = PurePosixPath(name)
                if (
                    not name
                    or "\\" in name
                    or normalized.is_absolute()
                    or normalized.as_posix() != name
                    or any(part in {"", ".", ".."} for part in normalized.parts)
                ):
                    raise ReleaseEvidenceV2Error("Signed UI archive contains a traversal or non-canonical name.")
            files = [entry for entry in entries if not entry.is_dir()]
            if len(files) != 1 or files[0].filename != SIGNED_UI_RECEIPT_NAME:
                raise ReleaseEvidenceV2Error(
                    "Signed UI archive must contain exactly one non-directory canonical receipt file."
                )
            receipt_entry = files[0]
            with archive.open(receipt_entry) as archived_receipt:
                archived_receipt_bytes = archived_receipt.read(MAX_RECEIPT_BYTES + 1)
            if len(archived_receipt_bytes) > MAX_RECEIPT_BYTES:
                raise ReleaseEvidenceV2Error("Signed UI archive receipt exceeds its bounded receipt limit.")
            if archived_receipt_bytes != receipt_bytes:
                raise ReleaseEvidenceV2Error("Signed UI archive receipt is not byte-identical to the archived receipt.")
    except zipfile.BadZipFile as error:
        raise ReleaseEvidenceV2Error("Signed UI archive is not a valid ZIP archive.") from error


def _validate_signed_ui(
    reader: _BundleReader,
    value: object,
    *,
    release_tag: str,
    release_receipt: Mapping[str, Any],
    release_binding: ReceiptBinding,
    release_workflow: WorkflowBinding,
    policy: Mapping[str, Any],
) -> SignedUIBinding:
    binding = _mapping(value, "capture-v2 signed UI")
    _exact_keys(binding, frozenset({"archive", "artifact_id", "receipt"}), "capture-v2 signed UI")
    archive = _digest_binding(
        binding.get("archive"),
        "capture-v2 signed UI archive",
        expected_path=f"docs/release-evidence/{release_tag}/{SIGNED_UI_ARCHIVE_NAME}",
    )
    receipt = _file_receipt_binding(
        binding.get("receipt"),
        "capture-v2 signed UI receipt",
        expected_path=f"docs/release-evidence/{release_tag}/{SIGNED_UI_RECEIPT_NAME}",
    )
    artifact_id = _positive_integer(binding.get("artifact_id"), "capture-v2 signed UI artifact ID")
    archive_bytes = _require_digest(reader, archive, "signed UI archive")
    receipt_bytes = reader.read(receipt.path, "signed UI receipt")
    if _digest_bytes(receipt_bytes) != receipt.file_sha256:
        raise ReleaseEvidenceV2Error("Signed UI receipt digest conflicts with its archived bytes.")
    _validate_signed_ui_archive(archive_bytes, receipt_bytes)
    try:
        policy_id = validate_policy_case(policy, PROFILE_CASE_ID)
        expectation = release_expectation_from_receipt(
            release_receipt,
            policy_id=policy_id,
            case_id=PROFILE_CASE_ID,
            workflow_run_id=release_workflow.run_id,
            workflow_run_attempt=release_workflow.run_attempt,
            release_receipt_asset_id=release_binding.asset_id,
            release_receipt_file_sha256=release_binding.file_sha256,
        )
        with tempfile.NamedTemporaryFile("wb", suffix=".json") as temporary:
            temporary.write(receipt_bytes)
            temporary.flush()
            validated = load_validated_receipt(
                Path(temporary.name), expectation, expected_file_sha256=receipt.file_sha256
            )
    except SignedArtifactReceiptError as error:
        raise ReleaseEvidenceV2Error(f"Archived signed UI receipt is invalid: {error}") from error
    if validated.get("receipt_sha256") != receipt.receipt_sha256:
        raise ReleaseEvidenceV2Error("Signed UI receipt binding does not match its archived receipt.")
    return SignedUIBinding(archive=archive, artifact_id=artifact_id, receipt=receipt)


def _validate_capture(reader: _BundleReader, record: Mapping[str, Any]) -> CaptureV2:
    _exact_keys(record, CAPTURE_KEYS, "capture-v2 record")
    if record.get("schema_version") != SCHEMA_VERSION or record.get("record_type") != "capture":
        raise ReleaseEvidenceV2Error("Unsupported capture-v2 schema or record type.")
    if record.get("state") != "CAPTURED":
        raise ReleaseEvidenceV2Error("capture-v2 state must be CAPTURED.")
    if record.get("repository") != EXPECTED_REPOSITORY:
        raise ReleaseEvidenceV2Error("capture-v2 repository is not canonical.")
    release_tag = _tag(record.get("release_tag"), "capture-v2 release tag")
    source_sha = _sha(record.get("source_sha"), "capture-v2 source SHA")
    captured_at = _timestamp(record.get("captured_at"), "capture-v2 captured_at")
    capture_sha256 = _verify_record_digest(record, "capture_sha256", "capture-v2 record")
    _verify_source_ancestry(reader, source_sha)
    receipt = _receipt_binding(
        record.get("receipt"),
        "capture-v2 release receipt",
        expected_path=f"docs/release-evidence/{release_tag}/{RECEIPT_NAME}",
    )
    receipt_data = reader.read(receipt.path, "release receipt")
    release_receipt = _validate_receipt_bytes(receipt_data, receipt)
    if release_receipt.get("source_sha") != source_sha:
        raise ReleaseEvidenceV2Error("capture-v2 source SHA conflicts with the archived release receipt.")
    release = _mapping(release_receipt.get("release"), "validated release receipt release")
    if release.get("tag") != release_tag:
        raise ReleaseEvidenceV2Error("capture-v2 release tag conflicts with the archived release receipt.")
    release_workflow = _workflow_binding(record.get("release_workflow"), "capture-v2 release workflow")
    workflow = _mapping(release_receipt.get("workflow"), "validated release receipt workflow")
    expected_release_workflow = {
        "actor": workflow.get("actor"),
        "path": workflow.get("path"),
        "run_attempt": workflow.get("run_attempt"),
        "run_id": workflow.get("run_id"),
    }
    if expected_release_workflow != {
        "actor": release_workflow.actor,
        "path": release_workflow.path,
        "run_attempt": release_workflow.run_attempt,
        "run_id": release_workflow.run_id,
    }:
        raise ReleaseEvidenceV2Error("capture-v2 release workflow conflicts with the archived release receipt.")
    capture_workflow = _workflow_binding(
        record.get("capture_workflow"), "capture-v2 workflow", expected_path=EVIDENCE_WORKFLOW_PATH
    )
    if capture_workflow.actor != REQUIRED_ACTOR:
        raise ReleaseEvidenceV2Error("capture-v2 workflow actor is not the approved release actor.")
    release_route = _string(release_receipt.get("release_route"), "release route")
    source_inputs = _validate_source_inputs(
        reader,
        source_sha,
        release_tag,
        release_route,
        record.get("source_inputs"),
    )
    policy_bytes = _git_run(
        reader.repo_root, ["show", f"{source_sha}:{source_inputs['policy'].path}"], "read source policy"
    )
    policy = _load_json_bytes(policy_bytes, "historical release qualification policy")
    signed_ui = _validate_signed_ui(
        reader,
        record.get("signed_ui"),
        release_tag=release_tag,
        release_receipt=release_receipt,
        release_binding=receipt,
        release_workflow=release_workflow,
        policy=policy,
    )
    qualification_record = _digest_binding(
        record.get("qualification_record"),
        "capture-v2 archived qualification record",
        expected_path=f"docs/release-evidence/{release_tag}/{QUALIFICATION_RECORD_NAME}",
    )
    _validate_qualification_snapshot(
        reader,
        qualification_record,
        source_sha=source_sha,
        source_inputs=source_inputs,
        release_receipt=release_receipt,
    )
    live_appcast = _mapping(record.get("live_appcast"), "capture-v2 live appcast")
    _exact_keys(live_appcast, frozenset({"sha256", "verified_at"}), "capture-v2 live appcast")
    live_appcast_sha256 = _sha256(live_appcast.get("sha256"), "capture-v2 live appcast SHA-256")
    if live_appcast_sha256 != _receipt_artifact_digest(release_receipt, "appcast"):
        raise ReleaseEvidenceV2Error("capture-v2 live appcast digest conflicts with the archived release receipt.")
    live_appcast_verified_at = _timestamp(live_appcast.get("verified_at"), "capture-v2 live appcast verified_at")
    publication_at = _timestamp(release.get("created_at"), "release publication timestamp")
    if publication_at > live_appcast_verified_at:
        raise ReleaseEvidenceV2Error("capture-v2 live appcast verification precedes release publication.")
    if live_appcast_verified_at > captured_at:
        raise ReleaseEvidenceV2Error("capture-v2 live appcast verification follows capture.")
    return CaptureV2(
        capture_sha256=capture_sha256,
        captured_at=captured_at,
        receipt=receipt,
        release_tag=release_tag,
        release_workflow=release_workflow,
        qualification_record=qualification_record,
        signed_ui=signed_ui,
        source_sha=source_sha,
    )


def _validate_terminal_common(
    record: Mapping[str, Any],
    *,
    capture: CaptureV2,
    record_type: Literal["qualification", "disposition"],
    state: str,
    timestamp_field: str,
    digest_field: str,
    expected_keys: frozenset[str],
) -> tuple[datetime, DigestBinding, str]:
    _exact_keys(record, expected_keys, f"{record_type}-v2 record")
    if record.get("schema_version") != SCHEMA_VERSION or record.get("record_type") != record_type:
        raise ReleaseEvidenceV2Error(f"Unsupported {record_type}-v2 schema or record type.")
    if record.get("state") != state:
        raise ReleaseEvidenceV2Error(f"{record_type}-v2 state must be {state}.")
    if _tag(record.get("release_tag"), f"{record_type}-v2 release tag") != capture.release_tag:
        raise ReleaseEvidenceV2Error(f"{record_type}-v2 release tag conflicts with capture-v2.")
    if _sha(record.get("source_sha"), f"{record_type}-v2 source SHA") != capture.source_sha:
        raise ReleaseEvidenceV2Error(f"{record_type}-v2 source SHA conflicts with capture-v2.")
    capture_binding = _digest_binding(
        record.get("capture"),
        f"{record_type}-v2 capture",
        expected_path=f"docs/release-evidence/{capture.release_tag}/{CAPTURE_NAME}",
    )
    if capture_binding.sha256 != capture.capture_sha256:
        raise ReleaseEvidenceV2Error(f"{record_type}-v2 capture digest conflicts with capture-v2.")
    event_at = _timestamp(record.get(timestamp_field), f"{record_type}-v2 {timestamp_field}")
    if event_at < capture.captured_at:
        raise ReleaseEvidenceV2Error(f"{record_type}-v2 {timestamp_field} precedes capture-v2.")
    return event_at, capture_binding, _verify_record_digest(record, digest_field, f"{record_type}-v2 record")


def _historical_policy(repo_root: Path, source_sha: str) -> tuple[str, Mapping[str, Mapping[str, Any]]]:
    policy_bytes = _git_run(
        repo_root,
        ["show", f"{source_sha}:{SOURCE_INPUT_PATHS['policy']}"],
        "read historical release qualification policy",
    )
    policy = _load_json_bytes(policy_bytes, "historical release qualification policy")
    policy_id = _string(policy.get("policy_id"), "historical release qualification policy ID")
    cases = policy.get("cases")
    if not isinstance(cases, list):
        raise ReleaseEvidenceV2Error("Historical release qualification policy cases are unavailable.")
    output: dict[str, Mapping[str, Any]] = {}
    for case in cases:
        case_mapping = _mapping(case, "historical release qualification case")
        case_id = _string(case_mapping.get("id"), "historical release qualification case ID")
        if case_id in output:
            raise ReleaseEvidenceV2Error("Historical release qualification policy contains duplicate case IDs.")
        output[case_id] = case_mapping
    return policy_id, output


def _case_source(case: Mapping[str, Any], case_id: str, source: str) -> None:
    sources = case.get("allowed_evidence_sources")
    if not isinstance(sources, list) or not all(isinstance(item, str) for item in sources):
        raise ReleaseEvidenceV2Error(f"Historical release qualification case {case_id} has invalid evidence sources.")
    if source not in sources:
        raise ReleaseEvidenceV2Error(
            f"qualification-v2 receipt {case_id} source is not allowed by the historical policy."
        )


def _validate_tier3_case_receipt(
    reader: _BundleReader,
    *,
    accepted_at: datetime,
    case_id: str,
    binding: DigestBinding,
    source: str,
    policy_id: str,
    case: Mapping[str, Any],
    capture: CaptureV2,
    qualified_at: datetime,
) -> None:
    content = _require_digest(reader, binding, f"qualification case receipt {case_id}")
    receipt = _load_json_bytes(content, f"qualification case receipt {case_id}")
    if content != canonical_record_bytes(receipt):
        raise ReleaseEvidenceV2Error(f"Qualification case receipt {case_id} must use canonical JSON serialization.")
    try:
        validated = load_validated_receipt_bytes(
            content,
            policy_id=policy_id,
            case=case,
            reference_content=lambda reference: reader.read(reference, "Tier 3 referenced release receipt"),
        )
    except (OSError, Tier3ReceiptError) as error:
        raise ReleaseEvidenceV2Error(f"Qualification case receipt {case_id} is invalid: {error}") from error
    result = _mapping(validated.get("result"), f"qualification case receipt {case_id} result")
    if validated.get("case_id") != case_id or validated.get("evidence_source") != source:
        raise ReleaseEvidenceV2Error(f"Qualification case receipt {case_id} identity is invalid.")
    if result.get("status") != "passed":
        raise ReleaseEvidenceV2Error(f"Qualification case receipt {case_id} did not pass.")
    release_identity = _mapping(
        validated.get("release_identity"), f"qualification case receipt {case_id} release identity"
    )
    release_reference = _mapping(
        release_identity.get("release_receipt"),
        f"qualification case receipt {case_id} release receipt reference",
    )
    if release_reference != {
        "file_sha256": capture.receipt.file_sha256,
        "receipt_sha256": capture.receipt.receipt_sha256,
        "reference": capture.receipt.path,
    }:
        raise ReleaseEvidenceV2Error(
            f"Qualification case receipt {case_id} is not bound to the captured release receipt."
        )
    timestamps = _mapping(validated.get("timestamps"), f"qualification case receipt {case_id} timestamps")
    started_at = _timestamp(timestamps.get("started_at"), f"qualification case receipt {case_id} started_at")
    completed_at = _timestamp(timestamps.get("completed_at"), f"qualification case receipt {case_id} completed_at")
    if started_at < capture.captured_at or completed_at > qualified_at or accepted_at < completed_at:
        raise ReleaseEvidenceV2Error(
            f"Qualification case receipt {case_id} timestamps conflict with bundle chronology."
        )


def _release_channel(release_tag: str, prerelease: object) -> str:
    if prerelease is False:
        return "stable"
    if prerelease is not True:
        raise ReleaseEvidenceV2Error("Validated release receipt prerelease flag is invalid.")
    if "-beta." in release_tag:
        return "beta"
    if "-rc." in release_tag:
        return "rc"
    raise ReleaseEvidenceV2Error("Prerelease tag does not identify a beta or release-candidate channel.")


def _validate_live_qualification_receipt(
    content: bytes,
    *,
    capture: CaptureV2,
    release_receipt: Mapping[str, Any],
    clean_machine_receipt: DigestBinding,
    artifact_id: int,
    artifact_sha256: str,
    milestone_workflow: WorkflowBinding,
    qualification_manifest_sha256: str,
    qualified_at: datetime,
) -> None:
    record = _load_json_bytes(content, "sparkle update live qualification receipt")
    if content != canonical_record_bytes(record):
        raise ReleaseEvidenceV2Error("Sparkle update live qualification receipt must use canonical JSON serialization.")
    _exact_keys(
        record,
        frozenset({"candidate", "cases", "qualification_id", "qualified_at", "schema_version"}),
        "sparkle update live qualification receipt",
    )
    if record.get("schema_version") != 1:
        raise ReleaseEvidenceV2Error("Sparkle update live qualification receipt schema_version must be 1.")
    release = _mapping(release_receipt.get("release"), "validated release receipt release")
    versions = _mapping(release_receipt.get("versions"), "validated release receipt versions")
    workflow = _mapping(release_receipt.get("workflow"), "validated release receipt workflow")
    expected_candidate = {
        "appcast_sha256": _receipt_artifact_digest(release_receipt, "appcast"),
        "build": versions.get("build"),
        "dmg_sha256": _receipt_artifact_digest(release_receipt, "dmg"),
        "package_version": versions.get("package"),
        "public_version": versions.get("public"),
        "release_id": release.get("id"),
        "release_run_id": workflow.get("run_id"),
        "release_tag": capture.release_tag,
        "signed_app_tree_sha256": release_receipt.get("signed_app_tree_sha256"),
        "source_sha": capture.source_sha,
    }
    candidate = _mapping(record.get("candidate"), "sparkle update live qualification candidate")
    _exact_keys(candidate, frozenset(expected_candidate), "sparkle update live qualification candidate")
    if candidate != expected_candidate:
        raise ReleaseEvidenceV2Error("Sparkle update live qualification candidate conflicts with the release receipt.")
    channel = _release_channel(capture.release_tag, release.get("prerelease"))
    if record.get("qualification_id") != f"{channel}-live-qualification-v1":
        raise ReleaseEvidenceV2Error("Sparkle update live qualification ID conflicts with the release channel.")
    live_qualified_at = _timestamp(record.get("qualified_at"), "sparkle update live qualification qualified_at")
    if live_qualified_at < capture.captured_at or live_qualified_at > qualified_at:
        raise ReleaseEvidenceV2Error("Sparkle update live qualification timestamp conflicts with bundle chronology.")
    cases = record.get("cases")
    if not isinstance(cases, list) or len(cases) != 1:
        raise ReleaseEvidenceV2Error("Sparkle update live qualification must contain exactly one case.")
    case = _mapping(cases[0], "sparkle update live qualification case")
    _exact_keys(case, frozenset({"id", "observations", "result"}), "sparkle update live qualification case")
    if case.get("id") != "sparkle-update-route" or case.get("result") != "passed":
        raise ReleaseEvidenceV2Error("Sparkle update live qualification case did not pass.")
    observations = _mapping(case.get("observations"), "sparkle update live qualification observations")
    route = _string(observations.get("route"), "sparkle update live qualification route")
    if route not in {"beta", "rc", "stable"}:
        raise ReleaseEvidenceV2Error("Sparkle update live qualification route is unsupported.")
    offered_key = f"candidate_offered_on_{route}_route"
    expected_observation_keys = frozenset(
        {
            offered_key,
            "install_action",
            "post_update_build",
            "post_update_package_version",
            "post_update_signed_app_tree_sha256",
            "prior_release_tag",
            "profile_preserved",
            "qualification_artifact_digest",
            "qualification_artifact_id",
            "qualification_evidence_sha",
            "qualification_manifest_sha256",
            "qualification_receipt_reference",
            "qualification_receipt_sha256",
            "qualification_workflow_run_id",
            "route",
        }
    )
    _exact_keys(observations, expected_observation_keys, "sparkle update live qualification observations")
    prior_tag = _tag(observations.get("prior_release_tag"), "sparkle update prior release tag")
    if prior_tag == capture.release_tag:
        raise ReleaseEvidenceV2Error("Sparkle update prior release tag must differ from the candidate.")
    expected_observations = {
        offered_key: True,
        "install_action": "Install and Relaunch",
        "post_update_build": versions.get("build"),
        "post_update_package_version": versions.get("package"),
        "post_update_signed_app_tree_sha256": release_receipt.get("signed_app_tree_sha256"),
        "prior_release_tag": prior_tag,
        "profile_preserved": True,
        "qualification_artifact_digest": f"sha256:{artifact_sha256}",
        "qualification_artifact_id": artifact_id,
        "qualification_evidence_sha": observations.get("qualification_evidence_sha"),
        "qualification_manifest_sha256": qualification_manifest_sha256,
        "qualification_receipt_reference": clean_machine_receipt.path,
        "qualification_receipt_sha256": clean_machine_receipt.sha256,
        "qualification_workflow_run_id": milestone_workflow.run_id,
        "route": route,
    }
    _sha(observations.get("qualification_evidence_sha"), "sparkle update qualification evidence SHA")
    if observations != expected_observations:
        raise ReleaseEvidenceV2Error("Sparkle update live qualification observations conflict with terminal evidence.")


def _validate_qualification(reader: _BundleReader, record: Mapping[str, Any], capture: CaptureV2) -> QualificationV2:
    qualified_at, capture_binding, qualification_sha256 = _validate_terminal_common(
        record,
        capture=capture,
        record_type="qualification",
        state="QUALIFIED",
        timestamp_field="qualified_at",
        digest_field="qualification_sha256",
        expected_keys=QUALIFICATION_KEYS,
    )
    qualification_record = _digest_binding(
        record.get("qualification_record"),
        "qualification-v2 archived qualification record",
        expected_path=f"docs/release-evidence/{capture.release_tag}/{QUALIFICATION_RECORD_NAME}",
    )
    if qualification_record != capture.qualification_record:
        raise ReleaseEvidenceV2Error(
            "qualification-v2 qualification record binding conflicts with capture-v2 qualification snapshot."
        )
    _load_json_bytes(
        _require_digest(reader, qualification_record, "archived qualification record"), "archived qualification record"
    )
    milestone_value = _mapping(record.get("successful_milestone"), "qualification-v2 successful milestone")
    _exact_keys(
        milestone_value,
        frozenset({"actor", "path", "result", "run_attempt", "run_id"}),
        "qualification-v2 successful milestone",
    )
    if milestone_value.get("result") != "success":
        raise ReleaseEvidenceV2Error("qualification-v2 milestone result must be success.")
    milestone_workflow = _workflow_binding(
        {field: milestone_value[field] for field in ("actor", "path", "run_attempt", "run_id")},
        "qualification-v2 successful milestone",
        expected_path=MILESTONE_WORKFLOW_PATH,
    )
    if milestone_workflow.actor != EXPECTED_MILESTONE_ACTOR:
        raise ReleaseEvidenceV2Error("qualification-v2 milestone actor is not the repository owner.")
    artifact = _mapping(record.get("artifact"), "qualification-v2 artifact")
    _exact_keys(artifact, frozenset({"artifact_id", "run_attempt", "run_id", "sha256"}), "qualification-v2 artifact")
    artifact_id = _positive_integer(artifact.get("artifact_id"), "qualification-v2 artifact ID")
    artifact_sha256 = _sha256(artifact.get("sha256"), "qualification-v2 artifact SHA-256")
    artifact_run_attempt = _positive_integer(artifact.get("run_attempt"), "qualification-v2 artifact run attempt")
    artifact_run_id = _positive_integer(artifact.get("run_id"), "qualification-v2 artifact run ID")
    if artifact_run_id != milestone_workflow.run_id or artifact_run_attempt != milestone_workflow.run_attempt:
        raise ReleaseEvidenceV2Error("qualification-v2 artifact is not bound to the successful milestone run.")
    if artifact_id == capture.signed_ui.artifact_id or artifact_sha256 == capture.signed_ui.archive.sha256:
        raise ReleaseEvidenceV2Error("qualification-v2 artifact must be independent from the signed UI artifact.")
    qualification_manifest = _digest_binding(
        record.get("qualification_manifest"),
        "qualification-v2 qualification manifest",
        expected_path=f"docs/release-evidence/{capture.release_tag}/{QUALIFICATION_MANIFEST_NAME}",
    )
    qualification_manifest_bytes = _require_digest(reader, qualification_manifest, "qualification manifest")
    qualification_manifest_record = _load_json_bytes(qualification_manifest_bytes, "qualification manifest")
    if qualification_manifest_bytes != canonical_record_bytes(qualification_manifest_record):
        raise ReleaseEvidenceV2Error("Qualification manifest must use canonical JSON serialization.")
    qualification_manifest_self_digest = _sha256(
        qualification_manifest_record.get("manifest_sha256"), "qualification manifest self SHA-256"
    )
    if qualification_manifest_self_digest != manifest_sha256(qualification_manifest_record):
        raise ReleaseEvidenceV2Error("Qualification manifest self-digest mismatch.")
    release_receipt = _validate_receipt_bytes(
        reader.read(capture.receipt.path, "archived release receipt"), capture.receipt
    )
    receipts = _mapping(record.get("accepted_case_receipts"), "qualification-v2 accepted case receipts")
    _exact_keys(receipts, REQUIRED_CASE_IDS, "qualification-v2 accepted case receipts")
    policy_id, policy_cases = _historical_policy(reader.repo_root, capture.source_sha)
    expected_receipt_paths = {
        PROFILE_CASE_ID: capture.signed_ui.receipt.path,
        "sparkle-update-route": f"docs/release-evidence/{capture.release_tag}/{LIVE_QUALIFICATION_NAME}",
        "clean-machine-signed-update": (
            f"docs/release-evidence/{capture.release_tag}/clean-machine-signed-update-receipt.json"
        ),
        "installed-ui-accessibility": (
            f"docs/release-evidence/{capture.release_tag}/installed-ui-accessibility-receipt.json"
        ),
    }
    validated_receipts: dict[str, DigestBinding] = {}
    receipt_accepted_at: dict[str, datetime] = {}
    receipt_sources: dict[str, str] = {}
    for case_id in sorted(REQUIRED_CASE_IDS):
        receipt = _mapping(receipts.get(case_id), f"qualification-v2 receipt {case_id}")
        _exact_keys(
            receipt, frozenset({"accepted_at", "path", "sha256", "source"}), f"qualification-v2 receipt {case_id}"
        )
        source = _string(receipt.get("source"), f"qualification-v2 receipt {case_id} source")
        case = policy_cases.get(case_id)
        if case is None:
            raise ReleaseEvidenceV2Error(f"Historical release qualification policy is missing case {case_id}.")
        _case_source(case, case_id, source)
        accepted_at = _timestamp(receipt.get("accepted_at"), f"qualification-v2 receipt {case_id} accepted_at")
        if accepted_at < capture.captured_at or accepted_at > qualified_at:
            raise ReleaseEvidenceV2Error(
                f"qualification-v2 receipt {case_id} timestamp conflicts with bundle chronology."
            )
        binding = _digest_binding(
            {"path": receipt.get("path"), "sha256": receipt.get("sha256")},
            f"qualification-v2 receipt {case_id}",
            expected_path=expected_receipt_paths[case_id],
        )
        validated_receipts[case_id] = binding
        receipt_accepted_at[case_id] = accepted_at
        receipt_sources[case_id] = source
    profile_receipt = validated_receipts[PROFILE_CASE_ID]
    if (
        profile_receipt.path != capture.signed_ui.receipt.path
        or profile_receipt.sha256 != capture.signed_ui.receipt.file_sha256
    ):
        raise ReleaseEvidenceV2Error("qualification-v2 profile receipt conflicts with the validated signed UI receipt.")
    _require_digest(reader, profile_receipt, "qualification profile receipt")
    for case_id in ("clean-machine-signed-update", "installed-ui-accessibility"):
        _validate_tier3_case_receipt(
            reader,
            accepted_at=receipt_accepted_at[case_id],
            case_id=case_id,
            binding=validated_receipts[case_id],
            source=receipt_sources[case_id],
            policy_id=policy_id,
            case=policy_cases[case_id],
            capture=capture,
            qualified_at=qualified_at,
        )
    live_qualification_content = _require_digest(
        reader,
        validated_receipts["sparkle-update-route"],
        "qualification case receipt sparkle-update-route",
    )
    _validate_live_qualification_receipt(
        live_qualification_content,
        capture=capture,
        release_receipt=release_receipt,
        clean_machine_receipt=validated_receipts["clean-machine-signed-update"],
        artifact_id=artifact_id,
        artifact_sha256=artifact_sha256,
        milestone_workflow=milestone_workflow,
        qualification_manifest_sha256=qualification_manifest_self_digest,
        qualified_at=qualified_at,
    )
    profile = _mapping(record.get("profile_preservation"), "qualification-v2 profile preservation")
    _exact_keys(profile, frozenset({"case_id", "preserved", "receipt_sha256"}), "qualification-v2 profile preservation")
    if (
        profile.get("case_id") != PROFILE_CASE_ID
        or _boolean(profile.get("preserved"), "profile preservation result") is not True
    ):
        raise ReleaseEvidenceV2Error("qualification-v2 must record an explicit preserved profile result.")
    if (
        _sha256(profile.get("receipt_sha256"), "profile preservation receipt SHA-256")
        != capture.signed_ui.receipt.file_sha256
    ):
        raise ReleaseEvidenceV2Error(
            "qualification-v2 profile preservation receipt conflicts with the required case receipt."
        )
    updater = _mapping(record.get("updater_route"), "qualification-v2 updater route")
    _exact_keys(updater, frozenset({"case_id", "receipt_sha256", "result"}), "qualification-v2 updater route")
    if updater.get("case_id") != "sparkle-update-route" or updater.get("result") != "passed":
        raise ReleaseEvidenceV2Error("qualification-v2 must record a passed updater-route result.")
    if (
        _sha256(updater.get("receipt_sha256"), "updater-route receipt SHA-256")
        != validated_receipts["sparkle-update-route"].sha256
    ):
        raise ReleaseEvidenceV2Error("qualification-v2 updater-route receipt conflicts with the required case receipt.")
    return QualificationV2(
        capture=capture_binding,
        qualification_sha256=qualification_sha256,
        qualified_at=qualified_at,
        release_tag=capture.release_tag,
        source_sha=capture.source_sha,
    )


def _validate_disposition(reader: _BundleReader, record: Mapping[str, Any], capture: CaptureV2) -> DispositionV2:
    failed_at, capture_binding, disposition_sha256 = _validate_terminal_common(
        record,
        capture=capture,
        record_type="disposition",
        state="FAILED",
        timestamp_field="failed_at",
        digest_field="disposition_sha256",
        expected_keys=DISPOSITION_KEYS,
    )
    workflow = _workflow_binding(
        record.get("failure_workflow"),
        "disposition-v2 failed workflow",
        expected_path=MILESTONE_WORKFLOW_PATH,
    )
    if workflow.actor != EXPECTED_MILESTONE_ACTOR:
        raise ReleaseEvidenceV2Error("disposition-v2 failed workflow actor is not the repository owner.")
    failure = _mapping(record.get("failure"), "disposition-v2 failure")
    _exact_keys(failure, frozenset({"code", "expected", "observed", "subject"}), "disposition-v2 failure")
    _identifier(failure.get("code"), "disposition-v2 failure code")
    for field in ("subject", "expected", "observed"):
        _string(failure.get(field), f"disposition-v2 failure {field}")
    preservation = _mapping(record.get("preservation"), "disposition-v2 preservation")
    _exact_keys(
        preservation,
        frozenset({"release_identity_preserved", "signed_artifact_preserved", "source_identity_preserved"}),
        "disposition-v2 preservation",
    )
    if not all(_boolean(preservation.get(field), f"disposition-v2 preservation {field}") for field in preservation):
        raise ReleaseEvidenceV2Error(
            "disposition-v2 preservation flags must make failure terminal for the exact identity."
        )
    return DispositionV2(
        capture=capture_binding,
        disposition_sha256=disposition_sha256,
        failed_at=failed_at,
        release_tag=capture.release_tag,
        source_sha=capture.source_sha,
    )


def validate_v2_bundle(
    repo_root: Path,
    release_tag: str,
    *,
    verification_revision: str | None = None,
    worktree: bool = False,
) -> dict[str, object]:
    repo_root = repo_root.resolve()
    tag = _tag(release_tag, "release tag")
    reader = _verification_reader(repo_root, verification_revision=verification_revision, worktree=worktree)
    bundle = f"{EVIDENCE_ROOT}/{tag}"
    capture_path = f"{bundle}/{CAPTURE_NAME}"
    qualification_path = f"{bundle}/{QUALIFICATION_NAME}"
    disposition_path = f"{bundle}/{DISPOSITION_NAME}"
    has_capture = reader.exists(capture_path)
    has_qualification = reader.exists(qualification_path)
    has_disposition = reader.exists(disposition_path)
    if not has_capture and (has_qualification or has_disposition):
        raise ReleaseEvidenceV2Error("Terminal v2 evidence requires a complete capture-v2 record.")
    if not has_capture:
        raise ReleaseEvidenceV2Error(f"Release evidence bundle {tag} has no v2 capture record.")
    if has_qualification and has_disposition:
        raise ReleaseEvidenceV2Error("Terminal v2 evidence is split-brain: qualification and disposition both exist.")
    capture = _validate_capture(reader, _load_record(reader, capture_path, "capture-v2 record"))
    if capture.release_tag != tag:
        raise ReleaseEvidenceV2Error("capture-v2 release tag conflicts with its bundle directory.")
    if has_qualification:
        qualification = _validate_qualification(
            reader, _load_record(reader, qualification_path, "qualification-v2 record"), capture
        )
        return {"class": "v2-qualified", "capture": capture, "terminal": qualification}
    if has_disposition:
        disposition = _validate_disposition(
            reader, _load_record(reader, disposition_path, "disposition-v2 record"), capture
        )
        return {"class": "v2-failed", "capture": capture, "terminal": disposition}
    return {"class": "v2-captured", "capture": capture}


def _read_worktree_json(repo_root: Path, relative_path: str, description: str) -> Mapping[str, Any]:
    target, _ = _repo_path(repo_root, relative_path, description)
    try:
        return _load_json_bytes(target.read_bytes(), description)
    except OSError as error:
        raise ReleaseEvidenceV2Error(f"Unable to read {description} at {target}: {error}") from error


def _legacy_release_receipt(repo_root: Path, release_tag: str) -> tuple[Mapping[str, Any], Path, str]:
    relative_path = f"{EVIDENCE_ROOT}/{release_tag}/{RECEIPT_NAME}"
    path, _ = _repo_path(repo_root, relative_path, "legacy release receipt")
    try:
        receipt, file_sha256 = load_validated_checked_receipt(path)
    except ReleaseReceiptError as error:
        raise ReleaseEvidenceV2Error(f"Legacy release receipt is invalid: {error}") from error
    return receipt, path, file_sha256


def _validate_legacy_publication(repo_root: Path, release_tag: str) -> None:
    receipt, _receipt_path, receipt_file_sha256 = _legacy_release_receipt(repo_root, release_tag)
    publication = _read_worktree_json(
        repo_root,
        f"{EVIDENCE_ROOT}/{release_tag}/publication-record.json",
        "legacy publication record",
    )
    base_keys = frozenset(
        {
            "live_pages",
            "published_at",
            "receipt_asset_id",
            "receipt_file_sha256",
            "release_id",
            "release_tag",
            "schema_version",
            "source_sha",
            "workflow_conclusion",
            "workflow_run_id",
        }
    )
    recovery_keys = base_keys | {"recovery_workflow_run"}
    backfill_keys = base_keys | {"immutable_release_receipt_asset", "note", "receipt_origin"}
    actual_keys = frozenset(publication)
    accepted_variants = {"base": base_keys, "recovery": recovery_keys, "backfill": backfill_keys}
    if actual_keys not in accepted_variants.values():
        variant_name, variant_keys = min(
            accepted_variants.items(),
            key=lambda item: len(item[1] - actual_keys) + len(actual_keys - item[1]),
        )
        missing = sorted(variant_keys - actual_keys)
        extra = sorted(actual_keys - variant_keys)
        raise ReleaseEvidenceV2Error(
            f"legacy publication record keys changed from nearest {variant_name} variant; "
            f"missing={missing}, extra={extra}."
        )
    if publication.get("schema_version") != 1:
        raise ReleaseEvidenceV2Error("Legacy publication record schema_version must be 1.")
    release = _mapping(receipt.get("release"), "legacy receipt release")
    workflow = _mapping(receipt.get("workflow"), "legacy receipt workflow")
    if {
        "release_tag": publication.get("release_tag"),
        "release_id": publication.get("release_id"),
        "source_sha": publication.get("source_sha"),
        "workflow_run_id": publication.get("workflow_run_id"),
    } != {
        "release_tag": release_tag,
        "release_id": release.get("id"),
        "source_sha": receipt.get("source_sha"),
        "workflow_run_id": workflow.get("run_id"),
    }:
        raise ReleaseEvidenceV2Error("Legacy publication record identity conflicts with its checked receipt.")
    if publication.get("receipt_file_sha256") != receipt_file_sha256:
        raise ReleaseEvidenceV2Error("Legacy publication receipt file digest conflicts with its checked receipt.")
    receipt_asset_id = publication.get("receipt_asset_id")
    if receipt_asset_id is not None:
        _positive_integer(receipt_asset_id, "legacy publication receipt asset ID")
    live_pages = _mapping(publication.get("live_pages"), "legacy publication live Pages")
    _exact_keys(live_pages, frozenset({"sha256", "state", "url"}), "legacy publication live Pages")
    if (
        live_pages.get("state") != "verified"
        or live_pages.get("url") != "https://cbusillo.github.io/BD_to_AVP/appcast.xml"
    ):
        raise ReleaseEvidenceV2Error("Legacy publication live Pages binding is not canonical.")
    if live_pages.get("sha256") != _receipt_artifact_digest(receipt, "appcast"):
        raise ReleaseEvidenceV2Error("Legacy publication live Pages digest conflicts with its checked receipt.")
    publication_at = _timestamp(publication.get("published_at"), "legacy publication published_at")
    release_at = _timestamp(release.get("created_at"), "legacy release publication timestamp")
    if publication_at < release_at:
        raise ReleaseEvidenceV2Error("Legacy publication timestamp precedes release publication.")
    conclusion = publication.get("workflow_conclusion")
    if conclusion not in {"success", "failure"}:
        raise ReleaseEvidenceV2Error("Legacy publication workflow conclusion is unsupported.")
    recovery = publication.get("recovery_workflow_run")
    if conclusion == "success" and recovery is not None:
        raise ReleaseEvidenceV2Error("Successful legacy publication must not carry recovery workflow data.")
    if conclusion == "failure" and not isinstance(recovery, Mapping):
        raise ReleaseEvidenceV2Error("Failed legacy publication must carry recovery workflow data.")
    if actual_keys == backfill_keys:
        if publication.get("immutable_release_receipt_asset") is not False:
            raise ReleaseEvidenceV2Error("Legacy backfilled publication must mark its receipt as non-asset evidence.")
        _string(publication.get("receipt_origin"), "legacy publication receipt origin")
        _string(publication.get("note"), "legacy publication note")


def _validate_legacy_qualification_manifest(repo_root: Path, release_tag: str) -> None:
    manifest_relative = f"{EVIDENCE_ROOT}/{release_tag}/qualification-manifest.json"
    manifest_path, _ = _repo_path(repo_root, manifest_relative, "legacy qualification manifest")
    try:
        load_validated_manifest(manifest_path, repo_root=repo_root)
    except ReleaseQualificationManifestError as error:
        raise ReleaseEvidenceV2Error(f"Legacy qualification manifest is invalid: {error}") from error


def _legacy_class(reader: _BundleReader, release_tag: str) -> str:
    if not reader.worktree:
        if reader.revision is None:
            raise ReleaseEvidenceV2Error("Verification revision is missing.")
        with _materialized_revision(reader.repo_root, reader.revision) as materialized_root:
            return _legacy_class(_BundleReader(materialized_root, None, True), release_tag)
    bundle = f"{EVIDENCE_ROOT}/{release_tag}"
    if reader.exists(f"{bundle}/failed-post-publication-qualification-v1.json"):
        try:
            validate_failed_post_publication_qualification_record(reader.repo_root, release_tag)
        except (OSError, ReleaseMilestoneContextError) as error:
            raise ReleaseEvidenceV2Error(f"Legacy failed post-publication record is invalid: {error}") from error
        return "legacy-failed-post-publication-v1"
    if reader.exists(f"{bundle}/qualification-manifest.json"):
        _validate_legacy_qualification_manifest(reader.repo_root, release_tag)
        return "legacy-qualification-manifest-v1"
    if reader.exists(f"{bundle}/publication-record.json"):
        _validate_legacy_publication(reader.repo_root, release_tag)
        return "legacy-publication-v1"
    if reader.exists(f"{bundle}/release-receipt.json"):
        _legacy_release_receipt(reader.repo_root, release_tag)
        return "legacy-receipt-v1"
    raise ReleaseEvidenceV2Error(f"Release evidence bundle {release_tag} has no recognized v1 or v2 evidence class.")


def verify_tag(
    repo_root: Path,
    release_tag: str,
    *,
    verification_revision: str | None = None,
    worktree: bool = False,
) -> dict[str, str]:
    repo_root = repo_root.resolve()
    tag = _tag(release_tag, "release tag")
    reader = _verification_reader(repo_root, verification_revision=verification_revision, worktree=worktree)
    bundle = f"{EVIDENCE_ROOT}/{tag}"
    if any(reader.exists(f"{bundle}/{name}") for name in (CAPTURE_NAME, QUALIFICATION_NAME, DISPOSITION_NAME)):
        result = validate_v2_bundle(
            repo_root, tag, verification_revision=reader.revision if not worktree else None, worktree=worktree
        )
        return {"class": cast(str, result["class"]), "release_tag": tag}
    return {"class": _legacy_class(reader, tag), "release_tag": tag}


def _tags_at_revision(reader: _BundleReader) -> list[str]:
    if reader.worktree:
        root = reader.repo_root / EVIDENCE_ROOT
        if not root.is_dir():
            raise ReleaseEvidenceV2Error(f"Release evidence root does not exist: {root}.")
        return sorted(path.name for path in root.iterdir() if path.is_dir() and TAG_PATTERN.fullmatch(path.name))
    if reader.revision is None:
        raise ReleaseEvidenceV2Error("Verification revision is missing.")
    names = (
        _git_run(
            reader.repo_root,
            ["ls-tree", "-d", "--name-only", f"{reader.revision}:{EVIDENCE_ROOT.as_posix()}"],
            "list evidence tags",
        )
        .decode()
        .splitlines()
    )
    return sorted(PurePosixPath(name).name for name in names if TAG_PATTERN.fullmatch(PurePosixPath(name).name))


def _v2_paths_at_revision(repo_root: Path, revision: str) -> set[str]:
    output = _git_run(
        repo_root, ["ls-tree", "-r", "--name-only", revision, "--", EVIDENCE_ROOT.as_posix()], "list evidence history"
    )
    return {
        path
        for path in output.decode().splitlines()
        if path.endswith((f"/{CAPTURE_NAME}", f"/{QUALIFICATION_NAME}", f"/{DISPOSITION_NAME}"))
    }


def verify_write_once_history(
    repo_root: Path,
    base_revision: str,
    *,
    verification_revision: str | None = None,
    worktree: bool = False,
) -> None:
    repo_root = repo_root.resolve()
    base = _resolve_revision(repo_root, base_revision)
    reader = _verification_reader(repo_root, verification_revision=verification_revision, worktree=worktree)
    target = (
        _git_run(repo_root, ["rev-parse", "HEAD"], "resolve worktree HEAD").decode().strip()
        if worktree
        else cast(str, reader.revision)
    )
    if (
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", base, target], cwd=repo_root, capture_output=True, check=False
        ).returncode
        != 0
    ):
        raise ReleaseEvidenceV2Error("Write-once base revision is not an ancestor of the verification revision.")
    for relative_path in sorted(_v2_paths_at_revision(repo_root, base)):
        baseline = _git_run(repo_root, ["show", f"{base}:{relative_path}"], "read write-once baseline")
        if not reader.exists(relative_path):
            raise ReleaseEvidenceV2Error(f"Write-once v2 evidence was removed: {relative_path}.")
        current = reader.read(relative_path, "write-once evidence")
        if current != baseline:
            raise ReleaseEvidenceV2Error(f"Write-once v2 evidence changed after base revision: {relative_path}.")


def verify_all_tags(
    repo_root: Path,
    *,
    verification_revision: str | None = None,
    base_revision: str | None = None,
    worktree: bool = False,
) -> list[dict[str, str]]:
    repo_root = repo_root.resolve()
    reader = _verification_reader(repo_root, verification_revision=verification_revision, worktree=worktree)
    results = [
        verify_tag(repo_root, tag, verification_revision=reader.revision if not worktree else None, worktree=worktree)
        for tag in _tags_at_revision(reader)
    ]
    if base_revision is not None:
        verify_write_once_history(
            repo_root,
            base_revision,
            verification_revision=reader.revision if not worktree else None,
            worktree=worktree,
        )
    return results


def _parse_arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify passive release-evidence v2 and explicit historical v1 classes offline."
    )
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--tag")
    selection.add_argument("--all-tags", action="store_true")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--revision")
    source.add_argument("--worktree", action="store_true")
    parser.add_argument("--base-revision")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_arguments(argv)
    try:
        if args.tag:
            results = [
                verify_tag(
                    args.repo_root,
                    args.tag,
                    verification_revision=args.revision,
                    worktree=args.worktree,
                )
            ]
            if args.base_revision is not None:
                verify_write_once_history(
                    args.repo_root,
                    args.base_revision,
                    verification_revision=args.revision,
                    worktree=args.worktree,
                )
        else:
            results = verify_all_tags(
                args.repo_root,
                verification_revision=args.revision,
                base_revision=args.base_revision,
                worktree=args.worktree,
            )
    except ReleaseEvidenceV2Error as error:
        print(f"release-evidence-v2: {error}", file=sys.stderr)
        return 1
    print(json.dumps({"verified": results}, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
