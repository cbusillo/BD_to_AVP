# Release Operations

- For either `Stable` or `Prerelease`, use
  `uv run python -m scripts.github_release_run watch` as the release-run monitor.
  A generic `gh run watch` or run waiter is not sufficient on its own.
- The watcher exit code `20` means a GitHub environment approval is required.
  Surface that gate immediately; do not continue silently polling a waiting run.
- Before approving `macos-signing`, obtain explicit user authorization in the
  current conversation. Then use `scripts.github_release_run approve` with the
  exact run ID, workflow name, full `main` SHA, confirmation SHA, and approval
  fingerprint emitted by `watch`. Do not call the pending-deployments API
  directly.
- Approval must use the active local GitHub identity validated by the helper.
  Never store a user token, make the automation bot a reviewer, or remove the
  environment review to bypass this contract.
- Keep `main` fixed while either release workflow is nonterminal. Coordinate a
  temporary merge hold on other pull requests because the workflows intentionally
  reject a release when protected `main` moves.

# Local Current Build

- When producing a user-launchable local build, use
  `BD_TO_AVP_SUPPORT_DIAGNOSTICS_ENDPOINT=https://diagnostics.shinycomputers.com uv run python scripts/native_app.py publish-current`
  instead of leaving the app inside a disposable worktree.
- The command requires a clean worktree, publishes an immutable commit-addressed
  ad-hoc app, and refreshes
  `~/Applications/3D Blu-ray to Vision Pro Current.app` plus its adjacent build
  metadata link. It must never replace the production-signed app in
  `/Applications`.
- Keep `scripts/native_app.py package` as the underlying package/release
  verification command; `publish-current` is the local durable handoff command.

# Tests

- A test earns its place only if it fails when the product is broken and passes
  when someone makes an intended change. If a version bump, toolchain bump,
  runner change or reworded workflow step would fail it, it is the wrong test.
- Never assert a literal that is defined elsewhere in the repository (version,
  build number, protocol version, toolchain, digest, file name). Assert that
  the sources agree, or derive the expectation from the single source of truth.
- Never assert workflow, script or document *text* (`assertIn("...", str(job))`,
  `step["run"]`, file contents). Check workflow structure by rule in
  `tests/test_workflow_security_policy.py`, or extract the script and run it.
- Code that loads or verifies a committed document must not depend on the state
  of the working tree. Check live files only on the path that acts on them.
- Do not add generated inventories, counts or snapshots that must be
  regenerated or hand-registered when an unrelated file changes.
- Keep byte-exact and digest gates on real artifacts and immutable evidence:
  the CI rebuild comparison of bundled tools, archived release evidence, and
  the signing approval contract.
- Removing or replacing tests: list what was removed and why in the pull
  request, and show planted faults that the remaining suite still catches.
