# Main-Only Release Process

## Release Preparation

Every release version and Sparkle build number is committed through a normal
pull request before release orchestration runs. Use the repository command:

```sh
uv run python scripts/release.py prepare \
  --version 0.2.144rc1 \
  --build 145
```

The version must be a canonical three-part stable version or release candidate,
such as `0.2.144` or `0.2.144rc1`. The numeric `CFBundleVersion` must increase
for every RC and Stable DMG. The command stages a refreshed `uv.lock`, validates
the staged metadata, and updates `pyproject.toml` and `uv.lock` together only
after every check succeeds. A lock refresh failure leaves both files unchanged.

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
2. Reject an existing tag, GitHub Release, Sparkle version/build, or Stable PyPI
   version. Both the live feed and the latest durable release snapshot are
   checked.
3. Build, sign, notarize, and Gatekeeper-validate the macOS DMG without a
   write-capable repository token. Record its exact name, byte size, SHA-256,
   and `SHA256SUMS` entry.
4. Create a draft GitHub Release targeting only `github.sha` and upload the
   package assets with overwrite disabled by default.
5. In the protected `sparkle-release` environment, re-download the draft DMG,
   verify its exact identity and distribution signatures, load the newest
   published `appcast.xml` release asset, sign the DMG, verify the EdDSA
   signature, and build the cumulative snapshot.
6. Upload `appcast.xml` to the draft, re-download the DMG, checksum, and appcast,
   and repeat the exact digest, size, notarization, Gatekeeper, bundle-version,
   and appcast-item checks.
7. Publish the verified draft only if it still targets the current `main` HEAD.
   Stable releases then publish the Python distributions through PyPI Trusted
   Publishing; RC releases never publish to PyPI.
8. Deploy the durable `appcast.xml` release asset to GitHub Pages. A deployment
   failure can be retried without rebuilding, retagging, or re-signing.

The cumulative `appcast.xml` attached to every published GitHub Release is the
recovery source of truth. GitHub Pages is a deployment target, not the only copy
of feed history.

## Retry, Restore, and Disable

If a release run fails before publication, leave the release as a draft while
diagnosing it. Never replace a published DMG or appcast asset. If the Pages job
fails after publication, rerun the failed job or dispatch `Manage Sparkle Pages`
from `main` with `deploy` and the release tag.

To restore an earlier last-good cumulative feed, dispatch `Manage Sparkle Pages`
from `main` with `restore` and the selected published release tag. The workflow
downloads and validates that release's `appcast.xml` asset before deployment.

For an emergency stop, dispatch the same workflow with `disable` and no tag.
Emergency disable preempts an in-flight Pages deployment and deploys the
committed valid empty feed. It does not edit or delete any GitHub Release,
release asset, or cumulative snapshot. Restore the last-good tag when updates
may resume.

## Required Repository Settings

The workflow changes are committed without mutating repository or publisher
settings. Before the first release on this path:

- change the `sparkle-release` environment deployment branch policy from
  `release` to protected `main`;
- configure a PyPI Trusted Publisher for repository `cbusillo/BD_to_AVP`,
  workflow `briefcase.yml`, environment `pypi`, and the `bd_to_avp` project;
- create/protect the `pypi` GitHub environment as desired, then remove the
  obsolete `PYPI_TOKEN` secret after a successful Trusted Publishing run;
- confirm GitHub Pages remains Actions-managed and restricted to `main`;
- enable immutable GitHub Releases after the draft-upload-verify-publish path
  has completed its first controlled RC; and
- retire the long-lived `release` branch and its ruleset after the main-only
  migration is merged and verified.

Apple signing/notarization credential migration and broader Actions provenance
hardening remain separate follow-up work under the parent release-control plan.
