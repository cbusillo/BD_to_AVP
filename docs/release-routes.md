# Production Release Routes

This document is the normative release-identity, version, history, and update-route
contract for the direct-distribution application. Implementation work must fail
closed when it cannot satisfy this contract.

The application preference model, release metadata/history parser, appcast
tooling, reusable release engine, and guarded operator entrypoints implement
this four-route contract. Beta 3 through Beta 8, Beta 10, and Beta 11 are
published and immutable at builds `148` through `156`, excluding permanently
burned builds `147`, `154`, and abandoned metadata build `157`. Failed Beta 9
(`0.3.0b9`, build `154`) was never published. RC 1 (`0.3.0rc1`, build `158`),
RC 2 (`0.3.0rc2`, build `159`), and RC 3 (`0.3.0rc3`, build `160`) are
published and immutable. Stable `0.3.0` build `161` is also published and
immutable. Stable `0.3.1` build `162`, Beta `0.3.2b1` build `163`, and Beta
`0.3.2b2` build `164` and Beta `0.3.2b5` build `167` are published and
immutable. Beta `0.3.2b3` build `165` and Beta `0.3.2b4` build `166` are failed
unpublished signed attempts and their identities are burned. Beta 5's
post-publication exact-artifact qualification is blocked by packaged worker
version metadata. Corrective Beta `0.3.2b6` build `168` is a cancelled,
unpublished signed attempt whose draft `374538590` was deleted through the
authorized immutable-disposition path; its tag and build are permanently
non-reusable. Beta `0.3.2b7` build `169` and RC `0.3.2rc1` build `170` are
published, immutable, and fully qualified. Issue #614 owns prepared successor
Stable `0.3.2` build `171`; dispatch, signing approval, and publication remain
separately authorized. Run-bound signing approval remains a separate
authorization boundary.

## Production Identity

Stable, RC, Beta, and Alpha are update routes for one application. They are not
separate products, bundle identifiers, feeds, signing identities, or support
services.

| Identity field | Production contract |
| --- | --- |
| Product name | `3D Blu-ray to Vision Pro` |
| Bundle identifier | `com.shinycomputers.bd-to-avp` |
| Distribution value | `direct` |
| Architecture and deployment | Apple Silicon, macOS 26 or later for the `0.3.x` line |
| Sparkle feed | `https://cbusillo.github.io/BD_to_AVP/appcast.xml` |
| Sparkle public key | The value in `sparkle-public-ed-key.txt`, byte-identical to packaged metadata |
| Apple signing identity | Exact authority `Developer ID Application: Shiny Computers Leasing LLC (MM5YXC7T6E)` and Team Identifier `MM5YXC7T6E`, pinned in `scripts/production_identity.py` |
| Diagnostics endpoint | The approved HTTPS value of the `SUPPORT_DIAGNOSTICS_ENDPOINT` repository variable, packaged as `BD_TO_AVP_SUPPORT_DIAGNOSTICS_ENDPOINT` |

Changing the bundle identifier, Apple Team Identifier, feed URL, or Sparkle key
is an identity migration and requires a separately reviewed migration plan.
Changing routes is not an identity migration.

## Version And Publication Mapping

Internal versions use canonical PEP 440. Public tags, release titles, and DMG
names use a readable dotted prerelease suffix. The two forms are first-class
release metadata and must never be reconstructed by adding or removing `v`.

| Stage | Internal/package/bundle/Sparkle short version | Public tag and GitHub title | Sparkle channel | GitHub prerelease | Latest | PyPI/Homebrew |
| --- | --- | --- | --- | --- | --- | --- |
| Alpha | `X.Y.ZaN` | `vX.Y.Z-alpha.N` | `alpha` | Yes | No | No |
| Beta | `X.Y.ZbN` | `vX.Y.Z-beta.N` | `beta` | Yes | No | No |
| RC | `X.Y.ZrcN` | `vX.Y.Z-rc.N` | `rc` | Yes | No | No |
| Stable | `X.Y.Z` | `vX.Y.Z` | absent | No | Yes | Yes |

`N` is a positive canonical integer without leading zeroes. New releases emit
only the public tag forms above. Historical compact production RC tags such as
`v0.2.143rc5` remain valid read-only history inputs and are never renamed.

Externally visible DMG names use the public version stem, for example
`3D-Blu-ray-to-Vision-Pro-0.3.0-beta.10.dmg`. Workflow names and operator intent
must not appear in versions, release titles, notes, artifact names, app metadata,
or appcast content.

For this direct-DMG application, PEP 440 prerelease strings in
`CFBundleShortVersionString` are an intentional, release-tested exception to
Apple's numeric marketing-version guidance. Every new Alpha, Beta, and RC form
must pass packaging, notarization, Gatekeeper, and installed-update smoke. A
future App Store target requires a separate numeric marketing-version design.

## Build And Train Ordering

`CFBundleVersion` and Sparkle `sparkle:version` are the same canonical integer.
The value increases globally for every production-identity build, regardless of
route or whether a previous attempt was published.

The repository supports one active forward-only release train. Within a product
version, the normal stage order is Alpha, Beta, RC, then Stable. Stages may be
skipped but a published train does not move backward. Concurrent maintenance or
backport trains require a new design rather than weakening global ordering.

The committed but unpublished `0.3.0rc1` build `147` attempt was the sole
one-time recovery exception. The dedicated audited `recover-beta3` migration
validated the pinned source tree and authenticated live remote state, proving
that the failed RC attempt left no tag, release, draft, or artifact and caused
no appcast, Pages, Latest, or PyPI mutation. It serialized the operation with a
checkout lock and used a durable rollback journal while replacing repository
metadata with `0.3.0b3` build `148`. Build `147` is permanently burned and
normal forward-only enforcement has resumed. The ordinary release preparation
command still rejects that backward stage move, and the recovery command
rejects every other source, target, evidence record, and rerun.

Guarded Beta 9 run `30426833488` built and production-signed the app at
protected-main commit `355a5f559ba36d4e6862ad93c7d48527f8c7d5c0`, then failed its
packaged preview presentation smoke before DMG creation. It left no tag,
release, draft, DMG, appcast item, Pages mutation, Latest change, or package
publication. Build `154` and prerelease version `0.3.0b9` are permanently
burned; the corrected source advances to Beta 10 build `155` rather than
reusing either identity.

## Sparkle Route Eligibility

Stable items omit `sparkle:channel`. Sparkle implicitly includes those default
items for every route. The application supplies only the additional allowed
channels shown here:

| User route | `allowedChannels` | Eligible items |
| --- | --- | --- |
| Stable | `{}` | Stable |
| RC | `{rc}` | Stable and RC |
| Beta | `{beta, rc}` | Stable, RC, and Beta |
| Alpha | `{alpha, beta, rc}` | Stable, RC, Beta, and Alpha |

Stable is the default for a new or unknown preference. The existing persisted
`releaseCandidate` value migrates to RC. Route changes affect only future newer
builds: moving to a safer route never installs an older build or downgrades the
currently installed application.

The updater selects the greatest eligible global build. Installing a
prerelease must not silently change an existing route preference.

## Beta 3 Manual-Download Seed

Published `v0.3.0-beta.3` is the first Beta on the production identity
and the one-time manual-download seed:

- internal version `0.3.0b3`;
- public tag and title `v0.3.0-beta.3`;
- global build `148`;
- Sparkle channel `beta` in the cumulative appcast;
- GitHub prerelease, never Latest, PyPI, or Homebrew; and
- the normal production bundle, feed, key, signing team, and diagnostics endpoint;
- bundle identifier `com.shinycomputers.bd-to-avp`; and
- a production-app replacement rather than a side-by-side Preview install.

Currently shipped Stable and RC clients expose only Stable and RC. They cannot
select Beta, so they cannot discover Beta 3 through Sparkle; release or support
guidance must never claim otherwise. Testers obtain the exact Beta 3 DMG through
its GitHub Release and drag it into `/Applications`, replacing the production
app because the bundle identity is intentionally the same. Before doing so, copy
`~/Library/Application Support/3D Blu-ray to Vision Pro/profiles.json` to a safe
location outside that folder if it exists. Quit the production app and every
retired Preview variant before copying or restoring it. Bundle-identity
separation does not isolate this file: both production and historical Preview
builds can read and write the same profile library, so do not edit it from a
retired Preview app after installing Beta 3.

After Beta 3 is installed, it exposes Stable, RC, Beta, and Alpha. Its `beta`
appcast item is eligible only on the Beta and Alpha routes; Stable and RC exclude
it. Existing Stable or RC preferences persist until a tester explicitly changes
the route. Testers explicitly choose Beta or Alpha to receive future eligible
prereleases. Beta 3 remains immutable production/feed history in the cumulative
appcast even though older clients cannot discover it.

Selecting Stable after installing Beta 3 does not downgrade to `0.2.143`; the
client waits for a newer eligible Stable build. Beta 4 through Beta 10 are
immutable production history. Failed Beta 9 burns build `154`, and Beta 11
reserves the next global build, `156`.

## Beta 6 Published History

Published `v0.3.0-beta.6` is immutable production history:

- internal version `0.3.0b6`;
- public tag and title `v0.3.0-beta.6`;
- global build `151`;
- Sparkle channel `beta`;
- GitHub prerelease, never Latest, PyPI, or Homebrew; and
- the same production product, bundle, feed, key, signing team, and diagnostics
  endpoint as Beta 5.

The cumulative appcast places Beta 6 above immutable Beta 5 build `150`, Beta 4
build `149`, Beta 3 build `148`, Stable build `146`, RC5 build `145`, and RC4
build `144`. Stable and RC exclude all Beta items; Beta and Alpha admit them.
Historical metadata and release evidence are in
[the Beta 6 cut packet](0.3.0-beta.6-cut-packet.md).

## Beta 7 Published History

Published `v0.3.0-beta.7` is immutable production history:

- internal version `0.3.0b7`;
- public tag and title `v0.3.0-beta.7`;
- global build `152`;
- Sparkle channel `beta`;
- GitHub prerelease, never Latest, PyPI, or Homebrew; and
- the same production product, bundle, feed, key, signing team, and diagnostics
  endpoint as Beta 6.

The cumulative appcast places Beta 7 above immutable Beta 6 build `151`, Beta 5 build
`150`, Beta 4 build `149`, Beta 3 build `148`, Stable build `146`, RC5 build
`145`, and RC4 build `144`. Stable and RC exclude all Beta items; Beta and Alpha
admit them. Historical metadata and release evidence are in
[the Beta 7 cut packet](0.3.0-beta.7-cut-packet.md).

## Beta 8 Published History

Published `v0.3.0-beta.8` is immutable production history:

- internal version `0.3.0b8`;
- public tag and title `v0.3.0-beta.8`;
- global build `153`;
- Sparkle channel `beta`;
- GitHub prerelease, never Latest, PyPI, or Homebrew; and
- the same production product, bundle, feed, key, signing team, and diagnostics
  endpoint as Beta 7.

The cumulative appcast places Beta 8 above immutable Beta 7 build `152`, Beta 6 build
`151`, Beta 5 build `150`, Beta 4 build `149`, Beta 3 build `148`, Stable build
`146`, RC5 build `145`, and RC4 build `144`. Stable and RC exclude all Beta
items; Beta and Alpha admit them. Historical metadata and release evidence are
in [the Beta 8 cut packet](0.3.0-beta.8-cut-packet.md).

## Beta 9 Failed Attempt

Beta 9 is failed, unpublished, and permanently burned:

- internal version `0.3.0b9`;
- intended public tag and title `v0.3.0-beta.9`;
- global build `154`;
- guarded Prerelease run `30426833488`, attempt 1;
- exact protected-main commit `355a5f559ba36d4e6862ad93c7d48527f8c7d5c0`;
- production signing completed before the package smoke failed; and
- no tag, release, draft, DMG, appcast item, Pages mutation, Latest change,
  PyPI, or Homebrew publication.

Beta 9 never entered cumulative production history and must not be rebuilt,
retagged, published, or represented as an updater item. Its failed-attempt
evidence is in [the Beta 9 cut packet](0.3.0-beta.9-cut-packet.md).

## Beta 10 Published History

Published `v0.3.0-beta.10` is immutable production history:

- internal version `0.3.0b10`;
- public tag and title `v0.3.0-beta.10`;
- global build `155`;
- protected-main commit `50b874a4ad681762f3aa94e02926b8a82f0aa221`;
- guarded Prerelease run `30445073119`;
- Sparkle channel `beta`;
- GitHub prerelease, never Latest, PyPI, or Homebrew; and
- DMG SHA-256
  `6fed922114e152be4f2e95ad7ee597465ae8d550539e7566ed05a64d8176d91c`.

The cumulative appcast places Beta 10 above immutable Beta 8 build `153`, with
burned Beta 9 build `154` absent. Stable and RC exclude all Beta items; Beta and
Alpha admit them. Historical metadata and release evidence are in
[the Beta 10 cut packet](0.3.0-beta.10-cut-packet.md).

## RC 2 Published History

Published `v0.3.0-rc.2` is immutable production history:

- internal version `0.3.0rc2`;
- public tag and title `v0.3.0-rc.2`;
- global build `159`;
- protected-main commit `cd56f02bab8589f527af6e45fe94b2ffcce473dc`;
- guarded Prerelease run `30944931796`;
- Sparkle channel `rc`;
- GitHub prerelease, never Latest, PyPI, or Homebrew; and
- DMG SHA-256
  `e39e81b99cf9c7bd272095d7a3f96de378e0a251334e9a4c5a83c54b8f4c1d45`.

RC 1 deliberately re-nominated the never-published `0.3.0rc1` version string
from burned build `147` at a newer build. Its publication superseded the Beta 3
recovery receipt's historical absence assertion for that tag; the immutable Beta
3 recovery evidence and build-`147` exclusion remain unchanged.

The cumulative appcast places RC 2 above immutable RC 1 build `158`, Beta 11
build `156`, Beta 10 build
`155`, Beta 8 build `153`, Beta 7 build `152`, Beta 6 build `151`, Beta 5 build
`150`, Beta 4 build `149`, Beta 3 build `148`, Stable build `146`, RC5 build
`145`, and RC4 build `144`, with burned builds `147` and `154` absent. Stable
continues to exclude the RC; RC, Beta, and Alpha admit it, allowing an installed
RC-route client to update forward without changing its saved route.

The immutable metadata and publication receipt are in
[the RC 2 cut packet](0.3.0-rc.2-cut-packet.md).

## RC 3 Published History

Published `v0.3.0-rc.3` is immutable production history:

- internal version `0.3.0rc3`;
- public tag and title `v0.3.0-rc.3`;
- global build `160`;
- protected-main commit `0b06582a83a45bb38d851e62ccf38cd148c7bb95`;
- guarded Prerelease run `30990186667`;
- Sparkle channel `rc`;
- GitHub prerelease, never Latest, PyPI, or Homebrew; and
- DMG SHA-256
  `e1d936cc3231aea4f9d87fde1fd9e7792c1189254fc85bfc10fea382ffce690f`.

The cumulative appcast places RC 3 above immutable RC 2 build `159` and all
earlier history, with burned builds `147` and `154` and abandoned build `157`
absent. Stable excludes the RC; RC, Beta, and Alpha admit it. Its updater,
native release-note links, accessibility, malformed-PGS recovery, and
privacy-safe subtitle diagnostics are fully qualified.

The immutable metadata and checked publication evidence are in
[the RC 3 cut packet](0.3.0-rc.3-cut-packet.md).

## Published Stable 0.3.0

Stable `v0.3.0` is published and immutable:

- internal and public version `0.3.0`;
- public tag and title `v0.3.0`;
- global build `161`;
- no Sparkle channel, making the item eligible on every route;
- GitHub Stable and Latest;
- PyPI publication and downstream Homebrew update; and
- the same production product, bundle, feed, key, signing team, and diagnostics
  endpoint as RC 3.

Publication placed Stable above immutable RC 3 build `160` and all earlier
history, with burned builds `147` and `154` and abandoned build `157` absent.
Stable, RC, Beta, and Alpha clients can all select the newer Stable item without
changing their saved route. The release targets exact source SHA
`a9abbcf6cd1281d2c701e0c050b68fdafc5b9522`; its checked receipt and publication
record remain immutable.

The reviewed metadata, release notes, publication effects, recovery history,
and exact qualification evidence are in
[the Stable cut packet](0.3.0-cut-packet.md).

## Published Stable 0.3.1

Stable `v0.3.1` is published and immutable:

- internal and public version `0.3.1`;
- public tag and title `v0.3.1`;
- global build `162`;
- no Sparkle channel, making the item eligible on every route;
- GitHub Stable and Latest;
- PyPI publication and downstream Homebrew update; and
- the same production product, bundle, feed, key, signing team, and diagnostics
  endpoint as Stable `0.3.0`.

Publication placed Stable `0.3.1` above immutable Stable `0.3.0` build `161`
and all earlier history. Stable, RC, Beta, and Alpha clients can all select the
newer Stable item without changing their saved route. The release targets exact
source SHA `22982d5037e3b2bd03bbc9fa25332be2c8b04c97`; its checked receipt and
publication record remain immutable.

The reviewed metadata, release notes, publication effects, and exact receipt are
in [the 0.3.1 cut packet](0.3.1-cut-packet.md).

## 0.3.2 Beta 1 Published Release

Published `v0.3.2-beta.1` has:

- internal version `0.3.2b1` and public version `0.3.2-beta.1`;
- public tag and title `v0.3.2-beta.1`;
- global build `163`;
- Sparkle channel `beta`, making the item eligible only on Beta and Alpha;
- GitHub prerelease, never Latest, PyPI, or Homebrew; and
- the same production product, bundle, feed, key, signing team, and diagnostics
  endpoint as Stable `0.3.1`.

Publication placed Beta 1 above immutable Stable `0.3.1` build `162` and all
earlier history. Stable and RC clients exclude the Beta item; Beta and Alpha
clients can select it because build `163` is newer. The release targets exact
source SHA `1a204ef26a02d63d5ca872f38db4aa82e0aa409f` and remains immutable.

The reviewed metadata, release-note seed, focused Beta scope, and qualification
boundary are in
[the 0.3.2 Beta 1 cut packet](0.3.2-beta.1-cut-packet.md).

## 0.3.2 Beta 2 Published History

Published `v0.3.2-beta.2` has:

- internal version `0.3.2b2` and public version `0.3.2-beta.2`;
- public tag and title `v0.3.2-beta.2`;
- global build `164`;
- Sparkle channel `beta`, making the item eligible only on Beta and Alpha;
- GitHub prerelease, never Latest, PyPI, or Homebrew; and
- the same production product, bundle, feed, key, signing team, and diagnostics
  endpoint as Beta `0.3.2b1`.

Publication placed Beta 2 above immutable Beta 1 build `163`, Stable `0.3.1`
build `162`, and all earlier history. Stable and RC clients exclude the Beta
item; Beta and Alpha clients can select it because build `164` is newer. The
release targets exact source SHA
`da307689f38ee696c872b98c7c784a17e9fe9d19` and remains immutable.

The reviewed metadata, release-note seed, scope freeze, and qualification
evidence are in
[the 0.3.2 Beta 2 cut packet](0.3.2-beta.2-cut-packet.md).

## 0.3.2 Beta 3 Failed Attempt

Beta 3 is failed, unpublished, and permanently burned:

- internal version `0.3.2b3` and public version `0.3.2-beta.3`;
- public tag and title `v0.3.2-beta.3`;
- global build `165`;
- Sparkle channel `beta`, making the item eligible only on Beta and Alpha;
- GitHub prerelease, never Latest, PyPI, or Homebrew; and
- the same production product, bundle, feed, key, signing team, and diagnostics
  endpoint as Beta `0.3.2b2`.

Guarded Prerelease run `32446869702` built, Developer ID signed, notarized, and
packaged build `165` from source SHA
`d2d455fdbedfde19b40a3be9c86e98997ec42981`. The run failed before publication
in the signed-artifact UI accessibility lane because the installer call omitted
its required DMG mount point. No public tag, release, appcast item, Pages state,
PyPI version, or Homebrew change was created.

The unpublished draft and its asset identities were recorded, then the draft
was deleted after explicit recovery authorization. The corrected source
advances to Beta 4 build `166` rather than reusing either Beta 3 identity. The
failed-attempt details are in
[the 0.3.2 Beta 3 cut packet](0.3.2-beta.3-cut-packet.md).

## 0.3.2 Beta 4 Failed Attempt

Beta 4 is failed, unpublished, and permanently burned:

- internal version `0.3.2b4` and public version `0.3.2-beta.4`;
- public tag and title `v0.3.2-beta.4`;
- global build `166`;
- Sparkle channel `beta`, making the item eligible only on Beta and Alpha;
- GitHub prerelease, never Latest, PyPI, or Homebrew; and
- the same production product, bundle, feed, key, signing team, and diagnostics
  endpoint as Beta `0.3.2b2`.

Guarded Prerelease run `32453767766` built, Developer ID signed, notarized, and
packaged build `166` from source SHA
`1473d14ea86562ff743da6f3d78f230361b7de23`. The run failed before publication
in the signed-artifact UI accessibility lane because the standalone fresh
install had no pre-save `profiles.json`, which the qualification test
incorrectly treated as an error. No public tag, release, appcast item, Pages
state, PyPI version, or Homebrew change was created.

The unpublished draft and its asset identities were recorded, then the draft
was deleted after explicit recovery authorization. PR #620 corrected the
qualification contract, and the successor advances to Beta 5 build `167`
rather than reusing either Beta 4 identity. The failed-attempt details are in
[the 0.3.2 Beta 4 cut packet](0.3.2-beta.4-cut-packet.md).

## 0.3.2 Beta 5 Publication And Cancelled Beta 6 Attempt

Guarded Prerelease run `32488665999` published immutable
`v0.3.2-beta.5` build `167` on August 21, 2026 from source SHA
`f6ce414db07030e2ad2c081a9d4287639a113446`. Its exact DMG and appcast remain
valid public artifacts, but post-publication qualification found that the
embedded worker reported `0.0.0` instead of `0.3.2b5`. PR #623 corrects that
diagnostics identity and strengthens the pre-signing package gate. Superseded
success-shaped evidence PR #622 was closed without merge after the accurate
failed-post-publication disposition merged.

The reviewed Beta 6 identity was:

- internal version `0.3.2b6` and public version `0.3.2-beta.6`;
- public tag and title `v0.3.2-beta.6`;
- global build `168`;
- Sparkle channel `beta`, making the item eligible only on Beta and Alpha;
- GitHub prerelease, never Latest, PyPI, or Homebrew; and
- the same production product, bundle, feed, key, signing team, and diagnostics
  endpoint as published Beta `0.3.2b5`.

Guarded Prerelease run `32502068709` built, Developer ID signed, notarized, and
verified build `168` from source SHA
`b4abf8829b6a539b40bb369bfd62dea504d35be1`. It created unpublished draft
`374538590`, cumulative appcast assets, and an immutable release receipt, then
was intentionally cancelled in the installed candidate UI accessibility lane
before exact-artifact qualification, Pages deployment, or publication.

No Git tag, public release, Pages appcast item, PyPI version, or Homebrew change
was created. The deleted draft was diagnostic evidence only. The exact asset
identities, checked receipt, and authorized deletion disposition are recorded under
`docs/release-attempts/v0.3.2-beta.6/`; `.github/release-freezes.json` prevents
redispatch of the Beta 6 tag. Build `168` must remain absent from future updater
feeds. Explicit authorization was granted and draft `374538590` was deleted at
`2026-08-21T22:58:29Z`; the checked `draft-deletion-v1.json` disposition records
verified absence of both draft and tag. A newer successor identity is expected
to begin at Beta 7/build `169`. Milestone-context validation accepts the
cancelled candidate only after that disposition validates, then still requires
build `168` in the successor qualification's burned-build list.

Beta 7 is published and immutable as internal version `0.3.2b7`, public tag and
title `v0.3.2-beta.7`, and global build `169`. Guarded Prerelease run
`32666346343` published release `375333598` from source and tag SHA
`d994ce11f9d4a669f068c89dc8e51ca269710290`. Final qualification passed with
zero blocking or operator-required cases; the immutable publication and
qualification contract is recorded in
[the 0.3.2 Beta 7 cut packet](0.3.2-beta.7-cut-packet.md).

RC 1 is published and immutable as internal version `0.3.2rc1`, public tag and
title `v0.3.2-rc.1`, and global build `170`. Guarded Prerelease run
`32796196439` published release `376092200` from source and tag SHA
`1d691a6db7c18d09f55a425ec77a01ab6d917d80`. Final qualification passed with
zero blocking cases and 15 completed cases. Its immutable publication and
qualification contract is recorded in
[the 0.3.2 RC 1 cut packet](0.3.2-rc.1-cut-packet.md).

Stable 0.3.2 is prepared as internal and public version `0.3.2`, tag and title
`v0.3.2`, and global build `171`. RC 1 is the immediate global predecessor and
exact RC-route qualification base. Stable `v0.3.1` build `162` remains the
previous Stable update and release-note base. The unchanneled Stable item is
eligible on Stable, RC, Beta, and Alpha routes. Failed builds `165` and `166`
and cancelled build `168` remain burned. No Stable dispatch, signing approval,
tag, release, draft, appcast, PyPI, or Homebrew mutation is authorized by this
preparation. The preparation contract is recorded in
[the 0.3.2 Stable cut packet](0.3.2-cut-packet.md).

## Historical Boundaries

The following releases belong to the retired side-by-side preview identity and
are not members of the production train:

- `native-ui-preview-1`;
- `v0.3.0-beta.1`; and
- `v0.3.0-beta.2`.

Their tags, assets, notes, product name, and bundle identifier remain immutable.
Release tooling must exclude them before version parsing, ordering, ancestry,
duplicate detection, release-note base selection, and appcast history. Their
public tag syntax does not grant them production Beta status. They cannot
Sparkle-update into Beta 3: their retired Preview identities remain separate from
the production bundle and its feed.

Production release-note history includes published production Alpha, Beta, RC,
and Stable releases. Prerelease notes compare with the newest lower production
release that is an ancestor of the release commit. Stable notes compare with the
newest lower production Stable release so they summarize the complete change
set since the previous Stable. Stable-form tags through `v0.2.139`, plus the
pulled `v0.2.141`, are bounded pre-contract exceptions that GitHub records as
prereleases. Release-note selection retains that GitHub prerelease
classification while still parsing their versions and detecting duplicates.
Starting with `v0.2.140`, every other production tag must agree with the GitHub
prerelease flag; prerelease-form tags marked Stable are never accepted.

## Operator Boundaries

`Production Preflight` is a separate manual, non-authorizing workflow. It accepts
only an exact full current protected-`main` SHA and runs the production-shaped
ad-hoc package, exact packaged-worker smoke, preventive DMG installation, and
shared installed-UI qualification before a successor identity is prepared. It
has no signing environment, secrets, write permission, route selection, tag,
draft, appcast, or publication capability. Its bounded seven-day artifacts are
source-specific readiness diagnostics, not signed release evidence. The guarded
release engine invokes the same maintained preflight implementation again before
the signing job can request `macos-signing` approval.

Release operators receive two manual entry workflows:

- **Stable**, which accepts only committed Stable metadata; and
- **Prerelease**, which accepts committed Alpha, Beta, or RC metadata.

Both call one guarded release engine, share the same repository-wide `release`
concurrency group, require protected `main`, reject stale SHAs, and preserve the
exact `macos-signing` approval contract. The workflow choice authorizes intent;
committed metadata alone determines stage, public identity, Sparkle channel,
Latest behavior, and package publication. Neither entrypoint nor the reusable
engine accepts a route, mode, stage, or publication override.

The `Stable` operator remains `.github/workflows/briefcase.yml`; the
`Prerelease` operator is `.github/workflows/prerelease.yml`. Each caller declares
the same `release` concurrency group, while
`.github/workflows/release-engine.yml` declares no concurrency group so a
caller and its reusable job cannot cancel or indefinitely queue each other.
Before any release work, the engine verifies the exact operator workflow ref and
definition SHA, derives Stable or Prerelease authority from that validated path,
verifies its own OIDC `job_workflow_ref` and `job_workflow_sha` claims, and binds
the run ID, attempt, protected-main SHA, dispatch event, and both configured
automation actors. The reusable interface declares the Apple and Sparkle secret
names as optional, and each operator caller forwards only those exact names.
Because the callers run outside the protected environments, the mappings carry
no protected value; the job-level reviewed environments supply and override them
inside the called jobs. Same-named repository secrets and `secrets: inherit` are
forbidden. Stable authority accepts only committed stable, Latest,
PyPI-enabled metadata. Prerelease authority accepts only committed Alpha, Beta,
or RC metadata that is a non-Latest GitHub prerelease with PyPI disabled. The
engine records the validated route and publication effects in the shared step
summary, then revalidates the policy fingerprint after the `macos-signing`
approval gate and before using any Apple credential.

PyPI is the deliberate caller-side exception to engine job ownership. PyPI
Trusted Publishing does not accept a reusable workflow as the configured
publisher workflow, so Stable Python distributions cross back from the engine
as an immutable artifact ID and GitHub-recorded digest with an exact
`SHA256SUMS` manifest. The pinned publisher action remains in `briefcase.yml`,
in the `pypi` environment, after the complete reusable engine succeeds. This
preserves the existing `job_workflow_ref`, OIDC provenance, environment, and
project identity without a live trusted-publisher migration.

Published assets and cumulative appcast snapshots are immutable. A failed
pre-publication run may resume its matching draft. A post-publication problem
uses the documented feed disable/restore path and never replaces assets.
