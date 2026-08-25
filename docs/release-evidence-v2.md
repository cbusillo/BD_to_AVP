# Release Evidence v2

Release Evidence v2 is an additive, offline-verifiable, write-once format for
release evidence under `docs/release-evidence/<tag>/`. It does not alter
release, qualification, or workflow/controller behavior. Evidence files are
passive data: the verifier uses only local files and local Git object reads. It
does not invoke `gh`, `curl`, a network client, or credentials.

## Verification

By default the verifier reads the committed `HEAD` tree. Pass a full immutable
commit SHA to replay a historical v2 tree; bundle files and `source_sha`
ancestry are then evaluated against that revision, not against a later
checkout. Clean checked-worktree validation of legacy v1 evidence is supported
through `--worktree`; arbitrary historical legacy revisions are rejected unless
they are materialized as a worktree.

```sh
uv run python -m scripts.release_evidence_v2 --tag v0.3.2 --revision <full-git-sha>
uv run python -m scripts.release_evidence_v2 --all-tags \
  --revision <full-git-sha> \
  --base-revision <full-git-sha>
uv run python -m scripts.release_evidence_v2 --tag v0.3.2 --worktree
```

`--base-revision` enforces write-once history: every v2 record in the base tree
must exist byte-identically in the verified tree. Changed or deleted records are
refused.

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
  template path is exactly
  `docs/qualification/<release-tag>-signed-qualification-v1.json`.

## Terminal Records

A bundle may contain capture only (`v2-captured`) or exactly one terminal
record. A qualification and disposition together are split-brain and are
rejected; either terminal record without capture is rejected.

`qualification-v2.json` binds the capture digest, the same archived
`qualification-record.json` snapshot already bound by capture, successful
milestone path/run/attempt/actor, and an independent positive qualification
artifact ID/digest bound to that milestone run. It rejects any artifact ID or
digest reused from the signed-UI capture artifact. The exact accepted-case set
is:

- `sparkle-update-route`
- `clean-machine-signed-update`
- `installed-ui-accessibility`
- `profile-save-action-accessibility`

Each of the four case receipts includes only `path`, `sha256`, `source`, and
`accepted_at`. All four files and digests are checked, each source must be
allowed by the historical policy, and timestamps must fit the
capture-to-qualification chronology. The qualification also has explicit
passed updater-route and preserved-profile results tied to their exact receipt
digests.

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
