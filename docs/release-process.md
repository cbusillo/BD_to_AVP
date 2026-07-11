# Main-Only Release Process

## Release Preparation

Every release version and Sparkle build number is committed through a normal
pull request before release orchestration runs. Use the repository command:

```sh
uv run python scripts/release.py prepare \
  --version 0.2.143rc5 \
  --build 145
```

The version must be a canonical three-part stable version or release candidate,
such as `0.2.144` or `0.2.144rc1`. The numeric `CFBundleVersion` must increase
for every RC and Stable DMG. The command stages a refreshed `uv.lock`, validates
the staged metadata, and updates `pyproject.toml` and `uv.lock` together only
after every check succeeds. A lock refresh failure leaves both files unchanged.

The main-only migration itself is prepared as `0.2.143rc4` with build `144`.
After that RC is published, prepare `0.2.143rc5` with build `145` for the real
RC-to-RC updater smoke. Prepare Stable `0.2.143` with build `146` only after the
smoke passes.

Review and commit all resulting changes. CI runs
`scripts/release.py validate`, the unit suite, Python package builds, and the
Briefcase create/build smoke. Do not dispatch a release from an unmerged branch
or from a stale main commit.

## Release Orchestration

Dispatch `Release from protected main` from `main`. The only optional input is
release-note text; the committed project version determines RC versus Stable,
the release tag, latest-release behavior, Sparkle channel, and whether PyPI is
published.

The workflow performs these ordered boundaries:

1. Prove `github.sha` is the current protected `main` HEAD and validate the
   committed version, build counter, and `uv.lock`.
2. Reject a conflicting tag, release, Sparkle version/build, or Stable PyPI
   version while allowing a matching draft to resume. The active Pages state
   and newest durable snapshot are both checked.
3. Build, sign, notarize, and Gatekeeper-validate the macOS DMG without a
   write-capable repository token. Record its exact name, byte size, SHA-256,
   and `SHA256SUMS` entry, then publish GitHub artifact attestations for the
   verified package before release creation.
4. Create a draft GitHub Release targeting only `github.sha`, retain its release
   ID for authenticated draft inspection, and upload the package assets with
   overwrite disabled by default.
5. In the protected `sparkle-release` environment, re-download the draft DMG,
   verify its exact identity and distribution signatures, load the active
   durable `appcast.xml` selected by Pages state, sign the DMG, verify the EdDSA
   signature, and build the cumulative snapshot.
6. Upload `appcast.xml` to the draft, re-download the DMG, checksum, and appcast,
   and repeat the exact digest, size, notarization, Gatekeeper, bundle-version,
   appcast-item, and exact-main-commit GitHub provenance checks.
7. Publish the verified draft only if it still targets the current `main` HEAD.
   Stable releases then publish separately built Python distributions through
   PyPI Trusted Publishing with PEP 740 attestations; RC releases never publish
   to PyPI.
8. Deploy the durable `appcast.xml` release asset to GitHub Pages. A deployment
   failure can be retried without rebuilding, retagging, or re-signing.

For Stable releases, PyPI publication and Sparkle Pages deployment are
independent post-publication jobs. Either channel can be retried without
rebuilding or changing the published GitHub Release.

The cumulative `appcast.xml` attached to every published GitHub Release is the
recovery source of truth. Pages also publishes `appcast-state.json`, which binds
the live feed to one durable release snapshot or records that updates are
disabled. GitHub Pages is a deployment target, not the only copy of feed
history.

## Retry, Restore, and Disable

If a release run fails before publication, leave the release as a draft while
diagnosing it, then rerun the failed jobs or dispatch the same committed release
again. A matching draft and its byte-identical assets resume safely; a
conflicting draft or tag fails closed. Never replace a published DMG or appcast
asset. If the Pages job fails after publication, rerun the failed job or dispatch
`Manage Sparkle Pages` from `main` with `deploy` and the release tag.

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

The workflow changes are committed without mutating repository or publisher
settings. Before the first release on this path:

- change the `sparkle-release` environment deployment branch policy from
  `release` to protected `main`;
- create a protected `macos-signing` environment limited to `main`, then move
  the Apple certificate, identity, notarization, and keychain secrets from
  repository scope into it. The legacy `KEYCHAIN_PASSWORD` value is the Apple
  app-specific password; the workflow generates a separate ephemeral build
  keychain password for every run and derives Briefcase's notarization profile
  name from `TEAM_ID`, so no `KEYCHAIN_NAME` secret is required;
- configure a PyPI Trusted Publisher for repository `cbusillo/BD_to_AVP`,
  workflow `briefcase.yml`, environment `pypi`, and the `bd_to_avp` project;
- create/protect the `pypi` GitHub environment as desired, then remove the
  obsolete `PYPI_TOKEN` secret after a successful Trusted Publishing run;
- confirm GitHub Pages remains Actions-managed and restricted to `main`;
- enable immutable GitHub Releases before the first release; drafts remain
  resumable while published tags and assets become immutable; and
- retire the long-lived `release` branch and its ruleset after the main-only
  migration is merged and verified.

The environment, Actions-permission, immutable-release, and release-branch
settings migration follows immediately after this workflow lands. GitHub does
not expose existing secret values, and PyPI Trusted Publisher authorization is
completed through the maintainer UI.
