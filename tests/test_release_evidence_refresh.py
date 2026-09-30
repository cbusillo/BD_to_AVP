"""Refreshing a release's evidence branch onto a main that moved on.

The scenarios are rebuilt from real repository history: an evidence branch
exactly as Release Evidence pushed it, and a protected main that has since
prepared the next release (carrying this release's receipt) and appended
another release's records. The refreshed branch must pass the same validators
protected CI runs on the evidence pull request.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
import unittest.mock

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from scripts.release_evidence import (
    EVIDENCE_INDEX_PATH,
    RELEASE_LEDGER_PATH,
    merge_evidence_receipts,
    merge_release_ledger_record,
    render_published_cut_packet,
)
from scripts.release_evidence_refresh import ReleaseEvidenceRefreshError, refresh_evidence_branch
from scripts.release_evidence_v2 import (
    QUALIFICATION_NAME,
    build_index_v2,
    check_index_v2,
    write_index_v2,
)
from scripts.release_milestone_context import (
    EXPECTED_REPOSITORY,
    QUALIFICATION_V2_PATH_PATTERN,
    RELEASE_V2_INDEX_PATH,
    discover_milestone_manifest,
    discover_terminal_v2_qualification,
    main as milestone_context_main,
)
from scripts.release_qualification_manifest import TERMINAL_RECORD_NAMES


REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ".github/github.json"
EVIDENCE_INDEX = EVIDENCE_INDEX_PATH.as_posix()
RELEASE_LEDGER = RELEASE_LEDGER_PATH.as_posix()

# Immutable history: v0.3.3-beta.4's capture-only evidence commit and the main it was cut from,
# plus the commits whose files show what the next Beta's preparation and evidence looked like.
BETA4_TAG = "v0.3.3-beta.4"
BETA4_EVIDENCE = "90e43a5b180585d7cefe797521647c638e0f9446"
BETA4_BASE = "32ad56270e1dc2afe682f35323c6487d309e2df4"
BETA5_TAG = "v0.3.3-beta.5"
BETA5_PREPARATION = "162877129189569f87ad2714af30978e2be1e1fe"
BETA5_EVIDENCE_ON_MAIN = "cc9855e3b3c8b57c202772939c37023219ef5c21"
# v0.3.3's terminal (qualified) evidence branch head and the main it was cut from.
STABLE_TAG = "v0.3.3"
STABLE_TERMINAL = "211d61b8ab983809f5d2b52553c8b150e2cf049d"
STABLE_BASE = "2329c9dae3624f13fc20d242f784bdc8d21168c0"
SUCCESSOR_TAG = "v0.3.4-beta.1"


def git(root: Path, *arguments: str) -> str:
    return subprocess.run(["git", *arguments], cwd=root, capture_output=True, text=True, check=True).stdout.strip()


def show(root: Path, revision: str, path: str) -> bytes:
    return subprocess.run(["git", "show", f"{revision}:{path}"], cwd=root, capture_output=True, check=True).stdout


def copy_from(root: Path, revision: str, path: str) -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(show(root, revision, path))


def evidence_path(tag: str, name: str) -> str:
    return f"docs/release-evidence/{tag}/{name}"


def rolling_path(root: Path, revision: str) -> str:
    return json.loads(show(root, revision, CONFIG_PATH))["releaseOperations"]["qualificationRecordPath"]


def advance_rolling_path(root: Path, new_path: str) -> None:
    config_file = root / CONFIG_PATH
    text = config_file.read_text(encoding="utf-8")
    current = json.loads(text)["releaseOperations"]["qualificationRecordPath"]
    config_file.write_text(text.replace(json.dumps(current), json.dumps(new_path), 1), encoding="utf-8")


def commit_all(root: Path, message: str) -> str:
    write_index_v2(root, build_index_v2(root, worktree=True))
    git(root, "add", "--all", "--", "docs", ".github")
    git(root, "commit", "-q", "-m", message)
    return git(root, "rev-parse", "HEAD")


def receipts(root: Path, revision: str) -> list[dict]:
    return json.loads(show(root, revision, EVIDENCE_INDEX))["receipts"]


def prepare_successor_carrying_receipt(
    root: Path,
    *,
    base: str,
    carried_tag: str,
    carried_from: str,
    successor_tag: str,
    qualification_source: tuple[str, str],
) -> str:
    """Protected main prepares the next release and carries the prior release's exact checked receipt."""
    git(root, "checkout", "-q", "--detach", base)
    successor_record = f"docs/qualification/{successor_tag}-signed-qualification-v1.json"
    source_revision, source_path = qualification_source
    record = json.loads(show(root, source_revision, source_path))
    record["candidate"]["release_tag"] = successor_tag
    (root / successor_record).write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    advance_rolling_path(root, successor_record)
    copy_from(root, carried_from, evidence_path(carried_tag, "release-receipt.json"))
    return commit_all(root, f"Prepare {successor_tag} carrying {carried_tag}")


class RefreshScenario(unittest.TestCase):
    fixture: Path
    temporary: tempfile.TemporaryDirectory[str]
    environment: unittest.mock._patch_dict

    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        base = Path(cls.temporary.name)
        empty_config = base / "gitconfig"
        empty_config.write_text("", encoding="utf-8")
        cls.environment = unittest.mock.patch.dict(
            os.environ,
            {
                "GIT_CONFIG_GLOBAL": str(empty_config),
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_AUTHOR_NAME": "Evidence Test",
                "GIT_AUTHOR_EMAIL": "evidence@example.com",
                "GIT_COMMITTER_NAME": "Evidence Test",
                "GIT_COMMITTER_EMAIL": "evidence@example.com",
            },
        )
        cls.environment.start()
        cls.fixture = base / "fixture"
        subprocess.run(
            ["git", "clone", "--quiet", "--shared", "--no-checkout", REPO_ROOT.as_posix(), cls.fixture.as_posix()],
            check=True,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.environment.stop()
        cls.temporary.cleanup()

    def clone(self) -> Path:
        directory = Path(tempfile.mkdtemp(dir=self.temporary.name))
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        root = directory / "repo"
        subprocess.run(
            ["git", "clone", "--quiet", "--shared", "--no-checkout", self.fixture.as_posix(), root.as_posix()],
            check=True,
        )
        return root

    def build_beta4_main(self, root: Path) -> str:
        """Beta 5 is prepared carrying Beta 4's receipt, then Beta 5's own records land before Beta 4's."""
        prepare_successor_carrying_receipt(
            root,
            base=BETA4_BASE,
            carried_tag=BETA4_TAG,
            carried_from=BETA4_EVIDENCE,
            successor_tag=BETA5_TAG,
            qualification_source=(BETA5_PREPARATION, f"docs/qualification/{BETA5_TAG}-signed-qualification-v1.json"),
        )
        copy_from(root, BETA5_PREPARATION, f"docs/{BETA5_TAG.removeprefix('v')}-cut-packet.md")
        copied = {
            evidence_path(BETA5_TAG, "release-receipt.json"),
            evidence_path(BETA5_TAG, "publication-record.json"),
            f"docs/qualification/{BETA5_TAG}-change-scoped-evidence-v1.json",
        }
        for path in sorted(copied):
            copy_from(root, BETA5_EVIDENCE_ON_MAIN, path)
        ledger = json.loads(show(root, BETA5_EVIDENCE_ON_MAIN, RELEASE_LEDGER))
        merge_release_ledger_record(root, next(item for item in ledger["releases"] if item["tag"] == BETA5_TAG))
        merge_evidence_receipts(
            root,
            [
                receipt
                for receipt in receipts(root, BETA5_EVIDENCE_ON_MAIN)
                if receipt["receipt_id"].startswith(f"{BETA5_TAG}:") and receipt["reference"] in copied
            ],
        )
        return commit_all(root, f"Record {BETA5_TAG} evidence ahead of {BETA4_TAG}")

    def build_stable_main(self, root: Path, *, edit_cut_packet: bool = False) -> str:
        """The next Beta is prepared carrying v0.3.3's receipt and records change-scoped evidence."""
        prepare_successor_carrying_receipt(
            root,
            base=STABLE_BASE,
            carried_tag=STABLE_TAG,
            carried_from=STABLE_TERMINAL,
            successor_tag=SUCCESSOR_TAG,
            qualification_source=(STABLE_BASE, f"docs/qualification/{STABLE_TAG}-stable-signed-qualification-v1.json"),
        )
        successor_receipts = []
        for receipt in receipts(root, BETA5_EVIDENCE_ON_MAIN):
            if receipt["reference"] == f"docs/qualification/{BETA5_TAG}-change-scoped-evidence-v1.json":
                successor = json.loads(json.dumps(receipt).replace(BETA5_TAG, SUCCESSOR_TAG))
                successor_receipts.append(successor)
        change_scoped = f"docs/qualification/{SUCCESSOR_TAG}-change-scoped-evidence-v1.json"
        (root / change_scoped).write_bytes(
            show(root, BETA5_EVIDENCE_ON_MAIN, f"docs/qualification/{BETA5_TAG}-change-scoped-evidence-v1.json")
        )
        merge_evidence_receipts(root, successor_receipts)
        if edit_cut_packet:
            cut_packet = root / f"docs/{STABLE_TAG.removeprefix('v')}-cut-packet.md"
            text = cut_packet.read_text(encoding="utf-8")
            cut_packet.write_text(text.replace("\n", "\n\nOperator note added on main.\n", 1), encoding="utf-8")
        return commit_all(root, f"Record {SUCCESSOR_TAG} change-scoped evidence")

    def refresh(self, root: Path, tag: str, branch: str, main: str):
        return refresh_evidence_branch(root, release_tag=tag, branch_ref=branch, main_ref=main)

    def assert_contains_main_with_history(self, root: Path, branch: str, main: str) -> None:
        self.assertEqual(git(root, "rev-list", "--parents", "-n", "1", "HEAD").split()[1:], [branch, main])
        self.assertEqual(git(root, "merge-base", "HEAD", main), main)
        self.assertEqual(git(root, "status", "--porcelain", "--untracked-files=all"), "")

    def assert_refused(self, root: Path, tag: str, branch: str, main: str, message: str) -> None:
        head = git(root, "rev-parse", "HEAD")
        with self.assertRaisesRegex(ReleaseEvidenceRefreshError, message):
            self.refresh(root, tag, branch, main)
        self.assertEqual(git(root, "rev-parse", "HEAD"), head)
        self.assertEqual(git(root, "status", "--porcelain", "--untracked-files=all"), "")

    def commit_on(self, root: Path, revision: str, path: str, content: bytes, message: str) -> str:
        git(root, "checkout", "-q", "--detach", revision)
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_bytes(content)
        git(root, "add", "--", path)
        git(root, "commit", "-q", "-m", message)
        return git(root, "rev-parse", "HEAD")


class CaptureOnlyRefreshTests(RefreshScenario):
    def test_textual_merge_conflicts_but_the_refreshed_branch_passes_the_evidence_pull_request_gate(self) -> None:
        root = self.clone()
        main = self.build_beta4_main(root)
        git(root, "checkout", "-q", "--detach", BETA4_EVIDENCE)
        merged = subprocess.run(["git", "merge", "--no-edit", main], cwd=root, capture_output=True, check=False)
        self.assertNotEqual(merged.returncode, 0)
        conflicts = set(git(root, "diff", "--name-only", "--diff-filter=U").splitlines())
        self.assertLessEqual({EVIDENCE_INDEX, RELEASE_V2_INDEX_PATH, RELEASE_LEDGER}, conflicts)
        git(root, "merge", "--abort")
        git(root, "checkout", "-q", "--detach", main)

        result = self.refresh(root, BETA4_TAG, BETA4_EVIDENCE, main)

        self.assertTrue(result.refreshed)
        self.assertFalse(result.terminal)
        self.assertEqual(result.head_sha, git(root, "rev-parse", "HEAD"))
        self.assert_contains_main_with_history(root, BETA4_EVIDENCE, main)
        manifest = discover_milestone_manifest(
            root,
            base_sha=main,
            head_sha=result.head_sha,
            head_branch=f"automation/release-evidence-{BETA4_TAG}",
            base_repo=EXPECTED_REPOSITORY,
            head_repo=EXPECTED_REPOSITORY,
            base_branch="main",
        )
        self.assertEqual(manifest, root / evidence_path(BETA4_TAG, "qualification-manifest.json"))
        check_index_v2(root, worktree=True)
        branch_receipts = [item["receipt_id"] for item in receipts(root, BETA4_EVIDENCE)]
        base_receipts = {item["receipt_id"] for item in receipts(root, BETA4_BASE)}
        self.assertEqual(
            [item["receipt_id"] for item in receipts(root, "HEAD")],
            [item["receipt_id"] for item in receipts(root, main)]
            + [receipt_id for receipt_id in branch_receipts if receipt_id not in base_receipts],
        )
        self.assertEqual(show(root, "HEAD", rolling_path(root, main)), show(root, main, rolling_path(root, main)))
        shared = {EVIDENCE_INDEX, RELEASE_LEDGER, RELEASE_V2_INDEX_PATH}
        for path in set(git(root, "diff", "--name-only", BETA4_BASE, BETA4_EVIDENCE).splitlines()) - shared:
            self.assertEqual(show(root, "HEAD", path), show(root, BETA4_EVIDENCE, path), path)

    def test_rerunning_on_a_fresh_branch_changes_nothing(self) -> None:
        root = self.clone()
        main = self.build_beta4_main(root)
        first = self.refresh(root, BETA4_TAG, BETA4_EVIDENCE, main)

        second = self.refresh(root, BETA4_TAG, first.head_sha, main)

        self.assertFalse(second.refreshed)
        self.assertEqual(second.head_sha, first.head_sha)
        self.assertEqual(git(root, "rev-parse", "HEAD"), first.head_sha)
        self.assertEqual(git(root, "status", "--porcelain", "--untracked-files=all"), "")

    def test_a_branch_change_outside_the_release_is_refused(self) -> None:
        root = self.clone()
        main = self.build_beta4_main(root)
        for path in (
            "README.md",
            evidence_path(BETA5_TAG, "note.json"),
            f"docs/{BETA5_TAG.removeprefix('v')}-cut-packet.md",
        ):
            with self.subTest(path=path):
                branch = self.commit_on(root, BETA4_EVIDENCE, path, b"not this release\n", "Stray change")
                git(root, "checkout", "-q", "--detach", main)
                self.assert_refused(root, BETA4_TAG, branch, main, "does not own")

    def test_release_owned_bytes_that_differ_on_main_are_refused(self) -> None:
        root = self.clone()
        prepared = self.build_beta4_main(root)
        receipt = evidence_path(BETA4_TAG, "release-receipt.json")
        main = self.commit_on(root, prepared, receipt, show(root, prepared, receipt) + b"\n", "Rewrite carried receipt")
        self.assert_refused(root, BETA4_TAG, BETA4_EVIDENCE, main, "changed on both main and the evidence branch")

    def test_a_receipt_id_that_main_already_holds_with_other_content_is_refused(self) -> None:
        root = self.clone()
        prepared = self.build_beta4_main(root)
        base_ids = {item["receipt_id"] for item in receipts(root, BETA4_BASE)}
        branch_receipt = next(item for item in receipts(root, BETA4_EVIDENCE) if item["receipt_id"] not in base_ids)
        git(root, "checkout", "-q", "--detach", prepared)
        merge_evidence_receipts(root, [{**branch_receipt, "accepted_at": "2026-01-01T00:00:00Z"}])
        main = commit_all(root, "Main records a conflicting receipt")
        self.assert_refused(root, BETA4_TAG, BETA4_EVIDENCE, main, "differs between main and the evidence branch")

    def test_a_branch_receipt_for_another_release_is_refused(self) -> None:
        root = self.clone()
        main = self.build_beta4_main(root)
        index = json.loads(show(root, BETA4_EVIDENCE, EVIDENCE_INDEX))
        foreign = {**index["receipts"][-1], "receipt_id": f"{BETA5_TAG}:unreviewed-case"}
        index["receipts"].append(foreign)
        content = (json.dumps(index, indent=2, sort_keys=True) + "\n").encode()
        branch = self.commit_on(root, BETA4_EVIDENCE, EVIDENCE_INDEX, content, "Foreign receipt")
        git(root, "checkout", "-q", "--detach", main)
        self.assert_refused(root, BETA4_TAG, branch, main, "belongs to another release")

    def test_protected_main_wins_the_rolling_record_while_the_release_is_still_rolling(self) -> None:
        root = self.clone()
        rolling = rolling_path(root, BETA4_BASE)
        record = json.loads(show(root, BETA4_BASE, rolling))
        record["operator_note"] = "reviewed on main"
        main = self.commit_on(
            root, BETA4_BASE, rolling, (json.dumps(record, indent=2, sort_keys=True) + "\n").encode(), "Edit rolling"
        )

        self.refresh(root, BETA4_TAG, BETA4_EVIDENCE, main)

        self.assertEqual(rolling_path(root, main), rolling)
        self.assertEqual(show(root, "HEAD", rolling), show(root, main, rolling))
        self.assert_contains_main_with_history(root, BETA4_EVIDENCE, main)


class TerminalRefreshTests(RefreshScenario):
    def test_terminal_evidence_passes_the_protected_ci_entry_point_after_main_edits_the_cut_packet(self) -> None:
        root = self.clone()
        main = self.build_stable_main(root, edit_cut_packet=True)
        git(root, "checkout", "-q", "--detach", STABLE_TERMINAL)
        merged = subprocess.run(["git", "merge", "--no-edit", main], cwd=root, capture_output=True, check=False)
        self.assertNotEqual(merged.returncode, 0)
        git(root, "merge", "--abort")
        git(root, "checkout", "-q", "--detach", main)

        result = self.refresh(root, STABLE_TAG, STABLE_TERMINAL, main)

        self.assertTrue(result.terminal)
        self.assert_contains_main_with_history(root, STABLE_TERMINAL, main)
        head_branch = f"automation/release-evidence-{STABLE_TAG}"
        pull_request = {
            "base_sha": main,
            "head_sha": result.head_sha,
            "head_branch": head_branch,
            "base_repo": EXPECTED_REPOSITORY,
            "head_repo": EXPECTED_REPOSITORY,
            "base_branch": "main",
        }
        self.assertEqual(discover_terminal_v2_qualification(root, **pull_request), STABLE_TAG)
        output = StringIO()
        arguments = [f"--{key.replace('_', '-')}={value}" for key, value in pull_request.items()]
        with redirect_stdout(output):
            self.assertEqual(milestone_context_main([*arguments, "--repo-root", str(root)]), 0)
        self.assertEqual(json.loads(output.getvalue()), {"required": "false"})
        cut_packet = f"docs/{STABLE_TAG.removeprefix('v')}-cut-packet.md"
        self.assertEqual(
            show(root, "HEAD", cut_packet).decode(),
            render_published_cut_packet(
                show(root, main, cut_packet).decode(),
                json.loads(show(root, STABLE_TERMINAL, evidence_path(STABLE_TAG, "release-receipt.json"))),
                json.loads(show(root, STABLE_TERMINAL, evidence_path(STABLE_TAG, "publication-record.json"))),
            ),
        )
        self.assertEqual(show(root, "HEAD", rolling_path(root, main)), show(root, main, rolling_path(root, main)))

    def test_only_records_that_skip_manifest_freshness_freeze_the_manifest(self) -> None:
        # A frozen manifest can only land through the terminal gate, which does not require freshness.
        for name in TERMINAL_RECORD_NAMES:
            with self.subTest(record=name):
                self.assertIsNotNone(QUALIFICATION_V2_PATH_PATTERN.fullmatch(evidence_path(STABLE_TAG, name)))
        self.assertIn(QUALIFICATION_NAME, TERMINAL_RECORD_NAMES)


if __name__ == "__main__":
    unittest.main()
