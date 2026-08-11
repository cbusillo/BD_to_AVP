#!/bin/sh
set -eu

script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
build_dir="$script_dir/build"
app_dir="$build_dir/BD to AVP Queue Mockup.app"
contents_dir="$app_dir/Contents"
binary_dir="$contents_dir/MacOS"

rm -rf "$app_dir"
mkdir -p "$binary_dir"
cp "$script_dir/Info.plist" "$contents_dir/Info.plist"

swiftc \
    -parse-as-library \
    -framework AppKit \
    -framework SwiftUI \
    "$script_dir/QueueMockupApp.swift" \
    "$script_dir/QueueMockupModels.swift" \
    "$script_dir/QueueMockupModel.swift" \
    "$script_dir/QueueMockupView.swift" \
    -o "$binary_dir/BDToAVPQueueMockup"

codesign --force --deep --sign - "$app_dir" >/dev/null
printf '%s\n' "$app_dir"
