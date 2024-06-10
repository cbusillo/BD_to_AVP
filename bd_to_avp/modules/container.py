from pathlib import Path

import ffmpeg

from bd_to_avp.modules.config import Stage, config
from bd_to_avp.modules.sub import convert_sup_to_srt, get_subtitle_tracks
from bd_to_avp.modules.util import run_command, run_ffmpeg_print_errors


def extract_mvc_audio_and_subtitle(
    input_path: Path,
    video_output_path: Path,
    audio_output_path: Path,
    subtitle_output_path: Path | None,
    subtitle_track: int,
) -> None:

    stream = ffmpeg.input(str(input_path))

    video_stream = ffmpeg.output(stream["v:0"], f"file:{video_output_path}", c="copy", bsf="h264_mp4toannexb")
    audio_stream = ffmpeg.output(stream["a:0"], f"file:{audio_output_path}", c="pcm_s24le")

    print("Running ffmpeg to extract video, audio, and subtitles from MKV")
    if subtitle_output_path:
        subtitle_stream = ffmpeg.output(stream[f"s:{subtitle_track}"], f"file:{subtitle_output_path}", c="copy")
        run_ffmpeg_print_errors([video_stream, audio_stream, subtitle_stream], overwrite_output=True)
    else:
        run_ffmpeg_print_errors([video_stream, audio_stream], overwrite_output=True)


def create_muxed_file(
    audio_path: Path,
    mv_hevc_path: Path,
    subtitle_path: Path | None,
    output_folder: Path,
    disc_name: str,
) -> Path:
    muxed_path = output_folder / f"{disc_name}{config.FINAL_FILE_TAG}.mov"
    if config.start_stage.value <= Stage.CREATE_FINAL_FILE.value:
        mux_video_audio_subs(mv_hevc_path, audio_path, muxed_path, subtitle_path)

    if not config.keep_files:
        mv_hevc_path.unlink(missing_ok=True)
        audio_path.unlink(missing_ok=True)
    return muxed_path


def create_mvc_audio_and_subtitle_files(
    disc_name: str,
    mkv_output_path: Path | None,
    output_folder: Path,
) -> tuple[Path, Path, Path | None]:
    video_output_path = output_folder / f"{disc_name}_mvc.h264"
    audio_output_path = output_folder / f"{disc_name}_audio_PCM.mov"

    subtitle_output_path = None
    subtitle_tracks: list[dict[str, int]] = []

    if config.start_stage.value <= Stage.EXTRACT_MVC_AUDIO_AND_SUB.value and mkv_output_path:
        if not config.skip_subtitles and (subtitle_tracks := get_subtitle_tracks(mkv_output_path)):

            subtitle_output_path = output_folder / f"{disc_name}_subtitles{subtitle_tracks[0]['extension']}"

        extract_mvc_audio_and_subtitle(
            mkv_output_path,
            video_output_path,
            audio_output_path,
            subtitle_output_path,
            int(subtitle_tracks[0].get("index", 0)) if subtitle_tracks else 0,
        )
        if subtitle_output_path and subtitle_output_path.suffix.lower() == ".sup":
            subtitle_output_path = convert_sup_to_srt(subtitle_output_path)
    else:
        if (output_folder / f"{disc_name}_subtitles.srt").exists():
            subtitle_output_path = output_folder / f"{disc_name}_subtitles.srt"

    # if (
    #     not input_args.keep_files
    #     and mkv_output_path
    #     and input_args.source_path != mkv_output_path
    # ):
    #     mkv_output_path.unlink(missing_ok=True)
    return audio_output_path, video_output_path, subtitle_output_path


def mux_video_audio_subs(mv_hevc_path: Path, audio_path: Path, muxed_path: Path, sub_path: Path | None) -> None:

    command = [
        config.MP4BOX_PATH,
        "-new",
        "-lang",
        "eng",
        "-add",
        mv_hevc_path,
        "-add",
        audio_path,
    ]
    if sub_path:
        command += [
            "-add",
            f"{sub_path}:hdlr=sbtl:group=2:name=English Subtitles:tx3g",
        ]

    command += [muxed_path]
    run_command(command, "Mux video, audio, and subtitles.")
