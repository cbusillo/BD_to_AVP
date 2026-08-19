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

When the operator workstation is not running the policy-required macOS major
version, use the owner-dispatched `Milestone Qualification` workflow instead of
weakening the environment check. The workflow runs the same collector on the
GitHub-hosted `macos-26` image with read-only repository and Actions access. It
accepts only the canonical `automation/release-evidence-<tag>` branch at its
exact remote head, rejects non-documentation differences from protected
`main`, validates the checked qualification manifest by its self digest,
downloads both DMGs by the asset IDs in their checked receipts, and uses the
checked signed-artifact UI receipt and original one-file Actions ZIP captured
by Release Evidence while the artifact was unexpired. It cannot push, edit a release, approve an
environment, or access release secrets.

Dispatch it only after its workflow definition is present on the canonical
evidence branch:

```sh
gh workflow run milestone-qualification.yml \
  --ref main \
  -f candidate_tag=v0.3.0 \
  -f manifest_sha256=<qualification-manifest-self-sha256>
```

The successful run uploads the validated signed-artifact UI receipt, both Tier
3 receipts, normalized evidence, checked manifest digest, evidence branch SHA,
and a bounded run summary with 30-day retention. Download those outputs,
validate them again, and add the accepted receipts to the same evidence branch.
The hosted collector proves the real Sparkle install/relaunch path, exact
appcast-bound release-notes URL, and installed accessibility semantics. The
guarded release engine separately proves that the embedded Markdown and
full-notes URL match the immutable release-note source. Targeted native-window
appearance capture remains useful operational evidence when Sparkle or
release-note rendering changes, but missing or failed
desktop capture does not block release evidence reconciliation.

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
that branch. Protected CI's `milestone` classifier remains red until all
blocking automated receipts are present. It reports optional presentation and
physical evidence without making those operator-only outcomes release blockers;
do not create a parallel evidence branch.

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
4. Revalidate the exact prior bundle and signed app tree immediately before
   updater interaction, invoke `Check for Updates…`, and drive Sparkle through
   an explicit bounded state machine. Identifier-scoped observations distinguish
   downloading, staged install, install-and-relaunch, install-on-quit,
   cancellation, terminal failure, and unknown states.
5. Durably record each selected action inside the owned qualification root
   before pressing it. Install-on-quit explicitly terminates the prior app,
   waits for the exact candidate on disk, and launches that candidate; staged
   installs remain bounded until Sparkle exposes the final action or the exact
   candidate appears on disk with a replacement application process, including
   relaunches completed between UI polls while Accessibility still reports a
   stale downloading, installing, or staged state.
6. Verify the final candidate bundle, build, signed app tree, and replacement
   running process, then verify the selected route, unrelated preference, and
   byte-for-byte profile library are preserved.
7. Run the candidate installed-app XCUITest lane. It verifies main-window
   readiness, profile-save accessibility and success, updater settings, the
   public releases link, and cropped light/dark app-window screenshots.
8. Quit the app, detach every mounted DMG, verify the cleanup ownership marker,
   and delete the qualification root with raw XCTest and build output.
9. Emit normalized public-safe evidence and validated receipts for both policy
   cases with `cleanup.status = disposed`.

Every mount, subprocess, network request, GUI wait, update wait, and cleanup
action has a timeout. Raw package-smoke output remains inside the disposable
root and is deleted; public evidence records only its SHA-256 digest. Raw AX
trees, XCTest logs, `.xcresult` bundles, local paths, and full-screen captures
are deleted. Retained screenshots contain only the app window. A failed run
does not emit either accepted receipt.

Timeout failures include only bounded public-safe state context: the final
state, staged/action counts, process relation, exact-candidate result, and the
ordered state enum history. They do not retain raw Accessibility output, paths,
process IDs, usernames, or hostnames.

The updater state machine prefers the Sparkle `SUUpdateAlert` window and
`SPUUserUpdateChoiceInstall` action identifiers. A title fallback is permitted
only inside the single owned updater window and is recorded in evidence. The
selected action, state, exact prior/candidate identities, attempt number, and
transition history are atomically written and synced before an asynchronous
press. That journal remains disposable; normalized `sparkle-update.json`
retains bounded public-safe action, window-match, state, attempt, route, and
candidate-version fields plus the journal and intent SHA-256 digests. It never
retains window text, local paths, usernames, hostnames, or raw AX output.

One clean retry is allowed only for a classified pre-press application-start,
update-menu, update-window timeout, or guarded state-change race where the
updater changed after durable intent but before any action was pressed. The
first attempt must fully dispose its owned root and app process before retry.
Cancellation, updater-reported failure, unknown UI, identity mismatch,
preference/profile drift, action-limit failure, and every failure after a press
are terminal. No other implicit retry occurs.

Native Sparkle-window presentation sampling remains visible operational
evidence but is nonblocking. The state-machine result, exact release-note source,
and installed accessibility semantics remain the automated blocking contract;
missing or failed manual presentation capture does not alter the runner result.

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
