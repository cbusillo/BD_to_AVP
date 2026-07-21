# Sparkle Updates for Direct DMG Builds

## Status

This document is the accepted architecture for issue #120 and the implementation
contract for #163 through #165. Published builds before the first
Sparkle-enabled release still use manual GitHub Release downloads.

The work is intentionally split across #162 through #165 so key custody,
framework packaging, runtime integration, appcast publication, and clean-machine
validation can be reviewed independently.

## Decision Summary

- Sparkle applies only to signed and notarized direct-DMG builds.
- Future App Store builds must omit Sparkle and use Apple's update mechanism.
- GitHub Pages will host a stable HTTPS appcast. Appcast enclosures will point
  to versioned DMG assets on GitHub Releases that release policy treats as
  immutable after publication.
- One cumulative appcast serves Stable, RC, Beta, and Alpha routes for the same
  production identity. Stable is default and unchanneled; RC, Beta, and Alpha
  use `rc`, `beta`, and `alpha` channel tokens.
- Route eligibility is cumulative: Stable `{}`, RC `{rc}`, Beta `{beta, rc}`,
  and Alpha `{alpha, beta, rc}`. Sparkle implicitly includes unchanneled Stable
  items for every route.
- Sparkle's standard user interface will own download, signature verification,
  installation, and relaunch. BD_to_AVP will expose `Check for Updates…` and
  will not silently install updates.
- The production EdDSA key follows the custody boundary in
  [sparkle-key-custody.md](sparkle-key-custody.md).
- The existing GitHub release link remains the fallback until the packaged
  Sparkle path passes an installed-app upgrade smoke.

## Current State

The implementation embeds pinned Sparkle 2.9.4 in direct-DMG builds, removes the
synchronous PyGithub About-dialog checker, and exposes Sparkle's standard
`Check for Updates…` action. Source/development builds retain only a manual
GitHub Releases link and never initialize Sparkle.

The production SwiftUI project exact-pins the same Sparkle 2.9.4 package. A
single observable update controller owns Sparkle's automatic-check preference,
the shared `BDToAVPUpdateChannel` selection, check availability, Settings
controls, and Help commands. It exposes Stable, RC, Beta, and Alpha using the
four routes in [Production Release Routes](release-routes.md). Existing Stable
and RC preferences stay at those routes until an explicit selection changes
them. It starts Sparkle only when the bundle has the complete
direct-distribution policy: `direct` channel, HTTPS feed, public key, automatic
installation disabled, extraction verification enabled, and no forced
`SUEnableAutomaticChecks` value. Missing or invalid metadata fails closed to the
GitHub Releases fallback. Sparkle relaunch is postponed while the conversion
worker is active and resumes after the worker becomes idle.

Debug builds use a separate Development identity, omit all update metadata, and
do not initialize Sparkle, although the single-target SPM build embeds the
dormant signed framework. The Release configuration uses the production bundle
identifier and updater policy. Signed installed-app upgrades, four-route feed
behavior, permission prompting, and final rendered-note appearance remain
release-cycle validation for #197.

The Xcode Release target writes the direct-distribution metadata and canonical
repository build counter. Briefcase stages only the embedded Python engine. The
manual main-only release workflow validates the final notarized app and DMG on
the release host and again on macOS 26, creates a draft release, signs it with
the protected environment key, attaches a cumulative appcast snapshot,
re-downloads and verifies every asset, publishes the draft, and deploys that
durable snapshot.
Each new appcast item embeds the digest-bound draft release body as Markdown, so
Sparkle uses its adaptive native text view without loading GitHub page chrome or
making a second release-note request. A full-release link remains available for
downloads and extended context, and historical external-link items remain valid.
The live feed remains empty until the first enabled release is published. See
[release-process.md](release-process.md) for the operator sequence.

Stable `0.2.143` remains compatible with macOS 14. The production SwiftUI line
starting with `0.3.0b3` requires Apple Silicon and macOS 26. Sparkle's minimum
system version prevents older systems from being offered an incompatible item;
those installations remain on the last compatible stable build.

## Implementation Evidence

Research for #120 used Briefcase 0.4.3 and Sparkle 2.9.4.

- Sparkle 2.9.4's official archive is universal (`arm64` and `x86_64`) and has
  SHA-256
  `ce89daf967db1e1893ed3ebd67575ed82d3902563e3191ca92aaec9164fbdef9`.
- PyObjC successfully loaded the official framework with `objc.loadBundle()`
  and resolved `SPUStandardUpdaterController` in a local feasibility probe.
- Briefcase 0.4.4 signs Mach-O files, embedded frameworks, and embedded apps,
  but does not treat Sparkle's nested `.xpc` services as bundle-signing targets.
  `scripts/briefcase_macos_signing.py` adds `.xpc` targets to the same
  depth-first signing pass and is guarded to Briefcase 0.4.4.
- Briefcase 0.4.4 supports arbitrary macOS Info.plist entries through its
  `macOS.info` map. Its v0.4.4 app template hard-codes `CFBundleVersion = 1`
  and does not consume a `build` value, so the macOS `info` map overrides that
  plist key explicitly.

Local create/build validation proves metadata resolution, framework layout,
PyObjC delegate bridging, and ad-hoc nested signing. It does not replace a
Developer ID signed, notarized, installed-app update test.

## Build Boundary

Direct distribution must be explicit rather than inferred from a receipt or
from the framework merely existing.

The direct-DMG build will contain an Info.plist key such as:

```text
BDToAVPDistributionChannel = direct
```

The direct-DMG build will also contain:

- `SUFeedURL = https://cbusillo.github.io/BD_to_AVP/appcast.xml`;
- the approved `SUPublicEDKey` from
  [`sparkle-public-ed-key.txt`](../sparkle-public-ed-key.txt);
- `SUAllowsAutomaticUpdates = false`, so every install remains user-approved;
  and
- `SUVerifyUpdateBeforeExtraction = true`.

`SUEnableAutomaticChecks` remains unset so Sparkle's normal permission prompt
controls whether periodic checks begin. Runtime initialization requires the
`direct` channel, the bundled framework, and all required metadata. If any are
missing, the app fails closed to the manual GitHub Releases path.

A future App Store build will set its channel to `app-store`, omit Sparkle
metadata, and omit `Sparkle.framework` entirely.

`vendor/sparkle-macos.toml` pins the Sparkle version, archive URL, and digest.
The Xcode project exact-pins that version through Swift Package Manager.
Production packaging signs the resulting framework, nested app, and XPC bundles
from the inside out before signing the containing application.

Sparkle 2.9.4 contains `Updater.app`, `Downloader.xpc`, and `Installer.xpc`.
Nested XPC services and apps are signed before `Sparkle.framework`, which is
signed before the containing app. Sparkle targets do not inherit the worker's
Python runtime entitlements.

## Build Versioning

`CFBundleShortVersionString` remains the canonical internal PEP 440 version,
such as `0.3.0b3`, `0.3.0rc1`, or `0.3.0`. Public tags, titles, and DMG names
use the separate readable forms defined in `release-routes.md`; tooling must not
derive one form by adding or removing `v`.

PEP 440 prerelease strings are an intentional direct-DMG exception to Apple's
numeric marketing-version guidance. They have no App Store compatibility
guarantee and must pass packaging, notarization, Gatekeeper, and installed-app
update smoke for every stage form.

`CFBundleVersion` must be a repository-tracked monotonic integer string. The
canonical value remains in the macOS Info.plist map used by runtime staging:

```toml
[tool.briefcase.app.bd-to-avp.macOS.info]
CFBundleVersion = "<next-global-build>"
```

It must increment globally for every production-identity build across Alpha,
Beta, RC, and Stable, including failed unpublished attempts. The Xcode Release
target and Sparkle `sparkle:version` must use the same value.

Briefcase staging and the Xcode package both inherit the internal version from
`[project].version`; the duplicate `[tool.briefcase].version` key is
intentionally absent. Full release metadata is committed before release.
Prepare the internal version, monotonic build counter, `uv.lock`, and Xcode Release
metadata with:

```sh
uv run python -m scripts.release prepare --version <version> --build <build>
```

The workflow must fail before release publication if the build number is
missing, still `1`, non-numeric, duplicated by an existing release, or not newer
than the feed. Packaging also rejects an Xcode bundle whose version or build
differs from the committed release metadata.

## Appcast and Key Custody

The appcast will live at
`https://cbusillo.github.io/BD_to_AVP/appcast.xml`. Changing that URL after the
first Sparkle-enabled release would require a manual application update, so it
is part of the distribution contract. GitHub Release DMGs remain the
downloadable update archives and must not be replaced after an appcast references
them.

The Sparkle EdDSA private key may exist only in these approved locations:

1. Sparkle's working item in the maintainer's login keychain;
2. a maintainer-owned Apple Passwords recovery entry synchronized by iCloud
   Keychain; and
3. a protected GitHub Actions environment secret used by the release/appcast
   workflow.

Temporary plaintext material is permitted only on a RAM disk during the
one-time Passwords import or a documented recovery test.

The operational names, one-time provisioning sequence, recovery test, and feed
disable procedure are maintained in
[sparkle-key-custody.md](sparkle-key-custody.md).

Only the public key is embedded in the app. Private key material must never be
committed, printed, uploaded as an artifact, or copied into issue/PR text.

The release receives one maintainer approval at the main-only
`macos-signing` boundary. Apple signing credentials, the Sparkle private key,
PyPI OIDC publication, and Pages deployment remain in separate environments,
but the downstream environments do not add redundant reviews; each job remains
blocked on the preceding verification results.
Manual deploy, restore, and disable operations use a separate secret-free
`sparkle-feed-ops` approval and are not part of normal release orchestration.

The appcast publication path must:

1. create an unpublished draft release targeting the validated `main` SHA;
2. transfer the already-verified package workflow artifact into the read-only
   Sparkle signing job and prove its exact name, size, SHA-256, notarization,
   Gatekeeper assessment, and bundle metadata;
3. start from the newest published `appcast.xml` release asset, or the committed
   empty feed before the first snapshot;
4. verify the pinned Sparkle tooling archive;
5. compute and verify the DMG's EdDSA signature with
   `sign_update --ed-key-file -`, without modifying the DMG;
6. add only a full-DMG enclosure and never generate delta elements;
7. set each new enclosure to its exact tag-qualified GitHub Release asset URL;
8. embed the exact digest-bound draft body as Markdown with a tag-qualified
   `sparkle:fullReleaseNotesLink`, capped at 128 KiB per item;
9. attach the cumulative `appcast.xml` snapshot to the draft release;
10. re-download and verify the DMG, checksum, appcast, and embedded notes; and
11. publish the draft before deploying its durable appcast asset atomically to
    GitHub Pages.

Release publication rejects an existing tag or short version, disables release
asset overwrite, and verifies that the downloaded GitHub Release DMG has the
same name, size, and SHA-256 digest as the locally notarized artifact before it
is signed into the appcast.

If appcast construction or verification fails, the GitHub Release remains an
unpublished draft and the existing feed remains unchanged. If Pages deployment
fails after publication, the durable release snapshot can be deployed again
without rebuilding or retagging.

## Stable And Prerelease Policy

One appcast serves all four routes:

- Stable items omit `sparkle:channel`;
- RC, Beta, and Alpha items use `rc`, `beta`, and `alpha` respectively;
- every new or unknown preference defaults to Stable;
- existing `releaseCandidate` preferences migrate to RC; and
- allowed-channel sets are `{}`, `{rc}`, `{beta, rc}`, and
  `{alpha, beta, rc}` from Stable through Alpha.

Sparkle continues to include default Stable items automatically. The updater
selects the greatest eligible global build, so choosing a safer route affects
future updates only and never downgrades the installed application. Published
`v0.3.0-beta.3` (`0.3.0b3`, build `148`) is an immutable `beta` item in the
cumulative feed and a manual-download seed. Currently shipped Stable and
RC clients cannot select Beta, so they cannot discover it through Sparkle; do
not claim otherwise. Its `beta` item is eligible only to Beta and Alpha, not
Stable or RC. After manually installing the production
`com.shinycomputers.bd-to-avp` DMG, testers see all four routes and explicitly
select Beta or Alpha for future prereleases. Retired `v0.3.0-beta.1` and
`v0.3.0-beta.2` Preview identities remain separate, immutable, and unable to
Sparkle-upgrade into Beta 3.

The next prepared item is authorized `v0.3.0-beta.4` (`0.3.0b4`, build `149`).
Issue #316 removed the committed freeze after production diagnostics
qualification. Publication must append Beta 4 above Beta 3 in the same
cumulative feed, remain excluded from Stable and RC, and be eligible to Beta and
Alpha. The exact-SHA Prerelease workflow, signing approval, notarization, and
public verification remain separate fail-closed boundaries.

## Runtime Integration and UX

The runtime integration loads the bundled framework on the main thread and
retains `SPUStandardUpdaterController` and its delegate for the application
lifetime.

A live packaged-app test must prove that Qt's macOS event loop delivers
Sparkle's timers, windows, and delegate callbacks. Resolving the Objective-C
class alone is not sufficient evidence.

The direct-DMG user experience is:

- Help contains `Check for Updates…`.
- Help contains an `Update Route` submenu with Stable, RC, Beta, and Alpha; the
  preference is persisted in the app's `NSUserDefaults` domain and defaults to
  Stable.
- Sparkle uses its standard permission and update windows.
- Automatic checks may be enabled only through Sparkle's normal consent path.
- Downloaded updates require user approval to install and relaunch.
- An update must not terminate active media processing. The updater delegate or
  application controller must use
  `updater:shouldPostponeRelaunchForUpdate:untilInvokingBlock:` to postpone the
  relaunch until processing is idle.

Development and PyPI builds continue to link to GitHub Releases and do not try
to load Sparkle. A future App Store build reports that updates are managed by
the App Store.

The synchronous PyGithub update check and About-dialog prerelease checkbox are
removed. PyGithub and the now-unused direct `packaging` dependency are also
removed from application and Briefcase requirements.

## Failure and Recovery

- Before publication, a failed Beta 3 preparation leaves the live feed and all
  published assets unchanged. Keep a matching draft only for an exact retry or
  stop the release before publication; do not claim a draft has become
  discoverable.
- An unreachable feed or invalid EdDSA signature must fail closed without
  replacing the installed app.
- A bad feed publication is recovered by redeploying the previous release's
  durable `appcast.xml` snapshot.
- Emergency disable deploys the committed empty feed without deleting or
  replacing any cumulative release snapshot. The companion
  `appcast-state.json` marker keeps disable sticky: release and normal deploy
  paths refuse to re-enable updates until an explicit restore selects a durable
  snapshot.
- A bad application release is fixed by publishing a newer build number;
  automatic downgrade is not part of the initial design.
- Previous signed DMGs remain available for manual recovery.
- Published GitHub Release DMGs and appcast assets are never replaced. For a
  severe post-publication withdrawal, disable the feed, then restore a
  last-good published snapshot when updates may resume.
- Losing the EdDSA private key requires a manual app release with a new public
  key before automatic updates can resume.
- Beta 3 bootstrap evidence starts from an installed Stable or RC production
  app and proves manual replacement with the exact tagged DMG; no updater offer
  is expected. Subsequent installed-update smoke starts from a Beta-3-or-later
  route-capable production app. Testing from a mounted DMG is not sufficient.

## Implementation Order

1. #162 — provision key custody and GitHub Pages hosting.
2. #163 — embed and sign Sparkle in Briefcase direct-DMG builds.
3. #164 — initialize Sparkle and wire the direct-DMG update UI.
4. #165 — publish signed appcasts and validate the installed-app upgrade path.

## Promotion Gates

Before enabling the appcast for normal users:

- the framework and every nested `.app` and `.xpc` pass explicit code-signature
  verification before the containing app passes `codesign --verify --deep --strict`;
- the app and DMG pass the existing notarization and Gatekeeper gates;
- Info.plist contains the expected direct channel, feed, public key, monotonic
  build version, `SUAllowsAutomaticUpdates = false`, and
  `SUVerifyUpdateBeforeExtraction = true`;
- the appcast enclosure URL, size, build version, and EdDSA signature validate;
- the complete matrix in `release-smoke.md` proves Stable, RC, Beta, and Alpha
  eligibility, Beta 3 exclusion from Stable/RC selection, legacy-preview
  exclusion, and no downgrade after a safer route change;
- the appcast contains no delta enclosures;
- Sparkle timers, windows, and delegate callbacks work under the packaged Qt
  event loop;
- for Beta 3, an installed Stable or RC production app is manually replaced by
  the exact tagged DMG and is never claimed to discover the seed through
  Sparkle; for later releases, an installed Beta-3-or-later route-capable app
  updates to the candidate, relaunches, and reports the expected version;
- unavailable-feed and invalid-signature tests leave the installed app intact;
- active processing postpones installation/relaunch; and
- the manual GitHub Releases recovery path remains documented.

## Non-Goals for the Initial Rollout

- App Store update support.
- Silent background installation.
- Delta updates.
- Phased rollout.
- Replacing GitHub Releases as the DMG artifact host.

## Primary References

- [Sparkle documentation](https://sparkle-project.org/documentation/)
- [Sparkle programmatic setup](https://sparkle-project.org/documentation/programmatic-setup/)
- [Sparkle publishing and channels](https://sparkle-project.org/documentation/publishing/)
- [Sparkle customization](https://sparkle-project.org/documentation/customization/)
- [Sparkle 2.9.4 release](https://github.com/sparkle-project/Sparkle/releases/tag/2.9.4)
- [Briefcase macOS configuration](https://briefcase.beeware.org/en/stable/reference/platforms/macOS/)
- [Briefcase macOS packaging](https://briefcase.beeware.org/en/stable/how-to/publishing/macOS.html)
- [GitHub Pages custom workflows](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages)
