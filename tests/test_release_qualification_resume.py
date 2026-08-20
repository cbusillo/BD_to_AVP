import json
import stat
import tempfile
import unittest

from collections import defaultdict, deque
from pathlib import Path
from unittest.mock import patch

from scripts.release_milestone_context import ReleaseMilestoneContext
from scripts.release_qualification_apply import ApplyOutcome
from scripts.release_qualification_artifact import ReconciliationBundle
from scripts.release_qualification_controller import EvidenceBinding
from scripts.release_qualification_resume import (
    EXIT_OPERATOR_REQUIRED,
    EXIT_SUCCESS,
    QualificationResumeError,
    QualificationResumeSafetyError,
    ResumeIdentity,
    _checkpoint_lock,
    _checkpoint_payload,
    _dispatch,
    _require_no_active_exact_runs,
    _revalidate_remote_identity,
    _run_matches,
    _write_checkpoint,
    resume_qualification,
    safety_error_payload,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN_SHA = "a" * 40
EVIDENCE_SHA = "b" * 40
CANDIDATE_SHA = "c" * 40
MANIFEST_SHA = "d" * 64
BASE_SHA = "e" * 40
RELEASE_TAG = "v1.0.0"
EVIDENCE_REF = f"automation/release-evidence-{RELEASE_TAG}"
RUNS_ENDPOINT = (
    "repos/cbusillo/BD_to_AVP/actions/workflows/milestone-qualification.yml/runs"
    "?event=workflow_dispatch&branch=main&per_page=100"
)
RUNS_ENDPOINT_PAGE_2 = RUNS_ENDPOINT + "&page=2"
PR_ENDPOINT = (
    "repos/cbusillo/BD_to_AVP/pulls?state=open&base=main"
    f"&head=cbusillo%3Aautomation%2Frelease-evidence-{RELEASE_TAG}&per_page=100"
)


class FakeGitHubAPI:
    def __init__(self) -> None:
        self.responses: dict[tuple[str, bool], object] = {}
        self.sequences: dict[tuple[str, bool], deque[object]] = defaultdict(deque)
        self.byte_responses: dict[tuple[str, bool], bytes] = {}
        self.gets: list[tuple[str, bool]] = []
        self.byte_gets: list[tuple[str, bool, int, float]] = []
        self.posts: list[tuple[str, MappingProxy, bool]] = []

    def set(self, endpoint: str, payload: object, *, active_auth: bool = True) -> None:
        self.responses[(endpoint, active_auth)] = payload

    def set_sequence(self, endpoint: str, payloads: list[object], *, active_auth: bool = True) -> None:
        self.sequences[(endpoint, active_auth)] = deque(payloads)

    def get_json(self, endpoint: str, *, active_auth: bool = False) -> object:
        key = (endpoint, active_auth)
        self.gets.append(key)
        sequence = self.sequences.get(key)
        if sequence:
            if len(sequence) > 1:
                return sequence.popleft()
            return sequence[0]
        if key not in self.responses:
            raise AssertionError(f"Unexpected GitHub GET: {key!r}")
        return self.responses[key]

    def post_json(
        self,
        endpoint: str,
        payload: dict[str, object],
        *,
        active_auth: bool = False,
    ) -> object:
        self.posts.append((endpoint, MappingProxy(payload), active_auth))
        return None

    def get_bytes(
        self,
        endpoint: str,
        *,
        active_auth: bool = False,
        max_bytes: int,
        timeout_seconds: float,
    ) -> bytes:
        self.byte_gets.append((endpoint, active_auth, max_bytes, timeout_seconds))
        key = (endpoint, active_auth)
        if key not in self.byte_responses:
            raise AssertionError(f"Unexpected GitHub byte GET: {key!r}")
        return self.byte_responses[key]


class MappingProxy(dict[str, object]):
    def __init__(self, payload: dict[str, object]) -> None:
        super().__init__(json.loads(json.dumps(payload)))


class FailOnGitHubAPI:
    def get_json(self, endpoint: str, *, active_auth: bool = False) -> object:
        raise AssertionError(f"Complete qualification must not call GitHub: {endpoint}")

    def post_json(
        self,
        endpoint: str,
        payload: dict[str, object],
        *,
        active_auth: bool = False,
    ) -> object:
        raise AssertionError(f"Complete qualification must not mutate GitHub: {endpoint}")

    def get_bytes(
        self,
        endpoint: str,
        *,
        active_auth: bool = False,
        max_bytes: int,
        timeout_seconds: float,
    ) -> bytes:
        raise AssertionError(f"Complete qualification must not download GitHub content: {endpoint}")


def manifest() -> dict[str, object]:
    return {
        "candidate": {"release_tag": RELEASE_TAG, "source_sha": CANDIDATE_SHA},
        "release": {"id": 123},
        "manifest_sha256": MANIFEST_SHA,
        "runner_sha": MAIN_SHA,
        "canonical_evidence": {"ref": EVIDENCE_REF, "base_sha": BASE_SHA},
        "input_digests": {
            "policy": "1" * 64,
            "policy_checkpoint": "2" * 64,
            "route_table": "3" * 64,
            "controller_runner": "4" * 64,
        },
        "release_receipt": {"file_sha256": "5" * 64},
        "signed_ui_artifact": {"artifact_id": 456, "artifact_sha256": "6" * 64},
    }


def binding(*, runner_sha: str = MAIN_SHA) -> EvidenceBinding:
    document = manifest()
    document["runner_sha"] = runner_sha
    context = ReleaseMilestoneContext(
        candidate_sha=CANDIDATE_SHA,
        evidence_path="docs/qualification/release-evidence-v1.json",
        first_candidate_of_cycle=False,
        policy_path="docs/qualification/release-qualification-policy-v1.json",
        qualification_path=f"docs/release-evidence/{RELEASE_TAG}/qualification-record.json",
        release_receipt_path=f"docs/release-evidence/{RELEASE_TAG}/release-receipt.json",
        release_stage="stable",
        release_tag=RELEASE_TAG,
        manifest_path=f"docs/release-evidence/{RELEASE_TAG}/qualification-manifest.json",
        manifest_sha256=MANIFEST_SHA,
        runner_sha=runner_sha,
    )
    return EvidenceBinding(context=context, manifest=document, mode="manifest")


def status_payload(*, blocking: bool = True) -> dict[str, object]:
    blockers = ["clean-machine-signed-update"] if blocking else []
    return {
        "release_tag": RELEASE_TAG,
        "candidate_sha": CANDIDATE_SHA,
        "overall_status": "blocked" if blockers else "complete",
        "passed": not blockers,
        "summary": {"blocking": len(blockers)},
        "groups": {"blocking": blockers},
    }


def pull_request(*, number: int = 42, evidence_sha: str = EVIDENCE_SHA) -> dict[str, object]:
    return {
        "number": number,
        "state": "open",
        "head": {
            "ref": EVIDENCE_REF,
            "sha": evidence_sha,
            "repo": {"full_name": "cbusillo/BD_to_AVP"},
        },
        "base": {"ref": "main", "repo": {"full_name": "cbusillo/BD_to_AVP"}},
    }


def workflow_run(
    run_id: int,
    *,
    status: str,
    conclusion: str | None,
    run_attempt: int = 1,
) -> dict[str, object]:
    return {
        "id": run_id,
        "run_attempt": run_attempt,
        "name": "Milestone Qualification",
        "path": ".github/workflows/milestone-qualification.yml",
        "display_title": f"Milestone {RELEASE_TAG} {MANIFEST_SHA}",
        "event": "workflow_dispatch",
        "head_branch": "main",
        "head_sha": MAIN_SHA,
        "actor": {"login": "cbusillo"},
        "triggering_actor": {"login": "cbusillo"},
        "status": status,
        "conclusion": conclusion,
        "updated_at": "2026-08-10T12:00:00Z",
        "html_url": f"https://github.example/runs/{run_id}",
    }


def artifact(run_id: int, *, expired: bool, run_attempt: int = 1) -> dict[str, object]:
    return {
        "id": 9000 + run_id,
        "name": f"milestone-qualification-{RELEASE_TAG}-{run_attempt}",
        "digest": "sha256:" + "7" * 64,
        "expired": expired,
        "size_in_bytes": 1024,
        "workflow_run": {"id": run_id, "head_branch": "main", "head_sha": MAIN_SHA},
    }


def identity() -> ResumeIdentity:
    return ResumeIdentity(
        release_tag=RELEASE_TAG,
        candidate_sha=CANDIDATE_SHA,
        release_id=123,
        manifest_sha256=MANIFEST_SHA,
        runner_sha=MAIN_SHA,
        main_sha=MAIN_SHA,
        evidence_ref=EVIDENCE_REF,
        evidence_sha=EVIDENCE_SHA,
        evidence_base_sha=BASE_SHA,
        evidence_pr_number=42,
        release_receipt_file_sha256="5" * 64,
        signed_ui_artifact_id=456,
        signed_ui_artifact_sha256="6" * 64,
        policy_sha256="1" * 64,
        policy_checkpoint_sha256="2" * 64,
        route_table_sha256="3" * 64,
        controller_runner_sha256="4" * 64,
    )


class ReleaseQualificationResumeTests(unittest.TestCase):
    def configured_client(self) -> FakeGitHubAPI:
        client = FakeGitHubAPI()
        client.set(
            "repos/cbusillo/BD_to_AVP",
            {"id": 771225421, "full_name": "cbusillo/BD_to_AVP", "owner": {"login": "cbusillo"}},
        )
        client.set("repos/cbusillo/BD_to_AVP/git/ref/heads/main", {"object": {"sha": MAIN_SHA}})
        client.set(
            f"repos/cbusillo/BD_to_AVP/git/ref/heads/automation%2Frelease-evidence-{RELEASE_TAG}",
            {"object": {"sha": EVIDENCE_SHA}},
        )
        client.set(PR_ENDPOINT, [pull_request()])
        return client

    def run_blocked(
        self,
        client: FakeGitHubAPI,
        checkpoint_path: Path,
        **kwargs: object,
    ):
        with (
            patch("scripts.release_qualification_resume.build_status", return_value=status_payload()),
            patch("scripts.release_qualification_resume.resolve_evidence_binding", return_value=binding()),
            patch("scripts.release_qualification_resume._require_local_evidence_checkout"),
        ):
            return resume_qualification(
                REPO_ROOT,
                RELEASE_TAG,
                client=client,
                checkpoint_path=checkpoint_path,
                poll_attempts=1,
                sleep=lambda _seconds: None,
                **kwargs,
            )

    def test_complete_v031_is_noop_without_github_or_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            checkpoint = Path(temporary_directory) / "checkpoint.json"
            result = resume_qualification(
                REPO_ROOT,
                "v0.3.1",
                client=FailOnGitHubAPI(),
                checkpoint_path=checkpoint,
            )

        self.assertEqual(result.exit_code, EXIT_SUCCESS)
        self.assertEqual(result.payload["state"], "complete")
        self.assertFalse(result.payload["checkpoint"]["present"])
        self.assertFalse(checkpoint.exists())

    def test_malformed_checked_status_is_reported_as_resume_error(self) -> None:
        with patch(
            "scripts.release_qualification_resume.build_status",
            side_effect=json.JSONDecodeError("invalid", "{", 0),
        ):
            with self.assertRaisesRegex(QualificationResumeError, "status could not be resolved"):
                resume_qualification(REPO_ROOT, RELEASE_TAG, client=FailOnGitHubAPI())

    def test_dispatch_ready_requires_exact_confirmation(self) -> None:
        client = self.configured_client()
        client.set(RUNS_ENDPOINT, {"workflow_runs": []})
        with tempfile.TemporaryDirectory() as temporary_directory:
            checkpoint = Path(temporary_directory) / "checkpoint.json"
            result = self.run_blocked(client, checkpoint)

        self.assertEqual(result.exit_code, EXIT_OPERATOR_REQUIRED)
        self.assertEqual(result.payload["state"], "dispatch_ready")
        self.assertEqual(client.posts, [])
        self.assertTrue(client.gets)
        self.assertTrue(all(active_auth for _endpoint, active_auth in client.gets))
        self.assertFalse(checkpoint.exists())

    def test_active_exact_run_on_second_page_blocks_apply(self) -> None:
        client = FakeGitHubAPI()
        client.set(
            RUNS_ENDPOINT,
            {"workflow_runs": [{"id": 1_000 + index} for index in range(100)]},
        )
        client.set(
            RUNS_ENDPOINT_PAGE_2,
            {"workflow_runs": [workflow_run(101, status="in_progress", conclusion=None)]},
        )

        with self.assertRaisesRegex(QualificationResumeSafetyError, "is active"):
            _require_no_active_exact_runs(client, identity())

    def test_remote_identity_retries_transient_pull_request_head_lag(self) -> None:
        client = self.configured_client()
        pushed_sha = "9" * 40
        client.set(
            f"repos/cbusillo/BD_to_AVP/git/ref/heads/automation%2Frelease-evidence-{RELEASE_TAG}",
            {"object": {"sha": pushed_sha}},
        )
        client.set_sequence(
            PR_ENDPOINT,
            [
                [pull_request(evidence_sha=EVIDENCE_SHA)],
                [pull_request(evidence_sha=pushed_sha)],
            ],
        )

        with patch("scripts.release_qualification_resume.time.sleep") as sleep:
            _revalidate_remote_identity(
                client,
                identity(),
                expected_evidence_sha=pushed_sha,
            )

        sleep.assert_called_once_with(1.0)

    def test_exact_dispatch_is_checkpointed_and_adopts_visible_run(self) -> None:
        client = self.configured_client()
        running = workflow_run(101, status="in_progress", conclusion=None)
        client.set_sequence(
            RUNS_ENDPOINT,
            [{"workflow_runs": []}, {"workflow_runs": [running]}],
        )
        client.set("user", {"login": "cbusillo"}, active_auth=True)
        with tempfile.TemporaryDirectory() as temporary_directory:
            checkpoint = Path(temporary_directory) / "checkpoint.json"
            result = self.run_blocked(
                client,
                checkpoint,
                expected_main_sha=MAIN_SHA,
                expected_manifest_sha256=MANIFEST_SHA,
            )
            checkpoint_payload = json.loads(checkpoint.read_text(encoding="utf-8"))
            mode = stat.S_IMODE(checkpoint.stat().st_mode)

        self.assertEqual(result.exit_code, EXIT_SUCCESS)
        self.assertEqual(result.payload["state"], "running")
        self.assertEqual(mode, 0o600)
        self.assertEqual(checkpoint_payload["dispatch"]["state"], "observed")
        self.assertEqual(checkpoint_payload["dispatch"]["run_id"], 101)
        self.assertEqual(len(client.posts), 1)
        endpoint, payload, active_auth = client.posts[0]
        self.assertEqual(
            endpoint,
            "repos/cbusillo/BD_to_AVP/actions/workflows/milestone-qualification.yml/dispatches",
        )
        self.assertEqual(
            payload,
            {"ref": "main", "inputs": {"candidate_tag": RELEASE_TAG, "manifest_sha256": MANIFEST_SHA}},
        )
        self.assertTrue(active_auth)

    def test_accepted_dispatch_without_visible_run_requires_later_observation(self) -> None:
        client = self.configured_client()
        client.set_sequence(RUNS_ENDPOINT, [{"workflow_runs": []}, {"workflow_runs": []}])
        client.set("user", {"login": "cbusillo"}, active_auth=True)
        with tempfile.TemporaryDirectory() as temporary_directory:
            checkpoint = Path(temporary_directory) / "checkpoint.json"
            result = self.run_blocked(
                client,
                checkpoint,
                expected_main_sha=MAIN_SHA,
                expected_manifest_sha256=MANIFEST_SHA,
            )

        self.assertEqual(result.exit_code, EXIT_OPERATOR_REQUIRED)
        self.assertEqual(result.payload["state"], "dispatch_visibility_pending")
        self.assertEqual(len(client.posts), 1)

    def test_prepared_checkpoint_prevents_duplicate_dispatch(self) -> None:
        client = self.configured_client()
        client.set(RUNS_ENDPOINT, {"workflow_runs": []})
        with tempfile.TemporaryDirectory() as temporary_directory:
            checkpoint = Path(temporary_directory) / "checkpoint.json"
            _write_checkpoint(
                checkpoint,
                _checkpoint_payload(
                    identity(),
                    state="prepared",
                    high_water_run_id=100,
                    retry_of_run_id=None,
                ),
            )
            result = self.run_blocked(
                client,
                checkpoint,
                expected_main_sha=MAIN_SHA,
                expected_manifest_sha256=MANIFEST_SHA,
            )

        self.assertEqual(result.payload["state"], "dispatch_visibility_pending")
        self.assertEqual(client.posts, [])

    def test_dispatch_race_reports_prepared_checkpoint_as_operator_required(self) -> None:
        client = self.configured_client()
        client.set("user", {"login": "cbusillo"}, active_auth=True)
        with tempfile.TemporaryDirectory() as temporary_directory:
            checkpoint = Path(temporary_directory) / "checkpoint.json"
            _write_checkpoint(
                checkpoint,
                _checkpoint_payload(
                    identity(),
                    state="prepared",
                    high_water_run_id=100,
                    retry_of_run_id=None,
                ),
            )
            result = _dispatch(
                client,
                identity(),
                checkpoint,
                high_water_run_id=100,
                retry_of_run_id=None,
                replace_prepared_checkpoint_sha256=None,
                status_payload=status_payload(),
                poll_attempts=1,
                poll_seconds=0,
                sleep=lambda _seconds: None,
            )

        self.assertEqual(result.exit_code, EXIT_OPERATOR_REQUIRED)
        self.assertEqual(result.payload["state"], "dispatch_visibility_pending")
        self.assertEqual(client.posts, [])

    def test_prepared_checkpoint_requires_exact_explicit_retry(self) -> None:
        client = self.configured_client()
        running = workflow_run(101, status="queued", conclusion=None)
        client.set_sequence(
            RUNS_ENDPOINT,
            [{"workflow_runs": []}, {"workflow_runs": [running]}],
        )
        client.set("user", {"login": "cbusillo"}, active_auth=True)
        with tempfile.TemporaryDirectory() as temporary_directory:
            checkpoint = Path(temporary_directory) / "checkpoint.json"
            payload = _checkpoint_payload(
                identity(),
                state="prepared",
                high_water_run_id=100,
                retry_of_run_id=None,
            )
            _write_checkpoint(checkpoint, payload)
            result = self.run_blocked(
                client,
                checkpoint,
                expected_main_sha=MAIN_SHA,
                expected_manifest_sha256=MANIFEST_SHA,
                retry_checkpoint_sha256=payload["checkpoint_sha256"],
                prepared_retry_after_seconds=0,
            )

        self.assertEqual(result.payload["state"], "running")
        self.assertEqual(len(client.posts), 1)

    def test_checkpoint_lock_rejects_concurrent_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            checkpoint = Path(temporary_directory) / "checkpoint.json"
            with _checkpoint_lock(checkpoint):
                with self.assertRaisesRegex(QualificationResumeSafetyError, "already owns"):
                    with _checkpoint_lock(checkpoint):
                        self.fail("Concurrent checkpoint lock unexpectedly succeeded")

    def test_observed_checkpoint_reloads_exact_run_by_id(self) -> None:
        client = self.configured_client()
        failed = workflow_run(101, status="completed", conclusion="failure")
        client.set(RUNS_ENDPOINT, {"workflow_runs": []})
        client.set("repos/cbusillo/BD_to_AVP/actions/runs/101", failed)
        with tempfile.TemporaryDirectory() as temporary_directory:
            checkpoint = Path(temporary_directory) / "checkpoint.json"
            _write_checkpoint(
                checkpoint,
                _checkpoint_payload(
                    identity(),
                    state="observed",
                    high_water_run_id=100,
                    retry_of_run_id=None,
                    run_id=101,
                    run_attempt=1,
                ),
            )
            result = self.run_blocked(client, checkpoint)

        self.assertEqual(result.payload["state"], "failed")
        self.assertIn(("repos/cbusillo/BD_to_AVP/actions/runs/101", True), client.gets)

    def test_checkpoint_self_digest_rejects_tampering(self) -> None:
        client = self.configured_client()
        client.set(RUNS_ENDPOINT, {"workflow_runs": []})
        with tempfile.TemporaryDirectory() as temporary_directory:
            checkpoint = Path(temporary_directory) / "checkpoint.json"
            _write_checkpoint(
                checkpoint,
                _checkpoint_payload(
                    identity(),
                    state="prepared",
                    high_water_run_id=100,
                    retry_of_run_id=None,
                ),
            )
            payload = json.loads(checkpoint.read_text(encoding="utf-8"))
            payload["dispatch"]["high_water_run_id"] = 101
            checkpoint.write_text(json.dumps(payload), encoding="utf-8")
            checkpoint.chmod(0o600)
            with self.assertRaisesRegex(QualificationResumeSafetyError, "self digest"):
                self.run_blocked(client, checkpoint)

    def test_error_payload_redacts_private_absolute_paths(self) -> None:
        payload = safety_error_payload(QualificationResumeSafetyError("Failed at /Users/example/private/file.json"))

        self.assertEqual(payload["error"], "Failed at <local-path>")

    def test_runner_stale_fails_before_local_checkout_or_dispatch(self) -> None:
        client = self.configured_client()
        client.set("repos/cbusillo/BD_to_AVP/git/ref/heads/main", {"object": {"sha": "f" * 40}})
        with (
            tempfile.TemporaryDirectory() as temporary_directory,
            patch("scripts.release_qualification_resume.build_status", return_value=status_payload()),
            patch(
                "scripts.release_qualification_resume.resolve_evidence_binding",
                return_value=binding(runner_sha=MAIN_SHA),
            ),
            patch("scripts.release_qualification_resume._require_local_evidence_checkout") as local_checkout,
        ):
            result = resume_qualification(
                REPO_ROOT,
                RELEASE_TAG,
                client=client,
                checkpoint_path=Path(temporary_directory) / "checkpoint.json",
            )

        self.assertEqual(result.payload["state"], "runner_stale")
        local_checkout.assert_not_called()
        self.assertEqual(client.posts, [])

    def test_duplicate_open_pull_requests_fail_closed(self) -> None:
        client = self.configured_client()
        client.set(PR_ENDPOINT, [pull_request(number=42), pull_request(number=43)])
        with tempfile.TemporaryDirectory() as temporary_directory:
            with self.assertRaisesRegex(QualificationResumeSafetyError, "exactly one open pull request"):
                self.run_blocked(client, Path(temporary_directory) / "checkpoint.json")

        self.assertEqual(client.posts, [])

    def test_wrong_active_identity_fails_before_checkpoint_or_dispatch(self) -> None:
        client = self.configured_client()
        client.set(RUNS_ENDPOINT, {"workflow_runs": []})
        client.set("user", {"login": "shiny-code-bot"}, active_auth=True)
        with tempfile.TemporaryDirectory() as temporary_directory:
            checkpoint = Path(temporary_directory) / "checkpoint.json"
            with self.assertRaisesRegex(QualificationResumeSafetyError, "active GitHub identity"):
                self.run_blocked(
                    client,
                    checkpoint,
                    expected_main_sha=MAIN_SHA,
                    expected_manifest_sha256=MANIFEST_SHA,
                )
            self.assertFalse(checkpoint.exists())

        self.assertEqual(client.posts, [])

    def test_failed_run_requires_exact_retry_run_id(self) -> None:
        client = self.configured_client()
        failed = workflow_run(101, status="completed", conclusion="failure")
        client.set(RUNS_ENDPOINT, {"workflow_runs": [failed]})
        with tempfile.TemporaryDirectory() as temporary_directory:
            result = self.run_blocked(client, Path(temporary_directory) / "checkpoint.json")

        self.assertEqual(result.exit_code, EXIT_OPERATOR_REQUIRED)
        self.assertEqual(result.payload["state"], "failed")
        self.assertIn("--retry-run-id 101", result.payload["next_action"])
        self.assertEqual(client.posts, [])

    def test_exact_failed_run_retry_dispatches_new_run(self) -> None:
        client = self.configured_client()
        failed = workflow_run(101, status="completed", conclusion="failure")
        running = workflow_run(102, status="queued", conclusion=None)
        client.set_sequence(
            RUNS_ENDPOINT,
            [{"workflow_runs": [failed]}, {"workflow_runs": [running, failed]}],
        )
        client.set("user", {"login": "cbusillo"}, active_auth=True)
        with tempfile.TemporaryDirectory() as temporary_directory:
            result = self.run_blocked(
                client,
                Path(temporary_directory) / "checkpoint.json",
                expected_main_sha=MAIN_SHA,
                expected_manifest_sha256=MANIFEST_SHA,
                retry_run_id=101,
            )

        self.assertEqual(result.payload["state"], "running")
        self.assertEqual(result.payload["run"]["id"], 102)
        self.assertEqual(len(client.posts), 1)

    def test_successful_run_reports_available_artifact_without_download(self) -> None:
        client = self.configured_client()
        succeeded = workflow_run(101, status="completed", conclusion="success")
        client.set(RUNS_ENDPOINT, {"workflow_runs": [succeeded]})
        client.set(
            "repos/cbusillo/BD_to_AVP/actions/runs/101/jobs?per_page=100",
            {
                "jobs": [
                    {
                        "name": "Collect exact post-publication qualification receipts",
                        "status": "completed",
                        "conclusion": "success",
                    }
                ]
            },
        )
        client.set(
            "repos/cbusillo/BD_to_AVP/actions/runs/101/artifacts?per_page=100",
            {"artifacts": [artifact(101, expired=False)]},
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            result = self.run_blocked(
                client,
                Path(temporary_directory) / "checkpoint.json",
                observe_only=True,
            )

        self.assertEqual(result.exit_code, EXIT_OPERATOR_REQUIRED)
        self.assertEqual(result.payload["state"], "artifact_available")
        self.assertEqual(result.payload["artifact"]["state"], "available")
        self.assertEqual(client.byte_gets, [])
        self.assertEqual(client.posts, [])

    def test_successful_run_builds_read_only_reconciliation_plan(self) -> None:
        client = self.configured_client()
        succeeded = workflow_run(101, status="completed", conclusion="success")
        available_artifact = artifact(101, expired=False)
        client.set(RUNS_ENDPOINT, {"workflow_runs": [succeeded]})
        client.set(
            "repos/cbusillo/BD_to_AVP/actions/runs/101/jobs?per_page=100",
            {
                "jobs": [
                    {
                        "name": "Collect exact post-publication qualification receipts",
                        "status": "completed",
                        "conclusion": "success",
                    }
                ]
            },
        )
        client.set(
            "repos/cbusillo/BD_to_AVP/actions/runs/101/artifacts?per_page=100",
            {"artifacts": [available_artifact]},
        )
        plan = {"plan_sha256": "8" * 64, "requires_changes": True}
        bundle = ReconciliationBundle(plan=plan, files=())
        with (
            tempfile.TemporaryDirectory() as temporary_directory,
            patch(
                "scripts.release_qualification_resume.download_reconciliation_bundle",
                return_value=bundle,
            ) as planner,
        ):
            result = self.run_blocked(client, Path(temporary_directory) / "checkpoint.json")

        self.assertEqual(result.exit_code, EXIT_OPERATOR_REQUIRED)
        self.assertEqual(result.payload["state"], "reconciliation_planned")
        self.assertEqual(result.payload["reconciliation_plan"], plan)
        self.assertEqual(client.posts, [])
        planner.assert_called_once()

    def test_successful_run_applies_exact_authorized_plan(self) -> None:
        client = self.configured_client()
        succeeded = workflow_run(101, status="completed", conclusion="success")
        available_artifact = artifact(101, expired=False)
        client.set(RUNS_ENDPOINT, {"workflow_runs": [succeeded]})
        client.set(
            "repos/cbusillo/BD_to_AVP/actions/runs/101/jobs?per_page=100",
            {
                "jobs": [
                    {
                        "name": "Collect exact post-publication qualification receipts",
                        "status": "completed",
                        "conclusion": "success",
                    }
                ]
            },
        )
        client.set(
            "repos/cbusillo/BD_to_AVP/actions/runs/101/artifacts?per_page=100",
            {"artifacts": [available_artifact]},
        )
        plan = {"plan_sha256": "8" * 64, "requires_changes": True}
        bundle = ReconciliationBundle(plan=plan, files=())
        outcome = ApplyOutcome(
            state="reconciliation_applied",
            plan=plan,
            commit_sha="9" * 40,
            comment_id=77,
        )
        with (
            tempfile.TemporaryDirectory() as temporary_directory,
            patch(
                "scripts.release_qualification_resume.download_reconciliation_bundle",
                return_value=bundle,
            ),
            patch(
                "scripts.release_qualification_resume.start_reconciliation_apply",
                return_value=outcome,
            ) as apply,
        ):
            result = self.run_blocked(
                client,
                Path(temporary_directory) / "checkpoint.json",
                apply_plan_sha256="8" * 64,
            )

        self.assertEqual(result.exit_code, EXIT_SUCCESS)
        self.assertEqual(result.payload["state"], "reconciliation_applied")
        self.assertEqual(result.payload["reconciliation_result"]["commit_sha"], "9" * 40)
        self.assertEqual(result.payload["reconciliation_result"]["comment_id"], 77)
        apply.assert_called_once()

    def test_apply_checkpoint_continues_before_complete_early_return(self) -> None:
        client = self.configured_client()
        plan = {"plan_sha256": "8" * 64, "requires_changes": True}
        outcome = ApplyOutcome(
            state="reconciliation_applied",
            plan=plan,
            commit_sha="9" * 40,
            comment_id=77,
        )
        complete_status = status_payload()
        complete_status["groups"] = {"blocking": []}
        with tempfile.TemporaryDirectory() as temporary_directory:
            checkpoint = Path(temporary_directory) / "checkpoint.json"
            apply_checkpoint = checkpoint.with_name("checkpoint.apply.json")
            apply_checkpoint.write_text("{}", encoding="utf-8")
            with (
                patch("scripts.release_qualification_resume.build_status", return_value=complete_status),
                patch("scripts.release_qualification_resume.resolve_evidence_binding", return_value=binding()),
                patch(
                    "scripts.release_qualification_resume.continue_reconciliation_apply",
                    return_value=outcome,
                ) as continuation,
                patch(
                    "scripts.release_qualification_resume.reconciliation_checkpoint_summary",
                    return_value={"present": True, "state": "commented", "plan_sha256": "8" * 64},
                ),
            ):
                result = resume_qualification(
                    REPO_ROOT,
                    RELEASE_TAG,
                    client=client,
                    checkpoint_path=checkpoint,
                    apply_plan_sha256="8" * 64,
                )

        self.assertEqual(result.payload["state"], "reconciliation_applied")
        continuation.assert_called_once()

    def test_apply_checkpoint_requires_exact_plan_echo(self) -> None:
        client = self.configured_client()
        complete_status = status_payload()
        complete_status["groups"] = {"blocking": []}
        with tempfile.TemporaryDirectory() as temporary_directory:
            checkpoint = Path(temporary_directory) / "checkpoint.json"
            apply_checkpoint = checkpoint.with_name("checkpoint.apply.json")
            apply_checkpoint.write_text("{}", encoding="utf-8")
            with (
                patch("scripts.release_qualification_resume.build_status", return_value=complete_status),
                patch("scripts.release_qualification_resume.resolve_evidence_binding", return_value=binding()),
                patch(
                    "scripts.release_qualification_resume.reconciliation_checkpoint_summary",
                    return_value={"present": True, "state": "pushed", "plan_sha256": "8" * 64},
                ),
            ):
                result = resume_qualification(
                    REPO_ROOT,
                    RELEASE_TAG,
                    client=client,
                    checkpoint_path=checkpoint,
                )

        self.assertEqual(result.exit_code, EXIT_OPERATOR_REQUIRED)
        self.assertEqual(result.payload["state"], "reconciliation_apply_pending")

    def test_corrupt_apply_checkpoint_is_translated_to_resume_safety_error(self) -> None:
        client = self.configured_client()
        complete_status = status_payload()
        complete_status["groups"] = {"blocking": []}
        with tempfile.TemporaryDirectory() as temporary_directory:
            checkpoint = Path(temporary_directory) / "checkpoint.json"
            apply_checkpoint = checkpoint.with_name("checkpoint.apply.json")
            apply_checkpoint.write_text('{"unexpected":true}\n', encoding="utf-8")
            apply_checkpoint.chmod(0o600)
            with (
                patch("scripts.release_qualification_resume.build_status", return_value=complete_status),
                patch("scripts.release_qualification_resume.resolve_evidence_binding", return_value=binding()),
            ):
                with self.assertRaisesRegex(QualificationResumeSafetyError, "keys changed"):
                    resume_qualification(
                        REPO_ROOT,
                        RELEASE_TAG,
                        client=client,
                        checkpoint_path=checkpoint,
                    )

    def test_completed_apply_checkpoint_revalidates_without_plan_echo(self) -> None:
        client = self.configured_client()
        plan = {"plan_sha256": "8" * 64, "requires_changes": True}
        outcome = ApplyOutcome(
            state="reconciliation_applied",
            plan=plan,
            commit_sha="9" * 40,
            comment_id=77,
        )
        complete_status = status_payload()
        complete_status["groups"] = {"blocking": []}
        with tempfile.TemporaryDirectory() as temporary_directory:
            checkpoint = Path(temporary_directory) / "checkpoint.json"
            apply_checkpoint = checkpoint.with_name("checkpoint.apply.json")
            apply_checkpoint.write_text("{}", encoding="utf-8")
            with (
                patch("scripts.release_qualification_resume.build_status", return_value=complete_status),
                patch("scripts.release_qualification_resume.resolve_evidence_binding", return_value=binding()),
                patch(
                    "scripts.release_qualification_resume.continue_reconciliation_apply",
                    return_value=outcome,
                ) as continuation,
                patch(
                    "scripts.release_qualification_resume.reconciliation_checkpoint_summary",
                    return_value={"present": True, "state": "commented", "plan_sha256": "8" * 64},
                ),
            ):
                result = resume_qualification(
                    REPO_ROOT,
                    RELEASE_TAG,
                    client=client,
                    checkpoint_path=checkpoint,
                )

        self.assertEqual(result.exit_code, EXIT_SUCCESS)
        self.assertEqual(result.payload["state"], "reconciliation_applied")
        continuation.assert_called_once()

    def test_expired_artifact_requires_exact_retry(self) -> None:
        client = self.configured_client()
        succeeded = workflow_run(101, status="completed", conclusion="success")
        client.set(RUNS_ENDPOINT, {"workflow_runs": [succeeded]})
        client.set(
            "repos/cbusillo/BD_to_AVP/actions/runs/101/jobs?per_page=100",
            {
                "jobs": [
                    {
                        "name": "Collect exact post-publication qualification receipts",
                        "status": "completed",
                        "conclusion": "success",
                    }
                ]
            },
        )
        client.set(
            "repos/cbusillo/BD_to_AVP/actions/runs/101/artifacts?per_page=100",
            {"artifacts": [artifact(101, expired=True)]},
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            result = self.run_blocked(client, Path(temporary_directory) / "checkpoint.json")

        self.assertEqual(result.payload["state"], "artifact_expired")
        self.assertIn("--retry-run-id 101", result.payload["next_action"])
        self.assertEqual(client.posts, [])

    def test_remote_main_move_between_preflight_and_dispatch_fails_closed(self) -> None:
        client = self.configured_client()
        client.set_sequence(
            "repos/cbusillo/BD_to_AVP/git/ref/heads/main",
            [{"object": {"sha": MAIN_SHA}}, {"object": {"sha": "f" * 40}}],
        )
        client.set(RUNS_ENDPOINT, {"workflow_runs": []})
        client.set("user", {"login": "cbusillo"}, active_auth=True)
        with tempfile.TemporaryDirectory() as temporary_directory:
            checkpoint = Path(temporary_directory) / "checkpoint.json"
            with self.assertRaisesRegex(QualificationResumeSafetyError, "Protected main moved"):
                self.run_blocked(
                    client,
                    checkpoint,
                    expected_main_sha=MAIN_SHA,
                    expected_manifest_sha256=MANIFEST_SHA,
                )
            self.assertFalse(checkpoint.exists())

        self.assertEqual(client.posts, [])

    def test_multiple_active_exact_runs_fail_closed(self) -> None:
        client = self.configured_client()
        client.set(
            RUNS_ENDPOINT,
            {
                "workflow_runs": [
                    workflow_run(102, status="queued", conclusion=None),
                    workflow_run(101, status="in_progress", conclusion=None),
                ]
            },
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            with self.assertRaisesRegex(QualificationResumeSafetyError, "Multiple active exact"):
                self.run_blocked(client, Path(temporary_directory) / "checkpoint.json")

    def test_workflow_run_name_binds_full_manifest_digest(self) -> None:
        workflow = (REPO_ROOT / ".github/workflows/milestone-qualification.yml").read_text(encoding="utf-8")

        self.assertEqual(identity().workflow_display_title, f"Milestone {RELEASE_TAG} {MANIFEST_SHA}")
        self.assertIn(
            "run-name: Milestone ${{ inputs.candidate_tag }} ${{ inputs.manifest_sha256 }}",
            workflow,
        )

    def test_dynamic_workflow_run_name_matches_the_bound_display_title(self) -> None:
        run = workflow_run(101, status="completed", conclusion="failure")
        run["name"] = f"Milestone {RELEASE_TAG} {MANIFEST_SHA}"

        self.assertTrue(_run_matches(run, identity()))


if __name__ == "__main__":
    unittest.main()
