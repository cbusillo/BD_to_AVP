"""Security rules every GitHub workflow must satisfy.

These rules check parsed permissions, trust relationships and executed guards.
Display labels and runner upgrades must not fail them. Release-specific wiring
and ordering are checked where they enforce a trust boundary.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, ClassVar

import yaml

from scripts.production_identity import PRODUCTION_DEVELOPER_IDENTITY, PRODUCTION_TEAM_ID
from scripts.release_workflow_policy import (
    SIGNING_ENVIRONMENT,
    ENGINE_WORKFLOW_PATH,
    RECEIPT_ACTORS,
    REPOSITORY,
    REQUIRED_ACTOR,
    REQUIRED_REF,
    STABLE_OPERATOR_WORKFLOW_PATH,
    STABLE_ROUTE,
)
from scripts.tier3_clean_machine import RESETTABLE_VM_ENVIRONMENT_CLASS

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIRECTORY = Path(__file__).resolve().parents[1] / ".github" / "workflows"
UNTRUSTED_TRIGGERS = {"pull_request", "pull_request_target", "push", "schedule", "issue_comment"}
SECRET_REFERENCE = re.compile(r"secrets\.([A-Za-z0-9_]+)")
PINNED_ACTION = re.compile(r"^[^@]+@[0-9a-f]{40}$")


def load_workflows() -> dict[str, dict[str, Any]]:
    return {
        path.name: yaml.safe_load(path.read_text(encoding="utf-8"))
        for path in sorted([*WORKFLOW_DIRECTORY.glob("*.yml"), *WORKFLOW_DIRECTORY.glob("*.yaml")])
    }


def load_workflow(name: str) -> dict:
    # Keep expression and scalar values as strings for structural wiring checks.
    return yaml.load((WORKFLOW_DIRECTORY / name).read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def load_release_engine() -> dict:
    return load_workflow(Path(ENGINE_WORKFLOW_PATH).name)


def load_github_config() -> dict:
    return json.loads((WORKFLOW_DIRECTORY.parent / "github.json").read_text(encoding="utf-8"))


def triggers(workflow: dict[str, Any]) -> set[str]:
    declared = workflow.get(True, workflow.get("on"))  # PyYAML reads the bare key `on` as True
    if isinstance(declared, str):
        return {declared}
    return set(declared)


def secrets_read_by(job: dict[str, Any]) -> set[str]:
    return set(SECRET_REFERENCE.findall(json.dumps(job))) - {"GITHUB_TOKEN"}


def write_permissions(permissions: object) -> set[str]:
    if permissions == "write-all":
        return {"write-all"}
    if isinstance(permissions, dict):
        return {scope for scope, access in permissions.items() if access == "write"}
    return set()


def direct_needs(job: dict[str, Any]) -> set[str]:
    needs = job.get("needs", [])
    return {needs} if isinstance(needs, str) else set(needs)


def ancestors(jobs: dict[str, dict[str, Any]], name: str) -> set[str]:
    found: set[str] = set()
    pending = list(direct_needs(jobs[name]))
    while pending:
        current = pending.pop()
        if current not in found:
            found.add(current)
            pending.extend(direct_needs(jobs[current]))
    return found


class WorkflowSecurityPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflows = load_workflows()

    def jobs(self):
        for workflow_name, workflow in self.workflows.items():
            for job_name, job in workflow["jobs"].items():
                yield workflow_name, workflow, job_name, job

    def test_release_metadata_agrees_with_the_guarded_actor(self) -> None:
        config = json.loads((WORKFLOW_DIRECTORY.parent / "github.json").read_text(encoding="utf-8"))
        self.assertEqual(config["releaseOperations"]["releaseActor"], REQUIRED_ACTOR)

    def test_python_publication_requires_the_guarded_release_engine(self) -> None:
        publishers = 0
        for workflow_name, workflow in self.workflows.items():
            jobs = workflow["jobs"]
            for job_name, job in jobs.items():
                if not any(
                    str(step.get("uses", "")).startswith("pypa/gh-action-pypi-publish@")
                    for step in job.get("steps", [])
                ):
                    continue
                publishers += 1
                with self.subTest(workflow=workflow_name, job=job_name):
                    engines = [
                        jobs[parent]
                        for parent in ancestors(jobs, job_name)
                        if jobs[parent].get("uses") == f"./{ENGINE_WORKFLOW_PATH}"
                    ]
                    self.assertEqual(len(engines), 1)
                    self.assertNotIn("if", engines[0])
        self.assertGreater(publishers, 0)

    def test_external_actions_are_pinned_to_commit_shas(self) -> None:
        for workflow_name, _, job_name, job in self.jobs():
            uses = [job["uses"]] if "uses" in job else []
            uses += [step["uses"] for step in job.get("steps", []) if "uses" in step]
            for reference in uses:
                if reference.startswith("./"):
                    continue
                with self.subTest(workflow=workflow_name, job=job_name, uses=reference):
                    self.assertRegex(reference, PINNED_ACTION)

    def test_workflows_that_untrusted_events_can_start_hold_no_secrets_or_repository_write_access(self) -> None:
        for workflow_name, workflow in self.workflows.items():
            if not triggers(workflow) & UNTRUSTED_TRIGGERS:
                continue
            with self.subTest(workflow=workflow_name):
                self.assertEqual(set(SECRET_REFERENCE.findall(json.dumps(workflow))) - {"GITHUB_TOKEN"}, set())
                granted = write_permissions(workflow.get("permissions"))
                for job in workflow["jobs"].values():
                    granted |= write_permissions(job.get("permissions"))
                    self.assertNotIn("environment", job)
                self.assertLessEqual(granted, {"security-events", "issues"})

    def test_a_job_that_reads_a_secret_is_environment_scoped_and_cannot_write(self) -> None:
        readers = 0
        for workflow_name, _, job_name, job in self.jobs():
            if "uses" in job or not secrets_read_by(job):
                continue  # a caller only forwards secrets; the called workflow's jobs are checked here too
            readers += 1
            with self.subTest(workflow=workflow_name, job=job_name):
                self.assertIn("environment", job)
                self.assertEqual(write_permissions(job.get("permissions")), set())
        self.assertGreater(readers, 0)

    def test_every_job_declares_its_permissions_or_inherits_a_read_only_default(self) -> None:
        for workflow_name, workflow, job_name, job in self.jobs():
            with self.subTest(workflow=workflow_name, job=job_name):
                if "permissions" not in job:
                    self.assertIn("permissions", workflow)
                    self.assertEqual(write_permissions(workflow["permissions"]) - {"security-events", "issues"}, set())

    def test_no_release_engine_job_runs_beside_the_publication_gate(self) -> None:
        jobs = self.workflows["release-engine.yml"]["jobs"]
        gate = next(name for name in jobs if name.startswith("publish-release"))
        before = ancestors(jobs, gate)
        after = {name for name in jobs if gate in ancestors(jobs, name)}
        self.assertEqual(set(jobs) - before - after - {gate}, set())
        # Everything that signs or holds a secret happens before publication, never after it.
        for name in after:
            self.assertEqual(secrets_read_by(jobs[name]), set(), name)

    def test_secret_reading_release_jobs_wait_for_a_secret_free_preflight(self) -> None:
        jobs = self.workflows["release-engine.yml"]["jobs"]
        for name, job in jobs.items():
            if "uses" in job or not secrets_read_by(job):
                continue
            with self.subTest(job=name):
                self.assertTrue(any("uses" in jobs[parent] for parent in ancestors(jobs, name)))

    def test_a_job_that_runs_despite_failed_needs_checks_each_need_explicitly(self) -> None:
        for workflow_name, _, job_name, job in self.jobs():
            condition = str(job.get("if", ""))
            if "!cancelled()" not in condition and "always()" not in condition:
                continue
            # The check may be in the condition or in a step that fails the job.
            examined = condition + json.dumps(job.get("steps", []))
            for need in direct_needs(job):
                with self.subTest(workflow=workflow_name, job=job_name, need=need):
                    self.assertIn(f"needs.{need}.result", examined)

    def test_release_checkouts_use_the_dispatched_commit(self) -> None:
        checkouts = 0
        for workflow_name in ("release-engine.yml", "production-preflight-engine.yml"):
            for job_name, job in self.workflows[workflow_name]["jobs"].items():
                for step in job.get("steps", []):
                    if str(step.get("uses", "")).startswith("actions/checkout@"):
                        checkouts += 1
                        with self.subTest(workflow=workflow_name, job=job_name):
                            # The exact dispatched commit, never a branch that can move under the run,
                            # and no token left behind for later steps.
                            self.assertEqual(step.get("with", {}).get("ref"), "${{ github.sha }}")
                            self.assertIs(step["with"].get("persist-credentials"), False)
        self.assertGreater(checkouts, 0)


class ReleaseWorkflowSecurityContractTests(unittest.TestCase):
    def test_preflight_qualification_and_evidence_never_receive_secrets(self) -> None:
        for name in (
            "production-preflight.yml",
            "production-preflight-engine.yml",
            "update-test.yml",
        ):
            with self.subTest(workflow=name):
                workflow = load_workflow(name)
                self.assertEqual(secrets_read_by(workflow), set())
                for job in workflow["jobs"].values():
                    self.assertNotIn("environment", job)
                    self.assertNotIn("secrets", job)

    def test_signing_steps_bind_identity_to_the_production_constants(self) -> None:
        steps = load_release_engine()["jobs"]["package"]["steps"]
        for ident in ("signing-certificate", "package_artifact"):
            with self.subTest(step=ident):
                environment = next(step["env"] for step in steps if step.get("id") == ident)
                self.assertEqual(environment["TEAM_ID"], "${{ secrets.TEAM_ID }}")
                self.assertEqual(environment["PRODUCTION_TEAM_ID"], PRODUCTION_TEAM_ID)
                self.assertEqual(environment["PRODUCTION_DEV_ID"], PRODUCTION_DEVELOPER_IDENTITY)

    def test_clean_machine_commands_select_the_ephemeral_runner_contract(self) -> None:
        steps = load_workflow("update-test.yml")["jobs"]["update"]["steps"]
        step = next(step for step in steps if step.get("id") == "update-test")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = root / "calls"
            uv = root / "uv"
            uv.write_text('#!/bin/sh\nprintf "%s\\n" "$@" >> "$CALLS"\n', encoding="utf-8")
            uv.chmod(0o700)
            result = subprocess.run(
                ["/bin/bash", "-c", step["run"]],
                cwd=root,
                env={
                    "PATH": f"{root}:/usr/bin:/bin",
                    "CALLS": str(calls),
                    "RUNNER_TEMP": str(root),
                    "GITHUB_RUN_ID": "100",
                    "GITHUB_RUN_ATTEMPT": "1",
                    "GITHUB_STEP_SUMMARY": str(root / "summary"),
                    "CANDIDATE_TAG": "v1.1.0",
                    "CANDIDATE_DMG": "candidate.dmg",
                    "PRIOR_TAG": "v1.0.0",
                    "PRIOR_DMG": "prior.dmg",
                    "ROUTE": "stable",
                },
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            arguments = calls.read_text(encoding="utf-8").splitlines()
            self.assertIn("scripts.tier3_clean_machine", arguments)
            self.assertEqual(arguments.count("--environment-class"), 1)
            self.assertEqual(arguments[arguments.index("--environment-class") + 1], RESETTABLE_VM_ENVIRONMENT_CLASS)
            self.assertEqual(step["env"]["BD_TO_AVP_TIER3_RUNNER_ENVIRONMENT"], "${{ runner.environment }}")

    def test_release_independent_production_preflight_is_secret_free_and_non_publishing(self) -> None:
        manual = load_workflow("production-preflight.yml")
        shared = load_workflow("production-preflight-engine.yml")
        release_engine = load_release_engine()
        manual_job = manual["jobs"]["preflight"]
        shared_call = shared["on"]["workflow_call"]
        shared_job = shared["jobs"]["preflight"]
        release_job = release_engine["jobs"]["pre-signing-package"]

        self.assertEqual(set(manual["on"]), {"workflow_dispatch"})
        source_input = manual["on"]["workflow_dispatch"]["inputs"]["source_sha"]
        self.assertEqual(source_input["required"], "true")
        self.assertEqual(source_input["type"], "string")
        self.assertEqual(manual["permissions"], {})
        self.assertEqual(manual_job["uses"], "./.github/workflows/production-preflight-engine.yml")
        self.assertEqual(manual_job["with"]["source_sha"], "${{ inputs.source_sha }}")
        self.assertEqual(manual_job["permissions"], {"contents": "read"})
        self.assertNotIn("environment", manual_job)
        self.assertNotIn("secrets", manual_job)

        self.assertEqual(set(shared["on"]), {"workflow_call"})
        self.assertEqual(set(shared_call["inputs"]), {"source_sha", "support_diagnostics_endpoint"})
        self.assertNotIn("secrets", shared_call)
        self.assertEqual(shared["permissions"], {"contents": "read"})
        self.assertEqual(shared_job["permissions"], {"contents": "read"})
        self.assertNotIn("environment", shared_job)
        self.assertNotIn("secrets", shared_job)
        checkout = next(step for step in shared_job["steps"] if step.get("id") == "checkout")
        self.assertEqual(checkout["with"]["ref"], "${{ github.sha }}")
        self.assertNotEqual(checkout["with"]["ref"], "${{ inputs.source_sha }}")

        self.assertEqual(release_job["uses"], manual_job["uses"])
        self.assertEqual(release_job["with"]["source_sha"], "${{ inputs.release_sha }}")
        self.assertEqual(release_job["permissions"], {"contents": "read"})
        self.assertEqual(set(release_job["needs"]), {"pre-signing-policy"})
        self.assertNotIn("environment", release_job)
        self.assertNotIn("secrets", release_job)

        step_ids = [step.get("id") for step in shared_job["steps"]]
        required_order = [
            "input",
            "checkout",
            "source",
            "runner",
            "xcodegen",
            "uv",
            "dependencies",
            "package",
            "smoke",
            "installed-ui",
            "evidence",
        ]
        self.assertEqual([ident for ident in step_ids if ident in required_order], required_order)

        success_upload = next(step for step in shared_job["steps"] if step.get("id") == "success-evidence")
        failure_upload = next(step for step in shared_job["steps"] if step.get("id") == "failure-diagnostics")
        self.assertNotIn(".dmg", success_upload["with"]["path"])
        self.assertNotIn(".app", success_upload["with"]["path"])
        self.assertIn("${{ inputs.source_sha }}", success_upload["with"]["name"])
        self.assertIn("${{ github.run_id }}", success_upload["with"]["name"])
        self.assertIn("${{ github.run_id }}", failure_upload["with"]["name"])

    def test_package_preserves_dmg_validation_without_write_token(self) -> None:
        workflow = load_release_engine()
        package = workflow["jobs"]["package"]
        next(step for step in package["steps"] if step.get("id") == "signing-certificate")
        next(step for step in package["steps"] if step.get("id") == "signing-cleanup")
        package_step = next(step for step in package["steps"] if step.get("id") == "package_artifact")

        self.assertEqual(
            set(package["needs"]),
            {"policy", "prepare", "pre-signing-package"},
        )
        self.assertEqual(package["environment"], "macos-signing")
        self.assertEqual(package["permissions"]["contents"], "read")
        self.assertIn("dmg_sha256", package["outputs"])
        self.assertIn("dmg_size", package["outputs"])
        self.assertEqual(package_step["env"]["DMG_NAME"], "${{ needs.prepare.outputs.dmg_name }}")
        next(step for step in package["steps"] if step.get("id") == "notary-credentials")

    def test_stable_pypi_publish_uses_oidc_trusted_publishing(self) -> None:
        operator = load_workflow("briefcase.yml")
        prerelease = load_workflow("prerelease.yml")
        workflow = load_release_engine()
        build = workflow["jobs"]["build-python"]
        publish = operator["jobs"]["publish-pypi"]
        self.assertEqual(publish["needs"], "release")
        self.assertIn("needs.release.result == 'success'", publish["if"])
        self.assertIn("needs.release.outputs.publish_pypi == 'true'", publish["if"])
        self.assertEqual(publish["environment"]["name"], "pypi")
        self.assertEqual(publish["permissions"]["actions"], "read")
        self.assertEqual(publish["permissions"]["id-token"], "write")
        self.assertEqual(secrets_read_by(publish), set())
        self.assertEqual(
            next(step for step in publish["steps"] if "pypa/gh-action-pypi-publish@" in step.get("uses", ""))["with"][
                "packages-dir"
            ],
            "python-distributions/dist",
        )
        self.assertIn("artifact_digest", build["outputs"])
        self.assertIn("artifact_id", build["outputs"])
        steps = publish["steps"]
        transfer_index = next(index for index, step in enumerate(steps) if step.get("id") == "python-transfer")
        freshness_index = next(index for index, step in enumerate(steps) if step.get("id") == "python-freshness")
        publisher_index = next(
            index for index, step in enumerate(steps) if "pypa/gh-action-pypi-publish@" in step.get("uses", "")
        )
        self.assertLess(transfer_index, freshness_index)
        self.assertEqual(freshness_index + 1, publisher_index)
        download = next(step for step in publish["steps"] if "actions/download-artifact@" in step.get("uses", ""))
        self.assertEqual(download["with"]["artifact-ids"], "${{ needs.release.outputs.python_artifact_id }}")
        self.assertEqual(download["with"]["merge-multiple"], "true")
        self.assertNotIn("publish-pypi", workflow["jobs"])
        self.assertNotIn("publish-pypi", prerelease["jobs"])


class SigningCredentialMainGuardTests(unittest.TestCase):
    """Run each step that receives a signing credential against a protected main that has moved."""

    # Commands a signing step reaches only after its guard. Each stub records that it ran and fails.
    STUBBED_COMMANDS = ("security", "xcrun", "codesign", "openssl", "uv", "ditto", "jq")
    SECRET_VALUES: ClassVar[dict[str, str]] = {"TEAM_ID": PRODUCTION_TEAM_ID, "DEV_ID": PRODUCTION_DEVELOPER_IDENTITY}

    @staticmethod
    def git(root: Path, *arguments: str) -> str:
        return subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *arguments],
            cwd=root,
            env={"PATH": "/usr/bin:/bin", "HOME": str(root), "GIT_CONFIG_NOSYSTEM": "1"},
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    def signing_steps(self) -> list[tuple[str, dict[str, Any]]]:
        jobs = load_workflows()[Path(ENGINE_WORKFLOW_PATH).name]["jobs"]
        steps = [
            (f"{job_name}: {step.get('name')}", step)
            for job_name, job in jobs.items()
            if job.get("environment") == SIGNING_ENVIRONMENT
            for step in job.get("steps", [])
            if "run" in step and secrets_read_by(step)
        ]
        self.assertTrue(steps)
        return steps

    def run_step(self, step: dict[str, Any], *, main_moved: bool) -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            origin, checkout, tools = root / "origin.git", root / "checkout", root / "tools"
            calls = root / "calls.log"
            for path in (origin, checkout, tools):
                path.mkdir()
            self.git(origin, "init", "-q", "--bare")
            self.git(checkout, "init", "-q", "-b", "main")
            self.git(checkout, "commit", "--allow-empty", "-qm", "dispatched")
            dispatched_sha = self.git(checkout, "rev-parse", "HEAD")
            self.git(checkout, "remote", "add", "origin", str(origin))
            self.git(checkout, "push", "-q", "origin", "HEAD:refs/heads/main")
            self.git(checkout, "fetch", "-q", "origin")
            if main_moved:
                # Another merge lands from elsewhere; only a fresh fetch can see it.
                other = root / "other"
                self.git(root, "clone", "-q", "-b", "main", str(origin), str(other))
                self.git(other, "commit", "--allow-empty", "-qm", "merged after approval")
                self.git(other, "push", "-q", "origin", "HEAD:refs/heads/main")
            for command in self.STUBBED_COMMANDS:
                stub = tools / command
                stub.write_text(f'#!/bin/sh\necho {command} >> "{calls}"\nexit 1\n', encoding="utf-8")
                stub.chmod(0o700)
            environment = {
                name: self.SECRET_VALUES.get(name, "placeholder") if "${{" in str(value) else str(value)
                for name, value in step.get("env", {}).items()
            }
            completed = subprocess.run(
                ["/bin/bash", "-c", step["run"]],
                cwd=checkout,
                env={
                    "PATH": f"{tools}:/usr/bin:/bin",
                    "HOME": str(root),
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "GITHUB_SHA": dispatched_sha,
                    "GITHUB_RUN_ID": "100",
                    "GITHUB_ENV": str(root / "github-env"),
                    "RUNNER_TEMP": str(root),
                    "KEYCHAIN_PATH": str(root / "release.keychain-db"),
                    "BUILD_KEYCHAIN_PASSWORD": "placeholder",
                    **environment,
                },
                capture_output=True,
                check=False,
            )
            return completed.returncode, calls.read_text(encoding="utf-8") if calls.exists() else ""

    def test_a_signing_step_proceeds_while_main_is_the_dispatched_commit(self) -> None:
        for name, step in self.signing_steps():
            with self.subTest(step=name):
                _, calls = self.run_step(step, main_moved=False)
                self.assertNotEqual(calls, "")

    def test_a_signing_step_stops_before_using_credentials_once_main_moves(self) -> None:
        for name, step in self.signing_steps():
            with self.subTest(step=name):
                returncode, calls = self.run_step(step, main_moved=True)
                self.assertNotEqual(returncode, 0)
                self.assertEqual(calls, "")


class ReleaseOperatorGuardTests(unittest.TestCase):
    """Run the release engine's operator guard instead of reading it."""

    APPROVED: ClassVar[dict[str, str]] = {
        "ACTUAL_REPOSITORY": "cbusillo/BD_to_AVP",
        "ACTUAL_EVENT_NAME": "workflow_dispatch",
        "ACTUAL_REF": "refs/heads/main",
        "ACTUAL_SHA": "a" * 40,
        "ACTUAL_OPERATOR_WORKFLOW_REF": "cbusillo/BD_to_AVP/.github/workflows/prerelease.yml@refs/heads/main",
        "ACTUAL_OPERATOR_WORKFLOW_SHA": "a" * 40,
        "ACTUAL_RUN_ID": "100",
        "ACTUAL_RUN_ATTEMPT": "1",
        "ACTUAL_ACTOR": REQUIRED_ACTOR,
        "ACTUAL_TRIGGERING_ACTOR": REQUIRED_ACTOR,
    }
    FORWARDED: ClassVar[dict[str, str]] = {
        "INPUT_RELEASE_SHA": "ACTUAL_SHA",
        "INPUT_OPERATOR_WORKFLOW_REF": "ACTUAL_OPERATOR_WORKFLOW_REF",
        "INPUT_OPERATOR_WORKFLOW_SHA": "ACTUAL_OPERATOR_WORKFLOW_SHA",
        "INPUT_OPERATOR_RUN_ID": "ACTUAL_RUN_ID",
        "INPUT_OPERATOR_RUN_ATTEMPT": "ACTUAL_RUN_ATTEMPT",
        "INPUT_OPERATOR_ACTOR": "ACTUAL_ACTOR",
        "INPUT_OPERATOR_TRIGGERING_ACTOR": "ACTUAL_TRIGGERING_ACTOR",
    }

    def setUp(self) -> None:
        jobs = load_workflows()["release-engine.yml"]["jobs"]
        guards = [
            step
            for job in jobs.values()
            for step in job.get("steps", [])
            if "ACTUAL_TRIGGERING_ACTOR" in step.get("env", {})
        ]
        self.assertEqual(len(guards), 1)
        self.guard = guards[0]
        # The guard must be the first thing the engine does, in a job nothing else precedes.
        first_job = next(iter(jobs.values()))
        self.assertEqual(direct_needs(first_job), set())
        self.assertIs(first_job["steps"][0], self.guard)

    def run_guard(self, **overrides: str) -> int:
        environment = {**self.APPROVED, **overrides}
        for forwarded, actual in self.FORWARDED.items():
            environment.setdefault(forwarded, self.APPROVED[actual])
        self.assertEqual(set(environment), set(self.guard["env"]))
        completed = subprocess.run(
            ["/bin/bash", "-c", self.guard["run"]],
            env={"PATH": "/usr/bin:/bin", **environment},
            capture_output=True,
            check=False,
        )
        return completed.returncode

    def test_the_approved_operator_context_is_accepted(self) -> None:
        self.assertEqual(self.run_guard(), 0)
        stable = self.APPROVED["ACTUAL_OPERATOR_WORKFLOW_REF"].replace("prerelease.yml", "briefcase.yml")
        self.assertEqual(
            self.run_guard(ACTUAL_OPERATOR_WORKFLOW_REF=stable, INPUT_OPERATOR_WORKFLOW_REF=stable),
            0,
        )

    def test_every_deviation_from_the_approved_context_is_rejected(self) -> None:
        deviations = {
            "another repository": {"ACTUAL_REPOSITORY": "someone/BD_to_AVP"},
            "a pull request event": {"ACTUAL_EVENT_NAME": "pull_request"},
            "a branch other than main": {"ACTUAL_REF": "refs/heads/feature"},
            "an unapproved operator workflow": {
                "ACTUAL_OPERATOR_WORKFLOW_REF": "cbusillo/BD_to_AVP/.github/workflows/ci.yml@refs/heads/main",
                "INPUT_OPERATOR_WORKFLOW_REF": "cbusillo/BD_to_AVP/.github/workflows/ci.yml@refs/heads/main",
            },
            "an operator workflow from another commit": {"ACTUAL_OPERATOR_WORKFLOW_SHA": "b" * 40},
            "a human actor": {"ACTUAL_ACTOR": "cbusillo", "INPUT_OPERATOR_ACTOR": "cbusillo"},
            "a human re-run": {
                "ACTUAL_TRIGGERING_ACTOR": "cbusillo",
                "INPUT_OPERATOR_TRIGGERING_ACTOR": "cbusillo",
            },
            "a retired automation actor": {
                "ACTUAL_ACTOR": "shiny-code-bot",
                "INPUT_OPERATOR_ACTOR": "shiny-code-bot",
            },
            "a retired automation re-run": {
                "ACTUAL_TRIGGERING_ACTOR": "shiny-code-bot",
                "INPUT_OPERATOR_TRIGGERING_ACTOR": "shiny-code-bot",
            },
            "a release SHA other than the dispatched one": {"INPUT_RELEASE_SHA": "c" * 40},
            "a forged operator run id": {"INPUT_OPERATOR_RUN_ID": "999"},
            "a forged operator run attempt": {"INPUT_OPERATOR_RUN_ATTEMPT": "2"},
            "a forged operator actor": {"INPUT_OPERATOR_ACTOR": "cbusillo"},
        }
        for description, overrides in deviations.items():
            with self.subTest(deviation=description):
                self.assertNotEqual(self.run_guard(**overrides), 0)


class StablePublicationGuardTests(unittest.TestCase):
    """Exercise the final Python-publication guard with read-only GitHub responses."""

    def run_guard(self, **overrides: str) -> int:
        jobs = load_workflows()[Path(STABLE_OPERATOR_WORKFLOW_PATH).name]["jobs"]
        guard = next(
            step
            for job in jobs.values()
            for step in job.get("steps", [])
            if "RELEASE_POLICY_FINGERPRINT" in step.get("env", {})
        )
        source_sha = "a" * 40
        environment = {
            "GITHUB_REPOSITORY": REPOSITORY,
            "GITHUB_REF": REQUIRED_REF,
            "GITHUB_SHA": source_sha,
            "GITHUB_ACTOR": REQUIRED_ACTOR,
            "GITHUB_TRIGGERING_ACTOR": REQUIRED_ACTOR,
            "ENGINE_WORKFLOW_REF": f"{REPOSITORY}/{ENGINE_WORKFLOW_PATH}@{REQUIRED_REF}",
            "ENGINE_WORKFLOW_SHA": source_sha,
            "RELEASE_ROUTE": STABLE_ROUTE,
            "OPERATOR_WORKFLOW_REF": f"{REPOSITORY}/{STABLE_OPERATOR_WORKFLOW_PATH}@{REQUIRED_REF}",
            "OPERATOR_WORKFLOW_PATH": STABLE_OPERATOR_WORKFLOW_PATH,
            "OPERATOR_WORKFLOW_SHA": source_sha,
            "RELEASE_POLICY_FINGERPRINT": "b" * 64,
            "RELEASE_SHA": source_sha,
            "PYTHON_ARTIFACT_DIGEST": "c" * 64,
            "PYTHON_ARTIFACT_ID": "123",
            **overrides,
        }
        with tempfile.TemporaryDirectory() as directory:
            gh = Path(directory) / "gh"
            gh.write_text(
                '#!/bin/sh\ncase "$*" in\n'
                '  *"/git/ref/heads/main"*) printf "%s\\n" "$GITHUB_SHA" ;;\n'
                '  *"/actions/artifacts/"*) printf "sha256:%s\\n" "$PYTHON_ARTIFACT_DIGEST" ;;\n'
                "  *) exit 99 ;;\nesac\n",
                encoding="utf-8",
            )
            gh.chmod(0o700)
            return subprocess.run(
                ["/bin/bash", "-c", guard["run"]],
                env={"PATH": f"{directory}:/usr/bin:/bin", **environment},
                capture_output=True,
                check=False,
            ).returncode

    def test_current_automation_can_reach_python_publication(self) -> None:
        self.assertEqual(self.run_guard(), 0)

    def test_human_retired_and_untrusted_actors_cannot_publish(self) -> None:
        for field in ("GITHUB_ACTOR", "GITHUB_TRIGGERING_ACTOR"):
            for actor in {"cbusillo", "untrusted-app[bot]", *RECEIPT_ACTORS} - {REQUIRED_ACTOR}:
                with self.subTest(field=field, actor=actor):
                    self.assertNotEqual(self.run_guard(**{field: actor}), 0)


class ToolchainAgreementTests(unittest.TestCase):
    """One Xcode toolchain builds everything that ships; the tests do not care which one."""

    def test_workflows_and_the_bundled_decoder_agree_on_one_xcode_toolchain(self) -> None:
        repository = WORKFLOW_DIRECTORY.parents[1]
        provenance = json.loads(
            (repository / "bd_to_avp/resources/notices/edge264-mvc-build.json").read_text(encoding="utf-8")
        )
        declared = {("bundled edge264", provenance["xcode_version"], provenance["xcode_build_version"])}
        for workflow_name, workflow in load_workflows().items():
            environment = workflow.get("env") or {}
            if "XCODE_VERSION" in environment:
                declared.add(
                    (workflow_name, str(environment["XCODE_VERSION"]), str(environment["XCODE_BUILD_VERSION"]))
                )
            for selected in set(re.findall(r"/Applications/Xcode_([0-9.]+)\.app", json.dumps(workflow))):
                self.assertEqual(selected, provenance["xcode_version"], workflow_name)
        self.assertGreater(len(declared), 1)
        self.assertEqual(len({(version, build) for _, version, build in declared}), 1, sorted(declared))

    def test_no_job_runs_on_a_self_hosted_runner(self) -> None:
        for workflow_name, workflow in load_workflows().items():
            for job_name, job in workflow["jobs"].items():
                with self.subTest(workflow=workflow_name, job=job_name):
                    self.assertNotIn("self-hosted", json.dumps(job.get("runs-on", "")))


class ReleasePublicationFreshnessTests(unittest.TestCase):
    def test_publication_leaves_a_draft_when_main_moves(self) -> None:
        script = load_release_engine()["jobs"]["publish-release"]["steps"][0]["run"]
        for main_moved in (False, True):
            with self.subTest(main_moved=main_moved), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                tools = root / "tools"
                tools.mkdir()
                notes, receipt = b"Candidate notes\n", b"fixture receipt\n"
                sha, tag, dmg = "a" * 40, "v1.2.3", "candidate.dmg"
                response = {
                    "body": notes.decode(),
                    "target_commitish": sha,
                    "draft": True,
                    "immutable": True,
                    "prerelease": False,
                    "name": tag,
                    "published_at": "2026-01-01T00:00:00Z",
                    "assets": [
                        {"id": 1, "name": "release-receipt.json"},
                        {
                            "name": dmg,
                            "browser_download_url": f"https://github.com/fixture/project/releases/download/{tag}/{dmg}",
                        },
                        {"name": "checksum.txt"},
                        {"name": "appcast.xml"},
                    ],
                }
                (root / "response.json").write_text(json.dumps(response), encoding="utf-8")
                (root / "fixture-receipt").write_bytes(receipt)
                gh = tools / "gh"
                gh.write_text(
                    f"#!{sys.executable}\n"
                    "import json, os, pathlib, sys\n"
                    "root = pathlib.Path.cwd()\n"
                    "args = sys.argv[1:]\n"
                    "if 'PATCH' in args:\n"
                    "    (root / 'published').touch()\n"
                    "elif any('/git/ref/heads/main' in arg for arg in args):\n"
                    "    print(os.environ['FIXTURE_MAIN_SHA'])\n"
                    "elif any('/git/ref/tags/' in arg for arg in args):\n"
                    "    print(os.environ['GITHUB_SHA'])\n"
                    "elif any('/releases/assets/' in arg for arg in args):\n"
                    "    sys.stdout.buffer.write((root / 'fixture-receipt').read_bytes())\n"
                    "else:\n"
                    "    response = json.loads((root / 'response.json').read_text())\n"
                    "    response['draft'] = not (root / 'published').exists()\n"
                    "    print(json.dumps(response))\n",
                    encoding="utf-8",
                )
                gh.chmod(0o700)
                curl = tools / "curl"
                curl.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                curl.chmod(0o700)
                completed = subprocess.run(
                    ["/bin/bash", "-c", script],
                    cwd=root,
                    env={
                        "PATH": f"{tools}:{os.environ['PATH']}",
                        "GITHUB_REPOSITORY": "fixture/project",
                        "GITHUB_SHA": sha,
                        "FIXTURE_MAIN_SHA": "b" * 40 if main_moved else sha,
                        "EXPECTED_RELEASE_NOTES_SHA256": hashlib.sha256(notes).hexdigest(),
                        "EXPECTED_RECEIPT_FILE_SHA256": hashlib.sha256(receipt).hexdigest(),
                        "EXPECTED_RECEIPT_ASSET_ID": "1",
                        "EXPECTED_DMG_NAME": dmg,
                        "RELEASE_ID": "123",
                        "RELEASE_NAME": tag,
                        "RELEASE_TAG": tag,
                        "PRERELEASE": "false",
                        "MAKE_LATEST": "true",
                    },
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=False,
                )
                self.assertEqual((root / "published").exists(), not main_moved, completed.stderr)
                if main_moved:
                    self.assertNotEqual(completed.returncode, 0, completed.stderr)
                else:
                    self.assertEqual(completed.returncode, 0, completed.stderr)


class ReleaseWorkflowWiringTests(unittest.TestCase):
    def test_ci_checkouts_leave_no_credentials(self) -> None:
        workflow = load_workflow("ci.yml")
        checkouts = [
            step
            for job in workflow["jobs"].values()
            for step in job["steps"]
            if step.get("uses", "").startswith("actions/checkout@")
        ]

        # Untrusted CI steps must not inherit checkout credentials.
        self.assertTrue(checkouts)
        for checkout in checkouts:
            self.assertRegex(checkout["uses"], r"^actions/checkout@[0-9a-f]{40}$")
            self.assertEqual(checkout["with"]["persist-credentials"], "false")

    def test_release_is_manual_only_and_packages_github_sha_from_main(self) -> None:
        operators = {
            "Stable": load_workflow("briefcase.yml"),
            "Prerelease": load_workflow("prerelease.yml"),
        }
        workflow = load_release_engine()
        load_workflow("production-preflight-engine.yml")
        prepare = workflow["jobs"]["prepare"]
        pre_signing = workflow["jobs"]["pre-signing-package"]
        package = workflow["jobs"]["package"]

        self.assertEqual({operator["name"] for operator in operators.values()}, set(operators))
        for name, operator in operators.items():
            with self.subTest(operator=name):
                self.assertEqual(set(operator["on"]), {"workflow_dispatch"})
                self.assertTrue(operator["concurrency"]["group"])
                self.assertEqual(operator["concurrency"]["group"], operators["Stable"]["concurrency"]["group"])
                self.assertEqual(operator["concurrency"]["cancel-in-progress"], "false")
        self.assertEqual(set(workflow["on"]), {"workflow_call"})
        self.assertEqual(workflow["env"]["GH_REPO"], "${{ github.repository }}")
        self.assertNotIn("concurrency", workflow)
        checkout = next(step for step in prepare["steps"] if step.get("uses", "").startswith("actions/checkout@"))
        self.assertEqual(checkout["with"]["ref"], "${{ github.sha }}")
        self.assertEqual(checkout["with"]["persist-credentials"], "false")
        self.assertEqual(pre_signing["uses"], "./.github/workflows/production-preflight-engine.yml")
        self.assertEqual(pre_signing["with"]["source_sha"], "${{ inputs.release_sha }}")
        self.assertIn("pre-signing-package", package["needs"])

    def test_reusable_engine_rejects_direct_invocation_and_policy_bypass(self) -> None:
        stable = load_workflow("briefcase.yml")
        prerelease = load_workflow("prerelease.yml")
        workflow = load_release_engine()
        release = stable["jobs"]["release"]
        for field in ("uses", "with", "permissions", "secrets"):
            self.assertEqual(prerelease["jobs"]["release"][field], release[field], field)
        call = workflow["on"]["workflow_call"]
        configured_environments = load_github_config()["releaseEnvironments"]
        macos_secret_names = set(configured_environments["macos-signing"]["secrets"])
        sparkle_secret_names = set(configured_environments["sparkle-release"]["secrets"])
        declared_secret_names = set(call["secrets"])
        referenced_secret_names = secrets_read_by(workflow)
        policy = workflow["jobs"]["policy"]
        entry_guard = next(step for step in policy["steps"] if "INPUT_RELEASE_SHA" in step.get("env", {}))
        policy_checkout = next(step for step in policy["steps"] if step.get("uses", "").startswith("actions/checkout@"))
        policy_step = next(step for step in policy["steps"] if step.get("id") == "policy")

        self.assertEqual(release["uses"], "./.github/workflows/release-engine.yml")
        self.assertEqual(declared_secret_names, macos_secret_names | sparkle_secret_names)
        self.assertEqual(declared_secret_names, referenced_secret_names)
        self.assertTrue(all(definition["required"] == "false" for definition in call["secrets"].values()))
        self.assertEqual(
            release["secrets"],
            {name: f"${{{{ secrets.{name} }}}}" for name in declared_secret_names},
        )
        for operator in (stable, prerelease):
            self.assertIsInstance(operator["jobs"]["release"]["secrets"], dict)
        self.assertEqual(
            set(
                re.findall(
                    r"\$\{\{\s*secrets\.([A-Z0-9_]+)\s*\}\}",
                    json.dumps(workflow["jobs"]["package"]),
                )
            ),
            macos_secret_names,
        )
        self.assertEqual(
            set(
                re.findall(
                    r"\$\{\{\s*secrets\.([A-Z0-9_]+)\s*\}\}",
                    json.dumps(workflow["jobs"]["publish-appcast"]),
                )
            ),
            sparkle_secret_names,
        )
        self.assertLessEqual(
            write_permissions(release["permissions"]),
            write_permissions(
                {
                    "actions": "read",
                    "attestations": "write",
                    "contents": "write",
                    "id-token": "write",
                    "pages": "write",
                }
            ),
        )
        self.assertLessEqual(
            write_permissions(workflow["permissions"]), write_permissions({"actions": "read", "contents": "read"})
        )
        self.assertEqual(release["with"]["release_sha"], "${{ github.sha }}")
        self.assertEqual(release["with"]["operator_workflow_ref"], "${{ github.workflow_ref }}")
        self.assertEqual(release["with"]["operator_workflow_sha"], "${{ github.workflow_sha }}")
        self.assertEqual(release["with"]["operator_actor"], "${{ github.actor }}")
        self.assertEqual(release["with"]["operator_triggering_actor"], "${{ github.triggering_actor }}")
        self.assertLessEqual(
            {
                "release_sha",
                "operator_workflow_ref",
                "operator_workflow_sha",
                "operator_run_id",
                "operator_run_attempt",
                "operator_actor",
                "operator_triggering_actor",
            },
            {name for name, definition in call["inputs"].items() if definition.get("required") == "true"},
        )
        self.assertFalse({"route", "mode", "stage", "prerelease", "publish_pypi"} & set(call["inputs"]))
        for operator in (stable, prerelease):
            dispatch_inputs = operator["on"]["workflow_dispatch"]["inputs"]
            self.assertFalse({"route", "mode", "stage", "prerelease", "publish_pypi"} & set(dispatch_inputs))
        self.assertLessEqual(
            write_permissions(policy["permissions"]), write_permissions({"contents": "read", "id-token": "write"})
        )
        self.assertNotIn("uses", entry_guard)
        self.assertEqual(policy_checkout["with"]["ref"], "${{ github.sha }}")
        self.assertEqual(policy_checkout["with"]["persist-credentials"], "false")
        self.assertEqual(policy_step["env"]["RELEASE_OPERATOR_WORKFLOW_REF"], "${{ github.workflow_ref }}")
        self.assertNotIn("RELEASE_ENGINE_WORKFLOW_REF", policy_step["env"])
        self.assertNotIn("RELEASE_ENGINE_WORKFLOW_SHA", policy_step["env"])
        self.assertIn("release_route", policy["outputs"])
        self.assertIn("operator_workflow_path", policy["outputs"])
        self.assertIn("policy", direct_needs(workflow["jobs"]["prepare"]))
        self.assertLessEqual(
            {"policy", "prepare", "pre-signing-package"},
            direct_needs(workflow["jobs"]["package"]),
        )

    def test_release_metadata_outputs_are_wired_to_route_validation(self) -> None:
        workflow = load_release_engine()
        prepare = workflow["jobs"]["prepare"]
        next(step for step in prepare["steps"] if step.get("id") == "release_history")
        route_validation = next(step for step in prepare["steps"] if step.get("id") == "route-validation")

        self.assertLessEqual(
            write_permissions(prepare["permissions"]), write_permissions({"actions": "read", "contents": "write"})
        )
        self.assertEqual(
            route_validation["env"]["RELEASE_OPERATOR_WORKFLOW_REF"],
            "${{ needs.policy.outputs.operator_workflow_ref }}",
        )
        self.assertEqual(route_validation["env"]["RELEASE_TAG"], "${{ steps.metadata.outputs.release_tag }}")
        self.assertEqual(route_validation["env"]["RELEASE_CHANNEL"], "${{ steps.metadata.outputs.channel }}")
        self.assertNotIn("RELEASE_ROUTE", route_validation["env"])
        self.assertIn("public_version", prepare["outputs"])
        self.assertIn("dmg_name", prepare["outputs"])
        self.assertIn("publish_pypi", prepare["outputs"])
        self.assertIn("previous_release_tag", prepare["outputs"])
        self.assertIn("base_snapshot_tag", prepare["outputs"])

    def test_declared_diagnostics_bindings_use_the_release_variable(self) -> None:
        workflow = load_release_engine()
        inspections = [
            step
            for job in workflow["jobs"].values()
            for step in job.get("steps", [])
            if "BD_TO_AVP_SUPPORT_DIAGNOSTICS_ENDPOINT" in step.get("env", {})
        ]
        self.assertTrue(inspections)
        for step in inspections:
            self.assertEqual(
                step["env"]["BD_TO_AVP_SUPPORT_DIAGNOSTICS_ENDPOINT"],
                "${{ vars.SUPPORT_DIAGNOSTICS_ENDPOINT }}",
            )

    def test_release_is_draft_until_assets_are_redownloaded_and_verified(self) -> None:
        workflow = load_release_engine()
        jobs = workflow["jobs"]

        self.assertLessEqual(
            {"prepare", "package", "attest-package", "compatibility"}, direct_needs(jobs["create-draft"])
        )
        self.assertIn("release_id", jobs["create-draft"]["outputs"])
        self.assertIn("release_created_at", jobs["create-draft"]["outputs"])
        self.assertLessEqual(
            {"prepare", "package", "create-draft", "publish-appcast"}, direct_needs(jobs["verify-draft"])
        )
        self.assertLessEqual(
            {
                "build-python",
                "create-draft",
                "prepare",
                "package",
                "verify-draft",
                "build-receipt",
                "signed-artifact-ui",
                "update-test",
            },
            direct_needs(jobs["publish-release"]),
        )
        self.assertIn("needs.create-draft.result == 'success'", jobs["publish-release"]["if"])
        self.assertIn("needs.signed-artifact-ui.result == 'success'", jobs["publish-release"]["if"])
        self.assertIn("needs.update-test.result == 'success'", jobs["publish-release"]["if"])
        self.assertIn("needs.build-python.result == 'success'", jobs["publish-release"]["if"])
        self.assertIn("needs.build-receipt.result == 'success'", jobs["publish-release"]["if"])

    def test_preflight_gates_block_signing_and_publication(self) -> None:
        workflow = load_release_engine()
        jobs = workflow["jobs"]
        pre_signing_policy = jobs["pre-signing-policy"]
        pre_signing = jobs["pre-signing-package"]
        signed_ui = jobs["signed-artifact-ui"]
        update_test = jobs["update-test"]

        operators = [load_workflow("briefcase.yml"), load_workflow("prerelease.yml")]
        self.assertTrue(
            all(
                operator["jobs"]["release"]["uses"] == "./.github/workflows/release-engine.yml"
                for operator in operators
            )
        )
        self.assertIn("pre-signing-package", set(jobs["package"]["needs"]))
        self.assertLessEqual({"policy", "prepare"}, direct_needs(pre_signing_policy))
        self.assertLessEqual({"pre-signing-policy"}, direct_needs(pre_signing))
        self.assertEqual(pre_signing["uses"], "./.github/workflows/production-preflight-engine.yml")
        for name in ("signed-artifact-ui", "update-test"):
            self.assertIn(name, set(jobs["publish-release"]["needs"]))
            self.assertIn(f"needs.{name}.result == 'success'", jobs["publish-release"]["if"])
        for job in (pre_signing_policy, pre_signing, signed_ui, update_test):
            self.assertNotIn("environment", job)
        for job in (pre_signing_policy, signed_ui, update_test):
            self.assertLessEqual(write_permissions(job["permissions"]), write_permissions({"contents": "read"}))
        self.assertIn("steps.release_package.outputs.artifact-id", jobs["package"]["outputs"]["workflow_artifact_id"])
        self.assertIn(
            "steps.receipt_artifact.outputs.artifact-id", jobs["build-receipt"]["outputs"]["receipt_artifact_id"]
        )

    def test_release_receipt_depends_on_verified_artifacts(self) -> None:
        workflow = load_release_engine()
        jobs = workflow["jobs"]
        receipt = jobs["build-receipt"]

        self.assertLessEqual(
            {"policy", "prepare", "package", "create-draft", "publish-appcast", "verify-draft"}, direct_needs(receipt)
        )
        self.assertLessEqual(write_permissions(receipt["permissions"]), write_permissions({"contents": "write"}))
        self.assertNotIn("environment", receipt)
        self.assertIn("receipt_file_sha256", receipt["outputs"])
        self.assertIn("signed_app_tree_sha256", jobs["package"]["outputs"])

    def test_release_provenance_jobs_have_attestation_permissions(self) -> None:
        workflow = load_release_engine()
        attest = workflow["jobs"]["attest-package"]
        verify = workflow["jobs"]["verify-draft"]

        self.assertEqual(attest["permissions"]["attestations"], "write")
        self.assertEqual(attest["permissions"]["id-token"], "write")
        self.assertEqual(verify["permissions"]["attestations"], "read")

    def test_private_key_is_only_exposed_to_read_only_signing_step(self) -> None:
        workflow = load_release_engine()
        publish = workflow["jobs"]["publish-appcast"]
        configured_secrets = set(load_github_config()["releaseEnvironments"][publish["environment"]]["secrets"])
        matching_steps = [step for step in publish["steps"] if secrets_read_by(step) & configured_secrets]

        self.assertEqual(
            secrets_read_by({key: value for key, value in workflow.items() if key not in {"on", "jobs"}}), set()
        )
        self.assertEqual(secrets_read_by({key: value for key, value in publish.items() if key != "steps"}), set())
        self.assertEqual(publish["permissions"]["contents"], "read")
        self.assertEqual(len(matching_steps), 1)

    def test_release_deploy_uses_resumable_pages_workflow(self) -> None:
        workflow = load_release_engine()
        deploy = workflow["jobs"]["deploy-appcast"]

        self.assertLessEqual({"prepare", "publish-appcast", "publish-release"}, direct_needs(deploy))
        self.assertIn("!cancelled()", deploy["if"])
        self.assertIn("needs.prepare.result == 'success'", deploy["if"])
        self.assertIn("needs.publish-appcast.result == 'success'", deploy["if"])
        self.assertIn("needs.publish-release.result == 'success'", deploy["if"])
        self.assertNotIn("build-python", deploy["if"])
        self.assertNotIn("publish_pypi", deploy["if"])
        self.assertEqual(deploy["uses"], "./.github/workflows/sparkle-pages.yml")
        self.assertEqual(deploy["with"]["operation"], "deploy")
        self.assertEqual(
            deploy["with"]["expected_appcast_sha256"],
            "${{ needs.publish-appcast.outputs.appcast_sha256 }}",
        )
        self.assertEqual(
            deploy["with"]["expected_base_release_tag"],
            "${{ needs.prepare.outputs.base_snapshot_tag }}",
        )

    def test_release_environment_contract_preserves_scoped_secrets(self) -> None:
        environments = load_github_config()["releaseEnvironments"]

        self.assertTrue(all(environment["branches"] == ["main"] for environment in environments.values()))
        self.assertEqual(environments["sparkle-feed-ops"]["secrets"], [])
        self.assertEqual(environments["pypi"]["secrets"], [])
        self.assertEqual(environments["github-pages"]["secrets"], [])
        self.assertTrue(
            set(environments["macos-signing"]["secrets"]).isdisjoint(environments["sparkle-release"]["secrets"])
        )

    def test_pages_workflow_supports_deploy_restore_and_disable(self) -> None:
        workflow = load_workflow("sparkle-pages.yml")
        manual = load_workflow("manage-sparkle-pages.yml")
        dispatch = manual["on"]["workflow_dispatch"]["inputs"]

        self.assertEqual(set(workflow["on"]), {"workflow_call"})
        self.assertEqual(set(manual["on"]), {"workflow_dispatch"})
        self.assertEqual(manual.get("permissions"), {})
        self.assertLessEqual({"deploy", "restore", "disable"}, set(dispatch["operation"]["options"]))
        # The feed operation waits on a dedicated approval environment, never one holding release secrets.
        approval = manual["jobs"]["approve"]["environment"]
        self.assertEqual(load_github_config()["releaseEnvironments"][approval]["secrets"], [])
        self.assertEqual(manual["jobs"]["approve"].get("permissions"), {})
        self.assertIn("approve", direct_needs(manual["jobs"]["manage"]))
        self.assertEqual(manual["jobs"]["manage"]["uses"], "./.github/workflows/sparkle-pages.yml")
        self.assertEqual(manual["jobs"]["manage"]["with"]["operation"], "${{ inputs.operation }}")
        self.assertEqual(manual["jobs"]["manage"]["with"]["release_tag"], "${{ inputs.release_tag }}")
        self.assertLessEqual(
            write_permissions(manual["jobs"]["manage"]["permissions"]),
            write_permissions({"contents": "read", "id-token": "write", "pages": "write"}),
        )
        self.assertTrue(workflow["concurrency"]["group"])


if __name__ == "__main__":
    unittest.main()
