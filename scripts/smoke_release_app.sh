#!/bin/sh
set -eu

APP_PATH="${1:-/Applications/3D Blu-ray to Vision Pro.app}"
SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

if command -v uv >/dev/null 2>&1; then
  exec uv run --project "$REPO_ROOT" python "$SCRIPT_DIR/smoke_release_app.py" --app-path "$APP_PATH"
fi

if command -v python3 >/dev/null 2>&1; then
  exec python3 "$SCRIPT_DIR/smoke_release_app.py" --app-path "$APP_PATH"
fi

printf 'Release app smoke failed: uv or python3 is required.\n' >&2
exit 1
