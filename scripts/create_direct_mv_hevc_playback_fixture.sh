#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
output_path="${1:-/tmp/bd-to-avp-direct-spatial-fixture/Probe.mov}"
work_dir="$(mktemp -d)"
trap 'rm -rf "$work_dir"' EXIT

for command_name in ffmpeg ffprobe python3 uv; do
	if ! command -v "$command_name" >/dev/null 2>&1; then
		printf 'Required command is unavailable: %s\n' "$command_name" >&2
		exit 1
	fi
done

encoder="$repo_root/build/mv-hevc-encoder/mv-hevc-encoder"
mp4box="$repo_root/bd_to_avp/bin/MP4Box"
if [[ ! -x "$mp4box" ]]; then
	printf 'Required bundled tool is unavailable: %s\n' "$mp4box" >&2
	exit 1
fi
if [[ ! -x "$encoder" ]]; then
	uv run python "$repo_root/scripts/build_mv_hevc_encoder_macos.py" --output "$encoder"
fi

mkdir -p "$(dirname "$output_path")"

font_file="/System/Library/Fonts/Helvetica.ttc"
left_filter="drawgrid=width=40:height=40:thickness=1:color=white@0.18,drawbox=x=52:y=58:w=158:h=82:color=0x2878ff@0.92:t=fill,drawtext=fontfile='${font_file}':text='BLUE  BEHIND':x=70:y=88:fontsize=20:fontcolor=white,drawbox=x=241:y=151:w=158:h=82:color=0x20a45b@0.92:t=fill,drawtext=fontfile='${font_file}':text='GREEN  SCREEN':x=250:y=181:fontsize=19:fontcolor=white,drawbox=x=430:y=244:w=158:h=82:color=0xe34242@0.94:t=fill,drawtext=fontfile='${font_file}':text='RED  IN FRONT':x=441:y=274:fontsize=19:fontcolor=white"
right_filter="drawgrid=width=40:height=40:thickness=1:color=white@0.18,drawbox=x=60:y=58:w=158:h=82:color=0x2878ff@0.92:t=fill,drawtext=fontfile='${font_file}':text='BLUE  BEHIND':x=78:y=88:fontsize=20:fontcolor=white,drawbox=x=241:y=151:w=158:h=82:color=0x20a45b@0.92:t=fill,drawtext=fontfile='${font_file}':text='GREEN  SCREEN':x=250:y=181:fontsize=19:fontcolor=white,drawbox=x=406:y=244:w=158:h=82:color=0xe34242@0.94:t=fill,drawtext=fontfile='${font_file}':text='RED  IN FRONT':x=417:y=274:fontsize=19:fontcolor=white"

ffmpeg -hide_banner -loglevel error \
	-f lavfi -i 'testsrc2=size=640x360:rate=30' \
	-filter_complex "[0:v]split=2[left_source][right_source];[left_source]${left_filter}[left];[right_source]${right_filter}[right];[left][right]hstack=inputs=2,format=yuv420p[stereo]" \
	-map '[stereo]' -frames:v 180 -f yuv4mpegpipe - |
	"$encoder" \
		--output "$work_dir/spatial.mov" \
		--bitrate-mbps 8 \
		--fov 90 \
		--baseline-mm 64 \
		--disparity-adjustment 0 \
		--expected-frames 180 \
		--overwrite

ffmpeg -hide_banner -loglevel error -f lavfi -i 'sine=frequency=880:sample_rate=48000' -t 6 -c:a aac -b:a 192k -metadata:s:a:0 language=eng -y "$work_dir/audio.m4a"

cat >"$work_dir/subtitles.srt" <<'EOF'
1
00:00:00,500 --> 00:00:02,500
Blue behind, green on screen, red in front

2
00:00:03,000 --> 00:00:05,500
Direct MV-HEVC beginning, middle, and end seek fixture
EOF

"$mp4box" -new \
	-add "$work_dir/spatial.mov:forcesync" \
	-add "$work_dir/audio.m4a#1:lang=eng:group=1:alternate_group=1" \
	-add "$work_dir/subtitles.srt#1:hdlr=sbtl:lang=eng:group=2:name=English Subtitles:tx3g" \
	"$work_dir/finalized.mov"

python3 "$repo_root/scripts/add_spatial_video_metadata.py" \
	"$work_dir/finalized.mov" \
	"$output_path" \
	--baseline-mm 64 \
	--disparity-adjustment 0

python3 "$repo_root/scripts/verify_apple_media.py" "$output_path"
"$mp4box" -diso "$output_path" -std >"$work_dir/boxes.xml"
for box_type in hvcC lhvC vexu eyes proj hfov; do
	if ! grep -q "Type=\"${box_type}\"" "$work_dir/boxes.xml"; then
		printf 'Direct playback fixture is missing required box: %s\n' "$box_type" >&2
		exit 1
	fi
done
for seek_position in 0 3 5.9; do
	ffmpeg -hide_banner -loglevel error -ss "$seek_position" -i "$output_path" -frames:v 1 -f null -
done
ffprobe -v error -show_entries format=duration:stream=index,codec_name,codec_type:stream_tags=language -of json "$output_path"
printf 'Created direct MV-HEVC playback fixture: %s\n' "$output_path"
