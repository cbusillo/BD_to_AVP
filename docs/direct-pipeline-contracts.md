# Direct Pipeline Contracts

BD_to_AVP preserves named files as the durable contract between processing
stages while using fewer intermediates for a normal unattended run. Named files
remain important for users who stop at a stage, run an external AI upscaler,
inspect an intermediate, and later resume with `--start-stage`.

The design constraint is DRY: automatic minimum-materialization and
`--keep-files` reuse the same stage code and logical contracts. The difference
is whether an eligible stage output is streamed/reused or materialized as a
named restartable file.

## Current Stage Contracts

### `CREATE_MKV`

- Input: disc, ISO/image, MKV, MTS, or M2TS source plus selected title metadata.
- Output: the selected MKV/MTS/M2TS source in default mode; a copy in the
  per-title output folder with `--keep-files`; MakeMKV output for disc/images.
- Restart/debug value: high. This is the common resume point and MakeMKV
  boundary.

### `EXTRACT_MVC_AND_AUDIO`

- Input: MKV/MTS/M2TS source.
- Output: the source container as the logical MVC input in default mode; the
  source as audio input when AAC transcoding is enabled; PCM audio otherwise;
  `<name>_mvc.h264` and `<name>_audio_PCM.mov` with `--keep-files`.
- PCM extraction maps the shared audio selection explicitly. All-languages mode
  keeps the current complete stream set; preferred-only mode keeps every
  canonical metadata-language match in source order and preserves aligned
  titles, channel layouts, and dispositions.
- Restart/debug value: high. These files feed video split, audio mux/transcode,
  and subtitle timing context.

### `EXTRACT_SUBTITLES`

- Input: MKV source.
- Output: zero or more `.srt` files in the output folder.
- Restart/debug value: high. Subtitle files are user-visible and final mux
  inputs.

### `CREATE_LEFT_RIGHT_FILES`

- Input: streamed source container in default mode or extracted MVC elementary
  stream with `--keep-files`, plus crop/title metadata.
- MV-HEVC output: `<name>_left_movie.mov` and `<name>_right_movie.mov`.
- AV1 stereo output: `<name>_AV1-SBS-unmarked.mp4`, encoded directly from the
  native splitter's Y4M output without a lossy HEVC intermediate. The AV1
  sequence header carries limited-range BT.709 matrix, primaries, and transfer
  signaling.
- Restart/debug value: very high. External AI upscaling workflows often start
  here for MV-HEVC; AV1 resumes use the packed file.

### `COMBINE_TO_MV_HEVC`

- MV-HEVC input/output: left/right eye movie files become
  `<name>_MV-HEVC.mov`.
- AV1 input/output: `<name>_AV1-SBS-unmarked.mp4` receives Apple
  `vexu/eyes/pack` metadata and becomes `<name>_AV1-Stereo.mp4`. MP4Box imports
  that track into the final MOV.
- Restart/debug value: high. This creates the mode-specific final video input
  for audio/subtitle muxing.

### `UPSCALE_VIDEO`

- Input: MV-HEVC movie.
- Output: `<name>_MV-HEVC Upscaled.mov` when enabled.
- Restart/debug value: high. External and built-in upscale workflows need this
  boundary.

### `TRANSCODE_AUDIO` / Prepare Audio

- Input: source container for `automatic` and `convert_aac`; generated PCM audio
  movie for `pcm`.
- Output: `<folder>_audio_AAC.m4a` for `automatic` and `convert_aac`. The MPEG-4
  audio container is an owned artifact and preserves AAC decoder configuration
  when MP4Box imports the tracks into the final spatial movie. `pcm` keeps the
  existing generated `<folder>_audio_PCM.mov` behavior.
- `automatic` remuxes/copies the selected audio set only when every selected
  stream is qualified AAC with a supported profile, sample rate, and channel
  layout. If any selected stream is unqualified, including
  AC-3 or E-AC-3, the whole selected set is converted to AAC and the worker
  emits a structured warning.
- The same canonical selector drives Automatic qualification, AAC copy, and AAC
  conversion. A non-retained incompatible track cannot force AAC conversion,
  and non-contiguous retained streams are mapped explicitly.
- If preferred-only finds no metadata-language match, Prepare Audio retains the
  source-default stream or the first stream and emits the structured
  `audio_language_fallback` warning. It never silently widens to all languages.
- Older builds wrote `<folder>_audio_AAC.mov`. A resume directory containing
  only that legacy artifact must restart from Prepare Audio so the app can
  regenerate a compatible M4A instead of attempting an unsafe final mux.
- Restart/debug value: medium. This is the final mux input and useful for audio
  debugging.

### `CREATE_FINAL_FILE`

- Input: finalized MV-HEVC/upscaled or AV1 stereo movie, audio movie, and `.srt`
  files.
- Output: `<name>_AVP.mov` for MV-HEVC or `<name>_AV1_Stereo.mov` for AV1 inside
  the output folder.
- Final mux applies the same audio selector to prepared artifacts. A Stage 8
  restart can remove tracks still present in that artifact but cannot restore a
  language removed during an earlier preparation run. A missing preferred
  language therefore keeps the artifact's default/first audio track and emits
  the same visible fallback warning.
- Preferred-only preparation writes an atomic hidden selection sidecar beside
  the audio artifact. If Stage 8 later requests All Languages, the sidecar lets
  the worker warn that omitted source languages cannot be restored without
  restarting from Extract MVC and Audio (PCM) or Prepare Audio (AAC).
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

Default mode uses minimum materialization. `--keep-files` selects persistent
stage boundaries for restart, inspection, and external-tool workflows.

## Automatic Direct Pipeline

When `--keep-files` is absent, direct MKV/MTS/M2TS inputs reuse the original
source path, AAC transcoding reads audio from that source without an
intermediate PCM MOV, and MVC video is demuxed as Annex B into the native
splitter without an intermediate `.h264` file.

The MVC path is a bounded three-process pipeline: source FFmpeg to
`edge264_test` to encoding FFmpeg. MV-HEVC materializes left/right eye files;
AV1 directly materializes one packed software-encoded video. Subtitles, AAC,
the mode-specific finalized video, and the final movie remain named stage
artifacts. Default mode removes consumed intermediates after the final output
is complete; `--keep-files` retains them. Disc/ISO inputs retain their MakeMKV
materialization.

The reused source is user-owned. Automatic cleanup never deletes it.
`--remove-original` is the explicit exception and removes the source only after
the complete conversion succeeds. With `--keep-files`, the source is copied
into the per-title output folder and the durable stage contract applies. If the
selected source is already that retained copy, `--remove-original` explicitly
removes it while leaving the other durable stage artifacts intact.

The development-only [direct SSIF prototype](direct-ssif-prototype.md) explores
an unencrypted/decrypted Blu-ray source boundary without changing this
production contract. MakeMKV remains the supported disc materialization path
until the prototype satisfies its replay, multi-consumer, packaging, and
fallback promotion gates.

## Supported Direct Boundaries

### Source Reuse For Existing MKV/MTS/M2TS Inputs

For direct file inputs, default mode reuses the original source path as an
unowned artifact. `--keep-files` copies it into the output folder so downstream
stages have a durable source boundary.

That keeps downstream stage code path-based and avoids one large copy. The
ownership rule is the critical part: direct mode must never delete a user source
that was not created by BD_to_AVP.

### Native MVC Splitter To Stereo Encode

This boundary streams internally: source FFmpeg writes Annex B MVC to
`edge264_test` stdin, `edge264_test` writes Y4M to stdout, and encoding FFmpeg
either produces left/right HEVC eye movies or one full side-by-side AV1 movie.
The output files remain the restart boundary.

Left/right HEVC outputs remain the external-upscaler boundary. AV1 does not use
FX Upscale in its initial contract; its packed intermediate is retained after
completion only with `--keep-files`.

### Extracted Audio To Transcoded Audio

When `automatic` or `convert_aac` is selected, the Prepare Audio stage reads
audio from the MKV/MTS/M2TS source and writes the final owned M4A directly. MVC
video uses the same source container through the direct splitter pipeline.

The final mux still needs a seekable audio file, so the AAC M4A remains
materialized. `--keep-files` controls retention only and does not change the
selected audio policy. Direct-mode resumes at Prepare Audio can recreate AAC
from the source; resumes at the final mux require the owned prepared M4A to
already exist. As with durable resumes, video artifacts from earlier stages must
already exist when restarting after `EXTRACT_MVC_AND_AUDIO`.

### Final Move

`MOVE_FILES` is a filesystem-only resume boundary. It moves the completed movie
without replaying source inspection, audio preparation, or muxing. Owned source,
audio, and video artifacts remain available until that move succeeds; default
cleanup then removes the completed output folder. A failed move therefore keeps
all inputs needed for another final-mux or move attempt. Direct-file resumes use
the source stem to select the matching completed movie. If multiple other
completed movies are present, isolate the intended output folder before
resuming rather than guessing which artifact to move.

## Risky Boundaries

### FIFO Or Pipe Chaining

The direct MVC implementation uses anonymous subprocess pipes, not filesystem
FIFOs. The patched native splitter supports stdin with bounded NAL buffering;
regular-file input remains available for durable mode.

Constraints retained by the implementation:

- FFmpeg cannot write every container format, especially MOV, to a non-seekable
  output.
- Consumer failure can leave producers blocked or can surface as SIGPIPE.
- Concurrent producer/consumer orchestration is harder to cancel and clean up
  than the current sequential stage model.

The MVC supervisor closes inherited pipe handles, attributes cascade failures to
the originating process, restarts all three processes for the single-threaded
retry, and uses terminate/wait/kill cleanup with bounded waits. Filesystem FIFOs
remain outside the Python pipeline.

### MKV Materialization

The MKV remains the shared source for color-depth probing, crop detection,
MVC/audio extraction, subtitle OCR, and restart. Existing MKV/MTS/M2TS inputs
are reused in place by default, while disc/image inputs retain MakeMKV output as
a seekable source boundary.

### MVC And PCM Extraction

Durable mode and `--keep-files` retain the extracted MVC stream and PCM audio
because they decouple video split, audio processing, debugging, and restart.
Direct mode removes both intermediates during an unattended full run. The
source container remains available until the MVC split and direct audio
transcode have completed.

### Left/Right Eye Files

These are large, but they are user-facing workflow artifacts. External AI
upscaling users depend on stopping here. They are retained after completion
when `--keep-files` is enabled.

### MV-HEVC File

The MV-HEVC intermediate is the input to optional upscaling and final MP4Box
muxing. Default mode removes it after the completed movie moves successfully;
`--keep-files` retains it for Apple playback debugging and staged resumes.

### Subtitle Files

`.srt` files are named final-mux inputs. Default mode removes the completed
output folder after the final move succeeds; `--keep-files` retains subtitles
for debugging and resume workflows.

## Benchmark Evidence

- Reusing a 7.4 GB MKV took about 0.15 seconds and wrote no copy; durable mode
  took about 8.55 seconds and wrote 7.4 GB.
- A five-minute four-track direct audio transcode produced a 56 MB AAC file in
  about 9.9 seconds without PCM or partial artifacts.
- A real MVC container streamed through source FFmpeg, `edge264_test`, and
  encoding FFmpeg into matching 1920x1080 left/right HEVC outputs without an
  extracted `.h264` intermediate.
- Existing left/right outputs remain the external-upscaler and stage-resume
  checkpoint.

## Regression Coverage

- Durable mode keeps the current deterministic filenames for every stage.
- `--start-stage` continues to find the same expected files in durable mode.
- Direct mode with an existing MKV/MTS/M2TS source can reuse the source path
  without copying it into the output folder.
- Cleanup removes owned transient artifacts but never removes unowned source
  files.
- Subtitle extraction still produces SRT files discoverable by final mux.
- Audio transcode direct experiments preserve stream count, language metadata,
  and final mux command construction.
- All-languages mode preserves the historical stream set. Preferred-only tests
  cover canonical aliases, multiple same-language tracks, unknown tags,
  default-not-first fallback, non-contiguous mapping, preview/batch parity, and
  late-stage mux fallback.
- The MVC pipe has cancellation, timeout, retry, SIGPIPE cascade, and producer,
  splitter, and encoder failure-attribution tests.

Audio-language selection changes track composition and can change output size,
but it does not prove the cause of issue #202 and does not claim to resolve the
separate Ship's *Top Gun* stall report.
