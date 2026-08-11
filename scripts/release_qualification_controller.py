from __future__ import annotations

import argparse
import json
import sys

from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import cast

from scripts.qualify_release_scope import QualificationScopeError
from scripts.release_milestone_context import ReleaseMilestoneContextError
from scripts.release_qualification_manifest import ReleaseQualificationManifestError
from scripts.release_qualification_status import (
    EvidenceBinding,
    ReleaseQualificationControllerError,
    _case_categories,
    _load_bound_policy,
    _require_checked_file,
    build_status,
    resolve_evidence_binding,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
__all__ = [
    "EvidenceBinding",
    "ReleaseQualificationControllerError",
    "_case_categories",
    "_load_bound_policy",
    "_require_checked_file",
    "build_parser",
    "build_status",
    "main",
    "resolve_evidence_binding",
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspect and resume release qualification safely.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    status_parser = subparsers.add_parser("status", help="Inspect checked qualification state without mutation.")
    status_parser.add_argument("--release-tag", required=True)
    status_parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    status_parser.add_argument("--as-of", type=date.fromisoformat)
    resume_parser = subparsers.add_parser(
        "resume",
        help="Observe qualification recovery and dispatch one exact milestone run when explicitly authorized.",
    )
    resume_parser.add_argument("--release-tag", required=True)
    resume_parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    resume_parser.add_argument("--expected-main-sha")
    resume_parser.add_argument("--expected-manifest-sha256")
    resume_parser.add_argument("--retry-run-id", type=int)
    resume_parser.add_argument("--retry-checkpoint-sha256")
    resume_parser.add_argument("--apply-plan-sha256")
    resume_parser.add_argument("--observe-only", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "resume":
        from scripts.github_release_run import ReleaseRunError
        from scripts.release_qualification_resume import (
            EXIT_FAILED,
            EXIT_SAFETY_ERROR,
            QualificationResumeError,
            QualificationResumeSafetyError,
            resume_qualification,
            safety_error_payload,
        )

        try:
            result = resume_qualification(
                args.repo_root,
                args.release_tag,
                expected_main_sha=args.expected_main_sha,
                expected_manifest_sha256=args.expected_manifest_sha256,
                retry_run_id=args.retry_run_id,
                retry_checkpoint_sha256=args.retry_checkpoint_sha256,
                apply_plan_sha256=args.apply_plan_sha256,
                observe_only=args.observe_only,
            )
        except QualificationResumeSafetyError as error:
            print(json.dumps(safety_error_payload(error), indent=2, sort_keys=True))
            return EXIT_SAFETY_ERROR
        except (json.JSONDecodeError, OSError, QualificationResumeError, ReleaseRunError) as error:
            print(json.dumps(safety_error_payload(error, state="error"), indent=2, sort_keys=True))
            return EXIT_FAILED
        print(json.dumps(result.payload, indent=2, sort_keys=True))
        return result.exit_code
    try:
        payload = build_status(
            args.repo_root,
            args.release_tag,
            as_of=args.as_of,
        )
    except (
        json.JSONDecodeError,
        OSError,
        QualificationScopeError,
        ReleaseMilestoneContextError,
        ReleaseQualificationControllerError,
        ReleaseQualificationManifestError,
    ) as error:
        print(f"Release qualification status failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 2 if cast(bool, payload["groups"]["blocking"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
