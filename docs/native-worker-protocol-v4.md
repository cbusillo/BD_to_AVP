# Native Worker Protocol v4

> Historical protocol. Current audio-mode behavior is documented in
> [Native Worker Protocol v10](native-worker-protocol-v10.md).

Protocol v4 adds explicit 3D Blu-ray title discovery and selection while
preserving the single-request, single-process transport from v3. The native app
and bundled Python worker ship atomically and both require version 4.

## Source Inspection

Successful `inspect_source` results keep the existing flat metadata for the
recommended main movie and add a `titles` array. Direct MKV, MTS, and M2TS
sources return an empty array.

```json
{
  "name": "Feature 3D",
  "resolution": "1920x1080",
  "frame_rate": "24000/1001",
  "interlaced": false,
  "duration_seconds": 7200,
  "titles": [
    {
      "id": "makemkv:0",
      "name": "Main Movie",
      "output_name": "Feature 3D",
      "duration_seconds": 7200,
      "resolution": "1920x1080",
      "frame_rate": "24000/1001",
      "main_feature": true
    },
    {
      "id": "makemkv:2",
      "name": "3D Video 1",
      "output_name": "Feature 3D - 3D Video 1",
      "duration_seconds": 600,
      "resolution": "1920x1080",
      "frame_rate": "24000/1001",
      "main_feature": false
    }
  ]
}
```

Title IDs are opaque provider identifiers. Clients must return an ID exactly as
reported and must not infer MakeMKV title numbers from it. The recommended main
movie is listed first, followed by the remaining MVC titles from longest to
shortest. Inspection disables MakeMKV's configured minimum-title-length filter
so short 3D trailers and special features remain visible.

## Conversion Selection

Disc image, Blu-ray folder, and physical-disc conversion requests require the
selected title ID inside `source`:

```json
"source": {
  "kind": "disc_image",
  "path": "/absolute/path/feature.iso",
  "title_id": "makemkv:2"
}
```

`source.title_id` is forbidden for inspection and direct-file conversion. It is
required for disc-backed `convert_source` and disc-image `preview_source`
requests. The worker rescans the source and fails with `title_unavailable`
before creating output when the selected title no longer exists.

Disc-backed conversion and preview results repeat the selected ID as
`title_id`. The worker remains authoritative for the final output path. Main
movie output naming remains unchanged; additional videos receive unique
title-specific workspaces and filenames from their inspection metadata.

## Multi-Title Queue

The worker still converts exactly one title and returns exactly one output per
process. The native app creates one immutable job per selected title and runs
those jobs serially. This keeps cancellation, recovery decisions, completed
outputs, and retries isolated. A failed or cancelled title stops the remaining
queue; completed files remain available. Source removal is disabled for every
queued job except the final one.

Preview remains a single-title child job. Selecting multiple videos disables
preview until the user narrows the selection to one title.

## Compatibility

Mixed v1, v2, v3, and v4 processes fail with `protocol_mismatch` before media
inspection or destination mutation. Existing event ordering, size limits,
heartbeat behavior, and terminal-event meanings are unchanged.
