# Sparkle Update Test

`scripts/tier3_clean_machine.py update-test` checks the update path testers
actually use: an installed older release finds the candidate through Sparkle,
installs it, relaunches, and keeps its settings. It operates only on immutable
signed artifacts and never signs, notarizes, publishes, approves an environment,
or changes the live appcast.

## Where It Runs

`.github/workflows/update-test.yml` runs it on GitHub's hosted Apple Silicon
`xcode-27` image:

- **Before publication.** The release engine calls it after the signed
  appcast and release receipt exist, and `publish-release` waits for it on
  every channel. It installs the newest lower published release that is an
  ancestor of the candidate (`scripts.release qualification-base`), and uses
  the same-run package, receipt and signed appcast.
- **On a pull request** that changes the workflow or the script, against the
  newest published release, so a broken harness shows up before a release
  needs it.

The hosted `resettable-vm` layout keeps the runner's real home, installs into
`/Applications`, and keeps scratch data under `RUNNER_TEMP`. It refuses to run on
local or self-hosted machines, or when the app or app-owned state already
exists. The `restorable-location` layout is for local debugging only and keeps
everything under a dedicated root in the current home.

## Local Feed

The candidate isn't public yet, so the test serves a copy of the signed appcast
and the candidate DMG from `127.0.0.1`, with the candidate's download URL
rewritten to the local server. The appcast is not signed as a whole, and each
DMG keeps its EdDSA signature, so the rewrite doesn't weaken Sparkle's checks.
The test writes `SUFeedURL` into the prior app's user defaults, which Sparkle
2.9.4 reads before `Info.plist`. The run fails unless the local server served
both the appcast and the DMG, so it can never pass by reading the live feed.

## What A Run Does

1. Install the exact prior release from its DMG and check its bundle identity
   and signed app tree against its release receipt.
2. Seed the version 5 profile fixture, the update route, an unrelated
   preference sentinel, and the local `SUFeedURL`.
3. Run the installed-app XCUITest updater phase against the prior app: Sparkle
   controls and the candidate's release-notes URL, without installing.
4. Revalidate the prior bundle, choose `Check for Updates…`, and drive Sparkle
   through a bounded state machine. Sparkle 2.9.4's first `Install Update`
   starts download and extraction; the harness waits, then requires and presses
   `SUStatusInstallAndRelaunch`. Install-on-quit terminates the prior app,
   waits for the candidate on disk, and launches it.
5. Verify the relaunched candidate's identity and replacement process, the
   preserved route and sentinel, and the exact version 6 profile migration.
6. Quit, detach every DMG, verify the cleanup ownership marker, and delete the
   qualification root. The hosted layout also removes only the exact verified
   app and allowlisted app-owned home state.
7. Write one small `result.json` plus bounded evidence files.

Each selected action is written and synced before it is pressed. One clean
retry is allowed only for a classified pre-press timeout or an updater change
between recording the intent and pressing. Cancellation, updater failure,
unknown UI, identity mismatch, preference or profile drift, and every failure
after a press are final.

If the cleanup ownership marker is missing or changed, the runner refuses to
delete the qualification root and leaves it for inspection.

## Validation

```sh
uv run python -m unittest tests.test_tier3_clean_machine
```

The fixtures use fake signed app trees, real release receipts, a generated
appcast served over local HTTP, and a simulated Sparkle that fetches the feed
and DMG through the `SUFeedURL` preference.

Two platform facts matter when reading UI screenshots. AppKit ignores
`-AppleInterfaceStyle` as a launch argument, so dark mode comes only from the
system setting; `-NSRequiresAquaSystemAppearance YES` does force light.
`CFFIXED_USER_HOME` redirects file APIs but not `UserDefaults`, so an installed
UI run on a development machine shares the real app's preferences (#769).
