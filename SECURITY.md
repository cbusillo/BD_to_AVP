# Security Policy

## Supported Versions

Only the latest release on the
[releases page](https://github.com/cbusillo/BD_to_AVP/releases) is supported.
Fixes ship in a new release built from `main`.

## Reporting a Vulnerability

Report suspected vulnerabilities privately through GitHub's
[Report a vulnerability](https://github.com/cbusillo/BD_to_AVP/security/advisories/new)
form. Do not open a public issue for a vulnerability.

Include the app and version (Mac converter or Vision Pro player), the macOS
or visionOS version, the impact, and the smallest steps that reproduce it.

Do not send pairing codes, device identifiers, network addresses, disc keys,
movie files, or other personal data. Use redacted or made-up values.

This is a single-maintainer project. Reports are handled on a best-effort
basis, and I aim to reply within seven days.

## Scope

Relevant reports include:

- movie sharing exposing folders or files to a headset or device that was not
  paired;
- unsafe handling of disc images, video files, or subtitles given to the
  converter or player;
- the signed app, DMG, or update path being replaced or tampered with; and
- dependency or GitHub Actions supply-chain problems.

Problems in MakeMKV, FFmpeg, or other bundled tools should go to their own
projects unless this repository makes them worse.
