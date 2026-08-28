import hashlib
import json
import os
import stat
import subprocess
import tempfile
import unittest

from pathlib import Path
from typing import cast
from unittest.mock import patch

from scripts.release_qualification_apply import (
    QualificationApplyError,
    QualificationApplySafetyError,
    _commit_message,
    _push_commit,
    _replace_progress,
    _validate_commit,
    continue_reconciliation_apply,
    load_reconciliation_checkpoint,
    start_reconciliation_apply,
)
from scripts.release_qualification_artifact import ReconciliationBundle, ReconciliationFile
from scripts.release_qualification_resume import ResumeIdentity


RELEASE_TAG = "v1.0.0"
EVIDENCE_REF = f"automation/release-evidence-{RELEASE_TAG}"
ACTOR_ID = 1_875_516


class ApplyFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.remote = root.parent / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", self.remote], check=True)
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "switch", "-qc", EVIDENCE_REF], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "Fixture"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=root, check=True)
        index_path = root / "docs/qualification/release-evidence-v1.json"
        index_path.parent.mkdir(parents=True)
        index_path.write_text('{"receipts":[],"schema_version":1}\n', encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "fixture"], cwd=root, check=True)
        subprocess.run(["git", "remote", "add", "origin", str(self.remote)], cwd=root, check=True)
        subprocess.run(["git", "push", "-qu", "origin", EVIDENCE_REF], cwd=root, check=True)
        self.base_sha = self.git("rev-parse", "HEAD")
        self.identity = ResumeIdentity(
            release_tag=RELEASE_TAG,
            candidate_sha="c" * 40,
            release_id=123,
            manifest_sha256="d" * 64,
            runner_sha="a" * 40,
            main_sha="a" * 40,
            evidence_ref=EVIDENCE_REF,
            evidence_sha=self.base_sha,
            evidence_base_sha="e" * 40,
            release_receipt_file_sha256="5" * 64,
            signed_ui_artifact_id=456,
            signed_ui_artifact_sha256="6" * 64,
            policy_sha256="1" * 64,
            policy_checkpoint_sha256="2" * 64,
            route_table_sha256="3" * 64,
            controller_runner_sha256="4" * 64,
        )
        receipt_path = f"docs/qualification/{RELEASE_TAG}-clean-machine-signed-update-v1.json"
        index_path_text = "docs/qualification/release-evidence-v1.json"
        receipt_content = b'{"receipt_sha256":"' + b"7" * 64 + b'"}\n'
        index_content = (
            json.dumps(
                {
                    "receipts": [
                        {
                            "accepted_at": "2026-08-10T21:48:28Z",
                            "case_id": "clean-machine-signed-update",
                            "receipt_id": f"{RELEASE_TAG}:clean-machine-signed-update:9001",
                            "reference": receipt_path,
                            "sha256": hashlib.sha256(receipt_content).hexdigest(),
                            "source": "tier3_automation_receipt",
                            "source_sha": "c" * 40,
                            "status": "accepted",
                        }
                    ],
                    "schema_version": 1,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode()
        self.files = (
            ReconciliationFile(path=receipt_path, content=receipt_content),
            ReconciliationFile(path=index_path_text, content=index_content),
        )
        plan: dict[str, object] = {
            "artifact": {"run_attempt": 1, "run_id": 9001},
            "evidence_sha": self.base_sha,
            "files": [
                {
                    "path": file.path,
                    "sha256": hashlib.sha256(file.content).hexdigest(),
                    "size_bytes": len(file.content),
                    "state": "append" if file.path == index_path_text else "create",
                }
                for file in self.files
            ],
            "operations": [],
            "plan_type": "bd_to_avp.release_qualification_reconciliation_plan",
            "release_tag": RELEASE_TAG,
            "requires_changes": True,
            "schema_version": 1,
        }
        plan["plan_sha256"] = hashlib.sha256(
            (json.dumps(plan, separators=(",", ":"), sort_keys=True) + "\n").encode()
        ).hexdigest()
        self.bundle = ReconciliationBundle(plan=plan, files=self.files)
        self.checkpoint = root.parent / "apply.json"
        self.revalidations: list[str] = []
        self.active_checks = 0

    def git(self, *arguments: str) -> str:
        return subprocess.run(
            ["git", *arguments],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    def remote_sha(self) -> str:
        output = subprocess.run(
            ["git", "ls-remote", self.remote, f"refs/heads/{EVIDENCE_REF}"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        return output.split()[0]

    def revalidate(self, expected: str) -> None:
        self.revalidations.append(expected)
        if self.remote_sha() != expected:
            raise QualificationApplySafetyError("simulated remote identity conflict")

    def ensure_no_active(self) -> None:
        self.active_checks += 1

    @staticmethod
    def require_actor() -> tuple[str, int]:
        return "cbusillo", ACTOR_ID

    def start(self):
        with (
            patch("scripts.release_qualification_apply._validate_origin"),
            patch("scripts.release_qualification_apply.HTTPS_REPOSITORY_URL", str(self.remote)),
        ):
            return start_reconciliation_apply(
                repo_root=self.root,
                identity=self.identity,
                bundle=self.bundle,
                expected_plan_sha256=self.bundle.plan["plan_sha256"],
                checkpoint_path=self.checkpoint,
                revalidate_remote=self.revalidate,
                ensure_no_active_runs=self.ensure_no_active,
                require_actor=self.require_actor,
                remote_evidence_sha=self.remote_sha,
            )

    def continue_apply(self):
        with (
            patch("scripts.release_qualification_apply._validate_origin"),
            patch("scripts.release_qualification_apply.HTTPS_REPOSITORY_URL", str(self.remote)),
        ):
            return continue_reconciliation_apply(
                repo_root=self.root,
                identity=self.identity,
                expected_plan_sha256=self.bundle.plan["plan_sha256"],
                checkpoint_path=self.checkpoint,
                revalidate_remote=self.revalidate,
                ensure_no_active_runs=self.ensure_no_active,
                require_actor=self.require_actor,
                remote_evidence_sha=self.remote_sha,
            )


class ReleaseQualificationApplyTests(unittest.TestCase):
    def test_apply_writes_commits_pushes_and_removes_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "work"
            root.mkdir()
            fixture = ApplyFixture(root)

            outcome = fixture.start()
            checkpoint = load_reconciliation_checkpoint(fixture.checkpoint)

            self.assertEqual(outcome.state, "reconciliation_applied")
            self.assertEqual(fixture.remote_sha(), outcome.commit_sha)
            self.assertIsNone(checkpoint)
            self.assertFalse(fixture.checkpoint.exists())
            self.assertGreaterEqual(fixture.active_checks, 3)
            self.assertNotIn("comment_id", outcome.__dataclass_fields__)

    def test_push_response_loss_adopts_remote_commit_without_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "work"
            root.mkdir()
            fixture = ApplyFixture(root)

            def push_then_fail(repo_root: Path, evidence_ref: str, commit_sha: str) -> None:
                subprocess.run(
                    ["git", "push", "origin", f"{commit_sha}:refs/heads/{evidence_ref}"],
                    cwd=repo_root,
                    check=True,
                    capture_output=True,
                )
                raise QualificationApplyError("simulated lost push response")

            with (
                patch("scripts.release_qualification_apply._validate_origin"),
                patch(
                    "scripts.release_qualification_apply._push_commit",
                    side_effect=push_then_fail,
                ),
            ):
                outcome = fixture.start()
            checkpoint = load_reconciliation_checkpoint(fixture.checkpoint)
            self.assertIsNone(checkpoint)
            self.assertNotEqual(fixture.remote_sha(), fixture.base_sha)
            self.assertEqual(outcome.commit_sha, fixture.remote_sha())

    def test_push_race_to_unrelated_commit_is_safety_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "work"
            root.mkdir()
            fixture = ApplyFixture(root)

            def move_remote_then_fail(repo_root: Path, evidence_ref: str, _commit_sha: str) -> None:
                tree_sha = fixture.git("rev-parse", f"{fixture.base_sha}^{{tree}}")
                sibling_sha = subprocess.run(
                    ["git", "commit-tree", tree_sha, "-p", fixture.base_sha, "-m", "unrelated"],
                    cwd=repo_root,
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.strip()
                subprocess.run(
                    ["git", "push", "origin", f"{sibling_sha}:refs/heads/{evidence_ref}"],
                    cwd=repo_root,
                    check=True,
                    capture_output=True,
                )
                raise QualificationApplyError("simulated non-fast-forward race")

            with (
                patch("scripts.release_qualification_apply._validate_origin"),
                patch(
                    "scripts.release_qualification_apply._push_commit",
                    side_effect=move_remote_then_fail,
                ),
            ):
                with self.assertRaisesRegex(QualificationApplySafetyError, "moved while"):
                    fixture.start()

    def test_checkpoint_removal_interruption_adopts_pushed_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "work"
            root.mkdir()
            fixture = ApplyFixture(root)

            with patch("pathlib.Path.unlink", side_effect=OSError("simulated checkpoint removal interruption")):
                with self.assertRaisesRegex(QualificationApplyError, "remove completed"):
                    fixture.start()
            checkpoint = load_reconciliation_checkpoint(fixture.checkpoint)
            if checkpoint is None:
                self.fail("pushed apply checkpoint was not preserved")
            self.assertEqual(checkpoint["schema_version"], 2)
            self.assertEqual(checkpoint["progress"]["state"], "pushed")
            self.assertNotIn("comment_id", checkpoint["progress"])
            self.assertEqual(stat.S_IMODE(fixture.checkpoint.stat().st_mode), 0o600)

            outcome = fixture.continue_apply()

            self.assertEqual(outcome.state, "reconciliation_applied")
            self.assertFalse(fixture.checkpoint.exists())

    def test_commit_created_before_checkpoint_update_is_adopted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "work"
            root.mkdir()
            fixture = ApplyFixture(root)

            def fail_committed_transition(path, checkpoint, *, state, commit_sha):
                if state == "committed":
                    raise QualificationApplyError("simulated checkpoint write interruption")
                return _replace_progress(
                    path,
                    checkpoint,
                    state=state,
                    commit_sha=commit_sha,
                )

            with (
                patch("scripts.release_qualification_apply._validate_origin"),
                patch(
                    "scripts.release_qualification_apply._replace_progress",
                    side_effect=fail_committed_transition,
                ),
            ):
                with self.assertRaisesRegex(QualificationApplyError, "checkpoint write interruption"):
                    fixture.start()
            checkpoint = load_reconciliation_checkpoint(fixture.checkpoint)
            if checkpoint is None:
                self.fail("files-written apply checkpoint was not preserved")
            self.assertEqual(checkpoint["progress"]["state"], "files_written")
            self.assertNotEqual(fixture.git("rev-parse", "HEAD"), fixture.base_sha)

            outcome = fixture.continue_apply()

            self.assertEqual(outcome.state, "reconciliation_applied")

    def test_active_run_blocks_before_checkpoint_or_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "work"
            root.mkdir()
            fixture = ApplyFixture(root)

            def active() -> None:
                raise QualificationApplySafetyError("active run")

            with patch("scripts.release_qualification_apply._validate_origin"):
                with self.assertRaisesRegex(QualificationApplySafetyError, "active run"):
                    start_reconciliation_apply(
                        repo_root=root,
                        identity=fixture.identity,
                        bundle=fixture.bundle,
                        expected_plan_sha256=cast(str, fixture.bundle.plan["plan_sha256"]),
                        checkpoint_path=fixture.checkpoint,
                        revalidate_remote=fixture.revalidate,
                        ensure_no_active_runs=active,
                        require_actor=fixture.require_actor,
                        remote_evidence_sha=fixture.remote_sha,
                    )

            self.assertFalse(fixture.checkpoint.exists())
            self.assertEqual(fixture.git("status", "--short"), "")

    def test_unrelated_worktree_change_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "work"
            root.mkdir()
            fixture = ApplyFixture(root)
            (root / "unrelated.txt").write_text("conflict", encoding="utf-8")

            with self.assertRaisesRegex(QualificationApplySafetyError, "clean evidence worktree"):
                fixture.start()

            self.assertFalse(fixture.checkpoint.exists())

    def test_checkpoint_tampering_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "work"
            root.mkdir()
            fixture = ApplyFixture(root)
            with patch("pathlib.Path.unlink", side_effect=OSError("simulated checkpoint removal interruption")):
                with self.assertRaises(QualificationApplyError):
                    fixture.start()
            payload = json.loads(fixture.checkpoint.read_text(encoding="utf-8"))
            payload["plan_sha256"] = "0" * 64
            fixture.checkpoint.write_text(json.dumps(payload), encoding="utf-8")
            fixture.checkpoint.chmod(0o600)

            with self.assertRaisesRegex(QualificationApplySafetyError, "self digest"):
                fixture.continue_apply()

    def test_commit_message_bytes_must_match_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "work"
            root.mkdir()
            fixture = ApplyFixture(root)
            for file in fixture.files:
                path = root / file.path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(file.content)
            subprocess.run(["git", "add", "--", *(file.path for file in fixture.files)], cwd=root, check=True)
            tree_sha = fixture.git("write-tree")
            environment = dict(os.environ)
            expected_email = f"{ACTOR_ID}+cbusillo@users.noreply.github.com"
            environment.update(
                {
                    "GIT_AUTHOR_NAME": "cbusillo",
                    "GIT_AUTHOR_EMAIL": expected_email,
                    "GIT_COMMITTER_NAME": "cbusillo",
                    "GIT_COMMITTER_EMAIL": expected_email,
                }
            )
            malformed_message = _commit_message(fixture.bundle.plan) + "\n"
            commit_sha = subprocess.run(
                ["git", "commit-tree", tree_sha, "-p", fixture.base_sha],
                cwd=root,
                input=malformed_message,
                capture_output=True,
                text=True,
                env=environment,
                check=True,
            ).stdout.strip()

            with self.assertRaisesRegex(QualificationApplySafetyError, "commit message conflicts"):
                _validate_commit(
                    root,
                    commit_sha=commit_sha,
                    base_sha=fixture.base_sha,
                    plan=fixture.bundle.plan,
                    files={file.path: file.content for file in fixture.files},
                    actor_login="cbusillo",
                    actor_id=ACTOR_ID,
                )

    def test_push_uses_active_gh_credential_without_token_environment(self) -> None:
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with (
            patch("scripts.release_qualification_apply._run_git", return_value=completed) as run_git,
            patch.dict(os.environ, {"GH_TOKEN": "wrong-token", "GITHUB_TOKEN": "wrong-token"}),
        ):
            _push_commit(Path("/tmp/repo"), EVIDENCE_REF, "9" * 40)

        arguments = run_git.call_args.args[1]
        environment = run_git.call_args.kwargs["env"]
        self.assertIn("credential.helper=!gh auth git-credential", arguments)
        self.assertIn("https://github.com/cbusillo/BD_to_AVP.git", arguments)
        self.assertNotIn("GH_TOKEN", environment)
        self.assertNotIn("GITHUB_TOKEN", environment)


if __name__ == "__main__":
    unittest.main()
