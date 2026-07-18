import logging
import typing
import subprocess

import ffmpeg
from babelfish import Language

from trakit.api import trakit

from bd_to_avp.vendor.pgsrip.media import Media, Pgs
from bd_to_avp.vendor.pgsrip.media_path import MediaPath
from bd_to_avp.vendor.pgsrip.options import Options
from bd_to_avp.modules.command import run_ffprobe
from bd_to_avp.modules.config import config

logger = logging.getLogger(__name__)


class MkvPgs(Pgs):
    @staticmethod
    def expected_srt_path(media_path: MediaPath, language: Language, number: int):
        return media_path.translate(language=language, number=number, extension="srt")

    @classmethod
    def read_data(cls, media_path: MediaPath, track_id: int, temp_folder: str):
        command = [
            config.FFMPEG_PATH,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-i",
            str(media_path),
            "-map",
            f"0:{track_id}",
            "-c:s",
            "copy",
            "-f",
            "sup",
            "pipe:1",
        ]
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        return result.stdout

    def __init__(self, media_path: MediaPath, track_id: int, language: Language, number: int, options: Options):
        temp_folder = media_path.create_temp_folder()
        super().__init__(
            media_path=media_path.translate(language=language, number=number),
            options=options,
            data_reader=lambda: self.read_data(media_path=media_path, track_id=track_id, temp_folder=temp_folder),
            temp_folder=temp_folder,
        )
        self.track_id = track_id

    def __str__(self):
        return (
            f"{self.media_path.translate(language=Language('und'), number=0)} "
            f"[{self.track_id}:{self.media_path.language}]"
        )


class MkvTrack:
    def __init__(self, track: dict):
        self.id = track["id"]
        self.type = track["type"]
        self.codec = track["codec"]
        self.properties = track.get("properties", {})

    @classmethod
    def from_ffprobe_stream(cls, stream: dict):
        disposition = stream.get("disposition", {})
        tags = stream.get("tags", {})
        language = tags.get("language") or tags.get("LANGUAGE") or "und"
        language_ietf = tags.get("language_ietf") or tags.get("language-ietf") or language
        stream_type = "subtitles" if stream.get("codec_type") == "subtitle" else stream.get("codec_type", "")
        return cls(
            {
                "id": stream["index"],
                "type": stream_type,
                "codec": "HDMV PGS"
                if stream.get("codec_name") == "hdmv_pgs_subtitle"
                else stream.get("codec_name", ""),
                "properties": {
                    "enabled_track": not bool(disposition.get("disabled", 0)),
                    "forced_track": bool(disposition.get("forced", 0)),
                    "default_track": bool(disposition.get("default", 0)),
                    "language": language,
                    "language_ietf": language_ietf,
                    "track_name": tags.get("title") or tags.get("handler_name"),
                },
            }
        )

    @property
    def enabled(self):
        return self.properties.get("enabled_track")

    @property
    def language(self):
        lang_ietf = self.properties.get("language_ietf")
        lang_alpha = self.properties.get("language")
        track_name = self.properties.get("track_name")

        language = Language.fromcleanit(lang_ietf or lang_alpha or "und")
        options = {"expected_language": language} if language else {}
        guess = trakit(track_name, options) if track_name else {}

        return guess.get("language") or language

    @property
    def forced(self):
        return bool(self.properties.get("forced_track", False))

    def __repr__(self):
        return f"<{self.__class__.__name__} [{self!s}]>"

    def __str__(self):
        return f"{self.id}:{self.type}:{self.codec}:{self.language}:{self.enabled}:{self.forced}"


class Mkv(Media):
    def __init__(self, path: str):
        try:
            metadata = run_ffprobe(path)
        except ffmpeg.Error as error:
            raise subprocess.CalledProcessError(
                returncode=1,
                cmd=[config.FFPROBE_PATH, path],
                output=error.stdout,
                stderr=error.stderr,
            ) from error
        tracks = [MkvTrack.from_ffprobe_stream(t) for t in metadata.get("streams", [])]
        super().__init__(MediaPath(path), languages={t.language for t in tracks if t.type == "subtitles"})
        self.tracks = tracks

    def get_selected_pgs_tracks(self, options: Options):
        tracks = [t for t in self.tracks if t.type == "subtitles" and t.codec == "HDMV PGS" and t.enabled]
        tracks.sort(key=lambda x: (x.forced, x.id))
        selected_languages: typing.Dict[str, int] = {}
        for t in tracks:
            language = t.language
            if options.languages and language not in options.languages:
                logger.debug("Filtering out track %s:%s in %s", t.id, language, self)
                continue

            language_key = str(language)
            if options.one_per_lang and language_key in selected_languages:
                logger.debug("Skipping track %s:%s in %s", t.id, language, self)
                continue

            if not language:
                logger.debug("Skipping unknown language track %s in %s", t.id, self)
                continue

            number = selected_languages.get(language_key, 0)
            srt_path = MkvPgs.expected_srt_path(self.media_path, language, number)
            selected_languages[language_key] = number + 1
            if srt_path.exists():
                if not options.overwrite:
                    logger.debug("Skipping %s:%s in %s since SRT already exists", t.id, language, self)
                    continue

                if options.srt_age and srt_path.m_age < options.srt_age:
                    logger.debug("Skipping track %s:%s in %s since SRT is too new", t.id, language, self)
                    continue

            yield t, language, number

    def get_selected_pgs_medias(self, options: Options):
        for t, language, number in self.get_selected_pgs_tracks(options):
            pgs = MkvPgs(self.media_path, t.id, language, number, options=options)
            if pgs.matches(options):
                logger.debug("Selecting track %s:%s in %s", t.id, language, self)
                yield t, pgs

    def get_pgs_medias(self, options: Options):
        for _, pgs in self.get_selected_pgs_medias(options):
            yield pgs
