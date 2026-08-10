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
  --qualification docs/qualification/stable-signed-qualification-v1.json \
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
| `milestone` | Everything from earlier phases plus blocking live-publication Tier 2 and automated Tier 3 evidence required before milestone closeout; optional operational evidence remains visible. |

Only Tier 2 cases that explicitly declare `requires_live_publication: true` may
use the `milestone` phase. The real Sparkle update route remains blocking because
its exact-candidate evidence requires the published DMG, checked release receipt,
and live appcast. Native Sparkle desktop capture is retained as nonblocking
presentation evidence. Exact release-note source identity, embedded Markdown,
full-notes URL, appcast identity, installed release-notes URL, and accessibility
semantics remain enforced by the guarded release engine, live Sparkle route, and
installed UI receipts. Other Tier 2 invalidations continue to block preparation
and require another reviewed candidate rather than being deferred through
publication.

```sh
uv run python -m scripts.qualify_release_scope \
  --candidate-sha "$CANDIDATE_SHA" \
  --qualification docs/qualification/stable-signed-qualification-v1.json \
  --evidence path/to/checked-evidence.json \
  --release-stage stable \
  --workflow-phase preparation \
  --require-evidence
```

Carry-forward is allowed only when an accepted named receipt exists and the
diff from that receipt's source SHA contains no path covered by the case's
direct invalidation patterns or referenced contracts. Stable continues to
require the live Sparkle route and automated clean-machine/UI receipts. It does
not force a fresh physical-hardware receipt or manual native-notes capture solely
because a Stable milestone is due. Tier 1 invalidation mappings document
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
[`docs/tier3-clean-machine.md`](tier3-clean-machine.md). It uses an isolated
synthetic home in a runner-owned disposable location, reuses the installed-app
package smoke, verifies a real-feed update from an exact prior signed release,
and emits checked `clean-machine-signed-update` and
`installed-ui-accessibility` receipts.

Physical drive, protected-media conversion, and Vision Pro playback evidence
uses the bounded operator-assisted helper documented in
[`docs/tier3-operator-hardware.md`](tier3-operator-hardware.md). The helper
derives assertion states from exact enums, binds the checked release artifact,
and rejects private media names, paths, serials, screenshots, and raw logs.
These cases are visible operational evidence governed by expiry, mapped
invalidation, environment/device-family changes, and explicit retest requests;
they do not block release publication or evidence-PR reconciliation.

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

The release engine enforces qualification at two secret-free guarded boundaries,
and protected pull-request CI enforces the post-publication blocking milestone
boundary while reporting nonblocking operational evidence separately.
None of these checks receives signing, notarization, PyPI, Sparkle, or Pages
secrets.

Pull-request CI distinguishes release preparation evidence from checked
post-publication evidence. A preparation PR may update the configured evidence
index without a checked release receipt only when the same diff updates the
configured qualification record and every immutable candidate field remains
present with an explicit JSON `null`. The candidate must advance from the bound
published Stable identity to a newer derived Stable version and global build,
and the evidence index may only
append receipts without modifying or deleting accepted history. Any mutation
under `docs/release-evidence/`, any evidence-index change without that validated
preparation transition, or any qualification record carrying release IDs, run
IDs, source SHA, artifact digests, or appcast digest still requires exactly one
canonical checked release receipt on the idempotent
`automation/release-evidence-<tag>` branch.

**Early gate (`qualify-preparation`)** runs after `prepare` and before `package`
(the macOS signing job). It checks the `preparation` phase using the exact
`github.sha`, the committed Sparkle channel as the release stage, the checked
`docs/qualification/release-evidence-v1.json` as evidence, and
`docs/qualification/stable-signed-qualification-v1.json` as the candidate file.
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
  --qualification docs/qualification/stable-signed-qualification-v1.json \
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
  --qualification docs/qualification/stable-signed-qualification-v1.json \
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

After publication, the Release Evidence workflow opens or updates
`automation/release-evidence-<tag>`. New evidence branches must include the
canonical public-safe `docs/release-evidence/<tag>/qualification-manifest.json`
plus the checked `qualification-record.json` and
`signed-artifact-ui-receipt.json`. Release Evidence creates the qualification
record atomically after updating the rolling qualification file, rejects a
different snapshot on rerun, and validates its candidate identity against the
exact release receipt. The manifest is exact-key JSON with a deterministic
self-digest over the canonical payload. It binds the
candidate version, tag, build, release ID, reviewed qualification-runner `main`
SHA, release workflow run/attempt/actor/name/path, prior release tag, Sparkle
route, release receipt path/file
digest/self digest, DMG/checksum/appcast/signed-app-tree identities, signed UI
Actions artifact ID/digest/archive path/receipt path/file digest/self digest,
qualification-record digest, policy and route-table digests, controller runner
digest, canonical evidence ref/base SHA,
the evidence-index baseline digest, and policy case classifications/checkpoint
digest. Release Evidence selects the newest lower published release whose tag is
an ancestor of the candidate. Prerelease candidates use their own Sparkle route;
stable candidates use the selected prior release's route, preserving transitions
such as RC to Stable. The prior release's checked receipt must already be present
in canonical evidence; missing prior evidence stops manifest creation rather than
reconstructing release identity. The evidence-index baseline remains an audit
checkpoint while validated milestone receipts append to the checked index. The
pull-request gate verifies that baseline against protected `main` and rejects
any rewrite of accepted evidence history. Milestone qualification rechecks the recorded Actions
artifact ID and digest through GitHub while metadata is retained; after GitHub
deletes expired metadata, the exact checked receipt and original Actions ZIP
captured and validated by Release Evidence are the durable replacement.
Manifest validation hashes that ZIP and requires its sole payload to be the
byte-identical checked receipt. Controller, policy, route-table, policy
checkpoint, and case-classification validation uses Git revision reads at the
manifest's recorded runner SHA rather than the current checkout, so historical
manifests remain valid after normal protected-main evolution. The current
evidence index, checked receipts, qualification snapshot, and original Actions
ZIP remain validated from the evidence tree, while the baseline evidence index
is validated at the manifest's canonical evidence base SHA. A later
protected-main advance may refresh runner-owned and evidence-baseline checkpoint
fields, but only after the prior manifest validates and the immutable release,
qualification snapshot, workflow, receipt, prior-release, and signed-UI
identities remain byte-for-byte equivalent. Evidence pull requests may not
modify the runner-bound policy or route table directly. A rolling qualification
merge conflict may be resolved only in favor of protected main because the
release-specific snapshot is immutable; every other conflict fails closed. If
no checked receipt was
captured before artifact expiry, qualification stops rather than reconstructing
evidence. Absolute paths, private field
names, conflicts, and partial manifest input state fail closed. CI validates the
checked manifest or, for immutable historical evidence that predates the
manifest, the checked release receipt against the configured qualification
record with `scripts.release_milestone_context`, then runs:

```sh
python -m scripts.qualify_release_scope \
  --policy "$POLICY_PATH" \
  --candidate-sha "$CANDIDATE_SHA" \
  --release-stage "$RELEASE_STAGE" \
  --evidence "$EVIDENCE_PATH" \
  --qualification "$QUALIFICATION_PATH" \
  --milestone-release-receipt "$RELEASE_RECEIPT_PATH" \
  --workflow-phase milestone \
  [--first-candidate-of-cycle] \
  --output release-qualification-milestone.json \
  --require-evidence
```

### Inspect Checked Qualification Status

The maintained read-only controller reports the current checked state for one
explicit release tag:

```sh
uv run python -m scripts.release_qualification_controller status \
  --release-tag v0.3.1
```

It performs no GitHub calls, dispatches, commits, pushes, comments, pull-request
updates, or git ref/index mutations. It uses only checked repository evidence
and fails if any controlling worktree file differs from `HEAD`. It uses the
existing fail-closed milestone validators and scope classifier. When a canonical
manifest exists, its recorded runner revision supplies the policy and its
evidence-index history must remain append-only from the recorded base. When no
manifest exists, the legacy release receipt and publication record must match
the rolling qualification candidate exactly. A present but invalid manifest
never falls back to legacy evidence.

The JSON report preserves the classifier facts for every case and adds
overlapping groups for `completed`, `stale`, `blocking`, `optional`, and
`operator_required`. `stale` means previously accepted or preregistered evidence
was invalidated, expired, explicitly retired, disallowed from carry-forward, or
bound to a different milestone receipt. `operator_required` is emitted only
when an applicable operator-assisted case is actually due. Optional cases remain
visible without satisfying or bypassing blockers. Exit `0` means classification
completed without blockers, exit `2` means classification completed with
blockers, and exit `1` means evidence or identity validation failed. The
optional `--as-of YYYY-MM-DD` input makes expiry-sensitive reports reproducible.

### Resume Blocking Automated Qualification

`resume` is an observation-first state machine with one bounded mutation: an
exact Milestone Qualification workflow dispatch. It must run from the checked
head of `automation/release-evidence-<tag>` when `status` still reports blocking
cases. Releases whose checked durable evidence already satisfies every blocker,
including legacy v0.3.1, return `complete` without contacting GitHub or writing a
checkpoint.

Before offering dispatch, the controller verifies the repository identity,
remote `main`, evidence ref and SHA, exactly one same-repository open evidence
PR, docs-only branch diff, manifest self digest, runner SHA, candidate and
release identities, signed UI artifact, policy/checkpoint/route/controller
digests, and existing exact workflow runs through the same active GitHub
identity used for dispatch. The workflow display title includes
the release tag and full manifest digest because `workflow_dispatch` returns no
run ID. More than one active exact run, a moved ref, a mismatched checkpoint, a
fork PR, a skipped qualification job, partial identity, or any non-documentation
evidence-branch change fails closed.

The initial command reports `dispatch_ready` and the exact values required for
authorization. Dispatch occurs only when both `--expected-main-sha` and
`--expected-manifest-sha256` match the preflight identity and the active local
GitHub login is `cbusillo`. Before the API call, the controller atomically writes
a mode-`0600` prepared checkpoint under the shared git directory. It records the
observed run only after exactly one newer run with the expected workflow path,
branch, head SHA, actors, tag, and manifest digest appears. A prepared checkpoint
with no visible run blocks redispatch, and a local checkpoint lock rejects
concurrent controller processes. Once the visibility window expires, retrying
that unresolved dispatch requires its exact checkpoint self digest through
`--retry-checkpoint-sha256` and repeats every remote identity check. Retrying a
failed or expired run also requires its exact ID through `--retry-run-id`.

This stage observes successful job and artifact state but deliberately does not
download ZIPs or mutate evidence refs, commits, comments, or pull requests.
`artifact_available` requires validated reconciliation as the next controller
stage. Expired milestone transport may trigger an explicitly authorized fresh
qualification run; it never reconstructs receipts. If equivalent receipts are
already committed and accepted, `status` reports no blockers and `resume`
returns `complete` regardless of Actions retention.

The initial evidence PR is expected to remain unmergeable while blocking
live-artifact or automated Tier 3 receipts are absent. Collectors add validated
receipts to that same idempotent branch. Optional physical and native-window
presentation outcomes remain visible in the report. A blocking milestone
failure never rebuilds, retags, re-signs, replaces, or unpublishes the immutable
release; it blocks evidence reconciliation and milestone completion until the
checked blocking evidence passes.
CI discovers checked manifest or release-receipt changes from the pull-request
diff and requires a same-repository PR targeting `main`, the exact
`automation/release-evidence-<tag>` branch, and a docs-only diff, so a fork or a
copied evidence branch cannot skip the milestone gate.

The `--output` flag writes the JSON report before the enforcement exit,
so the `if: always()` upload step captures it even when enforcement fails. The
classifier also writes a structured error report when policy or receipt
validation fails before case classification. The `--workflow-phase` flag keeps
later-phase retests visible but explicitly deferred. `artifact` additionally
binds the exact run, release, and asset identities recorded in the receipt.
`milestone` consumes the checked immutable receipt and enforces every blocking
live-publication Tier 2 and automated Tier 3 case. It reports due nonblocking
operator evidence without converting it into a pass. The receipt's appcast
digest is the verified snapshot awaiting deployment during the artifact phase;
live Pages state is required only after publication.

After the operator workflow completes, `.github/workflows/release-evidence.yml`
validates the successful `workflow_dispatch` run, approved actor, protected
source SHA, immutable release fields, asset IDs and sizes, receipt digest, live
Pages appcast, and the same-run signed UI artifact metadata and receipt while
the Actions artifact is still available. It then opens or updates one
task-branch PR containing the release receipt, signed UI receipt, qualification
manifest, publication record, release ledger, qualification fields, and cut
packet status. The workflow has no signing, notarization, Sparkle private-key,
PyPI, or deployment secrets. The evidence PR cannot merge until protected CI's
milestone report passes.
