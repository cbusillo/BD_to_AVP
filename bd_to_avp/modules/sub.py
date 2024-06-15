import os
import threading
from pathlib import Path

import ffmpeg
from babelfish import Language
from pgsrip import pgsrip, Options, Sup

from bd_to_avp.modules.config import config
from bd_to_avp.modules.util import Spinner


class SRTCreationError(Exception):
    pass


def get_subtitle_tracks(input_path: Path) -> list[dict]:
    subtitle_format_extensions = {
        "hdmv_pgs_subtitle": ".sup",
        "dvd_subtitle": ".sub",
        "subrip": ".srt",
    }
    subtitle_tracks = []
    try:
        print(f"Getting subtitle tracks from {input_path}")
        probe = ffmpeg.probe(str(input_path), select_streams="s")
        subtitle_streams = probe.get("streams", [])
        for index, stream in enumerate(subtitle_streams):
            tags = stream.get("tags", {})
            language = tags.get("language", "")
            if language != "eng":
                continue
    tessdata_path = config.app.config_path / "tessdata"
    subtitle_tracks = get_languages_in_mkv(mkv_path)

            codec_name = stream.get("codec_name", "")
            extension = subtitle_format_extensions.get(codec_name, "")
            if extension:
                subtitle_tracks.append({"index": index, "extension": extension, "codec_name": codec_name})
    except ffmpeg.Error as e:
        print(f"Error getting subtitle tracks: {e}")
    return subtitle_tracks


    needed_languages = [track["language"] for track in subtitle_tracks]
    if needed_languages:
        get_missing_tessdata_files(needed_languages, tessdata_path)

    spinner = Spinner(f"{sup_subtitle_path} to SRT Conversion")
    spinner_thread = threading.Thread(target=spinner.start)
    spinner_thread.start()

    os.environ["TESSDATA_PREFIX"] = str(config.SCRIPT_PATH / "bin")
    pgsrip.rip(sub_file, sub_options)
    os.environ["TESSDATA_PREFIX"] = tessdata_path.as_posix()

    spinner.stop()
    spinner_thread.join()
    srt_subtitle_path = sup_subtitle_path.with_suffix(".srt")
    if not srt_subtitle_path.exists() or srt_subtitle_path.stat().st_size == 0:
        if config.continue_on_error:
            return None
        raise SRTCreationError(f"Failed to create SRT file from {sup_subtitle_path}")
    return srt_subtitle_path

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
            "language": stream["tags"].get("language", "und"),  # 'und' stands for undefined
            "default": stream["disposition"].get("default", 0),
            "forced": stream["disposition"].get("forced", 0),
        }
        subtitle_info.append(info)
    return subtitle_info
