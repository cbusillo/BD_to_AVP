# Distribution Policy

BD_to_AVP currently ships the GUI as a notarized macOS DMG from
GitHub Releases. The DMG is the primary artifact for normal users; the
terminal/PyPI path remains available for power users who manage their own
command-line tools.

This policy keeps the app predictable on clean Macs and makes distribution
decisions explicit instead of hiding dependency installation behind first launch.

## Current GUI Channel

- Primary artifact: notarized DMG attached to GitHub Releases.
- Install flow: user downloads the DMG, drags the app to `/Applications`, and
  approves the normal Gatekeeper launch prompt if macOS asks.
- The GUI app must not install Homebrew, edit shell startup files, request
  administrator privileges for setup, or run broad package-manager upgrades.
- Runtime tools should be bundled into the app when their licenses and Apple
  notarization behavior make that practical.
- MakeMKV remains an external app dependency for Blu-ray disc and title
  extraction. The GUI should detect `/Applications/MakeMKV.app` and give a
  plain recovery message when it is missing.
- The release DMG must pass the clean-machine smoke checklist in
  [release-smoke.md](release-smoke.md) before wider promotion.

## Bundled Runtime Tools

The release workflow signs and verifies the GUI runtime tools before packaging.
`scripts/verify_app_tools.py --profile release` is the release gate for
app-local tools.

Required app-local tools today:

- `ffmpeg`
- `ffprobe`
- `MP4Box`
- `edge264_test`
- `spatial-media-kit-tool`

The release gate should fail if a required bundled executable is missing, not
executable, or linked to Homebrew paths such as `/opt/homebrew` or `/usr/local`.

Apple Vision OCR is the GUI subtitle OCR path. The clean-machine smoke should
verify that the packaged app runtime can import and run the Apple Vision OCR
smoke without requiring MKVToolNix or Tesseract.

## External Dependencies

External dependencies must be visible to the user and recoverable without
reinstalling BD_to_AVP.

- MakeMKV is currently external and expected at `/Applications/MakeMKV.app`.
- A missing external dependency should produce a message that names the external
  app/tool and explains the next step.
- A missing bundled dependency should be treated as a release blocker or linked
  to a focused follow-up issue. Do not tell users to reinstall the current app
  unless the current release artifact actually contains the missing dependency.

## Terminal And PyPI Channel

The terminal/PyPI channel is for users who intentionally manage dependencies
themselves. README terminal instructions may continue to use Homebrew because
that channel is explicitly command-line oriented.

Terminal dependency changes should not weaken the GUI policy. If a tool is
required by the GUI, the release workflow should either bundle and verify it or
document it as an external dependency with preflight behavior.

## Deferred Tracks

- The native SwiftUI shell and bundled-worker architecture proof is tracked in
  #198. It remains a direct-distribution prototype and does not change the
  current Briefcase DMG release channel.
- An early native UI feedback build is tracked in #202. It must ship as a
  separately identified, opt-in GitHub prerelease that installs beside the
  production app and does not enter either Sparkle channel, GitHub latest, or
  PyPI. The first native production-replacement candidate is reserved for the
  `0.3.0rc1` line after real conversion and updater prerequisites are complete.
  `Native UI Preview 1` is published by its own protected-main workflow using
  fixed tag `native-ui-preview-1`, a dedicated macOS 27/Xcode 27 release runner,
  and an exact two-asset allowlist: the notarized DMG and `SHA256SUMS`.
- PKG artifact policy is tracked in #118. Until that issue decides otherwise,
  PKG output is not the normal-user release path.
- Homebrew distribution for CLI users is tracked in #119.
- The Sparkle direct-DMG implementation and Stable/Release Candidates policy is
  documented in [sparkle-updates.md](sparkle-updates.md) and tracked through
  #162 through #165.
- App Store feasibility and sandbox constraints are tracked in #121.
- MakeMKV replacement/removal is tracked in #103.
- Native MVC splitter stability and upstream edge264 follow-through are tracked
  in #135 and #140.

## Promotion Checklist

Before promoting a GUI release beyond tester/RC use:

1. Release workflow completes successfully for the intended tag or branch.
2. The release gate verifies app-local runtime tools with the `release` profile.
3. The DMG passes Gatekeeper assessment on a clean Apple Silicon macOS machine.
4. The app launches without Homebrew, Python, virtualenv, or repo checkout state.
5. Missing MakeMKV produces the expected external-dependency recovery path.
6. Installed MakeMKV clears the preflight blocker.
7. A tester or maintainer records at least one media-path smoke result for the
   release candidate when test media is available.
8. If a Sparkle appcast is promoted for normal users, an installed-app upgrade
   smoke and appcast validation pass first.
9. Stable-channel smoke excludes RC items, and Release Candidates smoke proves
   the client can later select the unchanneled production release.
