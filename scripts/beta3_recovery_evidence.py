from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import xml.etree.ElementTree as ET

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, cast
from urllib.error import HTTPError
from urllib.parse import quote, unquote, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


REPO_ROOT = Path(__file__).resolve().parents[1]
BETA3_RECOVERY_EVIDENCE_PATH = REPO_ROOT / "docs" / "release-evidence" / "v0.3.0-beta.3-recovery.json"
BETA3_RECOVERY_EVIDENCE_SHA256 = "31bb6aba336eeca2400501c469c99b2682c6f1ff768dd43e54fa1cb230ba6a0a"
GITHUB_API_ROOT = "https://api.github.com"
PYPI_URL = "https://pypi.org/pypi/bd-to-avp/json"
PAGES_APPCAST_URL = "https://cbusillo.github.io/BD_to_AVP/appcast.xml"
SPARKLE_NAMESPACE = "http://www.andymatuschak.org/xml-namespaces/sparkle"
MAX_REMOTE_BODY_BYTES = 16 * 1024 * 1024
ALLOWED_REMOTE_HOSTS = frozenset({"api.github.com", "pypi.org", "cbusillo.github.io"})
PRODUCTION_REPOSITORY = "cbusillo/BD_to_AVP"
PRERELEASE_WORKFLOW_REF = f"{PRODUCTION_REPOSITORY}/.github/workflows/prerelease.yml@refs/heads/main"


class Beta3RecoveryEvidenceError(RuntimeError):
    pass


@dataclass(frozen=True)
class RemoteResponse:
    status: int
    body: bytes


RemoteFetcher = Callable[[str], RemoteResponse]


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise Beta3RecoveryEvidenceError(f"Duplicate JSON key in recovery evidence: {key!r}")
        result[key] = value
    return result


def _mapping(value: object, description: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise Beta3RecoveryEvidenceError(f"{description} must be a JSON object.")
    return cast(Mapping[str, Any], value)


def _sequence(value: object, description: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise Beta3RecoveryEvidenceError(f"{description} must be a JSON array.")
    return value


def _string(mapping: Mapping[str, Any], key: str, description: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise Beta3RecoveryEvidenceError(f"{description} is missing non-empty string {key!r}.")
    return value


def _integer(mapping: Mapping[str, Any], key: str, description: str) -> int:
    value = mapping.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise Beta3RecoveryEvidenceError(f"{description} is missing positive integer {key!r}.")
    return value


def _boolean(mapping: Mapping[str, Any], key: str, description: str) -> bool:
    value = mapping.get(key)
    if not isinstance(value, bool):
        raise Beta3RecoveryEvidenceError(f"{description} is missing boolean {key!r}.")
    return value


def load_beta3_recovery_evidence(
    path: Path = BETA3_RECOVERY_EVIDENCE_PATH,
) -> dict[str, Any]:
    try:
        content = path.read_bytes()
    except OSError as error:
        raise Beta3RecoveryEvidenceError(f"Unable to read Beta 3 recovery evidence from {path}: {error}") from error
    digest = hashlib.sha256(content).hexdigest()
    if digest != BETA3_RECOVERY_EVIDENCE_SHA256:
        raise Beta3RecoveryEvidenceError(
            f"Beta 3 recovery evidence digest {digest} does not match the reviewed receipt."
        )
    try:
        value = json.loads(content, object_pairs_hook=_reject_duplicate_json_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise Beta3RecoveryEvidenceError(f"Beta 3 recovery evidence is invalid JSON: {error}") from error
    evidence = dict(_mapping(value, "Beta 3 recovery evidence"))
    expected_scalars = {
        "schema": "bd_to_avp.release_recovery",
        "schema_version": 2,
        "repository": "cbusillo/BD_to_AVP",
    }
    for key, expected in expected_scalars.items():
        if evidence.get(key) != expected:
            raise Beta3RecoveryEvidenceError(f"Beta 3 recovery evidence {key!r} must be {expected!r}.")
    expected_repository_identity = {
        "id": 771225421,
        "node_id": "R_kgDOLff3TQ",
        "full_name": "cbusillo/BD_to_AVP",
        "private": False,
        "archived": False,
        "disabled": False,
    }
    repository_identity = _mapping(evidence.get("repository_identity"), "Repository identity")
    if dict(repository_identity) != expected_repository_identity:
        raise Beta3RecoveryEvidenceError("Beta 3 recovery evidence does not identify the production repository.")
    expected_source_identity = {
        "base_commit": "e4d89a54412b50b556f51ea3c32034a1dc015eb6",
        "tree": "4ff668a0f35ca5fe99880655e9f1c3de244778c5",
        "files": {
            "pyproject.toml": "4a01dc9e5dedf55899cc1e903a40514b93a9507ee88f22d14a6a19beb91568af",
            "uv.lock": "94015cc2525bcb84c3129f391593fa9fc624679c8592f630f7024a8ffd08fc20",
            "macos/project.yml": "8fc365f4e3d8032ae9eb22734030ee50f0b7e6759b8a5952032266f6ba331ea4",
        },
    }
    source_identity = _mapping(evidence.get("source_identity"), "Recovery source identity")
    if dict(source_identity) != expected_source_identity:
        raise Beta3RecoveryEvidenceError("Beta 3 recovery evidence does not identify the reviewed source tree.")
    transition = _mapping(evidence.get("transition"), "Recovery transition")
    source = _mapping(transition.get("source"), "Recovery source")
    target = _mapping(transition.get("target"), "Recovery target")
    expected_source = {"package_version": "0.3.0rc1", "public_version": "0.3.0-rc.1", "build": "147"}
    expected_target = {
        "package_version": "0.3.0b3",
        "public_version": "0.3.0-beta.3",
        "release_tag": "v0.3.0-beta.3",
        "build": "148",
    }
    if dict(source) != expected_source or dict(target) != expected_target:
        raise Beta3RecoveryEvidenceError("Beta 3 recovery evidence does not describe the one approved transition.")
    pages = _mapping(evidence.get("pages"), "Pages evidence")
    capture_relative = Path(_string(pages, "capture_path", "Pages evidence"))
    capture_path = (REPO_ROOT / capture_relative).resolve()
    if REPO_ROOT.resolve() not in capture_path.parents:
        raise Beta3RecoveryEvidenceError("Pages evidence capture path escapes the repository.")
    try:
        capture = capture_path.read_bytes()
    except OSError as error:
        raise Beta3RecoveryEvidenceError(f"Unable to read Pages evidence capture: {error}") from error
    if hashlib.sha256(capture).hexdigest() != _string(pages, "capture_sha256", "Pages evidence"):
        raise Beta3RecoveryEvidenceError("Pages evidence capture digest does not match the reviewed receipt.")
    captured_remote_bytes = capture[:-1] if capture.endswith(b"\n") else capture
    if hashlib.sha256(captured_remote_bytes).hexdigest() != _string(pages, "appcast_sha256", "Pages evidence"):
        raise Beta3RecoveryEvidenceError(
            "Pages evidence capture does not reproduce the observed remote appcast digest."
        )
    return evidence


@lru_cache(maxsize=1)
def _github_token() -> str:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise Beta3RecoveryEvidenceError(
            "Authenticated GitHub access is required to verify draft-release absence. "
            "Set GH_TOKEN or run gh auth login."
        ) from error
    token = result.stdout.strip()
    if not token:
        raise Beta3RecoveryEvidenceError("GitHub CLI returned an empty authentication token.")
    return token


def _origin(url: str) -> tuple[str, str, int | None]:
    parsed = urlsplit(url)
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port


class _SameOriginRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        absolute_url = urljoin(request.full_url, new_url)
        if _origin(absolute_url) != _origin(request.full_url):
            raise Beta3RecoveryEvidenceError(
                f"Remote evidence request refused a cross-origin redirect: {request.full_url} -> {absolute_url}"
            )
        return super().redirect_request(request, file_pointer, code, message, headers, absolute_url)


@lru_cache(maxsize=1)
def _remote_opener():
    return build_opener(_SameOriginRedirectHandler())


def _read_bounded(response, url: str) -> bytes:
    body = response.read(MAX_REMOTE_BODY_BYTES + 1)
    if len(body) > MAX_REMOTE_BODY_BYTES:
        raise Beta3RecoveryEvidenceError(f"Remote evidence response exceeded the size limit: {url}")
    return body


def fetch_remote(url: str) -> RemoteResponse:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in ALLOWED_REMOTE_HOSTS
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise Beta3RecoveryEvidenceError(f"Remote evidence URL is not approved: {url}")
    headers = {
        "Accept": "application/vnd.github+json" if url.startswith(GITHUB_API_ROOT) else "application/octet-stream",
        "Cache-Control": "no-cache",
        "User-Agent": "bd-to-avp-beta3-recovery-verifier",
    }
    if url.startswith(GITHUB_API_ROOT):
        headers["Authorization"] = f"Bearer {_github_token()}"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    request = Request(url, headers=headers, method="GET")
    try:
        with _remote_opener().open(request, timeout=45) as response:
            final_url = response.geturl()
            if _origin(final_url) != _origin(url):
                raise Beta3RecoveryEvidenceError(f"Remote evidence response changed origin: {url} -> {final_url}")
            return RemoteResponse(status=response.status, body=_read_bounded(response, url))
    except HTTPError as error:
        body = _read_bounded(error, url)
        if error.code == 404:
            return RemoteResponse(status=404, body=body)
        raise Beta3RecoveryEvidenceError(f"Remote evidence request failed with HTTP {error.code}: {url}") from error
    except OSError as error:
        raise Beta3RecoveryEvidenceError(f"Remote evidence request failed: {url}: {error}") from error


def _json_response(fetcher: RemoteFetcher, url: str, description: str) -> Any:
    response = fetcher(url)
    if response.status != 200:
        raise Beta3RecoveryEvidenceError(f"{description} returned HTTP {response.status}, expected 200.")
    try:
        return json.loads(response.body, object_pairs_hook=_reject_duplicate_json_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise Beta3RecoveryEvidenceError(f"{description} returned invalid JSON.") from error


def _expect_fields(actual: Mapping[str, Any], expected: Mapping[str, Any], description: str) -> None:
    mismatches = [
        f"{key}={actual.get(key)!r}, expected {value!r}" for key, value in expected.items() if actual.get(key) != value
    ]
    if mismatches:
        raise Beta3RecoveryEvidenceError(f"{description} mismatch: {', '.join(mismatches)}")


def _verify_repository_identity(
    fetcher: RemoteFetcher,
    repository: str,
    evidence: Mapping[str, Any],
    *,
    allow_github_actions_contents_write_token: bool,
) -> None:
    expected = _mapping(evidence.get("repository_identity"), "Repository identity")
    actual = _mapping(
        _json_response(fetcher, f"{GITHUB_API_ROOT}/repos/{repository}", "GitHub repository identity"),
        "GitHub repository identity",
    )
    _expect_fields(actual, expected, "GitHub repository identity")
    permissions = _mapping(actual.get("permissions"), "GitHub repository token permissions")
    if permissions.get("push") is not True and not allow_github_actions_contents_write_token:
        raise Beta3RecoveryEvidenceError(
            "GitHub repository token must have push visibility so draft-release absence is authoritative."
        )


def _validate_github_actions_publication_context(repository: str, expected_sha: str | None) -> None:
    expected_context = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_REPOSITORY": PRODUCTION_REPOSITORY,
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_REF_PROTECTED": "true",
        "GITHUB_SHA": expected_sha,
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_WORKFLOW_REF": PRERELEASE_WORKFLOW_REF,
        "GITHUB_ACTOR": "shiny-code-bot",
        "GITHUB_TRIGGERING_ACTOR": "shiny-code-bot",
    }
    mismatches = [
        f"{name}={os.environ.get(name)!r}, expected {expected!r}"
        for name, expected in expected_context.items()
        if os.environ.get(name) != expected
    ]
    if repository != PRODUCTION_REPOSITORY:
        mismatches.append(f"repository={repository!r}, expected {PRODUCTION_REPOSITORY!r}")
    if mismatches:
        raise Beta3RecoveryEvidenceError("GitHub Actions publication context mismatch: " + ", ".join(mismatches))


def _verify_failed_run(fetcher: RemoteFetcher, repository: str, evidence: Mapping[str, Any]) -> None:
    run_evidence = _mapping(evidence.get("failed_run"), "Failed run evidence")
    run_id = _integer(run_evidence, "id", "Failed run evidence")
    api_root = f"{GITHUB_API_ROOT}/repos/{repository}"
    run = _mapping(
        _json_response(fetcher, f"{api_root}/actions/runs/{run_id}", "Failed release run"),
        "Failed release run",
    )
    actor = _mapping(run.get("actor"), "Failed release run actor")
    triggering_actor = _mapping(run.get("triggering_actor"), "Failed release run triggering actor")
    run_repository = _mapping(run.get("repository"), "Failed release run repository")
    head_repository = _mapping(run.get("head_repository"), "Failed release run head repository")
    expected_run = {
        "id": run_id,
        "workflow_id": _integer(run_evidence, "workflow_id", "Failed run evidence"),
        "run_number": _integer(run_evidence, "run_number", "Failed run evidence"),
        "event": _string(run_evidence, "event", "Failed run evidence"),
        "head_branch": _string(run_evidence, "head_branch", "Failed run evidence"),
        "head_sha": _string(run_evidence, "head_sha", "Failed run evidence"),
        "run_attempt": _integer(run_evidence, "run_attempt", "Failed run evidence"),
        "path": _string(run_evidence, "workflow_path", "Failed run evidence"),
        "name": _string(run_evidence, "workflow_name", "Failed run evidence"),
        "display_title": _string(run_evidence, "display_title", "Failed run evidence"),
        "status": _string(run_evidence, "status", "Failed run evidence"),
        "conclusion": _string(run_evidence, "conclusion", "Failed run evidence"),
    }
    _expect_fields(run, expected_run, "Failed release run")
    _expect_fields(actor, {"login": _string(run_evidence, "actor", "Failed run evidence")}, "Run actor")
    _expect_fields(
        triggering_actor,
        {"login": _string(run_evidence, "triggering_actor", "Failed run evidence")},
        "Run triggering actor",
    )
    _expect_fields(
        run_repository,
        {"full_name": _string(run_evidence, "repository", "Failed run evidence")},
        "Run repository",
    )
    _expect_fields(
        head_repository,
        {"full_name": _string(run_evidence, "head_repository", "Failed run evidence")},
        "Run head repository",
    )

    run_attempt = _integer(run_evidence, "run_attempt", "Failed run evidence")
    jobs_payload = _mapping(
        _json_response(
            fetcher,
            f"{api_root}/actions/runs/{run_id}/attempts/{run_attempt}/jobs?per_page=100",
            "Failed run jobs",
        ),
        "Failed run jobs",
    )
    jobs = [_mapping(job, "Failed run job") for job in _sequence(jobs_payload.get("jobs"), "Failed run jobs")]
    expected_jobs = sorted(
        [
            dict(_mapping(job, "Failed run evidence job"))
            for job in _sequence(run_evidence.get("jobs"), "Failed run evidence jobs")
        ],
        key=lambda job: str(job.get("name")),
    )
    actual_jobs = sorted(
        [{"name": job.get("name"), "conclusion": job.get("conclusion")} for job in jobs],
        key=lambda job: str(job.get("name")),
    )
    if jobs_payload.get("total_count") != len(expected_jobs) or actual_jobs != expected_jobs:
        raise Beta3RecoveryEvidenceError("Failed release run job set changed after the recovery receipt.")
    if any(job.get("status") != "completed" for job in jobs):
        raise Beta3RecoveryEvidenceError("Failed release run contains a job that is no longer completed.")
    jobs_by_name = {str(job.get("name", "")): job for job in jobs}
    if len(jobs_by_name) != len(jobs):
        raise Beta3RecoveryEvidenceError("Failed release run contains duplicate job names.")
    failed_job_name = _string(run_evidence, "failed_job", "Failed run evidence")
    failed_job = jobs_by_name.get(failed_job_name)
    if failed_job is None or failed_job.get("conclusion") != "failure":
        raise Beta3RecoveryEvidenceError(f"Failed run job {failed_job_name!r} is not still recorded as failed.")
    steps = [_mapping(step, "Failed job step") for step in _sequence(failed_job.get("steps"), "Failed job steps")]
    steps_by_name = {str(step.get("name", "")): step for step in steps}
    failed_step_name = _string(run_evidence, "failed_step", "Failed run evidence")
    upload_step_name = _string(run_evidence, "package_upload_step", "Failed run evidence")
    if steps_by_name.get(failed_step_name, {}).get("conclusion") != "failure":
        raise Beta3RecoveryEvidenceError(f"Failed run step {failed_step_name!r} is not still recorded as failed.")
    if steps_by_name.get(upload_step_name, {}).get("conclusion") != "skipped":
        raise Beta3RecoveryEvidenceError(f"Package upload step {upload_step_name!r} was not skipped.")
    for job_name in _sequence(run_evidence.get("skipped_jobs"), "Failed run skipped jobs"):
        if not isinstance(job_name, str) or jobs_by_name.get(job_name, {}).get("conclusion") != "skipped":
            raise Beta3RecoveryEvidenceError(f"Release boundary job {job_name!r} was not skipped.")

    artifacts = _mapping(
        _json_response(
            fetcher,
            f"{api_root}/actions/runs/{run_id}/artifacts?per_page=100",
            "Failed run artifacts",
        ),
        "Failed run artifacts",
    )
    if artifacts.get("total_count") != run_evidence.get("artifact_count") or artifacts.get("artifacts") != []:
        raise Beta3RecoveryEvidenceError("Failed release run artifact state no longer matches the recovery receipt.")


def _all_releases(fetcher: RemoteFetcher, api_root: str) -> list[Mapping[str, Any]]:
    releases: list[Mapping[str, Any]] = []
    for page in range(1, 11):
        page_values = _json_response(
            fetcher,
            f"{api_root}/releases?per_page=100&page={page}",
            f"GitHub Releases page {page}",
        )
        page_releases = [_mapping(value, "GitHub Release") for value in _sequence(page_values, "GitHub Releases")]
        releases.extend(page_releases)
        if len(page_releases) < 100:
            return releases
    raise Beta3RecoveryEvidenceError("GitHub Releases exceeded the bounded pagination limit.")


def _verify_release_absence(
    fetcher: RemoteFetcher,
    repository: str,
    evidence: Mapping[str, Any],
    *,
    allow_beta3_draft: bool,
    expected_sha: str | None,
) -> None:
    api_root = f"{GITHUB_API_ROOT}/repos/{repository}"
    absence = _mapping(evidence.get("absent_github_state"), "Absent GitHub state")
    target_tag = _string(
        _mapping(_mapping(evidence.get("transition"), "Recovery transition").get("target"), "Recovery target"),
        "release_tag",
        "Recovery target",
    )
    releases = _all_releases(fetcher, api_root)
    for tag in _sequence(absence.get("releases"), "Absent GitHub releases"):
        published = [
            release for release in releases if release.get("tag_name") == tag and release.get("draft") is False
        ]
        if published:
            raise Beta3RecoveryEvidenceError(f"Published GitHub Release {tag!r} now exists.")
    resumable_draft = False
    for tag in _sequence(absence.get("drafts"), "Absent GitHub drafts"):
        drafts = [release for release in releases if release.get("tag_name") == tag and release.get("draft") is True]
        if tag == target_tag and allow_beta3_draft:
            if len(drafts) > 1:
                raise Beta3RecoveryEvidenceError(f"Multiple resumable Beta 3 drafts exist for {target_tag!r}.")
            if drafts:
                if expected_sha is None:
                    raise Beta3RecoveryEvidenceError(
                        "Expected protected-main SHA is required to validate a Beta 3 draft."
                    )
                _expect_fields(
                    drafts[0],
                    {
                        "tag_name": target_tag,
                        "name": target_tag,
                        "draft": True,
                        "prerelease": True,
                        "target_commitish": expected_sha,
                    },
                    "Resumable Beta 3 draft",
                )
                resumable_draft = True
        elif drafts:
            raise Beta3RecoveryEvidenceError(f"Draft GitHub Release {tag!r} now exists.")

    tags = [str(tag) for tag in _sequence(absence.get("tags"), "Absent GitHub tags")]
    for tag in tags:
        tag_url = f"{api_root}/git/ref/tags/{quote(tag, safe='')}"
        response = fetcher(tag_url)
        if response.status == 404:
            continue
        if tag != target_tag or not resumable_draft or response.status != 200 or expected_sha is None:
            raise Beta3RecoveryEvidenceError(f"Git tag {tag!r} now exists; Beta 3 recovery premise is invalid.")
        try:
            reference = _mapping(
                json.loads(response.body, object_pairs_hook=_reject_duplicate_json_keys),
                "Resumable Beta 3 tag",
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise Beta3RecoveryEvidenceError("Resumable Beta 3 tag returned invalid JSON.") from error
        _expect_fields(reference, {"ref": f"refs/tags/{target_tag}"}, "Resumable Beta 3 tag")
        tag_object = _mapping(reference.get("object"), "Resumable Beta 3 tag object")
        object_type = tag_object.get("type")
        object_sha = tag_object.get("sha")
        if object_type == "tag" and isinstance(object_sha, str):
            annotated_tag = _mapping(
                _json_response(
                    fetcher,
                    f"{api_root}/git/tags/{object_sha}",
                    "Resumable annotated Beta 3 tag",
                ),
                "Resumable annotated Beta 3 tag",
            )
            tag_object = _mapping(annotated_tag.get("object"), "Resumable annotated Beta 3 tag target")
            object_type = tag_object.get("type")
            object_sha = tag_object.get("sha")
        if object_type != "commit" or object_sha != expected_sha:
            raise Beta3RecoveryEvidenceError("Resumable Beta 3 tag does not target the expected protected-main SHA.")


def _verify_latest_pages_and_pypi(fetcher: RemoteFetcher, repository: str, evidence: Mapping[str, Any]) -> None:
    api_root = f"{GITHUB_API_ROOT}/repos/{repository}"
    latest_evidence = _mapping(evidence.get("github_latest"), "GitHub Latest evidence")
    latest = _mapping(_json_response(fetcher, f"{api_root}/releases/latest", "GitHub Latest release"), "GitHub Latest")
    _expect_fields(
        latest,
        {
            "id": _integer(latest_evidence, "release_id", "GitHub Latest evidence"),
            "tag_name": _string(latest_evidence, "release_tag", "GitHub Latest evidence"),
            "target_commitish": _string(latest_evidence, "target_sha", "GitHub Latest evidence"),
            "immutable": _boolean(latest_evidence, "immutable", "GitHub Latest evidence"),
            "draft": False,
            "prerelease": False,
        },
        "GitHub Latest release",
    )

    pages_evidence = _mapping(evidence.get("pages"), "Pages evidence")
    pages = _mapping(_json_response(fetcher, f"{api_root}/pages", "GitHub Pages state"), "GitHub Pages state")
    _expect_fields(
        pages,
        {
            "public": _boolean(pages_evidence, "public", "Pages evidence"),
            "build_type": _string(pages_evidence, "build_type", "Pages evidence"),
        },
        "GitHub Pages state",
    )
    appcast_response = fetcher(f"{PAGES_APPCAST_URL}?beta3-recovery-audit=1")
    if appcast_response.status != 200:
        raise Beta3RecoveryEvidenceError(
            f"Published Pages appcast returned HTTP {appcast_response.status}, expected 200."
        )
    appcast_digest = hashlib.sha256(appcast_response.body).hexdigest()
    if appcast_digest != _string(pages_evidence, "appcast_sha256", "Pages evidence"):
        raise Beta3RecoveryEvidenceError("Published Pages appcast digest changed after the recovery receipt.")
    try:
        channel = ET.fromstring(appcast_response.body).find("channel")
    except ET.ParseError as error:
        raise Beta3RecoveryEvidenceError("Published Pages appcast is invalid XML.") from error
    item = channel.find("item") if channel is not None else None
    if item is None:
        raise Beta3RecoveryEvidenceError("Published Pages appcast has no release item.")
    short_version = item.findtext(f"{{{SPARKLE_NAMESPACE}}}shortVersionString")
    build = item.findtext(f"{{{SPARKLE_NAMESPACE}}}version")
    enclosure = item.find("enclosure")
    enclosure_url = enclosure.get("url", "") if enclosure is not None else ""
    parts = urlsplit(enclosure_url).path.split("/")
    release_tag = unquote(parts[parts.index("download") + 1]) if "download" in parts else ""
    expected_appcast = {
        "short_version": _string(pages_evidence, "short_version", "Pages evidence"),
        "build": _string(pages_evidence, "build", "Pages evidence"),
        "release_tag": _string(pages_evidence, "release_tag", "Pages evidence"),
    }
    actual_appcast = {"short_version": short_version, "build": build, "release_tag": release_tag}
    if actual_appcast != expected_appcast:
        raise Beta3RecoveryEvidenceError(
            f"Published Pages appcast identity {actual_appcast!r} does not match {expected_appcast!r}."
        )

    pypi_evidence = _mapping(evidence.get("pypi"), "PyPI evidence")
    pypi = _mapping(_json_response(fetcher, PYPI_URL, "PyPI project state"), "PyPI project state")
    info = _mapping(pypi.get("info"), "PyPI project info")
    releases = _mapping(pypi.get("releases"), "PyPI project releases")
    if info.get("version") != _string(pypi_evidence, "latest_version", "PyPI evidence"):
        raise Beta3RecoveryEvidenceError("PyPI Latest version changed after the recovery receipt.")
    for version in _sequence(pypi_evidence.get("absent_versions"), "Absent PyPI versions"):
        if version in releases:
            raise Beta3RecoveryEvidenceError(f"PyPI version {version!r} now exists.")


def _verify_retired_previews(fetcher: RemoteFetcher, repository: str, evidence: Mapping[str, Any]) -> None:
    api_root = f"{GITHUB_API_ROOT}/repos/{repository}"
    for preview_value in _sequence(evidence.get("retired_preview_releases"), "Retired Preview releases"):
        preview = _mapping(preview_value, "Retired Preview release")
        tag = _string(preview, "tag", "Retired Preview release")
        release = _mapping(
            _json_response(
                fetcher,
                f"{api_root}/releases/tags/{quote(tag, safe='')}",
                f"Retired Preview release {tag}",
            ),
            f"Retired Preview release {tag}",
        )
        _expect_fields(
            release,
            {
                "id": _integer(preview, "release_id", f"Retired Preview release {tag}"),
                "tag_name": tag,
                "draft": False,
                "prerelease": True,
                "immutable": _boolean(preview, "immutable", f"Retired Preview release {tag}"),
                "target_commitish": _string(preview, "target_sha", f"Retired Preview release {tag}"),
            },
            f"Retired Preview release {tag}",
        )
        actual_assets = sorted(
            [
                {
                    "asset_id": asset.get("id"),
                    "name": asset.get("name"),
                    "size": asset.get("size"),
                    "digest": asset.get("digest"),
                }
                for asset in _sequence(release.get("assets"), f"Retired Preview release {tag} assets")
                if isinstance(asset, Mapping)
            ],
            key=lambda asset: str(asset["name"]),
        )
        expected_assets = sorted(
            [
                dict(_mapping(asset, f"Retired Preview release {tag} evidence asset"))
                for asset in _sequence(
                    preview.get("assets"),
                    f"Retired Preview release {tag} evidence assets",
                )
            ],
            key=lambda asset: str(asset["name"]),
        )
        if actual_assets != expected_assets:
            raise Beta3RecoveryEvidenceError(f"Retired Preview release {tag!r} assets changed.")


def verify_beta3_remote_state(
    evidence: Mapping[str, Any],
    *,
    fetcher: RemoteFetcher = fetch_remote,
    allow_beta3_draft: bool = False,
    expected_sha: str | None = None,
    allow_github_actions_contents_write_token: bool = False,
) -> None:
    repository = _string(evidence, "repository", "Beta 3 recovery evidence")
    valid_expected_sha = (
        expected_sha is not None
        and len(expected_sha) == 40
        and all(character in "0123456789abcdef" for character in expected_sha)
    )
    if allow_beta3_draft and not valid_expected_sha:
        raise Beta3RecoveryEvidenceError("Expected protected-main SHA must be a full lowercase Git SHA.")
    if allow_github_actions_contents_write_token:
        if not allow_beta3_draft:
            raise Beta3RecoveryEvidenceError(
                "GitHub Actions contents-write token allowance is valid only for publication preflight."
            )
        _validate_github_actions_publication_context(repository, expected_sha)
    _verify_repository_identity(
        fetcher,
        repository,
        evidence,
        allow_github_actions_contents_write_token=allow_github_actions_contents_write_token,
    )
    _verify_failed_run(fetcher, repository, evidence)
    _verify_release_absence(
        fetcher,
        repository,
        evidence,
        allow_beta3_draft=allow_beta3_draft,
        expected_sha=expected_sha,
    )
    _verify_latest_pages_and_pypi(fetcher, repository, evidence)
    _verify_retired_previews(fetcher, repository, evidence)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify the live remote premises for the Beta 3 recovery.")
    parser.add_argument("--evidence", type=Path, default=BETA3_RECOVERY_EVIDENCE_PATH)
    parser.add_argument("--allow-beta3-draft", action="store_true")
    parser.add_argument("--allow-github-actions-contents-write-token", action="store_true")
    parser.add_argument("--expected-sha")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.allow_beta3_draft and not args.expected_sha:
        raise Beta3RecoveryEvidenceError("--expected-sha is required with --allow-beta3-draft.")
    if args.allow_github_actions_contents_write_token and not args.allow_beta3_draft:
        raise Beta3RecoveryEvidenceError("--allow-github-actions-contents-write-token requires --allow-beta3-draft.")
    evidence = load_beta3_recovery_evidence(args.evidence)
    verify_beta3_remote_state(
        evidence,
        allow_beta3_draft=args.allow_beta3_draft,
        expected_sha=args.expected_sha,
        allow_github_actions_contents_write_token=args.allow_github_actions_contents_write_token,
    )
    print(
        json.dumps(
            {
                "evidence_sha256": BETA3_RECOVERY_EVIDENCE_SHA256,
                "repository": evidence["repository"],
                "verified": True,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
