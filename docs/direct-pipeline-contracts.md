# Direct Pipeline Contracts

BD_to_AVP currently uses named files as the contract between processing stages.
That is simple, debuggable, and important for users who stop at a stage, run an
external AI upscaler, inspect an intermediate, and later resume with
`--start-stage`.

Issue #126 investigates whether a one-shot mode can reduce disk writes without
creating a second conversion pipeline. The design constraint is DRY: direct mode
must reuse the same stage code and stage contracts. The only difference should
be whether a stage output is materialized as a named restartable file or treated
as an ephemeral stream/artifact for a full unattended run.

## Current Stage Contracts

### `CREATE_MKV`

- Input: disc, ISO/image, MKV, MTS, or M2TS source plus selected title metadata.
- Output: largest MKV/MTS/M2TS in the per-title output folder.
- Restart/debug value: high. This is the common resume point and MakeMKV
  boundary.

### `EXTRACT_MVC_AND_AUDIO`

- Input: MKV/MTS/M2TS source.
- Output: `<name>_mvc.h264` and `<name>_audio_PCM.mov`.
- Restart/debug value: high. These files feed video split, audio mux/transcode,
  and subtitle timing context.

### `EXTRACT_SUBTITLES`

- Input: MKV source.
- Output: zero or more `.srt` files in the output folder.
- Restart/debug value: high. Subtitle files are user-visible and final mux
  inputs.

### `CREATE_LEFT_RIGHT_FILES`

- Input: extracted MVC elementary stream plus crop/title metadata.
- Output: `<name>_left_movie.mov` and `<name>_right_movie.mov`.
- Restart/debug value: very high. External AI upscaling workflows often start
  here.

### `COMBINE_TO_MV_HEVC`

- Input: left/right eye movie files.
- Output: `<name>_MV-HEVC.mov`.
- Restart/debug value: high. This is the Apple spatial video intermediate and
  final mux input.

### `UPSCALE_VIDEO`

- Input: MV-HEVC movie.
- Output: `<name>_MV-HEVC Upscaled.mov` when enabled.
- Restart/debug value: high. External and built-in upscale workflows need this
  boundary.

### `TRANSCODE_AUDIO`

- Input: PCM audio movie.
- Output: `<folder>_audio_AAC.mov` when enabled.
- Restart/debug value: medium. This is the final mux input and useful for audio
  debugging.

### `CREATE_FINAL_FILE`

- Input: MV-HEVC/upscaled movie, audio movie, and `.srt` files.
- Output: `<name>_AVP.mov` inside the output folder.
- Restart/debug value: high. This is the MP4Box subtitle/audio/video mux
  boundary.

### `MOVE_FILES`

- Input: final file inside output folder.
- Output: final file in the output root.
- Restart/debug value: low. This is filesystem placement only.

## Materialization Policy

The current named files are not accidental implementation details. They are the
stage contract. A direct pipeline should preserve those contracts and add an
explicit materialization decision per stage:

- `persistent`: always write the named file because later stages, restart,
  external tools, or user inspection depend on it.
- `ephemeral`: a one-shot run may keep the artifact temporary or streamed, but
  the stage still exposes the same logical output to downstream code.
- `stream-only`: allowed only when both producer and consumer can share one
  process pipeline without losing restart/debug behavior for normal mode.

Default mode remains persistent. Direct mode should be opt-in until benchmarks
prove that it is safe and worth the complexity.

## Internal Source-Reuse Prototype

The first runtime prototype uses an internal direct-pipeline toggle. It is
intentionally hidden from the supported CLI surface until benchmark evidence
and the final UX decision are available. Its scope is narrow: direct
MKV/MTS/M2TS inputs reuse the original source path instead of copying the source
into the per-title output folder.

This does not create a second processing pipeline and does not stream later
stage boundaries. Downstream stages continue to consume paths, subtitles remain
persistent in the output folder, disc/ISO inputs retain their existing MakeMKV
materialization, and restart/external-upscaler artifacts remain unchanged.

The reused source is user-owned. Cleanup and `--remove-original` do not delete
it while this prototype is active. The supported CLI surface and boundaries
remain subject to the benchmark and UX decisions tracked by GitHub issue #126.

## Likely Safe Boundaries

### Source Reuse For Existing MKV/MTS/M2TS Inputs

For direct file inputs, `CREATE_MKV` currently copies the source into the output
folder and then downstream stages read that copy. A direct mode can likely reuse
the original source path as an unowned artifact instead.

That keeps downstream stage code path-based and avoids one large copy. The
ownership rule is the critical part: direct mode must never delete a user source
that was not created by BD_to_AVP.

### Native MVC Splitter To Left/Right Encode

This boundary already streams internally: `edge264_test` writes Y4M to stdout and
FFmpeg consumes it to produce left/right eye movie files. The output files remain
important because they are the main external upscaler/restart boundary.

Initial work here should focus on measuring and tightening the existing stream,
not removing left/right outputs.

### Extracted Audio To Transcoded Audio

When AAC transcoding is enabled, BD_to_AVP writes PCM audio and then reads it
back to produce AAC. This may be a good direct-mode candidate after source reuse
is understood. The final mux still needs an audio file, so the likely win is
avoiding the PCM file in one-shot transcode runs, not removing audio
materialization completely.

### Final Move

`MOVE_FILES` can stay as a filesystem operation. There is little value in making
it direct because it does not create a large intermediate by itself.

## Risky Boundaries

### FIFO Or Pipe Chaining

Named pipes are tempting because they can make existing path-based stage
functions talk to each other without changing every call site. Treat them as an
experiment, not the starting architecture.

Risks to prove first:

- `edge264_test` may require a regular or seekable `.264` file.
- FFmpeg cannot write every container format, especially MOV, to a non-seekable
  output.
- Consumer failure can leave producers blocked or can surface as SIGPIPE.
- Concurrent producer/consumer orchestration is harder to cancel and clean up
  than the current sequential stage model.

The safer first step is transient files with explicit ownership and cleanup.
Only use FIFOs after a focused probe proves the exact producer and consumer pair
can tolerate them.

### MKV Materialization

The MKV is currently the shared source for color-depth probing, crop detection,
MVC/audio extraction, subtitle OCR, and restart. It is also the boundary most
likely to change if MakeMKV is replaced later. Do not optimize this away in this
issue.

### MVC And PCM Extraction

The extracted MVC stream and PCM audio are large, but they decouple video split,
audio processing, subtitle timing context, and restart. Streaming MVC directly
from MKV extraction into the native splitter may be possible later, but it would
couple several failure domains and should not be the first prototype.

### Left/Right Eye Files

These are large, but they are user-facing workflow artifacts. External AI
upscaling users depend on stopping here. Direct mode must not remove this
boundary from the standard staged pipeline.

### MV-HEVC File

The MV-HEVC intermediate is the input to optional upscaling and final MP4Box
muxing. It is also useful for Apple playback debugging. Treat it as persistent
until final-mux benchmarks prove there is a safe replacement.

### Subtitle Files

`.srt` files are both final mux inputs and debugging artifacts. Keep them named
and persistent for now.

## Benchmark Plan

Use existing artifacts on the external volume to avoid re-ripping discs while
measuring the expensive boundaries:

- Representative manual workspace:
  `/Volumes/Docker-External/BD_to_AVP_artifacts/bd-to-avp-125/manual`
- Rainforest fallback workspace:
  `/Volumes/Docker-External/BD_to_AVP_artifacts/rainforest-main-probe-test`

Record, per run:

- wall-clock time
- peak disk usage in the output folder
- size of each stage artifact
- whether the stage can be resumed with `--start-stage`
- whether external upscaling can still use the same files

## Recommended First Prototype

Do not add a user-facing direct mode immediately. First, introduce a small
internal vocabulary for stage artifacts and materialization decisions. A useful
prototype would be a pure planning/refactor step:

1. Define each stage's logical output in one place.
2. Keep current filenames and behavior unchanged.
3. Add tests that prove `--start-stage` still resolves the same expected files.
4. Add ownership tests proving unowned source files are never deleted.
5. Only after that, experiment with one ephemeral boundary behind tests.

This keeps MakeMKV replacement compatible with #126. A future source-ingest
implementation can produce the same logical MKV/source artifact contract, while
the rest of the pipeline keeps using shared stage code.

## Tests Before Runtime Changes

- Durable mode keeps the current deterministic filenames for every stage.
- `--start-stage` continues to find the same expected files in durable mode.
- Direct mode with an existing MKV/MTS/M2TS source can reuse the source path
  without copying it into the output folder.
- Cleanup removes owned transient artifacts but never removes unowned source
  files.
- Subtitle extraction still produces SRT files discoverable by final mux.
- Audio transcode direct experiments preserve stream count, language metadata,
  and final mux command construction.
- Any FIFO experiment has cancellation and failure tests for both producer and
  consumer processes.
