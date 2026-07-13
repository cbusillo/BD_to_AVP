# Native Application Packaging

Issue #198 establishes a direct-distribution SwiftUI application with a bundled
Python conversion engine. It does not create an App Store target, enable App Sandbox, add
security-scoped bookmarks, or introduce a second release pipeline.

## Build Layout

The checked-in Xcode source is `macos/project.yml`; the generated `.xcodeproj`
is intentionally ignored. `scripts/native_app.py` coordinates XcodeGen, Xcode,
the existing Briefcase runtime staging path, signing, and a real worker smoke.

```text
3D Blu-ray to Vision Pro Native Preview.app/
└── Contents/
    ├── MacOS/
    │   ├── 3D Blu-ray to Vision Pro Native Preview   SwiftUI/AppKit application
    │   └── BluRayToVisionProEngine    Briefcase Python launcher
    ├── Frameworks/
    │   └── Python.framework
    └── Resources/
        ├── app/                bd_to_avp source and bundled tools
        ├── app_packages/       Python dependencies
        └── support/
```

The containing `Info.plist` keeps the normal Swift executable as
`CFBundleExecutable` and supplies `MainModule=bd_to_avp.worker` for the
secondary Briefcase launcher.

## Commands

```sh
uv run python scripts/native_app.py generate
uv run python scripts/native_app.py test
uv run python scripts/native_app.py build
uv run python scripts/native_app.py package
```

`package` builds or updates the existing Briefcase staging app, builds the
native `Preview` configuration, copies the Python runtime into the native bundle, signs
nested Mach-O content from the inside out, verifies the complete signature, and
runs `inspect_source` against a generated M2TS file through the packaged worker.
It uses ad-hoc signing by default. Set `BD_TO_AVP_NATIVE_SIGN_IDENTITY` or pass
`--sign-identity` to exercise a Developer ID identity.

The package command always produces the side-by-side preview identity
`com.shinycomputers.bd-to-avp.native-preview` with marketing version `0.3.0`
and preview-local build number `1`. It cannot overwrite the production app.

The auxiliary Python launcher is signed with the same direct-distribution
entitlements already required by the Briefcase launcher: unsigned executable
memory for CPython and disabled library validation for Python extension modules.
Those entitlements belong only to the engine executable, not the SwiftUI app.
They reinforce why this direct-distribution build is not an App Store target.

The direct application is Apple-Silicon-only. Release builds and the native host,
engine launcher, and bundled FFprobe executable are verified as `arm64` so an
Intel Mac cannot launch a shell whose processing runtime is incompatible.

## Proven And Deferred

This slice proves that the native executable and embedded Python launcher can
coexist in one directly signed app and that the packaged worker can execute the
real FFprobe-backed source inspection path without a repo checkout or system
Python.

Release assembly removes the development checkout path from `Info.plist` and
marks the bundled worker as required. A packaged app with a missing worker fails
closed instead of falling back to `uv` or a local repository.

Release builds also fail closed in the Swift launcher itself when the bundled
engine is absent, remove repository-only `README.md` and `pyproject.toml` files
from the copied runtime, strip inherited `PYTHON*`, `DYLD_*`, and `BD_TO_AVP_*`
overrides from the engine environment, and remap Swift source paths so local
checkout locations are not embedded in the native executable.

The review build deliberately reuses the full Briefcase runtime, including
dependencies needed by the current GUI. A later optimization may build a
worker-only runtime after the native shell exercises more representative engine
paths; slimming it now would create a third dependency graph before the worker
surface is known.

`Publish Native UI Preview` is the isolated distribution workflow for native UI
feedback builds. It runs only from the current protected `main` commit, uses the reviewed
`macos-signing` environment, Developer-ID signs and notarizes both the app and
DMG, staples and Gatekeeper-validates the result, repeats the packaged-worker
smoke from the mounted DMG, attests the artifact, and publishes only the DMG and
`SHA256SUMS` as a GitHub prerelease.

The native shell targets macOS 27. Review and release builds use the Xcode 27 SDK
without a compatibility deployment path for older macOS releases.

GitHub-hosted runners do not yet provide Xcode 27. Required CI therefore pins
the `macos-26` runner and overrides the deployment target to macOS 26 only for
the native test invocation. This is a compile/test
compatibility gate, not a supported deployment target: local review packages and
all eventual release artifacts continue to build with Xcode 27 for macOS 27.

The preview release job therefore requires an ephemeral Apple Silicon
self-hosted runner labeled `bd-to-avp-release`, running macOS 27 with Xcode 27
and XcodeGen 2.45.4 already installed. The protected-main workflow never applies the
CI deployment-target override. The runner is release infrastructure, not a
general pull-request runner, and should be registered only for the bounded
dispatch then removed after the job exits.

## Release Placement

The native shell does not yet replace the current `0.2.x` Briefcase/Sparkle
release line. Native Start Processing supports inspected Blu-ray folders, ISO,
physical disc, MKV, MTS, and M2TS sources; batch, interactive recovery, and the
native updater path still need release-level completion before promotion through
the normal Release Candidates appcast.

The preview uses a separate product name and bundle identifier, installs beside
the production app, targets Apple Silicon macOS 27, and is published only as a
GitHub prerelease. It does not mutate Sparkle Pages, either appcast channel,
GitHub latest, or PyPI. Release identity is derived from the committed native
version and monotonically increasing build number. Version `0.3.0` build `1`
uses tag `native-ui-preview-1` and title
`v0.3.0 (Build 1) — Native UI Preview`. Repeated runs may resume only a matching
draft with byte-identical assets, and fail closed after publication. Published
tags and assets remain immutable even if the human-readable title or notes need
a metadata-only correction.

The first native build eligible to replace production is reserved for
`0.3.0rc1`. That candidate must use a production `CFBundleVersion` greater than
the current build `146`, carry real conversion through the structured worker
boundary, and have an intentional native update path. A maintenance RC on the
existing app line would instead be `0.2.144rc1` build `147` and would continue to
package the Briefcase app.
