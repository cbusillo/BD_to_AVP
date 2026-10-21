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
  "source": {
    "kind": "disc_image",
    "path": "/absolute/source.iso",
    "identity": {
      "device": 16777234,
      "inode": 123456789,
      "size_bytes": 41234567168,
      "modification_seconds": 1760000000,
      "modification_nanoseconds": 123456789
    },
    "descriptor_handoff": {"socket_path": "/var/folders/…/T/bdtoavp-Ab12Cd/s", "token": "…"}
  },
  "destination": {"path": "/absolute/empty/workspace"},
  "live_hls": {
    "playlist": 1005,
    "audio_pid": 4353,
    "retention_seconds": 120,
    "retention_max_bytes": 268435456,
    "maximum_pairs": 14434,
    "viewer_lead_seconds": 30
  }
}
```

`retention_seconds` is at least 30 and `retention_max_bytes` is at least
64 MiB. `maximum_pairs` is optional and bounds qualification runs. The source
must be an ISO or Blu-ray folder, and the workspace must be empty and outside
the source.

`viewer_lead_seconds` is optional, from 10 through half of
`retention_seconds`; it turns on viewer pacing (see Backpressure).
`source.identity` is optional and is what the host saw when it authorized the
file. `source.descriptor_handoff` is how the Mac hands the worker the ISO it
opened under the approved folder's security scope, so the worker needs no
consent of its own to read a network share or a protected folder; see
[the v13 protocol](native-worker-protocol-v13.md#live-source-identity-and-descriptor-handoff).
The worker checks the identity before inspection and again before the source
read. With a handoff, both checks are `fstat` on the received descriptor, and
`ssif_probe` reads it as `/dev/fd/N`; the worker never opens the path. Without
one, the path must not be a symbolic link, and the file opened without
following links must match. A mismatch fails with `live_source_changed`. The
request shape is in `tests/fixtures/native_worker_start_live_hls_v13.json`.

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
returns true is a deliberate hold, like the grace hold.

With `viewer_lead_seconds`, viewer pacing adds a third deliberate hold. The
host sends the viewer's playhead with `job.viewer_position`
([native-worker-protocol-v13.md](native-worker-protocol-v13.md)); the worker
holds while the published edge is at least the lead past the latest position,
or past zero before the first report. An advancing position releases it on the
next poll. This hold only stops the source read, like the others, so it cannot
block publication or grace expiry: the grace hold still releases on wall-clock
time and staging still drains, and a held viewer always has the lead's worth of
published media ahead of it. A paused viewer holds production indefinitely
without tripping any no-progress deadline, and cancellation still reaches the
children promptly. The Mac relay uses a 30-second lead with 120-second and
256 MiB retention, which leaves about 75 seconds behind the viewer for rewind
at 20 Mbps.

The manifest carries a `producer` object so producer capacity can be measured
apart from pacing: `produced_microseconds` (the published edge),
`wall_seconds` since production started, `held_seconds` (all deliberate
holds), and `viewer_held_seconds` (the viewer hold's share). Unheld throughput
is `produced_microseconds / ((wall_seconds - held_seconds) * 1e6)`. The values
are as of the manifest's last publication. The terminal result also reports
`viewer_held_seconds`.

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

## Relay

The Mac serves a live stream through the authenticated relay
(`macos/BluRayToVisionPro/Relay/Live/`). Each stream runs its own worker on a
fresh `~/Library/Caches/<bundle>/LiveHLS/<stream-id>` workspace with 120-second
and 256 MiB retention and a 30-second viewer lead, and sends `source.identity`
from the source lease.

- **Following.** The relay polls `live-hls.json` every 250 ms and rereads it only
  when its inode, size, or modification time changed. It decodes the manifest
  strictly (at most 4 MiB, schema 1, `init.mp4`, public segment names,
  consecutive sequences, a contiguous timeline, durations within the target
  plus 0.5 s) and refuses a manifest whose media sequence or latest position
  goes back, whose completion reverts, or that retimes a known URI. Any
  violation fails the stream with `live_hls_manifest_invalid`.
- **Serving.** Only `init.mp4`, the retained segments, and segments the
  manifest still lists as retired are served; a retired segment the worker has
  unlinked is a 404. Files are opened without following links.
- **Playlist.** `LiveHLSPlaylistRenderer` renders the playlist and the
  `playlist.json` snapshot from the same window. URIs are root-relative under
  `/relay/v1/media/`, there is no `EXT-X-PLAYLIST-TYPE`, `EXT-X-START:TIME-OFFSET=0`
  starts at the earliest retained segment, and `EXT-X-ENDLIST` follows
  completion. Snapshot times convert segment boundaries, not durations, to
  milliseconds, so segments stay contiguous at 23.976 fps.
- **Pacing.** The player posts its playhead to `/relay/v1/control/position`.
  The stream clamps it to the window and forwards it as `job.viewer_position`
  when it moved at least 500 ms, at most once a second. With no report for
  10 seconds it uses the start of the most recently requested segment.
- **Lifecycle.** A worker failure or a manifest violation is a signed 503
  `stream_failed` carrying the worker's code and message, and starts cleanup at
  once; the failure stays visible until the stream is stopped. A stopped stream
  is a signed 410 `stream_ended` with the stop reason. A stream stops as `idle`
  60 seconds after its last verified request (120 seconds before the first) and
  as `sourceChanged` when its path no longer names the leased file (checked
  every 5 seconds). Stopping invalidates at once; one cleanup cancels the
  worker, kills its process group 10 seconds later if needed, and waits for the
  worker's confirmed exit with no timeout before it removes the workspace and
  releases the lease. Every caller awaits that cleanup, so a replacement never
  starts beside a worker that is still running. An exit still unconfirmed 5
  seconds after the kill is reported as an overdue drain. The Mac holds an
  idle-sleep assertion while the stream can serve.

The developer-probe `RelayHost` live mode starts one stream once a headset
pairs. Its live routes verify only under the session derived for that stream
(`RelayEstablishedSession.liveStreamSession(streamID:)`), media also needs the
derived media capability, any other stream ID gets a signed 410
`not_current`, a start that throws is a signed 503 `stream_failed` on every
live route, and `stop`, `cancel`, and app quit wait for the stream's cleanup.

On October 21, 2026, the relay at commit `93b16d75` served the Rainforest ISO
(playlist 1005, PID 4353) for 330 seconds to a simulated viewer that signed every
request under the derived session, kept a 20-second forward buffer, reported its
playhead each second, and paused for 45 seconds. The first segment was available
1.3 seconds after the stream started. The viewer fetched 151 segments (764 MB)
with no rebuffer and no listed segment missing. After the first 30 seconds the
published edge stayed 27.7 to 33.4 seconds ahead of the playhead, including
during the pause. The worker held for 253.9 of 329.2 seconds, 253.0 of them for
the viewer, and produced 313.9 seconds of media, an unheld throughput of 4.17x.
The workspace peaked at 547 MB. Stopping took 0.13 seconds and left no workspace
and no pipeline process. In that run the worker was started from a shell and
connected to the relay over FIFOs, because the test app had not been granted
access to the network volume holding the ISO; see the task evidence.

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
