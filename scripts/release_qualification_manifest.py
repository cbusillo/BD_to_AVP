from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import tempfile
import zipfile

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from scripts.release import ReleaseError, parse_release_version
from scripts.release_evidence import ReleaseEvidenceError, effective_successful_workflow_run_id
from scripts.release_receipt import ReleaseReceiptError, load_validated_checked_receipt
from scripts.signed_artifact_receipt import (
    PROFILE_CASE_ID,
    SignedArtifactReceiptError,
    validate_receipt_files,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = 1
MANIFEST_TYPE = "bd-to-avp-release-qualification"
DEFAULT_POLICY_PATH = Path("docs/qualification/release-qualification-policy-v1.json")
DEFAULT_EVIDENCE_PATH = Path("docs/qualification/release-evidence-v1.json")
DEFAULT_ROUTE_TABLE_PATH = Path("docs/qualification/video-quality-route-table-v2.json")
CONTROLLER_RUNNER_PATH = Path(".github/workflows/milestone-qualification.yml")
SIGNAL_RECEIPT_NAME = "signed-artifact-ui-receipt.json"
SIGNAL_ARCHIVE_NAME = "signed-artifact-ui.zip"
MANIFEST_NAME = "qualification-manifest.json"
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
REF_PATTERN = re.compile(r"^automation/release-evidence-v[0-9A-Za-z][0-9A-Za-z.-]*$")
SPARKLE_ROUTES = {"stable", "rc", "beta", "alpha"}
PRIVATE_FIELD_FRAGMENTS = (
    "absolute",
    "certificate",
    "diagnostic",
    "home",
    "hostname",
    "keychain",
    "password",
    "private",
    "secret",
    "serial",
    "token",
    "username",
)
MANIFEST_KEYS = frozenset(
    {
        "artifacts",
        "canonical_evidence",
        "case_classifications",
        "candidate",
        "input_digests",
        "manifest_sha256",
        "manifest_type",
        "paths",
        "prior",
        "release",
        "release_receipt",
        "runner_sha",
        "schema_version",
        "signed_ui_artifact",
        "workflow",
    }
)


class ReleaseQualificationManifestError(RuntimeError):
    pass


@dataclass(frozen=True)
class SignedUiArtifactBinding:
    artifact_id: int
    artifact_digest: str
    archive_path: str
    receipt_path: str
    receipt_file_sha256: str
    receipt_sha256: str


def _mapping(value: object, description: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ReleaseQualificationManifestError(f"{description} must be a JSON object.")
    return cast(Mapping[str, Any], value)


def _sequence(value: object, description: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise ReleaseQualificationManifestError(f"{description} must be a JSON array.")
    return cast(Sequence[Any], value)


def _exact_keys(value: Mapping[str, Any], expected: set[str], description: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ReleaseQualificationManifestError(f"{description} keys changed; missing={missing}, extra={extra}.")


def _string(value: object, description: str) -> str:
    if not isinstance(value, str) or not value:
        raise ReleaseQualificationManifestError(f"{description} must be a non-empty string.")
    return value


def _integer(value: object, description: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ReleaseQualificationManifestError(f"{description} must be a positive integer.")
    return value


def _sha(value: object, description: str) -> str:
    text = _string(value, description)
    if SHA_PATTERN.fullmatch(text) is None:
        raise ReleaseQualificationManifestError(f"{description} must be a full lowercase Git SHA.")
    return text


def _sha256(value: object, description: str) -> str:
    text = _string(value, description)
    if SHA256_PATTERN.fullmatch(text) is None:
        raise ReleaseQualificationManifestError(f"{description} must be a lowercase SHA-256 digest.")
    return text


def _load_json(path: Path, description: str) -> Mapping[str, Any]:
    try:
        return _mapping(json.loads(path.read_text(encoding="utf-8")), description)
    except OSError as error:
        raise ReleaseQualificationManifestError(f"Unable to read {description} at {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise ReleaseQualificationManifestError(f"Invalid JSON in {description} at {path}: {error}") from error


def _repo_path(repo_root: Path, value: object, description: str) -> tuple[Path, str]:
    text = _string(value, description)
    relative = Path(text)
    if relative.is_absolute() or ".." in relative.parts or text.startswith("./"):
        raise ReleaseQualificationManifestError(f"{description} must be a canonical repository-relative path.")
    path = repo_root / relative
    try:
        path.resolve().relative_to(repo_root.resolve())
    except ValueError as error:
        raise ReleaseQualificationManifestError(f"{description} must stay inside the repository.") from error
    return path, relative.as_posix()


def _file_sha256(path: Path, description: str) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as error:
        raise ReleaseQualificationManifestError(f"Unable to read {description} at {path}: {error}") from error


def _file_sha256_at_revision(
    repo_root: Path,
    revision: str,
    relative_path: str,
    description: str,
) -> str:
    result = subprocess.run(
        ["git", "show", f"{revision}:{relative_path}"],
        cwd=repo_root,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise ReleaseQualificationManifestError(f"Unable to read {description} from revision {revision}.")
    return hashlib.sha256(result.stdout).hexdigest()


def _artifact(receipt: Mapping[str, Any], kind: str) -> Mapping[str, Any]:
    matches = [
        _mapping(item, f"release receipt {kind} artifact")
        for item in _sequence(receipt.get("artifacts"), "release receipt artifacts")
        if _mapping(item, "release receipt artifact").get("kind") == kind
    ]
    if len(matches) != 1:
        raise ReleaseQualificationManifestError(f"Release receipt must contain exactly one {kind} artifact.")
    return matches[0]


def canonical_payload_bytes(manifest: Mapping[str, Any]) -> bytes:
    payload = dict(manifest)
    payload.pop("manifest_sha256", None)
    return (json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


def manifest_sha256(manifest: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_payload_bytes(manifest)).hexdigest()


def _reject_private_fields(value: object, path: str = "manifest") -> None:
    if isinstance(value, Mapping):
        for raw_key, raw_child in value.items():
            if not isinstance(raw_key, str) or not raw_key:
                raise ReleaseQualificationManifestError(f"{path} contains a non-string key.")
            lowered = raw_key.lower()
            if any(fragment in lowered for fragment in PRIVATE_FIELD_FRAGMENTS):
                raise ReleaseQualificationManifestError(f"{path}.{raw_key} is not a public-safe manifest field name.")
            _reject_private_fields(raw_child, f"{path}.{raw_key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_private_fields(item, f"{path}[{index}]")
    elif isinstance(value, str):
        if value.startswith("/") or value.startswith("~/"):
            raise ReleaseQualificationManifestError(f"{path} contains an absolute or home-relative path.")


def _load_canonical_manifest(path: Path) -> Mapping[str, Any]:
    manifest = _load_json(path, "release qualification manifest")
    expected_content = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    try:
        actual_content = path.read_bytes()
    except OSError as error:
        raise ReleaseQualificationManifestError(
            f"Unable to read release qualification manifest at {path}: {error}"
        ) from error
    if actual_content != expected_content:
        raise ReleaseQualificationManifestError("Manifest file must use deterministic sorted JSON serialization.")
    _reject_private_fields(manifest)
    _exact_keys(manifest, set(MANIFEST_KEYS), "release qualification manifest")
    if manifest.get("schema_version") != SCHEMA_VERSION or manifest.get("manifest_type") != MANIFEST_TYPE:
        raise ReleaseQualificationManifestError("Unsupported release qualification manifest schema or type.")
    recorded_digest = _sha256(manifest.get("manifest_sha256"), "manifest self SHA-256")
    if recorded_digest != manifest_sha256(manifest):
        raise ReleaseQualificationManifestError("Release qualification manifest self-digest mismatch.")
    return manifest


def validate_manifest_evidence_history(
    manifest: Mapping[str, Any],
    *,
    repo_root: Path,
    base_revision: str,
) -> None:
    base_revision = _sha(base_revision, "evidence base revision")
    evidence = _mapping(manifest.get("canonical_evidence"), "manifest canonical evidence")
    if evidence.get("base_sha") != base_revision:
        raise ReleaseQualificationManifestError(
            "Manifest evidence base revision does not match the requested history checkpoint."
        )
    paths = _mapping(manifest.get("paths"), "manifest paths")
    _evidence_path, evidence_relative = _repo_path(
        repo_root,
        paths.get("evidence_index"),
        "manifest evidence index path",
    )
    result = subprocess.run(
        ["git", "show", f"{base_revision}:{evidence_relative}"],
        cwd=repo_root,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise ReleaseQualificationManifestError(
            "Unable to read the qualification evidence baseline from the manifest checkpoint."
        )
    input_digests = _mapping(manifest.get("input_digests"), "manifest input digests")
    if hashlib.sha256(result.stdout).hexdigest() != input_digests.get("evidence_index_base"):
        raise ReleaseQualificationManifestError(
            "Manifest evidence index baseline digest conflicts with the manifest checkpoint."
        )
    try:
        base_document = _mapping(json.loads(result.stdout), "baseline qualification evidence")
    except json.JSONDecodeError as error:
        raise ReleaseQualificationManifestError(f"Invalid JSON in baseline qualification evidence: {error}") from error
    head_document = _load_json(repo_root / evidence_relative, "qualification evidence")
    if base_document.get("schema_version") != 1 or head_document.get("schema_version") != 1:
        raise ReleaseQualificationManifestError("Qualification evidence schema_version must remain 1.")
    base_receipts = list(_sequence(base_document.get("receipts"), "baseline qualification receipts"))
    head_receipts = list(_sequence(head_document.get("receipts"), "qualification receipts"))
    if len(head_receipts) < len(base_receipts) or head_receipts[: len(base_receipts)] != base_receipts:
        raise ReleaseQualificationManifestError(
            "Qualification evidence may only append receipts after the manifest checkpoint."
        )


def _case_classifications(policy: Mapping[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for raw_case in _sequence(policy.get("cases"), "release qualification cases"):
        case = _mapping(raw_case, "release qualification case")
        case_id = _string(case.get("id"), "release qualification case id")
        invalidates_on = _mapping(case.get("invalidates_on"), f"case {case_id} invalidates_on")
        tier = case.get("tier")
        if not isinstance(tier, int) or isinstance(tier, bool) or tier not in range(5):
            raise ReleaseQualificationManifestError(f"case {case_id} tier must be 0 through 4.")
        records.append(
            {
                "allowed_evidence_sources": sorted(
                    _string(source, f"case {case_id} evidence source")
                    for source in _sequence(case.get("allowed_evidence_sources"), f"case {case_id} evidence sources")
                ),
                "blocking_phase": _string(case.get("blocking_phase"), f"case {case_id} blocking_phase"),
                "contracts": sorted(
                    _string(contract, f"case {case_id} invalidating contract")
                    for contract in _sequence(invalidates_on.get("contracts"), f"case {case_id} invalidating contracts")
                ),
                "id": case_id,
                "release_blocking": bool(case.get("release_blocking", case.get("blocking_phase") != "none")),
                "tier": tier,
            }
        )
    return sorted(records, key=lambda item: cast(str, item["id"]))


def _canonical_qualification_path(repo_root: Path, release_tag: str) -> str:
    matches: list[str] = []
    for path in sorted((repo_root / "docs" / "qualification").glob("*-signed-qualification-v1.json")):
        document = _load_json(path, "signed qualification")
        candidate = document.get("candidate")
        if isinstance(candidate, Mapping) and candidate.get("release_tag") == release_tag:
            matches.append(path.relative_to(repo_root).as_posix())
    if len(matches) != 1:
        raise ReleaseQualificationManifestError(
            f"Expected exactly one canonical qualification record for {release_tag}; found {len(matches)}."
        )
    return matches[0]


def _policy_checkpoint_digest(policy: Mapping[str, Any]) -> str:
    checkpoints = {
        "case_classifications": _case_classifications(policy),
        "policy_id": _string(policy.get("policy_id"), "policy_id"),
        "result_states": list(_sequence(policy.get("result_states"), "result states")),
        "schema_version": policy.get("schema_version"),
    }
    return hashlib.sha256(json.dumps(checkpoints, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _publication_record(repo_root: Path, release_tag: str) -> Mapping[str, Any]:
    path = repo_root / "docs" / "release-evidence" / release_tag / "publication-record.json"
    publication = _load_json(path, "publication record")
    if effective_successful_workflow_run_id(publication) is None:
        raise ReleaseQualificationManifestError(
            "Publication record does not bind a successful release or recovery run."
        )
    return publication


def signed_ui_binding_from_files(
    *,
    repo_root: Path,
    release_receipt_path: Path,
    receipt: Mapping[str, Any],
    release_receipt_asset_id: int,
    release_receipt_file_sha256: str,
    signed_ui_artifact_id: int,
    signed_ui_artifact_digest: str,
    signed_ui_archive_path: Path,
    signed_ui_receipt_path: Path,
    signed_ui_receipt_file_sha256: str,
) -> SignedUiArtifactBinding:
    release = _mapping(receipt.get("release"), "release receipt release")
    workflow = _mapping(receipt.get("workflow"), "release receipt workflow")
    release_tag = _string(release.get("tag"), "release tag")
    expected_relative = f"docs/release-evidence/{release_tag}/{SIGNAL_RECEIPT_NAME}"
    receipt_path, receipt_relative = _repo_path(repo_root, expected_relative, "signed UI receipt path")
    if signed_ui_receipt_path.resolve() != receipt_path.resolve():
        raise ReleaseQualificationManifestError(f"Signed UI receipt must be checked at {expected_relative!r}.")
    expected_archive_relative = f"docs/release-evidence/{release_tag}/{SIGNAL_ARCHIVE_NAME}"
    archive_path, archive_relative = _repo_path(
        repo_root,
        expected_archive_relative,
        "signed UI artifact archive path",
    )
    if signed_ui_archive_path.resolve() != archive_path.resolve():
        raise ReleaseQualificationManifestError(
            f"Signed UI artifact archive must be checked at {expected_archive_relative!r}."
        )
    expected_artifact_digest = _sha256(signed_ui_artifact_digest, "signed UI artifact digest")
    if _file_sha256(signed_ui_archive_path, "signed UI artifact archive") != expected_artifact_digest:
        raise ReleaseQualificationManifestError(
            "Signed UI artifact archive digest does not match the GitHub Actions artifact digest."
        )
    try:
        receipt_bytes = signed_ui_receipt_path.read_bytes()
        with zipfile.ZipFile(signed_ui_archive_path) as archive:
            files = [item for item in archive.infolist() if not item.is_dir()]
            if (
                len(files) != 1
                or files[0].filename != SIGNAL_RECEIPT_NAME
                or files[0].file_size != len(receipt_bytes)
                or files[0].file_size > 1024 * 1024
            ):
                raise ReleaseQualificationManifestError(
                    "Signed UI artifact archive must contain exactly the bounded canonical receipt file."
                )
            archived_receipt = archive.read(files[0])
    except (OSError, zipfile.BadZipFile) as error:
        raise ReleaseQualificationManifestError(f"Signed UI artifact archive is invalid: {error}") from error
    if archived_receipt != receipt_bytes:
        raise ReleaseQualificationManifestError(
            "Checked signed UI receipt does not match the exact GitHub Actions artifact payload."
        )
    try:
        signed_receipt = validate_receipt_files(
            receipt_path=signed_ui_receipt_path,
            release_receipt_path=release_receipt_path,
            policy_path=repo_root / DEFAULT_POLICY_PATH,
            case_id=PROFILE_CASE_ID,
            workflow_run_id=_integer(workflow.get("run_id"), "workflow run ID"),
            workflow_run_attempt=_integer(workflow.get("run_attempt"), "workflow run attempt"),
            release_receipt_asset_id=release_receipt_asset_id,
            release_receipt_file_sha256=release_receipt_file_sha256,
            receipt_file_sha256=signed_ui_receipt_file_sha256,
        )
    except SignedArtifactReceiptError as error:
        raise ReleaseQualificationManifestError(f"Signed UI receipt is invalid: {error}") from error
    return SignedUiArtifactBinding(
        artifact_id=_integer(signed_ui_artifact_id, "signed UI artifact ID"),
        artifact_digest=expected_artifact_digest,
        archive_path=archive_relative,
        receipt_path=receipt_relative,
        receipt_file_sha256=_sha256(signed_ui_receipt_file_sha256, "signed UI receipt file SHA-256"),
        receipt_sha256=_sha256(signed_receipt.get("receipt_sha256"), "signed UI receipt self SHA-256"),
    )


def build_manifest(
    *,
    repo_root: Path,
    release_receipt_path: Path,
    prior_release_receipt_path: Path,
    signed_ui: SignedUiArtifactBinding,
    evidence_ref: str,
    evidence_base_sha: str,
    runner_sha: str,
    sparkle_route: str,
    policy_path: Path = DEFAULT_POLICY_PATH,
    evidence_path: Path = DEFAULT_EVIDENCE_PATH,
    qualification_path: Path = Path("docs/qualification/stable-signed-qualification-v1.json"),
    route_table_path: Path = DEFAULT_ROUTE_TABLE_PATH,
    controller_runner_path: Path = CONTROLLER_RUNNER_PATH,
) -> dict[str, Any]:
    repo_root = repo_root.resolve()
    policy_path, policy_relative = _repo_path(repo_root, policy_path.as_posix(), "policy path")
    evidence_path, evidence_relative = _repo_path(repo_root, evidence_path.as_posix(), "evidence path")
    qualification_path, qualification_relative = _repo_path(
        repo_root,
        qualification_path.as_posix(),
        "qualification record path",
    )
    route_table_path, route_table_relative = _repo_path(repo_root, route_table_path.as_posix(), "route table path")
    controller_runner_path, controller_runner_relative = _repo_path(
        repo_root,
        controller_runner_path.as_posix(),
        "controller runner path",
    )
    release_receipt_path = release_receipt_path.resolve()
    prior_release_receipt_path = prior_release_receipt_path.resolve()
    try:
        release_receipt_relative = release_receipt_path.relative_to(repo_root).as_posix()
        prior_receipt_relative = prior_release_receipt_path.relative_to(repo_root).as_posix()
    except ValueError as error:
        raise ReleaseQualificationManifestError("Manifest receipts must be inside the repository.") from error

    try:
        receipt, release_receipt_file_sha256 = load_validated_checked_receipt(release_receipt_path)
        prior_receipt, prior_receipt_file_sha256 = load_validated_checked_receipt(prior_release_receipt_path)
    except ReleaseReceiptError as error:
        raise ReleaseQualificationManifestError(f"Release receipt is invalid: {error}") from error
    release = _mapping(receipt.get("release"), "release receipt release")
    prior_release = _mapping(prior_receipt.get("release"), "prior release receipt release")
    versions = _mapping(receipt.get("versions"), "release receipt versions")
    workflow = _mapping(receipt.get("workflow"), "release receipt workflow")
    release_tag = _string(release.get("tag"), "release tag")
    prior_tag = _string(prior_release.get("tag"), "prior release tag")
    if release_receipt_relative != f"docs/release-evidence/{release_tag}/release-receipt.json":
        raise ReleaseQualificationManifestError("Candidate release receipt path does not match its release tag.")
    if prior_receipt_relative != f"docs/release-evidence/{prior_tag}/release-receipt.json":
        raise ReleaseQualificationManifestError("Prior release receipt path does not match its release tag.")
    if prior_tag == release_tag or prior_receipt.get("source_sha") == receipt.get("source_sha"):
        raise ReleaseQualificationManifestError("Candidate and prior release receipts must be distinct.")
    if evidence_ref != f"automation/release-evidence-{release_tag}" or REF_PATTERN.fullmatch(evidence_ref) is None:
        raise ReleaseQualificationManifestError("Evidence ref does not match the candidate release tag.")
    if sparkle_route not in SPARKLE_ROUTES:
        raise ReleaseQualificationManifestError(f"Sparkle route must be one of {sorted(SPARKLE_ROUTES)}.")
    try:
        parsed_version = parse_release_version(_string(versions.get("package"), "package version"))
    except ReleaseError as error:
        raise ReleaseQualificationManifestError(f"Candidate package version is invalid: {error}") from error

    policy = _load_json(policy_path, "release qualification policy")
    publication = _publication_record(repo_root, release_tag)
    if publication.get("receipt_file_sha256") != release_receipt_file_sha256:
        raise ReleaseQualificationManifestError("Publication record receipt digest does not match the checked receipt.")
    if publication.get("source_sha") != receipt.get("source_sha") or publication.get("release_id") != release.get("id"):
        raise ReleaseQualificationManifestError("Publication record identity conflicts with the release receipt.")
    if signed_ui.receipt_path != f"docs/release-evidence/{release_tag}/{SIGNAL_RECEIPT_NAME}":
        raise ReleaseQualificationManifestError("Signed UI receipt path does not match the candidate release tag.")
    dmg = _artifact(receipt, "dmg")
    checksum = _artifact(receipt, "checksum")
    appcast = _artifact(receipt, "appcast")
    manifest: dict[str, Any] = {
        "artifacts": {
            "appcast": {
                "asset_id": _integer(appcast.get("asset_id"), "appcast asset ID"),
                "kind": "appcast",
                "name": _string(appcast.get("name"), "appcast name"),
                "sha256": _sha256(appcast.get("sha256"), "appcast SHA-256"),
                "size_bytes": _integer(appcast.get("size_bytes"), "appcast size"),
            },
            "checksum": {
                "asset_id": _integer(checksum.get("asset_id"), "checksum asset ID"),
                "kind": "checksum",
                "name": _string(checksum.get("name"), "checksum name"),
                "sha256": _sha256(checksum.get("sha256"), "checksum SHA-256"),
                "size_bytes": _integer(checksum.get("size_bytes"), "checksum size"),
            },
            "dmg": {
                "asset_id": _integer(dmg.get("asset_id"), "DMG asset ID"),
                "kind": "dmg",
                "name": _string(dmg.get("name"), "DMG name"),
                "sha256": _sha256(dmg.get("sha256"), "DMG SHA-256"),
                "size_bytes": _integer(dmg.get("size_bytes"), "DMG size"),
            },
            "signed_app_tree_sha256": _sha256(receipt.get("signed_app_tree_sha256"), "signed app tree SHA-256"),
        },
        "canonical_evidence": {
            "base_sha": _sha(evidence_base_sha, "evidence base SHA"),
            "ref": evidence_ref,
        },
        "case_classifications": _case_classifications(policy),
        "candidate": {
            "build_version": _string(versions.get("build"), "build version"),
            "package_version": _string(versions.get("package"), "package version"),
            "public_version": _string(versions.get("public"), "public version"),
            "release_tag": release_tag,
            "source_sha": _sha(receipt.get("source_sha"), "source SHA"),
        },
        "input_digests": {
            "controller_runner": _file_sha256(controller_runner_path, "controller runner"),
            "evidence_index_base": _file_sha256_at_revision(
                repo_root,
                evidence_base_sha,
                evidence_relative,
                "qualification evidence baseline",
            ),
            "policy": _file_sha256(policy_path, "release qualification policy"),
            "policy_checkpoint": _policy_checkpoint_digest(policy),
            "route_table": _file_sha256(route_table_path, "video route table"),
        },
        "manifest_type": MANIFEST_TYPE,
        "paths": {
            "controller_runner": controller_runner_relative,
            "evidence_index": evidence_relative,
            "policy": policy_relative,
            "qualification": qualification_relative,
            "route_table": route_table_relative,
        },
        "prior": {
            "release_receipt": {
                "file_sha256": prior_receipt_file_sha256,
                "path": prior_receipt_relative,
                "receipt_sha256": _sha256(prior_receipt.get("receipt_sha256"), "prior receipt self SHA-256"),
            },
            "release_tag": prior_tag,
            "source_sha": _sha(prior_receipt.get("source_sha"), "prior source SHA"),
        },
        "release": {
            "id": _integer(release.get("id"), "release ID"),
            "route": _string(receipt.get("release_route"), "release route"),
            "sparkle_route": sparkle_route,
        },
        "release_receipt": {
            "asset_id": _integer(publication.get("receipt_asset_id"), "release receipt asset ID"),
            "file_sha256": release_receipt_file_sha256,
            "path": release_receipt_relative,
            "receipt_sha256": _sha256(receipt.get("receipt_sha256"), "release receipt self SHA-256"),
        },
        "schema_version": SCHEMA_VERSION,
        "signed_ui_artifact": {
            "archive_path": signed_ui.archive_path,
            "artifact_id": signed_ui.artifact_id,
            "artifact_sha256": signed_ui.artifact_digest,
            "receipt_file_sha256": signed_ui.receipt_file_sha256,
            "receipt_path": signed_ui.receipt_path,
            "receipt_sha256": signed_ui.receipt_sha256,
        },
        "workflow": {
            "actor": _string(workflow.get("actor"), "workflow actor"),
            "engine_path": _string(workflow.get("engine_path"), "workflow engine path"),
            "name": _string(workflow.get("name"), "workflow name"),
            "path": _string(workflow.get("path"), "workflow path"),
            "run_attempt": _integer(workflow.get("run_attempt"), "workflow run attempt"),
            "run_id": _integer(workflow.get("run_id"), "workflow run ID"),
        },
    }
    manifest["runner_sha"] = _sha(runner_sha, "runner SHA")
    if parsed_version.release_tag != release_tag:
        raise ReleaseQualificationManifestError("Candidate version and tag conflict.")
    manifest["manifest_sha256"] = manifest_sha256(manifest)
    validate_manifest(manifest, repo_root=repo_root)
    return manifest


def validate_manifest(manifest: Mapping[str, Any], *, repo_root: Path = REPO_ROOT) -> None:
    _validate_manifest(manifest, repo_root=repo_root, validate_signed_ui_receipt=False)


def _validate_manifest(
    manifest: Mapping[str, Any],
    *,
    repo_root: Path = REPO_ROOT,
    validate_signed_ui_receipt: bool,
) -> None:
    _reject_private_fields(manifest)
    _exact_keys(manifest, set(MANIFEST_KEYS), "release qualification manifest")
    if manifest.get("schema_version") != SCHEMA_VERSION or manifest.get("manifest_type") != MANIFEST_TYPE:
        raise ReleaseQualificationManifestError("Unsupported release qualification manifest schema or type.")
    candidate = _mapping(manifest.get("candidate"), "manifest candidate")
    _exact_keys(
        candidate,
        {"build_version", "package_version", "public_version", "release_tag", "source_sha"},
        "manifest candidate",
    )
    release_tag = _string(candidate.get("release_tag"), "candidate release tag")
    source_sha = _sha(candidate.get("source_sha"), "candidate source SHA")
    release = _mapping(manifest.get("release"), "manifest release")
    _exact_keys(release, {"id", "route", "sparkle_route"}, "manifest release")
    _integer(release.get("id"), "manifest release ID")
    if release.get("route") not in {"stable", "prerelease"}:
        raise ReleaseQualificationManifestError("Manifest release route is unsupported.")
    if release.get("sparkle_route") not in SPARKLE_ROUTES:
        raise ReleaseQualificationManifestError("Manifest Sparkle route is unsupported.")
    try:
        parsed_version = parse_release_version(_string(candidate.get("package_version"), "candidate package version"))
    except ReleaseError as error:
        raise ReleaseQualificationManifestError(f"Manifest candidate version is invalid: {error}") from error
    if candidate.get("public_version") != parsed_version.public_version or release_tag != parsed_version.release_tag:
        raise ReleaseQualificationManifestError("Manifest candidate version identity conflicts.")

    evidence = _mapping(manifest.get("canonical_evidence"), "manifest canonical evidence")
    _exact_keys(evidence, {"base_sha", "ref"}, "manifest canonical evidence")
    if (
        evidence.get("ref") != f"automation/release-evidence-{release_tag}"
        or REF_PATTERN.fullmatch(_string(evidence.get("ref"), "evidence ref")) is None
    ):
        raise ReleaseQualificationManifestError("Manifest evidence ref conflicts with candidate tag.")
    _sha(evidence.get("base_sha"), "evidence base SHA")
    _sha(manifest.get("runner_sha"), "manifest runner SHA")

    paths = _mapping(manifest.get("paths"), "manifest paths")
    _exact_keys(
        paths,
        {"controller_runner", "evidence_index", "policy", "qualification", "route_table"},
        "manifest paths",
    )
    path_values: dict[str, str] = {}
    for key in paths:
        path, relative = _repo_path(repo_root, paths[key], f"manifest paths.{key}")
        if not path.is_file():
            raise ReleaseQualificationManifestError(f"Manifest path {relative!r} does not exist.")
        path_values[key] = relative
    expected_paths = {
        "controller_runner": CONTROLLER_RUNNER_PATH.as_posix(),
        "evidence_index": DEFAULT_EVIDENCE_PATH.as_posix(),
        "policy": DEFAULT_POLICY_PATH.as_posix(),
        "qualification": _canonical_qualification_path(repo_root, release_tag),
        "route_table": DEFAULT_ROUTE_TABLE_PATH.as_posix(),
    }
    if path_values != expected_paths:
        raise ReleaseQualificationManifestError("Manifest paths must use the canonical qualification inputs.")

    digests = _mapping(manifest.get("input_digests"), "manifest input digests")
    _exact_keys(
        digests,
        {"controller_runner", "evidence_index_base", "policy", "policy_checkpoint", "route_table"},
        "manifest input digests",
    )
    for key in ("controller_runner", "policy", "route_table"):
        expected = _file_sha256(repo_root / path_values[key], f"manifest path {key}")
        if digests.get(key) != expected:
            raise ReleaseQualificationManifestError(f"Manifest {key} digest conflicts with checked content.")
    _sha256(digests.get("evidence_index_base"), "manifest evidence index baseline digest")
    policy = _load_json(repo_root / path_values["policy"], "release qualification policy")
    if digests.get("policy_checkpoint") != _policy_checkpoint_digest(policy):
        raise ReleaseQualificationManifestError("Manifest policy checkpoint digest conflicts with checked policy.")
    if manifest.get("case_classifications") != _case_classifications(policy):
        raise ReleaseQualificationManifestError("Manifest case classifications conflict with checked policy.")

    release_receipt = _mapping(manifest.get("release_receipt"), "manifest release receipt")
    _exact_keys(release_receipt, {"asset_id", "file_sha256", "path", "receipt_sha256"}, "manifest release receipt")
    receipt_path, receipt_relative = _repo_path(repo_root, release_receipt.get("path"), "manifest release receipt path")
    if receipt_relative != f"docs/release-evidence/{release_tag}/release-receipt.json":
        raise ReleaseQualificationManifestError("Manifest release receipt path conflicts with candidate tag.")
    try:
        receipt, receipt_file_sha256 = load_validated_checked_receipt(receipt_path)
    except ReleaseReceiptError as error:
        raise ReleaseQualificationManifestError(f"Manifest release receipt is invalid: {error}") from error
    receipt_self_digest = receipt.get("receipt_sha256")
    if (
        release_receipt.get("file_sha256") != receipt_file_sha256
        or release_receipt.get("receipt_sha256") != receipt_self_digest
    ):
        raise ReleaseQualificationManifestError("Manifest release receipt digests conflict with checked receipt.")
    receipt_release = _mapping(receipt.get("release"), "checked receipt release")
    if receipt.get("source_sha") != source_sha or receipt_release.get("id") != release.get("id"):
        raise ReleaseQualificationManifestError("Manifest release identity conflicts with checked receipt.")
    receipt_versions = _mapping(receipt.get("versions"), "checked receipt versions")
    expected_candidate = {
        "build_version": receipt_versions.get("build"),
        "package_version": receipt_versions.get("package"),
        "public_version": receipt_versions.get("public"),
        "release_tag": receipt_release.get("tag"),
        "source_sha": receipt.get("source_sha"),
    }
    if candidate != expected_candidate:
        raise ReleaseQualificationManifestError("Manifest candidate fields conflict with checked receipt.")
    if release.get("route") != receipt.get("release_route"):
        raise ReleaseQualificationManifestError("Manifest release route conflicts with checked receipt.")

    prior = _mapping(manifest.get("prior"), "manifest prior")
    _exact_keys(prior, {"release_receipt", "release_tag", "source_sha"}, "manifest prior")
    prior_tag = _string(prior.get("release_tag"), "manifest prior tag")
    if prior_tag == release_tag:
        raise ReleaseQualificationManifestError("Manifest candidate and prior tags conflict.")
    _sha(prior.get("source_sha"), "manifest prior source SHA")
    prior_release_receipt = _mapping(prior.get("release_receipt"), "manifest prior release receipt")
    _exact_keys(prior_release_receipt, {"file_sha256", "path", "receipt_sha256"}, "manifest prior release receipt")
    prior_path, prior_relative = _repo_path(repo_root, prior_release_receipt.get("path"), "manifest prior receipt path")
    if prior_relative != f"docs/release-evidence/{prior_tag}/release-receipt.json":
        raise ReleaseQualificationManifestError("Manifest prior receipt path conflicts with prior tag.")
    try:
        prior_receipt, prior_file_sha256 = load_validated_checked_receipt(prior_path)
    except ReleaseReceiptError as error:
        raise ReleaseQualificationManifestError(f"Manifest prior receipt is invalid: {error}") from error
    if prior_release_receipt.get("file_sha256") != prior_file_sha256 or prior_release_receipt.get(
        "receipt_sha256"
    ) != prior_receipt.get("receipt_sha256"):
        raise ReleaseQualificationManifestError("Manifest prior receipt digests conflict with checked receipt.")
    prior_release = _mapping(prior_receipt.get("release"), "checked prior receipt release")
    if prior_tag != prior_release.get("tag") or prior.get("source_sha") != prior_receipt.get("source_sha"):
        raise ReleaseQualificationManifestError("Manifest prior identity conflicts with checked receipt.")
    prior_versions = _mapping(prior_receipt.get("versions"), "checked prior receipt versions")
    try:
        prior_version = parse_release_version(_string(prior_versions.get("package"), "prior package version"))
    except ReleaseError as error:
        raise ReleaseQualificationManifestError(f"Manifest prior version is invalid: {error}") from error
    expected_sparkle_route = "stable" if prior_version.stage == "stable" else prior_version.stage
    if release.get("sparkle_route") != expected_sparkle_route:
        raise ReleaseQualificationManifestError("Manifest Sparkle route conflicts with the checked prior release.")

    artifacts = _mapping(manifest.get("artifacts"), "manifest artifacts")
    _exact_keys(artifacts, {"appcast", "checksum", "dmg", "signed_app_tree_sha256"}, "manifest artifacts")
    for kind in ("appcast", "checksum", "dmg"):
        artifact = _mapping(artifacts.get(kind), f"manifest artifact {kind}")
        _exact_keys(artifact, {"asset_id", "kind", "name", "sha256", "size_bytes"}, f"manifest artifact {kind}")
        checked = _artifact(receipt, kind)
        if artifact != checked:
            raise ReleaseQualificationManifestError(f"Manifest artifact {kind} conflicts with checked receipt.")
    if artifacts.get("signed_app_tree_sha256") != receipt.get("signed_app_tree_sha256"):
        raise ReleaseQualificationManifestError("Manifest signed app tree digest conflicts with checked receipt.")

    signed_ui = _mapping(manifest.get("signed_ui_artifact"), "manifest signed UI artifact")
    _exact_keys(
        signed_ui,
        {
            "archive_path",
            "artifact_id",
            "artifact_sha256",
            "receipt_file_sha256",
            "receipt_path",
            "receipt_sha256",
        },
        "manifest signed UI artifact",
    )
    archive_path, archive_relative = _repo_path(
        repo_root,
        signed_ui.get("archive_path"),
        "manifest signed UI artifact archive path",
    )
    if archive_relative != f"docs/release-evidence/{release_tag}/{SIGNAL_ARCHIVE_NAME}":
        raise ReleaseQualificationManifestError(
            "Manifest signed UI artifact archive path conflicts with candidate tag."
        )
    signed_path, signed_relative = _repo_path(
        repo_root,
        signed_ui.get("receipt_path"),
        "manifest signed UI receipt path",
    )
    if signed_relative != f"docs/release-evidence/{release_tag}/{SIGNAL_RECEIPT_NAME}":
        raise ReleaseQualificationManifestError("Manifest signed UI receipt path conflicts with candidate tag.")
    _integer(signed_ui.get("artifact_id"), "signed UI artifact ID")
    _sha256(signed_ui.get("artifact_sha256"), "signed UI artifact SHA-256")
    _sha256(signed_ui.get("receipt_file_sha256"), "signed UI receipt file SHA-256")
    _sha256(signed_ui.get("receipt_sha256"), "signed UI receipt self SHA-256")
    if validate_signed_ui_receipt:
        validated_signed_ui = signed_ui_binding_from_files(
            repo_root=repo_root,
            release_receipt_path=receipt_path,
            receipt=receipt,
            release_receipt_asset_id=_integer(release_receipt.get("asset_id"), "release receipt asset ID"),
            release_receipt_file_sha256=receipt_file_sha256,
            signed_ui_artifact_id=_integer(signed_ui.get("artifact_id"), "signed UI artifact ID"),
            signed_ui_artifact_digest=_sha256(signed_ui.get("artifact_sha256"), "signed UI artifact SHA-256"),
            signed_ui_archive_path=archive_path,
            signed_ui_receipt_path=signed_path,
            signed_ui_receipt_file_sha256=_sha256(
                signed_ui.get("receipt_file_sha256"),
                "signed UI receipt file SHA-256",
            ),
        )
        if signed_ui != {
            "archive_path": validated_signed_ui.archive_path,
            "artifact_id": validated_signed_ui.artifact_id,
            "artifact_sha256": validated_signed_ui.artifact_digest,
            "receipt_file_sha256": validated_signed_ui.receipt_file_sha256,
            "receipt_path": validated_signed_ui.receipt_path,
            "receipt_sha256": validated_signed_ui.receipt_sha256,
        }:
            raise ReleaseQualificationManifestError("Manifest signed UI binding conflicts with checked receipt.")
    workflow = _mapping(manifest.get("workflow"), "manifest workflow")
    _exact_keys(
        workflow,
        {"actor", "engine_path", "name", "path", "run_attempt", "run_id"},
        "manifest workflow",
    )
    receipt_workflow = _mapping(receipt.get("workflow"), "checked receipt workflow")
    if workflow != {
        "actor": receipt_workflow.get("actor"),
        "engine_path": receipt_workflow.get("engine_path"),
        "name": receipt_workflow.get("name"),
        "path": receipt_workflow.get("path"),
        "run_attempt": receipt_workflow.get("run_attempt"),
        "run_id": receipt_workflow.get("run_id"),
    }:
        raise ReleaseQualificationManifestError("Manifest workflow binding conflicts with checked receipt.")
    recorded_digest = _sha256(manifest.get("manifest_sha256"), "manifest self SHA-256")
    if recorded_digest != manifest_sha256(manifest):
        raise ReleaseQualificationManifestError("Release qualification manifest self-digest mismatch.")


def write_manifest(
    manifest: Mapping[str, Any],
    output: Path,
    *,
    replace_existing: bool = False,
) -> None:
    content = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        if output.read_text(encoding="utf-8") == content:
            return
        if not replace_existing:
            raise ReleaseQualificationManifestError(
                "Existing qualification manifest conflicts with this release state."
            )
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=output.parent, delete=False) as temporary:
        temporary.write(content)
        temporary_path = Path(temporary.name)
    temporary_path.replace(output)


def load_validated_manifest(
    path: Path,
    *,
    repo_root: Path = REPO_ROOT,
    expected_sha256: str | None = None,
) -> Mapping[str, Any]:
    manifest = _load_canonical_manifest(path)
    _validate_manifest(manifest, repo_root=repo_root, validate_signed_ui_receipt=True)
    if expected_sha256 is not None and manifest.get("manifest_sha256") != _sha256(
        expected_sha256,
        "expected manifest SHA-256",
    ):
        raise ReleaseQualificationManifestError("Manifest self-digest does not match the workflow input.")
    return manifest


def _immutable_release_identity(manifest: Mapping[str, Any]) -> dict[str, Any]:
    evidence = _mapping(manifest.get("canonical_evidence"), "manifest canonical evidence")
    return {
        "artifacts": manifest.get("artifacts"),
        "candidate": manifest.get("candidate"),
        "evidence_ref": evidence.get("ref"),
        "prior": manifest.get("prior"),
        "release": manifest.get("release"),
        "release_receipt": manifest.get("release_receipt"),
        "signed_ui_artifact": manifest.get("signed_ui_artifact"),
        "workflow": manifest.get("workflow"),
    }


def build_manifest_for_reconciled_release(
    *,
    repo_root: Path,
    release_tag: str,
    prior_tag: str,
    signed_ui_artifact_id: int,
    signed_ui_artifact_digest: str,
    signed_ui_archive_source_path: Path,
    signed_ui_receipt_source_path: Path,
    signed_ui_receipt_file_sha256: str,
    evidence_ref: str,
    evidence_base_sha: str,
    runner_sha: str,
    sparkle_route: str,
    qualification_path: Path,
) -> dict[str, Any]:
    release_receipt_path = repo_root / "docs" / "release-evidence" / release_tag / "release-receipt.json"
    prior_release_receipt_path = repo_root / "docs" / "release-evidence" / prior_tag / "release-receipt.json"
    receipt, release_receipt_file_sha256 = load_validated_checked_receipt(release_receipt_path)
    publication = _publication_record(repo_root, release_tag)
    checked_signed_archive = repo_root / "docs" / "release-evidence" / release_tag / SIGNAL_ARCHIVE_NAME
    checked_signed_receipt = repo_root / "docs" / "release-evidence" / release_tag / SIGNAL_RECEIPT_NAME
    manifest_path = repo_root / "docs" / "release-evidence" / release_tag / MANIFEST_NAME
    checked_signed_receipt.parent.mkdir(parents=True, exist_ok=True)
    archive_bytes = signed_ui_archive_source_path.read_bytes()
    if hashlib.sha256(archive_bytes).hexdigest() != _sha256(
        signed_ui_artifact_digest,
        "signed UI artifact digest",
    ):
        raise ReleaseQualificationManifestError(
            "Signed UI artifact archive digest does not match the supplied GitHub Actions digest."
        )
    source_bytes = signed_ui_receipt_source_path.read_bytes()
    source_digest = hashlib.sha256(source_bytes).hexdigest()
    if source_digest != _sha256(signed_ui_receipt_file_sha256, "signed UI receipt file SHA-256"):
        raise ReleaseQualificationManifestError("Signed UI receipt file digest does not match the supplied digest.")
    if checked_signed_receipt.exists() and checked_signed_receipt.read_bytes() != source_bytes:
        raise ReleaseQualificationManifestError(
            "Checked signed UI receipt conflicts with the Actions artifact receipt."
        )
    if checked_signed_archive.exists() and checked_signed_archive.read_bytes() != archive_bytes:
        raise ReleaseQualificationManifestError(
            "Checked signed UI artifact archive conflicts with the GitHub Actions artifact."
        )
    if not checked_signed_archive.exists():
        with tempfile.NamedTemporaryFile("wb", dir=checked_signed_archive.parent, delete=False) as temporary:
            temporary.write(archive_bytes)
            temporary_path = Path(temporary.name)
        temporary_path.replace(checked_signed_archive)
    if not checked_signed_receipt.exists():
        with tempfile.NamedTemporaryFile("wb", dir=checked_signed_receipt.parent, delete=False) as temporary:
            temporary.write(source_bytes)
            temporary_path = Path(temporary.name)
        temporary_path.replace(checked_signed_receipt)
    signed_ui = signed_ui_binding_from_files(
        repo_root=repo_root,
        release_receipt_path=release_receipt_path,
        receipt=receipt,
        release_receipt_asset_id=_integer(publication.get("receipt_asset_id"), "release receipt asset ID"),
        release_receipt_file_sha256=release_receipt_file_sha256,
        signed_ui_artifact_id=signed_ui_artifact_id,
        signed_ui_artifact_digest=signed_ui_artifact_digest,
        signed_ui_archive_path=checked_signed_archive,
        signed_ui_receipt_path=checked_signed_receipt,
        signed_ui_receipt_file_sha256=signed_ui_receipt_file_sha256,
    )
    manifest = build_manifest(
        repo_root=repo_root,
        release_receipt_path=release_receipt_path,
        prior_release_receipt_path=prior_release_receipt_path,
        signed_ui=signed_ui,
        evidence_ref=evidence_ref,
        evidence_base_sha=evidence_base_sha,
        runner_sha=runner_sha,
        sparkle_route=sparkle_route,
        qualification_path=qualification_path,
    )
    if manifest_path.exists():
        existing = _load_canonical_manifest(manifest_path)
        if _immutable_release_identity(existing) != _immutable_release_identity(manifest):
            raise ReleaseQualificationManifestError(
                "Existing qualification manifest conflicts with immutable release state."
            )
        if existing == manifest:
            return dict(existing)
        write_manifest(manifest, manifest_path, replace_existing=True)
    else:
        write_manifest(manifest, manifest_path)
    return manifest


def _write_github_output(path: Path, outputs: Mapping[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for key, value in outputs.items():
            handle.write(f"{key}={value}\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build and validate canonical release qualification manifests.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    build_parser = subparsers.add_parser("build", help="Build a manifest from checked release evidence.")
    build_parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    build_parser.add_argument("--release-tag", required=True)
    build_parser.add_argument("--prior-tag", required=True)
    build_parser.add_argument("--signed-ui-artifact-id", type=int, required=True)
    build_parser.add_argument("--signed-ui-artifact-digest", required=True)
    build_parser.add_argument("--signed-ui-artifact-archive", type=Path, required=True)
    build_parser.add_argument("--signed-ui-receipt", type=Path, required=True)
    build_parser.add_argument("--signed-ui-receipt-file-sha256", required=True)
    build_parser.add_argument("--evidence-ref", required=True)
    build_parser.add_argument("--evidence-base-sha", required=True)
    build_parser.add_argument("--runner-sha", required=True)
    build_parser.add_argument("--sparkle-route", required=True)
    build_parser.add_argument("--qualification-path", type=Path, required=True)
    build_parser.add_argument("--github-output", type=Path)

    validate_parser = subparsers.add_parser("validate", help="Validate a checked manifest.")
    validate_parser.add_argument("--manifest", type=Path, required=True)
    validate_parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    validate_parser.add_argument("--expected-sha256")
    validate_parser.add_argument("--evidence-base-revision")
    validate_parser.add_argument("--github-output", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            manifest = build_manifest_for_reconciled_release(
                repo_root=args.repo_root.resolve(),
                release_tag=args.release_tag,
                prior_tag=args.prior_tag,
                signed_ui_artifact_id=args.signed_ui_artifact_id,
                signed_ui_artifact_digest=args.signed_ui_artifact_digest,
                signed_ui_archive_source_path=args.signed_ui_artifact_archive,
                signed_ui_receipt_source_path=args.signed_ui_receipt,
                signed_ui_receipt_file_sha256=args.signed_ui_receipt_file_sha256,
                evidence_ref=args.evidence_ref,
                evidence_base_sha=args.evidence_base_sha,
                runner_sha=args.runner_sha,
                sparkle_route=args.sparkle_route,
                qualification_path=args.qualification_path,
            )
        else:
            manifest = dict(
                load_validated_manifest(
                    args.manifest,
                    repo_root=args.repo_root.resolve(),
                    expected_sha256=args.expected_sha256,
                )
            )
            if args.evidence_base_revision is not None:
                validate_manifest_evidence_history(
                    manifest,
                    repo_root=args.repo_root.resolve(),
                    base_revision=args.evidence_base_revision,
                )
        outputs = {
            "candidate_sha": _mapping(manifest["candidate"], "candidate")["source_sha"],
            "candidate_tag": _mapping(manifest["candidate"], "candidate")["release_tag"],
            "evidence_ref": _mapping(manifest["canonical_evidence"], "canonical evidence")["ref"],
            "evidence_base_sha": _mapping(manifest["canonical_evidence"], "canonical evidence")["base_sha"],
            "manifest_sha256": manifest["manifest_sha256"],
            "prior_tag": _mapping(manifest["prior"], "prior")["release_tag"],
            "release_receipt_path": _mapping(manifest["release_receipt"], "release receipt")["path"],
            "signed_ui_artifact_id": _mapping(manifest["signed_ui_artifact"], "signed UI artifact")["artifact_id"],
            "signed_ui_artifact_sha256": _mapping(manifest["signed_ui_artifact"], "signed UI artifact")[
                "artifact_sha256"
            ],
            "signed_ui_archive_path": _mapping(manifest["signed_ui_artifact"], "signed UI artifact")["archive_path"],
            "signed_ui_receipt_path": _mapping(manifest["signed_ui_artifact"], "signed UI artifact")["receipt_path"],
            "sparkle_route": _mapping(manifest["release"], "release")["sparkle_route"],
            "runner_sha": manifest["runner_sha"],
            "workflow_run_attempt": _mapping(manifest["workflow"], "workflow")["run_attempt"],
            "workflow_run_id": _mapping(manifest["workflow"], "workflow")["run_id"],
        }
        if args.github_output is not None:
            _write_github_output(args.github_output, outputs)
        print(json.dumps(outputs, sort_keys=True))
    except (ReleaseQualificationManifestError, ReleaseEvidenceError, ReleaseReceiptError, OSError) as error:
        parser.exit(1, f"{error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
