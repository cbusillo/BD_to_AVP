from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
import json
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from bd_to_avp.modules.languages import language_name, normalize_language_code, normalize_source_language
from bd_to_avp.presentation import cli_message


class AudioSelectionError(ValueError):
    pass


class AudioFallbackReason(StrEnum):
    SOURCE_DEFAULT = "source_default"
    FIRST_STREAM = "first_stream"


class AudioSelectionActivityReporter(Protocol):
    def warning(self, message: str, *, stage: str | None = None, **fields: object) -> None:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class SelectedAudioStream:
    audio_position: int
    stream_index: int
    language: str
    is_default: bool
    stream: dict[str, Any]

    @property
    def selector(self) -> str:
        return f"a:{self.audio_position}"


@dataclass(frozen=True, slots=True)
class AudioSelection:
    streams: tuple[SelectedAudioStream, ...]
    preferred_language: str | None
    source_stream_count: int
    fallback_reason: AudioFallbackReason | None = None

    @property
    def used_fallback(self) -> bool:
        return self.fallback_reason is not None


@dataclass(frozen=True, slots=True)
class AudioSelectionManifest:
    preferred_language: str
    source_stream_count: int
    selected_stream_count: int

    @property
    def omitted_streams(self) -> bool:
        return self.selected_stream_count < self.source_stream_count


def select_audio_streams(
    streams: Sequence[dict[str, Any]],
    preferred_language: str | None,
) -> AudioSelection:
    available_streams = tuple(_selected_audio_stream(position, stream) for position, stream in enumerate(streams))
    if not available_streams:
        raise AudioSelectionError("The source does not contain an audio stream.")

    if preferred_language is None:
        return AudioSelection(
            streams=available_streams,
            preferred_language=None,
            source_stream_count=len(available_streams),
        )

    canonical_language = normalize_language_code(preferred_language)
    matching_streams = tuple(stream for stream in available_streams if stream.language == canonical_language)
    if matching_streams:
        return AudioSelection(
            streams=matching_streams,
            preferred_language=canonical_language,
            source_stream_count=len(available_streams),
        )

    source_default = next((stream for stream in available_streams if stream.is_default), None)
    if source_default is not None:
        return AudioSelection(
            streams=(source_default,),
            preferred_language=canonical_language,
            source_stream_count=len(available_streams),
            fallback_reason=AudioFallbackReason.SOURCE_DEFAULT,
        )
    return AudioSelection(
        streams=(available_streams[0],),
        preferred_language=canonical_language,
        source_stream_count=len(available_streams),
        fallback_reason=AudioFallbackReason.FIRST_STREAM,
    )


def emit_audio_selection_warning(
    selection: AudioSelection,
    activity: AudioSelectionActivityReporter | None,
    *,
    stage: str,
) -> None:
    if not selection.used_fallback:
        return

    preferred_language = selection.preferred_language
    selected_stream = selection.streams[0]
    assert preferred_language is not None
    assert selection.fallback_reason is not None
    fallback_description = (
        "source-default audio track"
        if selection.fallback_reason is AudioFallbackReason.SOURCE_DEFAULT
        else "first source audio track"
    )
    message = (
        f"No audio tracks matched the preferred language {language_name(preferred_language)} "
        f"({preferred_language}). Keeping the {fallback_description} "
        f"({language_name(selected_stream.language)} ({selected_stream.language})) instead."
    )
    if activity is None:
        cli_message(message)
        return

    activity.warning(
        message,
        stage=stage,
        code="audio_language_fallback",
        preferred_language=preferred_language,
        selected_language=selected_stream.language,
        selected_stream_index=selected_stream.stream_index,
        selected_audio_position=selected_stream.audio_position,
        fallback_reason=selection.fallback_reason.value,
        action=(
            "keep_source_default_audio"
            if selection.fallback_reason is AudioFallbackReason.SOURCE_DEFAULT
            else "keep_first_audio"
        ),
    )


def persist_audio_selection(audio_path: Path, selection: AudioSelection | None) -> None:
    manifest_path = audio_selection_manifest_path(audio_path)
    if selection is None or selection.preferred_language is None:
        manifest_path.unlink(missing_ok=True)
        return

    manifest = {
        "version": 1,
        "preferred_language": selection.preferred_language,
        "source_stream_count": selection.source_stream_count,
        "selected_stream_count": len(selection.streams),
    }
    temporary_path = manifest_path.with_suffix(f"{manifest_path.suffix}.part")
    try:
        temporary_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary_path.replace(manifest_path)
    except OSError:
        with suppress(OSError):
            manifest_path.unlink(missing_ok=True)
        raise
    finally:
        with suppress(OSError):
            temporary_path.unlink(missing_ok=True)


def load_audio_selection_manifest(audio_path: Path) -> AudioSelectionManifest | None:
    manifest_path = audio_selection_manifest_path(audio_path)
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    required_keys = {
        "version",
        "preferred_language",
        "source_stream_count",
        "selected_stream_count",
    }
    if not isinstance(raw, Mapping) or not required_keys.issubset(raw):
        return None
    if raw.get("version") != 1:
        return None
    preferred_language = raw.get("preferred_language")
    source_stream_count = raw.get("source_stream_count")
    selected_stream_count = raw.get("selected_stream_count")
    if (
        not isinstance(preferred_language, str)
        or not isinstance(source_stream_count, int)
        or isinstance(source_stream_count, bool)
        or not isinstance(selected_stream_count, int)
        or isinstance(selected_stream_count, bool)
        or source_stream_count < 1
        or selected_stream_count < 1
        or selected_stream_count > source_stream_count
    ):
        return None
    try:
        canonical_language = normalize_language_code(preferred_language)
    except ValueError:
        return None
    return AudioSelectionManifest(
        preferred_language=canonical_language,
        source_stream_count=source_stream_count,
        selected_stream_count=selected_stream_count,
    )


def emit_unrestorable_all_languages_warning(
    manifest: AudioSelectionManifest,
    activity: AudioSelectionActivityReporter | None,
    *,
    stage: str,
    restart_stage: str,
) -> None:
    message = (
        "All audio languages were requested, but this prepared audio artifact contains only a prior "
        f"{language_name(manifest.preferred_language)} ({manifest.preferred_language}) selection. Keeping every "
        f"audio track available in the artifact; restart from {restart_stage} to restore omitted source languages."
    )
    if activity is None:
        cli_message(message)
        return
    activity.warning(
        message,
        stage=stage,
        code="audio_languages_unrestorable_at_mux",
        previous_preferred_language=manifest.preferred_language,
        source_stream_count=manifest.source_stream_count,
        available_stream_count=manifest.selected_stream_count,
        action="keep_prepared_audio_tracks",
        restart_stage=restart_stage,
    )


def audio_selection_manifest_path(audio_path: Path) -> Path:
    return audio_path.with_name(f".{audio_path.name}.audio-selection.json")


def _selected_audio_stream(position: int, stream: dict[str, Any]) -> SelectedAudioStream:
    tags = stream.get("tags")
    language = normalize_source_language(tags.get("language") if isinstance(tags, Mapping) else None)
    disposition = stream.get("disposition")
    is_default = _disposition_enabled(disposition.get("default")) if isinstance(disposition, Mapping) else False
    return SelectedAudioStream(
        audio_position=position,
        stream_index=_stream_index(stream.get("index"), position),
        language=language,
        is_default=is_default,
        stream=stream,
    )


def _stream_index(value: object, fallback: int) -> int:
    if isinstance(value, bool):
        return fallback
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return fallback
    return fallback


def _disposition_enabled(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        try:
            return int(value) != 0
        except ValueError:
            return False
    return False
