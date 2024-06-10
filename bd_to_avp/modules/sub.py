import os
from pathlib import Path

import ffmpeg
from babelfish import Language
from pgsrip import pgsrip, Options, Sup

from bd_to_avp.modules.config import config
from bd_to_avp.modules.util import Spinner


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

            codec_name = stream.get("codec_name", "")
            extension = subtitle_format_extensions.get(codec_name, "")
            if extension:
                subtitle_tracks.append({"index": index, "extension": extension, "codec_name": codec_name})
    except ffmpeg.Error as e:
        print(f"Error getting subtitle tracks: {e}")
    return subtitle_tracks


def convert_sup_to_srt(sup_subtitle_path: Path) -> Path:
    sub_file = Sup(str(sup_subtitle_path))
    sub_options = Options(languages={Language("eng")}, overwrite=True, one_per_lang=False)
    spinner = Spinner(f"Converting {sup_subtitle_path} to SRT")
    spinner.start()
    os.environ["TESSDATA_PREFIX"] = str(config.SCRIPT_PATH / "bin")
    pgsrip.rip(sub_file, sub_options)
    spinner.stop()
    return sup_subtitle_path.with_suffix(".srt")
