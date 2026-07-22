# macOS Application Packaging

The production macOS application is a SwiftUI/AppKit host with the Python
conversion engine embedded as a separately signed executable. It is distributed
directly as a notarized Apple-Silicon DMG and does not add an App Store target,
enable App Sandbox, or create a second release pipeline.

## Bundle Layout

`macos/project.yml` is the checked-in Xcode source of truth. The generated
project is intentionally ignored. `scripts/native_app.py` coordinates XcodeGen,
Xcode, the Briefcase-managed Python runtime staging path, signing, and real
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
        ├── app_packages/       Python dependencies
        └── support/
```

The containing `Info.plist` keeps the Swift executable as
`CFBundleExecutable` and supplies `MainModule=bd_to_avp.worker` for the
secondary launcher. Repository-only `README.md` and `pyproject.toml` files are
removed from the copied runtime.

## Commands

```sh
uv run python scripts/native_app.py generate
uv run python scripts/native_app.py test
uv run python scripts/native_app.py build
uv run python scripts/native_app.py package
```

`package` builds or updates the Briefcase staging app, builds the Xcode
`Release` configuration, copies the Python runtime into the production bundle,
signs nested Mach-O content from the inside out, verifies the complete
signature, launches the packaged app through `--startup-smoke`, and runs a real
`inspect_source` request through the embedded worker. The worker smoke also
requires canonical schema-v1 FFprobe observability in the protocol stream, so a
package cannot pass while silently falling back to a legacy child-process path.
Briefcase remains a runtime assembler; its Python GUI is no longer the shipping
interface.

Ad-hoc packaging is the default for local validation. Developer ID packaging
passes `--sign-identity` and `--sign-keychain`. Ad-hoc packages omit Hardened
Runtime because they have no Team ID for dyld library validation; Developer ID
packages retain Hardened Runtime.

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
`scripts/release.py prepare` updates the package version, `uv.lock`, Briefcase
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
3 exposes all four routes. Beta 4 (`0.3.0b4`, build `149`) and Beta 5
(`0.3.0b5`, build `150`) are published and immutable. Beta 6 (`0.3.0b6`, build
`151`) is the next prepared target for the guarded exact-SHA Prerelease
workflow. Its future cumulative item must sit above Beta 5, Beta 4, and Beta 3
and remain visible only to Beta and Alpha until a later newer Stable supersedes
it.

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

Beta 3, Beta 4, and Beta 5 publication are complete. Beta 6 signed installed-app
qualification must prove that:

- Beta 5 updates forward to Beta 6 on Beta and Alpha without changing the saved
  route;
- Stable and RC continue to exclude every Beta item;
- the production service rejects pre-v4 clients and accepts the signed Beta 6
  client; and
- a deliberate report can be submitted, retrieved by support code, assessed
  for privacy and diagnostic sufficiency, and deleted.
