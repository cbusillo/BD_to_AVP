# Protected-Main Release Process

The normative production identity, version mapping, update routes, history
boundary, and publication policy are defined in
[Production Release Routes](release-routes.md).

The repository carries published, immutable Beta 3 through Beta 8, Beta 10, and
Beta 11 history at builds `148` through `156` except burned build `154`. Failed,
unpublished Beta 9 (`0.3.0b9`, build `154`) and the earlier RC candidate build
`147` are permanently burned. RC 1, RC 2, and RC 3 are published at builds
`158`, `159`, and `160`. The
[Beta 8 cut packet](0.3.0-beta.8-cut-packet.md) records immutable publication
history, [the Beta 9 cut packet](0.3.0-beta.9-cut-packet.md) records the failed
unpublished attempt, [the Beta 10 cut packet](0.3.0-beta.10-cut-packet.md)
records immutable publication history, [the Beta 11 cut packet](0.3.0-beta.11-cut-packet.md)
records historical preparation, [the Beta 12 cut packet](0.3.0-beta.12-cut-packet.md)
records immutable publication and corrects the earlier abandoned-metadata claim, [the RC 1 cut packet](0.3.0-rc.1-cut-packet.md)
records immutable publication, [the RC 2 cut packet](0.3.0-rc.2-cut-packet.md)
records immutable publication, and [the RC 3 cut packet](0.3.0-rc.3-cut-packet.md)
records immutable publication plus its targeted qualification result. Stable
`0.3.0` build `161` is published and immutable; its publication and bounded
PyPI recovery are recorded in [the Stable cut packet](0.3.0-cut-packet.md).
Stable `0.3.1` build `162` is published and immutable; its publication is
recorded in [the 0.3.1 cut packet](0.3.1-cut-packet.md). Beta `0.3.2b1` build
`163` and Beta `0.3.2b2` build `164` are published and immutable, tracked in
[the 0.3.2 Beta 1 cut packet](0.3.2-beta.1-cut-packet.md),
[the 0.3.2 Beta 2 cut packet](0.3.2-beta.2-cut-packet.md), issues #584 and #593.
Beta `0.3.2b3` build `165` is a failed unpublished signed attempt and its
identity is permanently burned, as recorded in
[the 0.3.2 Beta 3 cut packet](0.3.2-beta.3-cut-packet.md). Beta `0.3.2b4`
build `166` is also a failed unpublished signed attempt and permanently burned,
as recorded in [the 0.3.2 Beta 4 cut packet](0.3.2-beta.4-cut-packet.md). Beta
`0.3.2b5` build `167` is published and immutable, but its post-publication
exact-artifact qualification is blocked because the packaged worker reported
`0.0.0` instead of `0.3.2b5`; the publication and qualification boundary are
tracked in [the 0.3.2 Beta 5 cut packet](0.3.2-beta.5-cut-packet.md) and its
immutable failed-post-publication disposition. The next corrective identity,
Beta `0.3.2b6` build `168`, became a cancelled unpublished signed attempt after
draft creation and is permanently burned, as recorded in
[the 0.3.2 Beta 6 cut packet](0.3.2-beta.6-cut-packet.md) and
`docs/release-attempts/v0.3.2-beta.6/cancelled-attempt-v1.json`. Its abandoned
draft was deleted only after explicit authorization, with the immutable
disposition at `docs/release-attempts/v0.3.2-beta.6/draft-deletion-v1.json`.
Issue #609 closed published and fully qualified Beta `0.3.2b7`, public tag
`v0.3.2-beta.7`, and build `169`. Issue #613 closed published and fully
qualified RC `0.3.2rc1`, public tag `v0.3.2-rc.1`, and build `170` under
feature freeze. Issue #614 closed published Stable `0.3.2`, public tag
`v0.3.2`, and build `171`. Beta `0.3.3b1` / build `172` through Beta
`0.3.3b5` / build `176` are published and immutable; Beta 5's qualification is
complete in issue #793 and recorded in
[the Beta 5 cut packet](0.3.3-beta.5-cut-packet.md). Issue #817 completed
Stable `0.3.3`, public tag `v0.3.3`, and build `177`, recorded in
[the Stable cut packet](0.3.3-cut-packet.md) and its immutable publication and
qualification records. Future dispatch, signing approval, and publication
remain separately authorized boundaries.

The four-route updater preference, release metadata, production-history
filtering, appcast validation, reusable engine, guarded Stable/Prerelease
entrypoints, Beta 3 bootstrap contract, and one-time metadata recovery are
implemented and regression-covered. RC 3's exact-artifact subtitle field case
passed and issue #458 is closed. Targeted qualification is complete: every link
present in the immutable native updater notes passed, while the absent issue-link
category is explicitly not applicable rather than failed. Metadata
preparation and review do not authorize dispatch; run-bound signing approval
remains a separate verified boundary.

Before a successor version or build identity is prepared, dispatch
`Production Preflight` from protected `main` with the exact full current
40-character `main` SHA. The release-independent workflow rejects abbreviated,
non-`main`, mismatched, or stale SHAs before qualifying the package. It has
read-only repository permission, uses no signing environment or secret, and
cannot create a tag, release, draft, appcast deployment, or package publication.
Its seven-day workflow artifact contains only source-bound validation, package
smoke, installed-UI evidence, and a bounded success manifest; failure artifacts
contain bounded source/workflow context and diagnostic tails rather than the app
or preventive DMG. A passing run is readiness evidence for that exact SHA, not
release authorization or signed-artifact evidence. If `main` moves, rerun the
preflight before preparing metadata.

Before the `macos-signing` environment can request approval, the reusable
release engine invokes the same `.github/workflows/production-preflight-engine.yml`
implementation again for its exact protected-`main` SHA. The shared job builds
the production package with ad-hoc signing, runs the complete maintained
release-app smoke (including exact embedded worker version and protocol
readiness), creates and installs a production-shaped preventive DMG, and
exercises the shared installed UI accessibility fixture. Failure, cancellation,
or skipped execution prevents the signing job from starting. The later
Developer ID, notarization, signed-DMG, exact-artifact UI, appcast, and
post-publication gates remain mandatory; production preflight moves fixture and
packaged-runtime feedback earlier without substituting for release evidence.

The release runner generates a small synthetic M2TS with the installed app's
FFmpeg before launching XCTest, retaining bounded command diagnostics on failure.
The installed-UI test starts in the queue workspace and opens that fixture through
**Add Sources > Configure Source…**. It waits for source inspection before entering
**Edit Settings…**, saves a new profile, dismisses source configuration, and
checks the update controls. The profile document and media fixture live in the
qualification home. Keep these real UI and persistence checks when navigation
changes; a missing control must fail qualification rather than skip it.
The test scopes the file picker action to its dialog so the duplicate Touch Bar
action cannot make the lookup ambiguous. It dismisses optional permission
prompts for this app during profile and updater checks; local-network permission
and playback still need their separate device qualification.

Normal CI runs the same installed-UI check against its packaged app and retains
the report, screenshots, and failure diagnostics for seven days. This catches
navigation and fixture regressions in a pull request. It does not replace the
separate Production Preflight on the exact protected-main commit.

## Release Preparation

Every release version and Sparkle build number is committed through a normal
pull request before release orchestration runs. First record a successful
`Production Preflight` run for the exact protected `main` SHA that will be the
base of the metadata change; do not consume the successor identity when that
run is absent, failed, or stale. Then use the repository command:

```sh
uv run python -m scripts.release prepare \
  --version <internal-pep440-version> \
  --build <next-global-build>
```

The internal version, public version, tag/title, DMG name, release stage,
Sparkle channel, and publication effects are derived independently by
`scripts/release.py metadata` from the mapping in `release-routes.md`. For
example, internal `0.3.0b11` maps to public `0.3.0-beta.11`, tag/title
`v0.3.0-beta.11`, and DMG
`3D-Blu-ray-to-Vision-Pro-0.3.0-beta.11.dmg`. The numeric
`CFBundleVersion` must increase for every production-identity build across all
routes, including failed unpublished attempts. The command stages a refreshed
`uv.lock`, validates the staged metadata, and updates `pyproject.toml`,
`uv.lock`, and the Xcode Release version/build together only after every check
succeeds. Release metadata operations share an advisory checkout lock. Before
replacement they write a durable transaction journal containing the original
bytes and target digests; a later invocation restores an interrupted prepared
transaction or finalizes an interrupted committed transaction. A lock refresh,
metadata failure, or detected concurrent edit fails closed.

`[tool.uv].required-version` in `pyproject.toml` defines the minimum supported
uv release. `astral-sh/setup-uv` resolves the newest compatible release, while
Dependabot can use its supported updater version. A newer uv release may
canonicalize lock data even when the resolved dependency graph is unchanged;
that normalization must land in a reviewed prerequisite change rather than
during release preparation.

The one-time `0.3.0rc1` build `147` to `0.3.0b3` build `148` correction used
`scripts/release.py recover-beta3` and the exact checked-in evidence in
[v0.3.0-beta.3-recovery.json](release-evidence/v0.3.0-beta.3-recovery.json).
The command has no version, build, stage, or publication override. It rejects
any other source or evidence and rejects a rerun after the target state exists.
Ordinary `prepare` continues to reject RC-to-Beta movement; do not reuse or
generalize the recovery path. The receipt pins repository ID `771225421`, base
commit `e4d89a54412b50b556f51ea3c32034a1dc015eb6`, its source tree and release
file digests, and the exact failed run/attempt/job boundaries. Recovery checks
the authenticated live GitHub, Pages, and PyPI state before staging and again
immediately before committing local metadata.

The initial main-only Sparkle migration used `0.2.143rc4` build `144` and
`0.2.143rc5` build `145` to prove a real RC-to-RC updater path. Stable
`0.2.143` build `146` follows only after that smoke passes. Future releases
must continue increasing `CFBundleVersion` from this sequence.

Review and commit all resulting changes. CI runs
`scripts/release.py validate`, the unit suite, Python package builds, and the
embedded-runtime and native-package smoke. Do not dispatch a release from an unmerged branch
or from a stale main commit.

### Alpha And Beta Change-Scoped Evidence

When an Alpha or Beta candidate's preparation classification exits `2` with
blocking `release_candidate` Tier 2 cases, produce the change-scoped evidence
with the repository command instead of assembling it by hand. Alpha and Beta
use the same command and the same requirements; RC and Stable candidates cannot
use this lane. The command keeps its historical `beta_` module name. From a
clean `qualify/<tag>` worktree whose HEAD is the candidate SHA on `main`, run:

```sh
uv run python -m scripts.beta_change_scoped_evidence
```

The command reruns the preparation classifier for the candidate's own release
stage exactly as the document records it, then the focused Python tests, the
native test suite, and the local ad hoc `scripts/native_app.py package` run with
the production support diagnostics endpoint. It checks every proof in its
committed per-case catalog against the individual passed results and fails
closed on a dirty worktree, a wrong branch, an RC or Stable release, a blocking
case outside the catalog or this lane, or any missing, skipped, or failed proof. On success it writes
`docs/qualification/<tag>-change-scoped-evidence-v1.json` and appends one
receipt per proved case to `docs/qualification/release-evidence-v1.json`.
Command logs stay outside the repository; only their digests are recorded. The
command does not commit or push: review the diff, commit both files on the
qualify branch, and open its pull request against the unchanged candidate SHA.
When the classifier reports no blocking cases, the command exits without
writing anything.

## Release Orchestration

Qualification dispatch, evidence commits, and evidence pushes below run as the
`shiny-code-app[bot]` automation identity, never the personal account. Run the
controller with that App's installation token in `GH_TOKEN`; agents obtain it
from their automation credential helper. Without that identity the controller
stops before any mutation. The separate run-bound signing approval contract
remains unchanged: `macos-signing` approval still uses the active human identity
through `scripts.github_release_run approve`.

> **RC 3 is published and immutable.** Guarded Prerelease run `30990186667`
> published build `160` from source SHA
> `0b06582a83a45bb38d851e62ccf38cd148c7bb95`. Do not rebuild, retag, re-sign,
> replace the artifact. Its targeted qualification is complete: the absent
> issue-link category was explicitly not applicable, and every link present in
> the immutable release notes passed. See [the RC 3 cut packet](0.3.0-rc.3-cut-packet.md).

Dispatch `Stable` from `main` only for reviewed committed Stable metadata, or
dispatch `Prerelease` only for reviewed committed Alpha, Beta, or RC metadata,
after any applicable release-freeze entry has been lifted. The only optional input to
either workflow is release-note text. There is no route or mode override: the
committed project version determines the public release tag/title and DMG name,
Latest behavior, Sparkle channel, and whether PyPI is published. A workflow and
committed-metadata mismatch fails before release construction. The GitHub
Release title is the exact public version tag so narrow release lists keep the
distinguishing version visible.

`.github/workflows/briefcase.yml` is the Stable operator entry and
`.github/workflows/prerelease.yml` is the Prerelease operator entry. Both callers
declare the same repository-wide `release` concurrency group and call the
guarded `.github/workflows/release-engine.yml` reusable workflow, which owns
source and metadata validation, macOS packaging, signing, notarization,
compatibility, attestation, draft creation and verification, cumulative appcast
mutation, GitHub Release publication, Pages deployment, and signing-material
cleanup. The engine intentionally has no `release` concurrency declaration,
preventing a caller and its called workflow from competing with or canceling
the same run. It declares each environment-secret name as an optional
`workflow_call` secret, and each operator caller forwards only those exact names.
The callers do not run in either protected environment, so those expressions are
empty unless a forbidden same-named repository secret exists; the job-level
`macos-signing` and `sparkle-release` environments supply the approved values in
the called workflow. `secrets: inherit` is forbidden. The engine derives the
trusted route from the exact validated caller path and writes the validated
route and publication effects to the run summary.

The workflow must be dispatched and rerun through the configured
`shiny-code-app[bot]` GitHub App identity. The required approver is `cbusillo`, and
the guarded approval helper rejects a run whose actor or triggering actor is the
same account. Verify both run actors and the exact protected-main SHA before
requesting approval. The reusable engine independently requires both run actors
to be `shiny-code-app[bot]` before release work begins.

Published receipts and archived evidence from the former `shiny-code-bot`
identity remain valid and immutable. Evidence reconciliation requires the live
run's actor and triggering actor to match the actor recorded in its receipt.
Historical evidence compatibility does not authorize the former bot to dispatch
or approve a new release; the signing approver remains `cbusillo`.

Generated notes use production-stage-aware history. An Alpha, Beta, or RC
compares with the newest lower published production release whose tag is an
ancestor of the release commit, keeping prerelease notes incremental. A Stable
release compares with the newest lower published production Stable tag rather
than the latest prerelease, so its notes summarize the complete change set since
the previous Stable. The retired preview tags are excluded before parsing or
history selection. Stable-form tags through `v0.2.139`, plus the pulled
`v0.2.141`, retain their historical GitHub prerelease classification; all other
production tag and prerelease-flag mismatches fail closed.

GitHub requests one maintainer approval when the run reaches the
`macos-signing` environment. That approval authorizes the release intent for the
specific run. The branch-restricted `sparkle-release`, `pypi`, and
`github-pages` environments keep their separate secret and permission scopes,
but do not request additional reviews; their jobs run only after the preceding
verification boundaries succeed.

### Release Run Monitoring

Do not use a generic Actions waiter as the sole release monitor. It can report a
reviewer-gated job as merely `waiting`, which hides the only human authorization
boundary protecting the Apple signing credentials. Record the dispatched run ID
and exact protected-main SHA, then use the repository helper:

```sh
uv run python -m scripts.github_release_run watch \
  --run-id "$RUN_ID" \
  --workflow "$WORKFLOW" \
  --head-sha "$MAIN_SHA"
```

Set `WORKFLOW` to exactly `Stable` or `Prerelease`; retired workflow names are
rejected.

The helper validates the repository, workflow, event, branch, and full commit
SHA on every poll. It also checks that protected `main` has not moved. Exit code
`20` emits an `approval_required` JSON event immediately after querying GitHub's
pending deployments, including a fingerprint bound to the repository, run,
exact operator workflow path and ID, reusable engine path, both workflow refs
and definition SHAs, validated route, run attempt, commit, actors, environment
ID, and reviewer. The workflow ID is the positive value returned by GitHub for
the run rather than a locally invented identifier.
The fingerprint is a non-secret identity checksum, not evidence of
human authorization. Obtain explicit maintainer authorization in the active
conversation, then approve through the guarded command rather than a raw API
call:

```sh
uv run python -m scripts.github_release_run approve \
  --run-id "$RUN_ID" \
  --workflow "$WORKFLOW" \
  --head-sha "$MAIN_SHA" \
  --confirm-sha "$MAIN_SHA" \
  --approval-fingerprint "$APPROVAL_FINGERPRINT" \
  --comment "Approved after explicit release authorization for $MAIN_SHA."
```

Approval removes bot-token environment variables, verifies the active local
GitHub login, requires that login to be the configured reviewer, rechecks the
exact run and current `main`, and approves only the expected `macos-signing`
deployment. Run the watcher again after approval and keep other pull requests
unmerged until the release reaches a terminal state. Exit code `21` is a safety
failure such as source movement or identity drift; stop or cancel the run rather
than retrying blindly.

The workflow performs these ordered boundaries:

1. Prove `github.sha` is the current protected `main` HEAD and validate the
   committed version, build counter, and `uv.lock`.
2. Reject a conflicting tag, release, Sparkle version/build, or Stable PyPI
   version while allowing a matching draft to resume. The active Pages state
   and newest durable snapshot are both checked.
3. Run the secret-free `qualify-preparation` gate. Classify the candidate
   against the checked `docs/qualification/release-evidence-v1.json` evidence
   and the exact `qualificationRecordPath` selected in `.github/github.json`
   for the `preparation` phase, using the exact `github.sha` and committed
   Sparkle channel as the release stage. `scripts.release validate` requires
   that path to identify the unique `*-signed-qualification-v1.json` record for
   the committed tag, version, build, DMG, workflow, and release stage. The
   preparation report is uploaded as an Actions artifact with 30-day retention
   before enforcement. macOS signing approval cannot be requested until this
   gate passes.
4. After the single release approval, build, sign, notarize, and
   Gatekeeper-validate the SwiftUI macOS app and DMG without a write-capable
   repository token. Record its exact name, byte size, SHA-256, and
   `SHA256SUMS` entry, then publish GitHub artifact attestations for the verified
   package before release creation.
5. Download that exact notarized DMG on the separate macOS 26 runner and repeat
   checksum, signature, Gatekeeper, startup, bundled-tool, and worker validation.
   Draft creation cannot begin unless this compatibility boundary passes.
6. Create a draft GitHub Release targeting only `github.sha`, retain its release
   ID for authenticated inspection, freeze the exact UTF-8 release body into a
   digest-bound workflow artifact, and transfer draft assets through release and
   asset IDs rather than runner-dependent tag lookup. Asset overwrite stays
   disabled by default.
7. In the main-only `sparkle-release` environment, download the verified
   package and release-note workflow artifacts without a write-capable
   repository token, verify their exact identities, load the active durable
   `appcast.xml` selected by Pages state, sign the DMG, and build the cumulative
   snapshot. New items embed the frozen body as
   `<description sparkle:format="markdown">` and retain the GitHub Release page
   as their full-notes link; historical tag-page items remain valid.
8. Upload `appcast.xml` to the draft, re-download the DMG, checksum, and appcast,
   and repeat the exact digest, size, notarization, Gatekeeper, bundle-version,
   embedded-release-note, appcast-item, and exact-main-commit GitHub provenance
   checks.
9. Build a deterministic public-safe `release-receipt.json` from those verified
   outputs. The receipt binds the route, workflow run and attempt, protected
   source SHA, versions, release ID, release asset IDs/sizes/digests, signed app
   tree, appcast, verification outcomes, and Tier 1 case references. Upload it
   to the draft, re-download it by asset ID, and verify its content and file
   digests before publication. The receipt deliberately excludes credentials,
   approval context, private paths, and diagnostic identifiers.
10. Run the secret-free signed-artifact UI and `qualify-artifact` gates. Transfer
    the already verified package and release receipt through same-run Actions
    artifacts by immutable artifact ID, then verify their externally supplied
    SHA-256 digests. The receipt preserves the original GitHub Release asset IDs
    and digests. Test the installed signed DMG, then classify the candidate for
    the `artifact` phase, binding the exact release route, workflow run ID and
    attempt, release ID, committed versions/tag/DMG name, signed app tree digest,
    and DMG/checksum/appcast asset IDs and digests. The artifact report is
    uploaded as an Actions artifact before enforcement. Publication cannot
    proceed until both gates pass. The classifier still lists cases tied to the
    live appcast; the `update-test` job below is what now tests the update path.
    Operator-only physical and native-window presentation evidence is reported
    separately and cannot bypass or satisfy these automated gates.
    The `update-test` job (`.github/workflows/update-test.yml`) also runs before
    publication on every channel. It installs the newest lower published
    release, points it at a locally served copy of the signed appcast, lets
    Sparkle install the candidate, and confirms the relaunched app's identity
    and saved profiles. It fails if Sparkle never fetched the local feed.
11. Publish the verified draft only if it still targets the current `main` HEAD.
   The release body is hashed again immediately before and after publication so
   edits cannot silently diverge from the updater notes. The reusable engine
   returns Stable Python distributions only as an immutable workflow artifact
   ID and GitHub-recorded digest containing an exact `SHA256SUMS` manifest; RC
   and other prerelease routes return no Python artifact. Stable releases then
   publish that verified artifact through PyPI Trusted Publishing with PEP 740
   attestations; Alpha, Beta, and RC releases never publish to PyPI.
12. Deploy the durable `appcast.xml` release asset to GitHub Pages. A deployment
   failure can be retried without rebuilding, retagging, or re-signing.
13. After the complete reusable engine succeeds, the Stable operator
    revalidates the current protected-main SHA, operator/engine policy evidence,
    GitHub artifact digest, and every distribution checksum, then publishes
    through the pinned PyPI Trusted Publisher with PEP 740 attestations. The publisher remains in
    `briefcase.yml` and the `pypi` environment so its existing OIDC identity does
    not change.
14. Nothing is committed back to `main` after publication. The release receipt
   stays attached to the GitHub Release, the update test (step 10) ran before
   publication, and step 11 and the Pages deployment check the public DMG and
   live appcast.
15. The separate `cbusillo/homebrew-tap` repository checks the latest stable
   GitHub Release on a schedule and by manual dispatch. Homebrew opens a formula
   update pull request when the version changes; tap CI must pass formula audit,
   source installation, command tests, and linkage checks before merge.
   Prereleases do not update the formula.
16. After a Mac Beta is published, point the visionOS player's TestFlight
   description and review notes at it
   ([procedure](visionos-player.md#point-the-testflight-text-at-each-new-mac-beta)).

For Stable releases, PyPI publication and the Homebrew tap update remain
independent post-publication operations. PyPI starts only after the reusable
engine, including Sparkle Pages deployment, succeeds; a failed PyPI job can be
retried without rebuilding or changing the published GitHub Release. The tap
uses its own repository token and requires no cross-repository release secret.

Release bodies are also the updater's native Markdown source. Keep the opening
paragraph useful as the version summary and prefer headings, lists, links,
emphasis, block quotes, and code. Avoid relying on GitHub-only tables, images,
or embedded HTML for information required in the Sparkle dialog.

### Production macOS Application

The accepted SwiftUI/AppKit interface is packaged by the reusable
`.github/workflows/release-engine.yml` production engine, called by the Stable
`.github/workflows/briefcase.yml` operator entry. The repository-owned runtime
builder stages the embedded Python engine; the workflow filename remains the
Stable/PyPI identity. The Xcode `Release` configuration owns the production name, bundle
identifier, macOS 26 deployment target, and Sparkle metadata.

The signing job runs on GitHub's Apple-Silicon `xcode-27` image, selects the Xcode
pinned by `XCODE_VERSION` and `XCODE_BUILD_VERSION` in the workflow, and installs XcodeGen 2.45.4 from its digest-pinned release
artifact. It uses the reviewed `macos-signing` environment, an ephemeral
keychain, Developer ID signing, and notarization for both the app and DMG. The
artifact must then pass a separate fresh-runner macOS 26 compatibility job
before the existing production draft, appcast, PyPI, Pages, and publication
boundaries can proceed.

Stable items remain unchanneled. RC, Beta, and Alpha items use `rc`, `beta`, and
`alpha` respectively. The application supplies cumulative allowed-channel sets
for Stable, RC, Beta, and Alpha exactly as defined in `release-routes.md`, while
Sparkle implicitly includes default Stable items for every route. Moving to a
safer route affects only future newer builds and never downgrades the installed
application.

Published `v0.3.0-beta.3` (`0.3.0b3`, build `148`) is appended to the cumulative
feed on channel `beta`, but currently shipped Stable and RC clients
cannot select that channel. It is therefore a manual-download seed, not a
Sparkle-discoverable update from those installations. Testers download the exact
GitHub Release DMG, replace the production
`com.shinycomputers.bd-to-avp` app in `/Applications`, and then explicitly
choose Beta or Alpha for future prereleases. The installation exposes all four
routes, while the Beta 3 item remains excluded from Stable and RC.

The retired side-by-side feedback releases remain immutable historical
evidence. Their tags include `native-ui-preview-1`, `v0.3.0-beta.1`, and
`v0.3.0-beta.2`. Do not replace those assets, repurpose their bundle identifier,
or add them to the production appcast. Those retired Preview identities cannot
Sparkle-upgrade into Beta 3.

The cumulative `appcast.xml` attached to every published GitHub Release is the
recovery source of truth, including the publication-time Markdown shown in the
updater. Pages also publishes `appcast-state.json`, which binds the live
feed to one durable release snapshot or records that updates are disabled.
GitHub Pages is a deployment target, not the only copy of feed history.

## Retry, Restore, and Disable

If a release run fails before publication, leave the release as a draft while
diagnosing it, then rerun the failed jobs or dispatch the same committed release
again. A matching draft and its byte-identical assets resume safely; a
conflicting draft or tag fails closed. Never replace a published DMG or appcast
asset. When the failure requires a workflow-definition fix or changes an
attempt-bound receipt, preserve the failed-run evidence, merge the fix, delete
only the abandoned unpublished draft after recording its identities, and start a
fresh release from the new protected-main SHA. If the Pages job fails after publication, rerun the failed job or dispatch
`Manage Sparkle Pages` from `main` with `deploy` and the release tag.

Stable `0.3.0` had one bounded post-publication PyPI recovery. Run
`31219050718` published the immutable GitHub release and Sparkle feed, then
failed before PyPI trusted publishing because its checksum manifest included a
generated `dist/.gitignore` that the artifact uploader omitted. Recovery
[run `31238358554`](https://github.com/cbusillo/BD_to_AVP/actions/runs/31238358554)
succeeded, and the [checked publication record](release-evidence/v0.3.0/publication-record.json)
preserves it separately from the original failed release run. On 2026-10-02 UTC,
the published wheel and source archive were downloaded and matched the original
sizes and hashes in `docs/release-evidence/v0.3.0-pypi-recovery.json`.

The original Actions artifact's recorded expiry was 2026-08-14 and its API
record is now unavailable. The one-time path is historical, not a current
dispatch procedure. Do not rebuild or republish this historical release.
The [Stable cut packet](0.3.0-cut-packet.md#post-publication-pypi-recovery)
preserves the original contract. The obsolete Stable dispatch input/job and
workflow-run recovery routing were removed in [#866](https://github.com/cbusillo/BD_to_AVP/issues/866).
Normal Stable publication still runs through the guarded release engine.
Immutable recovery, publication and qualification evidence remains preserved.
Treat the frozen `remove_after_verified_recovery` flag as historical. The helper
retains `load_evidence`/`verify-pypi`, and `scripts.release_evidence` retains the
`--recovery-workflow-run` CLI option for offline historical validation and
reconciliation; it is no longer invoked by the release-evidence workflow.

For the Beta 3 seed, a pre-publication failure leaves the existing feed and all
published assets unchanged: retain the matching draft for an exact retry or stop
the release before publication. After publication, do not upload a replacement
DMG or appcast asset. If the feed must be withdrawn for severity, use the Pages
`disable` operation; use `restore` with a last-good published release tag when
updates may resume. Correct an application defect with a newer build, never by
asking Sparkle to downgrade an installed app.

The draft release body becomes immutable for that run once the appcast is
constructed. Editing it afterward causes verification and publication to fail
closed because the embedded Markdown no longer matches the recorded digest.

Drafts are never deleted automatically because they preserve exact-commit
diagnostic and retry evidence. If a newer immutable release supersedes a failed
draft and no exact-commit retry remains useful, verify the published tag and
assets, then delete only the abandoned draft through the GitHub Releases UI.
Maintainers may otherwise see that draft pinned above newer published releases.
An intentionally cancelled signed attempt uses an immutable
`cancelled-attempt-v1.json` record while its draft remains present. That record
must bind the checked receipt, all draft asset IDs and digests, the cancellation
boundary, and the run-bound signing approval. Freeze the release tag immediately.
A later deletion requires fresh explicit authorization and a separate immutable
`draft-deletion-v1.json` disposition record. The disposition must bind the
cancelled-attempt file digest, checked receipt digest, draft release ID, source
SHA, authorization actor and canonical fingerprint, deletion actor and time, and
subsequent absence of both draft and tag. Never rewrite the cancelled-attempt
record to claim that the draft was deleted.

To restore an earlier last-good cumulative feed, dispatch `Manage Sparkle Pages`
from `main` with `restore` and the selected published release tag. The workflow
downloads and validates that release's `appcast.xml` asset before deployment.

For an emergency stop, dispatch the same workflow with `disable` and no tag.
Emergency disable preempts an in-flight Pages deployment and deploys the
committed valid empty feed plus a durable public disabled-state marker. Release
orchestration and normal deploy operations fail closed while that marker is
active. It does not edit or delete any GitHub Release, release asset, or
cumulative snapshot. Restore the last-good tag when updates may resume.

## Required Repository Settings

Keep the live repository settings aligned with these contracts:

- `macos-signing` is limited to `main`, contains only the Apple certificate,
  identity, notarization, and keychain secrets, and is the sole reviewed
  environment in normal release orchestration. Self-review is prevented and
  administrators cannot bypass the protection rule. The legacy
  `KEYCHAIN_PASSWORD` value is the
  Apple app-specific password; the workflow generates a separate ephemeral build
  keychain password for every run and derives the notarization profile
  name from `TEAM_ID`, so no `KEYCHAIN_NAME` secret is required.
- Repository-level secrets must not reuse any name declared by `macos-signing`
  or `sparkle-release`; exact caller mappings exist only to satisfy the reusable
  workflow interface, and a broader-scope duplicate could become an unintended
  fallback if an environment secret were removed.
- `sparkle-release` is limited to `main`, contains only
  `SPARKLE_EDDSA_PRIVATE_KEY`, and has no separate required-review rule. The
  private key remains visible only to the read-only signing step.
- `sparkle-feed-ops` is limited to `main`, contains no secrets, and requires a
  maintainer review only for manually dispatched deploy, restore, or disable
  operations. It is not part of normal release orchestration.
- `pypi` is limited to `main`, has no required-review rule, and is authorized by
  the PyPI Trusted Publisher for repository `cbusillo/BD_to_AVP`, workflow
  `briefcase.yml`, environment `pypi`, and project `bd_to_avp`. No
  `PYPI_TOKEN` exists. The publisher job deliberately remains in the operator
  workflow because PyPI does not accept a reusable workflow as the configured
  publisher workflow; no trusted-publisher migration is required by the engine
  extraction.
- `github-pages` is Actions-managed, limited to `main`, and has no additional
  required-review rule.
- Immutable GitHub Releases remain enabled; drafts are resumable while
  published tags and assets are immutable.
- The retired long-lived `release` branch and its ruleset remain absent.

GitHub does not expose existing secret values. Repository-setting reviews must
verify secret names, environment scopes, branch policies, and reviewer rules
without attempting to read secret contents.
