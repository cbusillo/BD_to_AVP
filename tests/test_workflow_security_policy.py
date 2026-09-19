"""Security rules every GitHub workflow must satisfy.

These are rules over the parsed workflows, not statements about one job or one
line of shell. Renaming a job, moving a step or changing a runner must not
fail them; weakening the release's trust boundaries must.
"""

import json
import re
import subprocess
import unittest
from pathlib import Path
from typing import Any, ClassVar

import yaml

WORKFLOW_DIRECTORY = Path(__file__).resolve().parents[1] / ".github" / "workflows"
UNTRUSTED_TRIGGERS = {"pull_request", "pull_request_target", "push", "schedule", "issue_comment"}
SECRET_REFERENCE = re.compile(r"secrets\.([A-Za-z0-9_]+)")
PINNED_ACTION = re.compile(r"^[^@]+@[0-9a-f]{40}$")


def load_workflows() -> dict[str, dict[str, Any]]:
    return {
        path.name: yaml.safe_load(path.read_text(encoding="utf-8")) for path in sorted(WORKFLOW_DIRECTORY.glob("*.yml"))
    }


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
            for need in direct_needs(job):
                with self.subTest(workflow=workflow_name, job=job_name, need=need):
                    self.assertIn(f"needs.{need}.result", condition)

    def test_release_checkouts_use_the_dispatched_commit(self) -> None:
        for workflow_name in ("release-engine.yml", "production-preflight-engine.yml"):
            for job_name, job in self.workflows[workflow_name]["jobs"].items():
                for step in job.get("steps", []):
                    if str(step.get("uses", "")).startswith("actions/checkout@"):
                        with self.subTest(workflow=workflow_name, job=job_name):
                            self.assertIn("ref", step.get("with", {}))
                            self.assertNotIn("github.ref", str(step["with"]["ref"]))


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
        "ACTUAL_ACTOR": "shiny-code-bot",
        "ACTUAL_TRIGGERING_ACTOR": "shiny-code-bot",
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
            "a release SHA other than the dispatched one": {"INPUT_RELEASE_SHA": "c" * 40},
            "a forged operator run id": {"INPUT_OPERATOR_RUN_ID": "999"},
            "a forged operator run attempt": {"INPUT_OPERATOR_RUN_ATTEMPT": "2"},
            "a forged operator actor": {"INPUT_OPERATOR_ACTOR": "cbusillo"},
        }
        for description, overrides in deviations.items():
            with self.subTest(deviation=description):
                self.assertNotEqual(self.run_guard(**overrides), 0)


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


if __name__ == "__main__":
    unittest.main()
