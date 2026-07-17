# Native Worker Protocol v6

> Historical protocol. The current contract is
> [Native Worker Protocol v7](native-worker-protocol-v7.md).

Protocol v6 replaces the flat audio fields with an exact nested audio policy.
The native app and bundled Python worker ship atomically and both require
version 6.

## Audio Policy

Conversion and preview requests include an `audio` object inside `encoding`:

```json
"encoding": {
  "audio": {
    "mode": "automatic",
    "bitrate": 384
  },
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

Supported audio modes are:

- `automatic`: prepare one owned M4A. The worker remuxes/copies the selected
  audio set only when every selected audio stream is qualified AAC. If any
  selected stream is unqualified, the worker converts the whole selected set to
  AAC at `bitrate` and emits a structured `warning` event.
- `convert_aac`: convert the whole selected audio set to AAC at `bitrate` and
  prepare one owned M4A.
- `pcm`: keep the existing generated PCM audio movie behavior.

The initial automatic qualification is deliberately conservative and
deterministic. Only AAC streams with qualified AAC profiles, 44.1 or 48 kHz
sample rates, and known mono, stereo, 5.1, or 7.1 layouts are eligible for
copy/remux. AC-3 and E-AC-3 are explicitly excluded from the allowlist until
final-MOV, AVFoundation, seeking, channel-layout, and physical Apple Vision Pro
evidence proves they are safe.

`keep_files` controls artifact retention only. It must not change whether audio
is copied, converted to AAC, or decoded to PCM.

Final muxing receives an owned prepared audio artifact for `automatic` and
`convert_aac`; it never receives a user-owned source container. The mux keeps
audio track order and normalizes language metadata, and it preserves source
track titles and default disposition when the current FFmpeg probe data and
MP4Box import options expose them. Owned mux inputs remain available until the
completed movie moves successfully, so final-mux and move-stage retries do not
lose their recovery artifacts.

## Fallback Warning

Automatic whole-set fallback emits a structured warning similar to:

```json
{
  "type": "warning",
  "payload": {
    "stage": "transcode_audio",
    "message": "Automatic audio selected AAC conversion because one or more selected tracks are not qualified AAC.",
    "code": "audio_automatic_fallback_to_aac",
    "audio_mode": "automatic",
    "action": "convert_aac",
    "source_codecs": ["aac", "ac3"],
    "unqualified_streams": [
      {
        "index": 1,
        "codec": "ac3",
        "profile": null,
        "sample_rate": 48000,
        "channels": 6,
        "channel_layout": "5.1(side)",
        "reason": "codec_not_allowed"
      }
    ]
  }
}
```

## Compatibility

The v6 `encoding` object has an exact schema. The v5
`encoding.transcode_audio` and `encoding.audio_bitrate` fields are not accepted
in worker protocol requests. Legacy Python callers may still use the old
`--transcode-audio` boolean surface; the backend maps it to
`audio.mode = "convert_aac"` internally. The Qt desktop UI exposes the same
three explicit modes as the native app.

The stage identifier remains `transcode_audio` and the stage number remains 7
for resume compatibility. User-facing stage text is now **Prepare Audio**.

Event ordering, title selection, subtitle policy, size limits, heartbeat
behavior, and terminal-event meanings are unchanged from v5.
