# Distribution Policy

BD_to_AVP currently ships the GUI as a notarized macOS DMG from
GitHub Releases. The DMG is the primary artifact for normal users; the
terminal/PyPI path remains available for power users who manage their own
command-line tools.

This policy keeps the app predictable on clean Macs and makes distribution
decisions explicit instead of hiding dependency installation behind first launch.
The normative production identity, version, and Sparkle route matrix are in
[Production Release Routes](release-routes.md).

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

## Terminal, PyPI, And Homebrew Channel

The custom `cbusillo/tap` Homebrew formula is the preferred terminal install.
It builds from the stable GitHub tag, consumes the committed `uv.lock`, depends
on Homebrew FFmpeg and Python 3.12, and omits the PySide6 GUI packages. The base
PyPI package follows the same CLI-only boundary; users who intentionally want
the legacy Python GUI may install the `gui` extra. The production DMG uses the
SwiftUI interface while retaining Briefcase only to stage the embedded Python
engine and its dependencies.

The formula does not depend on a MakeMKV cask. MakeMKV is optional for existing
MKV, MTS, and M2TS inputs, remains external for disc extraction, and should be
installed from its current supported macOS distribution. A Homebrew cask for
BD_to_AVP is not maintained because it would duplicate the signed DMG channel
without providing a reliable CLI link for the Briefcase launcher.

Terminal dependency changes should not weaken the GUI policy. If a tool is
required by the GUI, the release workflow should either bundle and verify it or
document it as an external dependency with preflight behavior.

## Current And Deferred Tracks

- The accepted SwiftUI application and bundled-worker architecture are the
  production GUI path. The protected-main release workflow builds that app with
  the production name and bundle identifier on a pinned GitHub-hosted macOS 26
  toolchain, then verifies the exact DMG again in a separate macOS 26 job.
- The next planned production-identity field build is `v0.3.0-beta.3`, internal
  version `0.3.0b3`, build `148`. It is a manual-download seed because currently
  shipped clients cannot select Beta or Alpha, but its immutable appcast item is
  visible to those routes after the new selector is installed.
- Stable, RC, Beta, and Alpha are routes for the same product, bundle identifier,
  feed, Sparkle key, signing team, and diagnostics endpoint. Stable is default;
  broader routes include progressively earlier stages without permitting
  downgrade.
- The former side-by-side feedback lane is retired. Its immutable historical
  tags and assets—`native-ui-preview-1`, `v0.3.0-beta.1`, and
  `v0.3.0-beta.2`—remain outside production Sparkle and release history. Their
  tag syntax does not make them production Alpha/Beta route releases.
- Stable alone may become GitHub Latest or publish to PyPI/Homebrew. Alpha,
  Beta, and RC releases are GitHub prereleases and never publish packages.
- PKG artifact policy is tracked in #118. Until that issue decides otherwise,
  PKG output is not the normal-user release path.
- The Sparkle direct-DMG implementation and four-route policy is
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
7. A tester or maintainer records at least one media-path smoke result for a
   prerelease when test media is available.
8. If a Sparkle appcast is promoted for normal users, an installed-app upgrade
   smoke and appcast validation pass first.
9. Route smoke proves Stable admits only Stable; RC admits Stable/RC; Beta admits
   Stable/RC/Beta; Alpha admits Stable/RC/Beta/Alpha; and every route can later
   select a newer unchanneled Stable item.
10. Switching to a safer route never offers or installs an older build.
