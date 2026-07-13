# Clean-Machine Release Smoke

Use this checklist before a wider GUI release. The goal is to prove the app
bundle works on a Mac that does not have the developer machine's Homebrew,
Python, virtualenv, or cached build state.

See [distribution-policy.md](distribution-policy.md) for the current GUI artifact
and dependency policy.

## Test Machine

- Apple Silicon Mac or VM running macOS 26 for the minimum-version pass. A
  second pass on the newest supported macOS is useful but does not replace the
  macOS 26 gate.
- Fresh user account.
- No Homebrew install.
- No repo checkout.
- No Python setup beyond the tools already present on macOS.
- Network access for GitHub release download and the app update check.

Snapshots are useful: take a clean snapshot before installing BD_to_AVP, and
another after installing MakeMKV if you need to repeat both missing-MakeMKV and
installed-MakeMKV paths.

## Artifacts

Use the release DMG from GitHub Releases. The DMG is the primary GUI artifact.
PKG artifacts are not part of the normal-user smoke unless a later issue keeps
them for a specific deployment use case.

Record:

- Release tag or workflow run.
- DMG filename and SHA256.
- macOS version and machine/VM model.
- Whether MakeMKV was absent or installed for each pass.

## Baseline Checks

On the clean machine:

```bash
test ! -e /opt/homebrew || \
  echo "Apple Silicon Homebrew is present; use a cleaner VM"
test ! -e /usr/local/Homebrew || \
  echo "Intel Homebrew is present; use a cleaner VM"
spctl --assess --type open --verbose=4 ~/Downloads/*.dmg
```

Before installation, confirm the app inside the DMG reports
`LSMinimumSystemVersion` as `26.0`. Launching the native host, probing the bundled
conversion tools, and running the packaged-worker smoke on macOS 26 are release
gates, not best-effort compatibility checks.

The DMG check uses `--type open` because it assesses the downloaded disk image.
The app smoke uses `--type execute` because it assesses the installed app
bundle.

Mount the DMG, drag the app to `/Applications`, eject the DMG, then open the app
once from Finder. Approve the normal Gatekeeper prompt if macOS shows one. After
that first user-approved launch, quit the app and run the automated smoke.

## Automated App Smoke

Run the shell smoke script from a checkout, copied script, or downloaded source
archive. It uses macOS system tools and the packaged app, so it is the preferred
VM command:

```bash
sh scripts/smoke_release_app.sh "/Applications/3D Blu-ray to Vision Pro.app"
```

For an in-repo Python harness with the same core checks, use a machine that
already has Python available:

```bash
python3 scripts/smoke_release_app.py \
  --app-path "/Applications/3D Blu-ray to Vision Pro.app"
```

Expected result:

- Gatekeeper assessment passes.
- App bundle layout is valid.
- Bundled tools run from the app bundle.
- If Xcode Command Line Tools are available, bundled tools do not link to
  `/opt/homebrew` or `/usr/local`. If developer tools are not installed, the
  scripts say the linkage check was skipped.
- The packaged CLI `--version` matches `Info.plist`.
- The packaged CLI `--help` runs with a sanitized `PATH`.
- The packaged Apple Vision OCR smoke runs with a sanitized `PATH`.
- If MakeMKV is absent, the script records that the first-run path should ask
  the user to install MakeMKV.

For unsigned local development builds only, use the Python harness and add
`--skip-spctl`.

## Manual GUI Smoke

With MakeMKV absent:

1. Open the app from `/Applications`.
2. Confirm Gatekeeper does not block launch.
3. Confirm the app does not ask to install Homebrew, run terminal commands, or
   request admin privileges.
4. Start an ISO or Blu-ray-folder conversion far enough to trigger preflight.
5. Confirm the message asks for MakeMKV in plain user-facing language.
6. Confirm `Download MakeMKV…` opens the official MakeMKV download page.

With MakeMKV installed:

1. Install the current macOS MakeMKV app from the official MakeMKV website.
2. Reopen BD_to_AVP.
3. Confirm the MakeMKV preflight no longer blocks disc-oriented work.
4. If test media is available, inspect both an ISO and a Blu-ray folder. Run a
   conversion smoke and record the first failing stage, logs, runtime, and
   whether output files are created.

### Physical-Disc Beta Smoke

Complete this section on real hardware during the physical-disc beta cycle. A
VM without an attached optical drive is not sufficient.

1. Connect a USB Blu-ray drive, preferably through a powered hub, install
   MakeMKV, and insert a known 3D Blu-ray disc.
2. Confirm the app detects the disc without relaunching. Select it and confirm
   metadata inspection completes through MakeMKV.
3. Start conversion, wait until MakeMKV is actively reading the disc, then press
   Stop. Confirm processing exits and Finder can eject the disc without a reboot.
4. Reinsert the disc and complete a conversion, or record the first failing
   stage, the activity log, drive model, connection type, and whether the drive
   disconnected or spun down.
5. Eject the selected disc while the app is idle. Confirm the stale selection is
   cleared, then reinsert it and confirm it reappears automatically.
6. Confirm “Remove original after success” is unavailable for the physical disc
   and that the selected output folder is outside the mounted disc volume.
7. If MakeMKV reports that it left a potentially usable intermediate MKV,
   confirm the app shows `Continue From Created MKV` and `Cancel` instead of a
   generic failure. Continue only with a known usable MKV and confirm a fresh
   job resumes at Extract MVC and Audio.
8. If subtitle extraction reports unusable output, confirm the app shows
   `Continue Without Subtitles` and `Cancel`. Continuing must start a fresh job
   without changing the visible profile or Include subtitles setting.
9. On either recovery card, confirm `Cancel` starts no worker and unlocks the
   conversion settings.

If the drive drops offline during heavy seeking, repeat once with a powered hub
before classifying the failure as an app defect. Preserve the activity log either
way so device, permission, AACS, and media-read failures can be distinguished.

## Pass Criteria

- No Homebrew or admin setup is required for GUI launch.
- The notarized DMG installs, launches, probes every bundled conversion tool,
  and completes its packaged-worker smoke on Apple Silicon macOS 26.
- The app's bundled tools are used where expected.
- Missing MakeMKV produces a clear recovery path.
- Installing MakeMKV clears the MakeMKV preflight blocker.
- The physical-disc beta passes inspection, cancellation/eject, automatic
  insertion/ejection refresh, and at least one recorded real-disc conversion
  attempt on supported hardware.
- A recoverable MakeMKV or subtitle failure presents explicit actions and never
  collapses into a generic dead end. The selected recovery starts a new job;
  cancelling starts none.
- Any remaining missing bundled tool is recorded as a release blocker or linked
  to a follow-up issue. A reinstall-app message for a tool that is not part of
  the app bundle is a blocker, because reinstalling the current app will not add
  it.

## Sparkle Packaging Gate

Before a draft GitHub Release is published, the release workflow must run:

```sh
uv run python -m scripts.sparkle_bundle \
  --app "build/bd-to-avp/macos/app/3D Blu-ray to Vision Pro.app"
uv run python -m scripts.sparkle_bundle \
  --app "build/bd-to-avp/macos/app/3D Blu-ray to Vision Pro.app" \
  --verify-signatures
uv run python -m scripts.sparkle_bundle \
  --dmg dist/<release>.dmg \
  --verify-signatures \
  --verify-distribution
```

The gate must confirm the repository build number, direct-distribution metadata,
public key, feed URL, automatic-update policy, pinned framework, `Updater.app`,
both XPC services, nested signatures, containing app signature, notarization,
and Gatekeeper assessment. The build number must also be newer than every item
in both the live appcast and latest durable release snapshot, the short version
must be unpublished, and the draft DMG name, size, and digest must match the
locally validated artifact before and after upload. The cumulative appcast and
checksum are re-downloaded and verified before the draft is published.

## Sparkle Update Smoke (After Enablement)

This is the acceptance smoke for #165. Do not begin it until #162 through #164
have provided the production key/feed, packaged framework, and runtime
integration described in [sparkle-updates.md](sparkle-updates.md).

1. Install an older signed/notarized DMG build in `/Applications` and launch it
   from there. Do not test the update while running from the mounted DMG.
2. Confirm Help > `Update Channel` defaults to Stable. With an RC newer than the
   installed build in the appcast, confirm `Check for Updates…` does not select
   it.
3. Select Release Candidates, run `Check for Updates…`, and confirm Sparkle's
   standard update UI selects the RC item.
4. Inspect the selected appcast item and confirm its `sparkle:version`,
   `sparkle:shortVersionString`, release-notes content or URL, enclosure URL, and
   enclosure length match the candidate and published GitHub Release DMG, and
   that its `sparkle:edSignature` is present and non-empty.
5. Confirm Sparkle's update UI displays the candidate version and release notes
   from that same appcast item.
6. Install the update, relaunch, and confirm the displayed and packaged versions
   match the candidate.
7. Publish or stage a newer unchanneled production item and confirm the
   Release Candidates client selects it without changing its preference.
8. Repeat with an unavailable feed and an intentionally invalid test signature;
   the installed app must remain unchanged.
9. Start media processing and confirm installation/relaunch is postponed until
   processing is idle.
10. Verify the manual GitHub Releases download remains usable as the recovery
    path.

## Follow-Up Routing

- Missing bundled GUI dependency: file or update the relevant #88 child issue.
- MakeMKV replacement/removal question: #103.
- Release artifact confusion such as PKG vs DMG: #118.
- Sparkle architecture: #120 and `docs/sparkle-updates.md`.
- Sparkle implementation and validation: #162 through #165.
- App Store sandbox/name/compliance question: #121.
