# Clean-Machine Release Smoke

Use this checklist before a wider GUI release. The goal is to prove the app bundle works on a Mac that does not have the developer machine's Homebrew, Python, virtualenv, or cached build state.

## Test Machine

- Apple Silicon macOS VM or spare Apple Silicon Mac.
- Fresh user account.
- No Homebrew install.
- No repo checkout.
- No Python setup beyond the tools already present on macOS.
- Network access for GitHub release download and the app update check.

Snapshots are useful: take a clean snapshot before installing BD_to_AVP, and another after installing MakeMKV if you need to repeat both missing-MakeMKV and installed-MakeMKV paths.

## Artifacts

Use the release DMG from GitHub Releases. The DMG is the primary GUI artifact. PKG artifacts are not part of the normal-user smoke unless a later issue keeps them for a specific deployment use case.

Record:

- Release tag or workflow run.
- DMG filename and SHA256.
- macOS version and machine/VM model.
- Whether MakeMKV was absent or installed for each pass.

## Baseline Checks

On the clean machine:

```bash
test ! -e /opt/homebrew || echo "Homebrew is present; use a cleaner VM for the required smoke"
spctl --assess --type open --verbose=4 ~/Downloads/*.dmg
```

The DMG check uses `--type open` because it assesses the downloaded disk image. The app smoke uses `--type execute` because it assesses the installed app bundle.

Mount the DMG, drag the app to `/Applications`, eject the DMG, then open the app once from Finder. Approve the normal Gatekeeper prompt if macOS shows one. After that first user-approved launch, quit the app and run the automated smoke.

## Automated App Smoke

Run the shell smoke script from a checkout, copied script, or downloaded source archive. It uses macOS system tools and the packaged app, so it is the preferred VM command:

```bash
sh scripts/smoke_release_app.sh "/Applications/3D Blu-ray to Vision Pro.app"
```

For an in-repo Python harness with the same core checks, use a machine that already has Python available:

```bash
python3 scripts/smoke_release_app.py --app-path "/Applications/3D Blu-ray to Vision Pro.app"
```

Expected result:

- Gatekeeper assessment passes.
- App bundle layout is valid.
- Bundled tools run from the app bundle.
- If Xcode Command Line Tools are available, bundled tools do not link to `/opt/homebrew`. If developer tools are not installed, the scripts say the linkage check was skipped.
- The packaged CLI `--version` matches `Info.plist`.
- The packaged CLI `--help` runs with a sanitized `PATH`.
- If MakeMKV is absent, the script records that the first-run path should ask the user to install MakeMKV.

For unsigned local development builds only, use the Python harness and add `--skip-spctl`.

## Manual GUI Smoke

With MakeMKV absent:

1. Open the app from `/Applications`.
2. Confirm Gatekeeper does not block launch.
3. Confirm the app does not ask to install Homebrew, run terminal commands, or request admin privileges.
4. Start a disc-oriented conversion far enough to trigger preflight.
5. Confirm the message asks for MakeMKV in plain user-facing language.

With MakeMKV installed:

1. Install the current macOS MakeMKV app from the official MakeMKV website.
2. Reopen BD_to_AVP.
3. Confirm the MakeMKV preflight no longer blocks disc-oriented work.
4. If test media is available, run a short conversion smoke and record the first failing stage, logs, runtime, and whether output files are created.

## Pass Criteria

- No Homebrew or admin setup is required for GUI launch.
- The app's bundled tools are used where expected.
- Missing MakeMKV produces a clear recovery path.
- Installing MakeMKV clears the MakeMKV preflight blocker.
- Any remaining missing tool is recorded as a release blocker or linked to a follow-up issue. A reinstall-app message for an unbundled tool such as MP4Box, MKVToolNix, or Tesseract is a blocker, because reinstalling the current app will not add those tools.

## Follow-Up Routing

- Missing bundled GUI dependency: file or update the relevant #88 child issue.
- MakeMKV replacement/removal question: #103.
- Release artifact confusion such as PKG vs DMG: #118.
- Direct-DMG auto-update behavior: #120.
- App Store sandbox/name/compliance question: #121.
