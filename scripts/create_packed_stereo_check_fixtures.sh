#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
output_dir="${1:-$repo_root/macos/BDToAVPPlayer/Resources}"
font_file="/System/Library/Fonts/Helvetica.ttc"
duration_seconds=45
frame_rate=30

for command_name in ffmpeg ffprobe; do
	if ! command -v "$command_name" >/dev/null 2>&1; then
		printf 'Required command is unavailable: %s\n' "$command_name" >&2
		exit 1
	fi
done

if [[ ! -f "$font_file" ]]; then
	printf 'Required font is unavailable: %s\n' "$font_file" >&2
	exit 1
fi

mkdir -p "$output_dir"

left_filter="drawgrid=width=48:height=48:thickness=1:color=white@0.10,drawbox=x=42:y=40:w=876:h=460:color=0x101823@0.92:t=fill,drawbox=x=58:y=56:w=844:h=428:color=0x2563eb@0.92:t=4,drawtext=fontfile='${font_file}':text='LEFT EYE ONLY':x=(w-text_w)/2:y=82:fontsize=76:fontcolor=white,drawtext=fontfile='${font_file}':text='COVER YOUR RIGHT EYE':x=(w-text_w)/2:y=180:fontsize=34:fontcolor=white,drawbox=x=122:y=268:w=190:h=112:color=0x2878ff@0.96:t=fill,drawtext=fontfile='${font_file}':text='BLUE':x=169:y=302:fontsize=34:fontcolor=white,drawbox=x=392:y=268:w=190:h=112:color=0x20a45b@0.92:t=fill,drawtext=fontfile='${font_file}':text='SCREEN':x=420:y=302:fontsize=30:fontcolor=white,drawbox=x=662:y=268:w=190:h=112:color=0xe34242@0.94:t=fill,drawtext=fontfile='${font_file}':text='RED':x=720:y=302:fontsize=34:fontcolor=white,drawtext=fontfile='${font_file}':text='%{pts\\:hms}':x=w-text_w-58:y=430:fontsize=28:fontcolor=white"
right_filter="drawgrid=width=48:height=48:thickness=1:color=white@0.10,drawbox=x=42:y=40:w=876:h=460:color=0x23160f@0.92:t=fill,drawbox=x=58:y=56:w=844:h=428:color=0xf97316@0.92:t=4,drawtext=fontfile='${font_file}':text='RIGHT EYE ONLY':x=(w-text_w)/2:y=82:fontsize=76:fontcolor=white,drawtext=fontfile='${font_file}':text='COVER YOUR LEFT EYE':x=(w-text_w)/2:y=180:fontsize=34:fontcolor=white,drawbox=x=98:y=268:w=190:h=112:color=0x2878ff@0.96:t=fill,drawtext=fontfile='${font_file}':text='BLUE':x=145:y=302:fontsize=34:fontcolor=white,drawbox=x=392:y=268:w=190:h=112:color=0x20a45b@0.92:t=fill,drawtext=fontfile='${font_file}':text='SCREEN':x=420:y=302:fontsize=30:fontcolor=white,drawbox=x=686:y=268:w=190:h=112:color=0xe34242@0.94:t=fill,drawtext=fontfile='${font_file}':text='RED':x=744:y=302:fontsize=34:fontcolor=white,drawtext=fontfile='${font_file}':text='%{pts\\:hms}':x=w-text_w-58:y=430:fontsize=28:fontcolor=white"

create_fixture() {
	local layout="$1"
	local output_path="$2"
	local packing_filter
	if [[ "$layout" == "sbs" ]]; then
		packing_filter='[left][right]hstack=inputs=2[packed]'
	else
		packing_filter='[left][right]vstack=inputs=2[packed]'
	fi

	ffmpeg -hide_banner -loglevel error \
		-f lavfi -i "color=c=0x080c12:s=960x540:r=${frame_rate}" \
		-f lavfi -i "color=c=0x080c12:s=960x540:r=${frame_rate}" \
		-filter_complex "[0:v]${left_filter}[left];[1:v]${right_filter}[right];${packing_filter}" \
		-map '[packed]' \
		-t "$duration_seconds" \
		-c:v hevc_videotoolbox \
		-tag:v hvc1 \
		-b:v 400k \
		-pix_fmt yuv420p \
		-color_range tv \
		-colorspace bt709 \
		-color_primaries bt709 \
		-color_trc bt709 \
		-an \
		-movflags +faststart \
		-y "$output_path"
}

sbs_path="$output_dir/Stereo-Check-SBS.mov"
ou_path="$output_dir/Stereo-Check-OU.mov"
create_fixture sbs "$sbs_path"
create_fixture ou "$ou_path"

validate_fixture() {
	local expected_width="$1"
	local expected_height="$2"
	local path="$3"
	local stream
	stream="$(ffprobe -v error -select_streams v:0 -show_entries stream=codec_name,pix_fmt,width,height,color_range -of csv=p=0 "$path")"
	if [[ "$stream" != hevc,"$expected_width","$expected_height",yuv420p,tv ]]; then
		printf 'Unexpected fixture stream for %s: %s\n' "$path" "$stream" >&2
		exit 1
	fi
}

validate_fixture 1920 540 "$sbs_path"
validate_fixture 960 1080 "$ou_path"

printf 'Created packed-stereo checks:\n  %s\n  %s\n' "$sbs_path" "$ou_path"
