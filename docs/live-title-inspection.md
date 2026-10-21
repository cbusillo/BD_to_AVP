# Live Title Inspection

## Scope

The worker operation `inspect_live_title` tells the Mac whether a Blu-ray 3D ISO
or BDMV folder can play through [`start_live_hls`](live-hls-service.md), and
which playlist and audio streams to offer. It does not run MakeMKV, start a
stream, or write any file. It reads only playlist and clip metadata.

Request:

```json
{
  "protocol_version": 13,
  "type": "job.start",
  "job_id": "…",
  "operation": "inspect_live_title",
  "source": {
    "kind": "disc_image",
    "path": "/absolute/source.iso",
    "identity": {"device": 16777234, "inode": 123456789, "size_bytes": 41234567168,
                 "modification_seconds": 1760000000, "modification_nanoseconds": 123456789},
    "descriptor_handoff": {"socket_path": "/var/folders/…/T/bdtoavp-Ab12Cd/s", "token": "…"}
  }
}
```

The source must be an ISO or Blu-ray folder. The request has no destination, no
options, and no `title_id`. `identity` and `descriptor_handoff` are optional and
work as for `start_live_hls`
([v13 protocol](native-worker-protocol-v13.md#live-source-identity-and-descriptor-handoff)):
with a handoff the worker checks the received descriptor with `fstat`, runs
`ssif_probe titles /dev/fd/N`, and never opens the path. A Blu-ray folder that is a mounted disc image is read
from its backing ISO, as for the other operations.

## Selection

The worker checks `ssif_probe contract 2`, then runs `ssif_probe titles` (see
[direct-ssif-prototype.md](direct-ssif-prototype.md#commands)). The helper
decides each playlist's own eligibility before it bounds the listing, so
longer rejected playlists cannot push an eligible one out. For each listed
eligible playlist the worker applies the rules `start_live_hls` applies to the
playlist it streams:

- the helper's own eligibility: unencrypted, 3D, one angle, one complete
  SSIF-backed clip;
- the live video shape: progressive 720p or 1080p at a standard frame rate; and
- at least one audio stream FFmpeg reads as a raw elementary stream: Dolby
  Digital, Dolby Digital Plus, or DTS including DTS-HD.

Both services call the same functions in `live_hls_service.py` for the video
and audio rules.

The result is the longest playlist that passes. On a tie, the playlist
libbluray reports as the main feature wins, then the lower playlist number. A
3D disc often has a 2D and a 3D playlist over the same clip with equal length;
the main-feature flag picks the 3D one on the Rainforest disc.

When no playlist passes, the result reports the longest playlist the helper
accepted, so its reason is a live rule (`unsupported_live_video` or
`unsupported_live_audio`). If the helper accepted none, it reports the longest
playlist and the helper's reason, such as `encrypted_source_unsupported`,
`source_is_not_3d`, `multiple_angles_unsupported`,
`multiple_clips_unsupported`, `partial_clip_unsupported`, or
`ssif_unavailable`. A source with no playlist of at least 60 seconds reports
`no_live_titles`.

The helper lists eligible playlists longest first, so any eligible playlist the
listing bound left out is shorter than every listed one. When no listed playlist
passes and `eligible_count` shows an eligible playlist was left out, the result
is `live_title_search_incomplete` with a null playlist: the search did not
finish, so the source is not reported as unsupported.

## Result

The terminal `job.completed` payload is `live_title_result`:

```json
{
  "eligible": true,
  "unsupported_reason": null,
  "playlist": 1005,
  "duration_seconds": 3240.111867,
  "audio_streams": [
    {"pid": 4352, "coding_type": 134, "format": 6, "rate": 1, "language": "deu"},
    {"pid": 4353, "coding_type": 134, "format": 6, "rate": 1, "language": "eng"}
  ]
}
```

`audio_streams` lists only the supported streams of the chosen playlist, in
clip order, and is empty when the title is ineligible. `language` is the
three-letter code from the clip, or null. `playlist` and `duration_seconds` are
null only with `no_live_titles` and `live_title_search_incomplete`. The fields `pid`, `coding_type`, `format`, and
`rate` are the Blu-ray stream values `ssif_probe` reports.

## Failures

An ineligible title is a successful result, not a failure. The job fails with:

- `ssif_probe_missing` when the helper is absent;
- `ssif_probe_incompatible` when the helper reports another contract, or
  answers `titles` with a usage error (exit 2) because it predates the command;
- the helper's own code, such as `source_open_failed`, when it cannot read the
  source;
- `invalid_source_inspection` when the listing is not a valid `source.titles`
  schema 1 document; and
- `source_helper_timeout` when the helper does not answer within 120 seconds.

## Evidence

On October 20, 2026, an Apple M4 Max (macOS 27.2) ran the operation through the
worker entrypoint on the Rainforest ISO in about 0.06 seconds with a warm file cache.
It chose playlist 1005 (3,240.1 seconds) over the equal-length playlist 5, and
listed German and English DTS-HD MA (PIDs 4352 and 4353) as supported. The
`ssif_probe titles` command alone took 0.02 seconds and listed 36 playlists.
