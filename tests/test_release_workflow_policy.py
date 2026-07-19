import base64
import io
import json
import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from scripts.release_workflow_policy import (
    ENGINE_WORKFLOW_PATH,
    OPERATOR_WORKFLOW_PATH,
    REPOSITORY,
    REQUIRED_ACTOR,
    REQUIRED_EVENT,
    REQUIRED_REF,
    OIDC_AUDIENCE,
    ReleaseWorkflowPolicyError,
    main,
    validate_engine_environment,
)


HEAD_SHA = "9e9a38c715dbbe5df97e6d3a8ba715731607db6a"


def valid_environment() -> dict[str, str]:
    operator_ref = f"{REPOSITORY}/{OPERATOR_WORKFLOW_PATH}@{REQUIRED_REF}"
    engine_ref = f"{REPOSITORY}/{ENGINE_WORKFLOW_PATH}@{REQUIRED_REF}"
    return {
        "RELEASE_REPOSITORY": REPOSITORY,
        "RELEASE_EVENT_NAME": REQUIRED_EVENT,
        "RELEASE_REF": REQUIRED_REF,
        "RELEASE_SHA": HEAD_SHA,
        "RELEASE_OPERATOR_WORKFLOW_REF": operator_ref,
        "RELEASE_OPERATOR_WORKFLOW_SHA": HEAD_SHA,
        "RELEASE_ENGINE_WORKFLOW_REF": engine_ref,
        "RELEASE_ENGINE_WORKFLOW_SHA": HEAD_SHA,
        "RELEASE_ENGINE_WORKFLOW_REPOSITORY": REPOSITORY,
        "RELEASE_RUN_ID": "29597548980",
        "RELEASE_RUN_ATTEMPT": "1",
        "RELEASE_ACTOR": REQUIRED_ACTOR,
        "RELEASE_TRIGGERING_ACTOR": REQUIRED_ACTOR,
        "INPUT_RELEASE_SHA": HEAD_SHA,
        "INPUT_OPERATOR_WORKFLOW_REF": operator_ref,
        "INPUT_OPERATOR_WORKFLOW_SHA": HEAD_SHA,
        "INPUT_OPERATOR_RUN_ID": "29597548980",
        "INPUT_OPERATOR_RUN_ATTEMPT": "1",
        "INPUT_OPERATOR_ACTOR": REQUIRED_ACTOR,
        "INPUT_OPERATOR_TRIGGERING_ACTOR": REQUIRED_ACTOR,
    }


def oidc_environment() -> dict[str, str]:
    environment = valid_environment()
    del environment["RELEASE_ENGINE_WORKFLOW_REF"]
    del environment["RELEASE_ENGINE_WORKFLOW_SHA"]
    del environment["RELEASE_ENGINE_WORKFLOW_REPOSITORY"]
    environment["ACTIONS_ID_TOKEN_REQUEST_URL"] = "https://token.actions.githubusercontent.test?id=1"
    environment["ACTIONS_ID_TOKEN_REQUEST_TOKEN"] = "ephemeral-request-token"
    return environment


def encoded_oidc_token(environment: dict[str, str], **overrides: str) -> str:
    claims = {
        "aud": OIDC_AUDIENCE,
        "repository": environment["RELEASE_REPOSITORY"],
        "event_name": environment["RELEASE_EVENT_NAME"],
        "ref": environment["RELEASE_REF"],
        "sha": environment["RELEASE_SHA"],
        "workflow_ref": environment["RELEASE_OPERATOR_WORKFLOW_REF"],
        "workflow_sha": environment["RELEASE_OPERATOR_WORKFLOW_SHA"],
        "run_id": environment["RELEASE_RUN_ID"],
        "run_attempt": environment["RELEASE_RUN_ATTEMPT"],
        "actor": environment["RELEASE_ACTOR"],
        "job_workflow_ref": f"{REPOSITORY}/{ENGINE_WORKFLOW_PATH}@{REQUIRED_REF}",
        "job_workflow_sha": HEAD_SHA,
    }
    claims.update(overrides)
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode("utf-8")).decode("ascii").rstrip("=")
    return f"header.{payload}.signature"


class ReleaseWorkflowPolicyTests(unittest.TestCase):
    def test_expected_operator_and_engine_are_bound_to_one_fingerprint(self) -> None:
        evidence = validate_engine_environment(valid_environment())

        self.assertEqual(evidence.release_sha, HEAD_SHA)
        self.assertIn(OPERATOR_WORKFLOW_PATH, evidence.operator_workflow_ref)
        self.assertIn(ENGINE_WORKFLOW_PATH, evidence.engine_workflow_ref)
        self.assertRegex(evidence.fingerprint(), r"^[0-9a-f]{64}$")

    def test_direct_invocation_from_another_workflow_fails_closed(self) -> None:
        environment = valid_environment()
        environment["RELEASE_OPERATOR_WORKFLOW_REF"] = f"{REPOSITORY}/.github/workflows/untrusted.yml@{REQUIRED_REF}"
        environment["INPUT_OPERATOR_WORKFLOW_REF"] = environment["RELEASE_OPERATOR_WORKFLOW_REF"]

        with self.assertRaisesRegex(ReleaseWorkflowPolicyError, "operator workflow ref"):
            validate_engine_environment(environment)

    def test_substituting_another_reusable_workflow_fails_closed(self) -> None:
        environment = valid_environment()
        environment["RELEASE_ENGINE_WORKFLOW_REF"] = (
            f"{REPOSITORY}/.github/workflows/untrusted-engine.yml@{REQUIRED_REF}"
        )

        with self.assertRaisesRegex(ReleaseWorkflowPolicyError, "engine workflow ref"):
            validate_engine_environment(environment)

    def test_non_dispatch_event_and_non_main_ref_fail_closed(self) -> None:
        for key, value, message in (
            ("RELEASE_EVENT_NAME", "push", "event"),
            ("RELEASE_REF", "refs/heads/release", "ref"),
        ):
            with self.subTest(key=key):
                environment = valid_environment()
                environment[key] = value
                with self.assertRaisesRegex(ReleaseWorkflowPolicyError, message):
                    validate_engine_environment(environment)

    def test_unapproved_actor_and_rerun_actor_fail_closed(self) -> None:
        for key in ("RELEASE_ACTOR", "RELEASE_TRIGGERING_ACTOR"):
            with self.subTest(key=key):
                environment = valid_environment()
                environment[key] = "cbusillo"
                with self.assertRaisesRegex(ReleaseWorkflowPolicyError, "actor"):
                    validate_engine_environment(environment)

    def test_caller_cannot_forge_operator_evidence_inputs(self) -> None:
        environment = valid_environment()
        environment["INPUT_OPERATOR_RUN_ATTEMPT"] = "2"

        with self.assertRaisesRegex(ReleaseWorkflowPolicyError, "run attempt input"):
            validate_engine_environment(environment)

    def test_workflow_definition_sha_must_match_release_sha(self) -> None:
        environment = valid_environment()
        environment["RELEASE_ENGINE_WORKFLOW_SHA"] = "01a17619e27b3fd42e7d3ba900e42428a065d28a"

        with self.assertRaisesRegex(ReleaseWorkflowPolicyError, "engine workflow SHA"):
            validate_engine_environment(environment)

    def test_post_approval_revalidation_rejects_changed_fingerprint(self) -> None:
        evidence = validate_engine_environment(valid_environment())

        with self.assertRaisesRegex(ReleaseWorkflowPolicyError, "approval boundary"):
            main(
                ["engine", "--expected-fingerprint", "0" * 64],
                environment=valid_environment(),
            )

        self.assertEqual(
            main(
                ["engine", "--expected-fingerprint", evidence.fingerprint()],
                environment=valid_environment(),
            ),
            0,
        )

    def test_engine_command_emits_validated_boundary_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_path = Path(temporary_directory) / "github-output.txt"
            result = main(
                ["engine", "--github-output", str(output_path)],
                environment=valid_environment(),
            )
            outputs = dict(line.split("=", 1) for line in output_path.read_text(encoding="utf-8").splitlines())

        self.assertEqual(result, 0)
        self.assertEqual(outputs["release_sha"], HEAD_SHA)
        self.assertIn(OPERATOR_WORKFLOW_PATH, outputs["operator_workflow_ref"])
        self.assertIn(ENGINE_WORKFLOW_PATH, outputs["engine_workflow_ref"])
        self.assertRegex(outputs["policy_fingerprint"], r"^[0-9a-f]{64}$")

    @patch("scripts.release_workflow_policy.urlopen")
    def test_engine_identity_is_loaded_from_github_oidc_claims(self, urlopen_mock: unittest.mock.Mock) -> None:
        environment = oidc_environment()
        token = encoded_oidc_token(environment)
        urlopen_mock.return_value = io.BytesIO(json.dumps({"value": token}).encode("utf-8"))

        self.assertEqual(main(["engine"], environment=environment), 0)
        request = urlopen_mock.call_args.args[0]
        self.assertIn(f"audience={OIDC_AUDIENCE}", request.full_url)
        self.assertEqual(request.headers["Authorization"], "Bearer ephemeral-request-token")

    @patch("scripts.release_workflow_policy.urlopen")
    def test_oidc_caller_claim_mismatch_fails_closed(self, urlopen_mock: unittest.mock.Mock) -> None:
        environment = oidc_environment()
        token = encoded_oidc_token(environment, workflow_ref="untrusted/workflow@refs/heads/main")
        urlopen_mock.return_value = io.BytesIO(json.dumps({"value": token}).encode("utf-8"))

        with self.assertRaisesRegex(ReleaseWorkflowPolicyError, "workflow_ref"):
            main(["engine"], environment=environment)

    @patch("scripts.release_workflow_policy.urlopen")
    def test_oidc_without_reusable_workflow_claim_fails_closed(self, urlopen_mock: unittest.mock.Mock) -> None:
        environment = oidc_environment()
        token = encoded_oidc_token(environment, job_workflow_ref="")
        urlopen_mock.return_value = io.BytesIO(json.dumps({"value": token}).encode("utf-8"))

        with self.assertRaisesRegex(ReleaseWorkflowPolicyError, "job_workflow_ref"):
            main(["engine"], environment=environment)


if __name__ == "__main__":
    unittest.main()
