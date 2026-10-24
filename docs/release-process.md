# Release Process

This is the one release procedure for the direct-distribution Mac app. A release
must prove two things: the download is the build we meant, signed and notarized,
and an installed older copy can update to it and still works. Everything below
serves one of those.

Release history lives in GitHub Releases and git history. Older per-release
cut packets and evidence files under `docs/` are historical and are not updated.

## Production Identity

Stable, RC, Beta, and Alpha are update routes for one application, not separate
products, bundle identifiers, feeds, signing identities, or support services.
`scripts/production_identity.py` pins these values in code.

| Identity field | Production contract |
| --- | --- |
| Product name | `3D Blu-ray to Vision Pro` |
| Bundle identifier | `com.shinycomputers.bd-to-avp` |
| Distribution value | `direct` |
| Architecture and deployment | Apple Silicon, macOS 26 or later for the `0.3.x` line |
| Sparkle feed | `https://cbusillo.github.io/BD_to_AVP/appcast.xml` |
| Sparkle public key | The value in `sparkle-public-ed-key.txt`, byte-identical to packaged metadata |
| Apple signing identity | `Developer ID Application: Shiny Computers Leasing LLC (MM5YXC7T6E)`, Team `MM5YXC7T6E` |
| Diagnostics endpoint | The approved HTTPS value of the `SUPPORT_DIAGNOSTICS_ENDPOINT` repository variable |

Changing the bundle identifier, Team Identifier, feed URL, or Sparkle key is an
identity migration and needs its own reviewed plan. Changing routes is not.

## Versions, Builds, And Routes

Internal versions use canonical PEP 440. Public tags, release titles, and DMG
names use a dotted prerelease suffix. `scripts.release metadata` derives every
form from `pyproject.toml`; never reconstruct one by adding or removing `v`.

| Stage | Internal version | Public tag and title | Sparkle channel | GitHub prerelease | Latest | PyPI/Homebrew |
| --- | --- | --- | --- | --- | --- | --- |
| Alpha | `X.Y.ZaN` | `vX.Y.Z-alpha.N` | `alpha` | Yes | No | No |
| Beta | `X.Y.ZbN` | `vX.Y.Z-beta.N` | `beta` | Yes | No | No |
| RC | `X.Y.ZrcN` | `vX.Y.Z-rc.N` | `rc` | Yes | No | No |
| Stable | `X.Y.Z` | `vX.Y.Z` | absent | No | Yes | Yes |

`N` is a positive integer without leading zeroes. DMG names use the public
version, for example `3D-Blu-ray-to-Vision-Pro-0.3.0-beta.10.dmg`.

`CFBundleVersion` and `sparkle:version` are the same integer. It increases for
every production-identity build across all routes, including failed unpublished
attempts, so a burned build number is never reused. There is one forward-only
train: Alpha, Beta, RC, then Stable within a version. Stages may be skipped, but
a train never moves backward.

Stable items omit `sparkle:channel`, and Sparkle includes them for every route.
The app adds these channels:

| User route | `allowedChannels` | Eligible items |
| --- | --- | --- |
| Stable | `{}` | Stable |
| RC | `{rc}` | Stable and RC |
| Beta | `{beta, rc}` | Stable, RC, and Beta |
| Alpha | `{alpha, beta, rc}` | Stable, RC, Beta, and Alpha |

Stable is the default. A route change affects only newer builds and never
downgrades the installed app. The updater picks the greatest eligible build.

The retired preview releases `native-ui-preview-1`, `v0.3.0-beta.1` and
`v0.3.0-beta.2` belong to a separate identity. Release tooling excludes them
before parsing, ordering, release-note selection, and appcast history.
Stable-form tags through `v0.2.139`, plus `v0.2.141`, keep their historical
GitHub prerelease flag; every later production tag must agree with that flag.

## Prepare A Release

1. Prepare the version and build in a pull request:

   ```sh
   uv run python -m scripts.release prepare \
     --version <internal-pep440-version> \
     --build <next-global-build>
   ```

   It refreshes `uv.lock` and updates `pyproject.toml`, `uv.lock`, and the Xcode
   Release version and build together, only after every check passes. An
   interrupted run is recovered from its transaction journal on the next run.
2. Merge it through the merge train. CI runs `scripts.release validate`, the
   unit suite, the package builds, and the native package smoke.

`[tool.uv].required-version` sets the minimum uv. Land any lock normalization
from a newer uv in its own change, not during release preparation.

## Dispatch, Watch, And Approve

Follow the [overall release rule](https://github.com/cbusillo/direction/blob/HEAD/DIRECTION.md#stop-boundaries).

Dispatch `Stable` (`.github/workflows/briefcase.yml`) for committed Stable
metadata, or `Prerelease` (`.github/workflows/prerelease.yml`) for Alpha, Beta,
or RC. The only input is optional release-note text; committed metadata decides
the tag, channel, Latest and PyPI. Both call the guarded
`.github/workflows/release-engine.yml` and share the `release` concurrency group.
Dispatch and reruns use the `shiny-code-app[bot]` identity; the engine refuses
any other run actor.

Watch the run with the repository helper, not a generic waiter:

```sh
uv run python -m scripts.github_release_run watch \
  --run-id "$RUN_ID" --workflow "$WORKFLOW" --head-sha "$MAIN_SHA"
```

Exit code `20` means the run is waiting for a GitHub environment approval and
prints an approval fingerprint. Use the guarded command as `cbusillo`:

```sh
uv run python -m scripts.github_release_run approve \
  --run-id "$RUN_ID" --workflow "$WORKFLOW" --head-sha "$MAIN_SHA" \
  --confirm-sha "$MAIN_SHA" --approval-fingerprint "$APPROVAL_FINGERPRINT" \
  --comment "Approved release run for $MAIN_SHA."
```

Watch again until the run finishes, and keep other pull requests unmerged
meanwhile: the engine rejects a release when `main` moves. Exit code `21` is a
safety failure; stop or cancel the run instead of retrying.

## What The Release Engine Does

1. Confirm `github.sha` is the protected `main` head, validate the committed
   version, build and lock, and reject a conflicting tag, release, Sparkle build,
   or Stable PyPI version. A matching draft may resume.
2. Run the production preflight for this exact SHA. It builds the production
   package with ad-hoc signing, runs the release-app smoke (including the
   embedded worker version), installs a production-shaped DMG, and runs the
   installed-UI check, with no secrets or write access. Signing cannot be
   requested until it passes. `Production Preflight` runs the same check from
   `main` on demand.
3. Build, sign, notarize and Gatekeeper-check the app
   and DMG without a write-capable token, and attest the package.
4. Check the notarized DMG again on a fresh macOS 26 runner: checksum,
   signature, Gatekeeper, startup, bundled tools, and worker.
5. Create a draft release on `github.sha` and freeze its body.
6. In `sparkle-release`, sign the DMG and build the cumulative appcast from the
   active snapshot. New items embed the frozen notes as Markdown.
7. Upload the appcast to the draft, download everything again, and repeat the
   digest, size, notarization, Gatekeeper, version, notes and provenance checks.
8. Build `release-receipt.json` from those verified outputs and attach it to the
   draft. It is the one record of what shipped.
9. Before publication, in parallel:
   - `signed-artifact-ui` installs the exact signed DMG and runs the installed
     UI check.
   - `update-test` (`.github/workflows/update-test.yml`) installs the newest
     lower published release, points it at a locally served copy of the signed
     appcast, lets Sparkle install the candidate, and checks the relaunched
     identity and saved profiles. It fails if Sparkle never fetched the local
     feed.
10. Publish the draft only if it still targets the current `main` head, then
    deploy the appcast to GitHub Pages and confirm the live feed byte for byte.
11. For Stable, `briefcase.yml` publishes the Python package through PyPI
    Trusted Publishing, and `cbusillo/homebrew-tap` opens its own formula update.

Nothing is committed back to `main` after publication. After a Mac Beta, point
the visionOS player's TestFlight text at it
([procedure](visionos-player.md#point-the-testflight-text-at-each-new-mac-beta)).

Hands-on checks that automation can't do, such as a physical disc or Vision Pro
playback, are a note on the release's issue: what was tested, on what, and the
result. They don't block publication.

Release bodies are also the updater's Markdown. Keep the opening paragraph a
useful summary and avoid GitHub-only tables, images or HTML for anything the
Sparkle dialog must show.

## Retry, Restore, And Disable

- **Before publication:** leave the draft, fix the cause, and rerun the failed
  jobs or dispatch the same committed release. A matching draft resumes; a
  conflicting one fails closed. If the fix changes the workflow, merge it and
  start a fresh release from the new `main`. Delete an abandoned draft only once
  a newer release supersedes it.
- **After publication:** never replace a published DMG or appcast. Fix a defect
  with a newer build. If the Pages job failed, rerun it or dispatch
  `Manage Sparkle Pages` with `deploy` and the release tag.
- **Restore:** dispatch `Manage Sparkle Pages` with `restore` and a last-good
  published tag; it validates that release's `appcast.xml` before deploying.
- **Emergency stop:** dispatch it with `disable` and no tag. It deploys the empty
  feed and a disabled-state marker, and release orchestration fails closed until
  you restore.

The cumulative `appcast.xml` attached to every published release is the recovery
source of truth. Editing a draft body after the appcast is built makes
verification fail, because the embedded notes no longer match their digest.

## Required Repository Settings

- `macos-signing` is limited to `main`, holds only the Apple certificate,
  identity, notarization and keychain secrets. `KEYCHAIN_PASSWORD` is the Apple
  app-specific password; the build keychain password is generated per run.
- Repository-level secrets never reuse a name from `macos-signing` or
  `sparkle-release`, and `secrets: inherit` is forbidden.
- `sparkle-release` is limited to `main`, holds only
  `SPARKLE_EDDSA_PRIVATE_KEY`, and needs no extra review.
- `sparkle-feed-ops` is limited to `main`, holds no secrets, and requires review
  only for manual deploy, restore or disable.
- `pypi` is limited to `main` and is the PyPI Trusted Publisher for
  `briefcase.yml`, environment `pypi`, project `bd_to_avp`. No `PYPI_TOKEN`
  exists. The publisher stays in the operator workflow because PyPI does not
  accept a reusable workflow as the publisher.
- `github-pages` is Actions-managed and limited to `main`.
- Immutable GitHub Releases stay enabled.

Repository-setting reviews check secret names, scopes, branch policies and
reviewers without reading secret values.
