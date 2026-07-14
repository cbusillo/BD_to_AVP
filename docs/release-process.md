# Main-Only Release Process

## Release Preparation

Every release version and Sparkle build number is committed through a normal
pull request before release orchestration runs. Use the repository command:

```sh
uv run python scripts/release.py prepare \
  --version 0.2.143 \
  --build 146
```

The version must be a canonical three-part stable version or release candidate,
such as `0.2.144` or `0.2.144rc1`. The numeric `CFBundleVersion` must increase
for every RC and Stable DMG. The command stages a refreshed `uv.lock`, validates
the staged metadata, and updates `pyproject.toml` and `uv.lock` together only
after every check succeeds. A lock refresh failure leaves both files unchanged.

The initial main-only Sparkle migration used `0.2.143rc4` build `144` and
`0.2.143rc5` build `145` to prove a real RC-to-RC updater path. Stable
`0.2.143` build `146` follows only after that smoke passes. Future releases
must continue increasing `CFBundleVersion` from this sequence.

Review and commit all resulting changes. CI runs
`scripts/release.py validate`, the unit suite, Python package builds, and the
Briefcase create/build smoke. Do not dispatch a release from an unmerged branch
or from a stale main commit.

## Release Orchestration

Dispatch `Release from protected main` from `main`. The only optional input is
release-note text; the committed project version determines RC versus Stable,
the release tag, latest-release behavior, Sparkle channel, and whether PyPI is
published. The GitHub Release title is the exact version tag so narrow release
lists keep the distinguishing version visible.

Generated notes use channel-aware history. An RC compares with the newest lower
published release whose tag is an ancestor of the release commit, keeping RC
notes incremental. A Stable release compares with the newest lower published
Stable tag rather than the latest RC, so its notes summarize the complete change
set since the previous Stable. Stable history is a product-version boundary and
may cross the retired legacy release branch; RC history remains ancestry-bound.

GitHub requests one maintainer approval when the run reaches the
`macos-signing` environment. That approval authorizes the release intent for the
specific run. The branch-restricted `sparkle-release`, `pypi`, and
`github-pages` environments keep their separate secret and permission scopes,
but do not request additional reviews; their jobs run only after the preceding
verification boundaries succeed.

The workflow performs these ordered boundaries:

1. Prove `github.sha` is the current protected `main` HEAD and validate the
   committed version, build counter, and `uv.lock`.
2. Reject a conflicting tag, release, Sparkle version/build, or Stable PyPI
   version while allowing a matching draft to resume. The active Pages state
   and newest durable snapshot are both checked.
3. After the single release approval, build, sign, notarize, and
   Gatekeeper-validate the macOS DMG without a write-capable repository token.
   Normalize its release filename to use hyphens instead of spaces, record its
   exact name, byte size, SHA-256, and `SHA256SUMS` entry, then publish GitHub
   artifact attestations for the verified package before release creation.
4. Create a draft GitHub Release targeting only `github.sha`, retain its release
   ID for authenticated inspection, freeze the exact UTF-8 release body into a
   digest-bound workflow artifact, and transfer draft assets through release and
   asset IDs rather than runner-dependent tag lookup. Asset overwrite stays
   disabled by default.
5. In the main-only `sparkle-release` environment, download the verified
   package and release-note workflow artifacts without a write-capable
   repository token, verify their exact identities, load the active durable
   `appcast.xml` selected by Pages state, sign the DMG, and build the cumulative
   snapshot. New items embed the frozen body as
   `<description sparkle:format="markdown">` and retain the GitHub Release page
   as their full-notes link; historical tag-page items remain valid.
6. Upload `appcast.xml` to the draft, re-download the DMG, checksum, and appcast,
   and repeat the exact digest, size, notarization, Gatekeeper, bundle-version,
   embedded-release-note, appcast-item, and exact-main-commit GitHub provenance
   checks.
7. Publish the verified draft only if it still targets the current `main` HEAD.
   The release body is hashed again immediately before and after publication so
   edits cannot silently diverge from the updater notes. Stable releases then
   publish separately built Python distributions through PyPI Trusted
   Publishing with PEP 740 attestations; RC releases never publish to PyPI.
8. Deploy the durable `appcast.xml` release asset to GitHub Pages. A deployment
   failure can be retried without rebuilding, retagging, or re-signing.

For Stable releases, PyPI publication and Sparkle Pages deployment are
independent post-publication jobs. Either channel can be retried without
rebuilding or changing the published GitHub Release.

Release bodies are also the updater's native Markdown source. Keep the opening
paragraph useful as the version summary and prefer headings, lists, links,
emphasis, block quotes, and code. Avoid relying on GitHub-only tables, images,
or embedded HTML for information required in the Sparkle dialog.

### Native UI Preview

The native UI feedback release lane uses
`.github/workflows/native-ui-preview.yml`, not the production Briefcase release
workflow. Dispatch it only from the current protected `main` commit after CI is
green and an ephemeral Apple Silicon runner labeled `bd-to-avp-release` is ready
with macOS 27, Xcode 27, and XcodeGen 2.45.4. The builder uses the committed
macOS 26 deployment target; publication is additionally gated by verification
and packaged-worker execution on a separate macOS 26 runner.

The workflow uses the existing reviewed `macos-signing` environment to sign and
notarize the side-by-side Preview bundle, creates and revalidates a DMG, and
derives its semantic prerelease tag, version-first release title, app name, and
DMG filename from committed native metadata. Historical build `1` keeps tag
`native-ui-preview-1` and is titled `v0.3.0-alpha.1`. Build `2` uses
`v0.3.0-beta.1` for both tag and title.
Native build numbers are monotonically increasing and may not be reused. The
workflow never derives metadata from `pyproject.toml`, invokes the
Sparkle/Pages workflows, publishes Python distributions, or marks a preview as
GitHub latest. A failed run may resume only an exact matching draft. Published
tags and assets are immutable; maintainers may correct the human-readable title
or notes without replacing artifacts or changing the target commit.

The cumulative `appcast.xml` attached to every published GitHub Release is the
recovery source of truth, including the publication-time Markdown shown in the
native updater. Pages also publishes `appcast-state.json`, which binds the live
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
  identity, notarization, and keychain secrets, and is the sole environment
  with a required maintainer review. The legacy `KEYCHAIN_PASSWORD` value is
  the Apple app-specific password; the workflow generates a separate ephemeral
  build keychain password for every run and derives Briefcase's notarization
  profile name from `TEAM_ID`, so no `KEYCHAIN_NAME` secret is required.
- `sparkle-release` is limited to `main`, contains only
  `SPARKLE_EDDSA_PRIVATE_KEY`, and has no separate required-review rule. The
  private key remains visible only to the read-only signing step.
- `sparkle-feed-ops` is limited to `main`, contains no secrets, and requires a
  maintainer review only for manually dispatched deploy, restore, or disable
  operations. It is not part of normal release orchestration.
- `pypi` is limited to `main`, has no required-review rule, and is authorized by
  the PyPI Trusted Publisher for repository `cbusillo/BD_to_AVP`, workflow
  `briefcase.yml`, environment `pypi`, and project `bd_to_avp`. No
  `PYPI_TOKEN` exists.
- `github-pages` is Actions-managed, limited to `main`, and has no additional
  required-review rule.
- Immutable GitHub Releases remain enabled; drafts are resumable while
  published tags and assets are immutable.
- The retired long-lived `release` branch and its ruleset remain absent.

GitHub does not expose existing secret values. Repository-setting reviews must
verify secret names, environment scopes, branch policies, and reviewer rules
without attempting to read secret contents.
