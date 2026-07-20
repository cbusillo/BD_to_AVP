import copy
import json
import os
import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

from scripts import beta3_recovery_evidence as recovery_evidence


EXPECTED_PRODUCTION_REPOSITORY = "cbusillo/BD_to_AVP"
EXPECTED_PRERELEASE_WORKFLOW_REF = "cbusillo/BD_to_AVP/.github/workflows/prerelease.yml@refs/heads/main"


def json_response(value: object) -> recovery_evidence.RemoteResponse:
    return recovery_evidence.RemoteResponse(status=200, body=json.dumps(value).encode("utf-8"))


def github_actions_publication_environment(expected_sha: str) -> dict[str, str]:
    return {
        "GITHUB_ACTIONS": "true",
        "GITHUB_REPOSITORY": EXPECTED_PRODUCTION_REPOSITORY,
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_REF_PROTECTED": "true",
        "GITHUB_SHA": expected_sha,
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_WORKFLOW_REF": EXPECTED_PRERELEASE_WORKFLOW_REF,
        "GITHUB_ACTOR": "shiny-code-bot",
        "GITHUB_TRIGGERING_ACTOR": "shiny-code-bot",
    }


class FakeFetcher:
    def __init__(self, responses: dict[str, recovery_evidence.RemoteResponse]) -> None:
        self.responses = responses
        self.requests: list[str] = []

    def __call__(self, url: str) -> recovery_evidence.RemoteResponse:
        self.requests.append(url)
        try:
            return self.responses[url]
        except KeyError as error:
            raise AssertionError(f"Unexpected remote evidence request: {url}") from error


def make_responses(evidence: dict[str, object]) -> dict[str, recovery_evidence.RemoteResponse]:
    repository = str(evidence["repository"])
    api_root = f"{recovery_evidence.GITHUB_API_ROOT}/repos/{repository}"
    failed_run = evidence["failed_run"]
    assert isinstance(failed_run, dict)
    run_id = failed_run["id"]
    run = {
        "id": run_id,
        "workflow_id": failed_run["workflow_id"],
        "run_number": failed_run["run_number"],
        "event": failed_run["event"],
        "head_branch": failed_run["head_branch"],
        "head_sha": failed_run["head_sha"],
        "run_attempt": failed_run["run_attempt"],
        "path": failed_run["workflow_path"],
        "name": failed_run["workflow_name"],
        "display_title": failed_run["display_title"],
        "status": failed_run["status"],
        "conclusion": failed_run["conclusion"],
        "actor": {"login": failed_run["actor"]},
        "triggering_actor": {"login": failed_run["triggering_actor"]},
        "repository": {"full_name": failed_run["repository"]},
        "head_repository": {"full_name": failed_run["head_repository"]},
    }
    jobs = []
    for expected_job in failed_run["jobs"]:
        steps = []
        if expected_job["name"] == failed_run["failed_job"]:
            steps = [
                {"name": failed_run["failed_step"], "conclusion": "failure"},
                {"name": failed_run["package_upload_step"], "conclusion": "skipped"},
            ]
        jobs.append(
            {
                "name": expected_job["name"],
                "status": "completed",
                "conclusion": expected_job["conclusion"],
                "steps": steps,
            }
        )
    previews = evidence["retired_preview_releases"]
    assert isinstance(previews, list)
    releases = []
    repository_identity = dict(evidence["repository_identity"])
    repository_identity["permissions"] = {"push": True}
    responses = {
        api_root: json_response(repository_identity),
        f"{api_root}/actions/runs/{run_id}": json_response(run),
        f"{api_root}/actions/runs/{run_id}/attempts/{failed_run['run_attempt']}/jobs?per_page=100": json_response(
            {"total_count": len(jobs), "jobs": jobs}
        ),
        f"{api_root}/actions/runs/{run_id}/artifacts?per_page=100": json_response(
            {"total_count": failed_run["artifact_count"], "artifacts": []}
        ),
        f"{api_root}/releases?per_page=100&page=1": json_response(releases),
        f"{api_root}/releases/latest": json_response(
            {
                "id": evidence["github_latest"]["release_id"],
                "tag_name": evidence["github_latest"]["release_tag"],
                "target_commitish": evidence["github_latest"]["target_sha"],
                "immutable": evidence["github_latest"]["immutable"],
                "draft": False,
                "prerelease": False,
            }
        ),
        f"{api_root}/pages": json_response(
            {
                "public": evidence["pages"]["public"],
                "build_type": evidence["pages"]["build_type"],
            }
        ),
        f"{recovery_evidence.PAGES_APPCAST_URL}?beta3-recovery-audit=1": recovery_evidence.RemoteResponse(
            status=200,
            body=(recovery_evidence.REPO_ROOT / str(evidence["pages"]["capture_path"])).read_bytes().rstrip(b"\n"),
        ),
        recovery_evidence.PYPI_URL: json_response(
            {
                "info": {"version": evidence["pypi"]["latest_version"]},
                "releases": {"0.2.143": [{}]},
            }
        ),
    }
    for tag in evidence["absent_github_state"]["tags"]:
        responses[f"{api_root}/git/ref/tags/{quote(tag, safe='')}"] = recovery_evidence.RemoteResponse(
            status=404,
            body=b"{}",
        )
    for preview in previews:
        assert isinstance(preview, dict)
        release = {
            "id": preview["release_id"],
            "tag_name": preview["tag"],
            "draft": False,
            "prerelease": True,
            "immutable": preview["immutable"],
            "target_commitish": preview["target_sha"],
            "assets": [
                {
                    "id": asset["asset_id"],
                    "name": asset["name"],
                    "size": asset["size"],
                    "digest": asset["digest"],
                }
                for asset in preview["assets"]
            ],
        }
        responses[f"{api_root}/releases/tags/{quote(str(preview['tag']), safe='')}"] = json_response(release)
    return responses


class Beta3RecoveryEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.evidence = recovery_evidence.load_beta3_recovery_evidence()

    def test_reviewed_receipt_and_appcast_capture_are_digest_pinned(self) -> None:
        self.assertEqual(recovery_evidence.PRODUCTION_REPOSITORY, EXPECTED_PRODUCTION_REPOSITORY)
        self.assertEqual(recovery_evidence.PRERELEASE_WORKFLOW_REF, EXPECTED_PRERELEASE_WORKFLOW_REF)
        self.assertEqual(self.evidence["schema_version"], 2)
        self.assertEqual(self.evidence["repository_identity"]["id"], 771225421)
        self.assertEqual(self.evidence["pages"]["release_tag"], "v0.2.143")
        cut_packet = (recovery_evidence.REPO_ROOT / "docs" / "0.3.0-beta.3-cut-packet.md").read_text(encoding="utf-8")
        self.assertIn(str(self.evidence["observed_at"]), cut_packet)
        self.assertIn(recovery_evidence.BETA3_RECOVERY_EVIDENCE_SHA256, cut_packet)

        with tempfile.TemporaryDirectory() as temp_dir:
            changed = Path(temp_dir) / "evidence.json"
            changed.write_bytes(
                recovery_evidence.BETA3_RECOVERY_EVIDENCE_PATH.read_bytes().replace(
                    b'"artifact_count": 0',
                    b'"artifact_count": 1',
                )
            )
            with self.assertRaisesRegex(recovery_evidence.Beta3RecoveryEvidenceError, "digest"):
                recovery_evidence.load_beta3_recovery_evidence(changed)

    def test_live_remote_verifier_accepts_the_exact_receipt(self) -> None:
        fetcher = FakeFetcher(make_responses(self.evidence))

        recovery_evidence.verify_beta3_remote_state(self.evidence, fetcher=fetcher)

        self.assertIn(recovery_evidence.PYPI_URL, fetcher.requests)
        self.assertIn(
            f"{recovery_evidence.PAGES_APPCAST_URL}?beta3-recovery-audit=1",
            fetcher.requests,
        )

    def test_read_only_repository_visibility_cannot_prove_draft_absence(self) -> None:
        responses = make_responses(self.evidence)
        repository = str(self.evidence["repository"])
        repository_url = f"{recovery_evidence.GITHUB_API_ROOT}/repos/{repository}"
        repository_identity = json.loads(responses[repository_url].body)
        repository_identity["permissions"] = {"push": False}
        responses[repository_url] = json_response(repository_identity)

        with self.assertRaisesRegex(
            recovery_evidence.Beta3RecoveryEvidenceError,
            "push visibility",
        ):
            recovery_evidence.verify_beta3_remote_state(self.evidence, fetcher=FakeFetcher(responses))

    def test_github_actions_contents_write_token_can_prove_draft_absence(self) -> None:
        expected_sha = "a" * 40
        responses = make_responses(self.evidence)
        repository = str(self.evidence["repository"])
        repository_url = f"{recovery_evidence.GITHUB_API_ROOT}/repos/{repository}"
        repository_identity = json.loads(responses[repository_url].body)
        repository_identity["permissions"] = {"push": False}
        responses[repository_url] = json_response(repository_identity)

        with patch.dict(os.environ, github_actions_publication_environment(expected_sha), clear=True):
            recovery_evidence.verify_beta3_remote_state(
                self.evidence,
                fetcher=FakeFetcher(responses),
                allow_beta3_draft=True,
                expected_sha=expected_sha,
                allow_github_actions_contents_write_token=True,
            )

    def test_github_actions_token_allowance_rejects_context_drift(self) -> None:
        expected_sha = "a" * 40
        expected_environment = github_actions_publication_environment(expected_sha)
        for name in expected_environment:
            for change in ("changed", "missing"):
                with self.subTest(name=name, change=change):
                    environment = dict(expected_environment)
                    if change == "changed":
                        environment[name] = "__wrong__"
                    else:
                        del environment[name]
                    with patch.dict(os.environ, environment, clear=True):
                        with self.assertRaisesRegex(
                            recovery_evidence.Beta3RecoveryEvidenceError,
                            "publication context mismatch",
                        ):
                            recovery_evidence.verify_beta3_remote_state(
                                self.evidence,
                                fetcher=FakeFetcher(make_responses(self.evidence)),
                                allow_beta3_draft=True,
                                expected_sha=expected_sha,
                                allow_github_actions_contents_write_token=True,
                            )

    def test_github_actions_token_allowance_requires_publication_preflight(self) -> None:
        with self.assertRaisesRegex(
            recovery_evidence.Beta3RecoveryEvidenceError,
            "valid only for publication preflight",
        ):
            recovery_evidence.verify_beta3_remote_state(
                self.evidence,
                fetcher=FakeFetcher(make_responses(self.evidence)),
                allow_github_actions_contents_write_token=True,
            )

        with self.assertRaisesRegex(
            recovery_evidence.Beta3RecoveryEvidenceError,
            "requires --allow-beta3-draft",
        ):
            recovery_evidence.main(["--allow-github-actions-contents-write-token"])

    def test_live_remote_verifier_rejects_new_tag_or_artifact_state(self) -> None:
        repository = str(self.evidence["repository"])
        api_root = f"{recovery_evidence.GITHUB_API_ROOT}/repos/{repository}"
        failed_run = self.evidence["failed_run"]
        assert isinstance(failed_run, dict)
        target_tag = self.evidence["transition"]["target"]["release_tag"]
        tag_url = f"{api_root}/git/ref/tags/{quote(target_tag, safe='')}"
        artifact_url = f"{api_root}/actions/runs/{failed_run['id']}/artifacts?per_page=100"
        for name, url, response, message in (
            ("tag", tag_url, json_response({"ref": f"refs/tags/{target_tag}"}), "now exists"),
            ("artifact", artifact_url, json_response({"total_count": 1, "artifacts": [{}]}), "artifact state"),
        ):
            with self.subTest(name=name):
                responses = make_responses(self.evidence)
                responses[url] = response
                with self.assertRaisesRegex(recovery_evidence.Beta3RecoveryEvidenceError, message):
                    recovery_evidence.verify_beta3_remote_state(
                        self.evidence,
                        fetcher=FakeFetcher(responses),
                    )

    def test_publication_preflight_allows_only_matching_beta3_draft(self) -> None:
        expected_sha = "a" * 40
        repository = str(self.evidence["repository"])
        releases_url = f"{recovery_evidence.GITHUB_API_ROOT}/repos/{repository}/releases?per_page=100&page=1"
        target_tag = self.evidence["transition"]["target"]["release_tag"]
        matching_draft = {
            "tag_name": target_tag,
            "name": target_tag,
            "draft": True,
            "prerelease": True,
            "target_commitish": expected_sha,
        }
        responses = make_responses(self.evidence)
        responses[releases_url] = json_response([matching_draft])

        recovery_evidence.verify_beta3_remote_state(
            self.evidence,
            fetcher=FakeFetcher(responses),
            allow_beta3_draft=True,
            expected_sha=expected_sha,
        )

        target_tag_url = (
            f"{recovery_evidence.GITHUB_API_ROOT}/repos/{repository}/git/ref/tags/{quote(target_tag, safe='')}"
        )
        responses[target_tag_url] = json_response(
            {
                "ref": f"refs/tags/{target_tag}",
                "object": {"type": "commit", "sha": expected_sha},
            }
        )
        recovery_evidence.verify_beta3_remote_state(
            self.evidence,
            fetcher=FakeFetcher(responses),
            allow_beta3_draft=True,
            expected_sha=expected_sha,
        )

        changed = copy.deepcopy(matching_draft)
        changed["target_commitish"] = "b" * 40
        responses[releases_url] = json_response([changed])
        with self.assertRaisesRegex(recovery_evidence.Beta3RecoveryEvidenceError, "mismatch"):
            recovery_evidence.verify_beta3_remote_state(
                self.evidence,
                fetcher=FakeFetcher(responses),
                allow_beta3_draft=True,
                expected_sha=expected_sha,
            )

    def test_live_remote_verifier_rejects_repository_or_job_identity_drift(self) -> None:
        repository = str(self.evidence["repository"])
        api_root = f"{recovery_evidence.GITHUB_API_ROOT}/repos/{repository}"
        failed_run = self.evidence["failed_run"]
        jobs_url = f"{api_root}/actions/runs/{failed_run['id']}/attempts/{failed_run['run_attempt']}/jobs?per_page=100"
        for name, url, mutate, message in (
            (
                "repository",
                api_root,
                lambda value: {**value, "id": 1},
                "repository identity mismatch",
            ),
            (
                "job",
                jobs_url,
                lambda value: {**value, "jobs": value["jobs"][:-1], "total_count": value["total_count"] - 1},
                "job set changed",
            ),
        ):
            with self.subTest(name=name):
                responses = make_responses(self.evidence)
                original = json.loads(responses[url].body)
                responses[url] = json_response(mutate(original))
                with self.assertRaisesRegex(recovery_evidence.Beta3RecoveryEvidenceError, message):
                    recovery_evidence.verify_beta3_remote_state(
                        self.evidence,
                        fetcher=FakeFetcher(responses),
                    )


if __name__ == "__main__":
    unittest.main()
