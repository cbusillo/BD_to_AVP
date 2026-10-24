#!/bin/zsh
# Throwaway spike for #1103: build, install and launch the stereo display spike.
# Usage: build.sh <clip.264> <clip_mvhevc_labeled.mov> [device-udid]
# Reuses #1101's edge264-mvc build steps for the xros static library.
# Make the MV-HEVC clip for mode B with the app's own tools and a B label in both eyes:
#   edge264_test clip.264 -O | ffmpeg -f yuv4mpegpipe -i - -vf "<drawbox+drawtext at x=1660 and x=3580, y=72>" \
#     -f yuv4mpegpipe -strict -1 - | mv-hevc-encoder --output clip_mvhevc_labeled.mov --bitrate-mbps 40
set -euo pipefail
here=${0:A:h}
clip=${1:?clip path}
mvhevc=${2:?MV-HEVC clip path}
device=${3:-00008112-001108C63A78A01E}
bundle=com.shinycomputers.bd-to-avp.stereo-spike
pin=$(python3 -I -c 'import json,sys;print(json.load(open(sys.argv[1]))["revision"])' \
  "$here/../../bd_to_avp/resources/notices/edge264-mvc-build.json")

if [[ ! -d $here/edge264-src ]]; then
  git clone -q --filter=blob:none https://github.com/jens-duttke/edge264-mvc.git "$here/edge264-src"
fi
git -C "$here/edge264-src" checkout -q --detach "$pin"
if [[ ! -f $here/edge264-src/libedge264mvc.a ]]; then
  sdk=$(xcrun --sdk xros --show-sdk-path)
  make -C "$here/edge264-src" OS=ios HOST_OS=distribution CC="$(xcrun --sdk xros --find clang)" \
    VARIANTS= BUILDTEST=no CFLAGS="-target arm64-apple-xros2.0 -isysroot $sdk" libedge264mvc.a
fi

mkdir -p "$here/Resources"
ln -f "$clip" "$here/Resources/clip.264" 2>/dev/null || cp "$clip" "$here/Resources/clip.264"
ln -f "$mvhevc" "$here/Resources/clip_mvhevc_labeled.mov" 2>/dev/null || cp "$mvhevc" "$here/Resources/clip_mvhevc_labeled.mov"
(cd "$here" && xcodegen generate)
xcodebuild -project "$here/StereoSpike.xcodeproj" -scheme StereoSpike -configuration Release \
  -destination 'generic/platform=visionOS' -derivedDataPath "$here/build/dd" -allowProvisioningUpdates build
xcrun devicectl device install app --device "$device" "$here/build/dd/Build/Products/Release-xros/StereoSpike.app"
xcrun devicectl device process launch --device "$device" --terminate-existing "$bundle"
echo "Copy results when done:"
echo "xcrun devicectl device copy from --device $device --domain-type appDataContainer --domain-identifier $bundle --source Documents --destination results/device"
