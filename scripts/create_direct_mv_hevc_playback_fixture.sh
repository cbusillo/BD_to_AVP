#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
acceptance_profile=0
if [[ "${1:-}" == "--acceptance" ]]; then
	acceptance_profile=1
	shift
fi
output_path="${1:-/tmp/bd-to-avp-direct-spatial-fixture/Probe.mov}"
encoder="${2:-$repo_root/build/mv-hevc-encoder/mv-hevc-encoder}"
frame_count=180
duration_seconds=6
if [[ "$acceptance_profile" -eq 1 ]]; then
	frame_count=720
	duration_seconds=24
fi
work_dir="$(mktemp -d)"
trap 'rm -rf "$work_dir"' EXIT

for command_name in ffmpeg ffprobe uv; do
	if ! command -v "$command_name" >/dev/null 2>&1; then
		printf 'Required command is unavailable: %s\n' "$command_name" >&2
		exit 1
	fi
done

mp4box="$repo_root/bd_to_avp/bin/MP4Box"
if [[ ! -x "$mp4box" ]]; then
	printf 'Required bundled tool is unavailable: %s\n' "$mp4box" >&2
	exit 1
fi
if [[ ! -x "$encoder" ]]; then
	if [[ $# -ge 2 ]]; then
		printf 'Requested MV-HEVC encoder is unavailable: %s\n' "$encoder" >&2
		exit 1
	fi
	uv run python "$repo_root/scripts/build_mv_hevc_encoder_macos.py" --output "$encoder"
fi
automatic_quality="$(
	cd "$repo_root"
	uv run python -c 'from bd_to_avp.modules.video_quality_defaults import AUTOMATIC_DIRECT_UPSCALE_QUALITY; print(AUTOMATIC_DIRECT_UPSCALE_QUALITY)'
)"

mkdir -p "$(dirname "$output_path")"

font_file="/System/Library/Fonts/Helvetica.ttc"
left_filter="drawgrid=width=120:height=120:thickness=2:color=white@0.18,drawbox=x=156:y=174:w=474:h=246:color=0x2878ff@0.92:t=fill,drawtext=fontfile='${font_file}':text='BLUE  BEHIND':x=210:y=264:fontsize=60:fontcolor=white,drawbox=x=723:y=453:w=474:h=246:color=0x20a45b@0.92:t=fill,drawtext=fontfile='${font_file}':text='GREEN  SCREEN':x=750:y=543:fontsize=57:fontcolor=white,drawbox=x=1290:y=732:w=474:h=246:color=0xe34242@0.94:t=fill,drawtext=fontfile='${font_file}':text='RED  IN FRONT':x=1323:y=822:fontsize=57:fontcolor=white"
right_filter="drawgrid=width=120:height=120:thickness=2:color=white@0.18,drawbox=x=180:y=174:w=474:h=246:color=0x2878ff@0.92:t=fill,drawtext=fontfile='${font_file}':text='BLUE  BEHIND':x=234:y=264:fontsize=60:fontcolor=white,drawbox=x=723:y=453:w=474:h=246:color=0x20a45b@0.92:t=fill,drawtext=fontfile='${font_file}':text='GREEN  SCREEN':x=750:y=543:fontsize=57:fontcolor=white,drawbox=x=1218:y=732:w=474:h=246:color=0xe34242@0.94:t=fill,drawtext=fontfile='${font_file}':text='RED  IN FRONT':x=1251:y=822:fontsize=57:fontcolor=white"
if [[ "$acceptance_profile" -eq 1 ]]; then
	left_filter+=",drawbox=x=840:y=924:w=240:h=120:color=white@0.70:t=fill:enable='gte(t,2)*lt(mod(t-2,2),0.1)',drawtext=fontfile='${font_file}':text='LEFT EYE ONLY':x=70:y=60:fontsize=48:fontcolor=white,drawtext=fontfile='${font_file}':text='TIME  %{eif\\:t\\:d} sec':x=950:y=60:fontsize=48:fontcolor=white,drawtext=fontfile='${font_file}':text='SYNC  %{eif\\:floor(t/2)\\:d}':x=1500:y=60:fontsize=48:fontcolor=white"
	right_filter+=",drawbox=x=840:y=924:w=240:h=120:color=white@0.70:t=fill:enable='gte(t,2)*lt(mod(t-2,2),0.1)',drawtext=fontfile='${font_file}':text='RIGHT EYE ONLY':x=70:y=60:fontsize=48:fontcolor=white,drawtext=fontfile='${font_file}':text='TIME  %{eif\\:t\\:d} sec':x=950:y=60:fontsize=48:fontcolor=white,drawtext=fontfile='${font_file}':text='SYNC  %{eif\\:floor(t/2)\\:d}':x=1500:y=60:fontsize=48:fontcolor=white"
fi

ffmpeg -hide_banner -loglevel error \
	-f lavfi -i 'testsrc2=size=1920x1080:rate=30' \
	-filter_complex "[0:v]split=2[left_source][right_source];[left_source]${left_filter}[left];[right_source]${right_filter}[right];[left][right]hstack=inputs=2,format=yuv420p[stereo]" \
	-map '[stereo]' -frames:v "$frame_count" -f yuv4mpegpipe - |
	"$encoder" \
		--output "$work_dir/spatial.mov" \
		--quality "$automatic_quality" \
		--upscale-mode metalfx \
		--fov 90 \
		--baseline-mm 64 \
		--disparity-adjustment 0 \
		--expected-frames "$frame_count" \
		--overwrite

if [[ "$acceptance_profile" -eq 1 ]]; then
	ffmpeg -hide_banner -loglevel error -f lavfi -i "aevalsrc=0.18*sin(2*PI*880*t)*between(mod(t-2\\,2)\\,0\\,0.1)*gte(t\\,2)|0.18*sin(2*PI*880*t)*between(mod(t-2\\,2)\\,0\\,0.1)*gte(t\\,2):s=48000:d=$duration_seconds:c=stereo" -c:a aac -b:a 192k -metadata:s:a:0 language=eng -y "$work_dir/audio.m4a"
else
	ffmpeg -hide_banner -loglevel error -f lavfi -i 'sine=frequency=880:sample_rate=48000' -t 6 -c:a aac -b:a 192k -metadata:s:a:0 language=eng -y "$work_dir/audio.m4a"
fi

if [[ "$acceptance_profile" -eq 1 ]]; then
	: >"$work_dir/subtitles.srt"
	for ((cue = 1; cue <= duration_seconds / 2 - 1; cue++)); do
		start=$((cue * 2))
		end=$((start + 1))
		printf '%d\n00:00:%02d,000 --> 00:00:%02d,000\nSYNC cue %d: white patch and beep\n\n' "$cue" "$start" "$end" "$cue" >>"$work_dir/subtitles.srt"
	done
else
	cat >"$work_dir/subtitles.srt" <<'EOF'
1
00:00:00,500 --> 00:00:02,500
Blue behind, green on screen, red in front

2
00:00:03,000 --> 00:00:05,500
Direct MV-HEVC beginning, middle, and end seek fixture
EOF
fi

"$mp4box" -new \
	-add "$work_dir/spatial.mov:forcesync" \
	-add "$work_dir/audio.m4a#1:lang=eng:group=1:alternate_group=1" \
	-add "$work_dir/subtitles.srt#1:hdlr=sbtl:lang=eng:group=2:name=English Subtitles:tx3g" \
	"$work_dir/finalized.mov"

if [[ "$acceptance_profile" -eq 1 ]]; then
	# Normalize signed composition offsets before fragmentation. Otherwise GPAC
	# adds a cslg shift to the video timeline, delaying flashes two frames behind
	# the AAC cues. Nonnegative CTTS plus an edit list preserves presentation time.
	"$mp4box" -add 'self#1:negctts=no' "$work_dir/finalized.mov"
fi

uv run python "$repo_root/scripts/add_spatial_video_metadata.py" \
	"$work_dir/finalized.mov" \
	"$output_path" \
	--baseline-mm 64 \
	--disparity-adjustment 0

uv run python "$repo_root/scripts/verify_apple_media.py" "$output_path"
"$mp4box" -diso "$output_path" -std >"$work_dir/boxes.xml"
for box_type in hvcC lhvC vexu eyes proj hfov; do
	if ! grep -q "Type=\"${box_type}\"" "$work_dir/boxes.xml"; then
		printf 'Direct playback fixture is missing required box: %s\n' "$box_type" >&2
		exit 1
	fi
done
if [[ "$acceptance_profile" -eq 1 ]]; then
	seek_positions=(0 2 12 23.9)
else
	seek_positions=(0 3 5.9)
fi
for seek_position in "${seek_positions[@]}"; do
	ffmpeg -hide_banner -loglevel error -ss "$seek_position" -i "$output_path" -frames:v 1 -f null -
done
ffprobe -v error \
	-show_entries format=duration:stream=index,codec_name,codec_type,width,height:stream_tags=language \
	-of json "$output_path" >"$work_dir/final-probe.json"
uv run python - "$work_dir/final-probe.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as probe_file:
    probe = json.load(probe_file)

streams = probe.get("streams", [])
video_dimensions = {
    (stream.get("width"), stream.get("height"))
    for stream in streams
    if stream.get("codec_type") == "video"
}
if video_dimensions != {(3840, 2160)}:
    raise SystemExit(f"Direct 4K playback fixture has unexpected video dimensions: {sorted(video_dimensions)!r}")
if not any(
    stream.get("codec_type") == "audio"
    and stream.get("codec_name") == "aac"
    and stream.get("tags", {}).get("language") == "eng"
    for stream in streams
):
    raise SystemExit("Direct 4K playback fixture is missing English AAC audio.")
if not any(
    stream.get("codec_type") == "subtitle" and stream.get("tags", {}).get("language") == "eng"
    for stream in streams
):
    raise SystemExit("Direct 4K playback fixture is missing English subtitles.")
PY
cat "$work_dir/final-probe.json"
printf 'Created direct 4K MetalFX MV-HEVC playback fixture: %s\n' "$output_path"
