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

The source-agnostic Mac-to-Vision-Pro relay advertises protocol version 3. It
uses X25519 and transcript-bound HKDF to show both devices the same six-digit
numeric-comparison code. The Mac commits its nonce before Vision Pro contributes
its key material, then reveals it for client verification; this prevents server
nonce grinding. A single provisional candidate has a 60-second window and needs
an authenticated Vision Pro confirmation plus an exact candidate-bound Mac
approval before any playlist or media route is available. The unpaired relay
advertises for at most ten minutes, rotating its ephemeral challenge every two
minutes while idle. Every protected HTTP
response carries a bounded HMAC header binding server role, request nonce,
status, and body SHA-256; clients verify it before interpreting content.

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

Create an ad-hoc signed package containing the pinned embedded Python runtime
and conversion engine with:

```sh
uv run python scripts/native_app.py package
```

Ad-hoc packages omit Hardened Runtime because ad-hoc signatures have no Team ID
for dyld library validation; Developer ID packages retain Hardened Runtime. The
package gate launches the signed Swift host with `--startup-smoke`, smokes the
embedded conversion worker, verifies cooperative cancellation can reap a
separate-session child and remove its preview workspace, and then performs
strict deep signature validation.

The app and engine use worker protocol v12. Audio and subtitle language controls
are independent: built-in and new profile options default to preferred-only
English audio, while existing profile choices remain unchanged and version-1
through version-3 profiles migrate to all-languages behavior. Profile document
version 5 stores stable route-relative quality intent separately from retained
Custom controls while continuing to write concrete compatibility fields.
Profile document version 6 stores encoding options plus only reusable-intermediate
policy and software-encoder pipeline defaults. Version-1 through version-5 profiles
migrate without carrying restart, overwrite, source-removal, continue-on-error, or
diagnostic command-output defaults forward. Version-1 through version-4 profiles
migrate losslessly for encoding settings: exact production
defaults become `Balanced`, while every other combination remains `Custom`.
Mapping version 2 resolves all seven checked direct positions, only `Balanced`
for generated MV-HEVC, and `Balanced` plus `Detailed` for stage-6 file upscale.
Unsupported positions remain visible but unavailable; `Custom` restores the
independently retained expert settings. These mappings remain a candidate until
#422 completes package, media, runtime, physical-device, and signed-beta gates.
Expert edits activate `Custom`, while returning to a guided step preserves the
retained snapshot. Direct-route summaries include exact direct quality and the
concrete generated fallback only for `Balanced` or `Custom`; resolved fallback
reports show separate requested and selected rows, and stage-6 existing-artifact
upscale shows only its active quality.
Protocol v12 projects exact direct quality plus a concrete generated fallback
only when direct capability selection can validly require it. Eligible
automatic MV-HEVC jobs use the packaged direct encoder during stage 4, while
reusable intermediates, software encoding, incompatible upscale geometry, and
restart workflows retain the generated/file-backed route. A valid unavailable
capability result preserves `Balanced` or Custom intent, visibly reports requested
and selected settings before media inspection, and preview child jobs use the
same route contract as full conversions. Non-Balanced guided direct jobs fail
before input when direct capability is unavailable rather than aliasing to
generated `Balanced`.
Preferred-only audio keeps every metadata-language match and visibly falls
back to the source-default or first audio stream when no match exists. MKV,
MTS, M2TS, ISO, and Blu-ray-folder sources can create an isolated beginning,
middle, or end preview child job with the current resolved profile. Direct-file
previews use the bounded app cache. ISO and Blu-ray-folder previews use a hidden,
capacity-checked per-job workspace on the selected destination so full-title
preparation does not consume the Mac startup volume. The finalized result stays
leased from that workspace while the embedded AVPlayer is open and is removed
when the preview closes.
See `docs/native-worker-protocol-v12.md` for the request, event, and ownership
contract.

The application targets Apple Silicon macOS 26 or later and uses the pinned
Xcode 26.5 release toolchain for production packaging. Packaged validation rejects a Swift
binary, embedded engine, or bundled Mach-O that requires a newer system.

See [macOS UI Acceptance](../docs/macos-ui-acceptance.md) for the current
profile, appearance, accessibility, window-size, and screenshot evidence.

See [visionOS Playback Validator](../docs/visionos-playback-validator.md)
for the isolated RealityKit companion target and physical-headset validation flow.

See [visionOS Player](../docs/visionos-player.md) for the standalone movie
library, persistent source-access contract, MV-HEVC playback behavior, and
player-specific validation commands.
