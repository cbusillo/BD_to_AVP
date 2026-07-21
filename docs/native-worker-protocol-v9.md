# Native Worker Protocol v9

Protocol v9 adds an explicit audio-language selection field. The native app
defaults built-in and new profile options to preferred-only English, while an
explicit all-languages selection and legacy profile migrations preserve the v8
behavior. The native app and bundled Python worker ship atomically and both
require version 9.

Observability events, lifecycle rules, ownership, heartbeats, request size
limits, and terminal behavior are unchanged from
[Native Worker Protocol v8](native-worker-protocol-v8.md).

## Audio Encoding Object

Every conversion and preview request must include an `encoding.audio` object
with exactly these keys:

```json
{
  "mode": "automatic",
  "bitrate": 384,
  "preferred_language": "eng"
}
```

- `mode` remains `automatic`, `convert_aac`, or `pcm`.
- `bitrate` remains the AAC conversion or Automatic fallback bitrate.
- `preferred_language` is always present. `null` means retain all audio
  languages, which is the v8 behavior. A canonical ISO 639-2/T code such as
  `eng`, `jpn`, or `nld` enables preferred-language-only selection. The native
  app's built-in/new-profile default is `eng`.

The worker accepts supported alpha-2, alpha-3/B, alpha-3/T, and valid base
language tags, then canonicalizes them to alpha-3/T. It rejects an omitted
field, unsupported code, non-string/non-null value, or any unknown audio key.
Swift encodes an explicit all-languages selection as JSON `null`; omission is
not equivalent.

## Selection Policy

When `preferred_language` is `null`, every source audio stream is retained in
source order and the v8 processing behavior is unchanged.

When it contains a canonical language code, the worker:

1. Reads only the stream's language metadata; titles are never used to infer a
   language.
2. Retains every canonical metadata-language match in source order, including
   multiple main, alternate, or commentary tracks with the same language.
3. If no stream matches, retains the first source-default audio stream. If no
   stream declares default disposition, it retains the first audio stream.
4. Emits a visible structured `warning` event for that fallback. It never
   silently changes the policy to all languages and never intentionally
   produces a video without audio.

The same selected stream set drives Automatic qualification, AAC copy,
AAC conversion, PCM extraction, preview conversion, batch conversion, and the
final mux. Non-contiguous source streams are mapped explicitly. Track order,
language metadata, titles, channel layouts, and dispositions remain aligned
with the retained streams.

Automatic qualifies only retained streams. An excluded incompatible stream
cannot force the retained set from AAC copy to AAC conversion.

## Fallback Warning

A missing preferred language produces a warning shaped like:

```json
{
  "protocol_version": 9,
  "type": "warning",
  "job_id": "11111111-1111-4111-8111-111111111111",
  "sequence": 0,
  "payload": {
    "stage": "transcode_audio",
    "message": "No audio tracks matched the preferred language Japanese (jpn). Keeping the source-default audio track (English (eng)) instead.",
    "code": "audio_language_fallback",
    "preferred_language": "jpn",
    "selected_language": "eng",
    "selected_stream_index": 3,
    "selected_audio_position": 1,
    "fallback_reason": "source_default",
    "action": "keep_source_default_audio"
  }
}
```

`fallback_reason` is `source_default` or `first_stream`. The selected stream
index and audio position identify the retained fallback without relying on its
title.

## Restart Semantics

Prepare Audio and PCM extraction materialize only the selected stream set.
Restarting at Stage 8 (`CREATE_FINAL_FILE`) can remove tracks that are present
in an existing prepared audio artifact. It cannot restore a language already
removed by an earlier preparation run. If the requested preferred language is
not present in that artifact, Stage 8 applies the same default/first fallback
and emits the same structured warning rather than claiming that the original
source policy was restored. Preferred-only preparation records a hidden atomic
selection sidecar. If Stage 8 later requests all languages from a filtered
artifact, the worker emits `audio_languages_unrestorable_at_mux`, keeps every
track still available in the artifact, and identifies the earlier stage needed
to rebuild the complete source set.

## Compatibility

Protocol v8 requests omit `encoding.audio.preferred_language`, so v9 workers
reject them with `protocol_mismatch`. Protocol v9 requests and events are also
rejected by v8 readers. Historical v8 fixtures remain in the repository; v9
fixtures are the current shared Swift/Python contract.
