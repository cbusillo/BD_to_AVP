from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import time

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, cast
from urllib.parse import quote


EXIT_SUCCESS = 0
EXIT_FAILED = 1
EXIT_APPROVAL_REQUIRED = 20
EXIT_SAFETY_ERROR = 21
EXIT_TIMEOUT = 22
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
NONTERMINAL_STATUSES = {"in_progress", "pending", "queued", "requested"}
REPOSITORY = "cbusillo/BD_to_AVP"
WORKFLOW_POLICIES = {
    "Release from protected main": (".github/workflows/briefcase.yml", 101_708_423),
    "Publish Native UI Preview": (".github/workflows/native-ui-preview.yml", 311_846_830),
}
ALLOWED_WORKFLOWS = tuple(WORKFLOW_POLICIES)
REQUIRED_BRANCH = "main"
REQUIRED_EVENT = "workflow_dispatch"
REQUIRED_ENVIRONMENT = "macos-signing"
APPROVAL_ACTOR = "cbusillo"


class ReleaseRunError(RuntimeError):
    pass


class GitHubAPI(Protocol):
    def get_json(self, endpoint: str, *, active_auth: bool = False) -> object: ...

    def post_json(self, endpoint: str, payload: Mapping[str, object], *, active_auth: bool = False) -> object: ...


class GhAPIClient:
    def __init__(
        self,
        executable: str = "gh",
        hostname: str = "github.com",
        request_timeout_seconds: float = 30.0,
    ) -> None:
        if request_timeout_seconds <= 0:
            raise ValueError("GitHub request timeout must be greater than zero.")
        self.executable = executable
        self.hostname = hostname
        self.request_timeout_seconds = request_timeout_seconds

    def _environment(self, *, active_auth: bool) -> dict[str, str]:
        environment = os.environ.copy()
        if active_auth:
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
        elif "GH_TOKEN" not in environment:
            token = environment.get("CODEX_GITHUB_TOKEN") or environment.get("GITHUB_TOKEN")
            if token:
                environment["GH_TOKEN"] = token
        return environment

    def _run_json(
        self,
        arguments: Sequence[str],
        *,
        active_auth: bool,
        input_payload: Mapping[str, object] | None = None,
    ) -> object:
        try:
            result = subprocess.run(
                [self.executable, *arguments],
                input=json.dumps(input_payload) if input_payload is not None else None,
                text=True,
                capture_output=True,
                env=self._environment(active_auth=active_auth),
                timeout=self.request_timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise ReleaseRunError("GitHub CLI request timed out.") from error
        if result.returncode != 0:
            raise ReleaseRunError(f"GitHub CLI request failed with exit status {result.returncode}.")
        if not result.stdout.strip():
            return None
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise ReleaseRunError("GitHub CLI returned invalid JSON.") from error

    def get_json(self, endpoint: str, *, active_auth: bool = False) -> object:
        return self._run_json(
            [
                "api",
                "--hostname",
                self.hostname,
                "-H",
                "Accept: application/vnd.github+json",
                "-H",
                "X-GitHub-Api-Version: 2022-11-28",
                endpoint,
            ],
            active_auth=active_auth,
        )

    def post_json(self, endpoint: str, payload: Mapping[str, object], *, active_auth: bool = False) -> object:
        return self._run_json(
            [
                "api",
                "--hostname",
                self.hostname,
                "-H",
                "Accept: application/vnd.github+json",
                "-H",
                "X-GitHub-Api-Version: 2022-11-28",
                "--method",
                "POST",
                endpoint,
                "--input",
                "-",
            ],
            active_auth=active_auth,
            input_payload=payload,
        )


@dataclass(frozen=True)
class RunExpectation:
    run_id: int
    workflow: str
    head_sha: str
    repository: str = REPOSITORY
    branch: str = REQUIRED_BRANCH
    event: str = REQUIRED_EVENT
    environment: str = REQUIRED_ENVIRONMENT

    @property
    def run_endpoint(self) -> str:
        return f"repos/{self.repository}/actions/runs/{self.run_id}"

    @property
    def pending_deployments_endpoint(self) -> str:
        return f"{self.run_endpoint}/pending_deployments"

    @property
    def branch_endpoint(self) -> str:
        return f"repos/{self.repository}/git/ref/heads/{quote(self.branch, safe='')}"

    @property
    def workflow_path(self) -> str:
        return WORKFLOW_POLICIES[self.workflow][0]

    @property
    def workflow_id(self) -> int:
        return WORKFLOW_POLICIES[self.workflow][1]


Emitter = Callable[[dict[str, object]], None]


def _emit_json(payload: dict[str, object]) -> None:
    print(json.dumps(payload, sort_keys=True), flush=True)


def _mapping(value: object, description: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ReleaseRunError(f"{description} must be a JSON object.")
    return cast(Mapping[str, Any], value)


def _sequence(value: object, description: str) -> Sequence[object]:
    if not isinstance(value, list):
        raise ReleaseRunError(f"{description} must be a JSON array.")
    return value


def _string(record: Mapping[str, Any], key: str, description: str) -> str:
    value = record.get(key)
    if not isinstance(value, str):
        raise ReleaseRunError(f"{description} is missing string field {key!r}.")
    return value


def validate_expectation(expectation: RunExpectation) -> None:
    if REPOSITORY_PATTERN.fullmatch(expectation.repository) is None:
        raise ReleaseRunError("Repository must use OWNER/REPO format.")
    if expectation.repository != REPOSITORY:
        raise ReleaseRunError(f"Release guard is fixed to repository {REPOSITORY!r}.")
    if expectation.run_id <= 0:
        raise ReleaseRunError("Run ID must be a positive integer.")
    if SHA_PATTERN.fullmatch(expectation.head_sha) is None:
        raise ReleaseRunError("Expected head SHA must be a full lowercase 40-character Git SHA.")
    if expectation.workflow not in ALLOWED_WORKFLOWS:
        raise ReleaseRunError(f"Workflow must be one of {ALLOWED_WORKFLOWS!r}.")
    if expectation.branch != REQUIRED_BRANCH:
        raise ReleaseRunError(f"Release guard requires branch {REQUIRED_BRANCH!r}.")
    if expectation.event != REQUIRED_EVENT:
        raise ReleaseRunError(f"Release guard requires event {REQUIRED_EVENT!r}.")
    if expectation.environment != REQUIRED_ENVIRONMENT:
        raise ReleaseRunError(f"Release guard requires environment {REQUIRED_ENVIRONMENT!r}.")


def validate_run_identity(run: Mapping[str, Any], expectation: RunExpectation) -> tuple[str, str, str, str, int]:
    actual = {
        "workflow": _string(run, "name", "Workflow run"),
        "workflow_path": _string(run, "path", "Workflow run"),
        "head_sha": _string(run, "head_sha", "Workflow run"),
        "branch": _string(run, "head_branch", "Workflow run"),
        "event": _string(run, "event", "Workflow run"),
    }
    expected = {
        "workflow": expectation.workflow,
        "workflow_path": expectation.workflow_path,
        "head_sha": expectation.head_sha,
        "branch": expectation.branch,
        "event": expectation.event,
    }
    mismatches = [
        f"{name}={actual[name]!r} (expected {expected[name]!r})" for name in expected if actual[name] != expected[name]
    ]
    if mismatches:
        raise ReleaseRunError("Workflow run identity mismatch: " + ", ".join(mismatches))
    workflow_id = run.get("workflow_id")
    if workflow_id != expectation.workflow_id:
        raise ReleaseRunError(
            f"Workflow run ID {workflow_id!r} does not match expected workflow ID {expectation.workflow_id!r}."
        )
    status = _string(run, "status", "Workflow run")
    conclusion = run.get("conclusion")
    if conclusion is None:
        conclusion_text = ""
    elif isinstance(conclusion, str):
        conclusion_text = conclusion
    else:
        raise ReleaseRunError("Workflow run conclusion must be a string or null.")
    actor = _mapping(run.get("actor"), "Workflow run actor")
    actor_login = _string(actor, "login", "Workflow run actor")
    triggering_actor = _mapping(run.get("triggering_actor"), "Workflow run triggering actor")
    triggering_actor_login = _string(triggering_actor, "login", "Workflow run triggering actor")
    run_attempt = run.get("run_attempt")
    if not isinstance(run_attempt, int) or run_attempt <= 0:
        raise ReleaseRunError("Workflow run attempt must be a positive integer.")
    return status, conclusion_text, actor_login, triggering_actor_login, run_attempt


def build_approval_fingerprint(
    expectation: RunExpectation,
    *,
    environment_id: int,
    run_attempt: int,
    run_actor: str,
    triggering_actor: str,
    reviewer: str = APPROVAL_ACTOR,
) -> str:
    payload = json.dumps(
        {
            "repository": expectation.repository,
            "run_id": expectation.run_id,
            "workflow": expectation.workflow,
            "workflow_path": expectation.workflow_path,
            "workflow_id": expectation.workflow_id,
            "head_sha": expectation.head_sha,
            "branch": expectation.branch,
            "event": expectation.event,
            "environment": expectation.environment,
            "environment_id": environment_id,
            "run_attempt": run_attempt,
            "run_actor": run_actor,
            "triggering_actor": triggering_actor,
            "reviewer": reviewer,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def current_branch_sha(client: GitHubAPI, expectation: RunExpectation, *, active_auth: bool = False) -> str:
    reference = _mapping(client.get_json(expectation.branch_endpoint, active_auth=active_auth), "Git reference")
    git_object = _mapping(reference.get("object"), "Git reference object")
    return _string(git_object, "sha", "Git reference object")


def expected_pending_deployment(pending: object, expectation: RunExpectation) -> Mapping[str, Any]:
    deployments = _sequence(pending, "Pending deployments")
    if len(deployments) != 1:
        raise ReleaseRunError(f"Expected exactly one pending deployment; found {len(deployments)}.")
    matches: list[Mapping[str, Any]] = []
    found_environments: list[str] = []
    for item in deployments:
        deployment = _mapping(item, "Pending deployment")
        environment = _mapping(deployment.get("environment"), "Pending deployment environment")
        name = _string(environment, "name", "Pending deployment environment")
        found_environments.append(name)
        if name == expectation.environment:
            matches.append(deployment)
    if len(matches) != 1:
        raise ReleaseRunError(
            f"Expected exactly one pending deployment for {expectation.environment!r}; found {len(matches)} "
            f"among {found_environments!r}."
        )
    return matches[0]


def pending_reviewer_logins(deployment: Mapping[str, Any]) -> list[str]:
    reviewers_value = deployment.get("reviewers", [])
    reviewers = _sequence(reviewers_value, "Pending deployment reviewers")
    if len(reviewers) != 1:
        raise ReleaseRunError(f"Expected exactly one pending deployment reviewer; found {len(reviewers)}.")
    reviewer = _mapping(reviewers[0], "Pending deployment reviewer")
    if reviewer.get("type") != "User":
        raise ReleaseRunError("Pending deployment reviewer must be a User.")
    user = _mapping(reviewer.get("reviewer"), "Pending deployment reviewer user")
    login = _string(user, "login", "Pending deployment reviewer user")
    if login != APPROVAL_ACTOR:
        raise ReleaseRunError(f"Pending deployment reviewer must be {APPROVAL_ACTOR!r}, not {login!r}.")
    return [login]


def watch_release_run(
    expectation: RunExpectation,
    client: GitHubAPI,
    *,
    poll_seconds: float = 10.0,
    timeout_seconds: float = 14_400.0,
    sleeper: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    emit: Emitter = _emit_json,
) -> int:
    validate_expectation(expectation)
    if poll_seconds <= 0:
        raise ReleaseRunError("Poll interval must be greater than zero.")
    if timeout_seconds <= 0:
        raise ReleaseRunError("Timeout must be greater than zero.")
    started_at = clock()
    previous_snapshot: tuple[str, str] | None = None
    while True:
        run = _mapping(client.get_json(expectation.run_endpoint), "Workflow run")
        status, conclusion, run_actor, triggering_actor, run_attempt = validate_run_identity(run, expectation)
        if status == "completed":
            event = "succeeded" if conclusion == "success" else "failed"
            emit(
                {
                    "event": event,
                    "run_id": expectation.run_id,
                    "workflow": expectation.workflow,
                    "head_sha": expectation.head_sha,
                    "conclusion": conclusion,
                    "run_actor": run_actor,
                    "triggering_actor": triggering_actor,
                    "run_attempt": run_attempt,
                }
            )
            return EXIT_SUCCESS if conclusion == "success" else EXIT_FAILED

        branch_sha = current_branch_sha(client, expectation)
        if branch_sha != expectation.head_sha:
            emit(
                {
                    "event": "source_moved",
                    "run_id": expectation.run_id,
                    "workflow": expectation.workflow,
                    "expected_head_sha": expectation.head_sha,
                    "current_branch_sha": branch_sha,
                    "branch": expectation.branch,
                }
            )
            return EXIT_SAFETY_ERROR

        if status == "waiting":
            deployment = expected_pending_deployment(
                client.get_json(expectation.pending_deployments_endpoint), expectation
            )
            environment = _mapping(deployment.get("environment"), "Pending deployment environment")
            environment_id = environment.get("id")
            if not isinstance(environment_id, int) or environment_id <= 0:
                raise ReleaseRunError("Pending deployment environment ID must be a positive integer.")
            can_approve = deployment.get("current_user_can_approve")
            if not isinstance(can_approve, bool):
                raise ReleaseRunError("Pending deployment approval capability must be boolean.")
            reviewers = pending_reviewer_logins(deployment)
            if APPROVAL_ACTOR not in reviewers:
                raise ReleaseRunError(
                    f"Required approval actor {APPROVAL_ACTOR!r} is not a reviewer for {expectation.environment!r}."
                )
            fingerprint = build_approval_fingerprint(
                expectation,
                environment_id=environment_id,
                run_attempt=run_attempt,
                run_actor=run_actor,
                triggering_actor=triggering_actor,
            )
            emit(
                {
                    "event": "approval_required",
                    "run_id": expectation.run_id,
                    "workflow": expectation.workflow,
                    "head_sha": expectation.head_sha,
                    "branch": expectation.branch,
                    "environment": expectation.environment,
                    "environment_id": environment_id,
                    "reviewers": reviewers,
                    "current_user_can_approve": can_approve,
                    "run_actor": run_actor,
                    "triggering_actor": triggering_actor,
                    "run_attempt": run_attempt,
                    "approval_fingerprint": fingerprint,
                }
            )
            return EXIT_APPROVAL_REQUIRED

        if status not in NONTERMINAL_STATUSES:
            raise ReleaseRunError(f"Unexpected workflow run status: {status!r}.")
        snapshot = (status, conclusion)
        if snapshot != previous_snapshot:
            emit(
                {
                    "event": "running",
                    "run_id": expectation.run_id,
                    "workflow": expectation.workflow,
                    "head_sha": expectation.head_sha,
                    "branch": expectation.branch,
                    "status": status,
                }
            )
            previous_snapshot = snapshot
        if clock() - started_at >= timeout_seconds:
            emit(
                {
                    "event": "timeout",
                    "run_id": expectation.run_id,
                    "workflow": expectation.workflow,
                    "head_sha": expectation.head_sha,
                    "timeout_seconds": timeout_seconds,
                }
            )
            return EXIT_TIMEOUT
        sleeper(poll_seconds)


def approve_release_run(
    expectation: RunExpectation,
    client: GitHubAPI,
    *,
    confirm_sha: str,
    approval_fingerprint: str,
    comment: str,
    emit: Emitter = _emit_json,
) -> int:
    validate_expectation(expectation)
    if confirm_sha != expectation.head_sha:
        raise ReleaseRunError("Confirmation SHA does not match the expected workflow head SHA.")
    if not comment.strip():
        raise ReleaseRunError("Approval comment must not be empty.")

    user = _mapping(client.get_json("user", active_auth=True), "Active GitHub user")
    login = _string(user, "login", "Active GitHub user")
    if login != APPROVAL_ACTOR:
        raise ReleaseRunError(
            f"Active GitHub login {login!r} does not match required approval actor {APPROVAL_ACTOR!r}."
        )

    run = _mapping(client.get_json(expectation.run_endpoint, active_auth=True), "Workflow run")
    status, _, run_actor, triggering_actor, run_attempt = validate_run_identity(run, expectation)
    if status != "waiting":
        raise ReleaseRunError(f"Workflow run must be waiting for approval, not {status!r}.")
    if run_actor == APPROVAL_ACTOR or triggering_actor == APPROVAL_ACTOR:
        raise ReleaseRunError("Approval actor must not approve a workflow run dispatched by the same account.")
    branch_sha = current_branch_sha(client, expectation, active_auth=True)
    if branch_sha != expectation.head_sha:
        raise ReleaseRunError(
            f"Protected {expectation.branch} moved to {branch_sha}; refusing to approve stale run {expectation.run_id}."
        )

    deployment = expected_pending_deployment(
        client.get_json(expectation.pending_deployments_endpoint, active_auth=True),
        expectation,
    )
    reviewers = pending_reviewer_logins(deployment)
    if APPROVAL_ACTOR not in reviewers:
        raise ReleaseRunError(
            f"Approval actor {APPROVAL_ACTOR!r} is not a required reviewer for {expectation.environment!r}."
        )
    if deployment.get("current_user_can_approve") is not True:
        raise ReleaseRunError(f"Active GitHub user {APPROVAL_ACTOR!r} cannot approve this deployment.")
    environment = _mapping(deployment.get("environment"), "Pending deployment environment")
    environment_id = environment.get("id")
    if not isinstance(environment_id, int) or environment_id <= 0:
        raise ReleaseRunError("Pending deployment environment ID must be a positive integer.")
    expected_fingerprint = build_approval_fingerprint(
        expectation,
        environment_id=environment_id,
        run_attempt=run_attempt,
        run_actor=run_actor,
        triggering_actor=triggering_actor,
    )
    if approval_fingerprint != expected_fingerprint:
        raise ReleaseRunError("Approval fingerprint does not match the pending deployment.")

    client.post_json(
        expectation.pending_deployments_endpoint,
        {
            "environment_ids": [environment_id],
            "state": "approved",
            "comment": comment,
        },
        active_auth=True,
    )
    emit(
        {
            "event": "approved",
            "run_id": expectation.run_id,
            "workflow": expectation.workflow,
            "head_sha": expectation.head_sha,
            "branch": expectation.branch,
            "environment": expectation.environment,
            "environment_id": environment_id,
            "actor": APPROVAL_ACTOR,
            "run_actor": run_actor,
            "triggering_actor": triggering_actor,
            "run_attempt": run_attempt,
            "approval_fingerprint": expected_fingerprint,
        }
    )
    return EXIT_SUCCESS


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return parsed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Watch and approve guarded GitHub release workflow runs.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_expectation_arguments(command: argparse.ArgumentParser) -> None:
        command.add_argument("--run-id", required=True, type=_positive_int, help="GitHub Actions run ID.")
        command.add_argument("--workflow", required=True, choices=ALLOWED_WORKFLOWS)
        command.add_argument("--head-sha", required=True, help="Expected full Git commit SHA.")

    watch = subparsers.add_parser("watch", help="Watch a release run until approval or completion.")
    add_expectation_arguments(watch)
    watch.add_argument("--poll-seconds", type=_positive_float, default=10.0)
    watch.add_argument("--timeout-seconds", type=_positive_float, default=14_400.0)

    approve = subparsers.add_parser("approve", help="Approve an exact pending release deployment.")
    add_expectation_arguments(approve)
    approve.add_argument("--confirm-sha", required=True, help="Explicit confirmation of the expected head SHA.")
    approve.add_argument("--approval-fingerprint", required=True, help="Fingerprint emitted by the watch command.")
    approve.add_argument("--comment", required=True, help="Audit comment recorded with the deployment review.")
    return parser.parse_args(argv)


def _expectation_from_args(args: argparse.Namespace) -> RunExpectation:
    return RunExpectation(
        run_id=args.run_id,
        workflow=args.workflow,
        head_sha=args.head_sha,
    )


def main(
    argv: list[str] | None = None,
    *,
    client: GitHubAPI | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    emit: Emitter = _emit_json,
) -> int:
    args = parse_args(argv)
    expectation = _expectation_from_args(args)
    github = client or GhAPIClient()
    try:
        if args.command == "watch":
            return watch_release_run(
                expectation,
                github,
                poll_seconds=args.poll_seconds,
                timeout_seconds=args.timeout_seconds,
                sleeper=sleeper,
                clock=clock,
                emit=emit,
            )
        return approve_release_run(
            expectation,
            github,
            confirm_sha=args.confirm_sha,
            approval_fingerprint=args.approval_fingerprint,
            comment=args.comment,
            emit=emit,
        )
    except ReleaseRunError as error:
        emit(
            {
                "event": "safety_error",
                "run_id": expectation.run_id,
                "workflow": expectation.workflow,
                "head_sha": expectation.head_sha,
                "message": str(error),
            }
        )
        return EXIT_SAFETY_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
