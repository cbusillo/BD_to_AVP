import json
import os
import subprocess
import tempfile
import unittest

from collections import defaultdict, deque
from pathlib import Path
from typing import Any
from unittest.mock import patch

from scripts.github_release_run import (
    EXIT_FAILED,
    EXIT_SAFETY_ERROR,
    EXIT_SUCCESS,
    GhAPIClient,
    ReleaseRunError,
    RunExpectation,
    main,
    validate_run_identity,
    watch_release_run,
)
from scripts.release_workflow_policy import (
    OPERATOR_WORKFLOWS,
    PRERELEASE_OPERATOR_WORKFLOW_PATH,
    PRERELEASE_ROUTE,
    PRERELEASE_WORKFLOW_NAME,
    REQUIRED_ACTOR,
    RECEIPT_ACTORS,
    STABLE_OPERATOR_WORKFLOW_PATH,
    STABLE_WORKFLOW_NAME,
)


REPOSITORY = "cbusillo/BD_to_AVP"
RUN_ID = 29597548980
HEAD_SHA = "9e9a38c715dbbe5df97e6d3a8ba715731607db6a"
WORKFLOW = STABLE_WORKFLOW_NAME
WORKFLOW_ID = 101_708_423
PRERELEASE_WORKFLOW_ID = 222_333_444
RUN_ENDPOINT = f"repos/{REPOSITORY}/actions/runs/{RUN_ID}"
BRANCH_ENDPOINT = f"repos/{REPOSITORY}/git/ref/heads/main"


class FakeGitHubAPI:
    def __init__(self) -> None:
        self.responses: dict[str, deque[object]] = defaultdict(deque)
        self.get_calls: list[str] = []

    def add(self, endpoint: str, response: object) -> None:
        self.responses[endpoint].append(response)

    def get_json(self, endpoint: str) -> object:
        self.get_calls.append(endpoint)
        queue = self.responses[endpoint]
        if not queue:
            raise AssertionError(f"Unexpected GitHub GET: endpoint={endpoint!r}")
        response = queue.popleft()
        if isinstance(response, Exception):
            raise response
        return response


def expectation(**overrides: Any) -> RunExpectation:
    values: dict[str, Any] = {
        "repository": REPOSITORY,
        "run_id": RUN_ID,
        "workflow": WORKFLOW,
        "head_sha": HEAD_SHA,
    }
    values.update(overrides)
    return RunExpectation(**values)


def workflow_run(*, status: str, conclusion: str | None = None, **overrides: Any) -> dict[str, object]:
    values: dict[str, object] = {
        "name": WORKFLOW,
        "path": STABLE_OPERATOR_WORKFLOW_PATH,
        "workflow_id": WORKFLOW_ID,
        "head_sha": HEAD_SHA,
        "head_branch": "main",
        "event": "workflow_dispatch",
        "status": status,
        "conclusion": conclusion,
        "actor": {"login": REQUIRED_ACTOR},
        "triggering_actor": {"login": REQUIRED_ACTOR},
        "run_attempt": 1,
    }
    values.update(overrides)
    return values


def branch_reference(sha: str = HEAD_SHA) -> dict[str, object]:
    return {"object": {"sha": sha}}


class GitHubReleaseRunWatchTests(unittest.TestCase):
    def test_waiting_run_is_a_safety_error_without_sleeping(self) -> None:
        client = FakeGitHubAPI()
        client.add(RUN_ENDPOINT, workflow_run(status="waiting"))
        client.add(BRANCH_ENDPOINT, branch_reference())

        with self.assertRaisesRegex(ReleaseRunError, "waiting for an environment approval"):
            watch_release_run(
                expectation(),
                client,
                sleeper=lambda _: self.fail("A waiting run must not keep sleeping."),
                emit=lambda _: None,
            )

    def test_unexpected_engine_workflow_path_is_rejected_before_github_access(self) -> None:
        client = FakeGitHubAPI()

        with self.assertRaisesRegex(ReleaseRunError, "engine workflow path"):
            watch_release_run(
                expectation(engine_workflow_path=".github/workflows/untrusted-engine.yml"),
                client,
            )

        self.assertEqual(client.get_calls, [])

    def test_nonterminal_run_fails_immediately_when_main_moves(self) -> None:
        client = FakeGitHubAPI()
        client.add(RUN_ENDPOINT, workflow_run(status="in_progress"))
        client.add(BRANCH_ENDPOINT, branch_reference("01a17619e27b3fd42e7d3ba900e42428a065d28a"))
        events: list[dict[str, object]] = []

        result = watch_release_run(expectation(), client, emit=events.append)

        self.assertEqual(result, EXIT_SAFETY_ERROR)
        self.assertEqual(events[0]["event"], "source_moved")
        self.assertEqual(events[0]["current_branch_sha"], "01a17619e27b3fd42e7d3ba900e42428a065d28a")

    def test_completed_success_does_not_require_main_to_remain_frozen(self) -> None:
        client = FakeGitHubAPI()
        client.add(RUN_ENDPOINT, workflow_run(status="completed", conclusion="success"))
        events: list[dict[str, object]] = []

        result = watch_release_run(expectation(), client, emit=events.append)

        self.assertEqual(result, EXIT_SUCCESS)
        self.assertEqual(events[0]["event"], "succeeded")
        self.assertEqual(client.get_calls, [RUN_ENDPOINT])

    def test_completed_failure_returns_failed_exit_code(self) -> None:
        client = FakeGitHubAPI()
        client.add(RUN_ENDPOINT, workflow_run(status="completed", conclusion="failure"))
        events: list[dict[str, object]] = []

        result = watch_release_run(expectation(), client, emit=events.append)

        self.assertEqual(result, EXIT_FAILED)
        self.assertEqual(events[0]["event"], "failed")

    def test_prerelease_workflow_is_supported_with_its_validated_route(self) -> None:
        client = FakeGitHubAPI()
        client.add(
            RUN_ENDPOINT,
            workflow_run(
                status="completed",
                conclusion="success",
                name=PRERELEASE_WORKFLOW_NAME,
                path=PRERELEASE_OPERATOR_WORKFLOW_PATH,
                workflow_id=PRERELEASE_WORKFLOW_ID,
            ),
        )
        events: list[dict[str, object]] = []

        result = watch_release_run(expectation(workflow=PRERELEASE_WORKFLOW_NAME), client, emit=events.append)

        self.assertEqual(result, EXIT_SUCCESS)
        self.assertEqual(events[0]["workflow_id"], PRERELEASE_WORKFLOW_ID)
        self.assertEqual(events[0]["workflow_path"], PRERELEASE_OPERATOR_WORKFLOW_PATH)
        self.assertEqual(events[0]["release_route"], PRERELEASE_ROUTE)

    def test_watch_polls_until_success(self) -> None:
        client = FakeGitHubAPI()
        client.add(RUN_ENDPOINT, workflow_run(status="in_progress"))
        client.add(RUN_ENDPOINT, workflow_run(status="completed", conclusion="success"))
        client.add(BRANCH_ENDPOINT, branch_reference())
        events: list[dict[str, object]] = []
        sleeps: list[float] = []

        result = watch_release_run(
            expectation(),
            client,
            poll_seconds=3,
            sleeper=sleeps.append,
            emit=events.append,
        )

        self.assertEqual(result, EXIT_SUCCESS)
        self.assertEqual(sleeps, [3])
        self.assertEqual([event["event"] for event in events], ["running", "succeeded"])

    def test_wrong_workflow_is_a_safety_error(self) -> None:
        client = FakeGitHubAPI()
        client.add(RUN_ENDPOINT, workflow_run(status="in_progress", name="Unexpected release workflow"))
        events: list[dict[str, object]] = []

        result = main(
            [
                "watch",
                "--run-id",
                str(RUN_ID),
                "--workflow",
                WORKFLOW,
                "--head-sha",
                HEAD_SHA,
            ],
            client=client,
            emit=events.append,
        )

        self.assertEqual(result, EXIT_SAFETY_ERROR)
        self.assertEqual(events[0]["event"], "safety_error")
        self.assertIn("identity mismatch", str(events[0]["message"]))

    def test_stable_recovery_run_name_preserves_workflow_identity(self) -> None:
        identity = validate_run_identity(
            workflow_run(status="in_progress", name="Stable PyPI recovery"),
            expectation(),
        )

        self.assertEqual(identity.workflow_id, WORKFLOW_ID)

    def test_wrong_workflow_path_is_a_safety_error(self) -> None:
        client = FakeGitHubAPI()
        client.add(RUN_ENDPOINT, workflow_run(status="in_progress", path=".github/workflows/untrusted.yml"))
        events: list[dict[str, object]] = []

        result = main(
            [
                "watch",
                "--run-id",
                str(RUN_ID),
                "--workflow",
                WORKFLOW,
                "--head-sha",
                HEAD_SHA,
            ],
            client=client,
            emit=events.append,
        )

        self.assertEqual(result, EXIT_SAFETY_ERROR)
        self.assertIn("workflow_path", str(events[0]["message"]))

    def test_nonpositive_or_noninteger_returned_workflow_id_is_a_safety_error(self) -> None:
        for workflow_id in (0, -1, "123", True):
            with self.subTest(workflow_id=workflow_id):
                client = FakeGitHubAPI()
                client.add(RUN_ENDPOINT, workflow_run(status="in_progress", workflow_id=workflow_id))
                events: list[dict[str, object]] = []

                result = main(
                    [
                        "watch",
                        "--run-id",
                        str(RUN_ID),
                        "--workflow",
                        WORKFLOW,
                        "--head-sha",
                        HEAD_SHA,
                    ],
                    client=client,
                    emit=events.append,
                )

                self.assertEqual(result, EXIT_SAFETY_ERROR)
                self.assertIn("positive integer returned by GitHub", str(events[0]["message"]))

    def test_both_run_actors_must_be_release_automation(self) -> None:
        for actor_field in ("actor", "triggering_actor"):
            for actor in {"cbusillo", "untrusted-app[bot]", *RECEIPT_ACTORS} - {REQUIRED_ACTOR}:
                with self.subTest(actor_field=actor_field, actor=actor):
                    client = FakeGitHubAPI()
                    client.add(
                        RUN_ENDPOINT,
                        workflow_run(status="completed", conclusion="success", **{actor_field: {"login": actor}}),
                    )
                    events: list[dict[str, object]] = []

                    result = main(
                        [
                            "watch",
                            "--run-id",
                            str(RUN_ID),
                            "--workflow",
                            WORKFLOW,
                            "--head-sha",
                            HEAD_SHA,
                        ],
                        client=client,
                        emit=events.append,
                    )

                    self.assertEqual(result, EXIT_SAFETY_ERROR)
                    self.assertIn(REQUIRED_ACTOR, str(events[0]["message"]))


class GitHubReleaseRunContractTests(unittest.TestCase):
    def test_operator_metadata_agrees_with_the_monitor_contract(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        operations = json.loads((repository / ".github/github.json").read_text(encoding="utf-8"))["releaseOperations"]

        self.assertEqual(operations["safetyErrorExitCode"], EXIT_SAFETY_ERROR)
        self.assertEqual(
            operations["workflows"],
            {name: {"path": policy.path, "route": policy.route} for name, policy in OPERATOR_WORKFLOWS.items()},
        )

    def test_retired_release_workflow_name_is_rejected(self) -> None:
        client = FakeGitHubAPI()

        with self.assertRaisesRegex(ReleaseRunError, "Workflow must be one of"):
            watch_release_run(expectation(workflow="Release from protected main"), client)

        self.assertEqual(client.get_calls, [])


class GhAPIClientTests(unittest.TestCase):
    @staticmethod
    def executable(root: Path, body: str) -> Path:
        path = root / "fake-gh"
        path.write_text(f"#!/bin/sh\nset -eu\n{body}\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    @patch("scripts.github_release_run.subprocess.run")
    def test_request_uses_the_automation_token_on_the_fixed_host(self, run: Any) -> None:
        run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout='{"id":1}',
            stderr="",
        )
        client = GhAPIClient()

        with patch.dict(os.environ, {"CODEX_GITHUB_TOKEN": "codex-token"}, clear=True):
            result = client.get_json(RUN_ENDPOINT)

        self.assertEqual(result, {"id": 1})
        command = run.call_args.args[0]
        self.assertEqual(command[:4], ["gh", "api", "--hostname", "github.com"])
        self.assertIn("Accept: application/vnd.github+json", command)
        self.assertIn("X-GitHub-Api-Version: 2022-11-28", command)
        self.assertEqual(command[-1], RUN_ENDPOINT)
        self.assertEqual(run.call_args.kwargs["timeout"], 30.0)
        self.assertEqual(run.call_args.kwargs["env"]["GH_TOKEN"], "codex-token")

    @patch("scripts.github_release_run.subprocess.run")
    def test_github_cli_failure_does_not_echo_stderr(self, run: Any) -> None:
        run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="sensitive-token-value",
        )
        client = GhAPIClient()

        with self.assertRaisesRegex(ReleaseRunError, "exit status 1") as context:
            client.get_json(RUN_ENDPOINT)

        self.assertNotIn("sensitive-token-value", str(context.exception))

    @patch("scripts.github_release_run.subprocess.run")
    def test_github_cli_timeout_is_bounded(self, run: Any) -> None:
        run.side_effect = subprocess.TimeoutExpired(cmd=["gh", "api"], timeout=30)
        client = GhAPIClient()

        with self.assertRaisesRegex(ReleaseRunError, "timed out"):
            client.get_json(RUN_ENDPOINT)

    def test_binary_download_uses_bounded_bytes_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            executable = self.executable(Path(temporary_directory), "printf archive")
            client = GhAPIClient(executable=str(executable))

            content = client.get_bytes(
                "repos/cbusillo/BD_to_AVP/actions/artifacts/123/zip",
                max_bytes=1024,
                timeout_seconds=5,
            )

        self.assertEqual(content, b"archive")

    def test_binary_download_stops_at_size_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            executable = self.executable(Path(temporary_directory), "while :; do printf oversized; done")
            client = GhAPIClient(executable=str(executable))

            with self.assertRaisesRegex(ReleaseRunError, "exceeded the 4-byte limit"):
                client.get_bytes(
                    "repos/cbusillo/BD_to_AVP/actions/artifacts/123/zip",
                    max_bytes=4,
                    timeout_seconds=5,
                )

    def test_binary_download_timeout_kills_process(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            executable = self.executable(Path(temporary_directory), "sleep 5; printf late")
            client = GhAPIClient(executable=str(executable))

            with self.assertRaisesRegex(ReleaseRunError, "timed out"):
                client.get_bytes(
                    "repos/cbusillo/BD_to_AVP/actions/artifacts/123/zip",
                    max_bytes=1024,
                    timeout_seconds=0.05,
                )


if __name__ == "__main__":
    unittest.main()
