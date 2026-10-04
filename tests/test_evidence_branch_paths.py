"""Exercise documentation-only boundaries against real Git rename detection."""

import os
import re
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from scripts.release_qualification_resume import (
    QualificationResumeSafetyError,
    _require_local_evidence_checkout,
)
from tests.test_release_qualification_resume import identity
from tests.test_workflow_security_policy import load_workflows


class EvidenceBranchPathTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.environment = {
            "PATH": os.defpath,
            "HOME": str(self.root),
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "diff.renames", "true")
        for name in ("scripts/example.py", "docs/example.py"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("example content\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-qm", "base")
        self.base_sha = self.git("rev-parse", "HEAD")
        self.git("switch", "-qc", identity().evidence_ref)

    def git(self, *arguments: str) -> str:
        return subprocess.run(
            ["git", *arguments],
            cwd=self.root,
            env=self.environment,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    def change(self, source: str, destination: str | None) -> None:
        if destination is None:
            (self.root / source).parent.mkdir(parents=True, exist_ok=True)
            (self.root / source).write_text("updated documentation\n", encoding="utf-8")
        else:
            (self.root / destination).parent.mkdir(parents=True, exist_ok=True)
            self.git("mv", source, destination)
        self.git("add", ".")
        self.git("commit", "-qm", "evidence")
        for ref in ("release-evidence-existing", "release-evidence-verified"):
            self.git("update-ref", f"refs/remotes/origin/{ref}", "HEAD")

    def assert_boundaries(self, *, accepted: bool) -> None:
        checked = replace(identity(), main_sha=self.base_sha, evidence_sha=self.git("rev-parse", "HEAD"))
        if accepted:
            _require_local_evidence_checkout(self.root, checked)
        else:
            with self.assertRaisesRegex(QualificationResumeSafetyError, "non-documentation changes"):
                _require_local_evidence_checkout(self.root, checked)

        self.assert_workflow_boundaries(accepted=accepted)

    def assert_workflow_boundaries(self, *, accepted: bool) -> None:
        workflow = load_workflows()["release-evidence.yml"]
        guards = [
            guard
            for job in workflow["jobs"].values()
            for step in job.get("steps", [])
            for guard in re.findall(
                r"EVIDENCE_DIFF=\$\(git diff\b.*?\)\n\s*if\b.*?\bfi\b|if git diff\b.*?\bfi\b",
                step.get("run", ""),
                flags=re.DOTALL,
            )
        ]
        self.assertTrue(guards, "No executable branch-diff guards were selected")
        for guard in guards:
            with self.subTest(guard=guard):
                result = subprocess.run(
                    ["/bin/bash", "-c", "set -euo pipefail\n" + guard],
                    cwd=self.root,
                    env={**self.environment, "EXPECTED_MAIN_SHA": self.base_sha},
                    capture_output=True,
                    check=False,
                )
                if accepted:
                    self.assertEqual(result.returncode, 0, result.stderr.decode())
                else:
                    self.assertNotEqual(result.returncode, 0, result.stderr.decode())

    def test_source_to_docs_rename_is_rejected(self) -> None:
        self.change("scripts/example.py", "docs/moved.py")
        self.assert_boundaries(accepted=False)

    def test_docs_to_source_rename_is_rejected(self) -> None:
        self.change("docs/example.py", "scripts/moved.py")
        self.assert_boundaries(accepted=False)

    def test_docs_rename_is_accepted(self) -> None:
        self.change("docs/example.py", "docs/moved.py")
        self.assert_boundaries(accepted=True)

    def test_docs_edit_is_accepted(self) -> None:
        self.change("docs/example.py", None)
        self.assert_boundaries(accepted=True)

    def test_source_deletion_is_rejected(self) -> None:
        self.git("rm", "scripts/example.py")
        self.change("docs/example.py", None)
        self.assert_boundaries(accepted=False)

    def test_empty_diff_is_accepted(self) -> None:
        for ref in ("release-evidence-existing", "release-evidence-verified"):
            self.git("update-ref", f"refs/remotes/origin/{ref}", "HEAD")
        self.assert_workflow_boundaries(accepted=True)

    def test_missing_base_is_rejected(self) -> None:
        self.change("docs/example.py", None)
        self.git("branch", "-D", "main")
        self.base_sha = "missing-ref"
        self.assert_workflow_boundaries(accepted=False)

    def test_missing_evidence_ref_is_rejected(self) -> None:
        self.change("docs/example.py", None)
        # Every guard's right-hand ref is unavailable, including the local HEAD.
        for ref in ("release-evidence-existing", "release-evidence-verified"):
            self.git("update-ref", "-d", f"refs/remotes/origin/{ref}")
        self.git("symbolic-ref", "HEAD", "refs/heads/missing-evidence")
        self.assert_workflow_boundaries(accepted=False)

    def test_unrelated_histories_are_rejected(self) -> None:
        self.git("switch", "--orphan", "unrelated")
        self.git("commit", "--allow-empty", "-qm", "unrelated root")
        self.change("docs/example.py", None)
        self.assert_workflow_boundaries(accepted=False)
