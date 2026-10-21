"""Cheap live-playback eligibility for a Blu-ray 3D source, without MakeMKV or a stream.

``ssif_probe titles`` lists the source's playlists of at least 60 seconds. The worker
applies the same admission rules ``start_live_hls`` applies to one playlist and reports
the longest playlist that passes them, with the audio streams live HLS can play.
"""

from __future__ import annotations

import json

from dataclasses import dataclass
from typing import Mapping, cast

from bd_to_avp.worker.live_hls_service import live_audio_input_format, live_video_shape
from bd_to_avp.worker.source_service import SourceServiceError, SSIFProbeRunner


TITLES_SCHEMA_VERSION = 1
HELPER_USAGE_EXIT_CODE = 2
NO_TITLES_REASON = "no_live_titles"
SEARCH_INCOMPLETE_REASON = "live_title_search_incomplete"


@dataclass(frozen=True)
class TitleListing:
    titles: list[Mapping[str, object]]
    eligible_count: int


@dataclass(frozen=True)
class TitleAssessment:
    title: Mapping[str, object]
    reason: str | None
    audio_streams: tuple[Mapping[str, object], ...]

    @property
    def playlist(self) -> int:
        return cast(int, self.title["playlist"])

    @property
    def rank(self) -> tuple[int, bool, int]:
        # Longest first; the libbluray main feature breaks a tie between a 2D and a 3D
        # playlist of the same clip, then the lower playlist number.
        return cast(int, self.title["duration_ticks"]), self.title["main_feature"] is True, -self.playlist


class LiveTitleInspector(SSIFProbeRunner):
    def run(self) -> dict[str, object]:
        self._validate_tool()
        self.activity.stage_started("inspect_live_title", "Checking live playback eligibility")
        listing = self._titles()
        assessments = [self._assess(title) for title in listing.titles]
        admitted = [assessment for assessment in assessments if assessment.reason is None]
        if admitted:
            return self._result(max(admitted, key=lambda assessment: assessment.rank))
        # The helper lists eligible titles first and longest first, so an omitted eligible
        # title is shorter than every listed one. If one was omitted, it could still pass
        # the live rules: the search is incomplete, not a rejection.
        listed_eligible = sum(1 for title in listing.titles if title["eligible"] is True)
        if listing.eligible_count > listed_eligible:
            return self._incomplete_result()
        # Report why the most promising title failed: one the helper accepted (so the
        # reason is a live HLS rule) before one it rejected, longest first.
        reported = max(
            assessments,
            key=lambda assessment: (assessment.title["eligible"] is True, assessment.rank),
            default=None,
        )
        return self._result(reported)

    def _titles(self) -> TitleListing:
        result = self._communicate([str(self.probe_path), "titles", str(self.job.source.path)])
        if result.returncode == HELPER_USAGE_EXIT_CODE:
            raise SourceServiceError(
                "ssif_probe_incompatible",
                "The direct SSIF source helper cannot list titles.",
                result.stderr.decode(errors="replace") or None,
            )
        if result.returncode != 0:
            self._raise_helper_error(result.stderr, "source_inspection_failed")
        try:
            document = json.loads(result.stdout)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise _invalid_titles("The source helper returned an unreadable title listing.") from error
        if (
            not isinstance(document, Mapping)
            or document.get("schema_version") != TITLES_SCHEMA_VERSION
            or document.get("type") != "source.titles"
        ):
            raise _invalid_titles("The source helper returned an unexpected title listing.")
        titles = document.get("titles")
        if not isinstance(titles, list):
            raise _invalid_titles("The title listing has no titles.")
        eligible_count = document.get("eligible_count")
        if not _is_integer(eligible_count) or not isinstance(document.get("truncated"), bool):
            raise _invalid_titles("The title listing has no completeness record.")
        for title in titles:
            _validate_title(title)
        return TitleListing(titles, cast(int, eligible_count))

    @staticmethod
    def _assess(title: Mapping[str, object]) -> TitleAssessment:
        if not title["eligible"]:
            return TitleAssessment(title, cast(str, title["unsupported_reason"]), ())
        clips = cast(list[Mapping[str, object]], title["clips"])
        try:
            live_video_shape(title)
        except SourceServiceError as error:
            return TitleAssessment(title, error.code, ())
        audio_streams: list[Mapping[str, object]] = []
        for stream in cast(list[Mapping[str, object]], clips[0]["audio_streams"]):
            try:
                live_audio_input_format(stream)
            except SourceServiceError:
                continue
            audio_streams.append(stream)
        if not audio_streams:
            return TitleAssessment(title, "unsupported_live_audio", ())
        return TitleAssessment(title, None, tuple(audio_streams))

    @staticmethod
    def _incomplete_result() -> dict[str, object]:
        return {
            "eligible": False,
            "unsupported_reason": SEARCH_INCOMPLETE_REASON,
            "playlist": None,
            "duration_seconds": None,
            "audio_streams": [],
        }

    @staticmethod
    def _result(assessment: TitleAssessment | None) -> dict[str, object]:
        if assessment is None:
            return {
                "eligible": False,
                "unsupported_reason": NO_TITLES_REASON,
                "playlist": None,
                "duration_seconds": None,
                "audio_streams": [],
            }
        return {
            "eligible": assessment.reason is None,
            "unsupported_reason": assessment.reason,
            "playlist": assessment.playlist,
            "duration_seconds": assessment.title["duration_seconds"],
            "audio_streams": [
                {
                    "pid": stream["pid"],
                    "coding_type": stream["coding_type"],
                    "format": stream.get("format"),
                    "rate": stream.get("rate"),
                    "language": stream.get("language"),
                }
                for stream in assessment.audio_streams
            ],
        }


def _invalid_titles(message: str) -> SourceServiceError:
    return SourceServiceError("invalid_source_inspection", message)


def _is_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_streams(value: object) -> None:
    if not isinstance(value, list):
        raise _invalid_titles("A title clip has no stream list.")
    for stream in value:
        if not isinstance(stream, Mapping) or not all(
            _is_integer(stream.get(key)) for key in ("pid", "coding_type", "format", "rate")
        ):
            raise _invalid_titles("A title clip has a malformed stream.")
        language = stream.get("language")
        if language is not None and not isinstance(language, str):
            raise _invalid_titles("A title clip has a malformed stream language.")


def _validate_title(title: object) -> None:
    if not isinstance(title, Mapping):
        raise _invalid_titles("The title listing contains a malformed title.")
    if not _is_integer(title.get("playlist")) or not _is_integer(title.get("duration_ticks")):
        raise _invalid_titles("A listed title has no playlist or duration.")
    duration_seconds = title.get("duration_seconds")
    if not isinstance(duration_seconds, (int, float)) or isinstance(duration_seconds, bool):
        raise _invalid_titles("A listed title has no duration in seconds.")
    if not isinstance(title.get("eligible"), bool) or not isinstance(title.get("main_feature"), bool):
        raise _invalid_titles("A listed title has no eligibility.")
    reason = title.get("unsupported_reason")
    if reason is not None and not isinstance(reason, str):
        raise _invalid_titles("A listed title has a malformed unsupported reason.")
    if title["eligible"] is not True:
        # Rejected titles are compact: their reason, without clip or stream detail.
        if not isinstance(reason, str):
            raise _invalid_titles("A rejected title has no unsupported reason.")
        return
    clips = title.get("clips")
    if not isinstance(clips, list) or len(clips) != 1:
        raise _invalid_titles("An eligible title must have exactly one clip.")
    for clip in clips:
        if not isinstance(clip, Mapping):
            raise _invalid_titles("A listed title has a malformed clip.")
        _validate_streams(clip.get("video_streams"))
        _validate_streams(clip.get("audio_streams"))
