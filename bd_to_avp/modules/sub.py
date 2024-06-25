import os
import threading
from pathlib import Path

import ffmpeg
import requests
from babelfish import Language
from pgsrip import Mkv, Options, pgsrip

from bd_to_avp.modules.config import config, Stage
from bd_to_avp.modules.command import Spinner


class SRTCreationError(Exception):
    pass


def create_srt_from_mkv(mkv_path: Path, output_path: Path) -> None:
    if config.start_stage.value <= Stage.EXTRACT_SUBTITLES.value:
        if config.skip_subtitles:
            return None
        extract_subtitle_to_srt(mkv_path, output_path)


def extract_subtitle_to_srt(mkv_path: Path, output_path: Path) -> None:
    if config.skip_subtitles:
        return None
    tessdata_path = config.app.config_path / "tessdata"
    subtitle_tracks = get_languages_in_mkv(mkv_path)

    if not subtitle_tracks and not config.continue_on_error:
        raise SRTCreationError("No subtitle tracks found in source.")

    if not subtitle_tracks:
        return None

    forced_subtitle_tracks = [track for track in subtitle_tracks if track["forced"] == 1]
    forced_track_language = forced_subtitle_tracks[0]["language"] if forced_subtitle_tracks else None

    needed_languages = [track["language"] for track in subtitle_tracks]
    if needed_languages:
        get_missing_tessdata_files(needed_languages, tessdata_path)

    sub_options = Options(overwrite=True, one_per_lang=False, keep_temp_files=config.keep_files)

    spinner = Spinner(f"Sup subtitles extraction and SRT conversion")
    spinner_thread = threading.Thread(target=spinner.start)
    spinner_thread.start()

    for subtitle_path in output_path.glob("*.srt"):
        subtitle_path.unlink()

    mkv_file = Mkv(mkv_path.as_posix())
    os.environ["TESSDATA_PREFIX"] = tessdata_path.as_posix()

    pgsrip.rip(mkv_file, sub_options)

    if mkv_path.parent != output_path:
        glob_pattern = f"{mkv_path.stem}*.srt"
        for srt_file in mkv_path.parent.glob(glob_pattern):
            srt_file.rename(output_path / srt_file.name)

    for srt_file in output_path.glob("*.srt"):
        if srt_file.stat().st_size == 0:
            srt_file.unlink()

    if not any(output_path.glob("*.srt")) and not config.continue_on_error:
        raise SRTCreationError("No SRT subtitle files created.")

    if forced_track_language:
        two_alpha_language_code = Language.fromietf(forced_track_language).alpha2

        forced_srt_file = next(output_path.glob(f"*{two_alpha_language_code}.srt"))
        if forced_srt_file and forced_srt_file.exists():
            new_stem = forced_srt_file.stem.replace(f".{two_alpha_language_code}", f".forced.{two_alpha_language_code}")
            forced_srt_file.rename(forced_srt_file.with_stem(new_stem))

    spinner.stop()
    spinner_thread.join()


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


def get_languages_in_mkv(mkv_path: Path) -> None | list[dict[str, str]]:
    mkv_info = ffmpeg.probe(str(mkv_path))
    streams = mkv_info["streams"]
    subtitle_streams = [stream for stream in streams if stream["codec_type"] == "subtitle"]
    if not subtitle_streams:
        print("No subtitle streams found in MKV.")
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
