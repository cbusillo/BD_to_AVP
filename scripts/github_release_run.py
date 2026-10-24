from __future__ import annotations

import argparse
import json
import os
import re
import selectors
import subprocess
import time

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, cast
from urllib.parse import quote

from scripts.release_workflow_policy import ENGINE_WORKFLOW_PATH, OPERATOR_WORKFLOWS, REQUIRED_ACTOR


EXIT_SUCCESS = 0
EXIT_FAILED = 1
EXIT_SAFETY_ERROR = 21
EXIT_TIMEOUT = 22
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
NONTERMINAL_STATUSES = {"in_progress", "pending", "queued", "requested"}
REPOSITORY = "cbusillo/BD_to_AVP"
WORKFLOW_POLICIES = OPERATOR_WORKFLOWS
ALLOWED_WORKFLOWS = tuple(WORKFLOW_POLICIES)
ALLOWED_RUN_NAMES = {
    "Stable": frozenset({"Stable", "Stable PyPI recovery"}),
    "Prerelease": frozenset({"Prerelease"}),
}
REQUIRED_BRANCH = "main"
REQUIRED_EVENT = "workflow_dispatch"


class ReleaseRunError(RuntimeError):
    pass


class GitHubAPI(Protocol):
    def get_json(self, endpoint: str) -> object:
        raise NotImplementedError

    def get_bytes(
        self,
        endpoint: str,
        *,
        max_bytes: int,
        timeout_seconds: float,
    ) -> bytes:
        raise NotImplementedError


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

    def _environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        if "GH_TOKEN" not in environment:
            token = environment.get("CODEX_GITHUB_TOKEN") or environment.get("GITHUB_TOKEN")
            if token:
                environment["GH_TOKEN"] = token
        return environment

    def _run_json(self, arguments: Sequence[str]) -> object:
        try:
            result = subprocess.run(
                [self.executable, *arguments],
                text=True,
                capture_output=True,
                env=self._environment(),
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

    def get_json(self, endpoint: str) -> object:
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
            ]
        )

    def get_bytes(
        self,
        endpoint: str,
        *,
        max_bytes: int,
        timeout_seconds: float,
    ) -> bytes:
        if max_bytes <= 0:
            raise ValueError("GitHub byte limit must be greater than zero.")
        if timeout_seconds <= 0:
            raise ValueError("GitHub byte request timeout must be greater than zero.")
        command = [
            self.executable,
            "api",
            "--hostname",
            self.hostname,
            "-H",
            "Accept: application/vnd.github+json",
            "-H",
            "X-GitHub-Api-Version: 2022-11-28",
            endpoint,
        ]
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=self._environment(),
            )
        except OSError as error:
            raise ReleaseRunError("GitHub CLI byte request could not start.") from error
        if process.stdout is None:
            process.kill()
            process.wait()
            raise ReleaseRunError("GitHub CLI byte request did not expose stdout.")
        deadline = time.monotonic() + timeout_seconds
        content = bytearray()
        selector = selectors.DefaultSelector()
        os.set_blocking(process.stdout.fileno(), False)
        selector.register(process.stdout, selectors.EVENT_READ)
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    process.kill()
                    process.wait()
                    raise ReleaseRunError("GitHub CLI byte request timed out.")
                events = selector.select(timeout=min(remaining, 0.25))
                if not events:
                    if process.poll() is not None:
                        try:
                            chunk = os.read(process.stdout.fileno(), max_bytes + 1 - len(content))
                        except BlockingIOError:
                            chunk = b""
                        if chunk:
                            content.extend(chunk)
                        if len(content) > max_bytes:
                            raise ReleaseRunError(f"GitHub CLI byte request exceeded the {max_bytes}-byte limit.")
                        break
                    continue
                try:
                    chunk = os.read(
                        process.stdout.fileno(),
                        min(64 * 1024, max_bytes + 1 - len(content)),
                    )
                except BlockingIOError:
                    continue
                if not chunk:
                    break
                content.extend(chunk)
                if len(content) > max_bytes:
                    process.kill()
                    process.wait()
                    raise ReleaseRunError(f"GitHub CLI byte request exceeded the {max_bytes}-byte limit.")
            remaining = max(0.001, deadline - time.monotonic())
            try:
                returncode = process.wait(timeout=remaining)
            except subprocess.TimeoutExpired as error:
                process.kill()
                process.wait()
                raise ReleaseRunError("GitHub CLI byte request timed out.") from error
        finally:
            selector.close()
            process.stdout.close()
            if process.poll() is None:
                process.kill()
                process.wait()
        if returncode != 0:
            raise ReleaseRunError(f"GitHub CLI byte request failed with exit status {returncode}.")
        if len(content) > max_bytes:
            raise ReleaseRunError(f"GitHub CLI byte request exceeded the {max_bytes}-byte limit.")
        if not content:
            raise ReleaseRunError("GitHub CLI byte request returned an empty response.")
        return bytes(content)


@dataclass(frozen=True)
class RunExpectation:
    run_id: int
    workflow: str
    head_sha: str
    repository: str = REPOSITORY
    branch: str = REQUIRED_BRANCH
    event: str = REQUIRED_EVENT
    engine_workflow_path: str = ENGINE_WORKFLOW_PATH

    @property
    def run_endpoint(self) -> str:
        return f"repos/{self.repository}/actions/runs/{self.run_id}"

    @property
    def branch_endpoint(self) -> str:
        return f"repos/{self.repository}/git/ref/heads/{quote(self.branch, safe='')}"

    @property
    def workflow_path(self) -> str:
        return WORKFLOW_POLICIES[self.workflow].path

    @property
    def route(self) -> str:
        return WORKFLOW_POLICIES[self.workflow].route

    @property
    def operator_workflow_ref(self) -> str:
        return f"{self.repository}/{self.workflow_path}@refs/heads/{self.branch}"

    @property
    def engine_workflow_ref(self) -> str:
        return f"{self.repository}/{self.engine_workflow_path}@refs/heads/{self.branch}"


@dataclass(frozen=True)
class ValidatedRunIdentity:
    status: str
    conclusion: str
    run_actor: str
    triggering_actor: str
    run_attempt: int
    workflow_id: int


Emitter = Callable[[dict[str, object]], None]


def _emit_json(payload: dict[str, object]) -> None:
    print(json.dumps(payload, sort_keys=True), flush=True)


def _mapping(value: object, description: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ReleaseRunError(f"{description} must be a JSON object.")
    return cast(Mapping[str, Any], value)


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
    if expectation.engine_workflow_path != ENGINE_WORKFLOW_PATH:
        raise ReleaseRunError(f"Release guard requires engine workflow path {ENGINE_WORKFLOW_PATH!r}.")


def validate_run_identity(run: Mapping[str, Any], expectation: RunExpectation) -> ValidatedRunIdentity:
    actual_workflow_name = _string(run, "name", "Workflow run")
    actual = {
        "workflow_path": _string(run, "path", "Workflow run"),
        "head_sha": _string(run, "head_sha", "Workflow run"),
        "branch": _string(run, "head_branch", "Workflow run"),
        "event": _string(run, "event", "Workflow run"),
    }
    expected = {
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
    if actual_workflow_name not in ALLOWED_RUN_NAMES[expectation.workflow]:
        raise ReleaseRunError(
            f"Workflow run identity mismatch: workflow={actual_workflow_name!r} "
            f"(expected one of {sorted(ALLOWED_RUN_NAMES[expectation.workflow])!r})"
        )
    workflow_id = run.get("workflow_id")
    if isinstance(workflow_id, bool) or not isinstance(workflow_id, int) or workflow_id <= 0:
        raise ReleaseRunError("Workflow run workflow ID must be a positive integer returned by GitHub.")
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
    if actor_login != REQUIRED_ACTOR:
        raise ReleaseRunError(f"Workflow run actor must be {REQUIRED_ACTOR!r}, not {actor_login!r}.")
    if triggering_actor_login != REQUIRED_ACTOR:
        raise ReleaseRunError(
            f"Workflow run triggering actor must be {REQUIRED_ACTOR!r}, not {triggering_actor_login!r}."
        )
    run_attempt = run.get("run_attempt")
    if not isinstance(run_attempt, int) or run_attempt <= 0:
        raise ReleaseRunError("Workflow run attempt must be a positive integer.")
    return ValidatedRunIdentity(
        status=status,
        conclusion=conclusion_text,
        run_actor=actor_login,
        triggering_actor=triggering_actor_login,
        run_attempt=run_attempt,
        workflow_id=workflow_id,
    )


def current_branch_sha(client: GitHubAPI, expectation: RunExpectation) -> str:
    reference = _mapping(client.get_json(expectation.branch_endpoint), "Git reference")
    git_object = _mapping(reference.get("object"), "Git reference object")
    return _string(git_object, "sha", "Git reference object")


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
        identity = validate_run_identity(run, expectation)
        if identity.status == "completed":
            event = "succeeded" if identity.conclusion == "success" else "failed"
            emit(
                {
                    "event": event,
                    "run_id": expectation.run_id,
                    "workflow": expectation.workflow,
                    "workflow_id": identity.workflow_id,
                    "workflow_path": expectation.workflow_path,
                    "release_route": expectation.route,
                    "head_sha": expectation.head_sha,
                    "conclusion": identity.conclusion,
                    "run_actor": identity.run_actor,
                    "triggering_actor": identity.triggering_actor,
                    "run_attempt": identity.run_attempt,
                }
            )
            return EXIT_SUCCESS if identity.conclusion == "success" else EXIT_FAILED

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

        if identity.status == "waiting":
            raise ReleaseRunError(
                "Release run is waiting for an environment approval; no release environment should require one. "
                "Check the release environments' protection rules."
            )

        if identity.status not in NONTERMINAL_STATUSES:
            raise ReleaseRunError(f"Unexpected workflow run status: {identity.status!r}.")
        snapshot = (identity.status, identity.conclusion)
        if snapshot != previous_snapshot:
            emit(
                {
                    "event": "running",
                    "run_id": expectation.run_id,
                    "workflow": expectation.workflow,
                    "workflow_id": identity.workflow_id,
                    "workflow_path": expectation.workflow_path,
                    "release_route": expectation.route,
                    "head_sha": expectation.head_sha,
                    "branch": expectation.branch,
                    "status": identity.status,
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
    parser = argparse.ArgumentParser(description="Watch guarded GitHub release workflow runs.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    watch = subparsers.add_parser("watch", help="Watch a release run until it completes.")
    watch.add_argument("--run-id", required=True, type=_positive_int, help="GitHub Actions run ID.")
    watch.add_argument("--workflow", required=True, choices=ALLOWED_WORKFLOWS)
    watch.add_argument("--head-sha", required=True, help="Expected full Git commit SHA.")
    watch.add_argument("--poll-seconds", type=_positive_float, default=10.0)
    watch.add_argument("--timeout-seconds", type=_positive_float, default=14_400.0)

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
        return watch_release_run(
            expectation,
            github,
            poll_seconds=args.poll_seconds,
            timeout_seconds=args.timeout_seconds,
            sleeper=sleeper,
            clock=clock,
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
