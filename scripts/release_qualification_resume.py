from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import time

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import quote

from scripts.github_release_run import GhAPIClient
from scripts.qualify_release_scope import QualificationScopeError
from scripts.release_milestone_context import ReleaseMilestoneContextError
from scripts.release_qualification_controller import (
    EvidenceBinding,
    ReleaseQualificationControllerError,
    build_status,
    resolve_evidence_binding,
)
from scripts.release_qualification_manifest import ReleaseQualificationManifestError


REPOSITORY = "cbusillo/BD_to_AVP"
REPOSITORY_OWNER = "cbusillo"
MAIN_BRANCH = "main"
WORKFLOW_FILE = "milestone-qualification.yml"
WORKFLOW_NAME = "Milestone Qualification"
WORKFLOW_PATH = ".github/workflows/milestone-qualification.yml"
WORKFLOW_JOB_NAME = "Collect exact post-publication qualification receipts"
RESUME_TYPE = "bd_to_avp.release_qualification_resume"
CHECKPOINT_TYPE = "bd_to_avp.release_qualification_resume_checkpoint"
SCHEMA_VERSION = 1
EXIT_SUCCESS = 0
EXIT_FAILED = 1
EXIT_OPERATOR_REQUIRED = 20
EXIT_SAFETY_ERROR = 21
PREPARED_RETRY_AFTER_SECONDS = 600
ACTIVE_RUN_STATUSES = {"in_progress", "pending", "queued", "requested", "waiting"}
TERMINAL_RUN_STATUS = "completed"
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
ABSOLUTE_PATH_PATTERN = re.compile(r"(?<![A-Za-z0-9])/(?:Users|home|private|var|tmp)/[^\s\"']+")


class QualificationResumeError(RuntimeError):
    pass


class QualificationResumeSafetyError(QualificationResumeError):
    pass


class GitHubAPI(Protocol):
    def get_json(self, endpoint: str, *, active_auth: bool = False) -> object:
        raise NotImplementedError

    def post_json(
        self,
        endpoint: str,
        payload: Mapping[str, object],
        *,
        active_auth: bool = False,
    ) -> object:
        raise NotImplementedError


@dataclass(frozen=True)
class ResumeIdentity:
    release_tag: str
    candidate_sha: str
    release_id: int
    manifest_sha256: str
    runner_sha: str
    main_sha: str
    evidence_ref: str
    evidence_sha: str
    evidence_base_sha: str
    evidence_pr_number: int
    release_receipt_file_sha256: str
    signed_ui_artifact_id: int
    signed_ui_artifact_sha256: str
    policy_sha256: str
    policy_checkpoint_sha256: str
    route_table_sha256: str
    controller_runner_sha256: str

    @property
    def workflow_display_title(self) -> str:
        return f"Milestone {self.release_tag} {self.manifest_sha256}"

    def payload(self) -> dict[str, object]:
        return asdict(self)

    @property
    def digest(self) -> str:
        return hashlib.sha256(_canonical_json_bytes(self.payload())).hexdigest()


@dataclass(frozen=True)
class ResumeResult:
    payload: dict[str, object]
    exit_code: int


Sleep = Callable[[float], None]
Clock = Callable[[], datetime]


def _mapping(value: object, description: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise QualificationResumeError(f"{description} must be a JSON object.")
    return cast(Mapping[str, Any], value)


def _sequence(value: object, description: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise QualificationResumeError(f"{description} must be a JSON array.")
    return value


def _string(value: object, description: str) -> str:
    if not isinstance(value, str) or not value:
        raise QualificationResumeError(f"{description} must be a non-empty string.")
    return value


def _integer(value: object, description: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise QualificationResumeError(f"{description} must be a positive integer.")
    return value


def _nonnegative_integer(value: object, description: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise QualificationResumeError(f"{description} must be a nonnegative integer.")
    return value


def _sha(value: object, description: str) -> str:
    text = _string(value, description)
    if SHA_PATTERN.fullmatch(text) is None:
        raise QualificationResumeError(f"{description} must be a full lowercase Git SHA.")
    return text


def _sha256(value: object, description: str) -> str:
    text = _string(value, description)
    if SHA256_PATTERN.fullmatch(text) is None:
        raise QualificationResumeError(f"{description} must be a lowercase SHA-256 digest.")
    return text


def _exact_keys(value: Mapping[str, Any], expected: set[str], description: str) -> None:
    actual = set(value)
    if actual != expected:
        raise QualificationResumeSafetyError(
            f"{description} keys changed; missing={sorted(expected - actual)!r}, extra={sorted(actual - expected)!r}."
        )


def _canonical_json_bytes(payload: Mapping[str, object]) -> bytes:
    return (json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n").encode()


def _git(
    repo_root: Path,
    arguments: Sequence[str],
    *,
    text: bool = True,
) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *arguments],
        cwd=repo_root,
        capture_output=True,
        text=text,
        check=False,
    )


def _git_text(repo_root: Path, arguments: Sequence[str], description: str) -> str:
    result = cast(subprocess.CompletedProcess[str], _git(repo_root, arguments))
    if result.returncode != 0:
        raise QualificationResumeError(f"Unable to read {description} from git.")
    return result.stdout.strip()


def _checkpoint_path(repo_root: Path, release_tag: str) -> Path:
    common_dir = Path(_git_text(repo_root, ["rev-parse", "--git-common-dir"], "git common directory"))
    if not common_dir.is_absolute():
        common_dir = repo_root / common_dir
    return common_dir.resolve() / "bd-to-avp" / "release-qualification" / f"{release_tag}.json"


@contextmanager
def _checkpoint_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    lock_path = path.with_suffix(path.suffix + ".lock")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise QualificationResumeSafetyError(
                "Another qualification resume process already owns this release checkpoint."
            ) from error
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _load_checkpoint(path: Path) -> Mapping[str, Any] | None:
    if not path.exists():
        return None
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise QualificationResumeSafetyError("Qualification resume checkpoint permissions must be 0600.")
    try:
        checkpoint = _mapping(json.loads(path.read_text(encoding="utf-8")), "qualification resume checkpoint")
    except OSError as error:
        raise QualificationResumeError("Unable to read qualification resume checkpoint.") from error
    except json.JSONDecodeError as error:
        raise QualificationResumeSafetyError(f"Qualification resume checkpoint is invalid JSON: {error}") from error
    if checkpoint.get("schema_version") != SCHEMA_VERSION or checkpoint.get("checkpoint_type") != CHECKPOINT_TYPE:
        raise QualificationResumeSafetyError("Qualification resume checkpoint schema or type is unsupported.")
    _exact_keys(
        checkpoint,
        {
            "schema_version",
            "checkpoint_type",
            "checkpoint_sha256",
            "identity",
            "identity_sha256",
            "dispatch",
        },
        "qualification resume checkpoint",
    )
    recorded_digest = _sha256(checkpoint.get("checkpoint_sha256"), "checkpoint self digest")
    digest_payload = dict(checkpoint)
    digest_payload.pop("checkpoint_sha256")
    if hashlib.sha256(_canonical_json_bytes(digest_payload)).hexdigest() != recorded_digest:
        raise QualificationResumeSafetyError("Qualification resume checkpoint self digest is invalid.")
    return checkpoint


def _write_checkpoint(path: Path, payload: Mapping[str, object]) -> None:
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        rendered = _canonical_json_bytes(payload)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile("wb", dir=path.parent, delete=False) as temporary:
                temporary_path = Path(temporary.name)
                os.chmod(temporary_path, 0o600)
                temporary.write(rendered)
                temporary.flush()
                os.fsync(temporary.fileno())
            temporary_path.replace(path)
            os.chmod(path, 0o600)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()
    except OSError as error:
        raise QualificationResumeError("Unable to write qualification resume checkpoint.") from error


def _checkpoint_payload(
    identity: ResumeIdentity,
    *,
    state: str,
    high_water_run_id: int,
    retry_of_run_id: int | None,
    run_id: int | None = None,
    run_attempt: int | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "checkpoint_type": CHECKPOINT_TYPE,
        "identity": identity.payload(),
        "identity_sha256": identity.digest,
        "dispatch": {
            "state": state,
            "prepared_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "high_water_run_id": high_water_run_id,
            "retry_of_run_id": retry_of_run_id,
            "run_id": run_id,
            "run_attempt": run_attempt,
        },
    }
    payload["checkpoint_sha256"] = hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()
    return payload


def _validate_checkpoint(checkpoint: Mapping[str, Any], identity: ResumeIdentity) -> Mapping[str, Any]:
    recorded_identity = _mapping(checkpoint.get("identity"), "checkpoint identity")
    if recorded_identity != identity.payload() or checkpoint.get("identity_sha256") != identity.digest:
        raise QualificationResumeSafetyError("Qualification resume checkpoint conflicts with current release identity.")
    dispatch = _mapping(checkpoint.get("dispatch"), "checkpoint dispatch")
    _exact_keys(
        dispatch,
        {
            "state",
            "prepared_at",
            "high_water_run_id",
            "retry_of_run_id",
            "run_id",
            "run_attempt",
        },
        "checkpoint dispatch",
    )
    if dispatch.get("state") not in {"prepared", "observed"}:
        raise QualificationResumeSafetyError("Qualification resume checkpoint dispatch state is unsupported.")
    prepared_at = _string(dispatch.get("prepared_at"), "checkpoint prepared timestamp")
    try:
        parsed_at = datetime.fromisoformat(prepared_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise QualificationResumeSafetyError("Qualification resume checkpoint timestamp is invalid.") from error
    if parsed_at.tzinfo is None:
        raise QualificationResumeSafetyError("Qualification resume checkpoint timestamp must include a timezone.")
    _nonnegative_integer(dispatch.get("high_water_run_id"), "checkpoint high-water run ID")
    retry_of = dispatch.get("retry_of_run_id")
    if retry_of is not None:
        _integer(retry_of, "checkpoint retry run ID")
    run_id = dispatch.get("run_id")
    run_attempt = dispatch.get("run_attempt")
    if dispatch.get("state") == "observed":
        _integer(run_id, "checkpoint observed run ID")
        _integer(run_attempt, "checkpoint observed run attempt")
    elif run_id is not None or run_attempt is not None:
        raise QualificationResumeSafetyError("Prepared qualification checkpoint cannot contain an observed run.")
    return dispatch


def _ref_endpoint(branch: str) -> str:
    return f"repos/{REPOSITORY}/git/ref/heads/{quote(branch, safe='')}"


def _workflow_runs_endpoint() -> str:
    return (
        f"repos/{REPOSITORY}/actions/workflows/{WORKFLOW_FILE}/runs"
        f"?event=workflow_dispatch&branch={MAIN_BRANCH}&per_page=100"
    )


def _workflow_dispatch_endpoint() -> str:
    return f"repos/{REPOSITORY}/actions/workflows/{WORKFLOW_FILE}/dispatches"


def _ref_sha(client: GitHubAPI, branch: str) -> str:
    reference = _mapping(
        client.get_json(_ref_endpoint(branch), active_auth=True),
        f"GitHub branch {branch}",
    )
    return _sha(_mapping(reference.get("object"), f"GitHub branch {branch} object").get("sha"), f"GitHub {branch} SHA")


def _validate_repository(client: GitHubAPI) -> None:
    repository = _mapping(
        client.get_json(f"repos/{REPOSITORY}", active_auth=True),
        "GitHub repository",
    )
    if repository.get("full_name") != REPOSITORY:
        raise QualificationResumeSafetyError("GitHub repository identity does not match the qualification controller.")
    owner = _mapping(repository.get("owner"), "GitHub repository owner")
    if owner.get("login") != REPOSITORY_OWNER:
        raise QualificationResumeSafetyError("GitHub repository owner does not match the qualification controller.")
    _integer(repository.get("id"), "GitHub repository ID")


def _open_evidence_pr(client: GitHubAPI, evidence_ref: str, evidence_sha: str) -> Mapping[str, Any]:
    endpoint = (
        f"repos/{REPOSITORY}/pulls?state=open&base={MAIN_BRANCH}"
        f"&head={REPOSITORY_OWNER}%3A{quote(evidence_ref, safe='')}&per_page=100"
    )
    pulls = _sequence(client.get_json(endpoint, active_auth=True), "open evidence pull requests")
    if len(pulls) != 1:
        raise QualificationResumeSafetyError(
            f"Expected exactly one open pull request for {evidence_ref}; found {len(pulls)}."
        )
    pull = _mapping(pulls[0], "evidence pull request")
    head = _mapping(pull.get("head"), "evidence pull request head")
    base = _mapping(pull.get("base"), "evidence pull request base")
    if (
        pull.get("state") != "open"
        or head.get("ref") != evidence_ref
        or head.get("sha") != evidence_sha
        or _mapping(head.get("repo"), "evidence pull request head repository").get("full_name") != REPOSITORY
        or base.get("ref") != MAIN_BRANCH
        or _mapping(base.get("repo"), "evidence pull request base repository").get("full_name") != REPOSITORY
    ):
        raise QualificationResumeSafetyError(
            "Evidence pull request identity conflicts with the checked evidence branch."
        )
    _integer(pull.get("number"), "evidence pull request number")
    return pull


def _require_local_evidence_checkout(repo_root: Path, identity: ResumeIdentity) -> None:
    local_head = _sha(_git_text(repo_root, ["rev-parse", "HEAD"], "local HEAD"), "local HEAD")
    local_branch = _git_text(repo_root, ["symbolic-ref", "--quiet", "--short", "HEAD"], "local branch")
    if local_branch != identity.evidence_ref or local_head != identity.evidence_sha:
        raise QualificationResumeSafetyError(
            "Qualification resume must run from the exact checked evidence branch head."
        )
    ancestor = cast(
        subprocess.CompletedProcess[str],
        _git(repo_root, ["merge-base", "--is-ancestor", identity.main_sha, "HEAD"]),
    )
    if ancestor.returncode != 0:
        raise QualificationResumeSafetyError("Evidence branch must contain the current protected main commit.")
    changed = cast(
        subprocess.CompletedProcess[bytes],
        _git(repo_root, ["diff", "--name-only", "-z", f"{identity.main_sha}...HEAD"], text=False),
    )
    if changed.returncode != 0:
        raise QualificationResumeError("Unable to inspect evidence branch changes against protected main.")
    changed_paths = [item.decode("utf-8", errors="surrogateescape") for item in changed.stdout.split(b"\0") if item]
    unexpected = sorted(path for path in changed_paths if not path.startswith("docs/"))
    if unexpected:
        raise QualificationResumeSafetyError(f"Evidence branch contains non-documentation changes: {unexpected!r}.")


def _identity_from_binding(
    binding: EvidenceBinding,
    *,
    main_sha: str,
    evidence_sha: str,
    evidence_pr_number: int,
) -> ResumeIdentity:
    manifest = binding.manifest
    if manifest is None:
        raise QualificationResumeError("Blocked qualification requires a canonical manifest before resume.")
    candidate = _mapping(manifest.get("candidate"), "manifest candidate")
    release = _mapping(manifest.get("release"), "manifest release")
    canonical_evidence = _mapping(manifest.get("canonical_evidence"), "manifest canonical evidence")
    input_digests = _mapping(manifest.get("input_digests"), "manifest input digests")
    release_receipt = _mapping(manifest.get("release_receipt"), "manifest release receipt")
    signed_ui = _mapping(manifest.get("signed_ui_artifact"), "manifest signed UI artifact")
    return ResumeIdentity(
        release_tag=_string(candidate.get("release_tag"), "manifest release tag"),
        candidate_sha=_sha(candidate.get("source_sha"), "manifest candidate SHA"),
        release_id=_integer(release.get("id"), "manifest release ID"),
        manifest_sha256=_sha256(manifest.get("manifest_sha256"), "manifest self digest"),
        runner_sha=_sha(manifest.get("runner_sha"), "manifest runner SHA"),
        main_sha=_sha(main_sha, "protected main SHA"),
        evidence_ref=_string(canonical_evidence.get("ref"), "manifest evidence ref"),
        evidence_sha=_sha(evidence_sha, "evidence branch SHA"),
        evidence_base_sha=_sha(canonical_evidence.get("base_sha"), "manifest evidence base SHA"),
        evidence_pr_number=_integer(evidence_pr_number, "evidence pull request number"),
        release_receipt_file_sha256=_sha256(
            release_receipt.get("file_sha256"),
            "manifest release receipt file digest",
        ),
        signed_ui_artifact_id=_integer(signed_ui.get("artifact_id"), "manifest signed UI artifact ID"),
        signed_ui_artifact_sha256=_sha256(
            signed_ui.get("artifact_sha256"),
            "manifest signed UI artifact digest",
        ),
        policy_sha256=_sha256(input_digests.get("policy"), "manifest policy digest"),
        policy_checkpoint_sha256=_sha256(
            input_digests.get("policy_checkpoint"),
            "manifest policy checkpoint digest",
        ),
        route_table_sha256=_sha256(input_digests.get("route_table"), "manifest route table digest"),
        controller_runner_sha256=_sha256(
            input_digests.get("controller_runner"),
            "manifest controller runner digest",
        ),
    )


def _workflow_runs(client: GitHubAPI, identity: ResumeIdentity) -> tuple[list[Mapping[str, Any]], int]:
    payload = _mapping(
        client.get_json(_workflow_runs_endpoint(), active_auth=True),
        "Milestone Qualification workflow runs",
    )
    runs = [
        _mapping(item, "Milestone Qualification workflow run")
        for item in _sequence(payload.get("workflow_runs"), "workflow runs")
    ]
    high_water = max((_integer(run.get("id"), "workflow run ID") for run in runs), default=0)
    matches = [run for run in runs if _run_matches(run, identity)]
    return sorted(matches, key=lambda run: cast(int, run["id"]), reverse=True), high_water


def _run_matches(run: Mapping[str, Any], identity: ResumeIdentity) -> bool:
    actor = run.get("actor")
    triggering_actor = run.get("triggering_actor")
    return (
        run.get("name") == WORKFLOW_NAME
        and run.get("path") == WORKFLOW_PATH
        and run.get("display_title") == identity.workflow_display_title
        and run.get("event") == "workflow_dispatch"
        and run.get("head_branch") == MAIN_BRANCH
        and run.get("head_sha") == identity.runner_sha
        and isinstance(actor, Mapping)
        and actor.get("login") == REPOSITORY_OWNER
        and isinstance(triggering_actor, Mapping)
        and triggering_actor.get("login") == REPOSITORY_OWNER
        and isinstance(run.get("id"), int)
        and not isinstance(run.get("id"), bool)
    )


def _run_identity(run: Mapping[str, Any], identity: ResumeIdentity) -> dict[str, object]:
    if not _run_matches(run, identity):
        raise QualificationResumeSafetyError("Milestone Qualification run identity conflicts with resume inputs.")
    status = _string(run.get("status"), "workflow run status")
    conclusion = run.get("conclusion")
    if conclusion is not None and not isinstance(conclusion, str):
        raise QualificationResumeError("Workflow run conclusion must be a string or null.")
    return {
        "id": _integer(run.get("id"), "workflow run ID"),
        "run_attempt": _integer(run.get("run_attempt"), "workflow run attempt"),
        "status": status,
        "conclusion": conclusion,
        "url": _string(run.get("html_url"), "workflow run URL"),
    }


def _workflow_run(client: GitHubAPI, run_id: int, identity: ResumeIdentity) -> Mapping[str, Any]:
    run = _mapping(
        client.get_json(f"repos/{REPOSITORY}/actions/runs/{run_id}", active_auth=True),
        "Milestone Qualification workflow run",
    )
    _run_identity(run, identity)
    return run


def _run_jobs_succeeded(client: GitHubAPI, run_id: int) -> bool:
    payload = _mapping(
        client.get_json(
            f"repos/{REPOSITORY}/actions/runs/{run_id}/jobs?per_page=100",
            active_auth=True,
        ),
        "workflow jobs",
    )
    jobs = [
        _mapping(item, "workflow job")
        for item in _sequence(payload.get("jobs"), "workflow jobs")
        if isinstance(item, Mapping) and item.get("name") == WORKFLOW_JOB_NAME
    ]
    if len(jobs) != 1:
        raise QualificationResumeSafetyError(
            f"Expected exactly one {WORKFLOW_JOB_NAME!r} job for run {run_id}; found {len(jobs)}."
        )
    return jobs[0].get("status") == "completed" and jobs[0].get("conclusion") == "success"


def _artifact_state(client: GitHubAPI, identity: ResumeIdentity, run: Mapping[str, Any]) -> dict[str, object]:
    run_id = _integer(run.get("id"), "workflow run ID")
    run_attempt = _integer(run.get("run_attempt"), "workflow run attempt")
    payload = _mapping(
        client.get_json(
            f"repos/{REPOSITORY}/actions/runs/{run_id}/artifacts?per_page=100",
            active_auth=True,
        ),
        "run artifacts",
    )
    expected_name = f"milestone-qualification-{identity.release_tag}-{run_attempt}"
    matches = [
        _mapping(item, "Milestone Qualification artifact")
        for item in _sequence(payload.get("artifacts"), "run artifacts")
        if isinstance(item, Mapping) and item.get("name") == expected_name
    ]
    if len(matches) > 1:
        raise QualificationResumeSafetyError(
            f"Multiple exact Milestone Qualification artifacts exist for run {run_id}."
        )
    if not matches:
        return {"state": "missing", "name": expected_name, "run_id": run_id}
    artifact = matches[0]
    workflow_run = _mapping(artifact.get("workflow_run"), "artifact workflow run")
    if (
        workflow_run.get("id") != run_id
        or workflow_run.get("head_branch") != MAIN_BRANCH
        or workflow_run.get("head_sha") != identity.runner_sha
    ):
        raise QualificationResumeSafetyError("Milestone Qualification artifact workflow identity conflicts.")
    digest = _string(artifact.get("digest"), "artifact digest")
    if not digest.startswith("sha256:") or SHA256_PATTERN.fullmatch(digest.removeprefix("sha256:")) is None:
        raise QualificationResumeError("Milestone Qualification artifact digest is invalid.")
    expired = artifact.get("expired")
    if not isinstance(expired, bool):
        raise QualificationResumeError("Milestone Qualification artifact expired state must be boolean.")
    return {
        "state": "expired" if expired else "available",
        "id": _integer(artifact.get("id"), "Milestone Qualification artifact ID"),
        "name": expected_name,
        "digest": digest,
        "expired": expired,
        "run_id": run_id,
    }


def _result(
    state: str,
    exit_code: int,
    status_payload: Mapping[str, Any],
    *,
    identity: ResumeIdentity | None = None,
    checkpoint_path: Path | None = None,
    run: Mapping[str, object] | None = None,
    artifact: Mapping[str, object] | None = None,
    next_action: str,
    mutation: Mapping[str, object] | None = None,
) -> ResumeResult:
    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "resume_type": RESUME_TYPE,
        "state": state,
        "next_action": next_action,
        "qualification_status": {
            "release_tag": status_payload.get("release_tag"),
            "candidate_sha": status_payload.get("candidate_sha"),
            "overall_status": status_payload.get("overall_status"),
            "passed": status_payload.get("passed"),
            "summary": status_payload.get("summary"),
            "groups": status_payload.get("groups"),
        },
        "identity": identity.payload() if identity is not None else None,
        "identity_sha256": identity.digest if identity is not None else None,
        "checkpoint": {
            "present": checkpoint_path is not None and checkpoint_path.exists(),
        },
        "run": dict(run) if run is not None else None,
        "artifact": dict(artifact) if artifact is not None else None,
        "planned_mutation": dict(mutation) if mutation is not None else None,
    }
    return ResumeResult(payload=payload, exit_code=exit_code)


def _validate_expected_mutation(
    identity: ResumeIdentity,
    *,
    expected_main_sha: str | None,
    expected_manifest_sha256: str | None,
) -> bool:
    if expected_main_sha is None and expected_manifest_sha256 is None:
        return False
    if expected_main_sha is None or expected_manifest_sha256 is None:
        raise QualificationResumeSafetyError(
            "Dispatch authorization requires both expected main SHA and expected manifest SHA-256."
        )
    if _sha(expected_main_sha, "expected main SHA") != identity.main_sha:
        raise QualificationResumeSafetyError("Expected main SHA does not match protected main.")
    if _sha256(expected_manifest_sha256, "expected manifest digest") != identity.manifest_sha256:
        raise QualificationResumeSafetyError("Expected manifest digest does not match checked evidence.")
    return True


def _require_dispatch_actor(client: GitHubAPI) -> None:
    user = _mapping(client.get_json("user", active_auth=True), "active GitHub user")
    if user.get("login") != REPOSITORY_OWNER:
        raise QualificationResumeSafetyError(
            f"Milestone Qualification dispatch requires active GitHub identity {REPOSITORY_OWNER!r}."
        )


def _revalidate_remote_identity(client: GitHubAPI, identity: ResumeIdentity) -> None:
    if _ref_sha(client, MAIN_BRANCH) != identity.main_sha:
        raise QualificationResumeSafetyError("Protected main moved after qualification resume preflight.")
    if _ref_sha(client, identity.evidence_ref) != identity.evidence_sha:
        raise QualificationResumeSafetyError("Evidence branch moved after qualification resume preflight.")
    pull = _open_evidence_pr(client, identity.evidence_ref, identity.evidence_sha)
    if pull.get("number") != identity.evidence_pr_number:
        raise QualificationResumeSafetyError("Evidence pull request changed after qualification resume preflight.")


def _dispatch(
    client: GitHubAPI,
    identity: ResumeIdentity,
    checkpoint_path: Path,
    *,
    high_water_run_id: int,
    retry_of_run_id: int | None,
    replace_prepared_checkpoint_sha256: str | None,
    status_payload: Mapping[str, Any],
    poll_attempts: int,
    poll_seconds: float,
    sleep: Sleep,
) -> ResumeResult:
    with _checkpoint_lock(checkpoint_path):
        return _dispatch_locked(
            client,
            identity,
            checkpoint_path,
            high_water_run_id=high_water_run_id,
            retry_of_run_id=retry_of_run_id,
            replace_prepared_checkpoint_sha256=replace_prepared_checkpoint_sha256,
            status_payload=status_payload,
            poll_attempts=poll_attempts,
            poll_seconds=poll_seconds,
            sleep=sleep,
        )


def _dispatch_locked(
    client: GitHubAPI,
    identity: ResumeIdentity,
    checkpoint_path: Path,
    *,
    high_water_run_id: int,
    retry_of_run_id: int | None,
    replace_prepared_checkpoint_sha256: str | None,
    status_payload: Mapping[str, Any],
    poll_attempts: int,
    poll_seconds: float,
    sleep: Sleep,
) -> ResumeResult:
    _require_dispatch_actor(client)
    _revalidate_remote_identity(client, identity)
    existing_checkpoint = _load_checkpoint(checkpoint_path)
    if existing_checkpoint is not None:
        existing_dispatch = _validate_checkpoint(existing_checkpoint, identity)
        existing_state = cast(str, existing_dispatch["state"])
        existing_run_id = cast(int | None, existing_dispatch.get("run_id"))
        if replace_prepared_checkpoint_sha256 is not None:
            recorded_digest = _sha256(
                existing_checkpoint.get("checkpoint_sha256"),
                "prepared checkpoint self digest",
            )
            if existing_state != "prepared" or recorded_digest != replace_prepared_checkpoint_sha256:
                raise QualificationResumeSafetyError(
                    "Prepared checkpoint retry does not match the serialized qualification checkpoint."
                )
        elif retry_of_run_id is None:
            return _result(
                "dispatch_visibility_pending" if existing_state == "prepared" else "running",
                EXIT_SUCCESS,
                status_payload,
                identity=identity,
                checkpoint_path=checkpoint_path,
                next_action="A serialized dispatch checkpoint already exists; run resume again to observe it.",
            )
        elif existing_state != "observed" or existing_run_id != retry_of_run_id:
            raise QualificationResumeSafetyError(
                "Retry dispatch conflicts with the serialized qualification checkpoint."
            )
    prepared = _checkpoint_payload(
        identity,
        state="prepared",
        high_water_run_id=high_water_run_id,
        retry_of_run_id=retry_of_run_id,
    )
    _write_checkpoint(checkpoint_path, prepared)
    dispatch_payload: dict[str, object] = {
        "ref": MAIN_BRANCH,
        "inputs": {
            "candidate_tag": identity.release_tag,
            "manifest_sha256": identity.manifest_sha256,
        },
    }
    client.post_json(_workflow_dispatch_endpoint(), dispatch_payload, active_auth=True)
    for attempt in range(max(1, poll_attempts)):
        matches, _high_water = _workflow_runs(client, identity)
        new_matches = [run for run in matches if cast(int, run["id"]) > high_water_run_id]
        if len(new_matches) > 1:
            raise QualificationResumeSafetyError("Multiple workflow runs appeared for one qualification dispatch.")
        if len(new_matches) == 1:
            run_payload = _run_identity(new_matches[0], identity)
            observed = _checkpoint_payload(
                identity,
                state="observed",
                high_water_run_id=high_water_run_id,
                retry_of_run_id=retry_of_run_id,
                run_id=cast(int, run_payload["id"]),
                run_attempt=cast(int, run_payload["run_attempt"]),
            )
            _write_checkpoint(checkpoint_path, observed)
            return _result(
                "running",
                EXIT_SUCCESS,
                status_payload,
                identity=identity,
                checkpoint_path=checkpoint_path,
                run=run_payload,
                next_action="Wait for the exact Milestone Qualification run to finish, then run resume again.",
            )
        if attempt + 1 < max(1, poll_attempts):
            sleep(poll_seconds)
    return _result(
        "dispatch_visibility_pending",
        EXIT_SUCCESS,
        status_payload,
        identity=identity,
        checkpoint_path=checkpoint_path,
        next_action="The dispatch was accepted but its run is not visible yet; run resume again without redispatching.",
    )


def resume_qualification(
    repo_root: Path,
    release_tag: str,
    *,
    expected_main_sha: str | None = None,
    expected_manifest_sha256: str | None = None,
    retry_run_id: int | None = None,
    retry_checkpoint_sha256: str | None = None,
    client: GitHubAPI | None = None,
    checkpoint_path: Path | None = None,
    poll_attempts: int = 10,
    poll_seconds: float = 2.0,
    sleep: Sleep = time.sleep,
    prepared_retry_after_seconds: int = PREPARED_RETRY_AFTER_SECONDS,
    now: Clock = lambda: datetime.now(timezone.utc),
) -> ResumeResult:
    repo_root = repo_root.resolve()
    if retry_run_id is not None:
        retry_run_id = _integer(retry_run_id, "retry run ID")
    if retry_checkpoint_sha256 is not None:
        retry_checkpoint_sha256 = _sha256(retry_checkpoint_sha256, "retry checkpoint digest")
    if prepared_retry_after_seconds < 0:
        raise QualificationResumeError("Prepared checkpoint retry delay must be nonnegative.")
    try:
        status_payload = build_status(repo_root, release_tag)
    except (
        json.JSONDecodeError,
        OSError,
        QualificationScopeError,
        ReleaseMilestoneContextError,
        ReleaseQualificationControllerError,
        ReleaseQualificationManifestError,
    ) as error:
        raise QualificationResumeError("Checked qualification status could not be resolved.") from error
    groups = _mapping(status_payload.get("groups"), "qualification status groups")
    blocking = _sequence(groups.get("blocking"), "blocking qualification cases")
    if not blocking:
        return _result(
            "complete",
            EXIT_SUCCESS,
            status_payload,
            next_action="No blocking qualification work remains; no GitHub or git mutation was performed.",
        )

    try:
        binding = resolve_evidence_binding(repo_root, release_tag)
    except (
        json.JSONDecodeError,
        OSError,
        ReleaseMilestoneContextError,
        ReleaseQualificationControllerError,
        ReleaseQualificationManifestError,
    ) as error:
        raise QualificationResumeError("Checked qualification manifest could not be resolved.") from error
    if binding.manifest is None:
        return _result(
            "manifest_missing",
            EXIT_OPERATOR_REQUIRED,
            status_payload,
            next_action="Refresh Release Evidence so the blocked release has a canonical qualification manifest.",
        )

    github = client or GhAPIClient()
    _validate_repository(github)
    main_sha = _ref_sha(github, MAIN_BRANCH)
    canonical_evidence = _mapping(binding.manifest.get("canonical_evidence"), "manifest canonical evidence")
    evidence_ref = _string(canonical_evidence.get("ref"), "manifest evidence ref")
    evidence_sha = _ref_sha(github, evidence_ref)
    pull = _open_evidence_pr(github, evidence_ref, evidence_sha)
    identity = _identity_from_binding(
        binding,
        main_sha=main_sha,
        evidence_sha=evidence_sha,
        evidence_pr_number=_integer(pull.get("number"), "evidence pull request number"),
    )
    if identity.runner_sha != identity.main_sha:
        return _result(
            "runner_stale",
            EXIT_OPERATOR_REQUIRED,
            status_payload,
            identity=identity,
            next_action="Rerun Release Evidence to refresh the manifest against the current protected main SHA.",
        )
    _require_local_evidence_checkout(repo_root, identity)

    resolved_checkpoint_path = checkpoint_path or _checkpoint_path(repo_root, release_tag)
    if resolved_checkpoint_path.exists():
        with _checkpoint_lock(resolved_checkpoint_path):
            checkpoint = _load_checkpoint(resolved_checkpoint_path)
    else:
        checkpoint = None
    matches, high_water_run_id = _workflow_runs(github, identity)
    active = [run for run in matches if run.get("status") in ACTIVE_RUN_STATUSES]
    if len(active) > 1:
        raise QualificationResumeSafetyError("Multiple active exact Milestone Qualification runs exist.")
    if checkpoint is not None:
        dispatch_checkpoint = _validate_checkpoint(checkpoint, identity)
        checkpoint_state = cast(str, dispatch_checkpoint["state"])
        checkpoint_high_water = cast(int, dispatch_checkpoint["high_water_run_id"])
        checkpoint_digest = _sha256(
            checkpoint.get("checkpoint_sha256"),
            "checkpoint self digest",
        )
        if checkpoint_state == "prepared":
            new_matches = [run for run in matches if cast(int, run["id"]) > checkpoint_high_water]
            if len(new_matches) > 1:
                raise QualificationResumeSafetyError("Multiple runs match one prepared qualification dispatch.")
            if not new_matches:
                if retry_checkpoint_sha256 is not None:
                    if retry_checkpoint_sha256 != checkpoint_digest:
                        raise QualificationResumeSafetyError(
                            "Retry checkpoint digest does not match the prepared dispatch."
                        )
                    prepared_at = datetime.fromisoformat(
                        _string(dispatch_checkpoint.get("prepared_at"), "checkpoint prepared timestamp").replace(
                            "Z", "+00:00"
                        )
                    )
                    age_seconds = (
                        now().astimezone(timezone.utc) - prepared_at.astimezone(timezone.utc)
                    ).total_seconds()
                    if age_seconds < prepared_retry_after_seconds:
                        return _result(
                            "dispatch_visibility_pending",
                            EXIT_OPERATOR_REQUIRED,
                            status_payload,
                            identity=identity,
                            checkpoint_path=resolved_checkpoint_path,
                            next_action=(
                                "The prepared dispatch is still inside its visibility window; wait before retrying."
                            ),
                        )
                    authorized_retry = _validate_expected_mutation(
                        identity,
                        expected_main_sha=expected_main_sha,
                        expected_manifest_sha256=expected_manifest_sha256,
                    )
                    if not authorized_retry:
                        raise QualificationResumeSafetyError(
                            "Prepared dispatch retry requires exact main and manifest authorization."
                        )
                    return _dispatch(
                        github,
                        identity,
                        resolved_checkpoint_path,
                        high_water_run_id=high_water_run_id,
                        retry_of_run_id=None,
                        replace_prepared_checkpoint_sha256=checkpoint_digest,
                        status_payload=status_payload,
                        poll_attempts=poll_attempts,
                        poll_seconds=poll_seconds,
                        sleep=sleep,
                    )
                return _result(
                    "dispatch_visibility_pending",
                    EXIT_OPERATOR_REQUIRED,
                    status_payload,
                    identity=identity,
                    checkpoint_path=resolved_checkpoint_path,
                    next_action=(
                        "A dispatch checkpoint is prepared; wait for its exact run or, after the visibility window, "
                        f"retry with --retry-checkpoint-sha256 {checkpoint_digest}."
                    ),
                )
            observed_run = _run_identity(new_matches[0], identity)
            checkpoint = _checkpoint_payload(
                identity,
                state="observed",
                high_water_run_id=checkpoint_high_water,
                retry_of_run_id=cast(int | None, dispatch_checkpoint.get("retry_of_run_id")),
                run_id=cast(int, observed_run["id"]),
                run_attempt=cast(int, observed_run["run_attempt"]),
            )
            with _checkpoint_lock(resolved_checkpoint_path):
                current_checkpoint = _load_checkpoint(resolved_checkpoint_path)
                if current_checkpoint is None or current_checkpoint.get("checkpoint_sha256") != checkpoint_digest:
                    raise QualificationResumeSafetyError(
                        "Qualification checkpoint moved before the observed run could be recorded."
                    )
                _write_checkpoint(resolved_checkpoint_path, checkpoint)
        observed_dispatch = _mapping(checkpoint.get("dispatch"), "checkpoint dispatch")
        observed_run_id = _integer(observed_dispatch.get("run_id"), "checkpoint observed run ID")
        selected_run = _workflow_run(github, observed_run_id, identity)
    else:
        terminal = [run for run in matches if run.get("status") == TERMINAL_RUN_STATUS]
        selected_run = active[0] if active else terminal[0] if terminal else None

    if active and selected_run is not None and selected_run.get("id") != active[0].get("id"):
        raise QualificationResumeSafetyError("An active exact run conflicts with the checkpoint-observed run.")
    if selected_run is not None and selected_run.get("status") in ACTIVE_RUN_STATUSES:
        run_payload = _run_identity(selected_run, identity)
        return _result(
            "running",
            EXIT_SUCCESS,
            status_payload,
            identity=identity,
            checkpoint_path=resolved_checkpoint_path,
            run=run_payload,
            next_action="Wait for the exact Milestone Qualification run to finish, then run resume again.",
        )

    authorized = _validate_expected_mutation(
        identity,
        expected_main_sha=expected_main_sha,
        expected_manifest_sha256=expected_manifest_sha256,
    )
    if selected_run is None:
        if retry_run_id is not None:
            raise QualificationResumeSafetyError("Retry run ID does not match any exact terminal qualification run.")
        mutation = {
            "operation": "dispatch",
            "endpoint": _workflow_dispatch_endpoint(),
            "ref": MAIN_BRANCH,
            "candidate_tag": identity.release_tag,
            "manifest_sha256": identity.manifest_sha256,
        }
        if not authorized:
            return _result(
                "dispatch_ready",
                EXIT_OPERATOR_REQUIRED,
                status_payload,
                identity=identity,
                checkpoint_path=resolved_checkpoint_path,
                mutation=mutation,
                next_action="Rerun resume with the exact expected main and manifest digests to authorize dispatch.",
            )
        return _dispatch(
            github,
            identity,
            resolved_checkpoint_path,
            high_water_run_id=high_water_run_id,
            retry_of_run_id=None,
            replace_prepared_checkpoint_sha256=None,
            status_payload=status_payload,
            poll_attempts=poll_attempts,
            poll_seconds=poll_seconds,
            sleep=sleep,
        )

    run_payload = _run_identity(selected_run, identity)
    run_id = cast(int, run_payload["id"])
    if run_payload["status"] != TERMINAL_RUN_STATUS:
        raise QualificationResumeSafetyError("Selected Milestone Qualification run has an unsupported state.")
    succeeded = run_payload["conclusion"] == "success" and _run_jobs_succeeded(github, run_id)
    artifact = _artifact_state(github, identity, selected_run) if succeeded else None
    if succeeded and artifact is not None and artifact["state"] == "available":
        return _result(
            "artifact_available",
            EXIT_OPERATOR_REQUIRED,
            status_payload,
            identity=identity,
            checkpoint_path=resolved_checkpoint_path,
            run=run_payload,
            artifact=artifact,
            next_action="Validate and reconcile the exact qualification artifact into the evidence branch.",
        )

    failure_state = (
        "artifact_expired" if succeeded and artifact is not None and artifact["state"] == "expired" else "failed"
    )
    if retry_run_id != run_id:
        return _result(
            failure_state,
            EXIT_OPERATOR_REQUIRED,
            status_payload,
            identity=identity,
            checkpoint_path=resolved_checkpoint_path,
            run=run_payload,
            artifact=artifact,
            next_action=f"Rerun resume with --retry-run-id {run_id} and the exact expected main and manifest digests.",
        )
    if not authorized:
        raise QualificationResumeSafetyError("Retry authorization requires exact expected main and manifest digests.")
    return _dispatch(
        github,
        identity,
        resolved_checkpoint_path,
        high_water_run_id=high_water_run_id,
        retry_of_run_id=run_id,
        replace_prepared_checkpoint_sha256=None,
        status_payload=status_payload,
        poll_attempts=poll_attempts,
        poll_seconds=poll_seconds,
        sleep=sleep,
    )


def safety_error_payload(error: Exception, *, state: str = "conflict") -> dict[str, object]:
    message = ABSOLUTE_PATH_PATTERN.sub("<local-path>", str(error))
    return {
        "schema_version": SCHEMA_VERSION,
        "resume_type": RESUME_TYPE,
        "state": state,
        "error": message,
    }


def default_client() -> GitHubAPI:
    return GhAPIClient()


__all__ = [
    "EXIT_FAILED",
    "EXIT_OPERATOR_REQUIRED",
    "EXIT_SAFETY_ERROR",
    "EXIT_SUCCESS",
    "QualificationResumeError",
    "QualificationResumeSafetyError",
    "ResumeIdentity",
    "ResumeResult",
    "resume_qualification",
    "safety_error_payload",
]
