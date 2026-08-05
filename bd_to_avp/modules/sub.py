import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from babelfish import Language
from bd_to_avp.vendor.pgsrip import Mkv, Options, pgsrip
from bd_to_avp.vendor.pgsrip.mkv import MkvPgs

from bd_to_avp.modules.config import config, Stage
from bd_to_avp.modules.command import get_spinner_update_func, run_ffprobe, Spinner
from bd_to_avp.modules.languages import (
    language_alpha2,
    language_name,
    normalize_language_code,
    normalize_source_language,
)
from bd_to_avp.observability import (
    ObservabilityCancellation,
    ObservabilityContext,
    ObservabilityData,
    ObservabilityFailure,
    ObservabilityPrivacy,
    ObservabilityProgress,
    ObservabilitySeverity,
    ObservabilityText,
)
from bd_to_avp.presentation import cli_message
from bd_to_avp.process_runner import ProcessCancelled
from bd_to_avp.runtime import RunContext


class SRTCreationError(Exception):
    pass


SubtitleWarningHandler = Callable[[str], None]


def report_subtitle_warning(message: str, warning_handler: SubtitleWarningHandler | None) -> None:
    if warning_handler is not None:
        warning_handler(message)
    else:
        cli_message(message)


def create_srt_from_mkv(
    mkv_path: Path,
    output_path: Path | None = None,
    warning_handler: SubtitleWarningHandler | None = None,
    *,
    run_context: RunContext | None = None,
    cancellation_event: threading.Event | None = None,
    observability_context: ObservabilityContext | None = None,
) -> None:
    output_path = output_path or mkv_path.parent
    if config.start_stage.value <= Stage.EXTRACT_SUBTITLES.value:
        if config.skip_subtitles:
            cleanup_existing_subtitle_files(output_path)
            return None
        extract_subtitle_to_srt(
            mkv_path,
            output_path,
            warning_handler,
            run_context=run_context,
            cancellation_event=cancellation_event,
            observability_context=observability_context,
        )


def extract_subtitle_to_srt(
    mkv_path: Path,
    output_path: Path | None = None,
    warning_handler: SubtitleWarningHandler | None = None,
    *,
    run_context: RunContext | None = None,
    cancellation_event: threading.Event | None = None,
    observability_context: ObservabilityContext | None = None,
) -> None:
    output_path = output_path or mkv_path.parent
    cleanup_existing_subtitle_files(output_path)

    if config.skip_subtitles:
        return None
    subtitle_tracks = get_languages_in_mkv(
        mkv_path,
        warning_handler=warning_handler,
        run_context=run_context,
        cancellation_event=cancellation_event,
        observability_context=observability_context,
    )

    if not subtitle_tracks:
        message = "No PGS subtitle tracks found in source; continuing without subtitles."
        report_subtitle_warning(message, warning_handler)
        return None

    sub_options = subtitle_rip_options()

    spinner = Spinner("Sup subtitles extraction and SRT conversion") if run_context is None else None
    spinner_update_func = get_spinner_update_func() if spinner is not None else None
    spinner_thread: threading.Thread | None = None
    if spinner is not None:
        spinner_thread = threading.Thread(target=spinner.start, args=(spinner_update_func,))
        spinner_thread.start()

    try:
        with subtitle_source_alias(mkv_path, output_path) as subtitle_mkv_path:
            mkv_file = Mkv(
                subtitle_mkv_path.as_posix(),
                run_context=run_context,
                cancellation_event=cancellation_event,
                observability_context=observability_context,
            )
            selected_subtitle_tracks = get_selected_subtitle_tracks(mkv_file, sub_options)

            if config.remove_extra_languages and not selected_subtitle_tracks:
                preferred_language = normalize_language_code(config.language_code)
                message = (
                    "No PGS subtitle tracks matched the preferred language "
                    f"{language_name(preferred_language)} ({preferred_language}); continuing without subtitles."
                )
                report_subtitle_warning(message, warning_handler)
                return None

            selected_count = len(selected_subtitle_tracks)
            successful_track_count = 0
            terminal_event_emitted = False
            emit_subtitle_progress(
                run_context,
                "subtitle.extract.started",
                completed=0,
                total=selected_count,
                context=observability_context,
            )

            try:
                try:
                    ripped_track_count = pgsrip.rip(mkv_file, sub_options)
                except ProcessCancelled:
                    raise
                except Exception as error:
                    cleanup_existing_subtitle_files(output_path)
                    emit_subtitle_progress(
                        run_context,
                        "subtitle.extract.failed",
                        completed=0,
                        total=selected_count,
                        severity=ObservabilitySeverity.WARNING,
                        message="Subtitle extraction failed before producing usable output.",
                        failure_code="subtitle_rip_failed",
                        context=observability_context,
                    )
                    terminal_event_emitted = True
                    report_subtitle_warning(
                        f"PGS subtitle extraction failed; continuing without subtitles. ({error})",
                        warning_handler,
                    )
                    return None

                if ripped_track_count == 0:
                    cleanup_existing_subtitle_files(output_path)
                    emit_subtitle_progress(
                        run_context,
                        "subtitle.extract.failed",
                        completed=0,
                        total=selected_count,
                        severity=ObservabilitySeverity.WARNING,
                        message="Subtitle extraction produced no usable output.",
                        failure_code="subtitle_rip_no_output",
                        context=observability_context,
                    )
                    terminal_event_emitted = True
                    report_subtitle_warning(
                        "PGS subtitle extraction did not produce usable subtitle files; continuing without subtitles.",
                        warning_handler,
                    )
                    return None

                empty_count = 0
                for srt_file in output_path.glob("*.srt"):
                    if srt_file.stat().st_size == 0:
                        srt_file.unlink()
                        empty_count += 1

                successful_track_count = max(0, min(selected_count, ripped_track_count - empty_count))
                failed_track_count = selected_count - successful_track_count
                if successful_track_count == 0:
                    cleanup_existing_subtitle_files(output_path)
                    emit_subtitle_progress(
                        run_context,
                        "subtitle.extract.failed",
                        completed=0,
                        total=selected_count,
                        severity=ObservabilitySeverity.WARNING,
                        message="Subtitle extraction produced no usable output.",
                        failure_code="subtitle_empty_output",
                        context=observability_context,
                    )
                    terminal_event_emitted = True
                    if not config.continue_on_error:
                        raise SRTCreationError("No SRT subtitle files with data created.")
                    report_subtitle_warning(
                        "PGS subtitle extraction did not produce usable subtitle files; continuing without subtitles.",
                        warning_handler,
                    )
                    return None

                if failed_track_count:
                    message = (
                        "Subtitles extracted with partial loss: "
                        f"{failed_track_count} of {selected_count} subtitle tracks produced no usable output."
                    )
                    report_subtitle_warning(message, warning_handler)
                    emit_subtitle_progress(
                        run_context,
                        "subtitle.extract.partial_output",
                        completed=successful_track_count,
                        total=selected_count,
                        severity=ObservabilitySeverity.WARNING,
                        message=message,
                        context=observability_context,
                    )

                mark_forced_srt_files(selected_subtitle_tracks, warning_handler)
                emit_subtitle_progress(
                    run_context,
                    "subtitle.extract.completed",
                    completed=successful_track_count,
                    total=selected_count,
                    severity=ObservabilitySeverity.WARNING if failed_track_count else ObservabilitySeverity.INFO,
                    context=observability_context,
                )
                terminal_event_emitted = True
            except ProcessCancelled:
                if not terminal_event_emitted:
                    emit_subtitle_progress(
                        run_context,
                        "subtitle.extract.cancelled",
                        completed=successful_track_count,
                        total=selected_count,
                        cancelled=True,
                        context=observability_context,
                    )
                raise
            except KeyboardInterrupt:
                if not terminal_event_emitted:
                    emit_subtitle_progress(
                        run_context,
                        "subtitle.extract.cancelled",
                        completed=successful_track_count,
                        total=selected_count,
                        cancelled=True,
                        context=observability_context,
                    )
                raise
            except Exception:
                if not terminal_event_emitted:
                    emit_subtitle_progress(
                        run_context,
                        "subtitle.extract.failed",
                        completed=successful_track_count,
                        total=selected_count,
                        severity=ObservabilitySeverity.WARNING,
                        message="Subtitle extraction failed while finalizing output.",
                        failure_code="subtitle_postprocess_failed",
                        context=observability_context,
                    )
                raise
    finally:
        if spinner is not None and spinner_thread is not None:
            spinner.stop(spinner_update_func)
            spinner_thread.join()


def emit_subtitle_progress(
    run_context: RunContext | None,
    kind: str,
    *,
    completed: int,
    total: int,
    severity: ObservabilitySeverity = ObservabilitySeverity.INFO,
    message: str | None = None,
    failure_code: str | None = None,
    cancelled: bool = False,
    context: ObservabilityContext | None = None,
) -> None:
    if run_context is None:
        return
    run_context.emit(
        kind,
        severity=severity,
        privacy=ObservabilityPrivacy.PUBLIC,
        context=context,
        data=ObservabilityData(
            message=(
                ObservabilityText.bounded(message, privacy=ObservabilityPrivacy.PUBLIC) if message is not None else None
            ),
            failure=ObservabilityFailure(code=failure_code) if failure_code is not None else None,
            cancellation=ObservabilityCancellation(requested=True) if cancelled else None,
            progress=ObservabilityProgress(
                completed_units=float(completed),
                total_units=float(total) if total > 0 else None,
                unit="tracks",
            ),
        ),
    )


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


def mark_forced_srt_files(
    subtitle_tracks: list[dict[str, Any]],
    warning_handler: SubtitleWarningHandler | None = None,
) -> None:
    for track in subtitle_tracks:
        if track["forced"] != 1:
            continue

        forced_srt_file = track["srt_path"]
        if not forced_srt_file.exists():
            report_subtitle_warning(
                f"Forced subtitle track {track['index']} did not create an SRT file.",
                warning_handler,
            )
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


def get_languages_in_mkv(
    mkv_path: Path,
    *,
    warning_handler: SubtitleWarningHandler | None = None,
    run_context: RunContext | None = None,
    cancellation_event: threading.Event | None = None,
    observability_context: ObservabilityContext | None = None,
) -> None | list[dict[str, Any]]:
    mkv_info = run_ffprobe(
        mkv_path,
        run_context=run_context,
        cancellation_event=cancellation_event,
        observability_context=observability_context,
    )
    streams = mkv_info.get("streams") or []
    subtitle_streams = [
        stream
        for stream in streams
        if stream["codec_type"] == "subtitle" and stream.get("codec_name") == "hdmv_pgs_subtitle"
    ]
    if not subtitle_streams:
        return None
    subtitle_info = []
    for stream in subtitle_streams:
        source_language = stream.get("tags", {}).get("language", "und") or "und"
        canonical_language = normalize_source_language(source_language)
        if canonical_language == "und" and (
            not isinstance(source_language, str) or source_language.casefold() != "und"
        ):
            report_subtitle_warning(
                f"Unrecognized subtitle language metadata {source_language!r}; treating it as undetermined.",
                warning_handler,
            )
        info = {
            "index": stream["index"],
            "language": canonical_language,
            "default": stream["disposition"].get("default", 0),
            "forced": stream["disposition"].get("forced", 0),
        }
        subtitle_info.append(info)
    return subtitle_info
