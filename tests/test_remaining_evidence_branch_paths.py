"""Exercise the remaining evidence path boundaries with Git rename detection."""

import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.release_evidence_reconcile import ReleaseEvidenceReconciliationError, _verify_docs_only_diff
from scripts.release_milestone_context import (
    ReleaseMilestoneContextError,
    discover_milestone_manifest,
    discover_milestone_receipt,
    discover_terminal_v2_qualification,
)
from tests.test_workflow_security_policy import load_workflows


class RemainingEvidenceBranchPathTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "evidence"
        self.root.mkdir()
        self.environment = {"PATH": os.defpath, "HOME": temporary.name, "GIT_CONFIG_NOSYSTEM": "1"}
        self.tag = "v1.0.0"
        self.bundle = f"docs/release-evidence/{self.tag}"
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "diff.renames", "true")
        self.write("scripts/example.py", "example content\n")
        self.write("docs/example.py", "example content\n")
        self.write("docs/release-evidence/v0.9.0/release-receipt.json", "{}\n")
        self.write("docs/qualification/policy.json", "{}\n")
        self.write("docs/qualification/qualification.json", "{}\n")
        self.write(
            ".github/github.json",
            json.dumps(
                {
                    "releaseOperations": {
                        "qualificationPolicyPath": "docs/qualification/policy.json",
                        "qualificationRecordPath": "docs/qualification/qualification.json",
                    }
                }
            ),
        )
        self.commit("base")
        self.base_sha = self.git("rev-parse", "HEAD")
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        self.git("switch", "-qc", f"automation/release-evidence-{self.tag}")
        # Discovery admits paths; artifact verification is a separate boundary.
        for name in ("qualification-manifest.json", "qualification-v2.json", "release-receipt.json"):
            self.write(f"{self.bundle}/{name}", "{}\n")
        self.write("docs/release-evidence/index-v2.json", "{}\n")

    def write(self, relative: str, contents: str) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")

    def git(self, *arguments: str) -> str:
        return subprocess.run(
            ["git", *arguments], cwd=self.root, env=self.environment, capture_output=True, text=True, check=True
        ).stdout.strip()

    def commit(self, message: str) -> None:
        self.git("add", ".")
        self.git("commit", "-qm", message)

    def move(self, source: str, destination: str) -> None:
        (self.root / destination).parent.mkdir(parents=True, exist_ok=True)
        self.git("mv", source, destination)

    def assert_discovery(self, *, accepted: bool) -> None:
        arguments = {
            "base_sha": self.base_sha,
            "head_sha": self.git("rev-parse", "HEAD"),
            "head_branch": f"automation/release-evidence-{self.tag}",
            "base_repo": "cbusillo/BD_to_AVP",
            "head_repo": "cbusillo/BD_to_AVP",
            "base_branch": "main",
        }
        for discover in (discover_milestone_manifest, discover_milestone_receipt, discover_terminal_v2_qualification):
            with self.subTest(boundary=discover.__name__):
                options = dict(arguments)
                if discover is discover_terminal_v2_qualification:
                    options["qualified_v2_verifier"] = lambda *_args: {"class": "v2-qualified"}
                if accepted:
                    result = discover(self.root, **options)
                    expected = {
                        discover_milestone_manifest: self.root / self.bundle / "qualification-manifest.json",
                        discover_milestone_receipt: self.root / self.bundle / "release-receipt.json",
                        discover_terminal_v2_qualification: self.tag,
                    }
                    self.assertEqual(result, expected[discover])
                else:
                    with self.assertRaises(ReleaseMilestoneContextError):
                        discover(self.root, **options)

    def assert_reconciliation(self, *, accepted: bool) -> None:
        arguments = (self.root, self.base_sha, self.git("rev-parse", "HEAD"), self.tag)
        if accepted:
            _verify_docs_only_diff(*arguments)
        else:
            with self.assertRaises(ReleaseEvidenceReconciliationError):
                _verify_docs_only_diff(*arguments)

    def assert_workflow(self, *, accepted: bool) -> None:
        workflow = load_workflows()["milestone-qualification.yml"]
        guards = [
            guard
            for job in workflow["jobs"].values()
            for step in job.get("steps", [])
            for guard in re.findall(r"EVIDENCE_DIFF=\$\(git\b.*?\bfi\b", step.get("run", ""), flags=re.DOTALL)
        ]
        self.assertTrue(guards, "No executable evidence diff guard selected")
        for guard in guards:
            with self.subTest(boundary="milestone workflow"):
                result = subprocess.run(
                    ["/bin/bash", "-c", "set -euo pipefail\n" + guard],
                    cwd=self.root.parent,
                    env=self.environment,
                    capture_output=True,
                    check=False,
                )
                if accepted:
                    self.assertEqual(result.returncode, 0, result.stderr.decode())
                else:
                    self.assertNotEqual(result.returncode, 0, result.stderr.decode())

    def test_source_to_bundle_rename_is_rejected_at_every_boundary(self) -> None:
        self.move("scripts/example.py", f"{self.bundle}/moved.py")
        self.commit("source moved into bundle")
        self.assert_discovery(accepted=False)
        self.assert_reconciliation(accepted=False)
        self.assert_workflow(accepted=False)

    def test_historical_receipt_move_is_rejected_by_single_tag_boundaries(self) -> None:
        self.move("docs/release-evidence/v0.9.0/release-receipt.json", f"{self.bundle}/moved-receipt.json")
        self.commit("historical receipt moved into bundle")
        self.assert_discovery(accepted=False)
        self.assert_reconciliation(accepted=False)

    def test_bundle_additions_are_accepted(self) -> None:
        self.commit("evidence bundle")
        self.assert_discovery(accepted=True)
        self.assert_reconciliation(accepted=True)
        self.assert_workflow(accepted=True)

    def test_move_within_bundle_is_accepted(self) -> None:
        self.write(f"{self.bundle}/old.py", "example content\n")
        self.commit("evidence bundle")
        self.base_sha = self.git("rev-parse", "HEAD")
        self.move(f"{self.bundle}/old.py", f"{self.bundle}/moved.py")
        self.commit("move within bundle")
        self.assert_reconciliation(accepted=True)
        self.assert_workflow(accepted=True)
