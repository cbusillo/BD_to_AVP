from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import tempfile

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, cast

from scripts.release_qualification_artifact import ReconciliationBundle


REPOSITORY = "cbusillo/BD_to_AVP"
REPOSITORY_OWNER = "cbusillo"
HTTPS_REPOSITORY_URL = "https://github.com/cbusillo/BD_to_AVP.git"
APPLY_CHECKPOINT_TYPE = "bd_to_avp.release_qualification_apply_checkpoint"
APPLY_SCHEMA_VERSION = 1
COMMENT_PAGE_SIZE = 100
MAX_COMMENT_PAGES = 20
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
APPLY_STATES = {"prepared", "files_written", "committed", "pushed", "commented"}


class QualificationApplyError(RuntimeError):
    pass


class QualificationApplySafetyError(QualificationApplyError):
    pass


class QualificationIdentity(Protocol):
    release_tag: str
    evidence_ref: str
    evidence_sha: str
    evidence_pr_number: int

    def payload(self) -> dict[str, object]:
        raise NotImplementedError


class GitHubApplyAPI(Protocol):
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


RevalidateRemote = Callable[[str], None]
EnsureNoActiveRuns = Callable[[], None]
RequireActor = Callable[[], tuple[str, int]]
RemoteEvidenceSHA = Callable[[], str]


@dataclass(frozen=True)
class ApplyOutcome:
    state: str
    plan: dict[str, object]
    commit_sha: str
    comment_id: int


def _mapping(value: object, description: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise QualificationApplyError(f"{description} must be a JSON object.")
    return cast(Mapping[str, Any], value)


def _sequence(value: object, description: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise QualificationApplyError(f"{description} must be a JSON array.")
    return value


def _string(value: object, description: str) -> str:
    if not isinstance(value, str) or not value:
        raise QualificationApplyError(f"{description} must be a non-empty string.")
    return value


def _integer(value: object, description: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise QualificationApplyError(f"{description} must be a positive integer.")
    return value


def _sha(value: object, description: str) -> str:
    text = _string(value, description)
    if SHA_PATTERN.fullmatch(text) is None:
        raise QualificationApplyError(f"{description} must be a full lowercase Git SHA.")
    return text


def _sha256(value: object, description: str) -> str:
    text = _string(value, description)
    if SHA256_PATTERN.fullmatch(text) is None:
        raise QualificationApplyError(f"{description} must be a lowercase SHA-256 digest.")
    return text


def _canonical_json_bytes(payload: Mapping[str, object]) -> bytes:
    return (json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n").encode()


def _exact_keys(value: Mapping[str, Any], expected: set[str], description: str) -> None:
    actual = set(value)
    if actual != expected:
        raise QualificationApplySafetyError(
            f"{description} keys changed; missing={sorted(expected - actual)!r}, extra={sorted(actual - expected)!r}."
        )


def reconciliation_checkpoint_path(dispatch_checkpoint_path: Path) -> Path:
    return dispatch_checkpoint_path.with_name(dispatch_checkpoint_path.stem + ".apply.json")


def _run_git(
    repo_root: Path,
    arguments: Sequence[str],
    *,
    text: bool = True,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *arguments],
        cwd=repo_root,
        capture_output=True,
        text=text,
        env=env,
        check=False,
    )


def _git_text(repo_root: Path, arguments: Sequence[str], description: str) -> str:
    result = cast(subprocess.CompletedProcess[str], _run_git(repo_root, arguments))
    if result.returncode != 0:
        raise QualificationApplyError(f"Unable to read {description} from git.")
    return result.stdout.strip()


def _git_bytes(repo_root: Path, arguments: Sequence[str], description: str) -> bytes:
    result = cast(subprocess.CompletedProcess[bytes], _run_git(repo_root, arguments, text=False))
    if result.returncode != 0:
        raise QualificationApplyError(f"Unable to read {description} from git.")
    return result.stdout


def _checked_content(repo_root: Path, revision: str, path: str) -> bytes | None:
    result = cast(
        subprocess.CompletedProcess[bytes],
        _run_git(repo_root, ["show", f"{revision}:{path}"], text=False),
    )
    if result.returncode == 0:
        return result.stdout
    missing = cast(
        subprocess.CompletedProcess[str],
        _run_git(repo_root, ["cat-file", "-e", f"{revision}:{path}"]),
    )
    if missing.returncode != 0:
        return None
    raise QualificationApplyError(f"Unable to read checked reconciliation path {path!r}.")


def _write_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=path.parent, delete=False) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        temporary_path.replace(path)
    except OSError as error:
        raise QualificationApplyError(f"Unable to write reconciliation path {path.name!r}.") from error
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def _write_checkpoint(path: Path, checkpoint: Mapping[str, object]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=path.parent, delete=False) as temporary:
            temporary_path = Path(temporary.name)
            os.chmod(temporary_path, 0o600)
            temporary.write(_canonical_json_bytes(checkpoint))
            temporary.flush()
            os.fsync(temporary.fileno())
        temporary_path.replace(path)
        os.chmod(path, 0o600)
    except OSError as error:
        raise QualificationApplyError("Unable to write qualification apply checkpoint.") from error
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def _checkpoint_payload(
    identity: QualificationIdentity,
    bundle: ReconciliationBundle,
    *,
    state: str,
    commit_sha: str | None = None,
    comment_id: int | None = None,
) -> dict[str, object]:
    if state not in APPLY_STATES:
        raise QualificationApplyError("Qualification apply checkpoint state is unsupported.")
    plan = dict(bundle.plan)
    files = [
        {
            "path": file.path,
            "sha256": hashlib.sha256(file.content).hexdigest(),
            "size_bytes": len(file.content),
            "content_base64": base64.b64encode(file.content).decode("ascii"),
        }
        for file in bundle.files
    ]
    payload: dict[str, object] = {
        "schema_version": APPLY_SCHEMA_VERSION,
        "checkpoint_type": APPLY_CHECKPOINT_TYPE,
        "identity": identity.payload(),
        "plan": plan,
        "plan_sha256": _sha256(plan.get("plan_sha256"), "reconciliation plan digest"),
        "files": files,
        "progress": {
            "state": state,
            "commit_sha": commit_sha,
            "comment_id": comment_id,
        },
    }
    payload["checkpoint_sha256"] = hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()
    return payload


def _replace_progress(
    path: Path,
    checkpoint: Mapping[str, Any],
    *,
    state: str,
    commit_sha: str | None,
    comment_id: int | None,
) -> dict[str, object]:
    payload = dict(checkpoint)
    payload["progress"] = {
        "state": state,
        "commit_sha": commit_sha,
        "comment_id": comment_id,
    }
    payload.pop("checkpoint_sha256", None)
    payload["checkpoint_sha256"] = hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()
    _write_checkpoint(path, payload)
    return payload


def load_reconciliation_checkpoint(path: Path) -> Mapping[str, Any] | None:
    if not path.exists():
        return None
    if path.stat().st_mode & 0o077:
        raise QualificationApplySafetyError("Qualification apply checkpoint permissions must be 0600.")
    try:
        checkpoint = _mapping(json.loads(path.read_text(encoding="utf-8")), "qualification apply checkpoint")
    except OSError as error:
        raise QualificationApplyError("Unable to read qualification apply checkpoint.") from error
    except json.JSONDecodeError as error:
        raise QualificationApplySafetyError("Qualification apply checkpoint is invalid JSON.") from error
    _exact_keys(
        checkpoint,
        {
            "schema_version",
            "checkpoint_type",
            "checkpoint_sha256",
            "identity",
            "plan",
            "plan_sha256",
            "files",
            "progress",
        },
        "qualification apply checkpoint",
    )
    if (
        checkpoint.get("schema_version") != APPLY_SCHEMA_VERSION
        or checkpoint.get("checkpoint_type") != APPLY_CHECKPOINT_TYPE
    ):
        raise QualificationApplySafetyError("Qualification apply checkpoint schema or type is unsupported.")
    recorded_digest = _sha256(checkpoint.get("checkpoint_sha256"), "qualification apply checkpoint self digest")
    digest_payload = dict(checkpoint)
    digest_payload.pop("checkpoint_sha256")
    if hashlib.sha256(_canonical_json_bytes(digest_payload)).hexdigest() != recorded_digest:
        raise QualificationApplySafetyError("Qualification apply checkpoint self digest is invalid.")
    plan = _mapping(checkpoint.get("plan"), "qualification apply plan")
    plan_digest = _sha256(checkpoint.get("plan_sha256"), "qualification apply plan digest")
    if plan.get("plan_sha256") != plan_digest:
        raise QualificationApplySafetyError("Qualification apply checkpoint plan digest conflicts.")
    plan_digest_payload = dict(plan)
    plan_digest_payload.pop("plan_sha256", None)
    if hashlib.sha256(_canonical_json_bytes(plan_digest_payload)).hexdigest() != plan_digest:
        raise QualificationApplySafetyError("Qualification apply checkpoint plan self digest is invalid.")
    _validated_checkpoint_files(checkpoint)
    progress = _mapping(checkpoint.get("progress"), "qualification apply progress")
    _exact_keys(progress, {"state", "commit_sha", "comment_id"}, "qualification apply progress")
    state = _string(progress.get("state"), "qualification apply state")
    if state not in APPLY_STATES:
        raise QualificationApplySafetyError("Qualification apply checkpoint state is unsupported.")
    commit_sha = progress.get("commit_sha")
    comment_id = progress.get("comment_id")
    if state in {"committed", "pushed", "commented"}:
        _sha(commit_sha, "qualification apply commit SHA")
    elif commit_sha is not None:
        raise QualificationApplySafetyError("Qualification apply checkpoint records a premature commit SHA.")
    if state == "commented":
        _integer(comment_id, "qualification apply comment ID")
    elif comment_id is not None:
        raise QualificationApplySafetyError("Qualification apply checkpoint records a premature comment ID.")
    return checkpoint


def _validated_checkpoint_files(checkpoint: Mapping[str, Any]) -> dict[str, bytes]:
    plan = _mapping(checkpoint.get("plan"), "qualification apply plan")
    planned_files = {
        _string(item.get("path"), "planned reconciliation file path"): item
        for item in (
            _mapping(value, "planned reconciliation file")
            for value in _sequence(plan.get("files"), "planned reconciliation files")
        )
    }
    files: dict[str, bytes] = {}
    for raw_file in _sequence(checkpoint.get("files"), "qualification apply files"):
        file = _mapping(raw_file, "qualification apply file")
        _exact_keys(file, {"path", "sha256", "size_bytes", "content_base64"}, "qualification apply file")
        path = _validated_path(file.get("path"))
        if path in files:
            raise QualificationApplySafetyError("Qualification apply checkpoint repeats a file path.")
        try:
            content = base64.b64decode(
                _string(file.get("content_base64"), "qualification apply file content"), validate=True
            )
        except ValueError as error:
            raise QualificationApplySafetyError("Qualification apply checkpoint contains invalid base64.") from error
        digest = _sha256(file.get("sha256"), "qualification apply file digest")
        size = _integer(file.get("size_bytes"), "qualification apply file size")
        if len(content) != size or hashlib.sha256(content).hexdigest() != digest:
            raise QualificationApplySafetyError(
                "Qualification apply checkpoint file content conflicts with its digest."
            )
        planned = planned_files.get(path)
        if planned is None or planned.get("sha256") != digest or planned.get("size_bytes") != size:
            raise QualificationApplySafetyError(
                "Qualification apply checkpoint file conflicts with the authorized plan."
            )
        files[path] = content
    if set(files) != set(planned_files):
        raise QualificationApplySafetyError(
            "Qualification apply checkpoint file set conflicts with the authorized plan."
        )
    return files


def _validated_path(value: object) -> str:
    text = _string(value, "qualification apply file path")
    path = PurePosixPath(text)
    if (
        path.is_absolute()
        or ".." in path.parts
        or path.as_posix() != text
        or not text.startswith("docs/qualification/")
    ):
        raise QualificationApplySafetyError("Qualification apply file path is outside the qualification evidence tree.")
    return text


def _validate_identity(checkpoint: Mapping[str, Any], identity: QualificationIdentity) -> tuple[str, str | None]:
    recorded = dict(_mapping(checkpoint.get("identity"), "qualification apply identity"))
    current = identity.payload()
    base_evidence_sha = _sha(recorded.get("evidence_sha"), "qualification apply base evidence SHA")
    progress = _mapping(checkpoint.get("progress"), "qualification apply progress")
    commit_sha = progress.get("commit_sha")
    for key, value in recorded.items():
        if key != "evidence_sha" and current.get(key) != value:
            raise QualificationApplySafetyError(
                "Qualification apply checkpoint conflicts with current release identity."
            )
    current_evidence_sha = _sha(current.get("evidence_sha"), "current evidence SHA")
    allowed = {base_evidence_sha}
    if commit_sha is not None:
        allowed.add(_sha(commit_sha, "qualification apply commit SHA"))
    if current_evidence_sha not in allowed:
        raise QualificationApplySafetyError("Qualification apply evidence branch moved to an unrelated commit.")
    return base_evidence_sha, cast(str | None, commit_sha)


def _changed_paths(repo_root: Path) -> set[str]:
    paths: set[str] = set()
    for arguments in (
        ["diff", "--name-only", "-z"],
        ["diff", "--cached", "--name-only", "-z"],
        ["ls-files", "--others", "--exclude-standard", "-z"],
    ):
        output = _git_bytes(repo_root, arguments, "qualification apply worktree changes")
        paths.update(item.decode("utf-8", errors="surrogateescape") for item in output.split(b"\0") if item)
    return paths


def _validate_origin(repo_root: Path) -> None:
    origin = _git_text(repo_root, ["remote", "get-url", "origin"], "origin URL")
    accepted = {
        "https://github.com/cbusillo/BD_to_AVP",
        "https://github.com/cbusillo/BD_to_AVP.git",
        "git@github.com:cbusillo/BD_to_AVP.git",
        "ssh://git@github.com/cbusillo/BD_to_AVP.git",
    }
    if origin not in accepted:
        raise QualificationApplySafetyError("Qualification apply origin does not match cbusillo/BD_to_AVP.")


def _validate_local_branch(repo_root: Path, identity: QualificationIdentity, allowed_heads: set[str]) -> str:
    branch = _git_text(repo_root, ["symbolic-ref", "--quiet", "--short", "HEAD"], "local branch")
    if branch != identity.evidence_ref:
        raise QualificationApplySafetyError("Qualification apply must run from the canonical evidence branch.")
    head = _sha(_git_text(repo_root, ["rev-parse", "HEAD"], "local HEAD"), "local HEAD")
    if head not in allowed_heads:
        raise QualificationApplySafetyError("Qualification apply local HEAD is not an authorized transition commit.")
    _validate_origin(repo_root)
    return head


def _validate_and_write_files(repo_root: Path, base_sha: str, files: Mapping[str, bytes]) -> None:
    unexpected = sorted(_changed_paths(repo_root) - set(files))
    if unexpected:
        raise QualificationApplySafetyError(f"Qualification apply worktree contains unrelated changes: {unexpected!r}.")
    for path, desired in files.items():
        base = _checked_content(repo_root, base_sha, path)
        local_path = repo_root / path
        current = local_path.read_bytes() if local_path.exists() else None
        if current not in {base, desired}:
            raise QualificationApplySafetyError(
                f"Qualification apply path {path!r} contains conflicting local content."
            )
    for path, desired in files.items():
        _write_atomic(repo_root / path, desired)
    for path, desired in files.items():
        if (repo_root / path).read_bytes() != desired:
            raise QualificationApplySafetyError(f"Qualification apply path {path!r} changed after writing.")
    expected_changed = {
        path for path, desired in files.items() if _checked_content(repo_root, base_sha, path) != desired
    }
    if _changed_paths(repo_root) != expected_changed:
        raise QualificationApplySafetyError("Qualification apply worktree diff does not match the authorized plan.")


def _commit_message(plan: Mapping[str, Any]) -> str:
    artifact = _mapping(plan.get("artifact"), "reconciliation plan artifact")
    return (
        f"Record qualification evidence for {_string(plan.get('release_tag'), 'release tag')}\n\n"
        f"Qualification-Plan-SHA256: {_sha256(plan.get('plan_sha256'), 'plan digest')}\n"
        f"Qualification-Run-ID: {_integer(artifact.get('run_id'), 'qualification run ID')}\n"
    )


def _commit_message_bytes(repo_root: Path, commit_sha: str) -> bytes:
    commit_object = _git_bytes(
        repo_root,
        ["cat-file", "commit", commit_sha],
        "qualification apply commit object",
    )
    _headers, separator, message = commit_object.partition(b"\n\n")
    if not separator:
        raise QualificationApplySafetyError("Qualification apply commit object has no message payload.")
    return message


def _validate_commit(
    repo_root: Path,
    *,
    commit_sha: str,
    base_sha: str,
    plan: Mapping[str, Any],
    files: Mapping[str, bytes],
    actor_login: str,
    actor_id: int,
) -> None:
    if _git_text(repo_root, ["rev-parse", f"{commit_sha}^"], "qualification apply commit parent") != base_sha:
        raise QualificationApplySafetyError(
            "Qualification apply commit parent conflicts with the authorized evidence head."
        )
    changed = set(
        item.decode("utf-8", errors="surrogateescape")
        for item in _git_bytes(
            repo_root,
            ["diff-tree", "--no-commit-id", "--name-only", "-r", "-z", commit_sha],
            "qualification apply commit paths",
        ).split(b"\0")
        if item
    )
    expected_changed = {
        path for path, desired in files.items() if _checked_content(repo_root, base_sha, path) != desired
    }
    if changed != expected_changed:
        raise QualificationApplySafetyError("Qualification apply commit paths conflict with the authorized plan.")
    for path, desired in files.items():
        if _checked_content(repo_root, commit_sha, path) != desired:
            raise QualificationApplySafetyError(f"Qualification apply commit content conflicts for {path!r}.")
    if _commit_message_bytes(repo_root, commit_sha) != _commit_message(plan).encode():
        raise QualificationApplySafetyError("Qualification apply commit message conflicts with the authorized plan.")
    expected_email = f"{actor_id}+{actor_login}@users.noreply.github.com"
    if _git_text(repo_root, ["show", "-s", "--format=%an", commit_sha], "qualification apply author") != actor_login:
        raise QualificationApplySafetyError(
            "Qualification apply commit author conflicts with the active GitHub identity."
        )
    if (
        _git_text(repo_root, ["show", "-s", "--format=%ae", commit_sha], "qualification apply author email")
        != expected_email
    ):
        raise QualificationApplySafetyError(
            "Qualification apply commit email conflicts with the active GitHub identity."
        )
    if _git_text(repo_root, ["show", "-s", "--format=%cn", commit_sha], "qualification apply committer") != actor_login:
        raise QualificationApplySafetyError(
            "Qualification apply commit committer conflicts with the active GitHub identity."
        )
    if (
        _git_text(repo_root, ["show", "-s", "--format=%ce", commit_sha], "qualification apply committer email")
        != expected_email
    ):
        raise QualificationApplySafetyError(
            "Qualification apply commit committer email conflicts with the active GitHub identity."
        )


def _create_or_adopt_commit(
    repo_root: Path,
    *,
    base_sha: str,
    plan: Mapping[str, Any],
    files: Mapping[str, bytes],
    actor_login: str,
    actor_id: int,
) -> str:
    head = _sha(_git_text(repo_root, ["rev-parse", "HEAD"], "local HEAD"), "local HEAD")
    if head != base_sha:
        _validate_commit(
            repo_root,
            commit_sha=head,
            base_sha=base_sha,
            plan=plan,
            files=files,
            actor_login=actor_login,
            actor_id=actor_id,
        )
        return head
    paths = sorted(files)
    add = cast(subprocess.CompletedProcess[str], _run_git(repo_root, ["add", "--", *paths]))
    if add.returncode != 0:
        raise QualificationApplyError("Unable to stage qualification reconciliation files.")
    expected_email = f"{actor_id}+{actor_login}@users.noreply.github.com"
    commit = cast(
        subprocess.CompletedProcess[str],
        _run_git(
            repo_root,
            [
                "-c",
                f"user.name={actor_login}",
                "-c",
                f"user.email={expected_email}",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-m",
                _commit_message(plan).rstrip("\n"),
                "--",
                *paths,
            ],
        ),
    )
    if commit.returncode != 0:
        raise QualificationApplyError("Unable to commit qualification reconciliation files.")
    commit_sha = _sha(_git_text(repo_root, ["rev-parse", "HEAD"], "qualification apply commit"), "commit SHA")
    _validate_commit(
        repo_root,
        commit_sha=commit_sha,
        base_sha=base_sha,
        plan=plan,
        files=files,
        actor_login=actor_login,
        actor_id=actor_id,
    )
    if _changed_paths(repo_root):
        raise QualificationApplySafetyError("Qualification apply worktree changed during commit creation.")
    return commit_sha


def _authenticated_git_environment() -> dict[str, str]:
    environment = dict(os.environ)
    for name in (
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "CODEX_GITHUB_TOKEN",
        "GH_ENTERPRISE_TOKEN",
        "GITHUB_ENTERPRISE_TOKEN",
        "GH_HOST",
        "GH_REPO",
    ):
        environment.pop(name, None)
    environment["GIT_TERMINAL_PROMPT"] = "0"
    return environment


def _push_commit(repo_root: Path, evidence_ref: str, commit_sha: str) -> None:
    push = cast(
        subprocess.CompletedProcess[str],
        _run_git(
            repo_root,
            [
                "-c",
                "credential.helper=",
                "-c",
                "credential.helper=!gh auth git-credential",
                "push",
                HTTPS_REPOSITORY_URL,
                f"{commit_sha}:refs/heads/{evidence_ref}",
            ],
            env=_authenticated_git_environment(),
        ),
    )
    if push.returncode != 0:
        raise QualificationApplyError("Unable to push the qualification reconciliation commit.")


def _comment_marker(plan_sha256: str) -> str:
    return f"<!-- bd-to-avp-qualification-reconciliation:{plan_sha256} -->"


def _comment_body(plan: Mapping[str, Any], commit_sha: str) -> str:
    artifact = _mapping(plan.get("artifact"), "reconciliation plan artifact")
    plan_sha256 = _sha256(plan.get("plan_sha256"), "reconciliation plan digest")
    return (
        f"{_comment_marker(plan_sha256)}\n"
        f"Qualification evidence for `{_string(plan.get('release_tag'), 'release tag')}` was reconciled from the exact "
        "Milestone Qualification artifact.\n\n"
        f"- Plan SHA-256: `{plan_sha256}`\n"
        f"- Evidence commit: `{commit_sha}`\n"
        f"- Milestone Qualification run: `{_integer(artifact.get('run_id'), 'qualification run ID')}` "
        f"attempt `{_integer(artifact.get('run_attempt'), 'qualification run attempt')}`\n"
    )


def _matching_comment(
    client: GitHubApplyAPI,
    *,
    pr_number: int,
    plan: Mapping[str, Any],
    commit_sha: str,
    actor_login: str,
    actor_id: int,
) -> Mapping[str, Any] | None:
    expected_body = _comment_body(plan, commit_sha)
    marker = _comment_marker(_sha256(plan.get("plan_sha256"), "reconciliation plan digest"))
    matches: list[Mapping[str, Any]] = []
    for page in range(1, MAX_COMMENT_PAGES + 1):
        payload = _sequence(
            client.get_json(
                f"repos/{REPOSITORY}/issues/{pr_number}/comments?per_page={COMMENT_PAGE_SIZE}&page={page}",
                active_auth=True,
            ),
            "evidence pull request comments",
        )
        comments = [_mapping(item, "evidence pull request comment") for item in payload]
        matches.extend(comment for comment in comments if marker in str(comment.get("body", "")))
        if len(comments) < COMMENT_PAGE_SIZE:
            break
        if page == MAX_COMMENT_PAGES:
            raise QualificationApplySafetyError("Evidence pull request comment history exceeds the adoption limit.")
    if len(matches) > 1:
        raise QualificationApplySafetyError("Multiple qualification reconciliation comments match one plan.")
    if not matches:
        return None
    comment = matches[0]
    _validate_comment(comment, expected_body=expected_body, actor_login=actor_login, actor_id=actor_id)
    return comment


def _validate_comment(
    comment: Mapping[str, Any],
    *,
    expected_body: str,
    actor_login: str,
    actor_id: int,
) -> int:
    user = _mapping(comment.get("user"), "qualification reconciliation comment user")
    if comment.get("body") != expected_body or user.get("login") != actor_login or user.get("id") != actor_id:
        raise QualificationApplySafetyError("Qualification reconciliation comment conflicts with the plan.")
    return _integer(comment.get("id"), "qualification reconciliation comment ID")


def _post_or_adopt_comment(
    client: GitHubApplyAPI,
    *,
    pr_number: int,
    plan: Mapping[str, Any],
    commit_sha: str,
    actor_login: str,
    actor_id: int,
) -> int:
    existing = _matching_comment(
        client,
        pr_number=pr_number,
        plan=plan,
        commit_sha=commit_sha,
        actor_login=actor_login,
        actor_id=actor_id,
    )
    if existing is not None:
        return _integer(existing.get("id"), "qualification reconciliation comment ID")
    body = _comment_body(plan, commit_sha)
    comment = _mapping(
        client.post_json(
            f"repos/{REPOSITORY}/issues/{pr_number}/comments",
            {"body": body},
            active_auth=True,
        ),
        "qualification reconciliation comment",
    )
    return _validate_comment(comment, expected_body=body, actor_login=actor_login, actor_id=actor_id)


def _comment_by_id(
    client: GitHubApplyAPI,
    *,
    comment_id: int,
    plan: Mapping[str, Any],
    commit_sha: str,
    actor_login: str,
    actor_id: int,
) -> Mapping[str, Any]:
    comment = _mapping(
        client.get_json(
            f"repos/{REPOSITORY}/issues/comments/{comment_id}",
            active_auth=True,
        ),
        "qualification reconciliation comment",
    )
    _validate_comment(
        comment,
        expected_body=_comment_body(plan, commit_sha),
        actor_login=actor_login,
        actor_id=actor_id,
    )
    return comment


def reconciliation_checkpoint_summary(path: Path) -> dict[str, object] | None:
    checkpoint = load_reconciliation_checkpoint(path)
    if checkpoint is None:
        return None
    progress = _mapping(checkpoint.get("progress"), "qualification apply progress")
    return {
        "present": True,
        "plan_sha256": checkpoint.get("plan_sha256"),
        "state": progress.get("state"),
        "commit_sha": progress.get("commit_sha"),
        "comment_id": progress.get("comment_id"),
    }


def start_reconciliation_apply(
    *,
    repo_root: Path,
    identity: QualificationIdentity,
    bundle: ReconciliationBundle,
    expected_plan_sha256: str,
    client: GitHubApplyAPI,
    checkpoint_path: Path,
    revalidate_remote: RevalidateRemote,
    ensure_no_active_runs: EnsureNoActiveRuns,
    require_actor: RequireActor,
    remote_evidence_sha: RemoteEvidenceSHA,
) -> ApplyOutcome:
    expected_plan_sha256 = _sha256(expected_plan_sha256, "authorized reconciliation plan digest")
    if bundle.plan.get("plan_sha256") != expected_plan_sha256:
        raise QualificationApplySafetyError("Authorized reconciliation plan digest does not match the current plan.")
    if bundle.plan.get("requires_changes") is not True:
        raise QualificationApplySafetyError("Qualification reconciliation plan does not require mutation.")
    existing = load_reconciliation_checkpoint(checkpoint_path)
    if existing is None:
        ensure_no_active_runs()
        revalidate_remote(identity.evidence_sha)
        _validate_local_branch(repo_root, identity, {identity.evidence_sha})
        if _changed_paths(repo_root):
            raise QualificationApplySafetyError("Qualification apply requires a clean evidence worktree.")
        checkpoint = _checkpoint_payload(identity, bundle, state="prepared")
        _write_checkpoint(checkpoint_path, checkpoint)
    return continue_reconciliation_apply(
        repo_root=repo_root,
        identity=identity,
        expected_plan_sha256=expected_plan_sha256,
        client=client,
        checkpoint_path=checkpoint_path,
        revalidate_remote=revalidate_remote,
        ensure_no_active_runs=ensure_no_active_runs,
        require_actor=require_actor,
        remote_evidence_sha=remote_evidence_sha,
    )


def continue_reconciliation_apply(
    *,
    repo_root: Path,
    identity: QualificationIdentity,
    expected_plan_sha256: str,
    client: GitHubApplyAPI,
    checkpoint_path: Path,
    revalidate_remote: RevalidateRemote,
    ensure_no_active_runs: EnsureNoActiveRuns,
    require_actor: RequireActor,
    remote_evidence_sha: RemoteEvidenceSHA,
) -> ApplyOutcome:
    checkpoint = load_reconciliation_checkpoint(checkpoint_path)
    if checkpoint is None:
        raise QualificationApplySafetyError("Qualification apply checkpoint is missing.")
    expected_plan_sha256 = _sha256(expected_plan_sha256, "authorized reconciliation plan digest")
    if checkpoint.get("plan_sha256") != expected_plan_sha256:
        raise QualificationApplySafetyError(
            "Authorized reconciliation plan digest conflicts with the apply checkpoint."
        )
    base_sha, checkpoint_commit_sha = _validate_identity(checkpoint, identity)
    plan = dict(_mapping(checkpoint.get("plan"), "qualification apply plan"))
    files = _validated_checkpoint_files(checkpoint)
    progress = _mapping(checkpoint.get("progress"), "qualification apply progress")
    state = _string(progress.get("state"), "qualification apply state")
    actor_login, actor_id = require_actor()
    allowed_heads = {base_sha}
    if checkpoint_commit_sha is not None:
        allowed_heads.add(checkpoint_commit_sha)
    if state == "files_written" and checkpoint_commit_sha is None:
        allowed_heads.add(_sha(_git_text(repo_root, ["rev-parse", "HEAD"], "local HEAD"), "local HEAD"))
    _validate_local_branch(repo_root, identity, allowed_heads)
    if state in {"committed", "pushed", "commented"} and _changed_paths(repo_root):
        raise QualificationApplySafetyError(
            "Qualification apply worktree changed after the reconciliation commit was created."
        )

    if state == "prepared":
        ensure_no_active_runs()
        revalidate_remote(base_sha)
        _validate_and_write_files(repo_root, base_sha, files)
        checkpoint = _replace_progress(
            checkpoint_path,
            checkpoint,
            state="files_written",
            commit_sha=None,
            comment_id=None,
        )
        state = "files_written"

    if state == "files_written":
        ensure_no_active_runs()
        revalidate_remote(base_sha)
        local_head = _sha(_git_text(repo_root, ["rev-parse", "HEAD"], "local HEAD"), "local HEAD")
        if local_head == base_sha:
            _validate_and_write_files(repo_root, base_sha, files)
        elif _changed_paths(repo_root):
            raise QualificationApplySafetyError(
                "Qualification apply worktree changed after the reconciliation commit was created."
            )
        commit_sha = _create_or_adopt_commit(
            repo_root,
            base_sha=base_sha,
            plan=plan,
            files=files,
            actor_login=actor_login,
            actor_id=actor_id,
        )
        checkpoint = _replace_progress(
            checkpoint_path,
            checkpoint,
            state="committed",
            commit_sha=commit_sha,
            comment_id=None,
        )
        state = "committed"
    else:
        commit_sha = _sha(
            _mapping(checkpoint.get("progress"), "qualification apply progress").get("commit_sha"),
            "qualification apply commit SHA",
        )

    if state == "committed":
        _validate_commit(
            repo_root,
            commit_sha=commit_sha,
            base_sha=base_sha,
            plan=plan,
            files=files,
            actor_login=actor_login,
            actor_id=actor_id,
        )
        ensure_no_active_runs()
        remote_sha = _sha(remote_evidence_sha(), "remote evidence SHA")
        if remote_sha == commit_sha:
            revalidate_remote(commit_sha)
        elif remote_sha == base_sha:
            revalidate_remote(base_sha)
            try:
                _push_commit(repo_root, identity.evidence_ref, commit_sha)
            except QualificationApplyError as error:
                observed_after_error = _sha(remote_evidence_sha(), "remote evidence SHA")
                if observed_after_error != commit_sha:
                    if observed_after_error != base_sha:
                        raise QualificationApplySafetyError(
                            "Evidence branch moved while the reconciliation commit was being pushed."
                        ) from error
                    raise
            if _sha(remote_evidence_sha(), "remote evidence SHA") != commit_sha:
                raise QualificationApplySafetyError("Evidence branch did not advance to the reconciliation commit.")
            revalidate_remote(commit_sha)
        else:
            raise QualificationApplySafetyError(
                "Evidence branch moved before the reconciliation commit could be pushed."
            )
        checkpoint = _replace_progress(
            checkpoint_path,
            checkpoint,
            state="pushed",
            commit_sha=commit_sha,
            comment_id=None,
        )
        state = "pushed"

    if state == "pushed":
        ensure_no_active_runs()
        if _sha(remote_evidence_sha(), "remote evidence SHA") != commit_sha:
            raise QualificationApplySafetyError("Evidence branch moved after the reconciliation commit was pushed.")
        revalidate_remote(commit_sha)
        comment_id = _post_or_adopt_comment(
            client,
            pr_number=identity.evidence_pr_number,
            plan=plan,
            commit_sha=commit_sha,
            actor_login=actor_login,
            actor_id=actor_id,
        )
        checkpoint = _replace_progress(
            checkpoint_path,
            checkpoint,
            state="commented",
            commit_sha=commit_sha,
            comment_id=comment_id,
        )
        state = "commented"
    else:
        comment_id = _integer(
            _mapping(checkpoint.get("progress"), "qualification apply progress").get("comment_id"),
            "qualification apply comment ID",
        )

    if state != "commented":
        raise QualificationApplySafetyError("Qualification apply stopped in an unsupported state.")
    if _sha(remote_evidence_sha(), "remote evidence SHA") != commit_sha:
        raise QualificationApplySafetyError("Evidence branch moved after qualification reconciliation completed.")
    revalidate_remote(commit_sha)
    adopted = _comment_by_id(
        client,
        comment_id=comment_id,
        plan=plan,
        commit_sha=commit_sha,
        actor_login=actor_login,
        actor_id=actor_id,
    )
    if _integer(adopted.get("id"), "qualification reconciliation comment ID") != comment_id:
        raise QualificationApplySafetyError("Qualification reconciliation comment is no longer durable.")
    try:
        checkpoint_path.unlink()
    except OSError as error:
        raise QualificationApplyError("Unable to remove completed qualification apply checkpoint.") from error
    return ApplyOutcome(
        state="reconciliation_applied",
        plan=plan,
        commit_sha=commit_sha,
        comment_id=comment_id,
    )


__all__ = [
    "ApplyOutcome",
    "QualificationApplyError",
    "QualificationApplySafetyError",
    "continue_reconciliation_apply",
    "load_reconciliation_checkpoint",
    "reconciliation_checkpoint_path",
    "reconciliation_checkpoint_summary",
    "start_reconciliation_apply",
]
