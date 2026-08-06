# Tier 3 Clean-Machine And Sparkle Qualification

`scripts/tier3_clean_machine.py` collects the automated
`clean-machine-signed-update` and `installed-ui-accessibility` evidence declared
by the release qualification policy. It operates only on immutable signed
release artifacts and never signs, notarizes, publishes, approves an
environment, or changes the live appcast.

## Maintained Environment

The first maintained lane is `restorable-location` on Apple Silicon macOS 26.
The qualification root must not exist before the run and must be a dedicated
location under the current user's home directory. The runner creates an
isolated synthetic home with `HOME` and `CFFIXED_USER_HOME`, installs the app
under that owned root, and deletes the complete root after bounded cleanup.

The host must provide:

- macOS 26 on `arm64`;
- Accessibility control for the terminal or automation host;
- Xcode with `xcodebuild`, plus `defaults`, `ditto`, `hdiutil`, `open`, and
  `osascript`;
- enough free space for both DMGs, two candidate copies, and 2 GiB of working
  headroom;
- no running production app process; and
- network access to the real public appcast.

Homebrew may be installed on the host. The package smoke and launched app use a
system-only runtime `PATH`; host developer tools are not accepted as packaged
dependencies.

## Inputs

Both the prior and candidate release receipts must be committed at the current
repository `HEAD`. Their local DMGs must match the exact names, sizes, and
SHA-256 digests in those receipts. The candidate build must be newer than the
prior build and must be present on the live public appcast for the selected
route.

For a newly published candidate, run from the idempotent
`automation/release-evidence-<tag>` branch created by the Release Evidence
workflow. Add the validated Tier 3 receipts and their evidence-index entries to
that branch. Protected CI's `milestone` classifier is expected to remain red
until all due receipts are present; do not merge around it or create a parallel
evidence branch.

Run the read-only preflight first:

```sh
uv run python -m scripts.tier3_clean_machine preflight \
  --candidate-release-receipt docs/release-evidence/<candidate>/release-receipt.json \
  --candidate-dmg ~/Downloads/<candidate>.dmg \
  --prior-release-receipt docs/release-evidence/<prior>/release-receipt.json \
  --prior-dmg ~/Downloads/<prior>.dmg \
  --qualification-root ~/Tier3-BD-to-AVP-Qualification \
  --route rc \
  --environment-class restorable-location
```

Preflight validates checked receipt bytes, both DMGs, build ordering, the
environment, free space, Accessibility, process state, required tools, network
access, appcast structure, source-bound release notes, enclosure identity, and
route eligibility. Its JSON output omits hostnames, usernames, and local paths.

## Qualification Run

After preflight passes, run:

```sh
uv run python -m scripts.tier3_clean_machine run \
  --candidate-release-receipt docs/release-evidence/<candidate>/release-receipt.json \
  --candidate-dmg ~/Downloads/<candidate>.dmg \
  --prior-release-receipt docs/release-evidence/<prior>/release-receipt.json \
  --prior-dmg ~/Downloads/<prior>.dmg \
  --qualification-root ~/Tier3-BD-to-AVP-Qualification \
  --route rc \
  --environment-class restorable-location \
  --output-receipt /path/out/clean-machine-signed-update.json \
  --ui-output-receipt /path/out/installed-ui-accessibility.json \
  --evidence-directory /path/out/clean-machine-signed-update-evidence
```

The runner performs this bounded sequence:

1. Install the exact candidate from its DMG, verify bundle and signed app-tree
   identity, and run `scripts/smoke_release_app.py` from the installed copy.
2. Clear the owned runtime location, install the exact prior release, and seed a
   valid profile library plus route and unrelated preference sentinels in the
   synthetic home.
3. Run the installed-app XCUITest lane against the exact prior app, verifying
   Sparkle controls and the source-bound release-notes URL without installing.
4. Launch the prior app again, invoke `Check for Updates…`, click Sparkle's
   bounded install/relaunch action, and wait for the exact candidate bundle,
   build, and signed app tree to relaunch.
5. Verify the selected route, unrelated preference, and byte-for-byte profile
   library are preserved.
6. Run the candidate installed-app XCUITest lane. It verifies main-window
   readiness, profile-save accessibility and success, updater settings, the
   public releases link, and cropped light/dark app-window screenshots.
7. Quit the app, detach every mounted DMG, verify the cleanup ownership marker,
   and delete the qualification root with raw XCTest and build output.
8. Emit normalized public-safe evidence and validated receipts for both policy
   cases with `cleanup.status = disposed`.

Every mount, subprocess, network request, GUI wait, update wait, and cleanup
action has a timeout. Raw package-smoke output remains inside the disposable
root and is deleted; public evidence records only its SHA-256 digest. Raw AX
trees, XCTest logs, `.xcresult` bundles, local paths, and full-screen captures
are deleted. Retained screenshots contain only the app window. A failed run
does not emit either accepted receipt.

If the cleanup ownership marker is missing or changed, the runner refuses to
delete the qualification root because it can no longer prove ownership. It
removes partial public outputs, reports the ownership failure, and leaves that
root for operator inspection. After confirming that the location contains only
qualification data, remove it manually before retrying with the same path.

## Validation

The deterministic runner fixtures use fake signed app trees, checked release
receipts, a generated valid appcast, isolated preferences, and a simulated
Sparkle replacement/relaunch:

```sh
uv run python -m unittest tests.test_tier3_clean_machine
```

The real lane must still run on the declared macOS 26 environment and a newer
published candidate before its receipt can be checked into the release evidence
index.
