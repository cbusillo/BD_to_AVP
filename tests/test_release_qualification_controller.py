import io
import json
import subprocess
import tempfile
import unittest

from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path
from unittest.mock import patch

from scripts.release_milestone_context import ReleaseMilestoneContextError
from scripts.release_qualification_controller import (
    EvidenceBinding,
    ReleaseQualificationControllerError,
    _case_categories,
    _load_bound_policy,
    _require_checked_file,
    build_status,
    main,
    resolve_evidence_binding,
)
from scripts.release_qualification_resume import ResumeResult
from scripts.release_milestone_context import ReleaseMilestoneContext


REPO_ROOT = Path(__file__).resolve().parents[1]
READ_ONLY_GIT_COMMANDS = {"diff", "ls-files", "merge-base", "show"}


class ReleaseQualificationControllerTests(unittest.TestCase):
    def test_reports_v031_legacy_evidence_without_mutation(self) -> None:
        before_status = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        before_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        before_refs = subprocess.run(
            ["git", "show-ref"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        original_run = subprocess.run
        observed_commands: list[tuple[str, ...]] = []

        def guarded_run(command: list[str], *args: object, **kwargs: object) -> subprocess.CompletedProcess[object]:
            observed_commands.append(tuple(command))
            self.assertEqual(command[0], "git")
            self.assertIn(command[1], READ_ONLY_GIT_COMMANDS)
            return original_run(command, *args, **kwargs)

        with patch("subprocess.run", side_effect=guarded_run):
            payload = build_status(REPO_ROOT, "v0.3.1", as_of=date(2026, 8, 10))

        after_status = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        after_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        after_refs = subprocess.run(
            ["git", "show-ref"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout

        self.assertTrue(observed_commands)
        self.assertEqual(before_status, after_status)
        self.assertEqual(before_head, after_head)
        self.assertEqual(before_refs, after_refs)
        self.assertIn(payload["evidence_binding"]["mode"], {"legacy", "manifest"})
        self.assertEqual(payload["overall_status"], "ready_with_optional_actions")
        self.assertTrue(payload["passed"])
        self.assertEqual(payload["groups"]["blocking"], [])
        self.assertIn("release-workflow-identity", payload["groups"]["completed"])
        self.assertIn("clean-machine-signed-update", payload["groups"]["completed"])
        self.assertIn("installed-ui-accessibility", payload["groups"]["completed"])
        self.assertEqual(payload["groups"]["operator_required"], [])

    def test_status_output_is_deterministic(self) -> None:
        outputs: list[str] = []
        for _ in range(2):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "status",
                        "--release-tag",
                        "v0.3.1",
                        "--repo-root",
                        str(REPO_ROOT),
                        "--as-of",
                        "2026-08-10",
                    ]
                )
            self.assertEqual(exit_code, 0)
            outputs.append(stdout.getvalue())

        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(json.loads(outputs[0])["release_tag"], "v0.3.1")

    def test_present_invalid_manifest_does_not_fall_back_to_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            manifest_path = root / "docs/release-evidence/v0.3.1/qualification-manifest.json"
            manifest_path.parent.mkdir(parents=True)
            manifest_path.write_text("{}\n", encoding="utf-8")
            with (
                patch(
                    "scripts.release_qualification_status.resolve_milestone_manifest_context",
                    side_effect=ReleaseMilestoneContextError("invalid manifest"),
                ),
                patch("scripts.release_qualification_status.resolve_milestone_context") as legacy_resolver,
            ):
                with self.assertRaisesRegex(ReleaseMilestoneContextError, "invalid manifest"):
                    resolve_evidence_binding(root, "v0.3.1")

        legacy_resolver.assert_not_called()

    def test_absent_manifest_uses_legacy_resolver(self) -> None:
        context = self._context()
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            receipt_path = root / "docs/release-evidence/v0.3.1/release-receipt.json"
            receipt_path.parent.mkdir(parents=True)
            receipt_path.write_text("{}\n", encoding="utf-8")
            with patch(
                "scripts.release_qualification_status.resolve_milestone_context",
                return_value=context,
            ) as legacy_resolver:
                binding = resolve_evidence_binding(root, "v0.3.1")

        legacy_resolver.assert_called_once_with(root, receipt_path)
        self.assertEqual(binding.mode, "legacy")
        self.assertIsNone(binding.manifest)

    def test_manifest_binding_loads_runner_policy(self) -> None:
        context = self._context(
            runner_sha="a" * 40, manifest_path="docs/release-evidence/v0.3.1/qualification-manifest.json"
        )
        manifest = {"canonical_evidence": {"base_sha": "b" * 40}}
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            manifest_path = root / context.manifest_path
            manifest_path.parent.mkdir(parents=True)
            manifest_path.write_text("{}\n", encoding="utf-8")
            with (
                patch(
                    "scripts.release_qualification_status.resolve_milestone_manifest_context",
                    return_value=context,
                ),
                patch(
                    "scripts.release_qualification_status.load_validated_manifest",
                    return_value=manifest,
                ),
            ):
                binding = resolve_evidence_binding(root, "v0.3.1")
            runner_policy = {"policy": "runner"}
            validated_policy = {"policy": "validated"}
            with (
                patch(
                    "scripts.release_qualification_status._read_json_at_revision",
                    return_value=runner_policy,
                ) as read_revision,
                patch(
                    "scripts.release_qualification_status.validate_policy",
                    return_value=validated_policy,
                ) as validate,
            ):
                policy = _load_bound_policy(root, binding)

        self.assertEqual(binding.mode, "manifest")
        self.assertEqual(binding.manifest, manifest)
        self.assertEqual(policy, validated_policy)
        read_revision.assert_called_once_with(
            root, context.runner_sha, context.policy_path, "runner-bound release qualification policy"
        )
        validate.assert_called_once_with(runner_policy)

    def test_manifest_status_validates_append_only_evidence_history(self) -> None:
        context = self._context(
            runner_sha="a" * 40,
            manifest_path="docs/release-evidence/v0.3.1/qualification-manifest.json",
        )
        manifest = {"canonical_evidence": {"base_sha": "b" * 40}}
        binding = EvidenceBinding(context=context, manifest=manifest, mode="manifest")
        policy = {"policy_id": "policy", "cases": [{"id": "complete"}]}
        classification = {
            "as_of": "2026-08-10",
            "blocking_retests": [],
            "cases": [
                {
                    "applicable": True,
                    "blocking": True,
                    "case_id": "complete",
                    "evidence_due": False,
                    "operational_status": None,
                    "status": "covered",
                    "trigger": "exact_candidate",
                }
            ],
            "policy_id": "policy",
            "qualification_id": "qualification",
        }
        with (
            patch("scripts.release_qualification_status.resolve_evidence_binding", return_value=binding),
            patch("scripts.release_qualification_status._require_checked_file"),
            patch("scripts.release_qualification_status.validate_manifest_evidence_history") as validate_history,
            patch("scripts.release_qualification_status._load_bound_policy", return_value=policy),
            patch("scripts.release_qualification_status.load_evidence", return_value={"schema_version": 1}),
            patch(
                "scripts.release_qualification_status.load_qualification_overrides",
                return_value=("qualification", {}),
            ),
            patch("scripts.release_qualification_status.classify_release_scope", return_value=classification),
        ):
            payload = build_status(REPO_ROOT, "v0.3.1", as_of=date(2026, 8, 10))

        validate_history.assert_called_once_with(manifest, repo_root=REPO_ROOT, base_revision="b" * 40)
        self.assertEqual(payload["evidence_binding"]["expected_evidence_ref"], "automation/release-evidence-v0.3.1")
        self.assertEqual(payload["overall_status"], "complete")

    def test_checked_file_rejects_worktree_content_that_differs_from_head(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            path = root / "evidence.json"
            path.write_text('{"state": "checked"}\n', encoding="utf-8")
            subprocess.run(["git", "add", "evidence.json"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "checked"], cwd=root, check=True)
            path.write_text('{"state": "dirty"}\n', encoding="utf-8")

            with self.assertRaisesRegex(ReleaseQualificationControllerError, "must match the checked content"):
                _require_checked_file(root, "evidence.json", "qualification evidence")

    def test_missing_checked_evidence_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            with self.assertRaisesRegex(
                ReleaseQualificationControllerError,
                "No checked qualification manifest or release receipt",
            ):
                resolve_evidence_binding(Path(temporary_directory), "v0.3.1")

    def test_noncanonical_release_tag_is_rejected_before_path_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            with self.assertRaisesRegex(ReleaseQualificationControllerError, "Release tag is invalid"):
                resolve_evidence_binding(Path(temporary_directory), "../../v0.3.1")

    def test_case_categories_preserve_overlapping_status_groups(self) -> None:
        scenarios = (
            (
                {
                    "case_id": "complete",
                    "status": "covered",
                    "trigger": "exact_candidate",
                    "blocking": True,
                    "evidence_due": False,
                    "applicable": True,
                    "operational_status": None,
                },
                {},
                set(),
                ("completed",),
            ),
            (
                {
                    "case_id": "stale-optional",
                    "status": "retest",
                    "trigger": "invalidated",
                    "blocking": False,
                    "evidence_due": True,
                    "applicable": True,
                    "operational_status": "due",
                },
                {},
                set(),
                ("stale", "optional"),
            ),
            (
                {
                    "case_id": "missing-blocker",
                    "status": "retest",
                    "trigger": "missing_prior_evidence",
                    "blocking": True,
                    "evidence_due": True,
                    "applicable": True,
                    "operational_status": None,
                },
                {},
                {"missing-blocker"},
                ("blocking",),
            ),
            (
                {
                    "case_id": "operator",
                    "status": "retest",
                    "trigger": "stable_candidate",
                    "blocking": False,
                    "evidence_due": True,
                    "applicable": True,
                    "operational_status": "due",
                },
                {"execution_mode": "operator_assisted"},
                set(),
                ("optional", "operator_required"),
            ),
            (
                {
                    "case_id": "external",
                    "status": "external",
                    "trigger": "post_publication",
                    "blocking": False,
                    "evidence_due": False,
                    "applicable": False,
                    "operational_status": None,
                },
                {},
                set(),
                (),
            ),
        )
        for result, policy_case, blockers, expected in scenarios:
            with self.subTest(case_id=result["case_id"]):
                self.assertEqual(_case_categories(result, policy_case, blockers), expected)

    def test_main_returns_two_for_computed_blocking_state(self) -> None:
        payload = {"groups": {"blocking": ["required-case"]}}
        stdout = io.StringIO()
        with patch("scripts.release_qualification_controller.build_status", return_value=payload):
            with redirect_stdout(stdout):
                exit_code = main(["status", "--release-tag", "v0.3.1"])

        self.assertEqual(exit_code, 2)
        self.assertEqual(json.loads(stdout.getvalue()), payload)

    def test_main_returns_one_for_validation_failure(self) -> None:
        stderr = io.StringIO()
        with patch(
            "scripts.release_qualification_controller.build_status",
            side_effect=ReleaseQualificationControllerError("conflicting identity"),
        ):
            with redirect_stderr(stderr):
                exit_code = main(["status", "--release-tag", "v0.3.1"])

        self.assertEqual(exit_code, 1)
        self.assertIn("conflicting identity", stderr.getvalue())

    def test_main_delegates_resume_arguments(self) -> None:
        result = ResumeResult(payload={"state": "dispatch_ready"}, exit_code=20)
        stdout = io.StringIO()
        with patch(
            "scripts.release_qualification_resume.resume_qualification",
            return_value=result,
        ) as resume:
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "resume",
                        "--release-tag",
                        "v1.0.0",
                        "--expected-main-sha",
                        "a" * 40,
                        "--expected-manifest-sha256",
                        "b" * 64,
                        "--retry-run-id",
                        "123",
                    ]
                )

        self.assertEqual(exit_code, 20)
        self.assertEqual(json.loads(stdout.getvalue()), result.payload)
        resume.assert_called_once_with(
            REPO_ROOT,
            "v1.0.0",
            expected_main_sha="a" * 40,
            expected_manifest_sha256="b" * 64,
            retry_run_id=123,
            retry_checkpoint_sha256=None,
        )

    @staticmethod
    def _context(*, runner_sha: str = "", manifest_path: str = "") -> ReleaseMilestoneContext:
        return ReleaseMilestoneContext(
            candidate_sha="c" * 40,
            evidence_path="docs/qualification/release-evidence-v1.json",
            first_candidate_of_cycle=False,
            policy_path="docs/qualification/release-qualification-policy-v1.json",
            qualification_path="docs/qualification/stable-signed-qualification-v1.json",
            release_receipt_path="docs/release-evidence/v0.3.1/release-receipt.json",
            release_stage="stable",
            release_tag="v0.3.1",
            manifest_path=manifest_path,
            runner_sha=runner_sha,
        )


if __name__ == "__main__":
    unittest.main()
