"""Security rules every GitHub workflow must satisfy.

These are rules over the parsed workflows, not statements about one job or one
line of shell. Renaming a job, moving a step or changing a runner must not
fail them; weakening the release's trust boundaries must.
"""

import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any, ClassVar

import yaml

from scripts.production_identity import PRODUCTION_DEVELOPER_IDENTITY, PRODUCTION_TEAM_ID
from scripts.tier3_clean_machine import RESETTABLE_VM_ENVIRONMENT_CLASS
from scripts.release_workflow_policy import (
    APPROVAL_ENVIRONMENT,
    ENGINE_WORKFLOW_PATH,
    RECEIPT_ACTORS,
    REPOSITORY,
    REQUIRED_ACTOR,
    REQUIRED_REF,
    STABLE_OPERATOR_WORKFLOW_PATH,
    STABLE_ROUTE,
)

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

    def test_a_job_that_reads_a_secret_is_approval_gated_and_cannot_write(self) -> None:
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
            "milestone-qualification.yml",
            "release-evidence.yml",
        ):
            with self.subTest(workflow=name):
                workflow = load_workflow(name)
                self.assertEqual(secrets_read_by(workflow), set())
                for job in workflow["jobs"].values():
                    self.assertNotIn("environment", job)
                    self.assertNotIn("secrets", job)

    def test_signing_steps_bind_identity_to_the_production_constants(self) -> None:
        checked = 0
        for job in load_release_engine()["jobs"].values():
            for step in job.get("steps", []):
                environment = step.get("env", {})
                if environment.get("DEV_ID") != "${{ secrets.DEV_ID }}":
                    continue
                checked += 1
                with self.subTest(step=step.get("id")):
                    self.assertEqual(environment["TEAM_ID"], "${{ secrets.TEAM_ID }}")
                    self.assertEqual(environment["PRODUCTION_TEAM_ID"], PRODUCTION_TEAM_ID)
                    self.assertEqual(environment["PRODUCTION_DEV_ID"], PRODUCTION_DEVELOPER_IDENTITY)
        self.assertGreater(checked, 0)

    def test_clean_machine_commands_select_the_ephemeral_runner_contract(self) -> None:
        steps = load_workflow("milestone-qualification.yml")["jobs"]["qualify"]["steps"]
        for ident in ("clean-machine-preflight", "clean-machine-run"):
            step = next(step for step in steps if step.get("id") == ident)
            with self.subTest(step=ident), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                inputs = root / "qualification-inputs"
                inputs.mkdir()
                (inputs / "paths.env").write_text(
                    "CANDIDATE_DMG=candidate.dmg\nPRIOR_TAG=v1.0.0\nPRIOR_DMG=prior.dmg\nROUTE=stable\n",
                    encoding="utf-8",
                )
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
                        "CANDIDATE_TAG": "v1.1.0",
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
        self.assertEqual(success_upload["with"]["retention-days"], "7")
        self.assertEqual(failure_upload["with"]["retention-days"], "7")
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
            {"policy", "prepare", "qualify-preparation", "pre-signing-package"},
        )
        self.assertEqual(package["environment"], "macos-signing")
        self.assertEqual(package["permissions"]["contents"], "read")
        self.assertIn("dmg_sha256", package["outputs"])
        self.assertIn("dmg_size", package["outputs"])
        self.assertEqual(package_step["env"]["DMG_NAME"], "${{ needs.prepare.outputs.dmg_name }}")
        next(step for step in package["steps"] if step.get("id") == "notary-credentials")

    def test_post_release_evidence_workflow_is_secret_free_and_idempotent(self) -> None:
        workflow = load_workflow("release-evidence.yml")
        jobs = workflow["jobs"]
        prepare = jobs["validate-and-prepare"]
        publish = jobs["publish-evidence-branch"]

        self.assertEqual(set(workflow["on"]["workflow_run"]["workflows"]), {"Stable", "Prerelease"})
        self.assertEqual(prepare["permissions"], {"actions": "read", "contents": "read"})
        self.assertEqual(publish["permissions"], {"contents": "write"})
        self.assertNotIn("environment", prepare)
        self.assertNotIn("environment", publish)
        evidence_writers = [
            step for step in prepare["steps"] if step.get("id") in {"signed-ui", "reconcile", "capture-v2"}
        ]
        self.assertEqual(len(evidence_writers), 3)
        self.assertEqual(len({step["if"] for step in evidence_writers}), 1)
        conflict_step = next(step for step in publish["steps"] if step.get("id") == "evidence-conflict")
        self.assertEqual(
            conflict_step["env"]["EXPECTED_MAIN_SHA"],
            "${{ needs.validate-and-prepare.outputs.base_main_sha }}",
        )
        actor_step = next(step for step in publish["steps"] if step.get("id") == "evidence-parent")
        self.assertEqual(actor_step["env"]["EVIDENCE_ACTOR_ID"], "${{ github.actor_id }}")
        self.assertEqual(actor_step["env"]["EVIDENCE_ACTOR_LOGIN"], "${{ github.actor }}")
        release_operations = load_github_config()["releaseOperations"]
        self.assertEqual(release_operations["evidenceBranchPattern"], "automation/release-evidence-<tag>")
        self.assertEqual(
            release_operations["evidenceCapturePath"],
            "docs/release-evidence/<tag>/capture-v2.json",
        )
        self.assertEqual(release_operations["evidenceCheckpointSemantics"], "branch_commit_plus_captured_v2")
        self.assertEqual(release_operations["evidenceMergeSemantics"], "operator_opened_protected_pull_request")

        next(step for step in publish["steps"] if step.get("id") == "publish")

    def test_milestone_qualification_is_hosted_secret_free_and_read_only(self) -> None:
        workflow = load_workflow("milestone-qualification.yml")
        dispatch = workflow["on"]["workflow_dispatch"]
        qualify = workflow["jobs"]["qualify"]
        steps = qualify["steps"]
        checkout = steps[0]
        upload = steps[-1]
        by_id = {step.get("id"): step for step in steps}
        diagnostics_upload = by_id["sparkle-diagnostics"]

        self.assertEqual(
            set(dispatch["inputs"]),
            {"candidate_tag", "manifest_sha256"},
        )
        self.assertEqual(workflow["permissions"], {})
        self.assertEqual(qualify["permissions"], {"actions": "read", "contents": "read"})
        # Only the automation identity, or the repository owner, may start a qualification job.
        self.assertEqual(
            {clause.strip() for clause in qualify["if"].split("||")},
            {f"github.actor == '{REQUIRED_ACTOR}'", "github.actor == github.repository_owner"},
        )
        self.assertNotIn("environment", qualify)
        self.assertRegex(checkout["uses"], r"^actions/checkout@[0-9a-f]{40}$")
        self.assertEqual(checkout["with"]["fetch-depth"], "0")
        self.assertEqual(checkout["with"]["persist-credentials"], "false")
        self.assertEqual(checkout["with"]["ref"], "${{ github.sha }}")
        self.assertRegex(upload["uses"], r"^actions/upload-artifact@[0-9a-f]{40}$")
        self.assertEqual(upload["with"]["retention-days"], "30")
        self.assertRegex(diagnostics_upload["uses"], r"^actions/upload-artifact@[0-9a-f]{40}$")
        self.assertEqual(
            diagnostics_upload["if"],
            "${{ failure() && hashFiles('qualification-output/sparkle-install-diagnostics.json') != '' }}",
        )
        self.assertEqual(
            diagnostics_upload["with"]["path"],
            "qualification-output/sparkle-install-diagnostics.json",
        )
        self.assertEqual(diagnostics_upload["with"]["retention-days"], "30")

        config = load_github_config()
        self.assertIn("Milestone Qualification", config["importantWorkflows"])
        self.assertEqual(
            config["releaseOperations"]["milestoneQualificationWorkflowPath"],
            ".github/workflows/milestone-qualification.yml",
        )
        self.assertEqual(
            config["releaseOperations"]["qualificationManifestPath"],
            "docs/release-evidence/<tag>/qualification-manifest.json",
        )
        self.assertEqual(
            config["releaseOperations"]["qualificationSnapshotPath"],
            "docs/release-evidence/<tag>/qualification-record.json",
        )
        self.assertEqual(
            config["releaseOperations"]["qualificationManifestCommand"],
            "uv run python -m scripts.release_qualification_manifest",
        )

    def test_release_evidence_pr_enforces_post_publication_milestone(self) -> None:
        workflow = load_workflow("ci.yml")
        by_id = {step.get("id"): step for job in workflow["jobs"].values() for step in job["steps"]}
        context = by_id["milestone-context"]
        classify = by_id["milestone-qualification"]
        upload = by_id["milestone-report"]
        summarize = by_id["milestone-summary"]
        enforce = by_id["milestone-enforce"]

        self.assertEqual(context["if"], "github.event_name == 'pull_request'")
        self.assertEqual(context["env"], {"GH_TOKEN": "${{ github.token }}"})
        for step in (classify, upload, summarize, enforce):
            self.assertIn("steps.milestone-context.outputs.required == 'true'", step["if"])
        self.assertTrue(classify["continue-on-error"])
        self.assertEqual(upload["with"]["if-no-files-found"], "error")
        self.assertIn("release-qualification-milestone-${{ github.run_attempt }}", upload["with"]["name"])

    def test_stable_pypi_publish_uses_oidc_trusted_publishing(self) -> None:
        operator = load_workflow("briefcase.yml")
        prerelease = load_workflow("prerelease.yml")
        workflow = load_release_engine()
        build = workflow["jobs"]["build-python"]
        publish = operator["jobs"]["publish-pypi"]
        release_operations = load_github_config()["releaseOperations"]

        self.assertFalse((REPO_ROOT / ".github" / "workflows" / "publish-to-pypi.yml").exists())
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
        self.assertEqual(
            release_operations["workflows"]["Stable"]["path"],
            ".github/workflows/briefcase.yml",
        )
        self.assertEqual(
            release_operations["workflows"]["Prerelease"],
            {"path": ".github/workflows/prerelease.yml", "route": "prerelease"},
        )
        self.assertEqual(release_operations["engineWorkflowPath"], ".github/workflows/release-engine.yml")


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
            if job.get("environment") == APPROVAL_ENVIRONMENT
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


class EvidenceCompletionSourceTests(unittest.TestCase):
    def test_evidence_uses_the_completed_run_identity(self) -> None:
        workflow = load_workflows()["release-evidence.yml"]
        steps = [
            step
            for job in workflow["jobs"].values()
            for step in job.get("steps", [])
            if "COMPLETED_RUN_ID" in step.get("env", {})
        ]
        self.assertEqual(len(steps), 1)
        step = steps[0]
        for title in ("Stable", "Prerelease", "Stable PyPI recovery"):
            with self.subTest(title=title), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "outputs"
                environment = {
                    "PATH": "/usr/bin:/bin",
                    "GITHUB_OUTPUT": str(output),
                    "COMPLETED_RUN_ID": "123",
                    "COMPLETED_SOURCE_SHA": "a" * 40,
                    "COMPLETED_DISPLAY_TITLE": title,
                }
                result = subprocess.run(
                    ["/bin/bash", "-c", step["run"]], env=environment, capture_output=True, check=False
                )
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                values = dict(line.split("=", 1) for line in output.read_text().splitlines())
                self.assertEqual(values["release_run_id"], environment["COMPLETED_RUN_ID"])
                self.assertEqual(values["release_source_sha"], environment["COMPLETED_SOURCE_SHA"])


class EvidenceBranchIdentityTests(unittest.TestCase):
    def run_preparation(self, root: Path, **overrides: str) -> subprocess.CompletedProcess[bytes]:
        self.git(root, "init", "-q")
        self.git(
            root, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "--allow-empty", "-qm", "base"
        )
        workflow = load_workflows()["release-evidence.yml"]
        step = next(
            step
            for job in workflow["jobs"].values()
            for step in job.get("steps", [])
            if "EVIDENCE_ACTOR_LOGIN" in step.get("env", {})
        )
        return subprocess.run(
            ["/bin/bash", "-c", step["run"]],
            cwd=root,
            env={
                "PATH": "/usr/bin:/bin",
                "HOME": str(root),
                "GIT_CONFIG_NOSYSTEM": "1",
                "EVIDENCE_ACTOR_ID": "1234",
                "EVIDENCE_ACTOR_LOGIN": REQUIRED_ACTOR,
                "EVIDENCE_REF": "automation/release-evidence-test",
                "EXPECTED_EXISTING_BRANCH_SHA": "",
                "EXPECTED_MAIN_SHA": self.git(root, "rev-parse", "HEAD"),
                **overrides,
            },
            capture_output=True,
            check=False,
        )

    @staticmethod
    def git(root: Path, *arguments: str) -> str:
        return subprocess.run(
            ["git", *arguments],
            cwd=root,
            env={"PATH": "/usr/bin:/bin", "HOME": str(root), "GIT_CONFIG_NOSYSTEM": "1"},
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    def test_app_identity_can_create_and_author_the_evidence_branch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = self.run_preparation(root)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            self.git(root, "commit", "--allow-empty", "-qm", "evidence")
            self.assertEqual(self.git(root, "show", "-s", "--format=%an", "HEAD"), REQUIRED_ACTOR)
            self.assertEqual(
                self.git(root, "show", "-s", "--format=%ae", "HEAD"),
                f"1234+{REQUIRED_ACTOR}@users.noreply.github.com",
            )

    def test_invalid_actor_metadata_is_rejected_before_branch_creation(self) -> None:
        for field, value in (
            ("EVIDENCE_ACTOR_LOGIN", ""),
            ("EVIDENCE_ACTOR_LOGIN", "app[bot"),
            ("EVIDENCE_ACTOR_LOGIN", "app[bot]extra"),
            ("EVIDENCE_ACTOR_LOGIN", "app\nname"),
            ("EVIDENCE_ACTOR_ID", "0"),
            ("EVIDENCE_ACTOR_ID", "not-a-number"),
        ):
            with self.subTest(field=field, value=value), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.assertNotEqual(self.run_preparation(root, **{field: value}).returncode, 0)
                self.assertEqual(self.git(root, "branch", "--list", "automation/release-evidence-test"), "")


# Post-publication qualification compiles a test helper on the macOS release that
# the qualification policy names; it builds nothing that ships.
QUALIFICATION_ONLY_WORKFLOWS = {"milestone-qualification.yml"}


class ToolchainAgreementTests(unittest.TestCase):
    """One Xcode toolchain builds everything that ships; the tests do not care which one."""

    def test_workflows_and_the_bundled_decoder_agree_on_one_xcode_toolchain(self) -> None:
        repository = WORKFLOW_DIRECTORY.parents[1]
        provenance = json.loads(
            (repository / "bd_to_avp/resources/notices/edge264-mvc-build.json").read_text(encoding="utf-8")
        )
        declared = {("bundled edge264", provenance["xcode_version"], provenance["xcode_build_version"])}
        for workflow_name, workflow in load_workflows().items():
            if workflow_name in QUALIFICATION_ONLY_WORKFLOWS:
                continue
            environment = workflow.get("env") or {}
            if "XCODE_VERSION" in environment:
                declared.add(
                    (workflow_name, str(environment["XCODE_VERSION"]), str(environment["XCODE_BUILD_VERSION"]))
                )
            for selected in set(re.findall(r"/Applications/Xcode_([0-9.]+)\.app", json.dumps(workflow))):
                self.assertEqual(selected, provenance["xcode_version"], workflow_name)
        self.assertGreater(len(declared), 1)
        self.assertEqual(len({(version, build) for _, version, build in declared}), 1, sorted(declared))

    def test_post_publication_qualification_runs_on_a_macos_release_the_policy_allows(self) -> None:
        repository = WORKFLOW_DIRECTORY.parents[1]
        policy = json.loads(
            (repository / "docs/qualification/release-qualification-policy-v1.json").read_text(encoding="utf-8")
        )
        allowed = {
            str(major)
            for case in policy["cases"]
            for major in (case.get("environment") or {}).get("macos_major_versions", [])
        }
        self.assertTrue(allowed)
        for workflow_name in QUALIFICATION_ONLY_WORKFLOWS:
            for job_name, job in load_workflows()[workflow_name]["jobs"].items():
                scripts = "\n".join(str(step.get("run", "")) for step in job.get("steps", []))
                checked = set(re.findall(r'sw_vers -productVersion \| cut -d\. -f1\)" = "(\d+)"', scripts))
                with self.subTest(workflow=workflow_name, job=job_name):
                    self.assertTrue(checked)
                    self.assertLessEqual(checked, allowed)

    def test_no_job_runs_on_a_self_hosted_runner(self) -> None:
        for workflow_name, workflow in load_workflows().items():
            for job_name, job in workflow["jobs"].items():
                with self.subTest(workflow=workflow_name, job=job_name):
                    self.assertNotIn("self-hosted", json.dumps(job.get("runs-on", "")))


if __name__ == "__main__":
    unittest.main()
