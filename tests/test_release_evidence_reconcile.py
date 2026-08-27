import json
import subprocess
import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from scripts.release_evidence_reconcile import (
    ReleaseEvidenceReconciliationError,
    preflight,
    reconcile,
)


TAG = "v0.3.2-beta.8"
REPOSITORY = "cbusillo/BD_to_AVP"
CHECKS = ["validate", "Analyze (actions)", "Analyze (python)"]
SOURCE_SHA = "a" * 40
ARTIFACT_SHA = "b" * 64


def git(root: Path, *arguments: str) -> str:
    return subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


class ReleaseEvidenceReconcileTests(unittest.TestCase):
    def build_repository(self, root: Path, *, stale_evidence: bool = False) -> tuple[Path, str, str]:
        repository = root / "repository"
        remote = root / "remote.git"
        repository.mkdir()
        git(repository, "init", "--initial-branch=main")
        git(repository, "config", "user.name", "Test Operator")
        git(repository, "config", "user.email", "operator@example.invalid")
        self.write_json(
            repository / ".github" / "github.json",
            {"branchProtection": {"main": {"requiredStatusChecks": CHECKS}}},
        )
        (repository / "README.md").write_text("base\n", encoding="utf-8")
        git(repository, "add", ".")
        git(repository, "commit", "-m", "base")
        base_sha = git(repository, "rev-parse", "HEAD")
        git(repository, "switch", "-c", f"automation/release-evidence-{TAG}")
        self.write_bundle(repository, source_sha=base_sha)
        git(repository, "add", ".")
        git(repository, "commit", "-m", "evidence")
        evidence_sha = git(repository, "rev-parse", "HEAD")
        git(root, "init", "--bare", str(remote))
        git(repository, "remote", "add", "origin", str(remote))
        git(repository, "push", "origin", "main")
        git(repository, "push", "origin", f"automation/release-evidence-{TAG}")
        if stale_evidence:
            git(repository, "switch", "main")
            (repository / "README.md").write_text("moved main\n", encoding="utf-8")
            git(repository, "add", "README.md")
            git(repository, "commit", "-m", "main moved")
            git(repository, "push", "origin", "main")
            main_sha = git(repository, "rev-parse", "HEAD")
        else:
            main_sha = base_sha
        return repository, evidence_sha, main_sha

    def write_bundle(self, repository: Path, *, source_sha: str) -> None:
        bundle = repository / "docs" / "release-evidence" / TAG
        release_workflow = {"actor": "shiny-code-bot", "run_id": 101}
        self.write_json(
            bundle / "capture-v2.json",
            {
                "capture_workflow": {"actor": "shiny-code-bot", "run_id": 102},
                "receipt": {"path": f"docs/release-evidence/{TAG}/release-receipt.json"},
                "release_tag": TAG,
                "release_workflow": release_workflow,
                "source_sha": source_sha,
            },
        )
        self.write_json(
            bundle / "qualification-v2.json",
            {
                "artifact": {"artifact_id": 301, "name": "qualification.zip", "run_id": 201, "sha256": ARTIFACT_SHA},
                "release_tag": TAG,
                "source_sha": source_sha,
                "successful_milestone": {"actor": "cbusillo", "run_id": 201},
            },
        )
        self.write_json(
            bundle / "release-receipt.json",
            {
                "release": {"id": 401, "tag": TAG},
                "source_sha": source_sha,
                "workflow": release_workflow,
            },
        )
        self.write_json(repository / "docs" / "release-evidence" / "index-v2.json", {"schema_version": 2})

    @staticmethod
    def write_json(path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")

    @staticmethod
    def protection(*, checks: list[str] | None = None) -> dict[str, object]:
        return {
            "required_status_checks": {"strict": True, "contexts": checks or CHECKS},
            "required_pull_request_reviews": {},
            "enforce_admins": {"enabled": True},
            "allow_force_pushes": {"enabled": False},
            "allow_deletions": {"enabled": False},
            "required_conversation_resolution": {"enabled": True},
        }

    def gh_json(
        self,
        pull_requests: list[dict[str, object]],
        *,
        operator: str = "cbusillo",
        checks: list[str] | None = None,
    ):
        def run(_root: Path, arguments: list[str], _description: str) -> object:
            if arguments == ["api", "user"]:
                return {"login": operator}
            if arguments[:2] == ["api", f"repos/{REPOSITORY}/branches/main/protection"]:
                return self.protection(checks=checks)
            if arguments[:2] == ["pr", "list"]:
                return pull_requests
            self.fail(f"unexpected gh request: {arguments}")

        return run

    def preflight_context(
        self,
        repository: Path,
        pull_requests: list[dict[str, object]],
        *,
        operator: str = "cbusillo",
        checks: list[str] | None = None,
    ):
        return (
            patch("scripts.release_evidence_reconcile.verify_tag", return_value={"class": "v2-qualified"}),
            patch("scripts.release_evidence_reconcile.verify_write_once_history"),
            patch("scripts.release_evidence_reconcile.check_index_v2"),
            patch(
                "scripts.release_evidence_reconcile._gh_json",
                side_effect=self.gh_json(pull_requests, operator=operator, checks=checks),
            ),
        )

    def test_stale_branch_is_refused_before_any_gh_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository, _, _ = self.build_repository(Path(temporary_directory), stale_evidence=True)
            contexts = self.preflight_context(repository, [])
            with (
                contexts[0],
                contexts[1],
                contexts[2],
                contexts[3],
                self.assertRaisesRegex(ReleaseEvidenceReconciliationError, "stale or diverged"),
            ):
                preflight(repository, release_tag=TAG)

    def test_moved_main_echo_is_refused_before_pr_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository, evidence_sha, main_sha = self.build_repository(Path(temporary_directory))
            contexts = self.preflight_context(repository, [])
            with (
                contexts[0],
                contexts[1],
                contexts[2],
                contexts[3],
                patch("scripts.release_evidence_reconcile._gh_command") as create,
                self.assertRaisesRegex(ReleaseEvidenceReconciliationError, "Protected main SHA echo"),
            ):
                reconcile(
                    repository,
                    release_tag=TAG,
                    evidence_sha=evidence_sha,
                    main_sha="c" * 40,
                    plan_digest="d" * 64,
                )
            create.assert_not_called()
            self.assertEqual(main_sha, git(repository, "rev-parse", "main"))

    def test_existing_exact_pr_is_adopted_without_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository, evidence_sha, main_sha = self.build_repository(Path(temporary_directory))
            pull_requests = [
                {
                    "author": {"login": "cbusillo"},
                    "baseRefName": "main",
                    "headRefName": f"automation/release-evidence-{TAG}",
                    "headRefOid": evidence_sha,
                    "number": 700,
                    "url": "https://example.invalid/pr/700",
                }
            ]
            contexts = self.preflight_context(repository, pull_requests)
            with (
                contexts[0],
                contexts[1],
                contexts[2],
                contexts[3],
                patch("scripts.release_evidence_reconcile._gh_command") as create,
            ):
                result = preflight(repository, release_tag=TAG)
                adopted = reconcile(
                    repository,
                    release_tag=TAG,
                    evidence_sha=evidence_sha,
                    main_sha=main_sha,
                    plan_digest=result.plan.digest,
                )
            self.assertEqual(adopted.action, "adopt")
            create.assert_not_called()

    def test_duplicate_request_adopts_the_single_existing_final_pr(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository, evidence_sha, main_sha = self.build_repository(Path(temporary_directory))
            pull_requests: list[dict[str, object]] = []
            contexts = self.preflight_context(repository, pull_requests)

            def create(_root: Path, _arguments: list[str], _description: str) -> None:
                pull_requests.append(
                    {
                        "author": {"login": "cbusillo"},
                        "baseRefName": "main",
                        "headRefName": f"automation/release-evidence-{TAG}",
                        "headRefOid": evidence_sha,
                        "number": 701,
                        "url": "https://example.invalid/pr/701",
                    }
                )

            with (
                contexts[0],
                contexts[1],
                contexts[2],
                contexts[3],
                patch("scripts.release_evidence_reconcile._gh_command", side_effect=create) as create_mock,
            ):
                plan = preflight(repository, release_tag=TAG).plan
                first = reconcile(
                    repository,
                    release_tag=TAG,
                    evidence_sha=evidence_sha,
                    main_sha=main_sha,
                    plan_digest=plan.digest,
                )
                second = reconcile(
                    repository,
                    release_tag=TAG,
                    evidence_sha=evidence_sha,
                    main_sha=main_sha,
                    plan_digest=first.plan.digest,
                )
            self.assertEqual(first.action, "adopt")
            self.assertEqual(second.action, "adopt")
            self.assertEqual(create_mock.call_count, 1)

    def test_conflicting_open_pr_and_invalid_bundle_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository, evidence_sha, _ = self.build_repository(Path(temporary_directory))
            conflicts = [
                {
                    "author": {"login": "cbusillo"},
                    "baseRefName": "main",
                    "headRefName": "feature/unrelated",
                    "headRefOid": evidence_sha,
                    "number": 702,
                    "url": "https://example.invalid/pr/702",
                }
            ]
            contexts = self.preflight_context(repository, conflicts)
            with (
                contexts[0],
                contexts[1],
                contexts[2],
                contexts[3],
                self.assertRaisesRegex(ReleaseEvidenceReconciliationError, "unrelated pull request"),
            ):
                preflight(repository, release_tag=TAG)
            contexts = self.preflight_context(repository, [])
            with (
                contexts[0],
                contexts[1],
                contexts[2],
                patch(
                    "scripts.release_evidence_reconcile.verify_tag",
                    side_effect=ReleaseEvidenceReconciliationError("bad bundle"),
                ),
                self.assertRaisesRegex(ReleaseEvidenceReconciliationError, "bad bundle"),
            ):
                preflight(repository, release_tag=TAG)

    def test_missing_terminal_and_durable_failure_refuse_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository, _, _ = self.build_repository(Path(temporary_directory))
            contexts = self.preflight_context(repository, [])
            with (
                contexts[1],
                contexts[2],
                contexts[3],
                patch("scripts.release_evidence_reconcile.verify_tag", return_value={"class": "v2-captured"}),
                self.assertRaisesRegex(ReleaseEvidenceReconciliationError, "v2-qualified"),
            ):
                preflight(repository, release_tag=TAG)
            contexts = self.preflight_context(repository, [])
            with (
                contexts[1],
                contexts[2],
                contexts[3],
                patch("scripts.release_evidence_reconcile.verify_tag", return_value={"class": "v2-disposed"}),
                self.assertRaisesRegex(ReleaseEvidenceReconciliationError, "Durable failed"),
            ):
                preflight(repository, release_tag=TAG)

    def test_actor_mismatch_refuses_adoption(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository, evidence_sha, _ = self.build_repository(Path(temporary_directory))
            pull_requests = [
                {
                    "author": {"login": "someone-else"},
                    "baseRefName": "main",
                    "headRefName": f"automation/release-evidence-{TAG}",
                    "headRefOid": evidence_sha,
                    "number": 703,
                    "url": "https://example.invalid/pr/703",
                }
            ]
            contexts = self.preflight_context(repository, pull_requests)
            with (
                contexts[0],
                contexts[1],
                contexts[2],
                contexts[3],
                self.assertRaisesRegex(ReleaseEvidenceReconciliationError, "active local GitHub operator"),
            ):
                preflight(repository, release_tag=TAG)

    def test_digest_and_sha_echoes_are_exact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository, evidence_sha, main_sha = self.build_repository(Path(temporary_directory))
            contexts = self.preflight_context(repository, [])
            with (
                contexts[0],
                contexts[1],
                contexts[2],
                contexts[3],
                patch("scripts.release_evidence_reconcile._gh_command") as create,
            ):
                plan = preflight(repository, release_tag=TAG).plan
                with self.assertRaisesRegex(ReleaseEvidenceReconciliationError, "Evidence SHA echo"):
                    reconcile(
                        repository,
                        release_tag=TAG,
                        evidence_sha="c" * 40,
                        main_sha=main_sha,
                        plan_digest=plan.digest,
                    )
                with self.assertRaisesRegex(ReleaseEvidenceReconciliationError, "Plan digest echo"):
                    reconcile(
                        repository,
                        release_tag=TAG,
                        evidence_sha=evidence_sha,
                        main_sha=main_sha,
                        plan_digest="c" * 64,
                    )
            create.assert_not_called()

    def test_required_protected_checks_must_exactly_match_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository, _, _ = self.build_repository(Path(temporary_directory))
            contexts = self.preflight_context(repository, [], checks=CHECKS[:-1])
            with (
                contexts[0],
                contexts[1],
                contexts[2],
                contexts[3],
                self.assertRaisesRegex(ReleaseEvidenceReconciliationError, "required checks do not exactly match"),
            ):
                preflight(repository, release_tag=TAG)


if __name__ == "__main__":
    unittest.main()
