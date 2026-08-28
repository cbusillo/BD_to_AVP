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
from scripts.release_evidence_v2 import ReleaseEvidenceV2Error, validate_v2_bundle
from scripts.release_milestone_context import ReleaseMilestoneContextError
from scripts.release_qualification_artifact import (
    QualificationArtifactError,
    QualificationArtifactSafetyError,
    download_reconciliation_bundle,
)
from scripts.release_qualification_apply import (
    QualificationApplyError,
    QualificationApplySafetyError,
    continue_reconciliation_apply,
    reconciliation_checkpoint_path,
    reconciliation_checkpoint_summary,
    start_reconciliation_apply,
)
from scripts.release_qualification_status import (
    EvidenceBinding,
    ReleaseQualificationControllerError,
    build_status,
    resolve_evidence_binding,
)
from scripts.release_qualification_manifest import ReleaseQualificationManifestError, manifest_sha256


REPOSITORY = "cbusillo/BD_to_AVP"
REPOSITORY_OWNER = "cbusillo"
MAIN_BRANCH = "main"
WORKFLOW_FILE = "milestone-qualification.yml"
WORKFLOW_NAME = "Milestone Qualification"
WORKFLOW_PATH = ".github/workflows/milestone-qualification.yml"
WORKFLOW_JOB_NAME = "Collect exact post-publication qualification receipts"
RESUME_TYPE = "bd_to_avp.release_qualification_resume"
CHECKPOINT_TYPE = "bd_to_avp.release_qualification_resume_checkpoint"
SCHEMA_VERSION = 2
EXIT_SUCCESS = 0
EXIT_FAILED = 1
EXIT_OPERATOR_REQUIRED = 20
EXIT_SAFETY_ERROR = 21
PREPARED_RETRY_AFTER_SECONDS = 600
MAX_WORKFLOW_RUN_PAGES = 20
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

    def get_bytes(
        self,
        endpoint: str,
        *,
        active_auth: bool = False,
        max_bytes: int,
        timeout_seconds: float,
    ) -> bytes:
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


def _identity_from_checkpoint(checkpoint: Mapping[str, Any]) -> ResumeIdentity:
    recorded = _mapping(checkpoint.get("identity"), "checkpoint identity")
    _exact_keys(recorded, set(ResumeIdentity.__dataclass_fields__), "checkpoint identity")
    identity = ResumeIdentity(
        release_tag=_string(recorded.get("release_tag"), "checkpoint release tag"),
        candidate_sha=_sha(recorded.get("candidate_sha"), "checkpoint candidate SHA"),
        release_id=_integer(recorded.get("release_id"), "checkpoint release ID"),
        manifest_sha256=_sha256(recorded.get("manifest_sha256"), "checkpoint manifest digest"),
        runner_sha=_sha(recorded.get("runner_sha"), "checkpoint runner SHA"),
        main_sha=_sha(recorded.get("main_sha"), "checkpoint main SHA"),
        evidence_ref=_string(recorded.get("evidence_ref"), "checkpoint evidence ref"),
        evidence_sha=_sha(recorded.get("evidence_sha"), "checkpoint evidence SHA"),
        evidence_base_sha=_sha(recorded.get("evidence_base_sha"), "checkpoint evidence base SHA"),
        release_receipt_file_sha256=_sha256(
            recorded.get("release_receipt_file_sha256"),
            "checkpoint release receipt digest",
        ),
        signed_ui_artifact_id=_integer(
            recorded.get("signed_ui_artifact_id"),
            "checkpoint signed UI artifact ID",
        ),
        signed_ui_artifact_sha256=_sha256(
            recorded.get("signed_ui_artifact_sha256"),
            "checkpoint signed UI artifact digest",
        ),
        policy_sha256=_sha256(recorded.get("policy_sha256"), "checkpoint policy digest"),
        policy_checkpoint_sha256=_sha256(
            recorded.get("policy_checkpoint_sha256"),
            "checkpoint policy checkpoint digest",
        ),
        route_table_sha256=_sha256(
            recorded.get("route_table_sha256"),
            "checkpoint route table digest",
        ),
        controller_runner_sha256=_sha256(
            recorded.get("controller_runner_sha256"),
            "checkpoint controller runner digest",
        ),
    )
    if checkpoint.get("identity_sha256") != identity.digest:
        raise QualificationResumeSafetyError("Qualification resume checkpoint identity digest is invalid.")
    return identity


def _validate_checkpoint_rebind(
    repo_root: Path,
    previous: ResumeIdentity,
    current: ResumeIdentity,
    current_manifest: Mapping[str, Any],
) -> None:
    immutable_fields = (
        "release_tag",
        "candidate_sha",
        "release_id",
        "evidence_ref",
        "release_receipt_file_sha256",
        "signed_ui_artifact_id",
        "signed_ui_artifact_sha256",
        "policy_sha256",
        "policy_checkpoint_sha256",
        "route_table_sha256",
    )
    changed = [field for field in immutable_fields if getattr(previous, field) != getattr(current, field)]
    if changed:
        raise QualificationResumeSafetyError(
            f"Qualification checkpoint rebind changes immutable release fields: {changed!r}."
        )
    if previous.runner_sha != previous.main_sha or current.runner_sha != current.main_sha:
        raise QualificationResumeSafetyError(
            "Qualification checkpoint rebind requires protected-main runner identities."
        )
    ancestor = cast(
        subprocess.CompletedProcess[str],
        _git(repo_root, ["merge-base", "--is-ancestor", previous.main_sha, current.main_sha]),
    )
    if ancestor.returncode != 0:
        raise QualificationResumeSafetyError(
            "Qualification checkpoint rebind requires the refreshed protected main to descend from the prior runner."
        )
    previous_manifest = _manifest_at_revision(repo_root, previous)
    if manifest_sha256(current_manifest) != current.manifest_sha256:
        raise QualificationResumeSafetyError(
            "Refreshed qualification manifest digest changed during checkpoint rebind."
        )
    if _checkpoint_rebind_manifest_projection(previous_manifest) != _checkpoint_rebind_manifest_projection(
        current_manifest
    ):
        raise QualificationResumeSafetyError(
            "Qualification checkpoint rebind changes decision-bearing manifest inputs."
        )


def _manifest_at_revision(repo_root: Path, identity: ResumeIdentity) -> Mapping[str, Any]:
    manifest_path = f"docs/release-evidence/{identity.release_tag}/qualification-manifest.json"
    result = cast(
        subprocess.CompletedProcess[str],
        _git(repo_root, ["show", f"{identity.evidence_sha}:{manifest_path}"]),
    )
    if result.returncode != 0:
        raise QualificationResumeSafetyError("Unable to load the prior qualification manifest for checkpoint rebind.")
    try:
        manifest = _mapping(json.loads(result.stdout), "prior qualification manifest")
    except json.JSONDecodeError as error:
        raise QualificationResumeSafetyError("Prior qualification manifest is invalid JSON.") from error
    recorded_digest = _sha256(manifest.get("manifest_sha256"), "prior qualification manifest digest")
    if recorded_digest != identity.manifest_sha256 or manifest_sha256(manifest) != recorded_digest:
        raise QualificationResumeSafetyError("Prior qualification manifest digest conflicts with the checkpoint.")
    return manifest


def _checkpoint_rebind_manifest_projection(manifest: Mapping[str, Any]) -> Mapping[str, Any]:
    projection = _mapping(json.loads(json.dumps(manifest)), "qualification manifest projection")
    normalized = dict(projection)
    normalized.pop("manifest_sha256", None)
    normalized.pop("runner_sha", None)
    canonical_evidence = dict(_mapping(normalized.get("canonical_evidence"), "manifest canonical evidence"))
    canonical_evidence.pop("base_sha", None)
    normalized["canonical_evidence"] = canonical_evidence
    return normalized


def _ref_endpoint(branch: str) -> str:
    return f"repos/{REPOSITORY}/git/ref/heads/{quote(branch, safe='')}"


def _workflow_runs_endpoint(page: int = 1) -> str:
    endpoint = (
        f"repos/{REPOSITORY}/actions/workflows/{WORKFLOW_FILE}/runs"
        f"?event=workflow_dispatch&branch={MAIN_BRANCH}&per_page=100"
    )
    return endpoint if page == 1 else f"{endpoint}&page={page}"


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


def _require_durable_capture_checkpoint(repo_root: Path, identity: ResumeIdentity) -> None:
    try:
        result = validate_v2_bundle(
            repo_root,
            identity.release_tag,
            verification_revision=identity.evidence_sha,
        )
    except ReleaseEvidenceV2Error as error:
        raise QualificationResumeSafetyError(
            "Evidence branch commit does not contain a valid durable v2 capture checkpoint."
        ) from error
    if result.get("class") not in {"v2-captured", "v2-qualified"}:
        raise QualificationResumeSafetyError(
            "Evidence branch commit does not contain an active CAPTURED qualification checkpoint."
        )


def _identity_from_binding(
    binding: EvidenceBinding,
    *,
    main_sha: str,
    evidence_sha: str,
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
    runs: list[Mapping[str, Any]] = []
    for page in range(1, MAX_WORKFLOW_RUN_PAGES + 1):
        payload = _mapping(
            client.get_json(_workflow_runs_endpoint(page), active_auth=True),
            "Milestone Qualification workflow runs",
        )
        page_runs = [
            _mapping(item, "Milestone Qualification workflow run")
            for item in _sequence(payload.get("workflow_runs"), "workflow runs")
        ]
        runs.extend(page_runs)
        if len(page_runs) < 100:
            break
        if page == MAX_WORKFLOW_RUN_PAGES:
            raise QualificationResumeSafetyError(
                "Milestone Qualification workflow history exceeds the bounded identity scan."
            )
    high_water = max((_integer(run.get("id"), "workflow run ID") for run in runs), default=0)
    matches = [run for run in runs if _run_matches(run, identity)]
    return sorted(matches, key=lambda run: cast(int, run["id"]), reverse=True), high_water


def _run_matches(run: Mapping[str, Any], identity: ResumeIdentity) -> bool:
    actor = run.get("actor")
    triggering_actor = run.get("triggering_actor")
    run_name = run.get("name")
    return (
        run_name in {WORKFLOW_NAME, identity.workflow_display_title}
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
    apply_checkpoint_path: Path | None = None,
    run: Mapping[str, object] | None = None,
    artifact: Mapping[str, object] | None = None,
    reconciliation_plan: Mapping[str, object] | None = None,
    reconciliation_result: Mapping[str, object] | None = None,
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
        "apply_checkpoint": _apply_checkpoint_summary(apply_checkpoint_path)
        if apply_checkpoint_path is not None
        else None,
        "run": dict(run) if run is not None else None,
        "artifact": dict(artifact) if artifact is not None else None,
        "reconciliation_plan": dict(reconciliation_plan) if reconciliation_plan is not None else None,
        "reconciliation_result": dict(reconciliation_result) if reconciliation_result is not None else None,
        "planned_mutation": dict(mutation) if mutation is not None else None,
    }
    return ResumeResult(payload=payload, exit_code=exit_code)


def _apply_checkpoint_summary(path: Path) -> dict[str, object] | None:
    try:
        return reconciliation_checkpoint_summary(path)
    except QualificationApplySafetyError as error:
        raise QualificationResumeSafetyError(str(error)) from error
    except QualificationApplyError as error:
        raise QualificationResumeError(str(error)) from error


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


def _require_mutation_actor(client: GitHubAPI) -> tuple[str, int]:
    user = _mapping(client.get_json("user", active_auth=True), "active GitHub user")
    if user.get("login") != REPOSITORY_OWNER:
        raise QualificationResumeSafetyError(
            f"Qualification mutation requires active GitHub identity {REPOSITORY_OWNER!r}."
        )
    return REPOSITORY_OWNER, _integer(user.get("id"), "active GitHub user ID")


def _require_dispatch_actor(client: GitHubAPI) -> None:
    user = _mapping(client.get_json("user", active_auth=True), "active GitHub user")
    if user.get("login") != REPOSITORY_OWNER:
        raise QualificationResumeSafetyError(
            f"Milestone Qualification dispatch requires active GitHub identity {REPOSITORY_OWNER!r}."
        )


def _revalidate_remote_identity(
    client: GitHubAPI,
    identity: ResumeIdentity,
    *,
    expected_evidence_sha: str | None = None,
) -> None:
    if _ref_sha(client, MAIN_BRANCH) != identity.main_sha:
        raise QualificationResumeSafetyError("Protected main moved after qualification resume preflight.")
    evidence_sha = (
        identity.evidence_sha
        if expected_evidence_sha is None
        else _sha(
            expected_evidence_sha,
            "expected evidence SHA",
        )
    )
    if _ref_sha(client, identity.evidence_ref) != evidence_sha:
        raise QualificationResumeSafetyError("Evidence branch moved after qualification resume preflight.")


def _require_no_active_exact_runs(client: GitHubAPI, identity: ResumeIdentity) -> None:
    matches, _high_water = _workflow_runs(client, identity)
    active = [run for run in matches if run.get("status") in ACTIVE_RUN_STATUSES]
    if active:
        raise QualificationResumeSafetyError(
            "An exact Milestone Qualification run is active; evidence mutation must wait for it to finish."
        )


def _dispatch(
    client: GitHubAPI,
    identity: ResumeIdentity,
    checkpoint_path: Path,
    *,
    high_water_run_id: int,
    retry_of_run_id: int | None,
    replace_prepared_checkpoint_sha256: str | None,
    replace_observed_checkpoint_sha256: str | None,
    replace_observed_run_conclusion: str | None,
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
            replace_observed_checkpoint_sha256=replace_observed_checkpoint_sha256,
            replace_observed_run_conclusion=replace_observed_run_conclusion,
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
    replace_observed_checkpoint_sha256: str | None,
    replace_observed_run_conclusion: str | None,
    status_payload: Mapping[str, Any],
    poll_attempts: int,
    poll_seconds: float,
    sleep: Sleep,
) -> ResumeResult:
    _require_dispatch_actor(client)
    _revalidate_remote_identity(client, identity)
    if replace_prepared_checkpoint_sha256 is not None and replace_observed_checkpoint_sha256 is not None:
        raise QualificationResumeSafetyError("Dispatch cannot replace prepared and observed checkpoints together.")
    if (replace_observed_checkpoint_sha256 is None) != (replace_observed_run_conclusion is None):
        raise QualificationResumeSafetyError(
            "Observed checkpoint replacement requires its exact terminal run conclusion."
        )
    if replace_observed_run_conclusion not in {None, "failure", "success"}:
        raise QualificationResumeSafetyError("Observed checkpoint replacement conclusion is unsupported.")
    existing_checkpoint = _load_checkpoint(checkpoint_path)
    if existing_checkpoint is not None:
        existing_identity = _identity_from_checkpoint(existing_checkpoint)
        existing_dispatch = _validate_checkpoint(existing_checkpoint, existing_identity)
        existing_state = cast(str, existing_dispatch["state"])
        existing_run_id = cast(int | None, existing_dispatch.get("run_id"))
        existing_run_attempt = cast(int | None, existing_dispatch.get("run_attempt"))
        recorded_digest = _sha256(
            existing_checkpoint.get("checkpoint_sha256"),
            "serialized checkpoint self digest",
        )
        if replace_observed_checkpoint_sha256 is not None:
            if (
                existing_state != "observed"
                or existing_run_id != retry_of_run_id
                or recorded_digest != replace_observed_checkpoint_sha256
            ):
                raise QualificationResumeSafetyError(
                    "Observed checkpoint rebind does not match the serialized qualification checkpoint."
                )
            previous_run = _run_identity(
                _workflow_run(client, cast(int, existing_run_id), existing_identity),
                existing_identity,
            )
            if (
                previous_run["run_attempt"] != existing_run_attempt
                or previous_run["status"] != TERMINAL_RUN_STATUS
                or previous_run["conclusion"] != replace_observed_run_conclusion
            ):
                raise QualificationResumeSafetyError(
                    "Observed checkpoint rebind run changed before the replacement dispatch."
                )
            refreshed_matches, refreshed_high_water = _workflow_runs(client, identity)
            if refreshed_matches:
                raise QualificationResumeSafetyError(
                    "An exact refreshed qualification run appeared before checkpoint rebind dispatch."
                )
            high_water_run_id = max(high_water_run_id, refreshed_high_water)
        elif existing_identity != identity:
            raise QualificationResumeSafetyError(
                "Qualification resume checkpoint conflicts with current release identity."
            )
        elif replace_prepared_checkpoint_sha256 is not None:
            if existing_state != "prepared" or recorded_digest != replace_prepared_checkpoint_sha256:
                raise QualificationResumeSafetyError(
                    "Prepared checkpoint retry does not match the serialized qualification checkpoint."
                )
        elif retry_of_run_id is None:
            checkpoint_state = "dispatch_visibility_pending" if existing_state == "prepared" else "running"
            return _result(
                checkpoint_state,
                EXIT_OPERATOR_REQUIRED if checkpoint_state == "dispatch_visibility_pending" else EXIT_SUCCESS,
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
        EXIT_OPERATOR_REQUIRED,
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
    apply_plan_sha256: str | None = None,
    observe_only: bool = False,
    client: GitHubAPI | None = None,
    checkpoint_path: Path | None = None,
    poll_attempts: int = 10,
    poll_seconds: float = 2.0,
    sleep: Sleep = time.sleep,
    prepared_retry_after_seconds: int = PREPARED_RETRY_AFTER_SECONDS,
    now: Clock = lambda: datetime.now(timezone.utc),
) -> ResumeResult:
    repo_root = repo_root.resolve()
    resolved_checkpoint_path = checkpoint_path or _checkpoint_path(repo_root, release_tag)
    resolved_apply_checkpoint_path = reconciliation_checkpoint_path(resolved_checkpoint_path)
    if retry_run_id is not None:
        retry_run_id = _integer(retry_run_id, "retry run ID")
    if retry_checkpoint_sha256 is not None:
        retry_checkpoint_sha256 = _sha256(retry_checkpoint_sha256, "retry checkpoint digest")
    if apply_plan_sha256 is not None:
        apply_plan_sha256 = _sha256(apply_plan_sha256, "authorized reconciliation plan digest")
    if observe_only and apply_plan_sha256 is not None:
        raise QualificationResumeSafetyError("Observe-only mode cannot authorize reconciliation apply.")
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
    if not blocking and not resolved_apply_checkpoint_path.exists():
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
    identity = _identity_from_binding(
        binding,
        main_sha=main_sha,
        evidence_sha=evidence_sha,
    )
    if identity.runner_sha != identity.main_sha:
        return _result(
            "runner_stale",
            EXIT_OPERATOR_REQUIRED,
            status_payload,
            identity=identity,
            next_action="Rerun Release Evidence to refresh the manifest against the current protected main SHA.",
        )
    _require_durable_capture_checkpoint(repo_root, identity)
    if resolved_apply_checkpoint_path.exists():
        apply_summary = _apply_checkpoint_summary(resolved_apply_checkpoint_path)
        if apply_summary is None:
            raise QualificationResumeSafetyError("Qualification apply checkpoint disappeared during preflight.")
        if apply_plan_sha256 is None and apply_summary.get("state") == "pushed":
            apply_plan_sha256 = _sha256(
                apply_summary.get("plan_sha256"),
                "completed reconciliation plan digest",
            )
        if apply_plan_sha256 is None:
            return _result(
                "reconciliation_apply_pending",
                EXIT_OPERATOR_REQUIRED,
                status_payload,
                identity=identity,
                checkpoint_path=resolved_checkpoint_path,
                apply_checkpoint_path=resolved_apply_checkpoint_path,
                next_action="Rerun resume with the exact apply checkpoint plan SHA-256 to continue reconciliation.",
            )
        try:
            with _checkpoint_lock(resolved_checkpoint_path):
                outcome = continue_reconciliation_apply(
                    repo_root=repo_root,
                    identity=identity,
                    expected_plan_sha256=apply_plan_sha256,
                    checkpoint_path=resolved_apply_checkpoint_path,
                    revalidate_remote=lambda evidence_sha: _revalidate_remote_identity(
                        github,
                        identity,
                        expected_evidence_sha=evidence_sha,
                    ),
                    ensure_no_active_runs=lambda: _require_no_active_exact_runs(github, identity),
                    require_actor=lambda: _require_mutation_actor(github),
                    remote_evidence_sha=lambda: _ref_sha(github, identity.evidence_ref),
                )
        except QualificationApplySafetyError as error:
            raise QualificationResumeSafetyError(str(error)) from error
        except QualificationApplyError as error:
            raise QualificationResumeError(str(error)) from error
        return _result(
            outcome.state,
            EXIT_SUCCESS,
            status_payload,
            identity=identity,
            checkpoint_path=resolved_checkpoint_path,
            apply_checkpoint_path=resolved_apply_checkpoint_path,
            reconciliation_plan=outcome.plan,
            reconciliation_result={
                "commit_sha": outcome.commit_sha,
            },
            next_action="Qualification evidence was committed and pushed to the canonical evidence branch.",
        )
    _require_local_evidence_checkout(repo_root, identity)
    if resolved_checkpoint_path.exists():
        with _checkpoint_lock(resolved_checkpoint_path):
            checkpoint = _load_checkpoint(resolved_checkpoint_path)
    else:
        checkpoint = None
    matches, high_water_run_id = _workflow_runs(github, identity)
    active = [run for run in matches if run.get("status") in ACTIVE_RUN_STATUSES]
    if len(active) > 1:
        raise QualificationResumeSafetyError("Multiple active exact Milestone Qualification runs exist.")
    selected_run: Mapping[str, Any] | None
    if checkpoint is not None:
        checkpoint_identity = _identity_from_checkpoint(checkpoint)
        if checkpoint_identity != identity:
            _validate_checkpoint_rebind(repo_root, checkpoint_identity, identity, binding.manifest)
            dispatch_checkpoint = _validate_checkpoint(checkpoint, checkpoint_identity)
            checkpoint_state = cast(str, dispatch_checkpoint["state"])
            checkpoint_digest = _sha256(
                checkpoint.get("checkpoint_sha256"),
                "checkpoint self digest",
            )
            observed_run_id = cast(int | None, dispatch_checkpoint.get("run_id"))
            observed_run_attempt = cast(int | None, dispatch_checkpoint.get("run_attempt"))
            if checkpoint_state != "observed" or observed_run_id is None or observed_run_attempt is None:
                raise QualificationResumeSafetyError(
                    "Qualification checkpoint rebind requires an observed terminal run."
                )
            previous_run = _run_identity(
                _workflow_run(github, observed_run_id, checkpoint_identity),
                checkpoint_identity,
            )
            if previous_run["run_attempt"] != observed_run_attempt:
                raise QualificationResumeSafetyError(
                    "Qualification checkpoint rebind run attempt differs from the observed terminal run."
                )
            previous_conclusion = previous_run["conclusion"]
            if previous_run["status"] != TERMINAL_RUN_STATUS or previous_conclusion not in {"failure", "success"}:
                raise QualificationResumeSafetyError(
                    "Qualification checkpoint rebind requires an exact terminal failed or successful run."
                )
            refreshing_success = previous_conclusion == "success"
            mutation = {
                "operation": (
                    "completed_checkpoint_refresh_dispatch" if refreshing_success else "checkpoint_rebind_dispatch"
                ),
                "endpoint": _workflow_dispatch_endpoint(),
                "ref": MAIN_BRANCH,
                "candidate_tag": identity.release_tag,
                "manifest_sha256": identity.manifest_sha256,
                "retry_of_run_id": observed_run_id,
                "replaced_checkpoint_sha256": checkpoint_digest,
            }
            if retry_run_id is None or retry_checkpoint_sha256 is None:
                return _result(
                    "completed_checkpoint_refresh_required" if refreshing_success else "checkpoint_rebind_required",
                    EXIT_OPERATOR_REQUIRED,
                    status_payload,
                    identity=identity,
                    checkpoint_path=resolved_checkpoint_path,
                    run=previous_run,
                    mutation=mutation,
                    next_action=(
                        f"Rerun resume with --retry-run-id {observed_run_id}, "
                        f"--retry-checkpoint-sha256 {checkpoint_digest}, and the exact expected "
                        "main and manifest digests."
                    ),
                )
            if retry_run_id != observed_run_id:
                raise QualificationResumeSafetyError(
                    "Checkpoint rebind retry run ID does not match the observed terminal run."
                )
            if retry_checkpoint_sha256 != checkpoint_digest:
                raise QualificationResumeSafetyError(
                    "Checkpoint rebind digest does not match the serialized qualification checkpoint."
                )
            if not _validate_expected_mutation(
                identity,
                expected_main_sha=expected_main_sha,
                expected_manifest_sha256=expected_manifest_sha256,
            ):
                raise QualificationResumeSafetyError(
                    "Checkpoint rebind requires exact expected main and manifest authorization."
                )
            if refreshing_success and matches:
                raise QualificationResumeSafetyError(
                    "An exact refreshed qualification run already exists during completed checkpoint refresh."
                )
            if active:
                raise QualificationResumeSafetyError(
                    "An exact refreshed qualification run is already active during checkpoint rebind."
                )
            return _dispatch(
                github,
                identity,
                resolved_checkpoint_path,
                high_water_run_id=high_water_run_id,
                retry_of_run_id=observed_run_id,
                replace_prepared_checkpoint_sha256=None,
                replace_observed_checkpoint_sha256=checkpoint_digest,
                replace_observed_run_conclusion=cast(str, previous_conclusion),
                status_payload=status_payload,
                poll_attempts=poll_attempts,
                poll_seconds=poll_seconds,
                sleep=sleep,
            )
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
                        replace_observed_checkpoint_sha256=None,
                        replace_observed_run_conclusion=None,
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
            replace_observed_checkpoint_sha256=None,
            replace_observed_run_conclusion=None,
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
        run_payload = _run_identity(selected_run, identity)
        if observe_only:
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
        try:
            bundle = download_reconciliation_bundle(
                client=github,
                repo_root=repo_root,
                identity=identity,
                run=selected_run,
                artifact=artifact,
                manifest=cast(Mapping[str, Any], binding.manifest),
            )
        except QualificationArtifactSafetyError as error:
            raise QualificationResumeSafetyError(str(error)) from error
        except QualificationArtifactError as error:
            raise QualificationResumeError(str(error)) from error
        plan = bundle.plan
        _revalidate_remote_identity(github, identity)
        requires_changes = plan.get("requires_changes") is True
        if apply_plan_sha256 is not None:
            if plan.get("plan_sha256") != apply_plan_sha256:
                raise QualificationResumeSafetyError(
                    "Authorized reconciliation plan digest does not match the current exact artifact plan."
                )
            if not requires_changes:
                return _result(
                    "reconciliation_current",
                    EXIT_SUCCESS,
                    status_payload,
                    identity=identity,
                    checkpoint_path=resolved_checkpoint_path,
                    apply_checkpoint_path=resolved_apply_checkpoint_path,
                    run=run_payload,
                    artifact=artifact,
                    reconciliation_plan=plan,
                    next_action="All planned qualification evidence is already present and identical.",
                )
            try:
                with _checkpoint_lock(resolved_checkpoint_path):
                    outcome = start_reconciliation_apply(
                        repo_root=repo_root,
                        identity=identity,
                        bundle=bundle,
                        expected_plan_sha256=apply_plan_sha256,
                        checkpoint_path=resolved_apply_checkpoint_path,
                        revalidate_remote=lambda evidence_sha: _revalidate_remote_identity(
                            github,
                            identity,
                            expected_evidence_sha=evidence_sha,
                        ),
                        ensure_no_active_runs=lambda: _require_no_active_exact_runs(github, identity),
                        require_actor=lambda: _require_mutation_actor(github),
                        remote_evidence_sha=lambda: _ref_sha(github, identity.evidence_ref),
                    )
            except QualificationApplySafetyError as error:
                raise QualificationResumeSafetyError(str(error)) from error
            except QualificationApplyError as error:
                raise QualificationResumeError(str(error)) from error
            return _result(
                outcome.state,
                EXIT_SUCCESS,
                status_payload,
                identity=identity,
                checkpoint_path=resolved_checkpoint_path,
                apply_checkpoint_path=resolved_apply_checkpoint_path,
                run=run_payload,
                artifact=artifact,
                reconciliation_plan=outcome.plan,
                reconciliation_result={
                    "commit_sha": outcome.commit_sha,
                },
                next_action="Qualification evidence was committed and pushed to the canonical evidence branch.",
            )
        return _result(
            "reconciliation_planned" if requires_changes else "reconciliation_current",
            EXIT_OPERATOR_REQUIRED if requires_changes else EXIT_SUCCESS,
            status_payload,
            identity=identity,
            checkpoint_path=resolved_checkpoint_path,
            apply_checkpoint_path=resolved_apply_checkpoint_path,
            run=run_payload,
            artifact=artifact,
            reconciliation_plan=plan,
            next_action=(
                "Review the exact reconciliation plan, then rerun resume with --apply-plan-sha256 set to its digest."
                if requires_changes
                else "All planned qualification evidence is already present and identical."
            ),
        )

    if apply_plan_sha256 is not None:
        raise QualificationResumeSafetyError(
            "Reconciliation apply requires the exact successful retained qualification artifact "
            "or an existing apply checkpoint."
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
        replace_observed_checkpoint_sha256=None,
        replace_observed_run_conclusion=None,
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
