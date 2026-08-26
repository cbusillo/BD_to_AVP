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

The automation branch is evidence state, not merge authorization. After capture
and qualification are complete, an operator opens the normal protected pull
request to merge the docs-only branch into `main`; branch protection, required
CI, review, and conversation-resolution rules remain unchanged.

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
artifact ID/digest bound to that milestone run. It also binds the canonical
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
