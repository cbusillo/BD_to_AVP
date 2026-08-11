#!/bin/sh
set -eu

script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
scenario=${1:-running}
appearance=${2:-light}
output=${3:-"$script_dir/screenshots/queue-$scenario-$appearance.png"}
width=${4:-1180}
height=${5:-760}
app="$script_dir/build/BD to AVP Queue Mockup.app"

if [ ! -d "$app" ]; then
    "$script_dir/build.sh" >/dev/null
fi

mkdir -p "$(dirname -- "$output")"
open -na "$app" --args "--scenario=$scenario" "--appearance=$appearance"
sleep 2

osascript <<EOF
tell application "BD to AVP Queue Mockup" to activate
tell application "System Events"
    tell process "BD to AVP Queue Mockup"
        set position of front window to {80, 80}
        set size of front window to {$width, $height}
    end tell
end tell
EOF

sleep 1
screencapture -x -R80,80,$width,$height "$output"
osascript -e 'tell application "BD to AVP Queue Mockup" to quit'
printf '%s\n' "$output"
