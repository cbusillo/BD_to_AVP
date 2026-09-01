# visionOS Sustained Playback Qualification

This is the host-side evidence contract for issue #705. The Swift recorder
emits strict JSON Lines; `scripts/qualify_visionos_sustained_playback.py` parses
them, evaluates acceptance, and writes a deterministic record. It computes
`evidence_summary`, every cell disposition, matrix acceptance, and
`record_sha256`; it does not merely collect metrics.

## Contract

The frozen contract is
`docs/qualification/visionos-sustained-playback-contract-v1.json`.

- `mvhevc-local-full` and `packed-sbs-local-full` are mandatory full-length
  cells and can never be unavailable in a passing record.
- `mvhevc-files-provider-sustained` and `packed-ou-local-sustained` are
  required when available. Valid optional unavailability reasons are
  `provider_unavailable`, `asset_unavailable`, and
  `redistribution_constraint`.
- Files-provider MV-HEVC must have the same media SHA-256 as local MV-HEVC;
  every sustained cell covers at least 1,800 seconds of active playback.
- Full-length cells must accumulate at least 98% of declared media duration as
  active playback or product-attributable waiting, in addition to reaching the
  natural end at 99.5% of the declared media duration.
- Sampled media time must advance by at least 98% of sustained coverage and 95%
  of full-length duration. A player that remains `.playing` while media time is
  wedged does not pass.
- Serious thermal without playback degradation is `accepted_limitation`.
  Critical thermal, serious thermal with degradation, item errors, failed
  completion, unacceptable waiting, memory growth, or a `no` observation is a
  product failure.
- Missing samples, all-null physical-footprint evidence, missing required
  events, malformed logs, insufficient active playback, clock-span mismatch,
  media-duration mismatch, or insufficient memory evidence are
  `evidence_failed` and fail the matrix.
- Full-length cells require an ordered user pause/resume and completed user
  seek. Automatic resume and eye-order restoration seeks do not satisfy that
  interaction. Packed cells additionally require an ordered completed
  eye-order change; any recorded eye-order change failure fails the cell even
  if a later retry succeeds.
- Waiting time is excluded only for the portion that overlaps a pause, seek,
  eye-order change, or inactive-scene control span; a control action cannot
  erase an earlier or later stall.
- Every log `run_id` must equal its matrix `case_id`; the log bundle ID,
  version, and build must match private app identity. The logged hardware model
  and parsed `Version … (Build …)` values must exactly match the private product
  type, OS version, and OS build. A single private log path cannot be reused by
  multiple cells.

The record persists sanitized identity only. Device identifiers and UDIDs are
used from private input but never persisted. The record embeds normalized,
schema-checked evidence fields so it can be revalidated without private source
files; private input paths and raw JSONL text are never copied. Privacy checks
recurse through objects and arrays and fail closed
on forbidden key names, paths, file URLs, security-scoped bookmark-looking
strings, control characters, and overlong strings.
`record_sha256` is a deterministic integrity checksum for independent
revalidation, not an authenticity signature.

## Host Commands

Run from the repository root with a connected Apple Vision Pro. Replace the
shell variables shown below. The recorder exists only in the binary built with
`SWIFT_ACTIVE_COMPILATION_CONDITIONS=BD_TO_AVP_QUALIFICATION`.

```sh
export DEVICE_ID='<connected-vision-pro-device-id>'
export BUNDLE_ID='com.shinycomputers.bd-to-avp.player'
export DERIVED_DATA="$PWD/macos/build/BDToAVPPlayerQualificationDerivedData"
export APP_PATH="$DERIVED_DATA/Build/Products/Release-xros/BDToAVPPlayer.app"
export PULL_ROOT="$PWD/qualification-evidence/$DEVICE_ID"
mkdir -p "$PULL_ROOT"

uv run python scripts/native_app.py generate
xcodebuild test \
  -project macos/BluRayToVisionPro.xcodeproj \
  -scheme BDToAVPPlayer \
  -configuration Debug \
  -destination 'platform=visionOS Simulator,name=Apple Vision Pro' \
  -derivedDataPath macos/build/BDToAVPPlayerQualificationDerivedData \
  -only-testing:BDToAVPPlayerTests/PlaybackQualificationRecorderTests \
  CODE_SIGNING_ALLOWED=NO \
  'SWIFT_ACTIVE_COMPILATION_CONDITIONS=$(inherited) BD_TO_AVP_QUALIFICATION'

xcodebuild build \
  -project macos/BluRayToVisionPro.xcodeproj \
  -scheme BDToAVPPlayer \
  -configuration Release \
  -destination "platform=visionOS,id=$DEVICE_ID" \
  -derivedDataPath "$DERIVED_DATA" \
  -allowProvisioningUpdates \
  'SWIFT_ACTIVE_COMPILATION_CONDITIONS=$(inherited) BD_TO_AVP_QUALIFICATION'

xcrun devicectl device install app --device "$DEVICE_ID" "$APP_PATH"
```

The recorder activates only when the selected library item matches the expected
item ID. For a file copied into the app's Documents directory, the item ID is
normally `documents:<lowercase-filename>`. For Files-provider sources, retrieve
`Library/Application Support/BDToAVPPlayer/library.json` from the app data
container to identify the stored item without exposing its bookmark.

Launch one cell with bounded evidence IDs and the exact expected library item:

```sh
export RUN_ID='mvhevc-local-full'
export MEDIA_ID='mvhevc-feature-a'
export ITEM_ID='documents:feature-a.mov'

xcrun devicectl device process launch \
  --device "$DEVICE_ID" \
  --terminate-existing \
  --environment-variables "{\"BD_TO_AVP_QUALIFICATION_RUN_ID\":\"$RUN_ID\",\"BD_TO_AVP_QUALIFICATION_MEDIA_ID\":\"$MEDIA_ID\",\"BD_TO_AVP_QUALIFICATION_ITEM_ID\":\"$ITEM_ID\"}" \
  "$BUNDLE_ID"
```

Wear the unlocked headset, select the expected item, and run the cell. Full
cells must include one pause/resume and one completed seek. Prefer a backward
seek; forward-skipped media does not count toward full-length progress, and a
forward skip beyond 5% of the declared duration invalidates the cell. Packed
cells must also complete an eye-order change. Let full-length playback reach
its natural end. Use Done only after at least 1,800 seconds of active playback
for sustained cells; extend the run for pauses or excluded control intervals.
These paths write the required footer automatically.

Use the exact matrix case ID as `RUN_ID`. A recorder never overwrites an
existing case log. Pull and preserve completed evidence before retrying; clear
the app data container before intentionally repeating the same case ID.

Logs are stored under the app's Application Support directory. Pull a completed
cell with the app data domain:

```sh
xcrun devicectl device copy from \
  --device "$DEVICE_ID" \
  --domain-type appDataContainer \
  --domain-identifier "$BUNDLE_ID" \
  --source "Library/Application Support/BDToAVPPlayer/PlaybackQualification/$RUN_ID.jsonl" \
  --destination "$PULL_ROOT/$RUN_ID.jsonl"
```

Validate a log, assemble the record, and independently revalidate it:

```sh
uv run python -m scripts.qualify_visionos_sustained_playback \
  validate-log --log "$PULL_ROOT/mvhevc-local-full.jsonl" --json
uv run python -m scripts.qualify_visionos_sustained_playback \
  assemble --input "$PULL_ROOT/private-input.json" --output "$PULL_ROOT/qualification-record.json"
uv run python -m scripts.qualify_visionos_sustained_playback \
  validate-record --record "$PULL_ROOT/qualification-record.json" --json
```

`assemble` and `validate-record` return exit code `0` only when the matrix is
accepted. A structurally valid but rejected matrix is still printed and returns
exit code `2`, so shell automation cannot mistake evidence failure for a pass.

`xctrace` collection is currently offline and is not a prerequisite. Wearer
private full-length media is required for both full-length cells; short public
fixtures do not establish the full-length acceptance claim.

The product binary built without the qualification compilation condition
contains no recorder. Do not describe a normal Release build as producing
qualification logs.

Raw JSONL keeps the header first and footer last. Samples and events are
normalized by their captured time during validation because AVPlayer callbacks
can be written a few milliseconds out of capture order.

## Private Input Example

This shape-only example uses fake IDs and SHAs. The `log_path` values are
private operator input consumed only during assembly and never appear in the
resulting record.

```json
{
  "generated_at": "2026-09-01T12:00:00Z",
  "contract_version": "visionos-sustained-playback-v1",
  "app_identity": {
    "repo_commit": "cccccccccccccccccccccccccccccccccccccccc",
    "bundle_id": "com.example.player",
    "version": "1.0",
    "build": "1",
    "app_tree_sha256": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
    "xcode_version": "Xcode 26.5",
    "qualification_compile_condition": "BD_TO_AVP_QUALIFICATION"
  },
  "device_identity": {
    "identifier": "private-device-identifier",
    "udid": "private-udid",
    "product_type": "RealityDevice1,1",
    "os_version": "27.0",
    "os_build": "24M000",
    "transport": "usb",
    "developer_mode": true
  },
  "cells": [
    {
      "case_id": "mvhevc-local-full",
      "media": {
        "media_id": "fake-mv",
        "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "size_bytes": 1000,
        "duration_seconds": 7200,
        "codec_tag": "mv-hevc",
        "stereo_format": "mv-hevc"
      },
      "source_class": "local_documents",
      "coverage": "full_length",
      "log_path": "/private/operator-only/mvhevc-local-full.jsonl",
      "battery": {
        "start_percent": 95,
        "end_percent": 70,
        "charging": false,
        "low_power_interruption": false
      },
      "observations": {
        "picture": "yes",
        "depth": "yes",
        "eye_order": "yes",
        "audio_sync": "yes"
      }
    },
    {
      "case_id": "packed-sbs-local-full",
      "media": {
        "media_id": "fake-sbs",
        "sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "size_bytes": 1000,
        "duration_seconds": 7200,
        "codec_tag": "hevc",
        "stereo_format": "side-by-side"
      },
      "source_class": "local_documents",
      "coverage": "full_length",
      "log_path": "/private/operator-only/packed-sbs-local-full.jsonl",
      "battery": {
        "start_percent": 95,
        "end_percent": 70,
        "charging": false,
        "low_power_interruption": false
      },
      "observations": {
        "picture": "yes",
        "depth": "yes",
        "eye_order": "yes",
        "audio_sync": "yes"
      }
    },
    {
      "case_id": "mvhevc-files-provider-sustained",
      "media": {
        "media_id": "fake-mv-provider",
        "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "size_bytes": 1000,
        "duration_seconds": 7200,
        "codec_tag": "mv-hevc",
        "stereo_format": "mv-hevc"
      },
      "source_class": "files_provider",
      "coverage": "sustained",
      "unavailable_reason": "provider_unavailable",
      "battery": {
        "start_percent": 60,
        "end_percent": 30,
        "charging": false,
        "low_power_interruption": false
      },
      "observations": {
        "picture": "yes",
        "depth": "yes",
        "eye_order": "yes",
        "audio_sync": "yes"
      }
    },
    {
      "case_id": "packed-ou-local-sustained",
      "media": {
        "media_id": "fake-ou",
        "sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "size_bytes": 1000,
        "duration_seconds": 7200,
        "codec_tag": "hevc",
        "stereo_format": "over-under"
      },
      "source_class": "local_documents",
      "coverage": "sustained",
      "unavailable_reason": "asset_unavailable",
      "battery": {
        "start_percent": 60,
        "end_percent": 30,
        "charging": false,
        "low_power_interruption": false
      },
      "observations": {
        "picture": "yes",
        "depth": "yes",
        "eye_order": "yes",
        "audio_sync": "yes"
      }
    }
  ]
}
```
