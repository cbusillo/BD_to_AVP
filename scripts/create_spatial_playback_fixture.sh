#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
output_path="${1:-/tmp/bd-to-avp-spatial-fixture/Probe.mov}"
work_dir="$(mktemp -d)"
trap 'rm -rf "$work_dir"' EXIT

for command_name in ffmpeg ffprobe; do
	if ! command -v "$command_name" >/dev/null 2>&1; then
		printf 'Required command is unavailable: %s\n' "$command_name" >&2
		exit 1
	fi
done

spatial_tool="$repo_root/bd_to_avp/bin/spatial-media-kit-tool"
mp4box="$repo_root/bd_to_avp/bin/MP4Box"
for tool_path in "$spatial_tool" "$mp4box"; do
	if [[ ! -x "$tool_path" ]]; then
		printf 'Required bundled tool is unavailable: %s\n' "$tool_path" >&2
		exit 1
	fi
done

mkdir -p "$(dirname "$output_path")"

ffmpeg -hide_banner -loglevel error -f lavfi -i 'testsrc2=size=656x360:rate=30' -t 6 -vf 'crop=640:360:0:0' -c:v hevc_videotoolbox -tag:v hvc1 -b:v 2M -an -y "$work_dir/left.mov"
ffmpeg -hide_banner -loglevel error -f lavfi -i 'testsrc2=size=656x360:rate=30' -t 6 -vf 'crop=640:360:16:0' -c:v hevc_videotoolbox -tag:v hvc1 -b:v 2M -an -y "$work_dir/right.mov"

"$spatial_tool" merge \
	--left-file "$work_dir/left.mov" \
	--right-file "$work_dir/right.mov" \
	--quality 60 \
	--left-is-primary \
	--horizontal-field-of-view 90 \
	--horizontal-disparity-adjustment 0 \
	--output-file "$work_dir/spatial.mov"

ffmpeg -hide_banner -loglevel error -f lavfi -i 'sine=frequency=880:sample_rate=48000' -t 6 -c:a aac -b:a 192k -metadata:s:a:0 language=eng -y "$work_dir/audio.m4a"

cat >"$work_dir/subtitles.srt" <<'EOF'
1
00:00:00,500 --> 00:00:02,500
BD to AVP guided playback check

2
00:00:03,000 --> 00:00:05,500
Beginning, middle, and end seek fixture
EOF

"$mp4box" -new \
	-add "$work_dir/spatial.mov:forcesync" \
	-add "$work_dir/audio.m4a#1:lang=eng:group=1:alternate_group=1" \
	-add "$work_dir/subtitles.srt#1:hdlr=sbtl:lang=eng:group=2:name=English Subtitles:tx3g" \
	"$output_path"

ffprobe -v error -show_entries format=duration:stream=index,codec_name,codec_type:stream_tags=language -of json "$output_path"
printf 'Created spatial playback fixture: %s\n' "$output_path"
