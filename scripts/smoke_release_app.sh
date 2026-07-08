#!/bin/sh
set -eu

APP_PATH="${1:-/Applications/3D Blu-ray to Vision Pro.app}"
MINIMAL_PATH="/usr/bin:/bin:/usr/sbin:/sbin"
INFO_PLIST="$APP_PATH/Contents/Info.plist"
APP_BIN_DIR="$APP_PATH/Contents/Resources/app/bd_to_avp/bin"

fail() {
  printf 'Release app smoke failed: %s\n' "$1" >&2
  exit 1
}

pass() {
  printf 'ok - %s\n' "$1"
}

plist_value() {
  /usr/libexec/PlistBuddy -c "Print :$1" "$INFO_PLIST"
}

[ -d "$APP_PATH" ] || fail "app bundle not found: $APP_PATH"
[ -f "$INFO_PLIST" ] || fail "Info.plist not found: $INFO_PLIST"

EXECUTABLE_NAME="$(plist_value CFBundleExecutable)"
SHORT_VERSION="$(plist_value CFBundleShortVersionString)"
APP_EXECUTABLE="$APP_PATH/Contents/MacOS/$EXECUTABLE_NAME"

[ -x "$APP_EXECUTABLE" ] || fail "app executable is missing or not executable: $APP_EXECUTABLE"
[ -d "$APP_BIN_DIR" ] || fail "app tool directory is missing: $APP_BIN_DIR"
pass "app bundle layout"

spctl --assess --type execute --verbose=4 "$APP_PATH" >/dev/null 2>&1 || fail "Gatekeeper assessment failed"
pass "Gatekeeper assessment"

if [ -e /opt/homebrew ] || [ -e /usr/local/Homebrew ]; then
  printf 'note - Homebrew exists on this machine; smoke uses sanitized PATH\n'
else
  pass "Homebrew absent"
fi

CHECK_LINKS=false
if xcode-select -p >/dev/null 2>&1 && command -v otool >/dev/null 2>&1; then
  CHECK_LINKS=true
else
  printf 'note - developer tools unavailable; skipping otool linkage checks\n'
fi

for tool in ffmpeg ffprobe edge264_test mkvextract mkvmerge MP4Box spatial-media-kit-tool tesseract; do
  TOOL_PATH="$APP_BIN_DIR/$tool"
  [ -x "$TOOL_PATH" ] || fail "missing or non-executable bundled tool: $TOOL_PATH"
  case "$tool" in
    ffmpeg|ffprobe) "$TOOL_PATH" -hide_banner -version >/dev/null 2>&1 || fail "$tool did not run" ;;
    mkvextract|mkvmerge|tesseract) "$TOOL_PATH" --version >/dev/null 2>&1 || fail "$tool did not run" ;;
    MP4Box) "$TOOL_PATH" -version >/dev/null 2>&1 || fail "$tool did not run" ;;
    *) "$TOOL_PATH" --help >/dev/null 2>&1 || fail "$tool did not run" ;;
  esac
  if [ "$CHECK_LINKS" = true ]; then
    LINKED_LIBRARIES="$(otool -L "$TOOL_PATH")"
    case "$LINKED_LIBRARIES" in
      *"/opt/homebrew"*) fail "$tool links to /opt/homebrew" ;;
      *"/usr/local"*) fail "$tool links to /usr/local" ;;
    esac
  fi
done
pass "bundled tools"

VERSION_OUTPUT="$(PATH="$MINIMAL_PATH" "$APP_EXECUTABLE" --version 2>&1)"
case "$VERSION_OUTPUT" in
  *"Version $SHORT_VERSION"*) pass "CLI version" ;;
  *) fail "CLI version '$VERSION_OUTPUT' did not match Info.plist version '$SHORT_VERSION'" ;;
esac

PATH="$MINIMAL_PATH" "$APP_EXECUTABLE" --help >/dev/null 2>&1 || fail "CLI help failed"
pass "CLI help with sanitized PATH"

if [ -x /Applications/MakeMKV.app/Contents/MacOS/makemkvcon ]; then
  pass "MakeMKV installed"
else
  printf 'note - MakeMKV absent; GUI preflight should ask the user to install MakeMKV\n'
fi

printf 'Release app smoke passed: %s\n' "$APP_PATH"
