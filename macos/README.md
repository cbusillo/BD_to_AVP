# Native macOS Application

This directory contains the SwiftUI/AppKit review build tracked by issue #198.
It targets direct distribution and does not add an App Store
target, sandbox entitlements, or a second release matrix.

The Release build is Apple-Silicon-only because the existing Briefcase
runtime and bundled media tools are arm64.

The checked-in source of truth is `project.yml`. Generate the local Xcode
project with:

```sh
uv run python scripts/native_app.py generate
```

Build and test the shell with:

```sh
uv run python scripts/native_app.py test
uv run python scripts/native_app.py build
```

The project exact-pins Sparkle 2.9.4 through Swift Package Manager. Debug and
Preview builds omit direct-distribution update metadata, never start Sparkle,
and retain the manual GitHub Releases fallback. The Release configuration uses
`Info-Release.plist` for the policy-checked direct appcast metadata.

Create an ad-hoc signed review build containing the Briefcase-managed Python
runtime and conversion engine with:

```sh
uv run python scripts/native_app.py package
```

Ad-hoc packages omit Hardened Runtime because ad-hoc signatures have no Team ID
for dyld library validation; Developer ID packages retain Hardened Runtime. The
package gate launches the signed native host with `--startup-smoke`, smokes the
embedded conversion worker, and then performs strict deep signature validation.

The native application targets Apple Silicon macOS 26 or later while remaining
buildable with the Xcode 27 SDK. Packaged release validation rejects a native
binary, embedded engine, or bundled Mach-O that requires a newer system.
