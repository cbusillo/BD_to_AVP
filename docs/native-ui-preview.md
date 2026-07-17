This is **v0.3.0-beta.2**, an opt-in preview of the native macOS interface for
3D Blu-ray to Vision Pro. It installs beside the current production app and
does not replace or update it.

## What's New Since Beta 1

- Select and convert multiple 3D titles from one source instead of being limited
  to the longest title.
- Process supported source folders through a sequential contextual queue with
  visible retry and stop behavior.
- Generate bounded beginning, middle, or end previews and review completed
  samples before running a full conversion.
- Create, duplicate, reorder, and choose default encoding profiles in native
  Settings.
- Search a canonical subtitle-language catalog with aliases and explicit
  subtitle modes.
- Choose Automatic, Convert to AAC, or Uncompressed PCM audio handling with
  visible whole-set fallback warnings when Automatic cannot copy every selected
  track safely.
- Choose the default native Apple spatial MV-HEVC output or an optional
  software-encoded, full-resolution side-by-side AV1 stereo export.
- Benefit from hardened worker cancellation, restart, cleanup, title metadata,
  audio-track naming, and final-mux recovery behavior.

## What To Try

- Launch the new SwiftUI workspace on an Apple Silicon Mac running macOS 26 or
  later.
- Choose a physical disc, ISO disc image, Blu-ray folder, MKV, MTS/M2TS
  transport stream, or a source folder containing several supported files.
- Inspect physical-disc, Blu-ray-folder, ISO, MKV, MTS, and M2TS source metadata
  through the bundled engine.
- Convert an inspected physical disc, Blu-ray folder, ISO, MKV, MTS, or M2TS
  source to a completed stereo `.mov` through the bundled engine, with native
  progress, cancellation, and results.
- During conversion and preview generation, confirm the app reports the current
  workflow stage and total stage count. MakeMKV source preparation also shows a
  determinate percentage from MakeMKV's own progress feed.
- Exercise the native recovery card if MakeMKV leaves a usable intermediate MKV
  or subtitle extraction cannot continue. Recovery creates a new one-off job
  without changing the selected profile or visible conversion defaults.
- Select a source folder and review its contextual queue. Confirm supported files
  are processed one at a time, failures remain visible and retryable, and Stop
  marks the active and not-yet-started items clearly.
- Review the Video, Audio & Subtitles, and Files & Recovery controls. Exercise
  the three audio modes and confirm any Automatic fallback warning describes
  the actual whole-set action.
- Compare the default MV-HEVC output with AV1 Stereo (Software). Confirm AV1 is
  presented as packed stereo rather than native Apple spatial video, and record
  encode time, output size, and playback behavior on the intended Mac or player.
- For one selected MKV, MTS/M2TS, or ISO title, generate beginning, middle, and
  end preview samples and verify completed samples play, seek, and preserve eye
  order.
- Create, duplicate, edit, reorder, choose a default, and delete named encoding
  profiles.
- Search for subtitle languages by names, aliases, and codes, then verify saved
  profiles restore the canonical choice and subtitle mode.
- Open Settings with `Command-,`, resize the window, and scroll through the
  complete Profiles detail pane.
- Exercise light and dark appearance, keyboard navigation, and window
  restoration.

## Important Limits

- Source-folder queues run sequentially and are not restored after the app quits.
  Parallel workers, queue reordering, and persistent queue history are not part
  of this preview.
- Stages without a trustworthy tool-reported denominator remain indeterminate.
  The activity heartbeat and elapsed time continue to show that those stages are
  running; the app does not infer an ETA from elapsed time or treat each stage as
  equal-duration work.
- This beta has no automatic updater. Future prerelease builds must be
  downloaded manually.
- Live playback of a file while it is still being generated is not supported.
  Completed bounded previews can be reviewed after their worker job finishes.
- Bounded preview generation is limited to one selected MKV, MTS/M2TS, or ISO
  title. It is not available for physical discs, Blu-ray folders, source-folder
  queues, or multi-title selections.
- Automatic is the default for new and built-in profiles after the four-mode
  physical Apple Vision Pro audio checklist completed in issue #56. Existing
  custom profiles keep their saved audio choice.
- AV1 encoding is software-only. The output is Apple-recognized side-by-side
  stereo, not MV-HEVC or native Apple spatial video; playback support and
  performance vary by device generation and player.
- MakeMKV remains an external dependency for physical discs, Blu-ray folders,
  and disc images. The source workspace links to the official download page when
  MakeMKV is missing.
- Beta 2 supports Apple Silicon and macOS 26 or later. Earlier macOS releases
  are not supported.

Please report general prerelease and physical-disc feedback in issue #202.
The completed four-mode headset audio checklist is recorded in issue #56.
Include the source type, selected titles, profile, output mode, recovery action,
and relevant app activity when reporting a conversion problem.
