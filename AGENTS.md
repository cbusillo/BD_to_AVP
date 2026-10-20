# Direction and Execution

Read the Director's [overall DIRECTION.md](https://github.com/cbusillo/direction/blob/HEAD/DIRECTION.md)
before working here. This repository has no DIRECTION.md of its own. The
Director (cbusillo) is this product's Client.
Use the maintained [executing loop](https://github.com/cbusillo/codex-skills/blob/main/skills/references/executing-loop.md)
and its owning skills for issue claims, linked worktrees, bot commits, review,
landing, and closeout. AGENTS.md is the sole agent-instruction filename;
path-specific instructions belong in nested AGENTS.md files.

Use `.github/github.json` for validation commands and repository workflow
metadata. Documentation-only changes use the existing CI lane selection in
`scripts/ci_lanes.py`; batch related edits into one push. Changes to execution
guidance need a review under [reviews by another model](https://github.com/cbusillo/codex-skills/blob/main/skills/references/model-review.md).
Release-specific requirements below still apply when release work is authorized.

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

Follow the shared [Tests rule](https://github.com/cbusillo/codex-skills/blob/main/instructions/global.md#tests).
Here, release workflow structure is checked by rule in
`tests/test_workflow_security_policy.py`.
