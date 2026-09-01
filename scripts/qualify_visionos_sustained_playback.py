#!/usr/bin/env python3
"""Validate and assemble deterministic visionOS sustained-playback evidence."""

from __future__ import annotations

import argparse
import datetime as datetime_module
import hashlib
import itertools
import json
import math
import re
import statistics
import sys
from pathlib import Path
from typing import Any

CONTRACT_VERSION = "visionos-sustained-playback-v1"
LOG_SCHEMA_VERSION = 1
RECORD_SCHEMA_VERSION = 1
SUSTAINED_SECONDS = 1800.0
MIB = 1024 * 1024
SAMPLE_INTERVAL_SECONDS = 15
CLOCK_SPAN_TOLERANCE_SECONDS = 5.0
CLOCK_SPAN_TOLERANCE_FRACTION = 0.02
FULL_LENGTH_COMPLETION_FRACTION = 0.995
FULL_LENGTH_ACTIVE_PLAYBACK_FRACTION = 0.98
FULL_LENGTH_MEDIA_PROGRESS_FRACTION = 0.95
SUSTAINED_ACTIVE_PLAYBACK_FRACTION = 0.995
SUSTAINED_MEDIA_PROGRESS_FRACTION = 0.98
MEMORY_MINIMUM_SAMPLES = 8
MEMORY_MINIMUM_SPAN_SECONDS = 300.0
MEMORY_MINIMUM_POST_WARMUP_SAMPLES = 4
MEMORY_MINIMUM_FINAL_THIRD_SAMPLES = 2
MEMORY_GROWTH_BYTES = 256 * MIB
MEMORY_SLOPE_MIB_PER_MINUTE = 2.0
MEMORY_FINAL_THIRD_RANGE_BYTES = 128 * MIB
LOCAL_MAX_UNPLANNED_WAITING_SECONDS = 5.0
PROVIDER_MAX_UNPLANNED_WAITING_SECONDS = 15.0
PROVIDER_MAX_UNRECOVERED_WAITING_SECONDS = 15.0
PROVIDER_TOTAL_UNPLANNED_WAITING_SECONDS = 60.0
FULL_LENGTH_MINIMUM_BATTERY_PERCENT = 90.0
CASE_ORDER = (
    "mvhevc-local-full",
    "packed-sbs-local-full",
    "mvhevc-files-provider-sustained",
    "packed-ou-local-sustained",
)
OPTIONAL_UNAVAILABLE_REASONS = frozenset({"provider_unavailable", "asset_unavailable", "redistribution_constraint"})
SOURCE_CLASSES = frozenset({"local_documents", "files_provider"})
COVERAGES = frozenset({"full_length", "sustained"})
THERMAL_STATES = frozenset({"nominal", "fair", "serious", "critical", "unknown"})
TIME_CONTROL_STATES = frozenset({"paused", "waiting", "playing", "unknown"})
WAITING_REASONS = frozenset({"none", "evaluating_buffering_rate", "to_minimize_stalls", "no_item", "other"})
ITEM_STATUSES = frozenset({"unknown", "ready", "failed"})
ITEM_ERROR_CATEGORIES = frozenset({"none", "decoder", "network", "media_services", "unknown"})
FOOTER_REASONS = frozenset({"playback_finished", "session_finished", "failure"})
EVENTS = frozenset(
    {
        "prepare",
        "ready",
        "play_requested",
        "pause_requested",
        "seek_started",
        "seek_completed",
        "eye_order_change_started",
        "eye_order_change_completed",
        "eye_order_change_failed",
        "scene_inactive",
        "scene_active",
        "time_control_changed",
        "playback_finished",
        "failure",
        "session_finished",
    }
)
DETAILS = frozenset(
    {
        "playing->waiting",
        "paused->waiting",
        "waiting->playing",
        "waiting->paused",
        "playing",
        "paused",
        "waiting",
        "none",
        "user_pause",
        "user_resume",
        "seek",
        "resume_restore",
        "eye_order_restore",
        "eye_order_change",
        "scene_inactive",
        "scene_active",
        "finished",
        "failed",
    }
)
OBSERVATIONS = frozenset({"yes", "no", "not_sure"})
OBSERVATION_FIELDS = ("picture", "depth", "eye_order", "audio_sync")
FORBIDDEN_KEY_PARTS = (
    "path",
    "url",
    "bookmark",
    "filename",
    "file_name",
    "title",
    "error_message",
    "localized",
)
HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
HEX_SHA = re.compile(r"^[0-9a-fA-F]{7,64}$")
RFC3339_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")
OS_VERSION_BUILD = re.compile(r"^Version (?P<version>\d+(?:\.\d+){1,2}) \(Build (?P<build>[A-Za-z0-9]+)\)$")
FILE_URL = re.compile(r"^file://", re.IGNORECASE)
WINDOWS_PATH = re.compile(r"^[A-Za-z]:[\\/]|^\\\\|^//")
SECURITY_BOOKMARK = re.compile(
    r"security[-_ ]scoped|bookmarkdata|startaccessingsecurityscopedresource|com\.apple\.security",
    re.IGNORECASE,
)


class EvidenceError(ValueError):
    pass


def _json_loads_strict(text: str, context: str) -> Any:
    def reject_constant(value: str) -> Any:
        raise EvidenceError(f"{context} contains non-finite number {value}.")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise EvidenceError(f"{context} contains duplicate key {key!r}.")
            result[key] = value
        return result

    try:
        return json.loads(text, object_pairs_hook=reject_duplicates, parse_constant=reject_constant)
    except EvidenceError:
        raise
    except (TypeError, json.JSONDecodeError) as error:
        raise EvidenceError(f"{context} is not valid JSON.") from error


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _is_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _mapping(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EvidenceError(f"{context} must be an object.")
    return value


def _list(value: Any, context: str) -> list[Any]:
    if not isinstance(value, list):
        raise EvidenceError(f"{context} must be an array.")
    return value


def _string(value: Any, context: str, *, empty_ok: bool = False) -> str:
    if not isinstance(value, str) or (not empty_ok and not value):
        raise EvidenceError(f"{context} must be a string.")
    if len(value) > 256:
        raise EvidenceError(f"{context} is too long.")
    return value


def _keys(value: dict[str, Any], required: set[str], allowed: set[str], context: str) -> None:
    missing = sorted(required - value.keys())
    extra = sorted(value.keys() - allowed)
    if missing:
        raise EvidenceError(f"{context} is missing {', '.join(missing)}.")
    if extra:
        raise EvidenceError(f"{context} contains unexpected key {extra[0]!r}.")


def _enum(value: Any, allowed: set[str] | frozenset[str], context: str) -> str:
    value = _string(value, context)
    if value not in allowed:
        raise EvidenceError(f"{context} has invalid enum value {value!r}.")
    return value


def _nonnegative(value: Any, context: str) -> float | int:
    if not _is_number(value) or value < 0:
        raise EvidenceError(f"{context} must be a nonnegative number.")
    return value


def _nonnegative_int(value: Any, context: str) -> int:
    if not _is_integer(value) or value < 0:
        raise EvidenceError(f"{context} must be a nonnegative integer.")
    return value


def _hash(value: Any, context: str) -> str:
    value = _string(value, context)
    if not HEX64.fullmatch(value):
        raise EvidenceError(f"{context} must be a 64-character hexadecimal SHA-256.")
    return value.lower()


def _timestamp(value: Any, context: str) -> datetime_module.datetime:
    value = _string(value, context)
    if not RFC3339_UTC.fullmatch(value):
        raise EvidenceError(f"{context} must be RFC3339 UTC ending in Z.")
    try:
        return datetime_module.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise EvidenceError(f"{context} is not a valid timestamp.") from error


def _privacy(value: Any, context: str = "value") -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if not isinstance(key, str):
                raise EvidenceError(f"{context} contains a non-string key.")
            if any(part in key.lower() for part in FORBIDDEN_KEY_PARTS):
                raise EvidenceError(f"{context} contains forbidden key {key!r}.")
            _privacy(nested, f"{context}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _privacy(nested, f"{context}[{index}]")
    elif isinstance(value, str):
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise EvidenceError(f"{context} contains a control character.")
        if len(value) > 256 and not HEX64.fullmatch(value):
            raise EvidenceError(f"{context} is too long.")
        if FILE_URL.search(value) or WINDOWS_PATH.search(value) or value.startswith("/"):
            raise EvidenceError(f"{context} contains a path or file URL.")
        if SECURITY_BOOKMARK.search(value):
            raise EvidenceError(f"{context} contains security-scoped bookmark data.")


def _common(record: dict[str, Any], index: int) -> tuple[str, float | int, datetime_module.datetime]:
    _keys(
        record,
        {"schema_version", "kind", "captured_at", "elapsed_seconds"},
        {
            "schema_version",
            "kind",
            "captured_at",
            "elapsed_seconds",
            "run_id",
            "media_id",
            "sample_interval_seconds",
            "app",
            "device",
            "thermal_state",
            "physical_footprint_bytes",
            "player_time_seconds",
            "duration_seconds",
            "rate",
            "time_control_status",
            "waiting_reason",
            "item_status",
            "likely_to_keep_up",
            "item_error_category",
            "event",
            "detail",
            "reason",
            "final_player_time_seconds",
        },
        f"record {index}",
    )
    if not _is_integer(record["schema_version"]) or record["schema_version"] != LOG_SCHEMA_VERSION:
        raise EvidenceError(f"record {index} has unsupported schema_version.")
    kind = _enum(record["kind"], {"header", "sample", "event", "footer"}, f"record {index}.kind")
    elapsed = _nonnegative(record["elapsed_seconds"], f"record {index}.elapsed_seconds")
    captured = _timestamp(record["captured_at"], f"record {index}.captured_at")
    for field in ("run_id", "media_id"):
        if field in record:
            _string(record[field], f"record {index}.{field}")
    return kind, elapsed, captured


def _header(record: dict[str, Any], index: int) -> dict[str, Any]:
    fields = {
        "schema_version",
        "kind",
        "captured_at",
        "elapsed_seconds",
        "run_id",
        "media_id",
        "sample_interval_seconds",
        "app",
        "device",
    }
    _keys(record, fields, fields, f"record {index}")
    if (
        not _is_integer(record["sample_interval_seconds"])
        or record["sample_interval_seconds"] != SAMPLE_INTERVAL_SECONDS
    ):
        raise EvidenceError(f"header.sample_interval_seconds must be {SAMPLE_INTERVAL_SECONDS}.")
    app = _mapping(record["app"], f"record {index}.app")
    _keys(
        app,
        {"bundle_id", "version", "build"},
        {"bundle_id", "version", "build"},
        f"record {index}.app",
    )
    for field in app:
        _string(app[field], f"record {index}.app.{field}")
    device = _mapping(record["device"], f"record {index}.device")
    _keys(
        device,
        {"hardware_model", "operating_system"},
        {"hardware_model", "operating_system"},
        f"record {index}.device",
    )
    for field in device:
        _string(device[field], f"record {index}.device.{field}")
    return dict(record)


def _sample(record: dict[str, Any], index: int) -> dict[str, Any]:
    fields = {
        "schema_version",
        "kind",
        "captured_at",
        "elapsed_seconds",
        "run_id",
        "media_id",
        "thermal_state",
        "physical_footprint_bytes",
        "player_time_seconds",
        "duration_seconds",
        "rate",
        "time_control_status",
        "waiting_reason",
        "item_status",
        "likely_to_keep_up",
        "item_error_category",
    }
    _keys(record, fields - {"run_id", "media_id"}, fields, f"record {index}")
    if record["physical_footprint_bytes"] is not None:
        _nonnegative_int(
            record["physical_footprint_bytes"],
            f"record {index}.physical_footprint_bytes",
        )
    for field in ("player_time_seconds", "duration_seconds", "rate"):
        _nonnegative(record[field], f"record {index}.{field}")
    _enum(record["thermal_state"], THERMAL_STATES, f"record {index}.thermal_state")
    _enum(
        record["time_control_status"],
        TIME_CONTROL_STATES,
        f"record {index}.time_control_status",
    )
    _enum(record["waiting_reason"], WAITING_REASONS, f"record {index}.waiting_reason")
    _enum(record["item_status"], ITEM_STATUSES, f"record {index}.item_status")
    if record["likely_to_keep_up"] is not None and not isinstance(record["likely_to_keep_up"], bool):
        raise EvidenceError(f"record {index}.likely_to_keep_up must be boolean or null.")
    _enum(
        record["item_error_category"],
        ITEM_ERROR_CATEGORIES,
        f"record {index}.item_error_category",
    )
    return dict(record)


def _event(record: dict[str, Any], index: int) -> dict[str, Any]:
    fields = {
        "schema_version",
        "kind",
        "captured_at",
        "elapsed_seconds",
        "run_id",
        "media_id",
        "event",
        "player_time_seconds",
        "detail",
    }
    _keys(record, fields - {"run_id", "media_id"}, fields, f"record {index}")
    _enum(record["event"], EVENTS, f"record {index}.event")
    _nonnegative(record["player_time_seconds"], f"record {index}.player_time_seconds")
    if record["detail"] is not None:
        _enum(record["detail"], DETAILS, f"record {index}.detail")
    return dict(record)


def _footer(record: dict[str, Any], index: int) -> dict[str, Any]:
    fields = {
        "schema_version",
        "kind",
        "captured_at",
        "elapsed_seconds",
        "run_id",
        "media_id",
        "reason",
        "final_player_time_seconds",
        "duration_seconds",
    }
    _keys(record, fields - {"run_id", "media_id"}, fields, f"record {index}")
    _enum(record["reason"], FOOTER_REASONS, f"record {index}.reason")
    _nonnegative(record["final_player_time_seconds"], f"record {index}.final_player_time_seconds")
    _nonnegative(record["duration_seconds"], f"record {index}.duration_seconds")
    return dict(record)


def _evidence_summary(
    header: dict[str, Any],
    samples: list[dict[str, Any]],
    events: list[dict[str, Any]],
    footer: dict[str, Any],
) -> dict[str, Any]:
    samples = sorted(samples, key=lambda value: (value["elapsed_seconds"], value["captured_at"]))
    events = sorted(events, key=lambda value: (value["elapsed_seconds"], value["captured_at"]))
    ordered_records = sorted(
        [*samples, *events],
        key=lambda value: (value["elapsed_seconds"], value["captured_at"]),
    )
    all_records = [header, *ordered_records, footer]
    return {
        "header": header,
        "samples": samples,
        "events": events,
        "footer": footer,
        "summary": {
            "sample_count": len(samples),
            "event_count": len(events),
            "elapsed_start_seconds": min(record["elapsed_seconds"] for record in all_records),
            "elapsed_end_seconds": max(record["elapsed_seconds"] for record in all_records),
            "captured_start": min(record["captured_at"] for record in all_records),
            "captured_end": max(record["captured_at"] for record in all_records),
        },
    }


def _validate_records(records: list[Any]) -> dict[str, Any]:
    if not records:
        raise EvidenceError("log is empty.")
    parsed: list[dict[str, Any]] = []
    header: dict[str, Any] | None = None
    footer: dict[str, Any] | None = None
    run_id: str | None = None
    media_id: str | None = None
    for index, raw in enumerate(records):
        record = _mapping(raw, f"record {index}")
        _privacy(record, f"record {index}")
        kind, _, _ = _common(record, index)
        if footer is not None:
            raise EvidenceError("log contains records after footer.")
        if kind == "header":
            if index != 0 or header is not None:
                raise EvidenceError("log must contain exactly one header first.")
            header = _header(record, index)
            run_id, media_id = header["run_id"], header["media_id"]
            normalized = header
        elif kind == "sample":
            normalized = _sample(record, index)
        elif kind == "event":
            normalized = _event(record, index)
        else:
            if index != len(records) - 1 or footer is not None:
                raise EvidenceError("log must contain exactly one footer last.")
            footer = _footer(record, index)
            normalized = footer
        for field, expected in (("run_id", run_id), ("media_id", media_id)):
            if field in record and expected is not None and record[field] != expected:
                raise EvidenceError(f"log {field} is inconsistent.")
        parsed.append(normalized)
    if header is None:
        raise EvidenceError("log is missing header.")
    if footer is None:
        raise EvidenceError("log is missing footer.")
    return _evidence_summary(
        header,
        [record for record in parsed if record["kind"] == "sample"],
        [record for record in parsed if record["kind"] == "event"],
        footer,
    )


def _load_jsonl(path: Path) -> tuple[bytes, list[dict[str, Any]]]:
    try:
        content = path.read_bytes()
    except OSError as error:
        raise EvidenceError("log_unreadable") from error
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise EvidenceError("malformed_log") from error
    lines = text.splitlines()
    if not lines or any(not line.strip() for line in lines):
        raise EvidenceError("malformed_log")
    records = [_json_loads_strict(line, "log record") for line in lines]
    if not all(isinstance(record, dict) for record in records):
        raise EvidenceError("malformed_log")
    return content, records


def validate_log(path: Path) -> dict[str, Any]:
    _, records = _load_jsonl(path)
    evidence = _validate_records(records)
    return {
        "schema_version": LOG_SCHEMA_VERSION,
        "run_id": evidence["header"]["run_id"],
        "media_id": evidence["header"]["media_id"],
        "sample_interval_seconds": SAMPLE_INTERVAL_SECONDS,
        "sample_count": len(evidence["samples"]),
        "event_count": len(evidence["events"]),
        "elapsed_start_seconds": evidence["summary"]["elapsed_start_seconds"],
        "elapsed_end_seconds": evidence["summary"]["elapsed_end_seconds"],
        "captured_start": evidence["summary"]["captured_start"],
        "captured_end": evidence["summary"]["captured_end"],
        "footer_reason": evidence["footer"]["reason"],
    }


def _time_control_status(detail: str | None) -> str | None:
    if detail is None:
        return None
    status = detail.rsplit("->", 1)[-1]
    return "unknown" if status == "none" else status


def _event_matches(event: dict[str, Any], name: str, detail: str | None = None) -> bool:
    return event["event"] == name and (detail is None or event.get("detail") == detail)


def _has_ordered_events(
    events: list[dict[str, Any]],
    first: tuple[str, str | None],
    second: tuple[str, str | None],
) -> bool:
    first_elapsed: float | None = None
    for event in events:
        if _event_matches(event, *first):
            first_elapsed = float(event["elapsed_seconds"])
        elif (
            first_elapsed is not None
            and _event_matches(event, *second)
            and float(event["elapsed_seconds"]) >= first_elapsed
        ):
            return True
    return False


def _playing_coverage_seconds(events: list[dict[str, Any]], footer_elapsed: float | int) -> float:
    playing_started: float | None = None
    total = 0.0
    for event in events:
        if event["event"] != "time_control_changed":
            continue
        current_status = _time_control_status(event.get("detail"))
        elapsed = float(event["elapsed_seconds"])
        if current_status == "playing":
            if playing_started is None:
                playing_started = elapsed
        elif current_status in {"paused", "waiting", "unknown"}:
            if playing_started is not None:
                total += max(0.0, elapsed - playing_started)
                playing_started = None
    if playing_started is not None:
        total += max(0.0, float(footer_elapsed) - playing_started)
    return total


def _sampled_player_progress_seconds(samples: list[dict[str, Any]]) -> float:
    total = 0.0
    for previous, current in itertools.pairwise(samples):
        elapsed_delta = max(
            0.0,
            float(current["elapsed_seconds"]) - float(previous["elapsed_seconds"]),
        )
        player_delta = max(
            0.0,
            float(current["player_time_seconds"]) - float(previous["player_time_seconds"]),
        )
        total += min(player_delta, elapsed_delta * 1.25)
    return total


def _device_identity_matches(header_device: dict[str, Any], device_identity: dict[str, Any]) -> bool:
    match = OS_VERSION_BUILD.fullmatch(header_device["operating_system"])
    return bool(
        header_device["hardware_model"] == device_identity["product_type"]
        and match is not None
        and match.group("version") == device_identity["os_version"]
        and match.group("build") == device_identity["os_build"]
    )


def _slope(points: list[tuple[float, float]]) -> float:
    if len(points) < 2:
        return 0.0
    x_values = [point[0] for point in points]
    y_values = [point[1] for point in points]
    x_mean = statistics.fmean(x_values)
    y_mean = statistics.fmean(y_values)
    denominator = sum((x - x_mean) ** 2 for x in x_values)
    return 0.0 if denominator == 0 else sum((x - x_mean) * (y - y_mean) for x, y in points) / denominator


def _waiting_intervals(events: list[dict[str, Any]], footer_elapsed: float | int) -> list[dict[str, Any]]:
    open_wait: dict[str, Any] | None = None
    intervals: list[dict[str, Any]] = []
    for event in events:
        detail = event.get("detail")
        current_status = _time_control_status(detail)
        elapsed = float(event["elapsed_seconds"])
        if event["event"] == "time_control_changed" and current_status == "waiting":
            if open_wait is None:
                open_wait = {"start": elapsed}
        elif event["event"] == "time_control_changed" and current_status in {"playing", "paused", "unknown"}:
            if open_wait is not None:
                end = max(elapsed, open_wait["start"])
                intervals.append(
                    {
                        "start_elapsed_seconds": open_wait["start"],
                        "end_elapsed_seconds": end,
                        "duration_seconds": end - open_wait["start"],
                        "recovered": current_status in {"playing", "paused"},
                        "end_transition": detail,
                    }
                )
                open_wait = None
    if open_wait is not None:
        end = max(float(footer_elapsed), open_wait["start"])
        intervals.append(
            {
                "start_elapsed_seconds": open_wait["start"],
                "end_elapsed_seconds": end,
                "duration_seconds": end - open_wait["start"],
                "recovered": False,
                "end_transition": None,
            }
        )
    control_spans: list[tuple[float, float]] = []
    open_pause: float | None = None
    for event in events:
        elapsed = float(event["elapsed_seconds"])
        if event["event"] == "pause_requested":
            open_pause = elapsed
            continue
        current_status = _time_control_status(event.get("detail"))
        if open_pause is not None and (
            event["event"] == "play_requested"
            or (event["event"] == "time_control_changed" and current_status in {"playing", "paused"})
        ):
            control_spans.append((open_pause, max(open_pause, elapsed)))
            open_pause = None
    for start_events, end_events in (
        ({"seek_started"}, {"seek_completed"}),
        (
            {"eye_order_change_started"},
            {"eye_order_change_completed", "eye_order_change_failed"},
        ),
        ({"scene_inactive"}, {"scene_active"}),
    ):
        open_control: float | None = None
        for event in events:
            elapsed = float(event["elapsed_seconds"])
            if event["event"] in start_events:
                open_control = elapsed
            elif event["event"] in end_events and open_control is not None:
                control_spans.append((open_control, max(open_control, elapsed)))
                open_control = None
    for interval in intervals:
        start = float(interval["start_elapsed_seconds"])
        end = float(interval["end_elapsed_seconds"])
        overlaps = sorted(
            (max(start, control_start), min(end, control_end))
            for control_start, control_end in control_spans
            if control_start < end and control_end > start
        )
        excluded_duration = 0.0
        merged_end: float | None = None
        for overlap_start, overlap_end in overlaps:
            if merged_end is None or overlap_start > merged_end:
                excluded_duration += max(0.0, overlap_end - overlap_start)
                merged_end = overlap_end
            elif overlap_end > merged_end:
                excluded_duration += overlap_end - merged_end
                merged_end = overlap_end
        unplanned_duration = max(0.0, float(interval["duration_seconds"]) - excluded_duration)
        interval["excluded_duration_seconds"] = excluded_duration
        interval["unplanned_duration_seconds"] = unplanned_duration
        interval["excluded"] = unplanned_duration == 0
        if excluded_duration == 0:
            interval["classification"] = "unplanned_waiting"
        elif unplanned_duration == 0:
            interval["classification"] = "excluded_control_interval"
        else:
            interval["classification"] = "partially_excluded_control_interval"
    return intervals


def _telemetry(evidence: dict[str, Any], coverage: str, media_duration: float) -> dict[str, Any]:
    header, footer = evidence["header"], evidence["footer"]
    samples, events = evidence["samples"], evidence["events"]
    elapsed = max(0.0, float(footer["elapsed_seconds"]) - float(header["elapsed_seconds"]))
    captured_elapsed = max(
        0.0,
        (
            _timestamp(footer["captured_at"], "footer.captured_at")
            - _timestamp(header["captured_at"], "header.captured_at")
        ).total_seconds(),
    )
    memory = [
        (float(sample["elapsed_seconds"]), float(sample["physical_footprint_bytes"]))
        for sample in samples
        if sample["physical_footprint_bytes"] is not None
    ]
    expected = SUSTAINED_SECONDS if coverage == "sustained" else media_duration
    warmup = min(1200.0, expected / 3.0)
    post_warmup = [point for point in memory if point[0] >= warmup]
    final_values: list[float] = []
    post_growth = slope_mib = final_range = 0.0
    if post_warmup:
        values = [point[1] for point in post_warmup]
        post_growth = max(values) - min(values)
        slope_mib = _slope(post_warmup) * 60.0 / MIB
        final_values = [value for elapsed_value, value in post_warmup if elapsed_value >= max(warmup, expected * 2 / 3)]
        if final_values:
            final_range = max(final_values) - min(final_values)
    intervals = _waiting_intervals(events, footer["elapsed_seconds"])
    unplanned = [interval for interval in intervals if interval["unplanned_duration_seconds"] > 0]
    unrecovered = [interval for interval in unplanned if not interval["recovered"]]
    active_playback = _playing_coverage_seconds(events, footer["elapsed_seconds"])
    unplanned_waiting_total = sum(item["unplanned_duration_seconds"] for item in unplanned)
    ranks = {"unknown": 0, "nominal": 1, "fair": 2, "serious": 3, "critical": 4}
    thermal = max(
        (sample["thermal_state"] for sample in samples),
        key=lambda value: ranks[value],
        default="unknown",
    )
    degraded = any(
        sample["item_status"] == "failed"
        or sample["item_error_category"] != "none"
        or (
            sample["time_control_status"] == "playing"
            and (sample["likely_to_keep_up"] is False or float(sample["rate"]) < 0.95)
        )
        for sample in samples
    )
    return {
        "elapsed_coverage_seconds": round(elapsed, 6),
        "captured_coverage_seconds": round(captured_elapsed, 6),
        "clock_span_delta_seconds": round(abs(captured_elapsed - elapsed), 6),
        "active_playback_seconds": round(active_playback, 6),
        "qualifying_playback_seconds": round(active_playback + unplanned_waiting_total, 6),
        "sampled_player_progress_seconds": round(_sampled_player_progress_seconds(samples), 6),
        "sample_count": len(samples),
        "event_count": len(events),
        "thermal_state_max": thermal,
        "unknown_thermal_sample_count": sum(sample["thermal_state"] == "unknown" for sample in samples),
        "playback_degradation": degraded,
        "item_error_categories": sorted({sample["item_error_category"] for sample in samples}),
        "memory_sample_count": len(memory),
        "post_warmup_memory_sample_count": len(post_warmup),
        "memory_span_seconds": (round(memory[-1][0] - memory[0][0], 6) if memory else 0.0),
        "memory_warmup_seconds": round(warmup, 6),
        "post_warmup_growth_bytes": round(post_growth, 6),
        "post_warmup_slope_mib_per_min": round(slope_mib, 6),
        "final_third_range_bytes": round(final_range, 6),
        "final_third_memory_sample_count": len(final_values) if post_warmup else 0,
        "memory_plateau": final_range <= MEMORY_FINAL_THIRD_RANGE_BYTES,
        "waiting_intervals": intervals,
        "unplanned_waiting_total_seconds": round(unplanned_waiting_total, 6),
        "unplanned_waiting_max_seconds": round(
            max(
                (item["unplanned_duration_seconds"] for item in unplanned),
                default=0.0,
            ),
            6,
        ),
        "unrecovered_waiting_max_seconds": round(
            max(
                (item["unplanned_duration_seconds"] for item in unrecovered),
                default=0.0,
            ),
            6,
        ),
    }


def _battery(value: Any, context: str) -> dict[str, Any]:
    result = _mapping(value, context)
    fields = {"start_percent", "end_percent", "charging", "low_power_interruption"}
    _keys(result, fields, fields, context)
    for field in ("start_percent", "end_percent"):
        if not _is_number(result[field]) or not 0 <= result[field] <= 100:
            raise EvidenceError(f"{context}.{field} must be between 0 and 100.")
    for field in ("charging", "low_power_interruption"):
        if not isinstance(result[field], bool):
            raise EvidenceError(f"{context}.{field} must be boolean.")
    return dict(result)


def _battery_result(value: dict[str, Any], elapsed: float) -> dict[str, Any]:
    drain = float(value["start_percent"]) - float(value["end_percent"])
    rate = drain / (elapsed / 3600.0) if elapsed > 0 else 0.0
    return {
        **value,
        "drain_percent": round(drain, 6),
        "drain_percent_per_hour": round(rate, 6),
    }


def _observations(value: Any, context: str) -> dict[str, str]:
    result = _mapping(value, context)
    fields: set[str] = set(OBSERVATION_FIELDS)
    _keys(result, fields, fields, context)
    return {field: _enum(result[field], OBSERVATIONS, f"{context}.{field}") for field in OBSERVATION_FIELDS}


def _case_contract(cell: dict[str, Any]) -> None:
    specs = {
        "mvhevc-local-full": (
            "local_documents",
            "full_length",
            "mv-hevc",
            {"mv-hevc", "mvhevc"},
        ),
        "packed-sbs-local-full": (
            "local_documents",
            "full_length",
            None,
            {"side-by-side", "sbs", "packed-sbs"},
        ),
        "mvhevc-files-provider-sustained": (
            "files_provider",
            "sustained",
            "mv-hevc",
            {"mv-hevc", "mvhevc"},
        ),
        "packed-ou-local-sustained": (
            "local_documents",
            "sustained",
            None,
            {"over-under", "ou", "packed-ou"},
        ),
    }
    expected_source, expected_coverage, expected_codec, expected_stereo = specs[cell["case_id"]]
    if cell["source_class"] != expected_source or cell["coverage"] != expected_coverage:
        raise EvidenceError(f"cell {cell['case_id']!r} does not match the frozen contract.")
    media = cell["media"]
    if expected_codec and media["codec_tag"].lower() != expected_codec:
        raise EvidenceError(f"cell {cell['case_id']!r} requires mv-hevc codec_tag.")
    if media["stereo_format"].lower() not in expected_stereo:
        raise EvidenceError(f"cell {cell['case_id']!r} has an incompatible stereo_format.")


def _evaluate(
    cell: dict[str, Any],
    evidence: dict[str, Any],
    telemetry: dict[str, Any],
    battery: dict[str, Any],
) -> tuple[str, list[str]]:
    samples, events, footer = (
        evidence["samples"],
        evidence["events"],
        evidence["footer"],
    )
    evidence_reasons: list[str] = []
    product_reasons: list[str] = []
    reasons: list[str] = []
    event_names = {event["event"] for event in events}
    if not samples:
        evidence_reasons.append("missing_samples")
    if not any(sample["physical_footprint_bytes"] is not None for sample in samples):
        evidence_reasons.append("null_physical_footprint")
    required = {"prepare", "ready"}
    if cell["coverage"] == "full_length":
        if footer["reason"] != "playback_finished":
            evidence_reasons.append("invalid_full_length_footer_reason")
        required.add("playback_finished")
    else:
        if footer["reason"] not in {"session_finished", "playback_finished"}:
            evidence_reasons.append("invalid_sustained_footer_reason")
        required.add("session_finished" if footer["reason"] == "session_finished" else "playback_finished")
    if cell["case_id"] in {"packed-sbs-local-full", "packed-ou-local-sustained"}:
        required.update({"eye_order_change_started", "eye_order_change_completed"})
    for name in sorted(required):
        if name not in event_names:
            evidence_reasons.append(f"missing_required_event:{name}")
    if cell["coverage"] == "full_length":
        if not _has_ordered_events(
            events,
            ("pause_requested", "user_pause"),
            ("play_requested", "user_resume"),
        ):
            evidence_reasons.append("missing_required_interaction:pause_resume")
        if not _has_ordered_events(events, ("seek_started", "seek"), ("seek_completed", "seek")):
            evidence_reasons.append("missing_required_interaction:user_seek")
    if cell["case_id"] in {"packed-sbs-local-full", "packed-ou-local-sustained"}:
        if not _has_ordered_events(
            events,
            ("eye_order_change_started", "eye_order_change"),
            ("eye_order_change_completed", "eye_order_change"),
        ):
            evidence_reasons.append("missing_required_interaction:eye_order_change")
    expected_duration = (
        SUSTAINED_SECONDS if cell["coverage"] == "sustained" else float(cell["media"]["duration_seconds"])
    )
    if telemetry["elapsed_coverage_seconds"] < expected_duration * (
        1.0 if cell["coverage"] == "sustained" else FULL_LENGTH_COMPLETION_FRACTION
    ):
        evidence_reasons.append("insufficient_duration")
    if (
        cell["coverage"] == "sustained"
        and telemetry["qualifying_playback_seconds"] < SUSTAINED_SECONDS * SUSTAINED_ACTIVE_PLAYBACK_FRACTION
    ):
        evidence_reasons.append("insufficient_active_playback")
    if (
        cell["coverage"] == "full_length"
        and telemetry["qualifying_playback_seconds"]
        < float(cell["media"]["duration_seconds"]) * FULL_LENGTH_ACTIVE_PLAYBACK_FRACTION
    ):
        evidence_reasons.append("insufficient_active_playback")
    if (
        cell["coverage"] == "sustained"
        and telemetry["sampled_player_progress_seconds"] < SUSTAINED_SECONDS * SUSTAINED_MEDIA_PROGRESS_FRACTION
    ):
        evidence_reasons.append("insufficient_media_progress")
    if (
        cell["coverage"] == "full_length"
        and telemetry["sampled_player_progress_seconds"]
        < float(cell["media"]["duration_seconds"]) * FULL_LENGTH_MEDIA_PROGRESS_FRACTION
    ):
        evidence_reasons.append("insufficient_media_progress")
    clock_tolerance = max(
        CLOCK_SPAN_TOLERANCE_SECONDS,
        telemetry["elapsed_coverage_seconds"] * CLOCK_SPAN_TOLERANCE_FRACTION,
    )
    if telemetry["clock_span_delta_seconds"] > clock_tolerance:
        evidence_reasons.append("clock_span_mismatch")
    if abs(float(footer["duration_seconds"]) - float(cell["media"]["duration_seconds"])) > max(
        1.0, float(cell["media"]["duration_seconds"]) * 0.005
    ):
        evidence_reasons.append("media_duration_mismatch")
    if (
        cell["coverage"] == "full_length"
        and float(footer["final_player_time_seconds"])
        < float(cell["media"]["duration_seconds"]) * FULL_LENGTH_COMPLETION_FRACTION
    ):
        product_reasons.append("full_length_completion_below_99_5_percent")
    if telemetry["memory_sample_count"] < MEMORY_MINIMUM_SAMPLES:
        evidence_reasons.append("insufficient_memory_samples")
    if telemetry["memory_span_seconds"] < MEMORY_MINIMUM_SPAN_SECONDS:
        evidence_reasons.append("insufficient_memory_span")
    if telemetry["post_warmup_memory_sample_count"] < MEMORY_MINIMUM_POST_WARMUP_SAMPLES:
        evidence_reasons.append("insufficient_post_warmup_memory_samples")
    if telemetry["final_third_memory_sample_count"] < MEMORY_MINIMUM_FINAL_THIRD_SAMPLES:
        evidence_reasons.append("insufficient_final_third_memory_samples")
    if telemetry["unknown_thermal_sample_count"] > 0:
        evidence_reasons.append("unknown_thermal_state")
    if telemetry["thermal_state_max"] == "critical":
        product_reasons.append("critical_thermal")
    elif telemetry["thermal_state_max"] == "serious":
        if telemetry["playback_degradation"]:
            product_reasons.append("serious_thermal_with_playback_degradation")
        else:
            reasons.append("serious_thermal_without_degradation")
    if any(category != "none" for category in telemetry["item_error_categories"]):
        product_reasons.append("item_error")
    if footer["reason"] == "failure" or "failure" in event_names:
        product_reasons.append("playback_failure")
    if "eye_order_change_failed" in event_names:
        product_reasons.append("eye_order_change_failed")
    if (
        cell["source_class"] == "local_documents"
        and telemetry["unplanned_waiting_max_seconds"] > LOCAL_MAX_UNPLANNED_WAITING_SECONDS
    ):
        product_reasons.append("local_unplanned_waiting_over_5_seconds")
    if cell["source_class"] == "files_provider" and (
        telemetry["unplanned_waiting_max_seconds"] > PROVIDER_MAX_UNPLANNED_WAITING_SECONDS
        or telemetry["unrecovered_waiting_max_seconds"] > PROVIDER_MAX_UNRECOVERED_WAITING_SECONDS
        or telemetry["unplanned_waiting_total_seconds"] > PROVIDER_TOTAL_UNPLANNED_WAITING_SECONDS
    ):
        product_reasons.append("provider_unplanned_waiting_limit_exceeded")
    if (
        telemetry["post_warmup_growth_bytes"] > MEMORY_GROWTH_BYTES
        and telemetry["post_warmup_slope_mib_per_min"] > MEMORY_SLOPE_MIB_PER_MINUTE
        and not telemetry["memory_plateau"]
    ):
        product_reasons.append("memory_growth_slope_no_plateau")
    if cell["coverage"] == "full_length":
        if battery["start_percent"] < FULL_LENGTH_MINIMUM_BATTERY_PERCENT:
            product_reasons.append("battery_start_below_90_percent")
        if battery["charging"]:
            product_reasons.append("battery_was_charging")
        if battery["low_power_interruption"]:
            product_reasons.append("battery_low_power_interruption")
    for field, observation in cell["observations"].items():
        if observation == "no":
            product_reasons.append(f"observation_{field}_no")
        elif observation == "not_sure":
            reasons.append(f"observation_{field}_not_sure")
    reasons.extend(evidence_reasons)
    reasons.extend(product_reasons)
    reasons = sorted(set(reasons))
    if evidence_reasons:
        disposition = "evidence_failed"
    elif product_reasons:
        disposition = "product_failed"
    elif any(observation == "not_sure" for observation in cell["observations"].values()):
        disposition = "needs_review"
    elif telemetry["thermal_state_max"] == "serious":
        disposition = "accepted_limitation"
    else:
        disposition = "accepted"
    return disposition, reasons


def _sanitized_identity(app: dict[str, Any], device: dict[str, Any]) -> dict[str, Any]:
    return {
        "app": {
            field: (app[field].lower() if field in {"repo_commit", "app_tree_sha256"} else app[field]) for field in app
        },
        "device": {
            field: device[field]
            for field in (
                "product_type",
                "os_version",
                "os_build",
                "transport",
                "developer_mode",
            )
        },
    }


def _matrix(cells: list[dict[str, Any]]) -> dict[str, Any]:
    by_case = {cell["case_id"]: cell for cell in cells}
    reasons: list[str] = []
    for cell in cells:
        if cell["disposition"] in {"evidence_failed", "needs_review", "product_failed"}:
            reasons.append(f"cell_not_accepted:{cell['case_id']}:{cell['disposition']}")
        if cell["case_id"] in CASE_ORDER[:2] and cell["disposition"] == "unavailable":
            reasons.append(f"mandatory_cell_unavailable:{cell['case_id']}")
    provider = by_case["mvhevc-files-provider-sustained"]
    local = by_case["mvhevc-local-full"]
    if (
        provider["disposition"] != "unavailable"
        and provider["media"]["sha256"].lower() != local["media"]["sha256"].lower()
    ):
        reasons.append("provider_mvhevc_sha256_mismatch")
    reasons = sorted(set(reasons))
    return {
        "accepted": not reasons,
        "disposition": "accepted" if not reasons else "failed",
        "reasons": reasons,
    }


def _canonical_hash(value: dict[str, Any]) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _invalid_telemetry() -> dict[str, Any]:
    return {
        "elapsed_coverage_seconds": 0.0,
        "captured_coverage_seconds": 0.0,
        "clock_span_delta_seconds": 0.0,
        "active_playback_seconds": 0.0,
        "qualifying_playback_seconds": 0.0,
        "sampled_player_progress_seconds": 0.0,
        "sample_count": 0,
        "event_count": 0,
        "thermal_state_max": "unknown",
        "unknown_thermal_sample_count": 0,
        "playback_degradation": False,
        "item_error_categories": [],
        "memory_sample_count": 0,
        "post_warmup_memory_sample_count": 0,
        "memory_span_seconds": 0.0,
        "memory_warmup_seconds": 0.0,
        "post_warmup_growth_bytes": 0.0,
        "post_warmup_slope_mib_per_min": 0.0,
        "final_third_range_bytes": 0.0,
        "final_third_memory_sample_count": 0,
        "memory_plateau": True,
        "waiting_intervals": [],
        "unplanned_waiting_total_seconds": 0.0,
        "unplanned_waiting_max_seconds": 0.0,
        "unrecovered_waiting_max_seconds": 0.0,
    }


def _private_input(value: Any) -> dict[str, Any]:
    data = _mapping(value, "private input")
    root_fields = {
        "generated_at",
        "contract_version",
        "app_identity",
        "device_identity",
        "cells",
    }
    _keys(data, root_fields, root_fields, "private input")
    _timestamp(data["generated_at"], "private input.generated_at")
    if data["contract_version"] != CONTRACT_VERSION:
        raise EvidenceError("private input contract_version is unsupported.")
    app = _mapping(data["app_identity"], "private input.app_identity")
    app_fields = {
        "repo_commit",
        "bundle_id",
        "version",
        "build",
        "app_tree_sha256",
        "xcode_version",
        "qualification_compile_condition",
    }
    _keys(app, app_fields, app_fields, "private input.app_identity")
    for field in app_fields:
        _string(app[field], f"private input.app_identity.{field}")
    if not HEX_SHA.fullmatch(app["repo_commit"]):
        raise EvidenceError("private input.app_identity.repo_commit is invalid.")
    _hash(app["app_tree_sha256"], "private input.app_identity.app_tree_sha256")
    if app["qualification_compile_condition"] != "BD_TO_AVP_QUALIFICATION":
        raise EvidenceError("private input app was not built with the qualification compile condition.")
    device = _mapping(data["device_identity"], "private input.device_identity")
    device_fields = {
        "identifier",
        "udid",
        "product_type",
        "os_version",
        "os_build",
        "transport",
        "developer_mode",
    }
    _keys(device, device_fields, device_fields, "private input.device_identity")
    for field in device_fields - {"developer_mode"}:
        _string(device[field], f"private input.device_identity.{field}")
    if not isinstance(device["developer_mode"], bool):
        raise EvidenceError("private input.device_identity.developer_mode must be boolean.")
    cells = _list(data["cells"], "private input.cells")
    if {cell.get("case_id") for cell in cells if isinstance(cell, dict)} != set(CASE_ORDER):
        raise EvidenceError("private input.cells must contain every contract case exactly once.")
    normalized: dict[str, dict[str, Any]] = {}
    log_paths: set[Path] = set()
    for index, raw in enumerate(cells):
        cell = _mapping(raw, f"private input.cells[{index}]")
        required = {
            "case_id",
            "media",
            "source_class",
            "coverage",
            "battery",
            "observations",
        }
        _keys(
            cell,
            required,
            required | {"log_path", "unavailable_reason"},
            f"private input.cells[{index}]",
        )
        case_id = _enum(cell["case_id"], set(CASE_ORDER), f"private input.cells[{index}].case_id")
        if case_id in normalized:
            raise EvidenceError(f"duplicate private input cell {case_id!r}.")
        media = _mapping(cell["media"], f"private input.cells[{index}].media")
        media_fields = {
            "media_id",
            "sha256",
            "size_bytes",
            "duration_seconds",
            "codec_tag",
            "stereo_format",
        }
        _keys(media, media_fields, media_fields, f"private input.cells[{index}].media")
        _string(media["media_id"], f"private input.cells[{index}].media.media_id")
        _hash(media["sha256"], f"private input.cells[{index}].media.sha256")
        _nonnegative_int(media["size_bytes"], f"private input.cells[{index}].media.size_bytes")
        if not _is_number(media["duration_seconds"]) or media["duration_seconds"] <= 0:
            raise EvidenceError(f"private input.cells[{index}].media.duration_seconds must be positive.")
        _string(media["codec_tag"], f"private input.cells[{index}].media.codec_tag")
        _string(media["stereo_format"], f"private input.cells[{index}].media.stereo_format")
        source = _enum(
            cell["source_class"],
            SOURCE_CLASSES,
            f"private input.cells[{index}].source_class",
        )
        coverage = _enum(cell["coverage"], COVERAGES, f"private input.cells[{index}].coverage")
        has_log, has_unavailable = "log_path" in cell, "unavailable_reason" in cell
        if has_log == has_unavailable:
            raise EvidenceError(f"private input cell {case_id!r} needs exactly one log_path or unavailable_reason.")
        if has_log:
            _string(cell["log_path"], f"private input.cells[{index}].log_path")
            canonical_path = Path(cell["log_path"]).expanduser().resolve(strict=False)
            if canonical_path in log_paths:
                raise EvidenceError("private input reuses a log_path across cells.")
            log_paths.add(canonical_path)
        else:
            _enum(
                cell["unavailable_reason"],
                OPTIONAL_UNAVAILABLE_REASONS,
                f"private input.cells[{index}].unavailable_reason",
            )
        normalized[case_id] = {
            "case_id": case_id,
            "media": dict(media),
            "source_class": source,
            "coverage": coverage,
            "battery": _battery(cell["battery"], f"private input.cells[{index}].battery"),
            "observations": _observations(cell["observations"], f"private input.cells[{index}].observations"),
            **({"log_path": cell["log_path"]} if has_log else {"unavailable_reason": cell["unavailable_reason"]}),
        }
    return {
        "generated_at": data["generated_at"],
        "app_identity": dict(app),
        "device_identity": dict(device),
        "cells": normalized,
    }


def assemble(input_path: Path, output_path: Path) -> dict[str, Any]:
    data = _private_input(_json_loads_strict(input_path.read_text(encoding="utf-8"), "private input"))
    output_cells: list[dict[str, Any]] = []
    for case_id in CASE_ORDER:
        cell = data["cells"][case_id]
        _case_contract(cell)
        if "unavailable_reason" in cell:
            output_cells.append(
                {
                    "case_id": case_id,
                    "media": {
                        **cell["media"],
                        "sha256": cell["media"]["sha256"].lower(),
                    },
                    "source_class": cell["source_class"],
                    "coverage": cell["coverage"],
                    "log_sha256": None,
                    "evidence_summary": None,
                    "telemetry": None,
                    "battery": _battery_result(cell["battery"], 0.0),
                    "observations": cell["observations"],
                    "disposition": "unavailable",
                    "reasons": [cell["unavailable_reason"]],
                }
            )
            continue
        path = Path(cell["log_path"])
        try:
            content, records = _load_jsonl(path)
            evidence = _validate_records(records)
            if evidence["header"]["run_id"] != case_id:
                raise EvidenceError("run_id_mismatch")
            if evidence["header"]["media_id"] != cell["media"]["media_id"]:
                raise EvidenceError("media_id_mismatch")
            header_app = evidence["header"]["app"]
            if any(header_app[field] != data["app_identity"][field] for field in ("bundle_id", "version", "build")):
                raise EvidenceError("app_identity_mismatch")
            header_device = evidence["header"]["device"]
            if not _device_identity_matches(header_device, data["device_identity"]):
                raise EvidenceError("device_identity_mismatch")
            telemetry = _telemetry(evidence, cell["coverage"], float(cell["media"]["duration_seconds"]))
            disposition, reasons = _evaluate(cell, evidence, telemetry, cell["battery"])
            log_sha = hashlib.sha256(content).hexdigest()
        except EvidenceError as error:
            code = (
                str(error)
                if str(error)
                in {
                    "malformed_log",
                    "log_unreadable",
                    "run_id_mismatch",
                    "media_id_mismatch",
                    "app_identity_mismatch",
                    "device_identity_mismatch",
                }
                else "malformed_log"
            )
            evidence = {"valid": False, "error": code}
            telemetry = _invalid_telemetry()
            disposition, reasons = "evidence_failed", [code]
            try:
                log_sha = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                log_sha = None
        output_cells.append(
            {
                "case_id": case_id,
                "media": {**cell["media"], "sha256": cell["media"]["sha256"].lower()},
                "source_class": cell["source_class"],
                "coverage": cell["coverage"],
                "log_sha256": log_sha,
                "evidence_summary": evidence,
                "telemetry": telemetry,
                "battery": _battery_result(cell["battery"], telemetry["elapsed_coverage_seconds"]),
                "observations": cell["observations"],
                "disposition": disposition,
                "reasons": reasons,
            }
        )
    record: dict[str, Any] = {
        "schema_version": RECORD_SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "generated_at": data["generated_at"],
        "identity": _sanitized_identity(data["app_identity"], data["device_identity"]),
        "cells": output_cells,
    }
    record["matrix"] = _matrix(output_cells)
    record["record_sha256"] = _canonical_hash(record)
    _privacy(record)
    output_path.write_text(
        json.dumps(record, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return record


def _embedded_evidence(value: Any) -> dict[str, Any]:
    evidence = _mapping(value, "embedded evidence")
    evidence_fields = {"header", "samples", "events", "footer", "summary"}
    _keys(evidence, evidence_fields, evidence_fields, "embedded evidence")
    header = _mapping(evidence["header"], "embedded evidence.header")
    samples = [
        _mapping(value, f"embedded evidence.samples[{index}]")
        for index, value in enumerate(_list(evidence["samples"], "embedded evidence.samples"))
    ]
    events = [
        _mapping(value, f"embedded evidence.events[{index}]")
        for index, value in enumerate(_list(evidence["events"], "embedded evidence.events"))
    ]
    footer = _mapping(evidence["footer"], "embedded evidence.footer")
    ordered_records = sorted(
        [*samples, *events],
        key=lambda item: (item["elapsed_seconds"], item["captured_at"]),
    )
    records = [header, *ordered_records, footer]
    checked = _validate_records(records)
    summary = _mapping(evidence["summary"], "embedded evidence.summary")
    summary_fields = {
        "sample_count",
        "event_count",
        "elapsed_start_seconds",
        "elapsed_end_seconds",
        "captured_start",
        "captured_end",
    }
    _keys(summary, summary_fields, summary_fields, "embedded evidence.summary")
    if summary != checked["summary"]:
        raise EvidenceError("embedded evidence summary is stale.")
    return checked


def _identity(value: Any) -> None:
    identity = _mapping(value, "record.identity")
    _keys(identity, {"app", "device"}, {"app", "device"}, "record.identity")
    app = _mapping(identity["app"], "record.identity.app")
    app_fields = {
        "repo_commit",
        "bundle_id",
        "version",
        "build",
        "app_tree_sha256",
        "xcode_version",
        "qualification_compile_condition",
    }
    _keys(app, app_fields, app_fields, "record.identity.app")
    for field in app_fields:
        _string(app[field], f"record.identity.app.{field}")
    if not HEX_SHA.fullmatch(app["repo_commit"]):
        raise EvidenceError("record.identity.app.repo_commit is invalid.")
    _hash(app["app_tree_sha256"], "record.identity.app.app_tree_sha256")
    device = _mapping(identity["device"], "record.identity.device")
    device_fields = {
        "product_type",
        "os_version",
        "os_build",
        "transport",
        "developer_mode",
    }
    _keys(device, device_fields, device_fields, "record.identity.device")
    for field in device_fields - {"developer_mode"}:
        _string(device[field], f"record.identity.device.{field}")
    if not isinstance(device["developer_mode"], bool):
        raise EvidenceError("record.identity.device.developer_mode must be boolean.")


def validate_record(path: Path) -> dict[str, Any]:
    record = _mapping(_json_loads_strict(path.read_text(encoding="utf-8"), "record"), "record")
    _privacy(record)
    fields = {
        "schema_version",
        "contract_version",
        "generated_at",
        "identity",
        "cells",
        "matrix",
        "record_sha256",
    }
    _keys(record, fields, fields, "record")
    if (
        not _is_integer(record["schema_version"])
        or record["schema_version"] != RECORD_SCHEMA_VERSION
        or record["contract_version"] != CONTRACT_VERSION
    ):
        raise EvidenceError("record schema or contract version is unsupported.")
    _timestamp(record["generated_at"], "record.generated_at")
    _identity(record["identity"])
    identity_app = _mapping(record["identity"], "record.identity")["app"]
    content_without_hash = {key: value for key, value in record.items() if key != "record_sha256"}
    if _hash(record["record_sha256"], "record.record_sha256") != _canonical_hash(content_without_hash):
        raise EvidenceError("record_sha256 does not match record content.")
    cells = _list(record["cells"], "record.cells")
    if len(cells) != len(CASE_ORDER):
        raise EvidenceError("record.cells must contain exactly four cells.")
    for index, raw in enumerate(cells):
        cell = _mapping(raw, f"record.cells[{index}]")
        cell_fields = {
            "case_id",
            "media",
            "source_class",
            "coverage",
            "log_sha256",
            "evidence_summary",
            "telemetry",
            "battery",
            "observations",
            "disposition",
            "reasons",
        }
        _keys(cell, cell_fields, cell_fields, f"record.cells[{index}]")
        if cell["case_id"] != CASE_ORDER[index]:
            raise EvidenceError("record cells are not in canonical order.")
        case_id = _enum(cell["case_id"], set(CASE_ORDER), f"record.cells[{index}].case_id")
        media = _mapping(cell["media"], f"record.cells[{index}].media")
        media_fields = {
            "media_id",
            "sha256",
            "size_bytes",
            "duration_seconds",
            "codec_tag",
            "stereo_format",
        }
        _keys(media, media_fields, media_fields, f"record.cells[{index}].media")
        _string(media["media_id"], f"record.cells[{index}].media.media_id")
        _hash(media["sha256"], f"record.cells[{index}].media.sha256")
        _nonnegative_int(media["size_bytes"], f"record.cells[{index}].media.size_bytes")
        if not _is_number(media["duration_seconds"]) or media["duration_seconds"] <= 0:
            raise EvidenceError("record media duration must be positive.")
        _string(media["codec_tag"], f"record.cells[{index}].media.codec_tag")
        _string(media["stereo_format"], f"record.cells[{index}].media.stereo_format")
        source = _enum(cell["source_class"], SOURCE_CLASSES, f"record.cells[{index}].source_class")
        coverage = _enum(cell["coverage"], COVERAGES, f"record.cells[{index}].coverage")
        _case_contract(
            {
                "case_id": case_id,
                "media": media,
                "source_class": source,
                "coverage": coverage,
            }
        )
        observations = _observations(cell["observations"], f"record.cells[{index}].observations")
        stored_battery = _mapping(cell["battery"], f"record.cells[{index}].battery")
        battery_fields = {
            "start_percent",
            "end_percent",
            "charging",
            "low_power_interruption",
            "drain_percent",
            "drain_percent_per_hour",
        }
        _keys(
            stored_battery,
            battery_fields,
            battery_fields,
            f"record.cells[{index}].battery",
        )
        battery = _battery(
            {
                field: stored_battery[field]
                for field in (
                    "start_percent",
                    "end_percent",
                    "charging",
                    "low_power_interruption",
                )
            },
            f"record.cells[{index}].battery",
        )
        if not _is_number(stored_battery["drain_percent"]) or not _is_number(stored_battery["drain_percent_per_hour"]):
            raise EvidenceError("record battery telemetry is invalid.")
        if cell["evidence_summary"] is None:
            if cell["log_sha256"] is not None or cell["telemetry"] is not None or cell["disposition"] != "unavailable":
                raise EvidenceError("unavailable cell contains evidence.")
            if (
                not isinstance(cell["reasons"], list)
                or len(cell["reasons"]) != 1
                or cell["reasons"][0] not in OPTIONAL_UNAVAILABLE_REASONS
            ):
                raise EvidenceError("unavailable cell reason is invalid.")
            expected_battery = _battery_result(battery, 0.0)
            if stored_battery != expected_battery:
                raise EvidenceError("unavailable battery telemetry is stale.")
        elif isinstance(cell["evidence_summary"], dict) and cell["evidence_summary"].get("valid") is False:
            sentinel = cell["evidence_summary"]
            _keys(
                sentinel,
                {"valid", "error"},
                {"valid", "error"},
                f"record.cells[{index}].evidence_summary",
            )
            if sentinel["error"] not in {
                "malformed_log",
                "log_unreadable",
                "run_id_mismatch",
                "media_id_mismatch",
                "app_identity_mismatch",
                "device_identity_mismatch",
            }:
                raise EvidenceError("invalid evidence summary sentinel.")
            if cell["log_sha256"] is not None:
                _hash(cell["log_sha256"], f"record.cells[{index}].log_sha256")
            if cell["telemetry"] != _invalid_telemetry():
                raise EvidenceError("invalid evidence summary contains stale data.")
            if cell["disposition"] != "evidence_failed" or cell["reasons"] != [sentinel["error"]]:
                raise EvidenceError("invalid evidence summary disposition is stale.")
            if stored_battery != _battery_result(battery, 0.0):
                raise EvidenceError("invalid evidence summary battery telemetry is stale.")
        else:
            _hash(cell["log_sha256"], f"record.cells[{index}].log_sha256")
            evidence = _embedded_evidence(cell["evidence_summary"])
            if evidence["header"]["run_id"] != case_id:
                raise EvidenceError(f"record.cells[{index}] evidence run_id is inconsistent.")
            if evidence["header"]["media_id"] != media["media_id"]:
                raise EvidenceError(f"record.cells[{index}] evidence media_id is inconsistent.")
            if any(
                evidence["header"]["app"][field] != identity_app[field] for field in ("bundle_id", "version", "build")
            ):
                raise EvidenceError(f"record.cells[{index}] evidence app identity is inconsistent.")
            identity_device = _mapping(record["identity"], "record.identity")["device"]
            if not _device_identity_matches(evidence["header"]["device"], identity_device):
                raise EvidenceError(f"record.cells[{index}] evidence device identity is inconsistent.")
            telemetry = _mapping(cell["telemetry"], f"record.cells[{index}].telemetry")
            expected_telemetry = _telemetry(evidence, coverage, float(media["duration_seconds"]))
            if telemetry != expected_telemetry:
                raise EvidenceError(f"record.cells[{index}].telemetry is stale.")
            check_cell = {
                "case_id": case_id,
                "media": media,
                "source_class": source,
                "coverage": coverage,
                "observations": observations,
            }
            disposition, reasons = _evaluate(check_cell, evidence, expected_telemetry, battery)
            expected_battery = _battery_result(battery, expected_telemetry["elapsed_coverage_seconds"])
            if stored_battery != expected_battery:
                raise EvidenceError(f"record.cells[{index}].battery telemetry is stale.")
            if cell["disposition"] != disposition or cell["reasons"] != reasons:
                raise EvidenceError(f"record.cells[{index}] disposition or reasons are stale.")
    matrix = _mapping(record["matrix"], "record.matrix")
    _keys(
        matrix,
        {"accepted", "disposition", "reasons"},
        {"accepted", "disposition", "reasons"},
        "record.matrix",
    )
    if not isinstance(matrix["accepted"], bool) or matrix["disposition"] not in {
        "accepted",
        "failed",
    }:
        raise EvidenceError("record.matrix has invalid acceptance fields.")
    if not isinstance(matrix["reasons"], list) or not all(isinstance(reason, str) for reason in matrix["reasons"]):
        raise EvidenceError("record.matrix.reasons must be a string array.")
    if matrix != _matrix(cells):
        raise EvidenceError("record.matrix is stale.")
    return {
        "schema_version": RECORD_SCHEMA_VERSION,
        "record_sha256": record["record_sha256"],
        "matrix": matrix,
    }


def _print_json(value: Any) -> None:
    print(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    log_parser = commands.add_parser("validate-log")
    log_parser.add_argument("--log", required=True, type=Path)
    log_parser.add_argument("--json", action="store_true")
    assemble_parser = commands.add_parser("assemble")
    assemble_parser.add_argument("--input", required=True, type=Path)
    assemble_parser.add_argument("--output", required=True, type=Path)
    record_parser = commands.add_parser("validate-record")
    record_parser.add_argument("--record", required=True, type=Path)
    record_parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "validate-log":
            result = validate_log(args.log)
            if args.json:
                _print_json({"valid": True, "summary": result})
            else:
                for key in sorted(result):
                    print(f"{key}: {result[key]}")
        elif args.command == "assemble":
            result = assemble(args.input, args.output)
            _print_json({"record_sha256": result["record_sha256"], "matrix": result["matrix"]})
            return 0 if result["matrix"]["accepted"] else 2
        else:
            result = validate_record(args.record)
            if args.json:
                _print_json({"valid": True, **result})
            else:
                print(
                    f"valid: true\nrecord_sha256: {result['record_sha256']}\nmatrix: {result['matrix']['disposition']}"
                )
            return 0 if result["matrix"]["accepted"] else 2
        return 0
    except (EvidenceError, OSError, UnicodeError) as error:
        if getattr(args, "json", False):
            _print_json({"valid": False, "error": str(error) or error.__class__.__name__})
        else:
            print(f"error: {str(error) or error.__class__.__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
