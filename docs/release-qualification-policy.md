# Release Qualification Policy

The versioned policy at
[`docs/qualification/release-qualification-policy-v1.json`](qualification/release-qualification-policy-v1.json)
separates release evidence by risk and ownership:

- Tier 0 is automatic per-commit CI coverage.
- Tier 1 is exact published-artifact evidence from the guarded release engine.
- Tier 2 is functional evidence invalidated by mapped repository paths or shared contracts.
- Tier 3 is periodic or hardware evidence with explicit milestone and expiry rules.
- Tier 4 is post-publication observation and never blocks dispatch or publication.

The classifier never performs signing, notarization, publication, or remote
GitHub reads. It validates checked receipt references and compares their source
SHAs with the candidate using the local Git repository.

## Validate The Policy

```sh
uv run python -m scripts.qualify_release_scope --validate-policy
```

## Classify A Candidate

```sh
uv run python -m scripts.qualify_release_scope \
  --candidate-sha "$CANDIDATE_SHA" \
  --qualification docs/qualification/rc3-signed-qualification-v1.json \
  --evidence path/to/checked-evidence.json \
  --release-stage rc
```

Evidence uses schema version 1 and a `receipts` array. Each accepted receipt
names a stable policy case, evidence source, source SHA, acceptance timestamp,
repository-relative reference, and SHA-256 digest. The referenced receipt must
be committed in the current repository `HEAD`, so an untracked local file can
never satisfy release preparation. Tier 1 receipts additionally record a
successful workflow conclusion and positive release-run ID. Tier 3 receipts
must name every hardware/environment requirement declared by the case.

Use `--require-evidence` during release preparation. It exits with status `2`
when any blocking case remains `retest`; Tier 4 `external` cases never affect
that exit status.

```sh
uv run python -m scripts.qualify_release_scope \
  --candidate-sha "$CANDIDATE_SHA" \
  --qualification docs/qualification/rc3-signed-qualification-v1.json \
  --evidence path/to/checked-evidence.json \
  --release-stage rc \
  --require-evidence
```

Carry-forward is allowed only when an accepted named receipt exists and the
diff from that receipt's source SHA contains no path covered by the case's
direct invalidation patterns or referenced contracts. RC3 migration overrides
keep its Sparkle, accessibility, PGS, and subtitle-diagnostic checks fresh.
Tier 1 invalidation mappings document release-engine ownership only; Tier 1
always requires a new exact-candidate receipt and never carries.

## Release Engine Receipts

Every successful guarded Stable or Prerelease publication includes a
`release-receipt.json` GitHub Release asset. The engine builds it only after the
DMG, checksum, appcast, signatures, notarization, Gatekeeper checks, package
smokes, attestation, and draft assets have been verified. It is uploaded while
the release is still a draft, re-downloaded by asset ID, and digest-verified
before publication.

The receipt's `receipt_sha256` is the SHA-256 of canonical compact JSON with the
`receipt_sha256` field omitted. The checked evidence index separately records
the SHA-256 of the exact formatted receipt file downloaded from GitHub. This
avoids a self-referential file hash while preserving deterministic semantic and
byte-for-byte identities.

After the operator workflow completes, `.github/workflows/release-evidence.yml`
validates the successful `workflow_dispatch` run, approved actor, protected
source SHA, immutable release fields, asset IDs and sizes, receipt digest, and
live Pages appcast. It then opens or updates one task-branch PR containing the
receipt, publication record, release ledger, qualification fields, and cut
packet status. The workflow has no signing, notarization, Sparkle private-key,
PyPI, or deployment secrets.
