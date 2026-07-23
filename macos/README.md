# macOS Application

This directory contains the production SwiftUI/AppKit application with its
bundled Python conversion engine. It targets direct distribution and does not
add an App Store target, sandbox entitlements, or a second release matrix.

The Release build is Apple-Silicon-only because the embedded Python runtime and
bundled media tools are arm64.

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

The project exact-pins Sparkle 2.9.4 through Swift Package Manager. Debug builds
use a separate Development identity, omit direct-distribution update metadata,
never start Sparkle, and retain the manual GitHub Releases fallback. The Release
configuration uses the production identity and `Info-Release.plist` for the
policy-checked direct appcast metadata.

Release builds expose four persisted update routes on that one production feed:

| Route | Additional Sparkle channels |
| --- | --- |
| Stable | none (`{}`) |
| RC | `{rc}` |
| Beta | `{beta, rc}` |
| Alpha | `{alpha, beta, rc}` |

Stable is the default for missing or unknown values. Existing Stable/RC values,
including the legacy `releaseCandidate` spelling, migrate to the canonical
preference. Selecting a safer route applies only to future newer builds;
Sparkle never downgrades the currently installed version.

Create an ad-hoc signed package containing the Briefcase-managed Python
runtime and conversion engine with:

```sh
uv run python scripts/native_app.py package
```

Ad-hoc packages omit Hardened Runtime because ad-hoc signatures have no Team ID
for dyld library validation; Developer ID packages retain Hardened Runtime. The
package gate launches the signed Swift host with `--startup-smoke`, smokes the
embedded conversion worker, and then performs strict deep signature validation.

The app and engine use worker protocol v9. Audio and subtitle language controls
are independent: built-in and new profile options default to preferred-only
English audio, while existing version-4 custom choices remain unchanged and
version-1 through version-3 profiles migrate to all-languages behavior.
Profile document version 4 also stores explicit MV-HEVC bitrate intent while
continuing to write the legacy quality, eye-bitrate, and linkage keys for one
stable rollback window. Legacy eye bitrate 20 migrates to Automatic with 20
preserved as the inactive custom value; other legacy values migrate to Custom.
Preferred-only audio keeps every metadata-language match and visibly falls
back to the source-default or first audio stream when no match exists. MKV,
MTS, M2TS, and ISO
sources can create an isolated beginning, middle, or end preview child job with
the current resolved profile. The finalized result is leased from the preview
cache while the embedded AVPlayer is open and removed when the preview closes.
See `docs/native-worker-protocol-v9.md` for the request, event, and ownership
contract.

The application targets Apple Silicon macOS 26 or later and uses the pinned
Xcode 26.5 release toolchain for production packaging. Packaged validation rejects a Swift
binary, embedded engine, or bundled Mach-O that requires a newer system.

See [macOS UI Acceptance](../docs/macos-ui-acceptance.md) for the current
profile, appearance, accessibility, window-size, and screenshot evidence.

See [visionOS Playback Validator](../docs/visionos-playback-validator.md)
for the isolated RealityKit companion target and physical-headset validation flow.
