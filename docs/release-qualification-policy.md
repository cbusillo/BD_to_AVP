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
successful workflow conclusion and positive release-run ID. Tier 3 evidence
references a checked `bd-to-avp-tier3-qualification` receipt that is validated
against the selected case and its exact checked release receipt.

Use `--require-evidence` during release preparation. It exits with status `2`
when any case applicable to the selected workflow phase remains a blocking
`retest`; later-phase cases remain visible as deferred, and Tier 4 `external`
cases never affect that exit status.

```sh
uv run python -m scripts.qualify_release_scope \
  --candidate-sha "$CANDIDATE_SHA" \
  --qualification docs/qualification/rc3-signed-qualification-v1.json \
  --evidence path/to/checked-evidence.json \
  --release-stage rc \
  --workflow-phase preparation \
  --require-evidence
```

Carry-forward is allowed only when an accepted named receipt exists and the
diff from that receipt's source SHA contains no path covered by the case's
direct invalidation patterns or referenced contracts. RC3 migration overrides
keep its Sparkle, accessibility, PGS, and subtitle-diagnostic checks fresh.
Tier 1 invalidation mappings document release-engine ownership only; Tier 1
always requires a new exact-candidate receipt and never carries.

## Tier 3 Cadence And Receipts

Tier 3 is risk- and cadence-based, not a blanket prerelease matrix. Each case
declares its automation lane, automated or operator-assisted execution mode,
allowed macOS environment classes and architecture, required public-safe
hardware fields, first-RC and Stable cadence, expiry, invalidating contracts,
assertions, evidence digest kinds, and cleanup outcomes.

The classifier requires Tier 3 evidence only when one of these triggers applies:

- the case declares the first candidate of an RC cycle;
- the case declares a Stable candidate;
- checked evidence has passed its receipt-declared expiry; or
- mapped code, packaging, UI, updater, runtime, or playback paths changed.

An ordinary alpha, beta, or later RC remains non-applicable when a case is not
expired or invalidated. Valid prior evidence carries without becoming a
blocking requirement. The report exposes the deterministic trigger for every
case (`first_rc`, `stable_candidate`, `expired`, `invalidated`, `cadence_valid`,
or `not_due`).

Tier 3 receipts bind the release source SHA, tag, route, versions, signed app
tree, DMG/checksum/appcast digests, and both semantic and file digests of the
checked release receipt. Environment identity is limited to environment class,
architecture, public macOS version, and public Apple build. Hardware identity
uses only the bounded field names declared by policy; hostnames, usernames,
paths, volume or disc titles, serials, tokens, media, and free-form logs are not
accepted. Evidence is represented by at most eight named SHA-256 digests.

Validate a checked Tier 3 receipt offline with:

```sh
uv run python -m scripts.tier3_receipt \
  --case-id clean-machine-signed-update \
  --receipt path/to/tier3-receipt.json
```

Passed receipts require every declared assertion to pass. Failed, skipped, and
not-applicable receipts remain valid audit records with explicit bounded reason
codes, but only a passed receipt may be indexed as accepted release evidence.

The maintained clean-machine and Sparkle collector is documented in
[`docs/tier3-clean-machine.md`](tier3-clean-machine.md). It uses an isolated
synthetic home in a runner-owned disposable location, reuses the installed-app
package smoke, verifies a real-feed update from an exact prior signed release,
and emits the checked `clean-machine-signed-update` receipt.

## Release Engine Receipts

Every successful guarded Stable or Prerelease publication includes a
`release-receipt.json` GitHub Release asset. The engine builds it only after the
DMG, checksum, appcast, signatures, notarization, Gatekeeper checks, package
smokes, attestation, and draft assets have been verified. It is uploaded while
the release is still a draft, re-downloaded by asset ID, and digest-verified
before publication.

RC 3 is the one historical transition exception: it was published before the
receipt automation merged. Its checked receipt was generated afterward from
verified public facts and is explicitly marked as a post-publication backfill;
the immutable RC 3 release does not contain a receipt asset.

The receipt's `receipt_sha256` is the SHA-256 of canonical compact JSON with the
`receipt_sha256` field omitted. The checked evidence index separately records
the SHA-256 of the exact formatted receipt file downloaded from GitHub. This
avoids a self-referential file hash while preserving deterministic semantic and
byte-for-byte identities.

## Workflow Integration

The release engine enforces qualification at two secret-free guarded boundaries.
Neither job touches signing, notarization, PyPI, or Pages secrets, and both run
on `ubuntu-latest` without a write-capable repository token.

**Early gate (`qualify-preparation`)** runs after `prepare` and before `package`
(the macOS signing job). It checks the `preparation` phase using the exact
`github.sha`, the committed Sparkle channel as the release stage, the checked
`docs/qualification/release-evidence-v1.json` as evidence, and
`docs/qualification/rc3-signed-qualification-v1.json` as the candidate file.
When committed metadata identifies the first candidate of a cycle,
`--first-candidate-of-cycle` is passed. The preparation report is uploaded as a workflow Actions artifact with
30-day retention before enforcement exits. macOS signing cannot reach the
`macos-signing` environment until this gate passes.

The qualification record path is committed release metadata. Release
preparation must update the workflow and `.github/github.json` catalog to the
new candidate record before dispatch; a missing, stale, or mismatched record
fails closed.

**Artifact gate (`qualify-artifact`)** runs after `build-receipt` and before
`publish-release`. It downloads the release receipt by its exact GitHub Release
asset ID (`build-receipt.outputs.receipt_asset_id`), verifies its SHA-256 digest
against `build-receipt.outputs.receipt_file_sha256`, then invokes the `artifact`
phase binding the exact release route, workflow run ID and attempt, release ID,
signed app tree digest, and DMG/checksum/appcast asset IDs and digests. The
artifact report is uploaded before enforcement. Publication cannot proceed until
this gate passes.

Both gates enforce using `--require-evidence` and upload their reports as Actions
artifacts rather than GitHub Release assets. The exact CLI interface for the
engine integration is:

```sh
# Early gate — preparation phase
python -m scripts.qualify_release_scope \
  --policy docs/qualification/release-qualification-policy-v1.json \
  --candidate-sha "$GITHUB_SHA" \
  --release-stage "$CHANNEL" \
  --evidence docs/qualification/release-evidence-v1.json \
  --qualification docs/qualification/rc3-signed-qualification-v1.json \
  --workflow-phase preparation \
  [--first-candidate-of-cycle] \
  --output release-qualification-preparation.json \
  --require-evidence

# Artifact gate — artifact phase
python -m scripts.qualify_release_scope \
  --policy docs/qualification/release-qualification-policy-v1.json \
  --candidate-sha "$GITHUB_SHA" \
  --release-stage "$CHANNEL" \
  --evidence docs/qualification/release-evidence-v1.json \
  --qualification docs/qualification/rc3-signed-qualification-v1.json \
  --workflow-phase artifact \
  [--first-candidate-of-cycle] \
  --release-receipt release-receipt.json \
  --release-route "$RELEASE_ROUTE" \
  --workflow-run-id "$GITHUB_RUN_ID" \
  --workflow-run-attempt "$GITHUB_RUN_ATTEMPT" \
  --release-id "$RELEASE_ID" \
  --package-version "$PACKAGE_VERSION" \
  --public-version "$PUBLIC_VERSION" \
  --build-version "$BUILD_VERSION" \
  --release-tag "$RELEASE_TAG" \
  --dmg-name "$DMG_NAME" \
  --release-receipt-sha256 "$RECEIPT_FILE_SHA256" \
  --signed-app-tree-sha256 "$SIGNED_APP_TREE_SHA256" \
  --dmg-asset-id "$DMG_ASSET_ID" \
  --dmg-sha256 "$DMG_SHA256" \
  --checksum-asset-id "$CHECKSUM_ASSET_ID" \
  --checksum-sha256 "$CHECKSUM_SHA256" \
  --appcast-asset-id "$APPCAST_ASSET_ID" \
  --appcast-sha256 "$APPCAST_SHA256" \
  --output release-qualification-final.json \
  --require-evidence
```

The `--output` flag writes the JSON report before the enforcement exit,
so the `if: always()` upload step captures it even when enforcement fails. The
classifier also writes a structured error report when policy or receipt
validation fails before case classification. The
`--workflow-phase` flag keeps publication-owned Tier 1 retests visible but
explicitly deferred during `preparation`; `artifact` additionally binds the
exact run, release, and asset identities recorded in the receipt. The receipt's
appcast digest is the verified snapshot awaiting deployment. Live Pages state
remains owned by the post-publication deployment and reconciliation boundaries,
so it is not required before its owning phase exists.

After the operator workflow completes, `.github/workflows/release-evidence.yml`
validates the successful `workflow_dispatch` run, approved actor, protected
source SHA, immutable release fields, asset IDs and sizes, receipt digest, and
live Pages appcast. It then opens or updates one task-branch PR containing the
receipt, publication record, release ledger, qualification fields, and cut
packet status. The workflow has no signing, notarization, Sparkle private-key,
PyPI, or deployment secrets.
