import hashlib
import json
import subprocess
import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from scripts.release import CUT_PACKET_PREPARED
from scripts.release_evidence import render_published_cut_packet
from scripts.release_evidence_reconcile import ReleaseEvidenceReconciliationError, _verify_docs_only_diff
from scripts.release_milestone_context import (
    EVIDENCE_INDEX_PATH,
    RELEASE_LEDGER_PATH,
    ReleaseMilestoneContextError,
    discover_terminal_v2_qualification,
    resolve_milestone_context,
    validate_terminal_v2_diff,
)
from tests import test_release_milestone_context as fixtures


TAG = "v0.3.0"


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


class ReleaseEvidenceCompatibilityTests(unittest.TestCase):
    @staticmethod
    def prepare(root: Path, *, existing_receipt: bool = False):
        receipt = fixtures.ReleaseMilestoneContextTests.build_repository(root)
        context = resolve_milestone_context(root, receipt)
        index = {"schema_version": 1, "receipts": [{"receipt_id": "preserved-history"}]}
        reference = f"docs/qualification/{TAG}-clean-machine-signed-update-v1.json"
        if existing_receipt:
            write_json(root / reference, {"result": "earlier"})
        cut_packet = root / "docs/0.3.0-cut-packet.md"
        cut_packet.write_text(f"# Release {TAG}\n\n{CUT_PACKET_PREPARED}\n")
        write_json(root / EVIDENCE_INDEX_PATH, index)
        git(root, "add", ".")
        git(root, "commit", "-qm", "accepted history")
        base = git(root, "rev-parse", "HEAD")
        write_json(root / reference, {"result": "passed"})
        bundle = root / f"docs/release-evidence/{TAG}"
        (bundle / "clean-machine-signed-update-receipt.json").write_bytes((root / reference).read_bytes())
        index["receipts"].append(
            {
                "receipt_id": f"{TAG}:clean-machine-signed-update:201",
                "case_id": "clean-machine-signed-update",
                "source_sha": context.candidate_sha,
                "status": "accepted",
                "reference": reference,
                "sha256": hashlib.sha256((root / reference).read_bytes()).hexdigest(),
            }
        )
        write_json(root / EVIDENCE_INDEX_PATH, index)
        write_json(
            root / RELEASE_LEDGER_PATH,
            {"schema_version": 1, "releases": [fixtures.ReleaseMilestoneContextTests.release_ledger_record(root)]},
        )
        cut_packet.write_text(
            render_published_cut_packet(
                cut_packet.read_text(),
                json.loads(receipt.read_text()),
                json.loads((bundle / "publication-record.json").read_text()),
            )
        )
        qualification = f"docs/qualification/{TAG}-signed-qualification-v1.json"
        (root / qualification).write_bytes((bundle / "qualification-record.json").read_bytes())
        write_json(bundle / "qualification-v2.json", {"state": "QUALIFIED"})
        write_json(root / "docs/release-evidence/index-v2.json", {})
        git(root, "add", ".")
        git(root, "commit", "-qm", "producer compatibility records and terminal evidence")
        paths = git(root, "diff", "--name-only", base, "HEAD").splitlines()
        return context, base, paths, index, qualification

    def test_ci_admits_generated_compatibility_records_and_still_verifies_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context, base, _paths, _index, _qualification = self.prepare(root)
            verified = []

            def verify(repo, tag, revision):
                verified.append((repo, tag, revision))
                return {"class": "v2-qualified"}

            with patch("scripts.release_milestone_context.resolve_milestone_manifest_context", return_value=context):
                result = discover_terminal_v2_qualification(
                    root,
                    base_sha=base,
                    head_sha=git(root, "rev-parse", "HEAD"),
                    head_branch=f"automation/release-evidence-{TAG}",
                    base_repo="cbusillo/BD_to_AVP",
                    head_repo="cbusillo/BD_to_AVP",
                    base_branch="main",
                    qualified_v2_verifier=verify,
                )
            self.assertEqual(result, TAG)
            self.assertEqual(verified, [(root, TAG, base)])

    def test_unrelated_docs_and_other_release_aliases_remain_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _context, base, paths, _index, _qualification = self.prepare(root)
            for extra in ["docs/unrelated.md", "docs/qualification/v0.3.1-live-qualification-v1.json"]:
                with self.subTest(extra=extra), self.assertRaisesRegex(ReleaseMilestoneContextError, "outside"):
                    validate_terminal_v2_diff(root, TAG, base, [*paths, extra])

    def test_accepted_history_cannot_be_rewritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context, base, paths, index, _qualification = self.prepare(root)
            index["receipts"][0]["receipt_id"] = "rewritten"
            write_json(root / EVIDENCE_INDEX_PATH, index)
            with (
                patch("scripts.release_milestone_context.resolve_milestone_manifest_context", return_value=context),
                self.assertRaisesRegex(ReleaseMilestoneContextError, "accepted history"),
            ):
                validate_terminal_v2_diff(root, TAG, base, paths)

    def test_new_receipt_must_bind_candidate_release_and_file_digest(self) -> None:
        for field, value, message in [
            ("source_sha", "f" * 40, "belong to this release"),
            ("receipt_id", "v0.3.1:clean-machine-signed-update:201", "belong to this release"),
            ("reference", "docs/unrelated.json", "case identity"),
            ("case_id", "release-workflow-identity", "case identity"),
            ("sha256", "f" * 64, "digest"),
        ]:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                context, base, paths, index, _qualification = self.prepare(root)
                index["receipts"][-1][field] = value
                write_json(root / EVIDENCE_INDEX_PATH, index)
                with (
                    patch("scripts.release_milestone_context.resolve_milestone_manifest_context", return_value=context),
                    self.assertRaisesRegex(ReleaseMilestoneContextError, message),
                ):
                    validate_terminal_v2_diff(root, TAG, base, paths)

    def test_rolling_qualification_cannot_disagree_with_archived_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context, base, paths, _index, qualification = self.prepare(root)
            write_json(root / qualification, {"candidate": {"release_tag": "v0.3.1"}})
            with (
                patch("scripts.release_milestone_context.resolve_milestone_manifest_context", return_value=context),
                self.assertRaisesRegex(ReleaseMilestoneContextError, "immutable snapshot"),
            ):
                validate_terminal_v2_diff(root, TAG, base, paths)

    def test_compatibility_receipts_cannot_be_added_without_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context, base, paths, _index, _qualification = self.prepare(root)
            with (
                patch("scripts.release_milestone_context.resolve_milestone_manifest_context", return_value=context),
                self.assertRaisesRegex(ReleaseMilestoneContextError, "new accepted index records"),
            ):
                validate_terminal_v2_diff(root, TAG, base, [path for path in paths if path != EVIDENCE_INDEX_PATH])

    def test_evidence_index_metadata_cannot_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context, base, paths, index, _qualification = self.prepare(root)
            index["unexpected_metadata"] = True
            write_json(root / EVIDENCE_INDEX_PATH, index)
            with (
                patch("scripts.release_milestone_context.resolve_milestone_manifest_context", return_value=context),
                self.assertRaisesRegex(ReleaseMilestoneContextError, "metadata may not change"),
            ):
                validate_terminal_v2_diff(root, TAG, base, paths)

    def test_compatibility_receipt_cannot_replace_a_checked_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context, base, paths, _index, _qualification = self.prepare(root, existing_receipt=True)
            with (
                patch("scripts.release_milestone_context.resolve_milestone_manifest_context", return_value=context),
                self.assertRaisesRegex(ReleaseMilestoneContextError, "cannot be replaced"),
            ):
                validate_terminal_v2_diff(root, TAG, base, paths)

    def test_receipt_contents_must_match_even_when_index_digest_matches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context, base, paths, index, _qualification = self.prepare(root)
            path = root / index["receipts"][-1]["reference"]
            write_json(path, {"result": "failed", "candidate": "another release"})
            index["receipts"][-1]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            write_json(root / EVIDENCE_INDEX_PATH, index)
            with (
                patch("scripts.release_milestone_context.resolve_milestone_manifest_context", return_value=context),
                self.assertRaisesRegex(ReleaseMilestoneContextError, "validated archived receipt"),
            ):
                validate_terminal_v2_diff(root, TAG, base, paths)

    def test_cut_packet_must_match_publication_and_cannot_be_deleted(self) -> None:
        for deleted in [False, True]:
            with self.subTest(deleted=deleted), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                context, base, paths, _index, _qualification = self.prepare(root)
                cut_packet = root / "docs/0.3.0-cut-packet.md"
                if deleted:
                    cut_packet.unlink()
                else:
                    cut_packet.write_text("Another candidate\n")
                with (
                    patch("scripts.release_milestone_context.resolve_milestone_manifest_context", return_value=context),
                    self.assertRaisesRegex(ReleaseMilestoneContextError, "regular checked files|publication renderer"),
                ):
                    validate_terminal_v2_diff(root, TAG, base, paths)

    def test_operator_validates_committed_evidence_instead_of_dirty_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context, base, _paths, _index, _qualification = self.prepare(root)
            head = git(root, "rev-parse", "HEAD")
            cut_packet = root / "docs/0.3.0-cut-packet.md"
            cut_packet.write_text("Uncommitted conflicting content\n")
            with patch("scripts.release_milestone_context.resolve_milestone_manifest_context", return_value=context):
                _verify_docs_only_diff(root, base, head, TAG)
            self.assertEqual(cut_packet.read_text(), "Uncommitted conflicting content\n")
            git(root, "add", ".")
            git(root, "commit", "-qm", "invalid committed cut packet")
            with (
                patch("scripts.release_milestone_context.resolve_milestone_manifest_context", return_value=context),
                self.assertRaisesRegex(ReleaseEvidenceReconciliationError, "publication renderer"),
            ):
                _verify_docs_only_diff(root, base, git(root, "rev-parse", "HEAD"), TAG)


if __name__ == "__main__":
    unittest.main()
