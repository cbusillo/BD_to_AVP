import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import ffmpeg
from babelfish import Language
from bd_to_avp.vendor.pgsrip import Mkv, Options, pgsrip
from bd_to_avp.vendor.pgsrip.mkv import MkvPgs

from bd_to_avp.modules.config import config, Stage
from bd_to_avp.modules.command import get_spinner_update_func, Spinner
from bd_to_avp.modules.languages import (
    language_alpha2,
    language_name,
    normalize_language_code,
    normalize_source_language,
)


class SRTCreationError(Exception):
    pass


SubtitleWarningHandler = Callable[[str], None]


def create_srt_from_mkv(
    mkv_path: Path,
    output_path: Path | None = None,
    warning_handler: SubtitleWarningHandler | None = None,
) -> None:
    output_path = output_path or mkv_path.parent
    if config.start_stage.value <= Stage.EXTRACT_SUBTITLES.value:
        if config.skip_subtitles:
            cleanup_existing_subtitle_files(output_path)
            return None
        extract_subtitle_to_srt(mkv_path, output_path, warning_handler)


def extract_subtitle_to_srt(
    mkv_path: Path,
    output_path: Path | None = None,
    warning_handler: SubtitleWarningHandler | None = None,
) -> None:
    output_path = output_path or mkv_path.parent
    cleanup_existing_subtitle_files(output_path)

    if config.skip_subtitles:
        return None
    subtitle_tracks = get_languages_in_mkv(mkv_path)

    if not subtitle_tracks:
        message = "No PGS subtitle tracks found in source; continuing without subtitles."
        print(message)
        if warning_handler:
            warning_handler(message)
        return None

    sub_options = subtitle_rip_options()

    spinner = Spinner("Sup subtitles extraction and SRT conversion")
    spinner_update_func = get_spinner_update_func()
    spinner_thread = threading.Thread(target=spinner.start, args=(spinner_update_func,))
    spinner_thread.start()

    try:
        with subtitle_source_alias(mkv_path, output_path) as subtitle_mkv_path:
            mkv_file = Mkv(subtitle_mkv_path.as_posix())
            selected_subtitle_tracks = get_selected_subtitle_tracks(mkv_file, sub_options)

            if config.remove_extra_languages and not selected_subtitle_tracks:
                preferred_language = normalize_language_code(config.language_code)
                message = (
                    "No PGS subtitle tracks matched the preferred language "
                    f"{language_name(preferred_language)} ({preferred_language}); continuing without subtitles."
                )
                print(message)
                if warning_handler:
                    warning_handler(message)
                return None

            pgsrip.rip(mkv_file, sub_options)

            for srt_file in output_path.glob("*.srt"):
                if srt_file.stat().st_size == 0:
                    srt_file.unlink()

            if not any(output_path.glob("*.srt")) and not config.continue_on_error:
                raise SRTCreationError("No SRT subtitle files with data created.")

            mark_forced_srt_files(selected_subtitle_tracks)
    finally:
        spinner.stop(spinner_update_func)
        spinner_thread.join()


@contextmanager
def subtitle_source_alias(mkv_path: Path, output_path: Path) -> Iterator[Path]:
    if mkv_path.parent.resolve() == output_path.resolve():
        yield mkv_path
        return

    source_path = mkv_path.resolve(strict=True)
    alias_path = output_path / mkv_path.name
    if alias_path.is_symlink() and not alias_path.exists():
        alias_path.unlink()

    if alias_path.exists() or alias_path.is_symlink():
        try:
            alias_matches_source = alias_path.samefile(source_path)
        except OSError:
            alias_matches_source = False

        if alias_matches_source:
            if alias_path.is_symlink():
                try:
                    yield alias_path
                finally:
                    alias_path.unlink(missing_ok=True)
            else:
                yield alias_path
            return

        alias_path = unique_subtitle_source_alias_path(mkv_path, output_path)

    alias_path.symlink_to(source_path)
    try:
        yield alias_path
    finally:
        alias_path.unlink(missing_ok=True)


def unique_subtitle_source_alias_path(mkv_path: Path, output_path: Path) -> Path:
    for index in range(1, 1000):
        candidate = output_path / f"{mkv_path.stem}.subtitle-source-{index}{mkv_path.suffix}"
        if not candidate.exists() and not candidate.is_symlink():
            return candidate

    raise FileExistsError(f"Unable to create a subtitle source alias in {output_path}")


def cleanup_existing_subtitle_files(output_path: Path) -> None:
    for subtitle_path in output_path.glob("*.srt"):
        subtitle_path.unlink()


def subtitle_rip_options() -> Options:
    languages = set()
    if config.remove_extra_languages:
        preferred_language = normalize_language_code(config.language_code)
        languages.add(Language.fromalpha3t(preferred_language))

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
    return language_alpha2(language_code)


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
        source_language = stream.get("tags", {}).get("language", "und") or "und"
        canonical_language = normalize_source_language(source_language)
        if canonical_language == "und" and (
            not isinstance(source_language, str) or source_language.casefold() != "und"
        ):
            print(f"Unrecognized subtitle language metadata {source_language!r}; treating it as undetermined.")
        info = {
            "index": stream["index"],
            "language": canonical_language,
            "default": stream["disposition"].get("default", 0),
            "forced": stream["disposition"].get("forced", 0),
        }
        subtitle_info.append(info)
    return subtitle_info
