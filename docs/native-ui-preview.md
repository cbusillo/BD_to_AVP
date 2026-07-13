This is an opt-in preview of the new native macOS interface for 3D Blu-ray to
Vision Pro. It installs beside the current production app and does not replace
or update it.

## What To Try

- Launch the new SwiftUI workspace on an Apple Silicon Mac running macOS 26 or
  later.
- Choose a physical disc, ISO disc image, Blu-ray folder, MKV, or MTS/M2TS
  transport stream.
- Inspect physical-disc, Blu-ray-folder, ISO, MKV, MTS, and M2TS source metadata
  through the bundled engine.
- Convert an inspected physical disc, Blu-ray folder, ISO, MKV, MTS, or M2TS source to a
  completed MV-HEVC `.mov` through the bundled engine, with native progress,
  cancellation, and results.
- Exercise the native recovery card if MakeMKV leaves a usable intermediate MKV
  or subtitle extraction cannot continue. Recovery creates a new one-off job
  without changing the selected profile or visible conversion defaults.
- Review the Video, Audio & Subtitles, and Files & Recovery controls.
- Create, duplicate, edit, import, export, and delete named encoding profiles.
- Open Settings with `Command-,`, resize the window, and scroll through the
  complete Profiles detail pane.
- Exercise light and dark appearance, keyboard navigation, and window
  restoration.

## Important Limits

- **Start Processing supports one physical disc, Blu-ray folder, ISO, MKV, MTS,
  or M2TS source at a time.** Source-folder batch conversion is hidden in this
  preview and remains available only in the production app.
- Native conversion currently creates the full movie. Short sample outputs are
  not yet available.
- This preview has no automatic updater. Future preview builds must be
  downloaded manually.
- Live playback of a file while it is still being generated is not supported.
  Finalized preview playback is planned separately.
- MakeMKV remains an external dependency for physical discs, Blu-ray folders,
  and disc images. The source workspace links to the official download page when
  MakeMKV is missing.
- The preview supports Apple Silicon and macOS 26 or later. macOS 25 and earlier
  are not supported by this release candidate.

Please report interface feedback in issue #202, including the source workflow,
window size, appearance, and any controls or terminology that were unclear.
