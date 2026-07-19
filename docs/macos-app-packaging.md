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

The Debug configuration uses a Development product and bundle identifier and
contains no Sparkle distribution metadata. It cannot enroll in either updater
channel.

The project version and repository build counter come from `pyproject.toml`.
`scripts/release.py prepare` updates the package version, `uv.lock`, Briefcase
build counter, and Xcode Release metadata atomically. The package command also
passes those canonical values directly to Xcode and rejects a bundle whose
identity differs.

Stable is the default Sparkle channel. Release Candidates are visible only to
installations that explicitly select the `rc` channel. Publishing an RC never
adds it to the stable channel. The first production candidate for the accepted
interface is reserved for `0.3.0rc1` with a build greater than stable build
`146`.

## Release Workflow

`.github/workflows/briefcase.yml` remains the sole production workflow and keeps
the stable workflow ID, PyPI Trusted Publisher binding, appcast history,
attestation checks, and guarded environment approval contract.

The package job runs on GitHub's Apple-Silicon `macos-26` runner. It selects
Xcode 26.5 build `17F42` explicitly and installs the XcodeGen 2.45.4 release
artifact only after verifying its committed SHA-256 digest. It:

1. verifies that protected `main` has not moved;
2. imports the Developer ID Application certificate into an ephemeral keychain;
3. builds and signs the production app;
4. notarizes and staples the app and DMG;
5. verifies production Sparkle metadata, signatures, Gatekeeper acceptance,
   bundled tools, and worker execution; and
6. uploads the exact DMG and `SHA256SUMS` for GitHub-hosted attestation.

A separate `macos-26` job downloads that exact notarized artifact and repeats
checksum, Gatekeeper, startup, bundled-tool, and worker validation before a
draft GitHub Release can be created. The existing downstream jobs then build
the channel-aware appcast, re-download and verify every release boundary,
publish the GitHub Release, optionally publish stable Python distributions, and
deploy the durable feed snapshot.

## Historical Prereleases

The retired side-by-side feedback lane published immutable alpha and beta
artifacts before the interface was accepted. The tags `native-ui-preview-1`,
`v0.3.0-beta.1`, and `v0.3.0-beta.2`, their assets, and their historical release
notes remain unchanged. They use a different bundle identifier and do not
update into the production application.

The separate publisher, release helper, build configuration, and workflow are
no longer active. Genuine bounded Preview conversion jobs and implementation
terms such as the native MVC splitter remain because they describe product
behavior and engine architecture rather than release branding.

## Remaining RC Evidence

Ship's physical-disc result still gates publishing `0.3.0rc1`. After that
evidence is accepted, the signed installed-app test in #197 must prove that:

- a normal stable installation does not discover RC1;
- a stable installation explicitly enrolled in Release Candidates can update
  to RC1 using the production bundle identity; and
- the final stable release can later supersede the RC for both channel choices.
