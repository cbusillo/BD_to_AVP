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

- Follow the [overall release rule](https://github.com/cbusillo/direction/blob/HEAD/DIRECTION.md#stop-boundaries).
- For either `Stable` or `Prerelease`, use
  `uv run python -m scripts.github_release_run watch` as the release-run monitor.
  A generic `gh run watch` or run waiter is not sufficient on its own.
- Releases have no per-release approval. If a run waits on an environment
  approval, `watch` stops with a safety error; report it to the Director rather
  than approving it. Never store a user token or make the automation bot a
  reviewer.
- `watch` retries a failed GitHub read and reports each one as `read_failed`.
  Exit `23` means GitHub stayed unreadable, so the run's state is unknown: read
  the run again and restart `watch`. Never cancel a release on a read failure.
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
The [local/CI command contract](https://github.com/cbusillo/codex-skills/blob/main/skills/github/references/config-schema.md#workflow-metadata)
applies through `.github/github.json`. Replace `{test_paths}` with the affected
test paths locally, selecting complete safety modules through the changed
code's callers. CI reads `test.ci` for the complete Python suite and retains
its native app, diagnostics and player gates. Fixture and hosted-platform
requirements remain in their workflow steps and linked testing guides.
Here, release workflow structure is checked by rule in
`tests/test_workflow_security_policy.py`.
