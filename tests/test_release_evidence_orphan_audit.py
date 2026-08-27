from __future__ import annotations

import base64
import hashlib
import json
import unittest

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from scripts.release_evidence_orphan_audit import (
    ALERT_MARKER,
    ALERT_TITLE,
    EvidenceFinding,
    ReleaseEvidenceOrphanAuditError,
    run_audit,
)


REPOSITORY = "cbusillo/BD_to_AVP"
NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)


def sha(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class FakeGitHubRestClient:
    def __init__(self) -> None:
        self.repository = REPOSITORY
        self.responses: dict[str, object] = {}
        self.ref_pages: list[list[Mapping[str, Any]]] = [[]]
        self.issue_pages: list[list[Mapping[str, Any]]] = [[]]
        self.get_calls: list[tuple[str, bool]] = []
        self.created: list[Mapping[str, object]] = []
        self.updated: list[tuple[int, Mapping[str, object]]] = []

    def get(self, endpoint: str, *, paginate: bool = False) -> Any:
        self.get_calls.append((endpoint, paginate))
        if endpoint.endswith("git/matching-refs/heads/automation/release-evidence-"):
            return self.ref_pages
        if endpoint.endswith("issues?state=all&per_page=100"):
            return self.issue_pages
        return self.responses[endpoint]

    def create_issue(self, payload: Mapping[str, object]) -> Mapping[str, Any]:
        self.created.append(payload)
        return {"number": 901}

    def update_issue(self, number: int, payload: Mapping[str, object]) -> Mapping[str, Any]:
        self.updated.append((number, payload))
        return {"number": number}


class AuditFixture:
    def __init__(self) -> None:
        self.api = FakeGitHubRestClient()
        self.refs: list[Mapping[str, Any]] = []
        self.main_entries: list[Mapping[str, Any]] = []

    def add_bundle(
        self,
        tag: str,
        *,
        captured_at: datetime,
        terminal: str | None = None,
        on_main: bool = False,
        malformed: bool = False,
    ) -> None:
        ref_sha = sha(f"ref:{tag}")
        tree_sha = sha(f"tree:{tag}")
        prefix = f"docs/release-evidence/{tag}"
        capture_digest = digest(f"capture:{tag}")
        capture: dict[str, object] = {
            "capture_sha256": capture_digest,
            "captured_at": captured_at.isoformat().replace("+00:00", "Z"),
            "record_type": "capture",
            "release_tag": tag,
            "schema_version": 2,
            "source_sha": sha(f"source:{tag}"),
            "state": "CAPTURED",
        }
        if malformed:
            capture["state"] = "BROKEN"
        records: dict[str, Mapping[str, object]] = {"capture-v2.json": capture}
        if terminal is not None:
            record_type, state, filename = {
                "qualified": ("qualification", "QUALIFIED", "qualification-v2.json"),
                "failed": ("disposition", "FAILED", "disposition-v2.json"),
            }[terminal]
            records[filename] = {
                "capture": {"path": f"{prefix}/capture-v2.json", "sha256": capture_digest},
                "record_type": record_type,
                "release_tag": tag,
                "schema_version": 2,
                "source_sha": sha(f"source:{tag}"),
                "state": state,
            }
        entries: list[Mapping[str, Any]] = []
        for filename, record in records.items():
            blob_sha = sha(f"blob:{tag}:{filename}")
            path = f"{prefix}/{filename}"
            entries.append({"path": path, "sha": blob_sha, "type": "blob"})
            self.api.responses[f"repos/{REPOSITORY}/git/blobs/{blob_sha}"] = {
                "content": base64.b64encode(json.dumps(record).encode("utf-8")).decode("ascii"),
                "encoding": "base64",
            }
        self.api.responses[f"repos/{REPOSITORY}/git/commits/{ref_sha}"] = {
            "committer": {"date": captured_at.isoformat().replace("+00:00", "Z")},
            "tree": {"sha": tree_sha},
        }
        self.api.responses[f"repos/{REPOSITORY}/git/trees/{tree_sha}?recursive=1"] = {
            "tree": entries,
            "truncated": False,
        }
        self.refs.append({"object": {"sha": ref_sha}, "ref": f"refs/heads/automation/release-evidence-{tag}"})
        if on_main:
            self.main_entries.extend(entries)

    def add_invalid_ref(self, suffix: str) -> None:
        ref_sha = sha(f"ref:{suffix}")
        self.api.responses[f"repos/{REPOSITORY}/git/commits/{ref_sha}"] = {
            "committer": {"date": (NOW - timedelta(days=3)).isoformat().replace("+00:00", "Z")},
            "tree": {"sha": sha(f"tree:{suffix}")},
        }
        self.refs.append({"object": {"sha": ref_sha}, "ref": f"refs/heads/automation/release-evidence-{suffix}"})

    def finalize(self) -> FakeGitHubRestClient:
        main_sha = sha("main")
        main_tree_sha = sha("main-tree")
        self.api.responses[f"repos/{REPOSITORY}/git/ref/heads/main"] = {"object": {"sha": main_sha}}
        self.api.responses[f"repos/{REPOSITORY}/git/commits/{main_sha}"] = {
            "committer": {"date": NOW.isoformat().replace("+00:00", "Z")},
            "tree": {"sha": main_tree_sha},
        }
        self.api.responses[f"repos/{REPOSITORY}/git/trees/{main_tree_sha}?recursive=1"] = {
            "tree": self.main_entries,
            "truncated": False,
        }
        self.api.ref_pages = [self.refs]
        return self.api


class ReleaseEvidenceOrphanAuditTests(unittest.TestCase):
    def test_reconciled_branch_allows_index_evolution(self) -> None:
        fixture = AuditFixture()
        fixture.add_bundle("v1.0.0", captured_at=NOW - timedelta(days=10), terminal="qualified", on_main=True)
        fixture.main_entries.append(
            {"path": "docs/release-evidence/index-v2.json", "sha": sha("later-index"), "type": "blob"}
        )
        report = run_audit(fixture.finalize(), now=NOW)

        self.assertEqual(report.findings[0].classification, "reconciled")
        self.assertEqual(report.findings[0].bundle_state, "qualified")
        self.assertEqual(report.alert_action, "clear")

    def test_recent_captured_branch_does_not_alert_before_threshold(self) -> None:
        fixture = AuditFixture()
        fixture.add_bundle("v1.0.1", captured_at=NOW - timedelta(hours=71))
        report = run_audit(fixture.finalize(), now=NOW)

        self.assertEqual(report.findings[0].classification, "recent")
        self.assertEqual(report.findings[0].bundle_state, "captured")
        self.assertEqual(report.alert_action, "clear")

    def test_stale_orphan_opens_one_actionable_alert(self) -> None:
        fixture = AuditFixture()
        fixture.add_bundle("v1.0.2", captured_at=NOW - timedelta(hours=72), terminal="failed")
        api = fixture.finalize()
        report = run_audit(api, now=NOW)

        self.assertEqual(report.findings[0].classification, "stale_orphan")
        self.assertEqual(report.findings[0].bundle_state, "failed")
        self.assertEqual(report.alert_action, "open")
        self.assertEqual(api.created[0]["assignees"], ["cbusillo"])
        self.assertIn("automation/release-evidence-v1.0.2", str(api.created[0]["body"]))
        self.assertIn("72.0h", str(api.created[0]["body"]))

    def test_malformed_bundle_alerts_without_reconciliation(self) -> None:
        fixture = AuditFixture()
        fixture.add_bundle("v1.0.3", captured_at=NOW - timedelta(days=4), malformed=True)
        api = fixture.finalize()
        report = run_audit(api, now=NOW)

        finding = report.findings[0]
        self.assertEqual(finding.classification, "malformed")
        self.assertEqual(finding.age_hours, 96.0)
        self.assertIn("Do not reconcile", finding.remediation)
        self.assertEqual(report.alert_action, "open")

    def test_noncanonical_ref_is_malformed_with_commit_age(self) -> None:
        fixture = AuditFixture()
        fixture.add_invalid_ref("not-a-release-tag-")
        report = run_audit(fixture.finalize(), now=NOW)

        self.assertEqual(report.findings[0].classification, "malformed")
        self.assertEqual(report.findings[0].age_hours, 72.0)

    def test_duplicate_alert_issues_refuse_ambiguous_ownership(self) -> None:
        fixture = AuditFixture()
        fixture.add_bundle("v1.0.4", captured_at=NOW - timedelta(days=4))
        api = fixture.finalize()
        api.issue_pages = [
            [
                {"body": ALERT_MARKER, "number": 20, "state": "open", "title": ALERT_TITLE},
                {"body": ALERT_MARKER, "number": 21, "state": "open", "title": ALERT_TITLE},
            ]
        ]

        with self.assertRaisesRegex(ReleaseEvidenceOrphanAuditError, "Ambiguous duplicate"):
            run_audit(api, now=NOW)
        self.assertEqual(api.created, [])
        self.assertEqual(api.updated, [])

    def test_adopts_one_legacy_alert_issue(self) -> None:
        fixture = AuditFixture()
        fixture.add_bundle("v1.0.5", captured_at=NOW - timedelta(days=4))
        api = fixture.finalize()
        api.issue_pages = [
            [
                {"assignees": [], "body": "legacy", "number": 22, "state": "open", "title": ALERT_TITLE},
            ]
        ]
        report = run_audit(api, now=NOW)

        self.assertEqual(report.alert_action, "adopt")
        self.assertEqual(api.updated[0][0], 22)
        self.assertIn(ALERT_MARKER, str(api.updated[0][1]["body"]))

    def test_updates_one_managed_alert_and_closes_it_when_clear(self) -> None:
        stale_fixture = AuditFixture()
        stale_fixture.add_bundle("v1.0.6", captured_at=NOW - timedelta(days=4))
        stale_api = stale_fixture.finalize()
        stale_api.issue_pages = [
            [
                {"assignees": [], "body": ALERT_MARKER, "number": 23, "state": "open", "title": ALERT_TITLE},
            ]
        ]
        stale_report = run_audit(stale_api, now=NOW)

        self.assertEqual(stale_report.alert_action, "update")
        self.assertEqual(stale_api.updated[0][0], 23)

        clear_fixture = AuditFixture()
        clear_fixture.add_bundle("v1.0.7", captured_at=NOW - timedelta(days=4), terminal="qualified", on_main=True)
        clear_api = clear_fixture.finalize()
        clear_api.issue_pages = [
            [
                {
                    "assignees": [{"login": "cbusillo"}],
                    "body": ALERT_MARKER,
                    "number": 23,
                    "state": "open",
                    "title": ALERT_TITLE,
                },
            ]
        ]
        clear_report = run_audit(clear_api, now=NOW)

        self.assertEqual(clear_report.alert_action, "clear")
        self.assertEqual(clear_api.updated, [(23, {"state": "closed"})])

    def test_pagination_and_workflow_never_use_automation_ref_code(self) -> None:
        fixture = AuditFixture()
        fixture.add_bundle("v1.0.8", captured_at=NOW - timedelta(days=4))
        fixture.add_bundle("v1.0.9", captured_at=NOW - timedelta(hours=1))
        api = fixture.finalize()
        api.ref_pages = [[fixture.refs[0]], [fixture.refs[1]]]
        api.issue_pages = [[], []]
        report = run_audit(api, now=NOW)

        self.assertEqual({finding.classification for finding in report.findings}, {"recent", "stale_orphan"})
        paginated = {endpoint for endpoint, paginate in api.get_calls if paginate}
        self.assertIn(f"repos/{REPOSITORY}/git/matching-refs/heads/automation/release-evidence-", paginated)
        self.assertIn(f"repos/{REPOSITORY}/issues?state=all&per_page=100", paginated)

        workflow = (Path(__file__).parents[1] / ".github/workflows/release-evidence-orphan-audit.yml").read_text(
            encoding="utf-8"
        )
        helper = (Path(__file__).parents[1] / "scripts/release_evidence_orphan_audit.py").read_text(encoding="utf-8")
        self.assertIn("if: github.ref == 'refs/heads/main'", workflow)
        self.assertIn("ref: main", workflow)
        self.assertNotIn("git fetch", workflow.lower())
        self.assertNotIn("automation/", workflow)
        self.assertNotIn('["git"', helper)
        self.assertIn("git/trees", helper)
        self.assertIn("git/blobs", helper)

    def test_finding_serialization_keeps_actionable_evidence(self) -> None:
        finding = EvidenceFinding(
            ref="refs/heads/automation/release-evidence-v1.2.3",
            sha="a" * 40,
            age_hours=72.04,
            classification="stale_orphan",
            bundle_state="qualified",
            captured_at="2026-08-24T11:57:36Z",
            reason="valid terminal v2 evidence (qualified)",
            remediation="reconcile",
        )

        self.assertEqual(finding.as_dict()["age_hours"], 72.0)


if __name__ == "__main__":
    unittest.main()
