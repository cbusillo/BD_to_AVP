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

Create an ad-hoc signed review build containing the Briefcase-managed Python
runtime and conversion engine with:

```sh
uv run python scripts/native_app.py package
```

The native application targets Apple Silicon macOS 26 or later while remaining
buildable with the Xcode 27 SDK. Packaged release validation rejects a native
binary, embedded engine, or bundled Mach-O that requires a newer system.
