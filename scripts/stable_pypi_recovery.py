from __future__ import annotations

import argparse
import hashlib
import json
import os
import time

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


REPO_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_PATH = REPO_ROOT / "docs" / "release-evidence" / "v0.3.0-pypi-recovery.json"
EVIDENCE_SHA256 = "46574b247fc9b9ef13cd64aafaa8e859c2cf10fcfde50913cd317ae5bfec361f"
PYPI_VERSION_URL = "https://pypi.org/pypi/bd-to-avp/0.3.0/json"


class StablePyPIRecoveryError(RuntimeError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise StablePyPIRecoveryError(f"Duplicate JSON key in recovery evidence: {key!r}")
        result[key] = value
    return result


def _mapping(value: object, description: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise StablePyPIRecoveryError(f"{description} must be a JSON object.")
    return cast(Mapping[str, Any], value)


def _sequence(value: object, description: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise StablePyPIRecoveryError(f"{description} must be a JSON array.")
    return value


def _string(mapping: Mapping[str, Any], key: str, description: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise StablePyPIRecoveryError(f"{description} is missing non-empty string {key!r}.")
    return value


def _integer(mapping: Mapping[str, Any], key: str, description: str) -> int:
    value = mapping.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise StablePyPIRecoveryError(f"{description} is missing positive integer {key!r}.")
    return value


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_evidence(
    path: Path = EVIDENCE_PATH,
    *,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    try:
        content = path.read_bytes()
    except OSError as error:
        raise StablePyPIRecoveryError(f"Unable to read Stable PyPI recovery evidence: {error}") from error
    digest = hashlib.sha256(content).hexdigest()
    if digest != EVIDENCE_SHA256:
        raise StablePyPIRecoveryError(f"Recovery evidence digest {digest} does not match the reviewed receipt.")
    if expected_sha256 is not None and expected_sha256 != EVIDENCE_SHA256:
        raise StablePyPIRecoveryError("Dispatch recovery fingerprint does not match the reviewed evidence.")
    try:
        value = json.loads(content, object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise StablePyPIRecoveryError(f"Stable PyPI recovery evidence is invalid JSON: {error}") from error
    evidence = dict(_mapping(value, "Stable PyPI recovery evidence"))
    if evidence.get("schema") != "bd_to_avp.pypi_recovery" or evidence.get("schema_version") != 1:
        raise StablePyPIRecoveryError("Stable PyPI recovery evidence schema is unsupported.")
    repository = _mapping(evidence.get("repository"), "Recovery repository")
    if repository.get("id") != 771225421 or repository.get("full_name") != "cbusillo/BD_to_AVP":
        raise StablePyPIRecoveryError("Recovery evidence does not identify the production repository.")
    release = _mapping(evidence.get("release"), "Recovery release")
    failed_run = _mapping(evidence.get("failed_run"), "Recovery failed run")
    artifact = _mapping(evidence.get("artifact"), "Recovery artifact")
    if release.get("version") != "0.3.0" or release.get("tag") != "v0.3.0":
        raise StablePyPIRecoveryError("Recovery evidence does not identify Stable 0.3.0.")
    if release.get("source_sha") != failed_run.get("head_sha"):
        raise StablePyPIRecoveryError("Recovery release and failed run source identities differ.")
    if failed_run.get("id") != 31219050718 or failed_run.get("conclusion") != "failure":
        raise StablePyPIRecoveryError("Recovery evidence does not identify the reviewed failed Stable run.")
    if artifact.get("id") != 9009656530 or artifact.get("name") != "python-distributions":
        raise StablePyPIRecoveryError("Recovery evidence does not identify the reviewed Python artifact.")
    distributions = _sequence(evidence.get("distributions"), "Recovery distributions")
    if len(distributions) != 2:
        raise StablePyPIRecoveryError("Recovery evidence must identify exactly two Python distributions.")
    paths = {
        _string(_mapping(item, "Recovery distribution"), "path", "Recovery distribution") for item in distributions
    }
    if paths != {"dist/bd_to_avp-0.3.0-py3-none-any.whl", "dist/bd_to_avp-0.3.0.tar.gz"}:
        raise StablePyPIRecoveryError("Recovery evidence contains an unexpected distribution set.")
    return evidence


def write_outputs(path: Path, evidence: Mapping[str, Any]) -> None:
    release = _mapping(evidence.get("release"), "Recovery release")
    failed_run = _mapping(evidence.get("failed_run"), "Recovery failed run")
    artifact = _mapping(evidence.get("artifact"), "Recovery artifact")
    outputs = {
        "artifact_digest": _string(artifact, "digest", "Recovery artifact"),
        "artifact_id": str(_integer(artifact, "id", "Recovery artifact")),
        "evidence_sha256": EVIDENCE_SHA256,
        "release_id": str(_integer(release, "id", "Recovery release")),
        "release_receipt_asset_id": str(_integer(release, "receipt_asset_id", "Recovery release")),
        "release_run_id": str(_integer(failed_run, "id", "Recovery failed run")),
        "release_source_sha": _string(release, "source_sha", "Recovery release"),
        "release_tag": _string(release, "tag", "Recovery release"),
    }
    with path.open("a", encoding="utf-8") as output:
        for name, value in outputs.items():
            output.write(f"{name}={value}\n")


def verify_payload(root: Path, evidence: Mapping[str, Any]) -> None:
    root = root.resolve()
    if not root.is_dir():
        raise StablePyPIRecoveryError(f"Recovery artifact root does not exist: {root}")
    if any(path.is_symlink() for path in root.rglob("*")):
        raise StablePyPIRecoveryError("Recovery artifact must not contain symbolic links.")
    distributions = [
        _mapping(item, "Recovery distribution")
        for item in _sequence(evidence["distributions"], "Recovery distributions")
    ]
    expected_files = {"SHA256SUMS", *(_string(item, "path", "Recovery distribution") for item in distributions)}
    actual_files = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
    if actual_files != expected_files:
        raise StablePyPIRecoveryError(
            f"Recovery artifact file set differs from reviewed evidence: expected {sorted(expected_files)}, "
            f"found {sorted(actual_files)}."
        )
    artifact = _mapping(evidence.get("artifact"), "Recovery artifact")
    manifest_path = root / "SHA256SUMS"
    if _file_sha256(manifest_path) != _string(artifact, "manifest_sha256", "Recovery artifact"):
        raise StablePyPIRecoveryError("Recovery artifact checksum manifest differs from the failed run.")
    for distribution in distributions:
        path = root / _string(distribution, "path", "Recovery distribution")
        if path.stat().st_size != _integer(distribution, "size", "Recovery distribution"):
            raise StablePyPIRecoveryError(f"Recovery distribution size differs: {path.name}")
        if _file_sha256(path) != _string(distribution, "sha256", "Recovery distribution"):
            raise StablePyPIRecoveryError(f"Recovery distribution digest differs: {path.name}")
    omitted = _mapping(artifact.get("omitted_hidden_file"), "Omitted hidden file")
    expected_manifest = {
        _string(omitted, "path", "Omitted hidden file"): _string(omitted, "sha256", "Omitted hidden file"),
        **{
            _string(item, "path", "Recovery distribution"): _string(item, "sha256", "Recovery distribution")
            for item in distributions
        },
    }
    parsed_manifest: dict[str, str] = {}
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        digest, separator, name = line.partition("  ")
        if not separator or name in parsed_manifest:
            raise StablePyPIRecoveryError("Recovery artifact checksum manifest is malformed.")
        parsed_manifest[name] = digest
    if parsed_manifest != expected_manifest:
        raise StablePyPIRecoveryError("Recovery artifact manifest does not reproduce the reviewed failure.")
    if (root / _string(omitted, "path", "Omitted hidden file")).exists():
        raise StablePyPIRecoveryError("The uploader omission premise no longer matches the recovery artifact.")


def verify_remote_files(
    workflow_run_path: Path,
    jobs_path: Path,
    artifact_path: Path,
    release_path: Path,
    receipt_path: Path,
    evidence: Mapping[str, Any],
) -> None:
    workflow_run = _mapping(json.loads(workflow_run_path.read_text(encoding="utf-8")), "Workflow run")
    jobs = _mapping(json.loads(jobs_path.read_text(encoding="utf-8")), "Workflow jobs")
    artifact = _mapping(json.loads(artifact_path.read_text(encoding="utf-8")), "Workflow artifact")
    release = _mapping(json.loads(release_path.read_text(encoding="utf-8")), "GitHub release")
    failed_run = _mapping(evidence.get("failed_run"), "Recovery failed run")
    expected_artifact = _mapping(evidence.get("artifact"), "Recovery artifact")
    expected_release = _mapping(evidence.get("release"), "Recovery release")
    expected_run_fields = {
        "id": failed_run["id"],
        "workflow_id": failed_run["workflow_id"],
        "run_number": failed_run["run_number"],
        "run_attempt": failed_run["run_attempt"],
        "name": failed_run["workflow_name"],
        "path": failed_run["workflow_path"],
        "event": failed_run["event"],
        "head_branch": failed_run["head_branch"],
        "head_sha": failed_run["head_sha"],
        "status": failed_run["status"],
        "conclusion": failed_run["conclusion"],
    }
    for field, expected in expected_run_fields.items():
        if workflow_run.get(field) != expected:
            raise StablePyPIRecoveryError(f"Failed Stable run {field} differs from reviewed evidence.")
    for actor_field in ("actor", "triggering_actor"):
        if (
            _mapping(workflow_run.get(actor_field), f"Workflow run {actor_field}").get("login")
            != failed_run[actor_field]
        ):
            raise StablePyPIRecoveryError(f"Failed Stable run {actor_field} differs from reviewed evidence.")
    job_matches = [
        _mapping(job, "Workflow job")
        for job in _sequence(jobs.get("jobs"), "Workflow jobs")
        if _mapping(job, "Workflow job").get("id") == failed_run["failed_job_id"]
    ]
    if (
        len(job_matches) != 1
        or job_matches[0].get("name") != failed_run["failed_job"]
        or job_matches[0].get("conclusion") != "failure"
    ):
        raise StablePyPIRecoveryError("Failed PyPI job identity differs from reviewed evidence.")
    steps = {
        _mapping(step, "Workflow step").get("name"): _mapping(step, "Workflow step").get("conclusion")
        for step in _sequence(job_matches[0].get("steps"), "Workflow steps")
    }
    if steps.get(failed_run["failed_step"]) != "failure" or steps.get(failed_run["skipped_publish_step"]) != "skipped":
        raise StablePyPIRecoveryError("Failed PyPI job no longer proves that publication was skipped.")
    if artifact.get("id") != expected_artifact["id"] or artifact.get("name") != expected_artifact["name"]:
        raise StablePyPIRecoveryError("Python artifact identity differs from reviewed evidence.")
    if (
        artifact.get("digest") != f"sha256:{expected_artifact['digest']}"
        or artifact.get("size_in_bytes") != expected_artifact["size"]
    ):
        raise StablePyPIRecoveryError("Python artifact digest or size differs from reviewed evidence.")
    if artifact.get("expired") is not False:
        raise StablePyPIRecoveryError("Python recovery artifact has expired.")
    artifact_run = _mapping(artifact.get("workflow_run"), "Artifact workflow run")
    if artifact_run.get("id") != failed_run["id"] or artifact_run.get("head_sha") != failed_run["head_sha"]:
        raise StablePyPIRecoveryError("Python artifact is not bound to the reviewed Stable run.")
    expected_release_fields = {
        "id": expected_release["id"],
        "tag_name": expected_release["tag"],
        "name": expected_release["name"],
        "target_commitish": expected_release["source_sha"],
        "draft": expected_release["draft"],
        "prerelease": expected_release["prerelease"],
        "immutable": expected_release["immutable"],
        "published_at": expected_release["published_at"],
    }
    for field, expected in expected_release_fields.items():
        if release.get(field) != expected:
            raise StablePyPIRecoveryError(f"Published Stable release {field} differs from reviewed evidence.")
    receipt_assets = [
        _mapping(item, "Release asset")
        for item in _sequence(release.get("assets"), "Release assets")
        if _mapping(item, "Release asset").get("id") == expected_release["receipt_asset_id"]
    ]
    if len(receipt_assets) != 1 or receipt_assets[0].get("digest") != f"sha256:{expected_release['receipt_sha256']}":
        raise StablePyPIRecoveryError("Published Stable receipt asset differs from reviewed evidence.")
    if receipt_assets[0].get("size") != expected_release["receipt_size"]:
        raise StablePyPIRecoveryError("Published Stable receipt asset size differs from reviewed evidence.")
    if _file_sha256(receipt_path) != expected_release["receipt_sha256"]:
        raise StablePyPIRecoveryError("Downloaded Stable release receipt digest differs from reviewed evidence.")
    receipt = _mapping(json.loads(receipt_path.read_text(encoding="utf-8")), "Release receipt")
    receipt_workflow = _mapping(receipt.get("workflow"), "Receipt workflow")
    if (
        receipt.get("source_sha") != expected_release["source_sha"]
        or receipt_workflow.get("run_id") != failed_run["id"]
    ):
        raise StablePyPIRecoveryError("Stable release receipt is not bound to the reviewed source and run.")


def _fetch_pypi() -> tuple[int, Mapping[str, Any] | None]:
    request = Request(PYPI_VERSION_URL, headers={"Accept": "application/json", "User-Agent": "bd-to-avp-pypi-recovery"})
    try:
        with urlopen(request, timeout=30) as response:
            return response.status, _mapping(json.loads(response.read()), "PyPI response")
    except HTTPError as error:
        if error.code == 404:
            return 404, None
        raise StablePyPIRecoveryError(f"PyPI returned unexpected HTTP status {error.code}.") from error
    except (OSError, URLError, json.JSONDecodeError) as error:
        raise StablePyPIRecoveryError(f"Unable to verify PyPI state: {error}") from error


def verify_pypi(state: str, evidence: Mapping[str, Any], *, retries: int = 1, delay: float = 0.0) -> str:
    expected = {
        _string(item, "filename", "Recovery distribution"): (
            _string(item, "sha256", "Recovery distribution"),
            _integer(item, "size", "Recovery distribution"),
            _string(item, "packagetype", "Recovery distribution"),
        )
        for item in (
            _mapping(value, "Recovery distribution")
            for value in _sequence(evidence["distributions"], "Recovery distributions")
        )
    }
    last_error: StablePyPIRecoveryError | None = None
    for attempt in range(retries):
        try:
            status, payload = _fetch_pypi()
            if state == "absent":
                if status != 404:
                    raise StablePyPIRecoveryError("PyPI 0.3.0 already exists; recovery publication is not idempotent.")
                return "absent"
            if state == "recoverable" and status == 404:
                return "absent"
            if status != 200 or payload is None:
                raise StablePyPIRecoveryError("PyPI 0.3.0 is not published yet.")
            urls = [
                _mapping(item, "PyPI distribution") for item in _sequence(payload.get("urls"), "PyPI distributions")
            ]
            actual = {
                _string(item, "filename", "PyPI distribution"): (
                    _string(_mapping(item.get("digests"), "PyPI digests"), "sha256", "PyPI digests"),
                    _integer(item, "size", "PyPI distribution"),
                    _string(item, "packagetype", "PyPI distribution"),
                )
                for item in urls
            }
            if state == "recoverable":
                if not actual or any(expected.get(filename) != details for filename, details in actual.items()):
                    raise StablePyPIRecoveryError("Published PyPI files differ from the reviewed Stable artifacts.")
                return "published" if actual == expected else "partial"
            if actual != expected:
                raise StablePyPIRecoveryError("Published PyPI files differ from the reviewed Stable artifacts.")
            return "published"
        except StablePyPIRecoveryError as error:
            last_error = error
            if attempt + 1 < retries:
                time.sleep(delay)
    assert last_error is not None
    raise last_error


def write_pypi_action_outputs(
    path: Path,
    evidence: Mapping[str, Any],
    *,
    retries: int,
    delay: float,
) -> None:
    state = verify_pypi("recoverable", evidence, retries=retries, delay=delay)
    with path.open("a", encoding="utf-8") as output:
        output.write(f"publish_required={'false' if state == 'published' else 'true'}\n")
        output.write(f"publication_action={'already_present' if state == 'published' else 'published'}\n")


def write_receipt(path: Path, evidence: Mapping[str, Any], publication_action: str) -> None:
    release = _mapping(evidence.get("release"), "Recovery release")
    failed_run = _mapping(evidence.get("failed_run"), "Recovery failed run")
    artifact = _mapping(evidence.get("artifact"), "Recovery artifact")
    receipt = {
        "schema": "bd_to_avp.pypi_recovery_receipt",
        "schema_version": 1,
        "evidence_sha256": EVIDENCE_SHA256,
        "repository": "cbusillo/BD_to_AVP",
        "release": {"tag": release["tag"], "version": release["version"], "source_sha": release["source_sha"]},
        "original_release_run": {"id": failed_run["id"], "attempt": failed_run["run_attempt"]},
        "python_artifact": {"id": artifact["id"], "digest": artifact["digest"]},
        "distributions": evidence["distributions"],
        "recovery_workflow": {
            "run_id": int(os.environ["GITHUB_RUN_ID"]),
            "run_attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]),
            "workflow_sha": os.environ["GITHUB_SHA"],
        },
        "publication_action": publication_action,
        "pypi_verification": "passed",
    }
    path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate the one-time Stable 0.3.0 PyPI recovery.")
    parser.add_argument("--evidence", type=Path, default=EVIDENCE_PATH)
    parser.add_argument("--expected-evidence-sha256")
    subparsers = parser.add_subparsers(dest="command", required=True)
    outputs_parser = subparsers.add_parser("outputs")
    outputs_parser.add_argument("--github-output", type=Path, required=True)
    payload_parser = subparsers.add_parser("verify-payload")
    payload_parser.add_argument("--artifact-root", type=Path, required=True)
    remote_parser = subparsers.add_parser("verify-remote-files")
    remote_parser.add_argument("--workflow-run", type=Path, required=True)
    remote_parser.add_argument("--jobs", type=Path, required=True)
    remote_parser.add_argument("--artifact", type=Path, required=True)
    remote_parser.add_argument("--release", type=Path, required=True)
    remote_parser.add_argument("--receipt", type=Path, required=True)
    pypi_parser = subparsers.add_parser("verify-pypi")
    pypi_parser.add_argument("--state", choices=("absent", "published", "recoverable"), required=True)
    pypi_parser.add_argument("--retries", type=int, default=1)
    pypi_parser.add_argument("--delay", type=float, default=0.0)
    action_parser = subparsers.add_parser("resolve-pypi-action")
    action_parser.add_argument("--github-output", type=Path, required=True)
    action_parser.add_argument("--retries", type=int, default=1)
    action_parser.add_argument("--delay", type=float, default=0.0)
    receipt_parser = subparsers.add_parser("write-receipt")
    receipt_parser.add_argument("--output", type=Path, required=True)
    receipt_parser.add_argument("--publication-action", choices=("published", "already_present"), required=True)
    args = parser.parse_args()
    evidence = load_evidence(args.evidence, expected_sha256=args.expected_evidence_sha256)
    if args.command == "outputs":
        write_outputs(args.github_output, evidence)
    elif args.command == "verify-payload":
        verify_payload(args.artifact_root, evidence)
    elif args.command == "verify-remote-files":
        verify_remote_files(args.workflow_run, args.jobs, args.artifact, args.release, args.receipt, evidence)
    elif args.command == "verify-pypi":
        verify_pypi(args.state, evidence, retries=args.retries, delay=args.delay)
    elif args.command == "resolve-pypi-action":
        write_pypi_action_outputs(
            args.github_output,
            evidence,
            retries=args.retries,
            delay=args.delay,
        )
    elif args.command == "write-receipt":
        write_receipt(args.output, evidence, args.publication_action)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
