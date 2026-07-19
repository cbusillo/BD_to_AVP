# Protected-Main Release Process

The normative production identity, version mapping, update routes, history
boundary, and publication policy are defined in
[Production Release Routes](release-routes.md).

The next planned production-identity field build is `v0.3.0-beta.3`, internal
version `0.3.0b3`, build `148`. The committed `0.3.0rc1` build `147` attempt
failed before publication and its build number is permanently burned. The
focused recovery and Beta 3 cut packet are owned by issue #293.

Issue #289 implements the four-route updater preference, release metadata,
production-history filtering, and appcast validation described here. The
guarded Stable/Prerelease entrypoints and Beta 3 recovery/bootstrap remain owned
by issues #290, #291, #292, and #293, so Alpha and Beta preparation and dispatch
stay blocked until those gates and the focused signed-install smoke have landed.

## Release Preparation

Every release version and Sparkle build number is committed through a normal
pull request before release orchestration runs. Use the repository command:

```sh
uv run python scripts/release.py prepare \
  --version <internal-pep440-version> \
  --build <next-global-build>
```

The internal version, public version, tag/title, DMG name, release stage,
Sparkle channel, and publication effects are derived independently by
`scripts/release.py metadata` from the mapping in `release-routes.md`. For
example, internal `0.3.0b3` maps to public `0.3.0-beta.3`, tag/title
`v0.3.0-beta.3`, and DMG
`3D-Blu-ray-to-Vision-Pro-0.3.0-beta.3.dmg`. The numeric
`CFBundleVersion` must increase for every production-identity build across all
routes, including failed unpublished attempts. The command stages a refreshed
`uv.lock`, validates the staged metadata, and updates `pyproject.toml`,
`uv.lock`, and the Xcode Release version/build together only after every check
succeeds. A lock refresh or metadata failure leaves all three files unchanged.

The initial main-only Sparkle migration used `0.2.143rc4` build `144` and
`0.2.143rc5` build `145` to prove a real RC-to-RC updater path. Stable
`0.2.143` build `146` follows only after that smoke passes. Future releases
must continue increasing `CFBundleVersion` from this sequence.

Review and commit all resulting changes. CI runs
`scripts/release.py validate`, the unit suite, Python package builds, and the
Briefcase create/build smoke. Do not dispatch a release from an unmerged branch
or from a stale main commit.

## Release Orchestration

> **Release dispatch is frozen.** Protected `main` still contains the burned
> `0.3.0rc1` build `147` metadata. Do not dispatch `Release from protected main`
> or any replacement release entry until #289 through #293 have landed and
> `scripts/release.py metadata` reports the reviewed `0.3.0b3` build `148`
> target. Publishing build `147` is prohibited.

Dispatch `Release from protected main` from `main` only when the release freeze
above has been lifted. The only optional input is release-note text; the
committed project version determines Alpha, Beta, RC, or Stable metadata, the
separate public release tag/title and DMG name, latest-release behavior, Sparkle
channel, and whether PyPI is published. The GitHub Release title is the exact
public version tag so narrow release lists keep the distinguishing version
visible.

`.github/workflows/briefcase.yml` is the Stable operator entry and the sole
owner of the repository-wide `release` concurrency group. It calls the guarded
`.github/workflows/release-engine.yml` reusable workflow, which owns source and
metadata validation, macOS packaging, signing, notarization, compatibility,
attestation, draft creation and verification, cumulative appcast mutation,
GitHub Release publication, Pages deployment, and signing-material cleanup.
The engine intentionally has no `release` concurrency declaration, preventing
the caller and called workflow from competing with or canceling the same run.

The workflow must be dispatched and rerun through the configured
`shiny-code-bot` automation identity. The required approver is `cbusillo`, and
the guarded approval helper rejects a run whose actor or triggering actor is the
same account. Verify both run actors and the exact protected-main SHA before
requesting approval. The reusable engine independently requires both run actors
to be `shiny-code-bot` before release work begins.

Generated notes use production-stage-aware history. An Alpha, Beta, or RC
compares with the newest lower published production release whose tag is an
ancestor of the release commit, keeping prerelease notes incremental. A Stable
release compares with the newest lower published production Stable tag rather
than the latest prerelease, so its notes summarize the complete change set since
the previous Stable. The retired preview tags are excluded before parsing or
history selection.

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
  --workflow "Release from protected main" \
  --head-sha "$MAIN_SHA"
```

The helper validates the repository, workflow, event, branch, and full commit
SHA on every poll. It also checks that protected `main` has not moved. Exit code
`20` emits an `approval_required` JSON event immediately after querying GitHub's
pending deployments, including a fingerprint bound to the repository, run,
exact operator workflow path and ID, reusable engine path, both workflow refs
and definition SHAs, run attempt, commit, actors, environment ID, and reviewer.
The fingerprint is a non-secret identity checksum, not evidence of
human authorization. Obtain explicit maintainer authorization in the active
conversation, then approve through the guarded command rather than a raw API
call:

```sh
uv run python -m scripts.github_release_run approve \
  --run-id "$RUN_ID" \
  --workflow "Release from protected main" \
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
3. After the single release approval, build, sign, notarize, and
   Gatekeeper-validate the SwiftUI macOS app and DMG without a write-capable
   repository token. Record its exact name, byte size, SHA-256, and
   `SHA256SUMS` entry, then publish GitHub artifact attestations for the verified
   package before release creation.
4. Download that exact notarized DMG on the separate macOS 26 runner and repeat
   checksum, signature, Gatekeeper, startup, bundled-tool, and worker validation.
   Draft creation cannot begin unless this compatibility boundary passes.
5. Create a draft GitHub Release targeting only `github.sha`, retain its release
   ID for authenticated inspection, freeze the exact UTF-8 release body into a
   digest-bound workflow artifact, and transfer draft assets through release and
   asset IDs rather than runner-dependent tag lookup. Asset overwrite stays
   disabled by default.
6. In the main-only `sparkle-release` environment, download the verified
   package and release-note workflow artifacts without a write-capable
   repository token, verify their exact identities, load the active durable
   `appcast.xml` selected by Pages state, sign the DMG, and build the cumulative
   snapshot. New items embed the frozen body as
   `<description sparkle:format="markdown">` and retain the GitHub Release page
   as their full-notes link; historical tag-page items remain valid.
7. Upload `appcast.xml` to the draft, re-download the DMG, checksum, and appcast,
   and repeat the exact digest, size, notarization, Gatekeeper, bundle-version,
   embedded-release-note, appcast-item, and exact-main-commit GitHub provenance
   checks.
8. Publish the verified draft only if it still targets the current `main` HEAD.
   The release body is hashed again immediately before and after publication so
   edits cannot silently diverge from the updater notes. The reusable engine
   returns Stable Python distributions only as an immutable workflow artifact
   ID and GitHub-recorded digest containing an exact `SHA256SUMS` manifest; RC
   and other prerelease routes return no Python artifact. Stable releases then
   publish that verified artifact through PyPI Trusted Publishing with PEP 740
   attestations; Alpha, Beta, and RC releases never publish to PyPI.
9. Deploy the durable `appcast.xml` release asset to GitHub Pages. A deployment
   failure can be retried without rebuilding, retagging, or re-signing.
10. After the complete reusable engine succeeds, the Stable operator
    revalidates the current protected-main SHA, operator/engine policy evidence,
    GitHub artifact digest, and every distribution checksum, then publishes
    through the pinned PyPI Trusted Publisher with PEP 740 attestations. The publisher remains in
    `briefcase.yml` and the `pypi` environment so its existing OIDC identity does
    not change.
11. The separate `cbusillo/homebrew-tap` repository checks the latest stable
   GitHub Release on a schedule and by manual dispatch. Homebrew opens a formula
   update pull request when the version changes; tap CI must pass formula audit,
   source installation, command tests, and linkage checks before merge.
   Prereleases do not update the formula.

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
`.github/workflows/briefcase.yml` operator entry. Briefcase remains the staging
mechanism for the embedded Python engine, but its Python GUI is not the shipping
interface. The Xcode `Release` configuration owns the production name, bundle
identifier, macOS 26 deployment target, and Sparkle metadata.

The signing job runs on GitHub's Apple-Silicon `macos-26` image, selects Xcode
26.5 build `17F42`, and installs XcodeGen 2.45.4 from its digest-pinned release
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

Beta 3 is appended to the cumulative feed on channel `beta`, but currently
shipped clients cannot select that channel. Testers bootstrap it by manual
GitHub Release download, then explicitly choose Beta or Alpha for future
prereleases.

The retired side-by-side feedback releases remain immutable historical
evidence. Their tags include `native-ui-preview-1`, `v0.3.0-beta.1`, and
`v0.3.0-beta.2`. Do not replace those assets, repurpose their bundle identifier,
or add them to the production appcast.

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
asset. If the Pages job fails after publication, rerun the failed job or dispatch
`Manage Sparkle Pages` from `main` with `deploy` and the release tag.

The draft release body becomes immutable for that run once the appcast is
constructed. Editing it afterward causes verification and publication to fail
closed because the embedded Markdown no longer matches the recorded digest.

Drafts are never deleted automatically because they preserve exact-commit
diagnostic and retry evidence. If a newer immutable release supersedes a failed
draft and no exact-commit retry remains useful, verify the published tag and
assets, then delete only the abandoned draft through the GitHub Releases UI.
Maintainers may otherwise see that draft pinned above newer published releases.

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
