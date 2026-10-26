import json
import logging
import shutil
import subprocess
import typing
from pathlib import Path
from threading import Event

import ffmpeg
from babelfish import Language

from trakit.api import trakit

from bd_to_avp.vendor.pgsrip.media import Media, Pgs
from bd_to_avp.vendor.pgsrip.media_path import MediaPath
from bd_to_avp.vendor.pgsrip.options import Options
from bd_to_avp.vendor.pgsrip.pgs import contains_forced_objects, forced_only_pgs
from bd_to_avp.modules.command import run_ffprobe, run_process_capture
from bd_to_avp.modules.config import config
from bd_to_avp.observability import ObservabilityContext
from bd_to_avp.process_runner import ProcessArtifactProbe, ProcessCancelled
from bd_to_avp.runtime import RunContext

logger = logging.getLogger(__name__)


class MkvPgs(Pgs):
    @staticmethod
    def expected_srt_path(media_path: MediaPath, language: Language, number: int):
        return media_path.translate(language=language, number=number, extension="srt")

    @classmethod
    def read_data(
        cls,
        media_path: MediaPath,
        track_id: int,
        temp_folder: str,
        *,
        run_context: RunContext | None = None,
        cancellation_event: Event | None = None,
        observability_context: ObservabilityContext | None = None,
    ):
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
        output_path = Path(temp_folder) / f"track-{track_id}.sup"
        with output_path.open("wb") as output_file:
            run_process_capture(
                command,
                "Extract PGS subtitle stream",
                tool_id="ffmpeg",
                stdout=output_file,
                artifacts=(ProcessArtifactProbe("subtitle_payload", path=output_path),),
                run_context=run_context,
                cancellation_event=cancellation_event,
                observability_context=observability_context,
            )
        return output_path.read_bytes()

    def __init__(
        self,
        media_path: MediaPath,
        track_id: int,
        language: Language,
        number: int,
        options: Options,
        *,
        run_context: RunContext | None = None,
        cancellation_event: Event | None = None,
        observability_context: ObservabilityContext | None = None,
        data_reader: typing.Callable[[], bytes] | None = None,
    ):
        temp_folder = media_path.create_temp_folder()
        super().__init__(
            media_path=media_path.translate(language=language, number=number),
            options=options,
            data_reader=data_reader
            or (
                lambda: self.read_data(
                    media_path=media_path,
                    track_id=track_id,
                    temp_folder=temp_folder,
                    run_context=run_context,
                    cancellation_event=cancellation_event,
                    observability_context=observability_context,
                )
            ),
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
        properties = {
            "enabled_track": not bool(disposition.get("disabled", 0)),
            "forced_track": bool(disposition.get("forced", 0)),
            "default_track": bool(disposition.get("default", 0)),
            "language": language,
            "language_ietf": language_ietf,
            "track_name": tags.get("title") or tags.get("handler_name"),
        }
        if "width" in stream or "height" in stream:
            properties["width"] = stream.get("width")
            properties["height"] = stream.get("height")
        return cls(
            {
                "id": stream["index"],
                "type": stream_type,
                "codec": "HDMV PGS"
                if stream.get("codec_name") == "hdmv_pgs_subtitle"
                else stream.get("codec_name", ""),
                "properties": properties,
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

    @property
    def forced_only(self) -> bool:
        """A track derived from this stream's forced captions, as MakeMKV adds after each PG stream."""
        return bool(self.properties.get("forced_only", False))

    def forced_only_track(self) -> "MkvTrack":
        return MkvTrack(
            {
                "id": self.id,
                "type": self.type,
                "codec": self.codec,
                "properties": {**self.properties, "forced_track": True, "forced_only": True},
            }
        )

    @property
    def has_decodable_pgs_dimensions(self) -> bool:
        if self.type != "subtitles" or self.codec != "HDMV PGS":
            return True
        width = self.properties.get("width")
        height = self.properties.get("height")
        if width is None or height is None:
            return True
        return isinstance(width, int) and width > 0 and isinstance(height, int) and height > 0

    def __repr__(self):
        return f"<{self.__class__.__name__} [{self!s}]>"

    def __str__(self):
        return f"{self.id}:{self.type}:{self.codec}:{self.language}:{self.enabled}:{self.forced}"


class Mkv(Media):
    # A Blu-ray read without MakeMKV has no forced-only tracks; with this set, each selected PG
    # stream that flags captions forced also yields a forced-only track, as MakeMKV makes.
    forced_only_tracks = False
    _stream_folder: str | None = None

    def __init__(
        self,
        path: str,
        *,
        run_context: RunContext | None = None,
        cancellation_event: Event | None = None,
        observability_context: ObservabilityContext | None = None,
        forced_only_tracks: bool = False,
    ):
        try:
            metadata = run_ffprobe(
                path,
                run_context=run_context,
                cancellation_event=cancellation_event,
                observability_context=observability_context,
            )
        except ffmpeg.Error as error:
            raise subprocess.CalledProcessError(
                returncode=1,
                cmd=[config.FFPROBE_PATH, path],
                output=error.stdout,
                stderr=error.stderr,
            ) from error
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise subprocess.CalledProcessError(
                returncode=1,
                cmd=[config.FFPROBE_PATH, path],
                stderr=str(error).encode("utf-8", errors="replace"),
            ) from error
        tracks = [MkvTrack.from_ffprobe_stream(t) for t in metadata.get("streams", [])]
        super().__init__(MediaPath(path), languages={t.language for t in tracks if t.type == "subtitles"})
        self.tracks = tracks
        self.run_context = run_context
        self.cancellation_event = cancellation_event
        self.observability_context = observability_context
        self.forced_only_tracks = forced_only_tracks
        self._stream_files: dict[int, Path] = {}
        self._forced_streams: dict[int, bool] = {}

    def stream_data(self, track_id: int) -> bytes:
        """The PG stream's segments, extracted once and shared by its full and forced-only tracks."""
        stream_file = self._stream_files.get(track_id)
        if stream_file is not None:
            return stream_file.read_bytes()
        if self._stream_folder is None:
            self._stream_folder = self.media_path.create_temp_folder()
        data = MkvPgs.read_data(
            self.media_path,
            track_id,
            self._stream_folder,
            run_context=self.run_context,
            cancellation_event=self.cancellation_event,
            observability_context=self.observability_context,
        )
        self._stream_files[track_id] = Path(self._stream_folder) / f"track-{track_id}.sup"
        return data

    def has_forced_captions(self, track_id: int) -> bool:
        if track_id not in self._forced_streams:
            try:
                self._forced_streams[track_id] = contains_forced_objects(self.stream_data(track_id), self.media_path)
            except ProcessCancelled:
                raise
            except Exception as error:
                # The full track's rip reports the unreadable stream.
                logger.warning("Could not read PGS track %s for forced captions: %s", track_id, error)
                self._forced_streams[track_id] = False
        return self._forced_streams[track_id]

    def close(self) -> None:
        if self._stream_folder is not None:
            shutil.rmtree(self._stream_folder, ignore_errors=True)
            self._stream_folder = None
            self._stream_files.clear()

    def get_selected_pgs_tracks(self, options: Options):
        tracks = [t for t in self.tracks if t.type == "subtitles" and t.codec == "HDMV PGS" and t.enabled]
        if self.forced_only_tracks:
            # MakeMKV drops a forced-only track that turns out empty, so only streams with forced
            # captions get one.
            tracks += [
                t.forced_only_track()
                for t in list(tracks)
                if not t.forced
                and t.has_decodable_pgs_dimensions
                and (not options.languages or t.language in options.languages)
                and self.has_forced_captions(t.id)
            ]
        tracks.sort(key=lambda x: (x.forced, x.id))
        selected_languages: typing.Dict[str, int] = {}
        for t in tracks:
            if not t.has_decodable_pgs_dimensions:
                logger.warning("Skipping malformed PGS track without dimensions: %s", t)
                continue
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
            data_reader = None
            if self.forced_only_tracks:
                data_reader = (
                    (lambda track_id=t.id: forced_only_pgs(self.stream_data(track_id), self.media_path))
                    if t.forced_only
                    else (lambda track_id=t.id: self.stream_data(track_id))
                )
            pgs = MkvPgs(
                self.media_path,
                t.id,
                language,
                number,
                options=options,
                run_context=getattr(self, "run_context", None),
                cancellation_event=getattr(self, "cancellation_event", None),
                observability_context=getattr(self, "observability_context", None),
                data_reader=data_reader,
            )
            if pgs.matches(options):
                logger.debug("Selecting track %s:%s in %s", t.id, language, self)
                yield t, pgs

    def get_pgs_medias(self, options: Options):
        for _, pgs in self.get_selected_pgs_medias(options):
            yield pgs
