#!/bin/zsh
# Throwaway spike for #1101: rebuild and run the headset MVC decode benchmark.
# Usage: build.sh <clip.264> [device-udid]
set -euo pipefail
here=${0:A:h}
clip=${1:?clip path}
device=${2:-00008112-001108C63A78A01E}
bundle=com.shinycomputers.bd-to-avp.mvc-spike
pin=$(python3 -I -c 'import json,sys;print(json.load(open(sys.argv[1]))["revision"])' \
  "$here/../../bd_to_avp/resources/notices/edge264-mvc-build.json")

if [[ ! -d $here/edge264-src ]]; then
  git clone -q --filter=blob:none https://github.com/jens-duttke/edge264-mvc.git "$here/edge264-src"
fi
git -C "$here/edge264-src" checkout -q --detach "$pin"
sdk=$(xcrun --sdk xros --show-sdk-path)
make -C "$here/edge264-src" OS=ios HOST_OS=distribution CC="$(xcrun --sdk xros --find clang)" \
  VARIANTS= BUILDTEST=no CFLAGS="-target arm64-apple-xros2.0 -isysroot $sdk" libedge264mvc.a

mkdir -p "$here/Resources"
ln -f "$clip" "$here/Resources/clip.264" 2>/dev/null || cp "$clip" "$here/Resources/clip.264"
(cd "$here" && xcodegen generate)
xcodebuild -project "$here/MVCSpike.xcodeproj" -scheme MVCSpike -configuration Release \
  -destination 'generic/platform=visionOS' -derivedDataPath "$here/build/dd" -allowProvisioningUpdates build
xcrun devicectl device install app --device "$device" "$here/build/dd/Build/Products/Release-xros/MVCSpike.app"
xcrun devicectl device process launch --device "$device" --terminate-existing "$bundle"
echo "Copy the result when complete:"
echo "xcrun devicectl device copy from --device $device --domain-type appDataContainer --domain-identifier $bundle --source Documents/mvc-spike-result.json --destination mvc-spike-result.json"
