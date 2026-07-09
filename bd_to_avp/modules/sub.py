import threading
from pathlib import Path
from typing import Any

import ffmpeg
from babelfish import Error as BabelfishError, Language
from bd_to_avp.vendor.pgsrip import Mkv, Options, pgsrip
from bd_to_avp.vendor.pgsrip.mkv import MkvPgs

from bd_to_avp.modules.config import config, Stage
from bd_to_avp.modules.command import Spinner


class SRTCreationError(Exception):
    pass


def create_srt_from_mkv(mkv_path: Path) -> None:
    if config.start_stage.value <= Stage.EXTRACT_SUBTITLES.value:
        if config.skip_subtitles:
            cleanup_existing_subtitle_files(mkv_path.parent)
            return None
        extract_subtitle_to_srt(mkv_path)


def extract_subtitle_to_srt(mkv_path: Path) -> None:
    output_path = mkv_path.parent

    cleanup_existing_subtitle_files(output_path)

    if config.skip_subtitles:
        return None
    subtitle_tracks = get_languages_in_mkv(mkv_path)

    if not subtitle_tracks:
        print("No PGS subtitle tracks found in source; continuing without subtitles.")
        return None

    sub_options = subtitle_rip_options()

    spinner = Spinner("Sup subtitles extraction and SRT conversion")
    spinner_thread = threading.Thread(target=spinner.start)
    spinner_thread.start()

    try:
        mkv_file = Mkv(mkv_path.as_posix())
        selected_subtitle_tracks = get_selected_subtitle_tracks(mkv_file, sub_options)

        pgsrip.rip(mkv_file, sub_options)

        for srt_file in output_path.glob("*.srt"):
            if srt_file.stat().st_size == 0:
                srt_file.unlink()

        if not any(output_path.glob("*.srt")) and not config.continue_on_error:
            raise SRTCreationError("No SRT subtitle files with data created.")

        mark_forced_srt_files(selected_subtitle_tracks)
    finally:
        spinner.stop()
        spinner_thread.join()


def cleanup_existing_subtitle_files(output_path: Path) -> None:
    for subtitle_path in output_path.glob("*.srt"):
        subtitle_path.unlink()


def subtitle_rip_options() -> Options:
    languages = set()
    if config.remove_extra_languages:
        try:
            languages.add(Language.fromietf(config.language_code))
        except (BabelfishError, ValueError):
            print(f"Invalid subtitle language code {config.language_code!r}; extracting all subtitle languages.")

    return Options(overwrite=True, one_per_lang=False, keep_temp_files=config.keep_files, languages=languages)


def get_selected_subtitle_tracks(mkv_file: Mkv, sub_options: Options) -> list[dict[str, Any]]:
    selected_tracks: list[dict[str, Any]] = []
    for track, language, number in mkv_file.get_selected_pgs_tracks(sub_options):
        selected_tracks.append(
            {
                "index": track.id,
                "language": str(track.language),
                "forced": 1 if track.forced else 0,
                "srt_path": Path(str(MkvPgs.expected_srt_path(mkv_file.media_path, language, number))),
            }
        )
    return selected_tracks


def mark_forced_srt_files(subtitle_tracks: list[dict[str, Any]]) -> None:
    for track in subtitle_tracks:
        if track["forced"] != 1:
            continue

        forced_srt_file = track["srt_path"]
        if not forced_srt_file.exists():
            print(f"Forced subtitle track {track['index']} did not create an SRT file.")
            continue

        if ".forced." in forced_srt_file.stem:
            continue

        forced_stem = forced_subtitle_stem(forced_srt_file)
        forced_srt_file.rename(forced_srt_file.with_stem(forced_stem))


def forced_subtitle_stem(subtitle_path: Path) -> str:
    stem = subtitle_path.stem
    language_suffix = subtitle_path.with_suffix("").suffix
    if not language_suffix or not stem.endswith(language_suffix):
        return f"{stem}.forced"

    return f"{stem[: -len(language_suffix)]}.forced{language_suffix}"


def subtitle_language_alpha2(language_code: str) -> str | None:
    if not language_code or language_code == "und":
        return None
    try:
        return Language.fromietf(language_code).alpha2
    except (BabelfishError, ValueError):
        return None


def get_languages_in_mkv(mkv_path: Path) -> None | list[dict[str, Any]]:
    mkv_info = ffmpeg.probe(str(mkv_path), cmd=config.FFPROBE_PATH.as_posix())
    streams = mkv_info["streams"]
    subtitle_streams = [
        stream
        for stream in streams
        if stream["codec_type"] == "subtitle" and stream.get("codec_name") == "hdmv_pgs_subtitle"
    ]
    if not subtitle_streams:
        print("No PGS subtitle streams found in MKV.")
        return None
    subtitle_info = []
    for stream in subtitle_streams:
        info = {
            "index": stream["index"],
            "language": stream.get("tags", {}).get("language", "und") or "und",
            "default": stream["disposition"].get("default", 0),
            "forced": stream["disposition"].get("forced", 0),
        }
        subtitle_info.append(info)
    return subtitle_info
