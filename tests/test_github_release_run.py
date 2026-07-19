import io
import json
import os
import subprocess
import unittest

from collections import defaultdict, deque
from collections.abc import Mapping
from contextlib import redirect_stderr
from pathlib import Path
from typing import Any
from unittest.mock import patch

from scripts.github_release_run import (
    EXIT_APPROVAL_REQUIRED,
    EXIT_FAILED,
    EXIT_SAFETY_ERROR,
    EXIT_SUCCESS,
    GhAPIClient,
    ReleaseRunError,
    RunExpectation,
    approve_release_run,
    build_approval_fingerprint,
    main,
    parse_args,
    watch_release_run,
)
from scripts.release_workflow_policy import ENGINE_WORKFLOW_PATH, OPERATOR_WORKFLOW_PATH, REQUIRED_ACTOR


REPOSITORY = "cbusillo/BD_to_AVP"
RUN_ID = 29597548980
HEAD_SHA = "9e9a38c715dbbe5df97e6d3a8ba715731607db6a"
WORKFLOW = "Release from protected main"
ENVIRONMENT_ID = 17_971_370_694
REPO_ROOT = Path(__file__).resolve().parents[1]
RUN_ENDPOINT = f"repos/{REPOSITORY}/actions/runs/{RUN_ID}"
BRANCH_ENDPOINT = f"repos/{REPOSITORY}/git/ref/heads/main"
PENDING_ENDPOINT = f"{RUN_ENDPOINT}/pending_deployments"


class FakeGitHubAPI:
    def __init__(self) -> None:
        self.responses: dict[tuple[str, bool], deque[object]] = defaultdict(deque)
        self.get_calls: list[tuple[str, bool]] = []
        self.post_calls: list[tuple[str, Mapping[str, object], bool]] = []

    def add(self, endpoint: str, response: object, *, active_auth: bool = False) -> None:
        self.responses[(endpoint, active_auth)].append(response)

    def get_json(self, endpoint: str, *, active_auth: bool = False) -> object:
        self.get_calls.append((endpoint, active_auth))
        queue = self.responses[(endpoint, active_auth)]
        if not queue:
            raise AssertionError(f"Unexpected GitHub GET: endpoint={endpoint!r} active_auth={active_auth}")
        response = queue.popleft()
        if isinstance(response, Exception):
            raise response
        return response

    def post_json(self, endpoint: str, payload: Mapping[str, object], *, active_auth: bool = False) -> object:
        self.post_calls.append((endpoint, payload, active_auth))
        return [{"environment": "macos-signing"}]


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
        "path": ".github/workflows/briefcase.yml",
        "workflow_id": 101_708_423,
        "head_sha": HEAD_SHA,
        "head_branch": "main",
        "event": "workflow_dispatch",
        "status": status,
        "conclusion": conclusion,
        "actor": {"login": "shiny-code-bot"},
        "triggering_actor": {"login": "shiny-code-bot"},
        "run_attempt": 1,
    }
    values.update(overrides)
    return values


def branch_reference(sha: str = HEAD_SHA) -> dict[str, object]:
    return {"object": {"sha": sha}}


def pending_deployments(
    *,
    environment: str = "macos-signing",
    environment_id: int = ENVIRONMENT_ID,
    reviewers: tuple[str, ...] = ("cbusillo",),
    can_approve: bool = False,
) -> list[dict[str, object]]:
    return [
        {
            "environment": {"id": environment_id, "name": environment},
            "current_user_can_approve": can_approve,
            "reviewers": [{"type": "User", "reviewer": {"login": reviewer}} for reviewer in reviewers],
        }
    ]


def approval_fingerprint() -> str:
    return build_approval_fingerprint(
        expectation(),
        environment_id=ENVIRONMENT_ID,
        run_attempt=1,
        run_actor="shiny-code-bot",
        triggering_actor="shiny-code-bot",
    )


class GitHubReleaseRunWatchTests(unittest.TestCase):
    def test_waiting_run_returns_approval_required_without_sleeping(self) -> None:
        client = FakeGitHubAPI()
        client.add(RUN_ENDPOINT, workflow_run(status="waiting"))
        client.add(BRANCH_ENDPOINT, branch_reference())
        client.add(PENDING_ENDPOINT, pending_deployments())
        events: list[dict[str, object]] = []

        result = watch_release_run(
            expectation(),
            client,
            sleeper=lambda _: self.fail("Approval-required runs must not keep sleeping."),
            emit=events.append,
        )

        self.assertEqual(result, EXIT_APPROVAL_REQUIRED)
        self.assertEqual(events[0]["event"], "approval_required")
        self.assertEqual(events[0]["environment"], "macos-signing")
        self.assertEqual(events[0]["reviewers"], ["cbusillo"])
        self.assertFalse(events[0]["current_user_can_approve"])
        self.assertEqual(events[0]["approval_fingerprint"], approval_fingerprint())
        self.assertEqual(events[0]["run_attempt"], 1)
        self.assertEqual(
            events[0]["operator_workflow_ref"],
            f"{REPOSITORY}/{OPERATOR_WORKFLOW_PATH}@refs/heads/main",
        )
        self.assertEqual(events[0]["operator_workflow_sha"], HEAD_SHA)
        self.assertEqual(
            events[0]["engine_workflow_ref"],
            f"{REPOSITORY}/{ENGINE_WORKFLOW_PATH}@refs/heads/main",
        )
        self.assertEqual(events[0]["engine_workflow_sha"], HEAD_SHA)

    def test_waiting_run_rejects_additional_pending_environment(self) -> None:
        client = FakeGitHubAPI()
        client.add(RUN_ENDPOINT, workflow_run(status="waiting"))
        client.add(BRANCH_ENDPOINT, branch_reference())
        client.add(
            PENDING_ENDPOINT,
            [
                *pending_deployments(),
                *pending_deployments(environment="sparkle-feed-ops", environment_id=99),
            ],
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
        self.assertIn("exactly one pending deployment", str(events[0]["message"]))

    def test_approval_fingerprint_changes_between_run_attempts(self) -> None:
        first = build_approval_fingerprint(
            expectation(),
            environment_id=ENVIRONMENT_ID,
            run_attempt=1,
            run_actor="shiny-code-bot",
            triggering_actor="shiny-code-bot",
        )
        second = build_approval_fingerprint(
            expectation(),
            environment_id=ENVIRONMENT_ID,
            run_attempt=2,
            run_actor="shiny-code-bot",
            triggering_actor="shiny-code-bot",
        )

        self.assertNotEqual(first, second)

    def test_approval_fingerprint_binds_reusable_engine_path(self) -> None:
        trusted = approval_fingerprint()
        substituted = build_approval_fingerprint(
            expectation(engine_workflow_path=".github/workflows/untrusted-engine.yml"),
            environment_id=ENVIRONMENT_ID,
            run_attempt=1,
            run_actor="shiny-code-bot",
            triggering_actor="shiny-code-bot",
        )

        self.assertNotEqual(trusted, substituted)

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
        self.assertEqual(client.get_calls, [(RUN_ENDPOINT, False)])

    def test_completed_failure_returns_failed_exit_code(self) -> None:
        client = FakeGitHubAPI()
        client.add(RUN_ENDPOINT, workflow_run(status="completed", conclusion="failure"))
        events: list[dict[str, object]] = []

        result = watch_release_run(expectation(), client, emit=events.append)

        self.assertEqual(result, EXIT_FAILED)
        self.assertEqual(events[0]["event"], "failed")

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

    def test_wrong_workflow_id_is_a_safety_error(self) -> None:
        client = FakeGitHubAPI()
        client.add(RUN_ENDPOINT, workflow_run(status="in_progress", workflow_id=123))
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
        self.assertIn("workflow ID", str(events[0]["message"]))


class GitHubReleaseRunApprovalTests(unittest.TestCase):
    def approval_client(
        self,
        *,
        can_approve: bool = True,
        environment: str = "macos-signing",
        run_actor: str = "shiny-code-bot",
        triggering_actor: str = "shiny-code-bot",
        reviewers: tuple[str, ...] = ("cbusillo",),
    ) -> FakeGitHubAPI:
        client = FakeGitHubAPI()
        client.add("user", {"login": "cbusillo"}, active_auth=True)
        client.add(
            RUN_ENDPOINT,
            workflow_run(
                status="waiting",
                actor={"login": run_actor},
                triggering_actor={"login": triggering_actor},
            ),
            active_auth=True,
        )
        client.add(BRANCH_ENDPOINT, branch_reference(), active_auth=True)
        client.add(
            PENDING_ENDPOINT,
            pending_deployments(
                can_approve=can_approve,
                environment=environment,
                reviewers=reviewers,
            ),
            active_auth=True,
        )
        return client

    def test_approval_uses_active_auth_and_exact_environment(self) -> None:
        client = self.approval_client()
        events: list[dict[str, object]] = []

        result = approve_release_run(
            expectation(),
            client,
            confirm_sha=HEAD_SHA,
            approval_fingerprint=approval_fingerprint(),
            comment="Approved after explicit release authorization.",
            emit=events.append,
        )

        self.assertEqual(result, EXIT_SUCCESS)
        self.assertTrue(all(active_auth for _, active_auth in client.get_calls))
        self.assertEqual(len(client.post_calls), 1)
        endpoint, payload, active_auth = client.post_calls[0]
        self.assertEqual(endpoint, PENDING_ENDPOINT)
        self.assertEqual(payload["environment_ids"], [ENVIRONMENT_ID])
        self.assertEqual(payload["state"], "approved")
        self.assertTrue(active_auth)
        self.assertEqual(events[0]["event"], "approved")
        self.assertEqual(events[0]["actor"], "cbusillo")
        self.assertEqual(
            events[0]["engine_workflow_ref"],
            f"{REPOSITORY}/{ENGINE_WORKFLOW_PATH}@refs/heads/main",
        )

    def test_wrong_confirmation_sha_fails_before_github_access(self) -> None:
        client = FakeGitHubAPI()
        events: list[dict[str, object]] = []

        result = main(
            [
                "approve",
                "--run-id",
                str(RUN_ID),
                "--workflow",
                WORKFLOW,
                "--head-sha",
                HEAD_SHA,
                "--confirm-sha",
                "01a17619e27b3fd42e7d3ba900e42428a065d28a",
                "--approval-fingerprint",
                approval_fingerprint(),
                "--comment",
                "Approved after explicit release authorization.",
            ],
            client=client,
            emit=events.append,
        )

        self.assertEqual(result, EXIT_SAFETY_ERROR)
        self.assertEqual(client.get_calls, [])
        self.assertIn("Confirmation SHA", str(events[0]["message"]))

    def test_wrong_active_login_is_rejected(self) -> None:
        client = FakeGitHubAPI()
        client.add("user", {"login": "shiny-code-bot"}, active_auth=True)
        events: list[dict[str, object]] = []

        result = main(
            [
                "approve",
                "--run-id",
                str(RUN_ID),
                "--workflow",
                WORKFLOW,
                "--head-sha",
                HEAD_SHA,
                "--confirm-sha",
                HEAD_SHA,
                "--approval-fingerprint",
                approval_fingerprint(),
                "--comment",
                "Approved after explicit release authorization.",
            ],
            client=client,
            emit=events.append,
        )

        self.assertEqual(result, EXIT_SAFETY_ERROR)
        self.assertIn("does not match", str(events[0]["message"]))

    def test_unexpected_environment_is_rejected(self) -> None:
        client = self.approval_client(environment="sparkle-release")
        events: list[dict[str, object]] = []

        result = main(
            [
                "approve",
                "--run-id",
                str(RUN_ID),
                "--workflow",
                WORKFLOW,
                "--head-sha",
                HEAD_SHA,
                "--confirm-sha",
                HEAD_SHA,
                "--approval-fingerprint",
                approval_fingerprint(),
                "--comment",
                "Approved after explicit release authorization.",
            ],
            client=client,
            emit=events.append,
        )

        self.assertEqual(result, EXIT_SAFETY_ERROR)
        self.assertIn("exactly one pending deployment", str(events[0]["message"]))
        self.assertEqual(client.post_calls, [])

    def test_non_approvable_deployment_is_rejected(self) -> None:
        client = self.approval_client(can_approve=False)
        events: list[dict[str, object]] = []

        result = main(
            [
                "approve",
                "--run-id",
                str(RUN_ID),
                "--workflow",
                WORKFLOW,
                "--head-sha",
                HEAD_SHA,
                "--confirm-sha",
                HEAD_SHA,
                "--approval-fingerprint",
                approval_fingerprint(),
                "--comment",
                "Approved after explicit release authorization.",
            ],
            client=client,
            emit=events.append,
        )

        self.assertEqual(result, EXIT_SAFETY_ERROR)
        self.assertIn("cannot approve", str(events[0]["message"]))
        self.assertEqual(client.post_calls, [])

    def test_wrong_approval_fingerprint_is_rejected(self) -> None:
        client = self.approval_client()
        events: list[dict[str, object]] = []

        result = main(
            [
                "approve",
                "--run-id",
                str(RUN_ID),
                "--workflow",
                WORKFLOW,
                "--head-sha",
                HEAD_SHA,
                "--confirm-sha",
                HEAD_SHA,
                "--approval-fingerprint",
                "0" * 64,
                "--comment",
                "Approved after explicit release authorization.",
            ],
            client=client,
            emit=events.append,
        )

        self.assertEqual(result, EXIT_SAFETY_ERROR)
        self.assertIn("fingerprint", str(events[0]["message"]))
        self.assertEqual(client.post_calls, [])

    def test_approval_actor_cannot_self_approve_own_dispatch(self) -> None:
        client = self.approval_client(run_actor="cbusillo")
        events: list[dict[str, object]] = []

        result = main(
            [
                "approve",
                "--run-id",
                str(RUN_ID),
                "--workflow",
                WORKFLOW,
                "--head-sha",
                HEAD_SHA,
                "--confirm-sha",
                HEAD_SHA,
                "--approval-fingerprint",
                approval_fingerprint(),
                "--comment",
                "Approved after explicit release authorization.",
            ],
            client=client,
            emit=events.append,
        )

        self.assertEqual(result, EXIT_SAFETY_ERROR)
        self.assertIn("must not approve", str(events[0]["message"]))
        self.assertEqual(client.post_calls, [])

    def test_approval_actor_cannot_self_approve_own_rerun(self) -> None:
        client = self.approval_client(triggering_actor="cbusillo")
        events: list[dict[str, object]] = []

        result = main(
            [
                "approve",
                "--run-id",
                str(RUN_ID),
                "--workflow",
                WORKFLOW,
                "--head-sha",
                HEAD_SHA,
                "--confirm-sha",
                HEAD_SHA,
                "--approval-fingerprint",
                approval_fingerprint(),
                "--comment",
                "Approved after explicit release authorization.",
            ],
            client=client,
            emit=events.append,
        )

        self.assertEqual(result, EXIT_SAFETY_ERROR)
        self.assertIn("must not approve", str(events[0]["message"]))
        self.assertEqual(client.post_calls, [])

    def test_additional_reviewer_is_rejected(self) -> None:
        client = self.approval_client(reviewers=("cbusillo", "another-maintainer"))
        events: list[dict[str, object]] = []

        result = main(
            [
                "approve",
                "--run-id",
                str(RUN_ID),
                "--workflow",
                WORKFLOW,
                "--head-sha",
                HEAD_SHA,
                "--confirm-sha",
                HEAD_SHA,
                "--approval-fingerprint",
                approval_fingerprint(),
                "--comment",
                "Approved after explicit release authorization.",
            ],
            client=client,
            emit=events.append,
        )

        self.assertEqual(result, EXIT_SAFETY_ERROR)
        self.assertIn("exactly one pending deployment reviewer", str(events[0]["message"]))
        self.assertEqual(client.post_calls, [])


class GitHubReleaseRunContractTests(unittest.TestCase):
    def test_parser_error_code_does_not_overlap_approval_required(self) -> None:
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as context:
            parse_args(["watch"])

        self.assertEqual(context.exception.code, 2)
        self.assertNotEqual(context.exception.code, EXIT_APPROVAL_REQUIRED)

    def test_operator_commands_and_reviewer_are_discoverable(self) -> None:
        config = json.loads((REPO_ROOT / ".github" / "github.json").read_text(encoding="utf-8"))
        operations = config["releaseOperations"]
        signing = config["releaseEnvironments"]["macos-signing"]

        self.assertEqual(operations["watchCommand"], "uv run python -m scripts.github_release_run watch")
        self.assertEqual(operations["approveCommand"], "uv run python -m scripts.github_release_run approve")
        self.assertEqual(operations["approvalRequiredExitCode"], EXIT_APPROVAL_REQUIRED)
        self.assertEqual(operations["safetyErrorExitCode"], EXIT_SAFETY_ERROR)
        self.assertEqual(operations["repository"], REPOSITORY)
        self.assertEqual(operations["releaseActor"], REQUIRED_ACTOR)
        self.assertEqual(operations["approvalActor"], "cbusillo")
        self.assertEqual(operations["requiredBranch"], "main")
        self.assertEqual(operations["requiredEvent"], "workflow_dispatch")
        self.assertEqual(operations["approvalEnvironment"], "macos-signing")
        self.assertEqual(operations["engineWorkflowPath"], ENGINE_WORKFLOW_PATH)
        self.assertEqual(
            operations["workflows"],
            {
                "Release from protected main": {
                    "path": ".github/workflows/briefcase.yml",
                    "id": 101_708_423,
                },
            },
        )
        self.assertTrue(signing["requiredReview"])
        self.assertEqual(signing["reviewers"], ["cbusillo"])
        self.assertTrue(signing["preventSelfReview"])
        self.assertFalse(signing["canAdminsBypass"])

    def test_root_agent_contract_requires_guarded_monitoring(self) -> None:
        instructions = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")

        self.assertIn("scripts.github_release_run watch", instructions)
        self.assertIn("scripts.github_release_run approve", instructions)
        self.assertIn("explicit user authorization", instructions)
        self.assertIn("Keep `main` fixed", instructions)


class GhAPIClientTests(unittest.TestCase):
    @patch("scripts.github_release_run.subprocess.run")
    def test_active_auth_scrubs_token_and_repo_overrides(self, run: Any) -> None:
        run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout='{"login":"cbusillo"}',
            stderr="",
        )
        client = GhAPIClient()

        with patch.dict(
            os.environ,
            {
                "GH_TOKEN": "gh-token",
                "GITHUB_TOKEN": "github-token",
                "CODEX_GITHUB_TOKEN": "codex-token",
                "GH_ENTERPRISE_TOKEN": "enterprise-token",
                "GITHUB_ENTERPRISE_TOKEN": "github-enterprise-token",
                "GH_HOST": "example.invalid",
                "GH_REPO": "wrong/repository",
            },
        ):
            result = client.get_json("user", active_auth=True)

        self.assertEqual(result, {"login": "cbusillo"})
        command = run.call_args.args[0]
        environment = run.call_args.kwargs["env"]
        self.assertEqual(command[:4], ["gh", "api", "--hostname", "github.com"])
        self.assertIn("Accept: application/vnd.github+json", command)
        self.assertIn("X-GitHub-Api-Version: 2022-11-28", command)
        self.assertEqual(command[-1], "user")
        self.assertEqual(run.call_args.kwargs["timeout"], 30.0)
        for name in (
            "GH_TOKEN",
            "GITHUB_TOKEN",
            "CODEX_GITHUB_TOKEN",
            "GH_ENTERPRISE_TOKEN",
            "GITHUB_ENTERPRISE_TOKEN",
            "GH_HOST",
            "GH_REPO",
        ):
            self.assertNotIn(name, environment)

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
            client.get_json("user", active_auth=True)

        self.assertNotIn("sensitive-token-value", str(context.exception))

    @patch("scripts.github_release_run.subprocess.run")
    def test_github_cli_timeout_is_bounded(self, run: Any) -> None:
        run.side_effect = subprocess.TimeoutExpired(cmd=["gh", "api"], timeout=30)
        client = GhAPIClient()

        with self.assertRaisesRegex(ReleaseRunError, "timed out"):
            client.get_json("user", active_auth=True)


if __name__ == "__main__":
    unittest.main()
