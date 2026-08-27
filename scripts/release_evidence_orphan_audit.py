from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, cast

from scripts.release_evidence_v2 import (
    CAPTURE_NAME,
    DISPOSITION_NAME,
    EVIDENCE_ROOT,
    QUALIFICATION_NAME,
    ReleaseEvidenceV2Error,
    evidence_ref_for_tag,
    sanitize_release_tag,
)


MAIN_BRANCH = "main"
DEFAULT_THRESHOLD_HOURS = 72
DEFAULT_ALERT_OWNER = "cbusillo"
EVIDENCE_REF_PREFIX = "refs/heads/automation/release-evidence-"
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
ALERT_MARKER = "<!-- release-evidence-orphan-audit:v1 -->"
ALERT_TITLE = "Release evidence orphan audit requires attention"
GITHUB_TIMEOUT_SECONDS = 30
LEGACY_EVIDENCE_MARKERS = frozenset(
    {
        "failed-post-publication-qualification-v1.json",
        "publication-record.json",
        "release-receipt.json",
    }
)


class ReleaseEvidenceOrphanAuditError(RuntimeError):
    pass


class GitHubRestApi(Protocol):
    repository: str

    def get(self, endpoint: str, *, paginate: bool = False) -> Any: ...

    def create_issue(self, payload: Mapping[str, object]) -> Mapping[str, Any]: ...

    def update_issue(self, number: int, payload: Mapping[str, object]) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class EvidenceFinding:
    ref: str
    sha: str
    age_hours: float | None
    classification: str
    bundle_state: str
    captured_at: str | None
    reason: str
    remediation: str

    @property
    def alertable(self) -> bool:
        return self.classification in {"stale_orphan", "malformed"}

    def as_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["age_hours"] = None if self.age_hours is None else round(self.age_hours, 1)
        return result


@dataclass(frozen=True)
class AuditReport:
    findings: tuple[EvidenceFinding, ...]
    threshold_hours: int
    observed_at: str
    alert_action: str
    alert_issue_number: int | None

    def as_dict(self) -> dict[str, object]:
        counts: dict[str, int] = {}
        for finding in self.findings:
            counts[finding.classification] = counts.get(finding.classification, 0) + 1
        return {
            "alert_action": self.alert_action,
            "alert_issue_number": self.alert_issue_number,
            "findings": [finding.as_dict() for finding in self.findings],
            "observed_at": self.observed_at,
            "summary": counts,
            "threshold_hours": self.threshold_hours,
        }


class GitHubRestClient:
    def __init__(self, repository: str) -> None:
        self.repository = repository

    def get(self, endpoint: str, *, paginate: bool = False) -> Any:
        return self._request("GET", endpoint, paginate=paginate)

    def create_issue(self, payload: Mapping[str, object]) -> Mapping[str, Any]:
        return _mapping(
            self._request("POST", f"repos/{self.repository}/issues", payload=payload), "created alert issue"
        )

    def update_issue(self, number: int, payload: Mapping[str, object]) -> Mapping[str, Any]:
        return _mapping(
            self._request("PATCH", f"repos/{self.repository}/issues/{number}", payload=payload), "updated alert issue"
        )

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        paginate: bool = False,
        payload: Mapping[str, object] | None = None,
    ) -> Any:
        arguments = ["api", "--method", method]
        if paginate:
            arguments.extend(["--paginate", "--slurp"])
        serialized_payload: str | None = None
        if payload is not None:
            arguments.extend(["--input", "-"])
            serialized_payload = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        arguments.append(endpoint)
        try:
            result = subprocess.run(
                ["gh", *arguments],
                text=True,
                input=serialized_payload,
                capture_output=True,
                timeout=GITHUB_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise ReleaseEvidenceOrphanAuditError(
                f"Timed out while requesting GitHub REST endpoint {endpoint!r}."
            ) from error
        except OSError as error:
            raise ReleaseEvidenceOrphanAuditError("Unable to start gh for the GitHub REST audit.") from error
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic"
            raise ReleaseEvidenceOrphanAuditError(f"GitHub REST request {endpoint!r} failed: {detail}")
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise ReleaseEvidenceOrphanAuditError(
                f"GitHub REST endpoint {endpoint!r} returned invalid JSON."
            ) from error


def _mapping(value: object, description: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ReleaseEvidenceOrphanAuditError(f"{description} must be an object.")
    return cast(Mapping[str, Any], value)


def _sequence(value: object, description: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise ReleaseEvidenceOrphanAuditError(f"{description} must be an array.")
    return value


def _string(value: object, description: str) -> str:
    if not isinstance(value, str) or not value:
        raise ReleaseEvidenceOrphanAuditError(f"{description} must be a non-empty string.")
    return value


def _sha(value: object, description: str) -> str:
    candidate = _string(value, description)
    if SHA_PATTERN.fullmatch(candidate) is None:
        raise ReleaseEvidenceOrphanAuditError(f"{description} must be a full lowercase Git SHA.")
    return candidate


def _timestamp(value: object, description: str) -> datetime:
    candidate = _string(value, description)
    try:
        parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError as error:
        raise ReleaseEvidenceOrphanAuditError(f"{description} must be an ISO-8601 timestamp.") from error
    if parsed.tzinfo is None:
        raise ReleaseEvidenceOrphanAuditError(f"{description} must include a timezone.")
    return parsed.astimezone(UTC)


def _flatten_pages(value: object, description: str) -> list[Mapping[str, Any]]:
    pages = _sequence(value, description)
    entries: list[Mapping[str, Any]] = []
    for page in pages:
        if isinstance(page, list):
            entries.extend(_mapping(entry, description) for entry in page)
        else:
            entries.append(_mapping(page, description))
    return entries


def _commit(api: GitHubRestApi, repository: str, sha: str) -> Mapping[str, Any]:
    return _mapping(api.get(f"repos/{repository}/git/commits/{sha}"), f"commit {sha}")


def _tree_blobs(api: GitHubRestApi, repository: str, commit: Mapping[str, Any]) -> dict[str, str]:
    tree = _mapping(commit.get("tree"), "commit tree")
    tree_sha = _sha(tree.get("sha"), "commit tree SHA")
    response = _mapping(api.get(f"repos/{repository}/git/trees/{tree_sha}?recursive=1"), "recursive tree")
    if response.get("truncated") is True:
        raise ReleaseEvidenceOrphanAuditError(
            "GitHub returned a truncated tree; refusing incomplete orphan-audit evidence."
        )
    blobs: dict[str, str] = {}
    for entry in _sequence(response.get("tree"), "recursive tree entries"):
        item = _mapping(entry, "recursive tree entry")
        if item.get("type") != "blob":
            continue
        path = _string(item.get("path"), "tree blob path")
        blobs[path] = _sha(item.get("sha"), f"tree blob SHA for {path}")
    return blobs


def _bundle_blobs(tree_blobs: Mapping[str, str], tag: str) -> dict[str, str]:
    prefix = f"{EVIDENCE_ROOT}/{tag}/"
    return {path: sha for path, sha in tree_blobs.items() if path.startswith(prefix)}


def _blob_json(api: GitHubRestApi, repository: str, sha: str, description: str) -> Mapping[str, Any]:
    response = _mapping(api.get(f"repos/{repository}/git/blobs/{sha}"), description)
    if response.get("encoding") != "base64":
        raise ReleaseEvidenceOrphanAuditError(f"{description} must use base64 blob encoding.")
    encoded = _string(response.get("content"), description)
    try:
        raw = base64.b64decode("".join(encoded.split()), validate=True)
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
        raise ReleaseEvidenceOrphanAuditError(f"{description} is not valid UTF-8 JSON.") from error
    return _mapping(value, description)


def _canonical_tag(ref: str) -> str:
    if not ref.startswith(EVIDENCE_REF_PREFIX):
        raise ReleaseEvidenceOrphanAuditError("ref is outside the canonical release-evidence namespace.")
    tag = ref.removeprefix(EVIDENCE_REF_PREFIX)
    try:
        sanitized = sanitize_release_tag(tag)
    except ReleaseEvidenceV2Error as error:
        raise ReleaseEvidenceOrphanAuditError("ref has an invalid release tag suffix.") from error
    if ref != f"refs/heads/{evidence_ref_for_tag(sanitized)}":
        raise ReleaseEvidenceOrphanAuditError("ref does not exactly match its canonical release-evidence tag.")
    return sanitized


def _record_string(record: Mapping[str, Any], name: str) -> str:
    return _string(record.get(name), f"record {name}")


def _capture_record(record: Mapping[str, Any], tag: str) -> tuple[datetime, str, str]:
    if record.get("schema_version") != 2 or record.get("record_type") != "capture" or record.get("state") != "CAPTURED":
        raise ReleaseEvidenceOrphanAuditError("capture-v2.json is not a CAPTURED v2 record.")
    if _record_string(record, "release_tag") != tag:
        raise ReleaseEvidenceOrphanAuditError("capture-v2.json release tag does not match its canonical ref.")
    source_sha = _sha(record.get("source_sha"), "capture-v2 source SHA")
    capture_sha256 = _record_string(record, "capture_sha256")
    if SHA256_PATTERN.fullmatch(capture_sha256) is None:
        raise ReleaseEvidenceOrphanAuditError("capture-v2 self digest is invalid.")
    return _timestamp(record.get("captured_at"), "capture-v2 captured_at"), source_sha, capture_sha256


def _terminal_state(record: Mapping[str, Any], tag: str, source_sha: str, capture_sha256: str) -> str:
    record_type = _record_string(record, "record_type")
    state = _record_string(record, "state")
    expected: tuple[str, str, str]
    if record_type == "qualification":
        expected = ("qualification", "QUALIFIED", "qualified")
    elif record_type == "disposition":
        expected = ("disposition", "FAILED", "failed")
    else:
        raise ReleaseEvidenceOrphanAuditError("terminal v2 record has an unknown record type.")
    if record_type != expected[0] or state != expected[1] or record.get("schema_version") != 2:
        raise ReleaseEvidenceOrphanAuditError("terminal v2 record has an invalid state.")
    if (
        _record_string(record, "release_tag") != tag
        or _sha(record.get("source_sha"), "terminal source SHA") != source_sha
    ):
        raise ReleaseEvidenceOrphanAuditError("terminal v2 record does not bind the captured release identity.")
    capture = _mapping(record.get("capture"), "terminal capture binding")
    if capture.get("path") != f"{EVIDENCE_ROOT}/{tag}/{CAPTURE_NAME}" or capture.get("sha256") != capture_sha256:
        raise ReleaseEvidenceOrphanAuditError("terminal v2 record does not bind the exact capture record.")
    return expected[2]


def _bundle_state(api: GitHubRestApi, repository: str, blobs: Mapping[str, str], tag: str) -> tuple[str, datetime, str]:
    prefix = f"{EVIDENCE_ROOT}/{tag}/"
    capture_sha = blobs.get(f"{prefix}{CAPTURE_NAME}")
    if capture_sha is None:
        raise ReleaseEvidenceOrphanAuditError("bundle is missing capture-v2.json.")
    capture = _blob_json(api, repository, capture_sha, "capture-v2 blob")
    captured_at, source_sha, capture_sha256 = _capture_record(capture, tag)
    terminal_paths = [(QUALIFICATION_NAME, "qualified"), (DISPOSITION_NAME, "failed")]
    present = [(name, expected) for name, expected in terminal_paths if f"{prefix}{name}" in blobs]
    if len(present) > 1:
        raise ReleaseEvidenceOrphanAuditError("bundle contains both terminal v2 records.")
    if not present:
        return "captured", captured_at, "valid CAPTURED v2 evidence"
    terminal_name, expected_state = present[0]
    terminal = _blob_json(api, repository, blobs[f"{prefix}{terminal_name}"], f"{terminal_name} blob")
    terminal_state = _terminal_state(terminal, tag, source_sha, capture_sha256)
    if terminal_state != expected_state:
        raise ReleaseEvidenceOrphanAuditError("terminal v2 record type does not match its canonical filename.")
    return terminal_state, captured_at, f"valid terminal v2 evidence ({terminal_state})"


def _hours_since(moment: datetime, now: datetime) -> float:
    return max(0.0, (now - moment).total_seconds() / 3600)


def _commit_age_hours(commit: Mapping[str, Any], now: datetime) -> float | None:
    try:
        committer = _mapping(commit.get("committer"), "commit committer")
        return _hours_since(_timestamp(committer.get("date"), "commit committer date"), now)
    except ReleaseEvidenceOrphanAuditError:
        return None


def _remediation(classification: str, tag: str | None, bundle_state: str = "unknown") -> str:
    if classification == "legacy_ignored":
        return "No action. This canonical branch contains legacy evidence only and is outside the v2 orphan policy."
    if classification == "reconciled":
        return "No action. Protected main contains the exact v2 bundle blobs; index-v2.json may evolve independently."
    if classification == "recent":
        return (
            "Complete the terminal v2 evidence and run the operator reconciliation preflight before the "
            f"72-hour threshold for {tag}."
        )
    if classification == "stale_orphan":
        if bundle_state == "failed":
            return (
                "Do not run the qualified-evidence reconciliation helper. Preserve the durable failure ref, review the "
                f"release incident for {tag}, and retire the orphan ref only through an explicit operator decision."
            )
        return (
            "Inspect the GitHub REST bundle, then use the actor-aware reconciliation helper to open or adopt the "
            f"protected-main PR for {tag}; do not force-push or rewrite evidence."
        )
    return (
        "Do not reconcile this ref. Inspect its REST tree/blob data, preserve it for incident review, and recreate "
        "only valid canonical v2 evidence through the trusted producer if repair is required."
    )


def _malformed(ref: str, sha: str, reason: str, age_hours: float | None = None) -> EvidenceFinding:
    return EvidenceFinding(
        ref=ref,
        sha=sha,
        age_hours=age_hours,
        classification="malformed",
        bundle_state="unknown",
        captured_at=None,
        reason=reason,
        remediation=_remediation("malformed", None),
    )


def inspect_evidence_ref(
    api: GitHubRestApi,
    repository: str,
    ref_record: Mapping[str, Any],
    *,
    main_blobs: Mapping[str, str],
    threshold_hours: int,
    now: datetime,
) -> EvidenceFinding:
    ref = _string(ref_record.get("ref"), "matching Git ref")
    object_record = _mapping(ref_record.get("object"), f"matching Git ref object for {ref}")
    try:
        sha = _sha(object_record.get("sha"), f"matching Git ref SHA for {ref}")
    except ReleaseEvidenceOrphanAuditError as error:
        return _malformed(ref, str(object_record.get("sha", "unknown")), str(error))
    try:
        commit = _commit(api, repository, sha)
    except ReleaseEvidenceOrphanAuditError as error:
        return _malformed(ref, sha, str(error))
    commit_age_hours = _commit_age_hours(commit, now)
    try:
        tag = _canonical_tag(ref)
    except ReleaseEvidenceOrphanAuditError as error:
        return _malformed(ref, sha, str(error), age_hours=commit_age_hours)
    try:
        ref_blobs = _bundle_blobs(_tree_blobs(api, repository, commit), tag)
        prefix = f"{EVIDENCE_ROOT}/{tag}/"
        v2_paths = {
            f"{prefix}{CAPTURE_NAME}",
            f"{prefix}{QUALIFICATION_NAME}",
            f"{prefix}{DISPOSITION_NAME}",
        }
        if not v2_paths.intersection(ref_blobs):
            legacy_paths = {f"{prefix}{name}" for name in LEGACY_EVIDENCE_MARKERS}
            if not legacy_paths.intersection(ref_blobs):
                raise ReleaseEvidenceOrphanAuditError(
                    "canonical branch contains neither v2 records nor a recognized legacy evidence marker."
                )
            return EvidenceFinding(
                ref=ref,
                sha=sha,
                age_hours=commit_age_hours,
                classification="legacy_ignored",
                bundle_state="legacy",
                captured_at=None,
                reason="canonical branch contains no v2 evidence records",
                remediation=_remediation("legacy_ignored", tag),
            )
        bundle_state, captured_at, reason = _bundle_state(api, repository, ref_blobs, tag)
    except ReleaseEvidenceOrphanAuditError as error:
        return _malformed(ref, sha, str(error), age_hours=commit_age_hours)
    main_bundle_blobs = _bundle_blobs(main_blobs, tag)
    if all(main_bundle_blobs.get(path) == blob_sha for path, blob_sha in ref_blobs.items()):
        return EvidenceFinding(
            ref=ref,
            sha=sha,
            age_hours=_hours_since(captured_at, now),
            classification="reconciled",
            bundle_state=bundle_state,
            captured_at=captured_at.isoformat().replace("+00:00", "Z"),
            reason="protected main has byte-identical bundle blobs",
            remediation=_remediation("reconciled", tag, bundle_state),
        )
    age_hours = _hours_since(captured_at, now)
    classification = "stale_orphan" if age_hours >= threshold_hours else "recent"
    return EvidenceFinding(
        ref=ref,
        sha=sha,
        age_hours=age_hours,
        classification=classification,
        bundle_state=bundle_state,
        captured_at=captured_at.isoformat().replace("+00:00", "Z"),
        reason=reason,
        remediation=_remediation(classification, tag, bundle_state),
    )


def audit_evidence_refs(
    api: GitHubRestApi, repository: str, *, threshold_hours: int, now: datetime
) -> tuple[EvidenceFinding, ...]:
    if threshold_hours <= 0:
        raise ReleaseEvidenceOrphanAuditError("Orphan threshold must be positive.")
    main_ref = _mapping(api.get(f"repos/{repository}/git/ref/heads/{MAIN_BRANCH}"), "protected main ref")
    main_object = _mapping(main_ref.get("object"), "protected main ref object")
    main_sha = _sha(main_object.get("sha"), "protected main SHA")
    main_blobs = _tree_blobs(api, repository, _commit(api, repository, main_sha))
    refs = _flatten_pages(
        api.get(f"repos/{repository}/git/matching-refs/heads/automation/release-evidence-", paginate=True),
        "matching release-evidence refs",
    )
    findings = [
        inspect_evidence_ref(
            api,
            repository,
            record,
            main_blobs=main_blobs,
            threshold_hours=threshold_hours,
            now=now,
        )
        for record in refs
    ]
    return tuple(sorted(findings, key=lambda finding: finding.ref))


def _issue_number(issue: Mapping[str, Any]) -> int:
    number = issue.get("number")
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        raise ReleaseEvidenceOrphanAuditError("Alert issue has an invalid issue number.")
    return number


def _issue_owner_matches(issue: Mapping[str, Any], owner: str) -> bool:
    assignees = issue.get("assignees")
    if not isinstance(assignees, list):
        return False
    return any(isinstance(assignee, Mapping) and assignee.get("login") == owner for assignee in assignees)


def _alert_issue(
    api: GitHubRestApi, findings: Sequence[EvidenceFinding], *, owner: str, threshold_hours: int
) -> tuple[str, int | None]:
    issues = [
        issue
        for issue in _flatten_pages(
            api.get(f"repos/{api.repository}/issues?state=all&per_page=100", paginate=True), "repository issues"
        )
        if "pull_request" not in issue
    ]
    marker_issues = [issue for issue in issues if ALERT_MARKER in str(issue.get("body") or "")]
    legacy_issues = [issue for issue in issues if issue.get("title") == ALERT_TITLE and issue not in marker_issues]
    candidates = marker_issues + legacy_issues
    if len(candidates) > 1:
        numbers = ", ".join(str(_issue_number(issue)) for issue in candidates)
        raise ReleaseEvidenceOrphanAuditError(f"Ambiguous duplicate release-evidence alert issues: {numbers}.")
    alertable = [finding for finding in findings if finding.alertable]
    if not candidates:
        if not alertable:
            return "clear", None
        payload = _issue_payload(alertable, owner=owner, threshold_hours=threshold_hours, state="open")
        created = api.create_issue({key: value for key, value in payload.items() if key != "state"})
        return "open", _issue_number(created)
    issue = candidates[0]
    number = _issue_number(issue)
    if not alertable:
        if issue.get("state") == "open":
            api.update_issue(number, {"state": "closed"})
            return "clear", number
        return "clear", number
    payload = _issue_payload(alertable, owner=owner, threshold_hours=threshold_hours, state="open")
    legacy = issue in legacy_issues
    state_matches = issue.get("state") == "open"
    body_matches = issue.get("body") == payload["body"]
    title_matches = issue.get("title") == ALERT_TITLE
    owner_matches = _issue_owner_matches(issue, owner)
    if legacy or not (state_matches and body_matches and title_matches and owner_matches):
        api.update_issue(number, payload)
        return ("adopt" if legacy else "update"), number
    return "unchanged", number


def _markdown(value: str) -> str:
    return value.replace("`", "\\`").replace("|", "\\|").replace("\n", " ")


def _issue_payload(
    findings: Sequence[EvidenceFinding], *, owner: str, threshold_hours: int, state: str
) -> dict[str, object]:
    lines = [
        ALERT_MARKER,
        "",
        "## Release evidence orphan audit",
        "",
        "The scheduled audit found stale or malformed canonical release-evidence refs. "
        f"The stale threshold is **{threshold_hours} hours**.",
        "",
        "| Ref | SHA | Age | Class | V2 evidence | Remediation |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for finding in sorted(findings, key=lambda item: item.ref):
        age = "unknown" if finding.age_hours is None else f"{finding.age_hours:.1f}h"
        lines.append(
            "| "
            f"`{_markdown(finding.ref)}` | `{_markdown(finding.sha)}` | {age} | {finding.classification} | "
            f"{finding.bundle_state} | {_markdown(finding.remediation)} |"
        )
    lines.extend(
        [
            "",
            "### Operator action",
            "",
            "Review only GitHub REST tree/blob data for the listed `automation/release-evidence-*` refs. "
            "Do not check out, fetch, or execute them. For valid terminal evidence, use the actor-aware "
            "reconciliation helper; for malformed evidence, preserve the ref for review and recreate only "
            "through the trusted producer.",
        ]
    )
    return {"assignees": [owner], "body": "\n".join(lines), "state": state, "title": ALERT_TITLE}


def run_audit(
    api: GitHubRestApi,
    *,
    threshold_hours: int = DEFAULT_THRESHOLD_HOURS,
    owner: str = DEFAULT_ALERT_OWNER,
    now: datetime | None = None,
) -> AuditReport:
    if not owner or "/" in owner:
        raise ReleaseEvidenceOrphanAuditError("Alert owner must be a GitHub login.")
    observed_at = (now or datetime.now(UTC)).astimezone(UTC)
    findings = audit_evidence_refs(api, api.repository, threshold_hours=threshold_hours, now=observed_at)
    alert_action, alert_issue_number = _alert_issue(api, findings, owner=owner, threshold_hours=threshold_hours)
    return AuditReport(
        findings=findings,
        threshold_hours=threshold_hours,
        observed_at=observed_at.isoformat().replace("+00:00", "Z"),
        alert_action=alert_action,
        alert_issue_number=alert_issue_number,
    )


def _arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit canonical release-evidence refs through GitHub REST only.")
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY"), help="GitHub owner/repository")
    parser.add_argument("--threshold-hours", type=int, default=DEFAULT_THRESHOLD_HOURS)
    parser.add_argument("--owner", default=DEFAULT_ALERT_OWNER, help="GitHub login assigned to the alert issue")
    parser.add_argument("--now", help="UTC observation time for deterministic tests, for example 2026-08-27T12:00:00Z")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _arguments(argv)
    if not isinstance(args.repository, str) or "/" not in args.repository:
        print("--repository or GITHUB_REPOSITORY must be an owner/repository value.", file=sys.stderr)
        return 2
    try:
        now = _timestamp(args.now, "--now") if args.now else None
        report = run_audit(
            GitHubRestClient(args.repository),
            threshold_hours=args.threshold_hours,
            owner=args.owner,
            now=now,
        )
    except ReleaseEvidenceOrphanAuditError as error:
        print(f"release-evidence orphan audit: {error}", file=sys.stderr)
        return 2
    print(json.dumps(report.as_dict(), sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
