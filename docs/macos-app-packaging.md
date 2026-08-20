# macOS Application Packaging

The production macOS application is a SwiftUI/AppKit host with the Python
conversion engine embedded as a separately signed executable. It is distributed
directly as a notarized Apple-Silicon DMG and does not add an App Store target,
enable App Sandbox, or create a second release pipeline.

## Bundle Layout

`macos/project.yml` is the checked-in Xcode source of truth. The generated
project is intentionally ignored. `scripts/native_app.py` coordinates XcodeGen,
Xcode, the digest-pinned embedded Python runtime, signing, and real
startup and worker smokes.

```text
3D Blu-ray to Vision Pro.app/
└── Contents/
    ├── MacOS/
    │   ├── 3D Blu-ray to Vision Pro   SwiftUI/AppKit application
    │   └── BluRayToVisionProEngine    Python worker launcher
    ├── Frameworks/
    │   ├── Sparkle.framework
    │   └── Python.framework
    └── Resources/
        ├── app/                bd_to_avp source and bundled tools
        └── app_packages/       lock-derived Python dependencies
```

The containing `Info.plist` keeps the Swift executable as
`CFBundleExecutable` and supplies `MainModule=bd_to_avp.worker` for the
secondary launcher. `scripts/embedded_python.py` stages only the application
package and its locked runtime dependencies.

## Commands

```sh
uv run python scripts/native_app.py generate
uv run python scripts/native_app.py test
uv run python scripts/native_app.py build
uv run python scripts/native_app.py package
BD_TO_AVP_SUPPORT_DIAGNOSTICS_ENDPOINT=https://diagnostics.shinycomputers.com \
  uv run python scripts/native_app.py publish-current
```

`package` builds the pinned embedded Python runtime, builds the Xcode `Release`
configuration, copies the runtime into the production bundle,
signs nested Mach-O content from the inside out, verifies the complete
signature, launches the packaged app through `--startup-smoke`, and runs a real
`inspect_source` request through the embedded worker. The worker smoke also
requires canonical schema-v1 FFprobe observability in the protocol stream, so a
package cannot pass while silently falling back to a legacy child-process path.
Layout verification also checks the shipping Swift executable's direct platform
framework links, including AVKit for the embedded preview player, so dead-linking
cannot defer a missing superclass failure until the player is presented.

Ad-hoc packaging is the default for local validation. Developer ID packaging
passes `--sign-identity` and `--sign-keychain`. Ad-hoc packages omit Hardened
Runtime because they have no Team ID for dyld library validation; Developer ID
packages retain Hardened Runtime.

`publish-current` requires a clean Git worktree, runs the complete ad-hoc
`package` pipeline, and publishes the validated result under
`~/Applications/BD to AVP Builds/<full-commit>/`. The stable app and adjacent
metadata links route through one hidden current-build pointer, so publishing a
new validated build switches both paths at the same atomic filesystem step.
The metadata records the source commit, merge base with `origin/main`, UTC build
time, app-tree SHA-256, app version and build, and the local ad-hoc signing
status. Existing commit-addressed builds are immutable and retained. The
command serializes concurrent publishers, refuses symlinked or conflicting
destination objects, and never modifies the production-signed app under
`/Applications`.

The auxiliary launcher receives the direct-distribution entitlements required
by CPython and extension modules. Those entitlements belong only to the worker,
not the SwiftUI application.

## Product And Update Identity

The Release configuration uses:

- product name `3D Blu-ray to Vision Pro`;
- bundle identifier `com.shinycomputers.bd-to-avp`;
- Apple Silicon architecture and macOS 26 deployment target; and
- `Info-Release.plist`, containing the production Sparkle feed, public key, and
  user-consent policy.

The same production identity is used for Stable, RC, Beta, and Alpha. The full
identity contract also fixes the `direct` distribution value, Apple signing
team, and approved diagnostics endpoint; see
[Production Release Routes](release-routes.md).

The Debug configuration uses a Development product and bundle identifier and
contains no Sparkle distribution metadata. It cannot enroll in a production
update route.

The project version and repository build counter come from `pyproject.toml`.
`scripts/release.py prepare` updates the package version, `uv.lock`, the app
build counter, and Xcode Release metadata atomically. The package command also
passes those canonical values directly to Xcode and rejects a bundle whose
identity differs. Release metadata separately derives the dotted public tag,
title, and DMG name instead of treating the internal PEP 440 version as a public
identifier.

Stable is the default unchanneled Sparkle route. The application persists
Stable `{}`, RC `{rc}`, Beta `{beta, rc}`, and Alpha `{alpha, beta, rc}` as exact
additional-channel sets. Existing `stable`, `rc`, and legacy
`releaseCandidate` preferences migrate without selecting a less stable route;
missing or unknown values fail closed to Stable. Choosing a safer route affects
only future newer builds and never downgrades the installed app. Published Beta
3 (`0.3.0b3`, build `148`) is the one-time manual-download production seed:
older Stable and RC installations cannot discover it, while an installed Beta
3 exposes all four routes. Beta 4 (`0.3.0b4`, build `149`) through Beta 8
(`0.3.0b8`, build `153`) are published and immutable. Beta 9 (`0.3.0b9`, build
`154`) failed after production signing and is burned without a public appcast
  item. Beta 10 (`0.3.0b10`, build `155`) and Beta 11 (`0.3.0b11`, build `156`)
  are published and immutable. Abandoned Beta 12 metadata build `157` has no
  public artifact. RC 1 (`0.3.0rc1`, build `158`), RC 2 (`0.3.0rc2`, build
  `159`), and RC 3 (`0.3.0rc3`, build `160`) are published and immutable.
  Stable `0.3.0` build `161` is published and immutable. Stable `0.3.1` build
  `162` is the next prepared target for the guarded exact-SHA Stable workflow.
  Its future unchanneled cumulative item must sit above Stable `0.3.0` and all
  earlier history, skip builds `147`, `154`, and `157`, and be visible to
  Stable, RC, Beta, and Alpha.

## Release Workflow

`.github/workflows/briefcase.yml` remains the Stable operator and PyPI
trusted-publisher identity, while `.github/workflows/prerelease.yml` is the
Prerelease operator. Both declare the same repository-wide `release` concurrency
group and call `.github/workflows/release-engine.yml`; the reusable engine owns
the shared packaging, signing, notarization, appcast, attestation, publication,
and cleanup path without declaring a competing concurrency group. The engine
binds its OIDC `job_workflow_ref` and `job_workflow_sha` claims to the exact
operator run, then revalidates that policy fingerprint after the `macos-signing`
approval gate.

The engine's package job runs on GitHub's Apple-Silicon `macos-26` runner. It selects
Xcode 26.5 build `17F42` explicitly and installs the XcodeGen 2.45.4 release
artifact only after verifying its committed SHA-256 digest. It:

1. verifies that protected `main` has not moved;
2. imports the Developer ID Application certificate into an ephemeral keychain;
3. builds and signs the production app;
4. notarizes and staples the app and DMG;
5. verifies production Sparkle metadata, signatures, Gatekeeper acceptance,
   bundled tools, and worker execution; and
6. uploads the exact DMG and `SHA256SUMS` for GitHub-hosted attestation.

A separate engine-owned `macos-26` job downloads that exact notarized artifact and repeats
checksum, Gatekeeper, startup, bundled-tool, and worker validation before a
draft GitHub Release can be created. The engine's downstream jobs then build the
channel-aware appcast, re-download and verify every release boundary, publish
the GitHub Release, and deploy the durable feed snapshot. Stable Python
distributions return to the `briefcase.yml` caller by immutable artifact ID,
GitHub-recorded digest, and checksum manifest; the caller verifies that boundary
before invoking the pinned PyPI publisher in the existing `pypi` environment.

## Historical Prereleases

The retired side-by-side feedback lane published immutable preview artifacts
before the interface was accepted. The tags `native-ui-preview-1`,
`v0.3.0-beta.1`, and `v0.3.0-beta.2`, their assets, and their historical release
notes remain unchanged. They use a different bundle identifier, are not
production Alpha/Beta route releases, and do not update into the production
application. They cannot Sparkle-upgrade into Beta 3 or replace the production
app.

The separate publisher, release helper, build configuration, and workflow are
no longer active. Genuine bounded Preview conversion jobs and implementation
terms such as the native MVC splitter remain because they describe product
behavior and engine architecture rather than release branding.

## Remaining Field Evidence

Beta 3 through Beta 10 publication is complete, signed installed-app diagnostics
qualification is complete, and #382's signed AAC/package/physical Vision Pro
matrix passed on the exact Beta 8 artifact. Beta 8 exposed a packaged GUI
preview crash after its worker completed the preview route; PRs #404 and #405
fixed the missing AVKit link and added an installed-player presentation smoke.
The exact Beta 10 artifact passed that release smoke and real preview
presentation, but its GUI cancellation run left destination-backed preview
residue. PR #416 fixed that cleanup behavior after Beta 10. The route-aware
quality system also landed after Beta 10 and requires protocol v12. RC 2
qualified the production updater route and signed package, while its real-source
#458 retest exposed a broader malformed-PGS class. RC 3 exact-artifact
qualification must prove that:

- RC 2 updates forward to RC 3 on RC, Beta, and Alpha without changing the
  saved route;
- Stable excludes RC 3;
- the downloaded notarized DMG passes signature, staple, Gatekeeper, startup,
  bundled-helper, and worker-capability checks; and
- the installed app exposes the frozen route-relative quality behavior through
  protocol v12 without changing reversible Custom values; and
- packaged GUI preview, capacity, cleanup, generated-network final output,
  overwrite, and cancellation carry-forward cases plus malformed-PGS parser
  recovery, aggregate subtitle diagnostics, and profile-save accessibility
  satisfy the pre-registered RC 3 matrix for #458 without treating unsigned
  evidence as release proof; and
- the release package smoke executes the real installed player guard before
  publication. Physical M5 Vision Pro AV1 evidence remains separately tracked
  by #409 and is not inferred from publication.
