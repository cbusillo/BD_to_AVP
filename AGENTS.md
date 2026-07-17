# Release Operations

- For `Release from protected main`, use
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
