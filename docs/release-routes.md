# Production Release Routes

This document is the normative release-identity, version, history, and update-route
contract for the direct-distribution application. Implementation work must fail
closed when it cannot satisfy this contract.

The application preference model, release metadata/history parser, appcast
tooling, reusable release engine, and guarded operator entrypoints implement
this four-route contract. The Beta 3 metadata migration and bootstrap remain
intentionally blocked on issues 292 and 293. Do not prepare or dispatch an Alpha
or Beta release until those gates have landed and the focused signed-install
smoke has passed.

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
| Apple signing identity | A `Developer ID Application` certificate whose Team Identifier equals the protected `TEAM_ID`; certificate rotation is allowed only within that team |
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
`3D-Blu-ray-to-Vision-Pro-0.3.0-beta.3.dmg`. Workflow names and operator intent
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

The committed but unpublished `0.3.0rc1` build `147` attempt is a one-time
recovery exception. Build `147` is permanently burned. Because no tag, release,
appcast item, Pages state, Latest change, or PyPI artifact was published, a
focused migration may replace the repository metadata with `0.3.0b3` build
`148`; normal forward-only enforcement resumes immediately afterward.
The normal release preparation command intentionally rejects that backward
stage move. The recovery must use a dedicated audited migration that first
reconfirms the failed RC attempt left no tag, release, appcast, Pages, Latest,
or PyPI residue; generic release preparation remains fail-closed.

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

When published, `v0.3.0-beta.3` is the first Beta on the production identity
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
location if it exists.

After Beta 3 is installed, it exposes Stable, RC, Beta, and Alpha. Its `beta`
appcast item is eligible only on the Beta and Alpha routes; Stable and RC exclude
it. Existing Stable or RC preferences persist until a tester explicitly changes
the route. Testers explicitly choose Beta or Alpha to receive future eligible
prereleases. Beta 3 remains immutable production/feed history in the cumulative
appcast even though older clients cannot discover it.

Selecting Stable after installing Beta 3 does not downgrade to `0.2.143`; the
client waits for a newer eligible Stable build. The next production build is at
least `149`.

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
set since the previous Stable.

## Operator Boundaries

Operators receive two manual entry workflows:

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
automation actors. Stable authority accepts only committed stable, Latest,
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
