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

To learn whether a source can play live before starting it, the worker
operation `inspect_live_title` applies these same rules to every listed
playlist; see [live-title-inspection.md](live-title-inspection.md).

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
public name, then atomically replace `media.m3u8` and `live-hls.json`.
Evicted segments are retired rather than deleted (see Retention). A client never
sees a partial file or a playlist naming a missing one.

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
It also lists `retired_segments` (URI, size, and `delete_after_unix`),
`retired_bytes`, and `retired_allowance_bytes`. `complete` becomes true with
`EXT-X-ENDLIST`.

## Retention

After each publication, `init.mp4` plus the retained segments fit within
`retention_max_bytes`, and the retained span fits within `retention_seconds`.
Eviction removes whole segments from the front, so the earliest available
position is always a real segment start.

RFC 8216 section 6.2.2 requires a removed segment to stay available for its
duration plus the duration of the longest playlist that contained it. An
evicted segment therefore stays on disk as a retired segment until that grace
period, measured in wall-clock time, ends, and is never deleted earlier. The
publisher deletes it on its first poll after the deadline.

Retired bytes are bounded by `retired_allowance_bytes`: `retention_max_bytes`
plus two of the largest segments seen so far. At realtime production a retired
segment lives for about one window plus one segment, so this allowance holds
the steady state.

## Backpressure

The worker stops reading the source, which leaves `ssif_probe` blocked on its
output pipe, whenever any of these holds:

- publishing the next staged segment would push retired bytes past the
  allowance (a grace hold, released when the oldest retired segment expires);
- the media forwarded to the decoder but not yet published reaches two
  segment durations (the largest published segment, or 2 seconds before the
  first); or
- two completed segments are waiting in `.pipeline/encoder`.

The last two keep staging at no more than two completed segments: the encoder
only receives what fits that budget, plus at most the GOP that was being
forwarded. With nothing pacing it to a viewer, production runs as fast as the
pipeline allows until the published window and the retired allowance fill,
then settles near real time as grace periods expire.

Disk use is at most `retention_max_bytes` published, `retention_max_bytes` plus
two segments retired, two completed segments in staging, and the one segment
the encoder is writing, plus the small playlist and manifest temporaries:
about twice `retention_max_bytes` plus five segments. Frames inside the
decoder, normalizer, and encoder are in memory, not on disk. Retired segments
still within their grace period when the stream completes stay on disk and in
the manifest; whoever owns the workspace afterward removes them, or removes
the workspace.

`SSIFLiveHLSService.hold_conditions` is a list of callables. Any condition that
returns true is a deliberate hold, like the grace hold. Viewer-position pacing
in the relay can add its own condition there.

The service fails instead of publishing when one segment cannot fit the byte
limit (`live_hls_segment_too_large`) or when the byte limit cannot hold three
target durations (`live_hls_retention_too_small`).

## Lifecycle

The worker remains the process-group owner of all five children. Cancellation,
any child failure, a stalled feeder, or a malformed encoder playlist stops every
child through the existing process-runner termination and kill path and
removes the whole workspace. Source reads, feeder writes, and the wait for
encoder output each use the 120-second no-progress deadline from the live
source service. The encoder-output deadline runs alongside production from the
encoder's start and each real publication, so an encoder that keeps accepting
input but stops publishing fails the job without waiting for the end of the
source. During a deliberate hold the source is not read and no feeder write
waits, and the publisher counts the hold as progress, so no deadline fires.
Waiting for room because the encoder has not yet published does not count as
progress, so a real encoder stall still fails. Cancellation is checked on every
hold poll and reaches the children promptly. On success the worker removes `.pipeline` after the final
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

## Evidence

On October 20, 2026, an Apple M4 Max (128 GB, macOS 27.2) ran this operation at
the code in this document's commit on the Rainforest ISO: playlist 1005, English DTS-HD MA 5.1
PID 4353, 120-second and 256 MiB retention, and `maximum_pairs` 14,434.

- The first segment, playlist, and manifest were ready 1.24 seconds after the
  worker started.
- Until the published window and retired allowance filled, production ran at
  2.50x real time (212 seconds of media in the first 85 seconds). After that,
  grace holds paced it to about real time, and all 14,424 frames (601.6
  seconds) took 501.5 seconds. The worker held the source for 303.8 seconds in
  73 grace holds. The process tree peaked near 160% CPU and 0.9 GB resident.
- 301 segments were published. The byte limit bound retention: published
  history peaked at 268,425,124 bytes, retired segments at 280,588,043 bytes,
  staging at two segments (11,115,818 bytes), and the sampled workspace at
  557,152,058 bytes. At completion the window was 497.9 to 601.6 seconds.
- A separate monitor saw all 194 retired-segment deletions happen after their
  grace deadlines, none early.
- FFprobe found MV-HEVC view IDs `0,1` and stereo 48 kHz AAC with no audio gap.
  Every source audio PTS step was one 512-sample DTS frame, and the encoder
  wrote 601.601 seconds of audio against 601.6016 seconds of video.

The same encoder build at commit `1cc3ef4b` also produced this
playback evidence; later commits changed publication, retirement, and
source backpressure, not the encoder:

- AVFoundation read the first video and audio samples at 0.0. A synthetic
  flash and click measured -0.7 ms and +0.4 ms audio-minus-video offsets.
- The first and last 28 seconds played through the production loopback bridge
  (`RelayLoopbackHTTPServer`) with video and audio tracks and decoded frames in
  every two-second interval.

FFprobe without edit-list handling reports video starting at 0.127 seconds
because of B-frame composition offsets; AVFoundation does not.
