from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import hmac
import json
import os
import re

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote
from urllib.request import Request, urlopen


REPOSITORY = "cbusillo/BD_to_AVP"
REPO_ROOT = Path(__file__).resolve().parents[1]
ENGINE_WORKFLOW_PATH = ".github/workflows/release-engine.yml"
RELEASE_FREEZES_PATH = REPO_ROOT / ".github" / "release-freezes.json"
REQUIRED_REF = "refs/heads/main"
REQUIRED_EVENT = "workflow_dispatch"
REQUIRED_ACTOR = "shiny-code-bot"
APPROVAL_ENVIRONMENT = "macos-signing"
OIDC_AUDIENCE = "bd-to-avp-release-engine"
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
STABLE_ROUTE = "stable"
PRERELEASE_ROUTE = "prerelease"
STABLE_WORKFLOW_NAME = "Stable"
PRERELEASE_WORKFLOW_NAME = "Prerelease"
STABLE_OPERATOR_WORKFLOW_PATH = ".github/workflows/briefcase.yml"
PRERELEASE_OPERATOR_WORKFLOW_PATH = ".github/workflows/prerelease.yml"


@dataclass(frozen=True)
class OperatorWorkflowPolicy:
    name: str
    path: str
    route: str


OPERATOR_WORKFLOWS = {
    STABLE_WORKFLOW_NAME: OperatorWorkflowPolicy(
        name=STABLE_WORKFLOW_NAME,
        path=STABLE_OPERATOR_WORKFLOW_PATH,
        route=STABLE_ROUTE,
    ),
    PRERELEASE_WORKFLOW_NAME: OperatorWorkflowPolicy(
        name=PRERELEASE_WORKFLOW_NAME,
        path=PRERELEASE_OPERATOR_WORKFLOW_PATH,
        route=PRERELEASE_ROUTE,
    ),
}
OPERATOR_ROUTES = {policy.route: policy for policy in OPERATOR_WORKFLOWS.values()}


class ReleaseWorkflowPolicyError(RuntimeError):
    pass


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "")
    if not value:
        raise ReleaseWorkflowPolicyError(f"Release workflow policy is missing {name}.")
    return value


def _positive_int(value: str, description: str) -> int:
    if not value.isdecimal() or int(value) <= 0:
        raise ReleaseWorkflowPolicyError(f"{description} must be a positive integer.")
    return int(value)


def _full_sha(value: str, description: str) -> str:
    if SHA_PATTERN.fullmatch(value) is None:
        raise ReleaseWorkflowPolicyError(f"{description} must be a full lowercase Git SHA.")
    return value


def _boolean(value: str, description: str) -> bool:
    if value == "true":
        return True
    if value == "false":
        return False
    raise ReleaseWorkflowPolicyError(f"{description} must be 'true' or 'false'.")


def _mapping(value: object, description: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ReleaseWorkflowPolicyError(f"{description} must be a JSON object.")
    return cast(Mapping[str, Any], value)


def _operator_policy_from_ref(workflow_ref: str) -> OperatorWorkflowPolicy:
    for policy in OPERATOR_WORKFLOWS.values():
        expected_ref = f"{REPOSITORY}/{policy.path}@{REQUIRED_REF}"
        if workflow_ref == expected_ref:
            return policy
    expected = tuple(f"{REPOSITORY}/{policy.path}@{REQUIRED_REF}" for policy in OPERATOR_WORKFLOWS.values())
    raise ReleaseWorkflowPolicyError(
        f"Release operator workflow ref {workflow_ref!r} does not match an approved operator workflow: {expected!r}."
    )


def _decode_oidc_claims(token: str) -> Mapping[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        raise ReleaseWorkflowPolicyError("GitHub OIDC token must contain three JWT segments.")
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        return _mapping(json.loads(decoded), "GitHub OIDC claims")
    except (binascii.Error, UnicodeError, ValueError, json.JSONDecodeError) as error:
        raise ReleaseWorkflowPolicyError("GitHub OIDC token payload is invalid.") from error


def _request_oidc_claims(environment: Mapping[str, str]) -> Mapping[str, Any]:
    request_url = _required(environment, "ACTIONS_ID_TOKEN_REQUEST_URL")
    request_token = _required(environment, "ACTIONS_ID_TOKEN_REQUEST_TOKEN")
    separator = "&" if "?" in request_url else "?"
    audience = quote(OIDC_AUDIENCE, safe="")
    request = Request(
        f"{request_url}{separator}audience={audience}",
        headers={"Authorization": f"Bearer {request_token}"},
    )
    try:
        with urlopen(request, timeout=30) as response:
            result = _mapping(json.load(response), "GitHub OIDC response")
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise ReleaseWorkflowPolicyError("Unable to obtain GitHub OIDC workflow identity.") from error
    token = result.get("value")
    if not isinstance(token, str) or not token:
        raise ReleaseWorkflowPolicyError("GitHub OIDC response is missing its token value.")
    return _decode_oidc_claims(token)


def _with_oidc_workflow_identity(environment: Mapping[str, str]) -> Mapping[str, str]:
    workflow_ref = environment.get("RELEASE_ENGINE_WORKFLOW_REF", "")
    workflow_sha = environment.get("RELEASE_ENGINE_WORKFLOW_SHA", "")
    workflow_repository = environment.get("RELEASE_ENGINE_WORKFLOW_REPOSITORY", "")
    if workflow_ref and workflow_sha and workflow_repository:
        return environment
    if workflow_ref or workflow_sha or workflow_repository:
        raise ReleaseWorkflowPolicyError("Reusable workflow identity must be provided completely or by GitHub OIDC.")

    claims = _request_oidc_claims(environment)
    expected_claims = {
        "aud": OIDC_AUDIENCE,
        "repository": _required(environment, "RELEASE_REPOSITORY"),
        "event_name": _required(environment, "RELEASE_EVENT_NAME"),
        "ref": _required(environment, "RELEASE_REF"),
        "sha": _required(environment, "RELEASE_SHA"),
        "workflow_ref": _required(environment, "RELEASE_OPERATOR_WORKFLOW_REF"),
        "workflow_sha": _required(environment, "RELEASE_OPERATOR_WORKFLOW_SHA"),
        "run_id": _required(environment, "RELEASE_RUN_ID"),
        "run_attempt": _required(environment, "RELEASE_RUN_ATTEMPT"),
        "actor": _required(environment, "RELEASE_ACTOR"),
    }
    for name, expected in expected_claims.items():
        actual = claims.get(name)
        if actual != expected:
            raise ReleaseWorkflowPolicyError(
                f"GitHub OIDC claim {name!r} {actual!r} does not match caller context {expected!r}."
            )

    job_workflow_ref = claims.get("job_workflow_ref")
    job_workflow_sha = claims.get("job_workflow_sha")
    if not isinstance(job_workflow_ref, str) or not job_workflow_ref:
        raise ReleaseWorkflowPolicyError("GitHub OIDC token is missing reusable job_workflow_ref evidence.")
    if not isinstance(job_workflow_sha, str) or not job_workflow_sha:
        raise ReleaseWorkflowPolicyError("GitHub OIDC token is missing reusable job_workflow_sha evidence.")
    workflow_marker = "/.github/workflows/"
    if workflow_marker not in job_workflow_ref:
        raise ReleaseWorkflowPolicyError("GitHub OIDC job_workflow_ref is not a repository workflow ref.")

    merged = dict(environment)
    merged["RELEASE_ENGINE_WORKFLOW_REF"] = job_workflow_ref
    merged["RELEASE_ENGINE_WORKFLOW_SHA"] = job_workflow_sha
    merged["RELEASE_ENGINE_WORKFLOW_REPOSITORY"] = job_workflow_ref.split(workflow_marker, 1)[0]
    return merged


@dataclass(frozen=True)
class ReleaseWorkflowEvidence:
    repository: str
    event_name: str
    ref: str
    release_sha: str
    operator_route: str
    operator_workflow_ref: str
    operator_workflow_sha: str
    engine_workflow_ref: str
    engine_workflow_sha: str
    engine_workflow_repository: str
    run_id: int
    run_attempt: int
    actor: str
    triggering_actor: str

    @property
    def operator_workflow_path(self) -> str:
        return OPERATOR_ROUTES[self.operator_route].path

    @property
    def expected_operator_workflow_ref(self) -> str:
        return f"{REPOSITORY}/{self.operator_workflow_path}@{REQUIRED_REF}"

    @property
    def expected_engine_workflow_ref(self) -> str:
        return f"{REPOSITORY}/{ENGINE_WORKFLOW_PATH}@{REQUIRED_REF}"

    def fingerprint(self) -> str:
        payload = json.dumps(
            {
                "actor": self.actor,
                "approval_environment": APPROVAL_ENVIRONMENT,
                "engine_workflow_ref": self.engine_workflow_ref,
                "engine_workflow_sha": self.engine_workflow_sha,
                "event": self.event_name,
                "head_sha": self.release_sha,
                "operator_route": self.operator_route,
                "operator_workflow_ref": self.operator_workflow_ref,
                "operator_workflow_path": self.operator_workflow_path,
                "operator_workflow_sha": self.operator_workflow_sha,
                "ref": self.ref,
                "repository": self.repository,
                "run_attempt": self.run_attempt,
                "run_id": self.run_id,
                "triggering_actor": self.triggering_actor,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ReleaseRouteMetadata:
    operator_name: str
    operator_route: str
    operator_workflow_path: str
    release_tag: str
    channel: str
    prerelease: bool
    make_latest: bool
    publish_pypi: bool

    def summary(self) -> str:
        yes_no = {True: "Yes", False: "No"}
        return "\n".join(
            (
                "### Validated release route",
                "",
                "| Field | Validated value |",
                "| --- | --- |",
                f"| Operator route | {self.operator_name} |",
                f"| Operator workflow | `{self.operator_workflow_path}` |",
                f"| Release tag | `{self.release_tag}` |",
                f"| Committed release stage | `{self.channel}` |",
                f"| GitHub prerelease | {yes_no[self.prerelease]} |",
                f"| GitHub Latest | {yes_no[self.make_latest]} |",
                f"| PyPI publication | {yes_no[self.publish_pypi]} |",
                "",
            )
        )


def validate_engine_environment(environment: Mapping[str, str]) -> ReleaseWorkflowEvidence:
    operator_workflow_ref = _required(environment, "RELEASE_OPERATOR_WORKFLOW_REF")
    operator_policy = _operator_policy_from_ref(operator_workflow_ref)
    evidence = ReleaseWorkflowEvidence(
        repository=_required(environment, "RELEASE_REPOSITORY"),
        event_name=_required(environment, "RELEASE_EVENT_NAME"),
        ref=_required(environment, "RELEASE_REF"),
        release_sha=_full_sha(_required(environment, "RELEASE_SHA"), "Release SHA"),
        operator_route=operator_policy.route,
        operator_workflow_ref=operator_workflow_ref,
        operator_workflow_sha=_full_sha(
            _required(environment, "RELEASE_OPERATOR_WORKFLOW_SHA"),
            "Operator workflow SHA",
        ),
        engine_workflow_ref=_required(environment, "RELEASE_ENGINE_WORKFLOW_REF"),
        engine_workflow_sha=_full_sha(
            _required(environment, "RELEASE_ENGINE_WORKFLOW_SHA"),
            "Engine workflow SHA",
        ),
        engine_workflow_repository=_required(environment, "RELEASE_ENGINE_WORKFLOW_REPOSITORY"),
        run_id=_positive_int(_required(environment, "RELEASE_RUN_ID"), "Release run ID"),
        run_attempt=_positive_int(
            _required(environment, "RELEASE_RUN_ATTEMPT"),
            "Release run attempt",
        ),
        actor=_required(environment, "RELEASE_ACTOR"),
        triggering_actor=_required(environment, "RELEASE_TRIGGERING_ACTOR"),
    )

    expected_values = {
        "repository": (evidence.repository, REPOSITORY),
        "event": (evidence.event_name, REQUIRED_EVENT),
        "ref": (evidence.ref, REQUIRED_REF),
        "operator workflow ref": (
            evidence.operator_workflow_ref,
            evidence.expected_operator_workflow_ref,
        ),
        "engine workflow ref": (
            evidence.engine_workflow_ref,
            evidence.expected_engine_workflow_ref,
        ),
        "engine workflow repository": (evidence.engine_workflow_repository, REPOSITORY),
        "actor": (evidence.actor, REQUIRED_ACTOR),
        "triggering actor": (evidence.triggering_actor, REQUIRED_ACTOR),
        "release SHA input": (
            _full_sha(_required(environment, "INPUT_RELEASE_SHA"), "Release SHA input"),
            evidence.release_sha,
        ),
        "operator workflow ref input": (
            _required(environment, "INPUT_OPERATOR_WORKFLOW_REF"),
            evidence.operator_workflow_ref,
        ),
        "operator workflow SHA input": (
            _full_sha(
                _required(environment, "INPUT_OPERATOR_WORKFLOW_SHA"),
                "Operator workflow SHA input",
            ),
            evidence.operator_workflow_sha,
        ),
        "operator run ID input": (
            _positive_int(_required(environment, "INPUT_OPERATOR_RUN_ID"), "Operator run ID input"),
            evidence.run_id,
        ),
        "operator run attempt input": (
            _positive_int(
                _required(environment, "INPUT_OPERATOR_RUN_ATTEMPT"),
                "Operator run attempt input",
            ),
            evidence.run_attempt,
        ),
        "operator actor input": (
            _required(environment, "INPUT_OPERATOR_ACTOR"),
            evidence.actor,
        ),
        "operator triggering actor input": (
            _required(environment, "INPUT_OPERATOR_TRIGGERING_ACTOR"),
            evidence.triggering_actor,
        ),
    }
    for description, (actual, expected) in expected_values.items():
        if actual != expected:
            raise ReleaseWorkflowPolicyError(
                f"Release {description} {actual!r} does not match required value {expected!r}."
            )

    for description, workflow_sha in (
        ("operator workflow SHA", evidence.operator_workflow_sha),
        ("engine workflow SHA", evidence.engine_workflow_sha),
    ):
        if workflow_sha != evidence.release_sha:
            raise ReleaseWorkflowPolicyError(
                f"Release {description} {workflow_sha!r} does not match release SHA {evidence.release_sha!r}."
            )
    return evidence


def _frozen_release_tags(path: Path = RELEASE_FREEZES_PATH) -> Mapping[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReleaseWorkflowPolicyError(f"Unable to load committed release freezes from {path}: {error}") from error
    freezes = _mapping(payload, "Release freeze policy")
    if freezes.get("schema") != "bd_to_avp.release_freezes" or freezes.get("schema_version") != 1:
        raise ReleaseWorkflowPolicyError("Release freeze policy has an unsupported schema.")
    return _mapping(freezes.get("frozen_release_tags"), "Frozen release tags")


def validate_release_metadata(
    environment: Mapping[str, str],
    *,
    freezes_path: Path = RELEASE_FREEZES_PATH,
) -> ReleaseRouteMetadata:
    operator_policy = _operator_policy_from_ref(_required(environment, "RELEASE_OPERATOR_WORKFLOW_REF"))
    metadata = ReleaseRouteMetadata(
        operator_name=operator_policy.name,
        operator_route=operator_policy.route,
        operator_workflow_path=operator_policy.path,
        release_tag=_required(environment, "RELEASE_TAG"),
        channel=_required(environment, "RELEASE_CHANNEL"),
        prerelease=_boolean(_required(environment, "RELEASE_PRERELEASE"), "GitHub prerelease policy"),
        make_latest=_boolean(_required(environment, "RELEASE_MAKE_LATEST"), "GitHub Latest policy"),
        publish_pypi=_boolean(_required(environment, "RELEASE_PUBLISH_PYPI"), "PyPI publication policy"),
    )
    if metadata.operator_route == STABLE_ROUTE:
        valid = (
            metadata.channel == "stable" and not metadata.prerelease and metadata.make_latest and metadata.publish_pypi
        )
        requirement = "stable, non-prerelease, Latest, and PyPI-enabled"
    else:
        valid = (
            metadata.channel in {"alpha", "beta", "rc"}
            and metadata.prerelease
            and not metadata.make_latest
            and not metadata.publish_pypi
        )
        requirement = "alpha, beta, or rc; prerelease; non-Latest; and PyPI-disabled"
    if not valid:
        raise ReleaseWorkflowPolicyError(
            f"{metadata.operator_name} operator requires committed metadata that is {requirement}; received "
            f"channel={metadata.channel!r}, prerelease={metadata.prerelease!r}, "
            f"make_latest={metadata.make_latest!r}, publish_pypi={metadata.publish_pypi!r}."
        )
    freeze = _frozen_release_tags(freezes_path).get(metadata.release_tag)
    if freeze is not None:
        freeze_record = _mapping(freeze, f"Release freeze for {metadata.release_tag}")
        issue = freeze_record.get("issue")
        reason = freeze_record.get("reason")
        invalid_issue = isinstance(issue, bool) or not isinstance(issue, int) or issue <= 0
        if invalid_issue or not isinstance(reason, str) or not reason:
            raise ReleaseWorkflowPolicyError(f"Release freeze for {metadata.release_tag} is invalid.")
        raise ReleaseWorkflowPolicyError(f"Release {metadata.release_tag} is frozen by issue #{issue}: {reason}")
    return metadata


def _write_github_output(path: Path, evidence: ReleaseWorkflowEvidence) -> None:
    outputs = {
        "policy_fingerprint": evidence.fingerprint(),
        "release_route": evidence.operator_route,
        "operator_workflow_path": evidence.operator_workflow_path,
        "operator_workflow_ref": evidence.operator_workflow_ref,
        "operator_workflow_sha": evidence.operator_workflow_sha,
        "engine_workflow_ref": evidence.engine_workflow_ref,
        "engine_workflow_sha": evidence.engine_workflow_sha,
        "release_sha": evidence.release_sha,
    }
    with path.open("a", encoding="utf-8") as handle:
        for name, value in outputs.items():
            handle.write(f"{name}={value}\n")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate the guarded release workflow call boundary.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    engine = subparsers.add_parser("engine", help="Validate an operator route and reusable engine identity.")
    engine.add_argument("--github-output", type=Path)
    engine.add_argument("--expected-fingerprint")
    metadata = subparsers.add_parser("metadata", help="Validate committed metadata for the trusted operator route.")
    metadata.add_argument("--github-step-summary", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, *, environment: Mapping[str, str] | None = None) -> int:
    args = parse_args(argv)
    values = os.environ if environment is None else environment
    if args.command == "engine":
        evidence = validate_engine_environment(_with_oidc_workflow_identity(values))
        fingerprint = evidence.fingerprint()
        if args.expected_fingerprint is not None and not hmac.compare_digest(args.expected_fingerprint, fingerprint):
            raise ReleaseWorkflowPolicyError("Release policy fingerprint changed across the approval boundary.")
        if args.github_output is not None:
            _write_github_output(args.github_output, evidence)
    else:
        metadata = validate_release_metadata(values)
        if args.github_step_summary is not None:
            with args.github_step_summary.open("a", encoding="utf-8") as handle:
                handle.write(metadata.summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
