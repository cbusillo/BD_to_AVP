# Release Evidence v2

Release Evidence v2 is an additive, offline-verifiable, write-once format for
release evidence under `docs/release-evidence/<tag>/`. For new or in-progress
evidence, the commit at `automation/release-evidence-<tag>` plus its validated
`capture-v2.json` in state `CAPTURED` is the durable capture checkpoint. The
secret-free Release Evidence workflow creates or advances that branch directly
with a non-force push; it does not create, update, comment on, or delete a pull
request. Reruns whose complete checkpoint is already on protected `main` remain
a no-op. V1 evidence remains available for compatibility and reconciliation.
Evidence files are passive data: the verifier uses only local files and local
Git operations. For legacy revision replay it creates and removes a temporary
detached worktree. It does not invoke `gh`, `curl`, a network client, or
credentials.

## Orphan Audit

`Release Evidence Orphan Audit` runs daily at 14:17 UTC and can also be run
manually from protected `main`. It has only `contents: read` and `issues: write`
permissions. The workflow checks out the trusted protected `main` helper only;
it never checks out, fetches, or executes an `automation/release-evidence-*`
ref. Instead, it reads matching refs, commits, recursive trees, and the v2
record blobs through the GitHub REST API.

The audit recognizes a valid `CAPTURED` record and either terminal v2 state
(`QUALIFIED` or `FAILED`). It compares the complete
`docs/release-evidence/<tag>/` blob path/SHA map from each canonical
`automation/release-evidence-<tag>` ref to protected `main`. Exact bundle
identity is reconciled even if `docs/release-evidence/index-v2.json` has since
evolved. A valid non-reconciled bundle is **recent** for less than 72 hours and
a **stale orphan** at or after 72 hours, measured from `capture-v2.json`'s
`captured_at`. Invalid canonical refs, missing or contradictory v2 records,
and unreadable REST blobs are **malformed**.

Stale or malformed findings are grouped into one marker-owned alert issue,
assigned to `cbusillo`. The audit updates or reopens that same issue, can adopt
one pre-existing matching alert, and closes it when the findings clear. It
refuses to act if more than one matching alert issue exists, avoiding ambiguous
ownership and issue spam. Each alert lists the ref, commit SHA, age,
classification, v2 state, and remediation.

For a stale valid terminal bundle, inspect the REST evidence and use the
actor-aware reconciliation helper to open or adopt the protected-main PR; do
not force-push or rewrite the evidence. For malformed evidence, do not
reconcile it: preserve the ref for incident review and recreate valid canonical
evidence only through the trusted producer. The helper may be run locally with
an authenticated `gh` client, but it remains REST-only for automation refs:

```sh
uv run python -m scripts.release_evidence_orphan_audit \
  --repository cbusillo/BD_to_AVP --threshold-hours 72 --owner cbusillo
```

## Verification

By default the verifier reads the committed `HEAD` tree. Pass a full immutable
commit SHA to replay a historical v2 tree; bundle files and `source_sha`
ancestry are then evaluated against that revision, not against a later
checkout. Legacy v1 evidence at an immutable revision is validated in a
temporary detached worktree so the maintained validators retain their full
historical Git context. Pass `--worktree` to validate intentional uncommitted
evidence during local development.

```sh
uv run python -m scripts.release_evidence_v2 --tag v0.3.2 --revision <full-git-sha>
uv run python -m scripts.release_evidence_v2 --all-tags \
  --revision <full-git-sha> \
  --base-revision <full-git-sha>
uv run python -m scripts.release_evidence_v2 --tag v0.3.2 --worktree
uv run python -m scripts.release_evidence_v2 index --write --repo-root .
uv run python -m scripts.release_evidence_v2 index --check --worktree \
  --repo-root .
```

`--base-revision` enforces write-once history: every v2 record in the base tree
must exist byte-identically in the verified tree. Changed or deleted records are
refused.

The capture producer consumes only already-validated v1 files plus explicit
workflow provenance and timestamps. Its inputs include `--captured-at`,
`--live-appcast-verified-at`, `--capture-workflow-run-id`, and
`--capture-workflow-run-attempt`; supplying the same inputs produces identical
bytes. A capture already present in the bundle is validated and reused, never
regenerated or overwritten. The `sanitize` command emits the exact canonical
release tag and `automation/release-evidence-<tag>` ref before workflow shell
or Git ref use.

## Terminal Production

`qualify` and `dispose` are offline, deterministic, write-once producers for
the two mutually exclusive terminal records. They read only explicit local
files, local workflow provenance, and the already-captured bundle; neither
command invokes `gh`, makes a network request, or reads credentials.

Before either command writes, it validates `capture-v2.json` in the exact
worktree bundle. A valid terminal record with the same immutable identity is
reused byte-for-byte. A different terminal timestamp may therefore be supplied
on a rerun without changing the durable record. Any other identity conflict,
an invalid or missing capture, or the opposite terminal record fails closed and
does not overwrite evidence.

For `qualify`, provide the downloaded qualification artifact as a local file;
the producer binds its basename, ID, SHA-256, and exact milestone actor/path/run
ID/run attempt. A rerun must materialize the artifact under the same basename;
renaming identical bytes is treated as an immutable identity conflict.
`--accepted-case-receipts` is a local JSON object with exactly
the four required case IDs. Each entry has these explicit fields:

```json
{
  "clean-machine-signed-update": {
    "accepted_at": "2026-08-05T12:04:00Z",
    "path": "docs/release-evidence/v0.3.0-rc.3/clean-machine-signed-update-receipt.json",
    "result": "passed",
    "source": "tier3_automation_receipt"
  }
}
```

The producer computes each receipt file digest itself and then reuses the
existing deep receipt validators. It rejects a non-passed input, malformed
receipt, unexpected path/source, or chronology conflict. A successful command
prints only a canonical compact JSON object with the sanitized release tag,
record path, and self-digest.

```sh
uv run python -m scripts.release_evidence_v2 qualify \
  --release-tag v0.3.2 --qualified-at <utc-timestamp> \
  --milestone-actor cbusillo --milestone-run-id <id> \
  --milestone-run-attempt <attempt> \
  --qualification-artifact /local/path/qualification-artifact.zip \
  --qualification-artifact-id <id> \
  --qualification-manifest \
  docs/release-evidence/v0.3.2/qualification-manifest.json \
  --accepted-case-receipts /local/path/accepted-case-receipts.json

uv run python -m scripts.release_evidence_v2 dispose \
  --release-tag v0.3.2 --failed-at <utc-timestamp> \
  --failure-workflow-actor cbusillo --failure-workflow-run-id <id> \
  --failure-workflow-run-attempt <attempt> \
  --failure-code <lowercase_identifier> --failure-subject <subject> \
  --failure-expected <expected> --failure-observed <observed> \
  --release-identity-preserved --signed-artifact-preserved --source-identity-preserved
```

`dispose` requires all structured failure fields and all three terminal
identity-preservation remediation flags. After either terminal command changes
a bundle, intentionally regenerate and check the index; the producers do not
silently alter unrelated evidence files:

```sh
uv run python -m scripts.release_evidence_v2 index --write --repo-root .
uv run python -m scripts.release_evidence_v2 index --check --worktree \
  --repo-root .
```

`index-v2.json` contains only `schema_version`, a path/digest binding for
`index-v1.json`, and sorted release entries. Each release entry records the
existing verifier class and every sorted repo-relative file/digest in that tag
directory. The index contains no timestamps or current-time fields and excludes
itself from release inventories.
Protected-main CI regenerates this view in memory and rejects committed index
drift; the release-evidence workflow writes and immediately rechecks the same
bytes before staging its durable branch output.
Any later commit that adds terminal receipts or other checked files beneath a
release bundle must regenerate `index-v2.json` with the documented `index
--write` command before CI will accept the branch.

The automation branch is evidence state, not merge authorization. After a
qualification is complete, an authenticated operator uses the local
reconciliation helper to open or adopt the normal protected pull request to
merge the docs-only branch into `main`; branch protection, required CI,
review, and conversation-resolution rules remain unchanged. The secret-free
workflow never receives pull-request write permission.

## Operator Reconciliation

Run the observational preflight first. It reads the active local `gh` operator,
canonical remote evidence ref, current protected `main`, branch protection,
open pull requests, and immutable evidence files. It fetches only the two
remote commits locally; it never creates a pull request or changes a remote
branch. The helper ignores token environment variables such as `GH_TOKEN` and
uses the authenticated local `gh` credential store for every GitHub read and
the eventual PR creation.

```sh
uv run python -m scripts.release_evidence_reconcile preflight \
  --release-tag v0.3.2-beta.8
```

The compact JSON output contains the exact `evidence_sha`, `main_sha`, and
`plan_digest`. The plan binds the exact tag/ref, release/source identity,
release and capture actors/runs, successful qualification actor/run, artifact
identity, active operator, and required protected checks. To permit the only
mutation, echo all three values exactly from a fresh preflight:

```sh
uv run python -m scripts.release_evidence_reconcile reconcile \
  --release-tag v0.3.2-beta.8 \
  --evidence-sha <preflight-evidence-sha> \
  --main-sha <preflight-main-sha> \
  --plan-digest <preflight-plan-digest>
```

The helper verifies that the checkout's exact `origin` resolves to the canonical
repository, that `automation/release-evidence-<tag>` points at the echoed remote
commit, and that the commit descends from the current protected-main base. It
refuses stale branches, a moved `main`, non-evidence diffs, stale
`index-v2.json`, incomplete bundles, any terminal class other than
`v2-qualified`, and a durable `v2-failed` disposition. The standard offline v2
verifier and write-once history check run against the immutable evidence SHA.

It also refuses another open PR to `main`, a canonical evidence PR from a fork
or with a different head SHA, or an author other than the active local `gh`
operator. An existing exact PR is adopted without another write, making retry
idempotent. When no PR exists, the helper invokes only `gh pr create` for the
canonical branch and `main`; it never merges, force-pushes, deletes a branch,
edits protection, or uses a workflow token. Required checks continue to run on
that final protected PR and must complete under the repository's normal merge
rules.

## Capture Record

`capture-v2.json` has exact keys, canonical two-space sorted JSON bytes, and a
`capture_sha256` self-digest over its compact sorted payload. It binds:

- The archived, byte-exact `release-receipt.json`, including the published
  receipt asset ID, file digest, and receipt self-digest. The native release
  receipt validator confirms its exact release, asset, appcast, and workflow
  identity.
- The signed UI artifact ID plus exact `signed-artifact-ui.zip` and
  `signed-artifact-ui-receipt.json` bytes/digests. The native signed-artifact
  receipt validator is reused, and the ZIP must contain exactly one
  `signed-artifact-ui-receipt.json` entry whose bytes exactly match the bounded
  archived receipt. The signed-UI receipt binding is a file/self-digest binding
  and has no GitHub Release `asset_id`; only the release receipt binding carries
  that field.
- The archived, canonical `qualification-record.json` snapshot is directly
  bound by the capture record and validated with the maintained v1 qualification
  validator.
- Release workflow actor/path/run ID/attempt, evidence-capture workflow
  actor/path/run ID/attempt, and the live appcast digest with its verification
  timestamp. The appcast digest must match the release receipt.
- Historical policy, route table, tag-specific signed qualification template,
  and milestone runner digests read at immutable `source_sha`, after proving
  `source_sha` is an ancestor of the requested verification revision. The
  template path is `docs/qualification/<release-tag>-signed-qualification-v1.json`
  for prereleases and
  `docs/qualification/<release-tag>-stable-signed-qualification-v1.json` for a
  stable release.

## Terminal Records

A bundle may contain capture only (`v2-captured`) or exactly one terminal
record. A qualification and disposition together are split-brain and are
rejected; either terminal record without capture is rejected.

`qualification-v2.json` binds the capture digest, the same archived
`qualification-record.json` snapshot already bound by capture, successful
milestone path/run/attempt/actor, and an independent positive qualification
artifact ID/name/digest bound to that milestone run. It also binds the canonical
`qualification-manifest.json` bytes and self-digest. It rejects any artifact ID
or digest reused from the signed-UI capture artifact. The exact accepted-case
set is:

- `sparkle-update-route`
- `clean-machine-signed-update`
- `installed-ui-accessibility`
- `profile-save-action-accessibility`

Each of the four case receipts includes only `path`, `sha256`, `source`, and
`accepted_at`, and each path is canonical inside the release bundle. The
profile case reuses the deeply validated signed-UI receipt, the two Tier 3 cases
run through the maintained Tier 3 receipt validator against historical policy,
and the Sparkle case validates the full live-qualification candidate, updater,
profile, artifact, workflow, manifest, and clean-machine receipt bindings. The
qualification also has explicit passed updater-route and preserved-profile
results tied to their exact receipt file digests.

`disposition-v2.json` binds the capture digest, failed workflow path/run/attempt
/actor, structured failure `code`, `subject`, `expected`, and `observed`, plus
all three terminal identity-preservation flags. A failed disposition therefore
cannot silently be reused for a different source, release, or signed artifact.

## Compatibility

V1 evidence remains read-only and is reported with explicit historical classes
after calling the maintained v1 validators for receipts, publication records,
qualification manifests, qualification snapshots, signed-UI archives, and
failed post-publication records:
`legacy-receipt-v1`, `legacy-publication-v1`,
`legacy-qualification-manifest-v1`, and
`legacy-failed-post-publication-v1`. V2 never rewrites legacy evidence.
