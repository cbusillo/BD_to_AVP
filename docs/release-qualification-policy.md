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

The clean-machine update and installed-UI cases also invalidate when their
seed or expected profile fixture changes. Unrelated test fixtures do not
require new evidence. Queue persistence,
resolution, work coordination, and worker cancellation/stopping state belong to the conversion-ownership
contract, so changes there require evidence for every case that depends on
that contract to be re-established through the existing qualification workflow.
Renames are compared as a deletion and an addition: both the old and new paths
are checked against invalidation mappings, even when the file bytes stay the same.

## Validate The Policy

For commands that take `--qualification`, select the current candidate record
from `releaseOperations.qualificationRecordPath` in `.github/github.json`:

```sh
export RELEASE_QUALIFICATION_RECORD="$(jq -r '.releaseOperations.qualificationRecordPath' .github/github.json)"
```

For a historical candidate, explicitly select its archived qualification record
instead. The generic original Stable record is not the current candidate.

```sh
uv run python -m scripts.qualify_release_scope --validate-policy
```

## Classify A Candidate

```sh
uv run python -m scripts.qualify_release_scope \
  --candidate-sha "$CANDIDATE_SHA" \
  --qualification "$RELEASE_QUALIFICATION_RECORD" \
  --evidence path/to/checked-evidence.json \
  --release-stage stable
```

Evidence uses schema version 1 and a `receipts` array. Each accepted receipt
names a stable policy case, evidence source, source SHA, acceptance timestamp,
repository-relative reference, and SHA-256 digest. The referenced receipt must
be committed in the current repository `HEAD`, so an untracked local file can
never satisfy release preparation. Tier 1 receipts additionally record a
successful workflow conclusion and positive release-run ID. Tier 3 evidence
references a checked `bd-to-avp-tier3-qualification` receipt that is validated
against the selected case and its exact checked release receipt.

Milestone-owned Tier 2 references must be structured JSON containing a matching
candidate identity, a passed accepted case ID, and an embedded qualification
timestamp after publication. The classifier derives the canonical checked
release-receipt and publication-record paths from that candidate tag, verifies
their file digests and release/run/appcast identities, and requires index
acceptance after publication. This prevents a generic exact-SHA file or a
relabeled pre-publication observation from satisfying a live-publication case.

Use `--require-evidence` during release preparation. It exits with status `2`
when any case applicable to the selected workflow phase remains a blocking
`retest`; later-phase cases remain visible as deferred, and Tier 4 `external`
cases never affect that exit status.

The ordered phases are:

| Classifier phase | Enforced evidence |
| --- | --- |
| `preparation` | Per-commit coverage and ordinary change-scoped Tier 2 evidence required before signing. |
| `artifact` | Everything from preparation plus exact same-run Tier 1 receipt evidence required before publication. |
| `milestone` | Classifier phase for live-publication Tier 2 and automated Tier 3 cases. Nothing runs it now; the update test covers the update route before publication. |

Only Tier 2 cases that explicitly declare `requires_live_publication: true` may
use the `milestone` phase. Native Sparkle desktop capture is retained as nonblocking
presentation evidence. Exact release-note source identity, embedded Markdown,
full-notes URL, appcast identity, installed release-notes URL, and accessibility
semantics remain enforced by the guarded release engine, the update test, and
installed UI receipts. Other Tier 2 invalidations continue to block preparation
and require another reviewed candidate rather than being deferred through
publication.

The release engine also runs a secret-free macOS package/UI qualification after
preparation classification and before the `macos-signing` environment job. It
uses an ad-hoc signed production package and production-shaped DMG to exercise
worker identity, startup, cancellation, preview, bundled tools, clean-library
profile creation, DMG installation, and installed accessibility behavior. This
gate is preventive rather than evidentiary: it must pass before signing can
begin, while the exact Developer ID signed artifact still requires fresh
artifact receipts and the update test.

```sh
uv run python -m scripts.qualify_release_scope \
  --candidate-sha "$CANDIDATE_SHA" \
  --qualification "$RELEASE_QUALIFICATION_RECORD" \
  --evidence path/to/checked-evidence.json \
  --release-stage stable \
  --workflow-phase preparation \
  --require-evidence
```

Carry-forward is allowed only when an accepted named receipt exists and the
diff from that receipt's source SHA contains no path covered by the case's
direct invalidation patterns or referenced contracts. A change to
`pyproject.toml`, `uv.lock`, or `macos/project.yml` that is only the release
version and build written by `scripts.release` does not count, so a Beta whose
signing, packaging, Sparkle, bundle layout, entitlements, and toolchain did not
move carries the prior updater and clean-machine evidence instead of rerunning
it. Any other change to those files still invalidates. The update test in
`.github/workflows/update-test.yml` runs before publication for every release,
Stable included, whatever the classifier carries. Tier 1 invalidation mappings document
release-engine ownership only; Tier 1 always requires a new exact-candidate
receipt and never carries.

## Tier 3 Cadence And Receipts

Tier 3 is risk- and cadence-based, not a blanket prerelease matrix. Each case
declares its automation lane, automated or operator-assisted execution mode,
allowed macOS environment classes and architecture, required public-safe
hardware fields, first-RC and Stable cadence, expiry, invalidating contracts,
assertions, evidence digest kinds, and cleanup outcomes.

The classifier marks Tier 3 evidence due only when one of these triggers applies:

- the case declares the first candidate of an RC cycle;
- an automated case declares a Stable candidate;
- checked evidence has passed its receipt-declared expiry; or
- mapped code, packaging, UI, updater, runtime, or playback paths changed;
- an environment or device-family change is recorded as an explicit retest; or
- a maintainer explicitly requests a retest.

An ordinary alpha, beta, later RC, or Stable release remains non-applicable for
operator-assisted hardware when the case is not expired, invalidated, or
explicitly requested. The USB/MakeMKV first-RC baseline is still scheduled, but
it is operational evidence rather than a release blocker. Valid prior evidence
carries without becoming a blocking requirement. The report exposes the
deterministic trigger for every case (`first_rc`, `stable_candidate`, `expired`,
`invalidated`, `explicit_retest`, `cadence_valid`, or `not_due`) and reports
nonblocking operational status as `passed`, `failed`, `skipped`, `missing`, or
`due`.

A candidate migration may use `fresh_retest` for nonblocking physical evidence
only with `retest_reason` set to `first_baseline`, `environment_changed`,
`device_family_changed`, or `maintainer_request`. Optional native presentation
may use only `rendering_contract_changed` or `maintainer_request`. This prevents
an unexplained per-release ceremony from becoming policy by accident.

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

Passed receipts require every declared assertion to pass. Failed and skipped
receipts remain valid audit records with explicit bounded reason codes. For the
four policy-approved nonblocking operational cases, they may be indexed with
matching `failed` or `skipped` status so reports remain honest, but they never
satisfy a blocking assertion. Blocking cases reject failed or skipped index
entries. Only a passed receipt may be indexed as accepted release evidence.

The maintained clean-machine, Sparkle, and installed UI/accessibility collector is documented in
[`docs/tier3-clean-machine.md`](tier3-clean-machine.md). Its local
`restorable-location` lane uses an isolated synthetic home in a runner-owned
location. Its GitHub-hosted `resettable-vm` lane uses the runner's real home and
normal `/Applications`, with strict hosted-runner, clean-state, exact-bundle
cleanup, and bounded diagnostic guards. Both reuse the installed-app package
smoke, revalidate the exact prior signed app immediately before an
identifier-scoped bounded updater state machine, permit one clean retry only
for classified pre-press environmental startup failures or a guarded state
change before any action is pressed, detects exact candidate relaunches and
application-process turnover between UI polls, verifies the final candidate
identity, and emits checked
`clean-machine-signed-update` and `installed-ui-accessibility` receipts.

Physical drive, protected-media conversion, and Vision Pro playback evidence
uses the guided collector and bounded receipt builder documented in
[`docs/tier3-operator-hardware.md`](tier3-operator-hardware.md). They derive
safe machine facts and assertion states from exact enums, bind the checked
release artifact, and reject private media names, paths, serials, screenshots,
tokens, diagnostic identifiers, and raw logs.
These cases are visible operational evidence governed by expiry, mapped
invalidation, environment/device-family changes, and explicit retest requests;
they do not block release publication.

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
Neither receives signing, notarization, PyPI, Sparkle, or Pages
secrets.

Pull-request CI no longer validates release evidence or preparation records.
The release engine's gates below are the checks that remain.

An Alpha or Beta preparation remediation may also append change-scoped Tier 2
receipts without resetting the Stable qualification record. Both stages share
one set of requirements, and RC and Stable candidates cannot use this lane
(`CHANGE_SCOPED_EVIDENCE_STAGES` in `scripts/release.py`). This lane requires the
exact `qualify/<tag>` branch, one new public-safe
`docs/qualification/<tag>-change-scoped-evidence-v1.json` document whose source
identity is that same release, receipts bound to the pull-request base SHA and
that document's digest, and policy cases owned by the `release_candidate`
phase. It rejects artifact-owned, live publication, milestone, Tier 1, and
Tier 3 evidence, and it cannot claim Developer ID signing, notarization, or a
created release identity.
`uv run python -m scripts.beta_change_scoped_evidence` produces the document and
receipts for this lane; see `release-process.md`.

These receipts appear in `release-evidence-v1.json` with
`source: signed_artifact_receipt` because that is the evidence-source name the
policy admits for these cases, not because a production-signed artifact was
tested. The referenced document records `packaged_candidate.signing:
ad_hoc_local` and `evidence_source_semantics.developer_id_or_notarization_claimed:
false`; capacity cases record `capacity_readings: controlled_test_fixtures`.
Do not read one of these receipts as Developer ID, notarization, or
physical-condition proof. The network case does not rerun a mounted-network conversion; its
`scope.prior_real_network_evidence` names the RC 3 targeted record, whose
`network-generated-final-output` case observed a network destination. Documents
produced before October 2026 name the `v0.3.1` MKV audio record there instead.
That record holds audio-layout and no-audio regression results from an ad-hoc
package and is not network-destination proof. Those documents stay unchanged as
immutable evidence.

**Early gate (`qualify-preparation`)** runs after `prepare` and before `package`
(the macOS signing job). It checks the `preparation` phase using the exact
`github.sha`, the committed Sparkle channel as the release stage, the checked
`docs/qualification/release-evidence-v1.json` as evidence, and
the candidate file selected by `releaseOperations.qualificationRecordPath` in
`.github/github.json`.
When committed metadata identifies the first candidate of a cycle,
`--first-candidate-of-cycle` is passed. The preparation report is uploaded as a workflow Actions artifact with
30-day retention before enforcement exits. macOS signing cannot reach the
`macos-signing` environment until this gate passes.

The qualification record path is committed release metadata. Release
preparation must update the workflow and `.github/github.json` catalog to the
new candidate record before dispatch; a missing, stale, or mismatched record
fails closed.

**Signed artifact UI (`signed-artifact-ui`)** runs after `build-receipt` on a
secret-free macOS 26 runner. It downloads the exact verified release receipt and
package from same-run Actions artifacts by immutable artifact ID, verifies their
externally supplied file digests and the receipt's GitHub Release asset bindings,
installs the signed DMG, runs the maintained
`BluRayToVisionProUITests/InstalledUIAcceptanceTests/testCandidateMainWindowProfileAndSettings`
selector, normalizes the candidate UI evidence, and emits a bounded
`signed_artifact_receipt` for `profile-save-action-accessibility`. That case
remains Tier 2, but it is artifact-owned and preparation defers invalidated
evidence to this phase. The workflow uploads only the normalized receipt with a
seven-day retention window; the artifact gate downloads it by immutable Actions
artifact ID and verifies the externally supplied receipt file digest.
Main-window readiness is asserted through XCUITest; the raw AX receipt is limited
to the actionable profile and updater controls plus the source-bound releases link
because macOS does not consistently expose combined SwiftUI status text as an AX node.
The update-route picker remains bound by identifier, `AXPopUpButton` role, enabled
state, and `AXPress`; its raw label may be empty because macOS 27 omits the visible
SwiftUI picker title from that AX node.
If the UI lane fails, it uploads a separate seven-day diagnostic artifact containing
only the bounded `.xcresult`, partial UI evidence, and failure summary; the installed
app, synthetic home, and qualification workspace are still deleted.

**Artifact gate (`qualify-artifact`)** runs after `build-receipt` and
`signed-artifact-ui` and before `publish-release`. It downloads the already
verified release receipt from a same-run Actions artifact by immutable artifact
ID, verifies its SHA-256 digest against
`build-receipt.outputs.receipt_file_sha256`,
then invokes the `artifact` phase binding the exact release route, workflow run
ID and attempt, release ID, original GitHub Release receipt asset ID, release receipt file and
self digests, signed app tree digest, DMG asset ID/name/size/digest, and
checksum/appcast asset IDs and digests. It also validates the signed UI receipt
against those same bindings. The artifact report is uploaded before enforcement.
Publication cannot proceed until both gates pass.

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
  --qualification "$RELEASE_QUALIFICATION_RECORD" \
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
  --qualification "$RELEASE_QUALIFICATION_RECORD" \
  --workflow-phase artifact \
  [--first-candidate-of-cycle] \
  --release-receipt release-receipt.json \
  --signed-artifact-receipt signed-artifact-ui-receipt.json \
  --signed-artifact-receipt-sha256 "$SIGNED_ARTIFACT_RECEIPT_SHA256" \
  --release-route "$RELEASE_ROUTE" \
  --workflow-run-id "$GITHUB_RUN_ID" \
  --workflow-run-attempt "$GITHUB_RUN_ATTEMPT" \
  --release-id "$RELEASE_ID" \
  --package-version "$PACKAGE_VERSION" \
  --public-version "$PUBLIC_VERSION" \
  --build-version "$BUILD_VERSION" \
  --release-tag "$RELEASE_TAG" \
  --dmg-name "$DMG_NAME" \
  --release-receipt-asset-id "$RECEIPT_ASSET_ID" \
  --release-receipt-sha256 "$RECEIPT_FILE_SHA256" \
  --release-receipt-self-sha256 "$RECEIPT_SELF_SHA256" \
  --signed-app-tree-sha256 "$SIGNED_APP_TREE_SHA256" \
  --dmg-asset-id "$DMG_ASSET_ID" \
  --dmg-size "$DMG_SIZE" \
  --dmg-sha256 "$DMG_SHA256" \
  --checksum-asset-id "$CHECKSUM_ASSET_ID" \
  --checksum-sha256 "$CHECKSUM_SHA256" \
  --appcast-asset-id "$APPCAST_ASSET_ID" \
  --appcast-sha256 "$APPCAST_SHA256" \
  --output release-qualification-final.json \
  --require-evidence
```

## After Publication

Nothing is committed back to `main` after publication. The release receipt stays
attached to the GitHub Release, and the update test has already run before
publication. Historical files under `docs/release-evidence/` stay as they are.
