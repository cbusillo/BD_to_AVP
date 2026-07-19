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
OPERATOR_WORKFLOW_PATH = ".github/workflows/briefcase.yml"
ENGINE_WORKFLOW_PATH = ".github/workflows/release-engine.yml"
REQUIRED_REF = "refs/heads/main"
REQUIRED_EVENT = "workflow_dispatch"
REQUIRED_ACTOR = "shiny-code-bot"
APPROVAL_ENVIRONMENT = "macos-signing"
OIDC_AUDIENCE = "bd-to-avp-release-engine"
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


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


def _mapping(value: object, description: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ReleaseWorkflowPolicyError(f"{description} must be a JSON object.")
    return cast(Mapping[str, Any], value)


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
    def expected_operator_workflow_ref(self) -> str:
        return f"{REPOSITORY}/{OPERATOR_WORKFLOW_PATH}@{REQUIRED_REF}"

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
                "operator_workflow_ref": self.operator_workflow_ref,
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


def validate_engine_environment(environment: Mapping[str, str]) -> ReleaseWorkflowEvidence:
    evidence = ReleaseWorkflowEvidence(
        repository=_required(environment, "RELEASE_REPOSITORY"),
        event_name=_required(environment, "RELEASE_EVENT_NAME"),
        ref=_required(environment, "RELEASE_REF"),
        release_sha=_full_sha(_required(environment, "RELEASE_SHA"), "Release SHA"),
        operator_workflow_ref=_required(environment, "RELEASE_OPERATOR_WORKFLOW_REF"),
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


def _write_github_output(path: Path, evidence: ReleaseWorkflowEvidence) -> None:
    outputs = {
        "policy_fingerprint": evidence.fingerprint(),
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
    engine = subparsers.add_parser("engine", help="Validate the Stable operator and reusable engine identities.")
    engine.add_argument("--github-output", type=Path)
    engine.add_argument("--expected-fingerprint")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, *, environment: Mapping[str, str] | None = None) -> int:
    args = parse_args(argv)
    evidence = validate_engine_environment(_with_oidc_workflow_identity(environment or os.environ))
    fingerprint = evidence.fingerprint()
    if args.expected_fingerprint is not None and not hmac.compare_digest(args.expected_fingerprint, fingerprint):
        raise ReleaseWorkflowPolicyError("Release policy fingerprint changed across the approval boundary.")
    if args.github_output is not None:
        _write_github_output(args.github_output, evidence)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
