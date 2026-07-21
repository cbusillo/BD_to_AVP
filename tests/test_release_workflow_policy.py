import base64
import io
import json
import tempfile
import unittest

from pathlib import Path
from unittest.mock import Mock, patch

from scripts.release_workflow_policy import (
    ENGINE_WORKFLOW_PATH,
    OPERATOR_WORKFLOWS,
    PRERELEASE_OPERATOR_WORKFLOW_PATH,
    PRERELEASE_ROUTE,
    PRERELEASE_WORKFLOW_NAME,
    REPOSITORY,
    REQUIRED_ACTOR,
    REQUIRED_EVENT,
    REQUIRED_REF,
    OIDC_AUDIENCE,
    ReleaseWorkflowPolicyError,
    STABLE_OPERATOR_WORKFLOW_PATH,
    STABLE_ROUTE,
    STABLE_WORKFLOW_NAME,
    main,
    validate_engine_environment,
    validate_release_metadata,
)


HEAD_SHA = "9e9a38c715dbbe5df97e6d3a8ba715731607db6a"


def valid_environment(workflow: str = STABLE_WORKFLOW_NAME) -> dict[str, str]:
    operator_ref = f"{REPOSITORY}/{OPERATOR_WORKFLOWS[workflow].path}@{REQUIRED_REF}"
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


def valid_metadata_environment(
    workflow: str = STABLE_WORKFLOW_NAME,
    *,
    release_tag: str | None = None,
    channel: str = "stable",
    prerelease: str = "false",
    make_latest: str = "true",
    publish_pypi: str = "true",
) -> dict[str, str]:
    if release_tag is None:
        release_tag = "v1.2.3" if channel == "stable" else f"v1.2.3-{channel}.1"
    return {
        "RELEASE_OPERATOR_WORKFLOW_REF": f"{REPOSITORY}/{OPERATOR_WORKFLOWS[workflow].path}@{REQUIRED_REF}",
        "RELEASE_TAG": release_tag,
        "RELEASE_CHANNEL": channel,
        "RELEASE_PRERELEASE": prerelease,
        "RELEASE_MAKE_LATEST": make_latest,
        "RELEASE_PUBLISH_PYPI": publish_pypi,
    }


def oidc_environment(workflow: str = STABLE_WORKFLOW_NAME) -> dict[str, str]:
    environment = valid_environment(workflow)
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
    def test_each_operator_and_engine_are_bound_to_route_specific_fingerprints(self) -> None:
        fingerprints: set[str] = set()
        for workflow, route, path in (
            (STABLE_WORKFLOW_NAME, STABLE_ROUTE, STABLE_OPERATOR_WORKFLOW_PATH),
            (PRERELEASE_WORKFLOW_NAME, PRERELEASE_ROUTE, PRERELEASE_OPERATOR_WORKFLOW_PATH),
        ):
            with self.subTest(workflow=workflow):
                evidence = validate_engine_environment(valid_environment(workflow))

                self.assertEqual(evidence.release_sha, HEAD_SHA)
                self.assertEqual(evidence.operator_route, route)
                self.assertEqual(evidence.operator_workflow_path, path)
                self.assertIn(path, evidence.operator_workflow_ref)
                self.assertIn(ENGINE_WORKFLOW_PATH, evidence.engine_workflow_ref)
                self.assertRegex(evidence.fingerprint(), r"^[0-9a-f]{64}$")
                fingerprints.add(evidence.fingerprint())

        self.assertEqual(len(fingerprints), 2)

    def test_direct_invocation_from_another_workflow_fails_closed(self) -> None:
        environment = valid_environment()
        environment["RELEASE_OPERATOR_WORKFLOW_REF"] = f"{REPOSITORY}/.github/workflows/untrusted.yml@{REQUIRED_REF}"
        environment["INPUT_OPERATOR_WORKFLOW_REF"] = environment["RELEASE_OPERATOR_WORKFLOW_REF"]

        with self.assertRaisesRegex(ReleaseWorkflowPolicyError, "operator workflow ref"):
            validate_engine_environment(environment)

    def test_retired_operator_workflow_name_and_path_fail_closed(self) -> None:
        environment = valid_environment()
        retired_ref = f"{REPOSITORY}/.github/workflows/release-from-main.yml@{REQUIRED_REF}"
        environment["RELEASE_OPERATOR_WORKFLOW_REF"] = retired_ref
        environment["INPUT_OPERATOR_WORKFLOW_REF"] = retired_ref

        with self.assertRaisesRegex(ReleaseWorkflowPolicyError, "approved operator workflow"):
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
        self.assertEqual(outputs["release_route"], STABLE_ROUTE)
        self.assertEqual(outputs["operator_workflow_path"], STABLE_OPERATOR_WORKFLOW_PATH)
        self.assertIn(STABLE_OPERATOR_WORKFLOW_PATH, outputs["operator_workflow_ref"])
        self.assertIn(ENGINE_WORKFLOW_PATH, outputs["engine_workflow_ref"])
        self.assertRegex(outputs["policy_fingerprint"], r"^[0-9a-f]{64}$")

    def test_stable_metadata_policy_accepts_only_stable_latest_and_pypi(self) -> None:
        metadata = validate_release_metadata(valid_metadata_environment())

        self.assertEqual(metadata.operator_route, STABLE_ROUTE)
        self.assertFalse(metadata.prerelease)
        self.assertTrue(metadata.make_latest)
        self.assertTrue(metadata.publish_pypi)

        rejected = (
            {"channel": "rc", "prerelease": "true", "make_latest": "false", "publish_pypi": "false"},
            {"channel": "stable", "prerelease": "false", "make_latest": "false", "publish_pypi": "true"},
            {"channel": "stable", "prerelease": "false", "make_latest": "true", "publish_pypi": "false"},
        )
        for overrides in rejected:
            with (
                self.subTest(overrides=overrides),
                self.assertRaisesRegex(ReleaseWorkflowPolicyError, "Stable operator requires"),
            ):
                validate_release_metadata(valid_metadata_environment(**overrides))

    def test_prerelease_metadata_policy_accepts_alpha_beta_and_rc_only(self) -> None:
        for channel in ("alpha", "beta", "rc"):
            with self.subTest(channel=channel):
                metadata = validate_release_metadata(
                    valid_metadata_environment(
                        PRERELEASE_WORKFLOW_NAME,
                        channel=channel,
                        prerelease="true",
                        make_latest="false",
                        publish_pypi="false",
                    )
                )
                self.assertEqual(metadata.operator_route, PRERELEASE_ROUTE)
                self.assertEqual(metadata.channel, channel)

        rejected = (
            {"channel": "stable", "prerelease": "false", "make_latest": "true", "publish_pypi": "true"},
            {"channel": "beta", "prerelease": "true", "make_latest": "true", "publish_pypi": "false"},
            {"channel": "rc", "prerelease": "true", "make_latest": "false", "publish_pypi": "true"},
        )
        for overrides in rejected:
            with (
                self.subTest(overrides=overrides),
                self.assertRaisesRegex(ReleaseWorkflowPolicyError, "Prerelease operator requires"),
            ):
                validate_release_metadata(valid_metadata_environment(PRERELEASE_WORKFLOW_NAME, **overrides))

    def test_metadata_command_writes_shared_validated_route_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            summary_path = Path(temporary_directory) / "summary.md"
            result = main(
                ["metadata", "--github-step-summary", str(summary_path)],
                environment=valid_metadata_environment(
                    PRERELEASE_WORKFLOW_NAME,
                    channel="beta",
                    prerelease="true",
                    make_latest="false",
                    publish_pypi="false",
                ),
            )
            summary = summary_path.read_text(encoding="utf-8")

        self.assertEqual(result, 0)
        self.assertIn("Validated release route", summary)
        self.assertIn("| Operator route | Prerelease |", summary)
        self.assertIn(f"`{PRERELEASE_OPERATOR_WORKFLOW_PATH}`", summary)
        self.assertIn("| Release tag | `v1.2.3-beta.1` |", summary)
        self.assertIn("| Committed release stage | `beta` |", summary)
        self.assertIn("| GitHub Latest | No |", summary)
        self.assertIn("| PyPI publication | No |", summary)

    def test_beta4_unfreeze_and_explicit_freeze_fail_closed(self) -> None:
        beta3_environment = valid_metadata_environment(
            PRERELEASE_WORKFLOW_NAME,
            release_tag="v0.3.0-beta.3",
            channel="beta",
            prerelease="true",
            make_latest="false",
            publish_pypi="false",
        )

        metadata = validate_release_metadata(beta3_environment)
        self.assertEqual(metadata.release_tag, "v0.3.0-beta.3")

        beta4_environment = valid_metadata_environment(
            PRERELEASE_WORKFLOW_NAME,
            release_tag="v0.3.0-beta.4",
            channel="beta",
            prerelease="true",
            make_latest="false",
            publish_pypi="false",
        )
        beta4_metadata = validate_release_metadata(beta4_environment)
        self.assertEqual(beta4_metadata.release_tag, "v0.3.0-beta.4")

        with tempfile.TemporaryDirectory() as temporary_directory:
            freezes_path = Path(temporary_directory) / "release-freezes.json"
            freezes_path.write_text(
                json.dumps(
                    {
                        "schema": "bd_to_avp.release_freezes",
                        "schema_version": 1,
                        "frozen_release_tags": {
                            "v0.3.0-beta.4": {
                                "issue": 316,
                                "reason": "test freeze",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ReleaseWorkflowPolicyError, r"frozen by issue #316"):
                validate_release_metadata(beta4_environment, freezes_path=freezes_path)

    @patch("scripts.release_workflow_policy.urlopen")
    def test_engine_identity_is_loaded_from_github_oidc_claims(self, urlopen_mock: Mock) -> None:
        environment = oidc_environment()
        token = encoded_oidc_token(environment)
        urlopen_mock.return_value = io.BytesIO(json.dumps({"value": token}).encode("utf-8"))

        self.assertEqual(main(["engine"], environment=environment), 0)
        request = urlopen_mock.call_args.args[0]
        self.assertIn(f"audience={OIDC_AUDIENCE}", request.full_url)
        self.assertEqual(request.headers["Authorization"], "Bearer ephemeral-request-token")

    @patch("scripts.release_workflow_policy.urlopen")
    def test_prerelease_caller_identity_is_loaded_from_github_oidc_claims(self, urlopen_mock: Mock) -> None:
        environment = oidc_environment(PRERELEASE_WORKFLOW_NAME)
        token = encoded_oidc_token(environment)
        urlopen_mock.return_value = io.BytesIO(json.dumps({"value": token}).encode("utf-8"))

        self.assertEqual(main(["engine"], environment=environment), 0)

    @patch("scripts.release_workflow_policy.urlopen")
    def test_oidc_caller_claim_mismatch_fails_closed(self, urlopen_mock: Mock) -> None:
        environment = oidc_environment()
        token = encoded_oidc_token(environment, workflow_ref="untrusted/workflow@refs/heads/main")
        urlopen_mock.return_value = io.BytesIO(json.dumps({"value": token}).encode("utf-8"))

        with self.assertRaisesRegex(ReleaseWorkflowPolicyError, "workflow_ref"):
            main(["engine"], environment=environment)

    @patch("scripts.release_workflow_policy.urlopen")
    def test_oidc_without_reusable_workflow_claim_fails_closed(self, urlopen_mock: Mock) -> None:
        environment = oidc_environment()
        token = encoded_oidc_token(environment, job_workflow_ref="")
        urlopen_mock.return_value = io.BytesIO(json.dumps({"value": token}).encode("utf-8"))

        with self.assertRaisesRegex(ReleaseWorkflowPolicyError, "job_workflow_ref"):
            main(["engine"], environment=environment)


if __name__ == "__main__":
    unittest.main()
