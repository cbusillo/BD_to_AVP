# Live HLS Service

## Scope

The worker operation `start_live_hls` turns an unencrypted, single-clip,
single-angle Blu-ray 3D ISO or BDMV folder into a bounded live MV-HEVC HLS
stream with the selected audio muxed as AAC. It writes only to its workspace.
It does not serve the stream over the network, pair a headset, or decrypt
anything; the relay reads the workspace.

Request:

```json
{
  "protocol_version": 13,
  "type": "job.start",
  "job_id": "…",
  "operation": "start_live_hls",
  "source": {"kind": "disc_image", "path": "/absolute/source.iso"},
  "destination": {"path": "/absolute/empty/workspace"},
  "live_hls": {
    "playlist": 1005,
    "audio_pid": 4353,
    "retention_seconds": 120,
    "retention_max_bytes": 268435456,
    "maximum_pairs": 14434
  }
}
```

`retention_seconds` is at least 30 and `retention_max_bytes` is at least
64 MiB. `maximum_pairs` is optional and bounds qualification runs. The source
must be an ISO or Blu-ray folder, and the workspace must be empty and outside
the source.

The worker emits `artifact.ready` with a `live_hls` payload naming the manifest
and playlist once the first segment, playlist, and manifest are durable. The
terminal `job.completed` payload is `live_hls_result`: the manifest and playlist
paths, earliest and latest available positions, segment counts, peak retained
bytes, and the encoder summary. Late cancellation does not remove a completed
stream, as with `start_live_source`.

## Admission

The service reuses the live source checks: `ssif_probe contract 2`, strict
inspection, and the selected audio PID inside the single eligible clip.
Encrypted, multi-clip, multi-angle, and incomplete-clip sources fail with the
inspection's unsupported reason. The service also requires:

- progressive 720p or 1080p base video at 23.976, 24, 25, 29.97, 50, or
  59.94 fps (`unsupported_live_video` otherwise); and
- selected audio that FFmpeg reads as a raw elementary stream: Dolby Digital,
  Dolby Digital Plus, or DTS including DTS-HD (`unsupported_live_audio`
  otherwise). LPCM and TrueHD are not supported.

## Pipeline

One `ssif_probe stream-service` read supplies both media types. An MVC record's
payload is the base-view Annex B followed by the dependent view, the same bytes
`stream-mvc` writes, so the worker forwards it unchanged.

```text
ssif_probe stream-service ─┬─ video ─> edge264_test -Osk ─> ffmpeg crop/hstack ─> mv-hevc-encoder ─> staging
                           └─ audio ─> ffmpeg (decode, stereo 48 kHz s16le) ─> FIFO ───┘
```

- The worker buffers records until it has the first video keyframe and the
  first selected-audio PES. Video time zero is that keyframe's PTS. The encoder
  receives the audio offset as `--audio-start-ticks` (90 kHz): leading audio is
  dropped and late audio starts at its offset, so sync comes from source PTS.
- Video is forwarded one whole GOP at a time. When `maximum_pairs` ends the
  source inside a GOP, that partial GOP is dropped so the decoder never emits a
  display-order gap.
- Each decoder input has its own feeder thread with a 64 MiB queue, so one busy
  decoder cannot stop the source read from serving the other.
- The encoder is `mv-hevc-encoder --hls-directory` with 2-second segments at
  20 Mbps plus `--audio-pcm` and `--audio-start-ticks`. It encodes PCM to
  192 kbps stereo AAC in the same fMP4 segments as the MV-HEVC video and trims
  audio at the last video frame.

Multichannel sources are downmixed to stereo by FFmpeg.

## Workspace

The encoder writes only to `.pipeline/encoder`. The worker owns the public
workspace and publishes in this order: rename the finished segment to its
public name, then atomically replace `media.m3u8` and `live-hls.json`, then
delete segments that were evicted. A client never sees a partial file or a
playlist naming a missing one.

```text
init.mp4
segment-000000000.m4s …
media.m3u8
live-hls.json
```

`media.m3u8` is a sliding-window live playlist. Because history is evicted, it
has no `EXT-X-PLAYLIST-TYPE` tag: an `EVENT` playlist must never remove
segments. `EXT-X-MEDIA-SEQUENCE` is the sequence of the first retained segment,
the target duration is the encoder's latched value, and `EXT-X-ENDLIST` is
added when the encoder finishes. There are no discontinuities.

`live-hls.json` lists the retained segments with sequence, URI, start, duration,
and size, the earliest and latest available positions in microseconds, the
retention limits, the retained bytes, and the video and audio stream identities.
`complete` becomes true with `EXT-X-ENDLIST`.

## Retention

After each publication, `init.mp4` plus the retained segments fit within
`retention_max_bytes`, and the retained span fits within `retention_seconds`.
Eviction removes whole segments from the front, so the earliest available
position is always a real segment start. Evicted files are deleted right after
the playlist and manifest that drop them are durable. The limit covers what is
published; on disk the workspace also holds the one segment being published
until those deletions finish.

The service fails instead of publishing when one segment cannot fit the byte
limit (`live_hls_segment_too_large`) or when the byte limit cannot hold three
target durations (`live_hls_retention_too_small`).

Production is not paced to a viewer. A producer faster than real time evicts
history at its own speed.

## Lifecycle

The worker remains the process-group owner of all five children. Cancellation,
any child failure, a stalled feeder, or a malformed encoder playlist stops every
child through the existing process-runner termination and kill path and
removes the whole workspace. Source reads, feeder writes, and the wait for
encoder output each use the 120-second no-progress deadline from the live
source service. On success the worker removes `.pipeline` after the final
playlist is published.

Failures report the stage that failed: `source_stream_failed` or the helper's
own code, `live_video_decode_failed`, `live_video_normalize_failed`,
`live_video_encode_failed`, `live_audio_decode_failed`, and
`live_hls_pipeline_stalled`.

## Tools

The worker resolves `ssif_probe`, `edge264_test`, `ffmpeg`, and
`mv-hevc-encoder` through the usual `BD_TO_AVP_<TOOL>_PATH` overrides, then the
bundled `bd_to_avp/bin`, then `PATH`. A development checkout builds the encoder
with `scripts/build_mv_hevc_encoder_macos.py` and sets
`BD_TO_AVP_MV_HEVC_ENCODER_PATH`.
