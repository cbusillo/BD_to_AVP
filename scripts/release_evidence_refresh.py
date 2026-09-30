"""Refresh one release's evidence branch onto protected main without a textual merge.

A successor release may be prepared, and may carry this release's checked receipt, before this
release's own evidence lands. Main then appends to the same shared indexes the evidence branch
appends to, so `git merge` conflicts on work that has one correct answer. This command rebuilds
the branch from main's tree instead: it copies only files this release owns, re-appends this
release's evidence-index receipts and ledger record after main's, regenerates index-v2 with the
maintained generator, and records a commit whose parents are the branch head and main, so the
branch keeps its history and contains main.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.release_evidence import (
    EVIDENCE_INDEX_PATH,
    RELEASE_LEDGER_PATH,
    ReleaseEvidenceError,
    _update_cut_packet,
    merge_evidence_receipts,
    merge_release_ledger_record,
    render_published_cut_packet,
)
from scripts.release_evidence_v2 import (
    ReleaseEvidenceV2Error,
    build_index_v2,
    sanitize_release_tag,
    write_index_v2,
)
from scripts.release_milestone_context import (
    GITHUB_CONFIG_PATH,
    RELEASE_V2_INDEX_PATH,
    ReleaseMilestoneContextError,
    _validate_append_only_evidence_index,
    _validate_append_only_release_ledger,
    release_compatibility_paths,
)
from scripts.release_qualification_manifest import TERMINAL_RECORD_NAMES


EVIDENCE_INDEX = EVIDENCE_INDEX_PATH.as_posix()
RELEASE_LEDGER = RELEASE_LEDGER_PATH.as_posix()
SHARED_INDEX_PATHS = frozenset({EVIDENCE_INDEX, RELEASE_LEDGER, RELEASE_V2_INDEX_PATH})
REGULAR_FILE_MODES = frozenset({"100644", "100755"})


class ReleaseEvidenceRefreshError(RuntimeError):
    """Raised when an evidence branch cannot be refreshed onto main without a judgment call."""


@dataclass(frozen=True)
class RefreshResult:
    head_sha: str
    refreshed: bool
    terminal: bool

    def github_outputs(self) -> dict[str, str]:
        return {
            "refresh_head_sha": self.head_sha,
            "refreshed": "true" if self.refreshed else "false",
            "terminal": "true" if self.terminal else "false",
        }


@dataclass(frozen=True)
class _Blob:
    mode: str
    data: bytes


def _git(repo_root: Path, *arguments: str, input_bytes: bytes | None = None) -> bytes:
    result = subprocess.run(["git", *arguments], cwd=repo_root, input=input_bytes, capture_output=True, check=False)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ReleaseEvidenceRefreshError(f"git {arguments[0]} failed: {detail or 'no detail'}.")
    return result.stdout


def _commit(repo_root: Path, reference: str) -> str:
    return _git(repo_root, "rev-parse", "--verify", "--end-of-options", f"{reference}^{{commit}}").decode().strip()


def _blob(repo_root: Path, revision: str, path: str) -> _Blob | None:
    listing = _git(repo_root, "ls-tree", "-z", revision, "--", path).decode()
    entries = [entry for entry in listing.split("\0") if entry]
    if not entries:
        return None
    metadata, _, listed_path = entries[0].partition("\t")
    mode, object_type, object_id = metadata.split()
    if listed_path != path or object_type != "blob" or mode not in REGULAR_FILE_MODES:
        raise ReleaseEvidenceRefreshError(f"{path} at {revision[:12]} is not a regular file.")
    return _Blob(mode=mode, data=_git(repo_root, "cat-file", "blob", object_id))


def _json(blob: _Blob | None, description: str) -> Mapping[str, Any]:
    if blob is None:
        raise ReleaseEvidenceRefreshError(f"{description} is missing.")
    try:
        value = json.loads(blob.data)
    except json.JSONDecodeError as error:
        raise ReleaseEvidenceRefreshError(f"{description} is not valid JSON: {error}") from error
    if not isinstance(value, Mapping):
        raise ReleaseEvidenceRefreshError(f"{description} must be a JSON object.")
    return value


def _records(document: Mapping[str, Any], field: str, key: str, description: str) -> list[Mapping[str, Any]]:
    raw = document.get(field)
    if not isinstance(raw, list) or not all(isinstance(item, Mapping) for item in raw):
        raise ReleaseEvidenceRefreshError(f"{description} {field} must be a list of objects.")
    identifiers = [item.get(key) for item in raw]
    if not all(isinstance(identifier, str) and identifier for identifier in identifiers):
        raise ReleaseEvidenceRefreshError(f"Every {description} record needs a {key}.")
    if len(set(identifiers)) != len(identifiers):
        raise ReleaseEvidenceRefreshError(f"{description} has duplicate {key} values.")
    return list(raw)


def _metadata(document: Mapping[str, Any], field: str) -> dict[str, Any]:
    return {key: value for key, value in document.items() if key != field}


def _branch_additions(
    *,
    base: Mapping[str, Any],
    branch: Mapping[str, Any],
    main: Mapping[str, Any],
    field: str,
    key: str,
    description: str,
    owned: Any,
) -> list[Mapping[str, Any]]:
    """Return the branch's own records in branch order after checking that it rewrote nothing shared."""
    if _metadata(branch, field) != _metadata(base, field):
        raise ReleaseEvidenceRefreshError(f"The evidence branch changed {description} metadata.")
    base_records = {item[key]: item for item in _records(base, field, key, f"merge-base {description}")}
    main_records = {item[key]: item for item in _records(main, field, key, f"main {description}")}
    branch_records = _records(branch, field, key, f"branch {description}")
    removed = sorted(set(base_records) - {item[key] for item in branch_records})
    if removed:
        raise ReleaseEvidenceRefreshError(f"The evidence branch removed accepted {description} records: {removed!r}.")
    additions: list[Mapping[str, Any]] = []
    for record in branch_records:
        identifier = record[key]
        if identifier in base_records:
            if record != base_records[identifier]:
                raise ReleaseEvidenceRefreshError(
                    f"The evidence branch rewrote accepted {description} record {identifier!r}."
                )
            continue
        if identifier in main_records:
            if record != main_records[identifier]:
                raise ReleaseEvidenceRefreshError(
                    f"The {description} record {identifier!r} differs between main and the evidence branch."
                )
            continue
        if not owned(identifier):
            raise ReleaseEvidenceRefreshError(
                f"The evidence branch adds {description} record {identifier!r}, which belongs to another release."
            )
        additions.append(record)
    return additions


def _branch_changes(repo_root: Path, base_sha: str, branch_sha: str) -> dict[str, str]:
    fields = _git(repo_root, "diff", "--name-status", "--no-renames", "-z", base_sha, branch_sha).decode().split("\0")
    fields = [field for field in fields if field]
    return {fields[index + 1]: fields[index] for index in range(0, len(fields), 2)}


def _rolling_qualification_path(repo_root: Path, main_sha: str) -> str:
    config = _json(_blob(repo_root, main_sha, GITHUB_CONFIG_PATH.as_posix()), "main GitHub config")
    operations = config.get("releaseOperations")
    path = operations.get("qualificationRecordPath") if isinstance(operations, Mapping) else None
    if not isinstance(path, str) or not path:
        raise ReleaseEvidenceRefreshError("Main GitHub config has no releaseOperations.qualificationRecordPath.")
    return path


def _require_clean_checkout(repo_root: Path) -> None:
    if _git(repo_root, "status", "--porcelain", "--untracked-files=no").strip():
        raise ReleaseEvidenceRefreshError("The checkout has tracked changes; refresh needs a clean checkout.")
    if _git(repo_root, "ls-files", "--others", "--exclude-standard", "--", "docs").strip():
        raise ReleaseEvidenceRefreshError("The checkout has untracked files under docs/; refresh needs a clean tree.")


def _write(repo_root: Path, path: str, blob: _Blob) -> None:
    target = repo_root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(blob.data)
    target.chmod(0o755 if blob.mode == "100755" else 0o644)


def _plan_owned_file(
    repo_root: Path,
    *,
    path: str,
    rolling_path: str,
    base_sha: str,
    branch_sha: str,
    main_sha: str,
) -> _Blob | None:
    """Return the branch bytes to write, or None when main's bytes stand."""
    branch = _blob(repo_root, branch_sha, path)
    base = _blob(repo_root, base_sha, path)
    main = _blob(repo_root, main_sha, path)
    if branch is None:
        raise ReleaseEvidenceRefreshError(f"The evidence branch deletes {path}; evidence is add-only.")
    if main == branch:
        return None
    if main == base:
        return branch
    if path == rolling_path:
        # The rolling record belongs to whatever release main prepares; protected main wins.
        return None
    raise ReleaseEvidenceRefreshError(
        f"{path} changed on both main and the evidence branch with different bytes; refusing to choose."
    )


def refresh_evidence_branch(
    repo_root: Path,
    *,
    release_tag: str,
    branch_ref: str,
    main_ref: str,
    message: str | None = None,
) -> RefreshResult:
    repo_root = repo_root.resolve()
    try:
        tag = sanitize_release_tag(release_tag)
        compatibility = release_compatibility_paths(tag)
    except (ReleaseEvidenceV2Error, ReleaseMilestoneContextError) as error:
        raise ReleaseEvidenceRefreshError(str(error)) from error
    branch_sha = _commit(repo_root, branch_ref)
    main_sha = _commit(repo_root, main_ref)
    _require_clean_checkout(repo_root)
    merge_bases = _git(repo_root, "merge-base", "--all", branch_sha, main_sha).decode().split()
    if len(merge_bases) != 1:
        raise ReleaseEvidenceRefreshError(
            f"Expected one merge base for the evidence branch and main; found {merge_bases}."
        )
    base_sha = merge_bases[0]
    bundle_prefix = compatibility.bundle_prefix
    terminal = any(_blob(repo_root, branch_sha, f"{bundle_prefix}{name}") is not None for name in TERMINAL_RECORD_NAMES)

    changes = _branch_changes(repo_root, base_sha, branch_sha)
    owned_exact = compatibility.release_owned_paths
    unexpected = sorted(
        path
        for path in changes
        if path not in SHARED_INDEX_PATHS and path not in owned_exact and not path.startswith(bundle_prefix)
    )
    if unexpected:
        raise ReleaseEvidenceRefreshError(
            f"The evidence branch changes files that {tag} does not own: {unexpected!r}. "
            f"Only {bundle_prefix}, this release's compatibility records, and the shared evidence indexes may change."
        )
    deleted = sorted(path for path, status in changes.items() if status.startswith("D"))
    if deleted:
        raise ReleaseEvidenceRefreshError(f"The evidence branch deletes files; evidence is add-only: {deleted!r}.")

    if base_sha == main_sha:
        _git(repo_root, "checkout", "--quiet", "--detach", branch_sha)
        return RefreshResult(head_sha=branch_sha, refreshed=False, terminal=terminal)

    rolling_path = _rolling_qualification_path(repo_root, main_sha)
    cut_packet = compatibility.cut_packet
    owned_writes: dict[str, _Blob] = {}
    for path in sorted(changes):
        if path in SHARED_INDEX_PATHS or path == cut_packet:
            continue
        planned = _plan_owned_file(
            repo_root,
            path=path,
            rolling_path=rolling_path,
            base_sha=base_sha,
            branch_sha=branch_sha,
            main_sha=main_sha,
        )
        if planned is not None:
            owned_writes[path] = planned

    receipt_path = f"{bundle_prefix}release-receipt.json"
    publication_path = f"{bundle_prefix}publication-record.json"
    render_cut_packet = False
    if cut_packet in changes:
        branch_packet = _blob(repo_root, branch_sha, cut_packet)
        base_packet = _blob(repo_root, base_sha, cut_packet)
        main_packet = _blob(repo_root, main_sha, cut_packet)
        if branch_packet is None:
            raise ReleaseEvidenceRefreshError(f"The evidence branch deletes {cut_packet}.")
        if main_packet is None:
            if base_packet is not None:
                raise ReleaseEvidenceRefreshError(f"Main removed {cut_packet}; the published packet has no home.")
            owned_writes[cut_packet] = branch_packet
        elif base_packet is None:
            if main_packet != branch_packet:
                raise ReleaseEvidenceRefreshError(f"{cut_packet} was added on both main and the evidence branch.")
        else:
            receipt = _json(_blob(repo_root, branch_sha, receipt_path), f"{tag} release receipt")
            publication = _json(_blob(repo_root, branch_sha, publication_path), f"{tag} publication record")
            try:
                expected = render_published_cut_packet(base_packet.data.decode("utf-8"), receipt, publication)
            except ReleaseEvidenceError as error:
                raise ReleaseEvidenceRefreshError(f"Unable to check {cut_packet}: {error}") from error
            if branch_packet.data != expected.encode("utf-8"):
                raise ReleaseEvidenceRefreshError(
                    f"{cut_packet} on the evidence branch carries edits beyond the maintained publication renderer."
                )
            render_cut_packet = True

    evidence_additions: list[Mapping[str, Any]] = []
    if EVIDENCE_INDEX in changes:
        evidence_additions = _branch_additions(
            base=_json(_blob(repo_root, base_sha, EVIDENCE_INDEX), "merge-base evidence index"),
            branch=_json(_blob(repo_root, branch_sha, EVIDENCE_INDEX), "branch evidence index"),
            main=_json(_blob(repo_root, main_sha, EVIDENCE_INDEX), "main evidence index"),
            field="receipts",
            key="receipt_id",
            description="evidence index",
            owned=lambda identifier: identifier.startswith(f"{tag}:"),
        )
    ledger_additions: list[Mapping[str, Any]] = []
    if RELEASE_LEDGER in changes:
        ledger_additions = _branch_additions(
            base=_json(_blob(repo_root, base_sha, RELEASE_LEDGER), "merge-base release ledger"),
            branch=_json(_blob(repo_root, branch_sha, RELEASE_LEDGER), "branch release ledger"),
            main=_json(_blob(repo_root, main_sha, RELEASE_LEDGER), "main release ledger"),
            field="releases",
            key="tag",
            description="release ledger",
            owned=lambda identifier: identifier == tag,
        )

    original_head = _commit(repo_root, "HEAD")
    _git(repo_root, "checkout", "--quiet", "--detach", main_sha)
    try:
        for path, blob in sorted(owned_writes.items()):
            _write(repo_root, path, blob)
        if render_cut_packet:
            receipt = _json(_blob(repo_root, branch_sha, receipt_path), f"{tag} release receipt")
            publication = _json(_blob(repo_root, branch_sha, publication_path), f"{tag} publication record")
            written = _update_cut_packet(repo_root, receipt, publication)
            if written.relative_to(repo_root).as_posix() != cut_packet:
                raise ReleaseEvidenceRefreshError(f"The {tag} receipt names a different cut packet than {cut_packet}.")
        if evidence_additions:
            merge_evidence_receipts(repo_root, evidence_additions)
            _validate_append_only_evidence_index(repo_root, base_sha=main_sha)
        if ledger_additions:
            for record in ledger_additions:
                merge_release_ledger_record(repo_root, record)
            _validate_append_only_release_ledger(repo_root, base_sha=main_sha, release_tag=tag)
        write_index_v2(repo_root, build_index_v2(repo_root, worktree=True))
        _git(repo_root, "add", "--all", "--", "docs")
        staged = set(_git(repo_root, "diff", "--cached", "--name-only", "-z").decode().split("\0")) - {""}
        stray = sorted(
            path
            for path in staged
            if path not in SHARED_INDEX_PATHS and path not in owned_exact and not path.startswith(bundle_prefix)
        )
        if stray:
            raise ReleaseEvidenceRefreshError(f"Refresh staged files {tag} does not own: {stray!r}.")
        if rolling_path in staged and rolling_path not in owned_writes:
            raise ReleaseEvidenceRefreshError(f"Refresh must not rewrite the rolling qualification {rolling_path}.")
        tree = _git(repo_root, "write-tree").decode().strip()
        commit_message = message or (
            f"Refresh {tag} release evidence onto main\n\n"
            f"Rebuilt from main {main_sha} with the {tag} evidence of {branch_sha}.\n"
        )
        commit = (
            _git(
                repo_root,
                "commit-tree",
                tree,
                "-p",
                branch_sha,
                "-p",
                main_sha,
                "-F",
                "-",
                input_bytes=commit_message.encode("utf-8"),
            )
            .decode()
            .strip()
        )
        _git(repo_root, "reset", "--quiet", "--soft", commit)
    except (ReleaseEvidenceError, ReleaseEvidenceV2Error, ReleaseMilestoneContextError, OSError) as error:
        _restore(repo_root, original_head)
        raise ReleaseEvidenceRefreshError(str(error)) from error
    except ReleaseEvidenceRefreshError:
        _restore(repo_root, original_head)
        raise
    return RefreshResult(head_sha=commit, refreshed=True, terminal=terminal)


def _restore(repo_root: Path, original_head: str) -> None:
    subprocess.run(["git", "reset", "--quiet", "--hard"], cwd=repo_root, capture_output=True, check=False)
    subprocess.run(["git", "clean", "-fdq", "--", "docs"], cwd=repo_root, capture_output=True, check=False)
    subprocess.run(
        ["git", "checkout", "--quiet", "--detach", original_head], cwd=repo_root, capture_output=True, check=False
    )


def _write_github_output(path: Path, outputs: Mapping[str, str]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for key, value in outputs.items():
            handle.write(f"{key}={value}\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--branch-ref", required=True, help="Current head of the release's evidence branch.")
    parser.add_argument("--main-ref", required=True, help="Exact protected-main commit to refresh onto.")
    parser.add_argument("--message")
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args(argv)
    try:
        result = refresh_evidence_branch(
            args.repo_root,
            release_tag=args.release_tag,
            branch_ref=args.branch_ref,
            main_ref=args.main_ref,
            message=args.message,
        )
    except ReleaseEvidenceRefreshError as error:
        print(f"release-evidence-refresh: {error}", file=sys.stderr)
        return 1
    outputs = result.github_outputs()
    if args.github_output is not None:
        _write_github_output(args.github_output, outputs)
    print(json.dumps(outputs, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
