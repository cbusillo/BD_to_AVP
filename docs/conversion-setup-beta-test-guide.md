# Conversion Setup Beta Test Guide

## Purpose

This Beta keeps the source-first Conversion Setup checks and adds focused
coverage for folders containing Blu-ray ISO images. It asks whether Main
Feature versus All 3D Videos is clear, selected disc titles become distinct
sequential conversions, retries remain safe, and non-disc files still convert
exactly once. It is not a test of the future first-class persistent queue.

Recruit three to five testers when possible:

- one existing user with custom Profiles;
- one clean-install or built-in-Profile user;
- one user converting an inserted disc, ISO, or Blu-ray folder.

## Before Installing

1. Quit every running Stable, Beta, RC, Alpha, Development, or retired Preview
   copy of 3D Blu-ray to Vision Pro.
2. Record the currently installed app version.
3. Back up the following folder before opening the Beta:

   ```text
   ~/Library/Application Support/3D Blu-ray to Vision Pro/
   ```

   The most important files are `profiles.json`, `queue.json`, and
   `resolution-memory.json`. Copy the entire folder so rollback preserves the
   original Profile library and queue state together.
4. Confirm MakeMKV is installed at one of the supported locations when testing
   physical discs, disc images, or Blu-ray folders:

   ```text
   /Applications/MakeMKV.app
   /Applications/MakeMKV/MakeMKV.app
   ```
5. Use non-sensitive test media when possible. Do not include movie titles,
   personal paths, license keys, or other private information in written
   feedback.

## Required Test Journeys

### 1. First Conversion

1. Choose a supported source.
2. Confirm the setup clearly identifies the source, selected Result,
   destination, and expected outcome.
3. Open the Result menu and confirm `Recommended`, `Smaller File`,
   `Higher Quality`, and `4K Upscale` are understandable choices.
4. Change the destination once, then restore the intended destination.
5. Run Preview if practical.
6. Start one short conversion and confirm the finished movie is created.

Record whether the primary action was obvious and whether any technical wording
required explanation.

### 2. Repeated Conversion

1. Choose a second source that should use the same unchanged Result.
2. Confirm the app returns to a calm setup state without requiring every option
   to be reviewed again.
3. Start the conversion without opening the editor.

Record whether the repeated run felt like one action and whether the retained
Result choice was expected.

### 3. Advanced Settings

1. Open **Advanced Settings…**, change at least one setting, then Cancel or
   press Escape.
2. Confirm the main setup summary returns to its original state.
3. Open **Advanced Settings…** again, make a change, and apply it only to the current
   conversion.
4. Confirm the main setup identifies the conversion as customized.
5. If using a custom Profile, test **Update Profile**. Otherwise test
   **Save as New Profile…**.
6. On Video, confirm all seven quality choices from **Space Saver** through
   **Maximum Detail** remain visible and that **Smaller File** starts on
   **Compact**.

Record whether Cancel, Apply, Update, and Save as New matched their labels and
whether any change appeared to escape the editor unexpectedly.

### 4. Conflict And Queue Review

1. Choose a reusable-file outcome or quality setting that requires an explicit
   route/quality decision for the selected source.
2. Confirm no answer is silently preselected and Start/Preview remain blocked
   until a valid choice is applied.
3. Resolve the conflict and optionally enable the suggestion for the selected
   Profile.
4. Exercise **Forget suggestion**, then repeat the conflict once if practical.
5. Use **Add to Queue**, confirm the queued source retains its resolved result,
   and confirm **Add to Queue** becomes unavailable for the same source/title.
6. Attempt to add the same source/title again if practical and confirm the queue
   remains at one item. Different titles from the same disc should remain valid.
7. Start the queue.

Record whether the consequence of each choice was clear and whether the queue
made it obvious which item needed attention.

### 5. Window And Appearance Check

1. Resize the window to approximately 820 points wide.
2. Confirm the source, destination, recovery controls, and primary action remain
   reachable without horizontal scrolling.
3. Repeat one Ready or Editor check in both light and dark appearance.

### 6. Batch Folder Disc Titles

1. Choose **Add Folder of Movies…** with a folder containing at least one
   Blu-ray ISO and one ordinary supported movie file.
2. Confirm the setup offers **Main Feature** and **All 3D Videos**, with Main
   Feature selected by default.
3. Run Main Feature and confirm each ISO contributes only its selected main
   title while the ordinary movie is queued once.
4. Repeat with All 3D Videos and confirm every eligible stereoscopic title is
   represented by a distinct queue label and output identity.
5. Confirm selected titles run sequentially and the source is not removed until
   all selected titles from that source finish.
6. Retry one failed title or rerun a completed batch and confirm existing
   successful outputs are not silently duplicated or overwritten.

Record whether title selection, queue labels, ordering, and retry behavior made
the batch outcome predictable before conversion started.

## Playback Check

Play at least one Beta-produced movie on Apple Vision Pro when available. Record:

- whether stereoscopic depth appears correct;
- whether left and right eyes appear swapped;
- whether audio and subtitle choices match the setup summary;
- whether playback starts, seeks, and resumes normally.

This is a manual observation, not release qualification or the guided playback
companion planned separately.

## Feedback Questions

Every tester should answer these four questions:

1. Was the selected Result obvious before starting?
2. Did the repeated run feel like one action?
3. Was the conflict understandable without technical help?
4. Could the same movie be queued accidentally more than once?

Also include the app version, macOS version, source kind, and whether the test
used a built-in or custom Profile. Do not include the source title or full path.

## Diagnostics

For a failure or confusing state, use the app's diagnostic report flow and
include the resulting support code with the feedback. Add a short description
of what was expected and what happened. A support code is not needed for a
successful test.

## Rollback

1. Quit the Beta and every other app variant.
2. Restore the complete Application Support folder copied before installation.
3. Reinstall or reopen the intended Stable build.
4. Confirm the original Profiles and queue state are visible before editing
   them.

Do not open the restored Stable build against Profile or queue files first
written by the Beta. Restoring the backup is part of rollback.

## Deliberate Non-Goals

The following work remains separate so feedback stays attributable to the
prepared Beta 3 scope:

- the first-class persistent queue workspace, scheduling, notifications,
  storage forecasting, history, unattended policies, and watch-folder intake;
- the guided Vision Pro playback companion;
- broad test-suite cleanup or whole-file Swift reformatting.
