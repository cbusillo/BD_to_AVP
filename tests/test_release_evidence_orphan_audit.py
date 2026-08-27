from __future__ import annotations

import base64
import hashlib
import json
import unittest

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from scripts.release_evidence_orphan_audit import (
    ALERT_MARKER,
    ALERT_TITLE,
    EvidenceFinding,
    GitHubRestTransportError,
    run_audit,
)


REPOSITORY = "cbusillo/BD_to_AVP"
NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)


def sha(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def alert_issue(
    number: int,
    *,
    body: str = ALERT_MARKER,
    state: str = "open",
    title: str = ALERT_TITLE,
    author: str = "github-actions[bot]",
    assignees: list[Mapping[str, str]] | None = None,
) -> dict[str, object]:
    return {
        "assignees": assignees or [],
        "body": body,
        "number": number,
        "state": state,
        "title": title,
        "user": {"login": author},
    }


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
        response = self.responses[endpoint]
        if isinstance(response, Exception):
            raise response
        return response

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
        wrap_blob_content: bool = False,
        both_terminals: bool = False,
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
        if both_terminals:
            records["qualification-v2.json"] = {
                "capture": {"path": f"{prefix}/capture-v2.json", "sha256": capture_digest},
                "record_type": "qualification",
                "release_tag": tag,
                "schema_version": 2,
                "source_sha": sha(f"source:{tag}"),
                "state": "QUALIFIED",
            }
            records["disposition-v2.json"] = {
                "capture": {"path": f"{prefix}/capture-v2.json", "sha256": capture_digest},
                "record_type": "disposition",
                "release_tag": tag,
                "schema_version": 2,
                "source_sha": sha(f"source:{tag}"),
                "state": "FAILED",
            }
        entries: list[Mapping[str, Any]] = []
        for filename, record in records.items():
            blob_sha = sha(f"blob:{tag}:{filename}")
            path = f"{prefix}/{filename}"
            entries.append({"path": path, "sha": blob_sha, "type": "blob"})
            encoded = base64.b64encode(json.dumps(record).encode("utf-8")).decode("ascii")
            if wrap_blob_content:
                encoded = "\n".join(encoded[index : index + 20] for index in range(0, len(encoded), 20))
            self.api.responses[f"repos/{REPOSITORY}/git/blobs/{blob_sha}"] = {
                "content": encoded,
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

    def add_legacy_bundle(self, tag: str, *, committed_at: datetime) -> None:
        ref_sha = sha(f"legacy-ref:{tag}")
        tree_sha = sha(f"legacy-tree:{tag}")
        self.api.responses[f"repos/{REPOSITORY}/git/commits/{ref_sha}"] = {
            "committer": {"date": committed_at.isoformat().replace("+00:00", "Z")},
            "tree": {"sha": tree_sha},
        }
        self.api.responses[f"repos/{REPOSITORY}/git/trees/{tree_sha}?recursive=1"] = {
            "tree": [
                {
                    "path": f"docs/release-evidence/{tag}/release-receipt.json",
                    "sha": sha(f"legacy-blob:{tag}"),
                    "type": "blob",
                }
            ],
            "truncated": False,
        }
        self.refs.append({"object": {"sha": ref_sha}, "ref": f"refs/heads/automation/release-evidence-{tag}"})

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
        fixture.main_entries.append(
            {
                "path": "docs/release-evidence/v1.0.0/later-annotation.json",
                "sha": sha("later-annotation"),
                "type": "blob",
            }
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
        self.assertNotIn("state", api.created[0])
        self.assertIn("automation/release-evidence-v1.0.2", str(api.created[0]["body"]))
        self.assertIn("72.0h", str(api.created[0]["body"]))
        self.assertIn("Do not run the qualified-evidence reconciliation helper", str(api.created[0]["body"]))

    def test_legacy_only_branch_is_ignored(self) -> None:
        fixture = AuditFixture()
        fixture.add_legacy_bundle("v0.3.2-beta.5", committed_at=NOW - timedelta(days=30))
        report = run_audit(fixture.finalize(), now=NOW)

        self.assertEqual(report.findings[0].classification, "legacy_ignored")
        self.assertEqual(report.findings[0].bundle_state, "legacy")
        self.assertEqual(report.alert_action, "clear")

    def test_branch_without_v2_or_legacy_marker_is_malformed(self) -> None:
        fixture = AuditFixture()
        tag = "v0.9.0"
        ref_sha = sha(f"empty-ref:{tag}")
        tree_sha = sha(f"empty-tree:{tag}")
        fixture.api.responses[f"repos/{REPOSITORY}/git/commits/{ref_sha}"] = {
            "committer": {"date": (NOW - timedelta(days=5)).isoformat().replace("+00:00", "Z")},
            "tree": {"sha": tree_sha},
        }
        fixture.api.responses[f"repos/{REPOSITORY}/git/trees/{tree_sha}?recursive=1"] = {
            "tree": [],
            "truncated": False,
        }
        fixture.refs.append({"object": {"sha": ref_sha}, "ref": f"refs/heads/automation/release-evidence-{tag}"})

        report = run_audit(fixture.finalize(), now=NOW)

        self.assertEqual(report.findings[0].classification, "malformed")
        self.assertIn("neither v2 records nor a recognized legacy", report.findings[0].reason)

    def test_line_wrapped_github_blob_content_is_supported(self) -> None:
        fixture = AuditFixture()
        fixture.add_bundle(
            "v1.0.10",
            captured_at=NOW - timedelta(hours=1),
            wrap_blob_content=True,
        )
        report = run_audit(fixture.finalize(), now=NOW)

        self.assertEqual(report.findings[0].classification, "recent")

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

    def test_future_capture_timestamp_is_malformed(self) -> None:
        fixture = AuditFixture()
        fixture.add_bundle("v1.0.11", captured_at=NOW + timedelta(minutes=6))
        report = run_audit(fixture.finalize(), now=NOW)

        self.assertEqual(report.findings[0].classification, "malformed")
        self.assertIn("more than five minutes in the future", report.findings[0].reason)

    def test_transport_failure_aborts_without_impeaching_evidence(self) -> None:
        fixture = AuditFixture()
        fixture.add_bundle("v1.0.12", captured_at=NOW - timedelta(days=4), terminal="qualified")
        api = fixture.finalize()
        blob_endpoint = next(endpoint for endpoint in api.responses if "/git/blobs/" in endpoint)
        api.responses[blob_endpoint] = GitHubRestTransportError("rate limited")

        with self.assertRaisesRegex(GitHubRestTransportError, "rate limited"):
            run_audit(api, now=NOW)
        self.assertEqual(api.created, [])

    def test_truncated_tree_aborts_the_audit(self) -> None:
        fixture = AuditFixture()
        fixture.add_bundle("v1.0.13", captured_at=NOW - timedelta(days=4))
        api = fixture.finalize()
        ref_sha = fixture.refs[0]["object"]["sha"]
        commit = api.responses[f"repos/{REPOSITORY}/git/commits/{ref_sha}"]
        self.assertIsInstance(commit, Mapping)
        commit_record = cast(Mapping[str, Any], commit)
        tree = commit_record["tree"]
        self.assertIsInstance(tree, Mapping)
        tree_sha = cast(Mapping[str, Any], tree)["sha"]
        api.responses[f"repos/{REPOSITORY}/git/trees/{tree_sha}?recursive=1"] = {
            "tree": [],
            "truncated": True,
        }

        with self.assertRaisesRegex(GitHubRestTransportError, "truncated tree"):
            run_audit(api, now=NOW)

    def test_split_brain_terminal_records_are_malformed(self) -> None:
        fixture = AuditFixture()
        fixture.add_bundle("v1.0.14", captured_at=NOW - timedelta(days=4), both_terminals=True)
        report = run_audit(fixture.finalize(), now=NOW)

        self.assertEqual(report.findings[0].classification, "malformed")
        self.assertIn("both terminal v2 records", report.findings[0].reason)

    def test_duplicate_alert_issues_refuse_ambiguous_ownership(self) -> None:
        fixture = AuditFixture()
        fixture.add_bundle("v1.0.4", captured_at=NOW - timedelta(days=4))
        api = fixture.finalize()
        api.issue_pages = [[alert_issue(20), alert_issue(21)]]

        report = run_audit(api, now=NOW)

        self.assertEqual(report.alert_action, "ambiguous")
        self.assertIsNone(report.alert_issue_number)
        self.assertEqual(api.created, [])
        self.assertEqual(api.updated, [])

    def test_untrusted_lookalike_issues_cannot_block_alert_creation(self) -> None:
        fixture = AuditFixture()
        fixture.add_bundle("v1.0.15", captured_at=NOW - timedelta(days=4))
        api = fixture.finalize()
        api.issue_pages = [
            [
                alert_issue(30, author="outsider"),
                alert_issue(31, author="outsider", body="lookalike", title=ALERT_TITLE),
            ]
        ]

        report = run_audit(api, now=NOW)

        self.assertEqual(report.alert_action, "open")
        self.assertEqual(len(api.created), 1)

    def test_adopts_one_legacy_alert_issue(self) -> None:
        fixture = AuditFixture()
        fixture.add_bundle("v1.0.5", captured_at=NOW - timedelta(days=4))
        api = fixture.finalize()
        api.issue_pages = [[alert_issue(22, body="legacy", author="cbusillo")]]
        report = run_audit(api, now=NOW)

        self.assertEqual(report.alert_action, "adopt")
        self.assertEqual(api.updated[0][0], 22)
        self.assertIn(ALERT_MARKER, str(api.updated[0][1]["body"]))

    def test_updates_one_managed_alert_and_closes_it_when_clear(self) -> None:
        stale_fixture = AuditFixture()
        stale_fixture.add_bundle("v1.0.6", captured_at=NOW - timedelta(days=4))
        stale_api = stale_fixture.finalize()
        stale_api.issue_pages = [[alert_issue(23)]]
        stale_report = run_audit(stale_api, now=NOW)

        self.assertEqual(stale_report.alert_action, "update")
        self.assertEqual(stale_api.updated[0][0], 23)

        clear_fixture = AuditFixture()
        clear_fixture.add_bundle("v1.0.7", captured_at=NOW - timedelta(days=4), terminal="qualified", on_main=True)
        clear_api = clear_fixture.finalize()
        clear_api.issue_pages = [[alert_issue(23, assignees=[{"login": "cbusillo"}])]]
        clear_report = run_audit(clear_api, now=NOW)

        self.assertEqual(clear_report.alert_action, "clear")
        self.assertEqual(clear_api.updated, [(23, {"state": "closed"})])

    def test_closed_managed_alert_reopens_when_findings_return(self) -> None:
        fixture = AuditFixture()
        fixture.add_bundle("v1.0.16", captured_at=NOW - timedelta(days=4))
        api = fixture.finalize()
        api.issue_pages = [[alert_issue(24, state="closed", assignees=[{"login": "cbusillo"}])]]

        report = run_audit(api, now=NOW)

        self.assertEqual(report.alert_action, "update")
        self.assertEqual(api.updated[0][0], 24)
        self.assertEqual(api.updated[0][1]["state"], "open")

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
        self.assertIn("uv sync --locked --all-groups --python 3.12", workflow)
        self.assertIn("uv run --frozen", workflow)
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
