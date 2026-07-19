# Native Worker Protocol v5

> Historical protocol. Current audio-mode behavior is documented in
> [Native Worker Protocol v8](native-worker-protocol-v8.md).

Protocol v5 replaces the three legacy subtitle fields with one explicit,
backend-neutral subtitle policy. The native app and bundled Python worker ship
atomically and both require version 5.

## Subtitle Policy

Conversion and preview requests include a `subtitles` object inside `encoding`:

```json
"encoding": {
  "transcode_audio": false,
  "audio_bitrate": 384,
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

Supported modes are:

- `off`: skip subtitle extraction. `preferred_language` must be `null`.
- `preferred_only`: extract only subtitle tracks matching the preferred
  language, including forced tracks in that language.
- `preferred_plus_others`: extract all detected subtitle languages while
  retaining the preferred language for the client-facing policy.

`preferred_language` accepts ISO 639 alpha-2, alpha-3/B, alpha-3/T, and a base
language with an IETF region or script suffix. The worker normalizes accepted
values to lowercase ISO 639-2/T before conversion. `und` and non-language
values are rejected for user preference.

## Language Catalog

The shared catalog is checked in as
`bd_to_avp/resources/iso639_languages.json`. It contains the 414 individual
and macrolanguage entries from the ISO 639-2 data bundled with Babelfish 0.6.1.
Special-purpose values such as `mis`, `mul`, `und`, and `zxx` are not selectable.

Known bibliographic and terminology alternatives normalize to one canonical
code, including `fre` to `fra`, `ger` to `deu`, `dut` to `nld`, and `chi` to
`zho`. Source metadata that cannot be resolved is written as `und` and shown as
Unknown instead of changing the requested subtitle policy.

## Media Behavior

Disc, image, folder, and direct-file workflows apply subtitle filtering after
the source MKV is available. MakeMKV retains all source tracks, and subtitle
policy never removes or reorders audio tracks.

When `preferred_only` finds no matching PGS subtitle track, conversion
continues without subtitles and emits a warning. This is distinct from an OCR
failure after matching tracks were selected, which keeps the existing subtitle
recovery decision.

Subtitle filenames may use alpha-2 or alpha-3 suffixes. Final MOV metadata is
normalized to ISO 639-2/T, including languages without alpha-2 codes.

## Compatibility

The v5 encoding object has an exact schema. The legacy `skip_subtitles`,
`language_code`, and `remove_extra_languages` keys are not accepted in v5.
Mixed v1 through v4 clients and v5 workers fail with `protocol_mismatch` before
media inspection or destination mutation. Event ordering, title selection,
size limits, heartbeat behavior, and terminal-event meanings are unchanged
from v4.
