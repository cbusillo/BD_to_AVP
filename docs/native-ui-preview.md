This is **v0.3.0-beta.1**, an opt-in preview of the new native macOS interface
for 3D Blu-ray to Vision Pro. It installs beside the current production app and
does not replace or update it.

## What To Try

- Launch the new SwiftUI workspace on an Apple Silicon Mac running macOS 26 or
  later.
- Choose a physical disc, ISO disc image, Blu-ray folder, MKV, MTS/M2TS
  transport stream, or a source folder containing several supported files.
- Inspect physical-disc, Blu-ray-folder, ISO, MKV, MTS, and M2TS source metadata
  through the bundled engine.
- Convert an inspected physical disc, Blu-ray folder, ISO, MKV, MTS, or M2TS source to a
  completed MV-HEVC `.mov` through the bundled engine, with native progress,
  cancellation, and results.
- During conversion and preview generation, confirm the app reports the current
  workflow stage and total stage count. MakeMKV source preparation also shows a
  determinate percentage from MakeMKV's own progress feed.
- Exercise the native recovery card if MakeMKV leaves a usable intermediate MKV
  or subtitle extraction cannot continue. Recovery creates a new one-off job
  without changing the selected profile or visible conversion defaults.
- Select a source folder and review its contextual queue. Confirm supported files
  are processed one at a time, failures remain visible and retryable, and Stop
  marks the active and not-yet-started items clearly.
- Review the Video, Audio & Subtitles, and Files & Recovery controls.
- Create, duplicate, edit, reorder, choose a default, and delete named encoding profiles.
- Open Settings with `Command-,`, resize the window, and scroll through the
  complete Profiles detail pane.
- Exercise light and dark appearance, keyboard navigation, and window
  restoration.

## Important Limits

- Source-folder queues run sequentially and are not restored after the app quits.
  Parallel workers, queue reordering, and persistent queue history are not part
  of this preview.
- Native conversion currently creates the full movie. Short sample outputs are
  not yet available.
- Stages without a trustworthy tool-reported denominator remain indeterminate.
  The activity heartbeat and elapsed time continue to show that those stages are
  running; the app does not infer an ETA from elapsed time or treat each stage as
  equal-duration work.
- This beta has no automatic updater. Future prerelease builds must be
  downloaded manually.
- Live playback of a file while it is still being generated is not supported.
  Finalized preview playback is planned separately.
- MakeMKV remains an external dependency for physical discs, Blu-ray folders,
  and disc images. The source workspace links to the official download page when
  MakeMKV is missing.
- Beta 1 supports Apple Silicon and macOS 26 or later. Earlier macOS releases
  are not supported.

Please report interface feedback in issue #202, including the source workflow,
window size, appearance, and any controls or terminology that were unclear.
