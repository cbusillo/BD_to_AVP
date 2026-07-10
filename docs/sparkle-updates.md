# Sparkle Updates for Direct DMG Builds

## Status

This document is the accepted architecture for issue #120. It defines the
production path, but does not enable Sparkle in current releases.

The work is intentionally split across #162 through #165 so key custody,
framework packaging, runtime integration, appcast publication, and clean-machine
validation can be reviewed independently.

## Decision Summary

- Sparkle applies only to signed and notarized direct-DMG builds.
- Future App Store builds must omit Sparkle and use Apple's update mechanism.
- GitHub Pages will host a stable HTTPS appcast. Appcast enclosures will point
  to versioned DMG assets on GitHub Releases that release policy treats as
  immutable after publication.
- The first production rollout will publish stable updates only. Prerelease
  DMGs remain manual downloads until the stable path is proven.
- Sparkle's standard user interface will own download, signature verification,
  installation, and relaunch. BD_to_AVP will expose `Check for Updates…` and
  will not silently install updates.
- A production EdDSA key will not be generated until private-key backup and
  recovery ownership are explicitly approved.
- The existing GitHub release link remains the fallback until the packaged
  Sparkle path passes an installed-app upgrade smoke.

## Current State

The current About dialog performs synchronous unauthenticated GitHub API calls,
compares release tags, and links to the release page. It does not download,
verify, install, or relaunch an update.

The current release workflow already creates a Developer ID signed and
notarized DMG with Briefcase. It does not embed Sparkle, publish signed appcast
items, or consume the Sparkle signing key. GitHub Pages is configured for
Actions deployment, and the #162 foundation provides a production public key
and a separate valid empty appcast; signed update entries remain pending.

The generated local app bundle also uses `CFBundleVersion = 1`. Sparkle uses
the bundle build version for update ordering, so a monotonic build-number policy
is required before updates can be enabled.

## Feasibility Evidence

Research for #120 used Briefcase 0.4.3 and Sparkle 2.9.4.

- Sparkle 2.9.4's official archive is universal (`arm64` and `x86_64`) and has
  SHA-256
  `ce89daf967db1e1893ed3ebd67575ed82d3902563e3191ca92aaec9164fbdef9`.
- PyObjC successfully loaded the official framework with `objc.loadBundle()`
  and resolved `SPUStandardUpdaterController` in a local feasibility probe.
- Briefcase 0.4.3 signs Mach-O files, embedded frameworks, and embedded apps,
  but does not treat Sparkle's nested `.xpc` services as bundle-signing targets.
  #163 must augment the signing path and prove the complete inside-out order.
- Briefcase 0.4.3 supports arbitrary macOS Info.plist entries through its
  `macOS.info` map. Its v0.4.3 app template hard-codes `CFBundleVersion = 1`
  and does not consume a `build` value, so #163 must override that plist key
  explicitly or first upgrade Briefcase and prove the generated value.

These checks prove the integration is feasible. They do not replace a signed,
notarized, installed-app update test.

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

The Sparkle release must be pinned by version and archive digest. The framework
must be copied after `briefcase build` and before `briefcase package`, preserving
symlinks and executable permissions.

Sparkle 2.9.4 contains `Updater.app`, `Downloader.xpc`, and `Installer.xpc`.
Briefcase 0.4.3 does not sign `.xpc` bundles as first-class targets, so #163 must
extend or supplement the signing path. Nested XPC services and apps must be
signed before `Sparkle.framework`, which must be signed before the containing
app. Briefcase may own final app signing, DMG packaging, and notarization only
after that nested-bundle order is proven.

## Build Versioning

`CFBundleShortVersionString` remains the human release version, such as
`0.2.144` or `0.2.144rc1`.

`CFBundleVersion` must be a repository-tracked monotonic integer string. With
the currently locked Briefcase 0.4.3, #163 must set it explicitly through the
macOS Info.plist map, for example:

```toml
[tool.briefcase.app.bd-to-avp.macOS.info]
CFBundleVersion = "144"
```

It must increment for every published direct-DMG build, including prereleases,
independently of the human version. If #163 upgrades Briefcase instead, the
generated app must prove that the replacement configuration writes the expected
`CFBundleVersion` before this direct override can be removed.

Because the v0.4.3 template emits its default before custom `info` entries,
implementation issue #163 must inspect the generated plist with
`plutil -extract CFBundleVersion raw <Info.plist>` and fail unless the resolved
value exactly matches the repository counter.

The workflow must fail before release publication if the build number is
missing, still `1`, non-numeric, duplicated by an existing release, or not newer
than the feed.

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

The appcast publication job must:

1. wait until the matching GitHub Release DMG is downloadable;
2. verify the pinned Sparkle tooling archive;
3. compute the DMG's EdDSA signature with `generate_appcast --ed-key-file -` or
   `sign_update`, without modifying the DMG;
4. disable delta generation with `--maximum-deltas 0` or an equivalent
   full-update-only path;
5. set each new enclosure to its exact tag-qualified GitHub Release asset URL;
6. validate the appcast version, URL, length, EdDSA signature, and absence of
   delta enclosures; and
7. deploy the feed atomically to GitHub Pages.

If appcast publication fails, the GitHub Release remains available for manual
installation and the existing feed remains unchanged.

## Stable and Prerelease Policy

The initial updater serves stable releases only. This keeps current RC builds
manual and avoids introducing channel delegates or a user preference before the
base upgrade path is proven.

After stable rollout evidence exists, a separate decision may add a prerelease
feed or Sparkle channel. A prerelease design must guarantee that prerelease
clients can later receive the stable release and that stable clients never see
prerelease items.

## Runtime Integration and UX

The runtime integration will use a small wrapper that loads the bundled
framework on the main thread and retains `SPUStandardUpdaterController` for the
application lifetime.

A live packaged-app test must prove that Qt's macOS event loop delivers
Sparkle's timers, windows, and delegate callbacks. Resolving the Objective-C
class alone is not sufficient evidence.

The direct-DMG user experience is:

- Help contains `Check for Updates…`.
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

After Sparkle is active, the synchronous PyGithub update check and the About
dialog prerelease checkbox become redundant and should be removed.

## Failure and Recovery

- An unreachable feed or invalid EdDSA signature must fail closed without
  replacing the installed app.
- A bad feed publication is recovered by restoring the previous Pages content.
- A bad application release is fixed by publishing a newer build number;
  automatic downgrade is not part of the initial design.
- Previous signed DMGs remain available for manual recovery.
- Losing the EdDSA private key requires a manual app release with a new public
  key before automatic updates can resume.
- Sparkle cannot be enabled for wider users until update smoke succeeds from an
  older app installed in `/Applications`; testing from a mounted DMG is not
  sufficient.

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
- the appcast contains no delta enclosures;
- Sparkle timers, windows, and delegate callbacks work under the packaged Qt
  event loop;
- an older installed app updates to the candidate, relaunches, and reports the
  expected version;
- unavailable-feed and invalid-signature tests leave the installed app intact;
- active processing postpones installation/relaunch; and
- the manual GitHub Releases recovery path remains documented.

## Non-Goals for the Initial Rollout

- App Store update support.
- Silent background installation.
- Delta updates.
- Phased rollout.
- Prerelease opt-in or channel switching.
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
