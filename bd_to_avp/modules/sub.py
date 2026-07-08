import os
import threading
from pathlib import Path
from typing import Any

import ffmpeg
import requests
from babelfish import Error as BabelfishError, Language
from bd_to_avp.vendor.pgsrip import Mkv, Options, pgsrip

from bd_to_avp.modules.config import config, Stage
from bd_to_avp.modules.command import Spinner


class SRTCreationError(Exception):
    pass


def create_srt_from_mkv(mkv_path: Path) -> None:
    if config.start_stage.value <= Stage.EXTRACT_SUBTITLES.value:
        if config.skip_subtitles:
            return None
        extract_subtitle_to_srt(mkv_path)


def extract_subtitle_to_srt(mkv_path: Path) -> None:
    output_path = mkv_path.parent

    if config.skip_subtitles:
        return None
    tessdata_path = config.app.config_path / "tessdata"
    subtitle_tracks = get_languages_in_mkv(mkv_path)

    if not subtitle_tracks:
        print("No PGS subtitle tracks found in source; continuing without subtitles.")
        return None

    needed_languages = [track["language"] for track in subtitle_tracks]
    if needed_languages:
        get_missing_tessdata_files(needed_languages, tessdata_path)

    sub_options = Options(overwrite=True, one_per_lang=False, keep_temp_files=config.keep_files)

    spinner = Spinner("Sup subtitles extraction and SRT conversion")
    spinner_thread = threading.Thread(target=spinner.start)
    spinner_thread.start()

    try:
        for subtitle_path in output_path.glob("*.srt"):
            subtitle_path.unlink()

        mkv_file = Mkv(mkv_path.as_posix())
        os.environ["TESSDATA_PREFIX"] = tessdata_path.as_posix()

        pgsrip.rip(mkv_file, sub_options)

        for srt_file in output_path.glob("*.srt"):
            if srt_file.stat().st_size == 0:
                srt_file.unlink()

        if not any(output_path.glob("*.srt")) and not config.continue_on_error:
            raise SRTCreationError("No SRT subtitle files with data created.")

        mark_forced_srt_files(output_path, subtitle_tracks)
    finally:
        spinner.stop()
        spinner_thread.join()


def mark_forced_srt_files(output_path: Path, subtitle_tracks: list[dict[str, Any]]) -> None:
    language_counts: dict[str, int] = {}
    for track in sorted(subtitle_tracks, key=lambda track: (track["forced"] == 1, int(track["index"]))):
        language_code = subtitle_language_alpha2(track["language"])
        if not language_code:
            continue

        track_number_for_language = language_counts.get(language_code, 0)
        language_counts[language_code] = track_number_for_language + 1

        if track["forced"] != 1:
            continue

        forced_srt_file = find_srt_for_subtitle_track(output_path, language_code, track_number_for_language)
        if not forced_srt_file:
            print(f"Forced subtitle track {track['index']} did not create an SRT file.")
            continue

        if ".forced." in forced_srt_file.stem:
            continue

        forced_stem = forced_srt_file.stem.replace(f".{language_code}", f".forced.{language_code}")
        forced_srt_file.rename(forced_srt_file.with_stem(forced_stem))


def find_srt_for_subtitle_track(output_path: Path, language_code: str, track_number_for_language: int) -> Path | None:
    suffix = f"-{track_number_for_language}.{language_code}" if track_number_for_language else f".{language_code}"
    candidates = [
        candidate for candidate in sorted(output_path.glob(f"*{suffix}.srt")) if candidate.stem.endswith(suffix)
    ]
    return candidates[0] if candidates else None


def subtitle_language_alpha2(language_code: str) -> str | None:
    if not language_code or language_code == "und":
        return None
    try:
        return Language.fromietf(language_code).alpha2
    except (BabelfishError, ValueError):
        return None


def get_missing_tessdata_files(languages: list[str], tessdata_path: Path) -> None:
    tessdata_path.mkdir(exist_ok=True)
    if "zho" in languages:
        languages.remove("zho")
        languages += ["chi_sim", "chi_tra", "chi_sim_vert", "chi_tra_vert"]

    for language in languages:
        if not (tessdata_path / f"{language}.traineddata").exists():
            print(f"Downloading {language}.traineddata")
            response = requests.get(f"https://github.com/tesseract-ocr/tessdata_best/raw/main/{language}.traineddata")
            with open(tessdata_path / f"{language}.traineddata", "wb") as f:
                f.write(response.content)


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
