This is an opt-in preview of the new native macOS interface for 3D Blu-ray to
Vision Pro. It installs beside the current production app and does not replace
or update it.

## What To Try

- Launch the new SwiftUI workspace on an Apple Silicon Mac running macOS 27.
- Choose a physical disc, ISO disc image, Blu-ray folder, MKV, source folder, or
  MTS/M2TS transport stream.
- Inspect ISO, MKV, MTS, and M2TS source metadata through the bundled engine.
- Convert an inspected ISO, MKV, MTS, or M2TS source to a completed MV-HEVC `.mov`
  through the bundled engine, with native progress, cancellation, and results.
- Review the Video, Audio & Subtitles, and Files & Recovery controls.
- Create, duplicate, edit, import, export, and delete named encoding profiles.
- Open Settings with `Command-,`, resize the window, and scroll through the
  complete Profiles detail pane.
- Exercise light and dark appearance, keyboard navigation, and window
  restoration.

## Important Limits

- **Start Processing currently supports ISO, MKV, MTS, and M2TS files only.**
  Keep the production app installed for physical discs, Blu-ray folders,
  source-folder batches, and recovery decisions that require a second
  interactive step.
- Native conversion currently creates the full movie. Short sample outputs are
  not yet available.
- This preview has no automatic updater. Future preview builds must be
  downloaded manually.
- Live playback of a file while it is still being generated is not supported.
  Finalized preview playback is planned separately.
- MakeMKV remains an external dependency for physical discs and disc images.
- The preview supports Apple Silicon and macOS 27 only.

Please report interface feedback in issue #202, including the source workflow,
window size, appearance, and any controls or terminology that were unclear.
