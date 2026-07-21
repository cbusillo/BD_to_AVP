# Native Worker Protocol v7

> Historical protocol. The current contract is
> [Native Worker Protocol v9](native-worker-protocol-v9.md).

Protocol v7 adds an explicit video output mode and software AV1 quality to the
exact `encoding` schema. The native app and bundled Python worker ship
atomically and both require version 7.

Execution diagnostics use the separate, versioned
[structured observability contract](observability.md). Its schema evolves
independently from this control protocol.

## Video Output

Conversion and preview requests include `video_mode` and `av1_crf`:

```json
"encoding": {
  "audio": {
    "mode": "automatic",
    "bitrate": 384
  },
  "video_mode": "mv_hevc",
  "av1_crf": 32,
  "left_right_bitrate": 20,
  "link_quality": true,
  "mv_hevc_quality": 75,
  "upscale_quality": 75,
  "fov": 90,
  "frame_rate": "",
  "resolution": "",
  "crop_black_bars": false,
  "swap_eyes": false,
  "fx_upscale": false,
  "subtitles": {
    "mode": "preferred_plus_others",
    "preferred_language": "eng"
  }
}
```

Supported video modes are:

- `mv_hevc`: the existing native Apple spatial-video path. The worker encodes
  left and right HEVC eye files and combines them into MV-HEVC. This remains the
  default.
- `av1_sbs`: a software-encoded, full-resolution side-by-side AV1 stereo path.
  The worker encodes one `av01` track with `libsvtav1`, writes limited-range
  BT.709 signaling with FFmpeg's AV1 metadata bitstream filter, then adds
  Apple's codec-agnostic stereo metadata (`vexu` containing `eyes/stri` and
  `pack/pkin=side`) with MP4Box.

`av1_crf` accepts `0...63`; lower values preserve more detail and create larger
files. The default is `32`. The AV1 preset is intentionally fixed at `9` so the
first protocol surface exposes quality without creating an unstable matrix of
encoder-specific controls.

AV1 output is software-only. `fx_upscale = true` is rejected with
`video_mode = "av1_sbs"`; the existing FX Upscale stage currently targets the
MV-HEVC workflow. Eye swapping, symmetric black-bar cropping, frame-rate
override, audio policy, subtitle policy, cancellation, cleanup, final mux, and
resume semantics remain active.

The completed filename distinguishes the modes:

- MV-HEVC: `<name>_AVP.mov`
- AV1 stereo: `<name>_AV1_Stereo.mov`

## Stereo Metadata Contract

The AV1 output is not described as MV-HEVC, multiview-compressed video, or
Apple spatial video. On macOS 27, the finalized AV1 sample description exposes
both `HasLeftStereoEyeView` and `HasRightStereoEyeView`, reports
`ViewPackingKind = SideBySide`, and causes `AVAssetPlaybackAssistant` to return
`AVAssetPlaybackConfigurationOptionStereoVideo`. A bare AV1 control returns no
stereo playback option.

That proves an Apple-recognized packed-stereo asset contract. It does not, by
itself, prove stereoscopic rendering on every Apple Vision Pro generation;
device evidence remains tracked separately in issue #200.

## Audio Policy

The nested v6 audio policy is unchanged. Supported modes remain `automatic`,
`convert_aac`, and `pcm`. Prepared audio ownership, automatic AAC fallback,
track ordering, language normalization, title/default-disposition retention,
and move-stage recovery semantics are unchanged.

## Compatibility

The v7 `encoding` object has an exact schema. Protocol v6 requests do not
contain `video_mode` or `av1_crf` and are rejected. Persisted native profiles
are migrated atomically from profile schema v2 to v3 with
`videoOutputMode = "mv_hevc"` and `av1CRF = 32`, preserving prior behavior.

The numeric conversion stages remain unchanged for resume compatibility. AV1
jobs report `encode_av1_stereo` at stage 4 and `finalize_av1_stereo` at stage 5;
MV-HEVC jobs retain `create_left_right_files` and `combine_to_mv_hevc`.

Event ordering, source/title selection, size limits, heartbeat behavior, and
terminal-event meanings are unchanged from v6.
