from __future__ import annotations

import datetime as dt
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

from scripts import qualify_visionos_sustained_playback as qualification

SHA_A = "a" * 64
SHA_B = "b" * 64


class QualificationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.base_time = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def stamp(self, elapsed: float) -> str:
        return (self.base_time + dt.timedelta(seconds=elapsed)).isoformat().replace("+00:00", "Z")

    def record_common(self, kind: str, elapsed: float) -> dict[str, object]:
        return {
            "schema_version": 1,
            "kind": kind,
            "captured_at": self.stamp(elapsed),
            "elapsed_seconds": elapsed,
        }

    def header(self, media_id: str, run_id: str = "run-test-1") -> dict[str, object]:
        return {
            **self.record_common("header", 0),
            "run_id": run_id,
            "media_id": media_id,
            "sample_interval_seconds": 15,
            "app": {"bundle_id": "com.example.player", "version": "1.0", "build": "1"},
            "device": {
                "hardware_model": "RealityDevice1,1",
                "operating_system": "Version 27.0 (Build 24M000)",
            },
        }

    def event(self, name: str, elapsed: float, detail: str | None = None) -> dict[str, object]:
        return {
            **self.record_common("event", elapsed),
            "event": name,
            "player_time_seconds": elapsed,
            "detail": detail,
        }

    def sample(
        self,
        elapsed: float,
        *,
        physical: int | None = 300 * 1024 * 1024,
        thermal: str = "nominal",
        rate: float = 1.0,
        likely_to_keep_up: bool | None = True,
        item_error_category: str = "none",
        duration: float = 1800,
        player_time: float | None = None,
    ) -> dict[str, object]:
        return {
            **self.record_common("sample", elapsed),
            "thermal_state": thermal,
            "physical_footprint_bytes": physical,
            "player_time_seconds": elapsed if player_time is None else player_time,
            "duration_seconds": duration,
            "rate": rate,
            "time_control_status": "playing",
            "waiting_reason": "none",
            "item_status": "ready",
            "likely_to_keep_up": likely_to_keep_up,
            "item_error_category": item_error_category,
        }

    def footer(
        self,
        elapsed: float,
        duration: float,
        reason: str,
        final_position: float | None = None,
    ) -> dict[str, object]:
        return {
            **self.record_common("footer", elapsed),
            "reason": reason,
            "final_player_time_seconds": (duration if final_position is None else final_position),
            "duration_seconds": duration,
        }

    def write_log(
        self,
        name: str,
        *,
        media_id: str,
        coverage: str,
        duration: float,
        samples: list[dict[str, object]] | None = None,
        extra_events: list[dict[str, object]] | None = None,
        footer_reason: str | None = None,
        final_position: float | None = None,
        run_id: str | None = None,
    ) -> Path:
        inferred_run_ids: dict[tuple[str, str], str] = {
            ("mv", "full_length"): "mvhevc-local-full",
            ("sbs", "full_length"): "packed-sbs-local-full",
            ("mv-provider", "sustained"): "mvhevc-files-provider-sustained",
            ("ou", "sustained"): "packed-ou-local-sustained",
        }
        run_id = run_id or inferred_run_ids.get((media_id, coverage), "run-test-1")
        events = [
            self.event("prepare", 1),
            self.event("ready", 2),
            self.event("time_control_changed", 3, "playing"),
        ]
        if coverage == "full_length":
            events.extend(
                [
                    self.event("pause_requested", 90, "user_pause"),
                    self.event("time_control_changed", 90.1, "paused"),
                    self.event("play_requested", 95, "user_resume"),
                    self.event("time_control_changed", 95.1, "playing"),
                    self.event("seek_started", 120, "seek"),
                    self.event("seek_completed", 122, "seek"),
                ]
            )
        if media_id in {"sbs", "ou"}:
            events.extend(
                [
                    self.event("eye_order_change_started", 150, "eye_order_change"),
                    self.event("eye_order_change_completed", 152, "eye_order_change"),
                ]
            )
        events.extend(extra_events or [])
        if footer_reason is None:
            footer_reason = "playback_finished" if coverage == "full_length" else "session_finished"
        final_event = "playback_finished" if footer_reason == "playback_finished" else "session_finished"
        events.append(self.event(final_event, duration - 1))
        if samples is None:
            sample_times = [duration * index / 7 for index in range(7)] + [duration - 1]
            samples = [self.sample(float(time), duration=duration) for time in sample_times]
        records = [
            self.header(media_id, run_id),
            *events,
            *samples,
            self.footer(duration, duration, footer_reason, final_position),
        ]
        ordered_records = sorted(
            records[1:-1],
            key=lambda record: (record["elapsed_seconds"], record["captured_at"]),
        )
        body = "\n".join(json.dumps(record, sort_keys=True) for record in ordered_records)
        content = (
            json.dumps(records[0], sort_keys=True) + "\n" + body + "\n" + json.dumps(records[-1], sort_keys=True) + "\n"
        )
        path = self.root / name
        path.write_text(content, encoding="utf-8")
        return path

    def media(
        self,
        media_id: str,
        sha: str,
        stereo: str,
        codec: str = "hevc",
        duration: float = 600,
    ) -> dict[str, object]:
        return {
            "media_id": media_id,
            "sha256": sha,
            "size_bytes": 10_000,
            "duration_seconds": duration,
            "codec_tag": codec,
            "stereo_format": stereo,
        }

    def private_input(
        self,
        *,
        logs: dict[str, Path],
        shas: dict[str, str] | None = None,
        unavailable: set[str] | None = None,
        observations: dict[str, dict[str, str]] | None = None,
        batteries: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        shas = shas or {}
        unavailable = unavailable or set()
        observations = observations or {}
        batteries = batteries or {}
        specs = {
            "mvhevc-local-full": (
                "mv",
                "local_documents",
                "full_length",
                "mv-hevc",
                "mv-hevc",
                600,
            ),
            "packed-sbs-local-full": (
                "sbs",
                "local_documents",
                "full_length",
                "hevc",
                "side-by-side",
                600,
            ),
            "mvhevc-files-provider-sustained": (
                "mv-provider",
                "files_provider",
                "sustained",
                "mv-hevc",
                "mv-hevc",
                1800,
            ),
            "packed-ou-local-sustained": (
                "ou",
                "local_documents",
                "sustained",
                "hevc",
                "over-under",
                1800,
            ),
        }
        cells = []
        for case_id in qualification.CASE_ORDER:
            media_id, source, coverage, codec, stereo, duration = specs[case_id]
            sha = shas.get(case_id, SHA_A if "mv" in case_id else SHA_B)
            cell: dict[str, Any] = {
                "case_id": case_id,
                "media": self.media(media_id, sha, stereo, codec, duration),
                "source_class": source,
                "coverage": coverage,
                "battery": batteries.get(
                    case_id,
                    {
                        "start_percent": 95,
                        "end_percent": 80,
                        "charging": False,
                        "low_power_interruption": False,
                    },
                ),
                "observations": observations.get(
                    case_id,
                    {
                        "picture": "yes",
                        "depth": "yes",
                        "eye_order": "yes",
                        "audio_sync": "yes",
                    },
                ),
            }
            if case_id in unavailable:
                cell["unavailable_reason"] = "provider_unavailable"
            else:
                cell["log_path"] = str(logs[case_id])
            cells.append(cell)
        return {
            "generated_at": "2026-09-01T12:00:00Z",
            "contract_version": qualification.CONTRACT_VERSION,
            "app_identity": {
                "repo_commit": "c" * 40,
                "bundle_id": "com.example.player",
                "version": "1.0",
                "build": "1",
                "app_tree_sha256": "d" * 64,
                "xcode_version": "Xcode 26.5",
                "qualification_compile_condition": "BD_TO_AVP_QUALIFICATION",
            },
            "device_identity": {
                "identifier": "fake-device-identifier",
                "udid": "fake-udid",
                "product_type": "RealityDevice1,1",
                "os_version": "27.0",
                "os_build": "24M000",
                "transport": "usb",
                "developer_mode": True,
            },
            "cells": cells,
        }

    def assemble(self, data: dict[str, Any]) -> dict[str, Any]:
        input_path = self.root / "private-input.json"
        output_path = self.root / "record.json"
        input_path.write_text(json.dumps(data), encoding="utf-8")
        return qualification.assemble(input_path, output_path)

    def clean_logs(self) -> dict[str, Path]:
        return {
            "mvhevc-local-full": self.write_log("mv.jsonl", media_id="mv", coverage="full_length", duration=600),
            "packed-sbs-local-full": self.write_log("sbs.jsonl", media_id="sbs", coverage="full_length", duration=600),
            "mvhevc-files-provider-sustained": self.write_log(
                "provider.jsonl",
                media_id="mv-provider",
                coverage="sustained",
                duration=1800,
            ),
            "packed-ou-local-sustained": self.write_log("ou.jsonl", media_id="ou", coverage="sustained", duration=1800),
        }


class ValidateLogTests(QualificationTestCase):
    def test_frozen_contract_matches_enforced_constants_and_enums(self) -> None:
        contract_path = (
            Path(__file__).resolve().parents[1] / "docs/qualification/visionos-sustained-playback-contract-v1.json"
        )
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        acceptance = contract["acceptance"]
        memory = acceptance["memory"]
        waiting = acceptance["waiting"]
        battery = acceptance["battery"]

        self.assertEqual(contract["contract_version"], qualification.CONTRACT_VERSION)
        self.assertEqual(contract["schema_version"], qualification.RECORD_SCHEMA_VERSION)
        self.assertEqual(
            contract["log"]["sample_interval_seconds"],
            qualification.SAMPLE_INTERVAL_SECONDS,
        )
        self.assertEqual(set(contract["log"]["event"]["event"]), qualification.EVENTS)
        self.assertEqual(
            set(value for value in contract["log"]["event"]["detail"] if value is not None),
            qualification.DETAILS,
        )
        self.assertEqual(
            set(contract["log"]["sample"]["thermal_state"]),
            qualification.THERMAL_STATES,
        )
        self.assertEqual(
            set(contract["log"]["sample"]["time_control_status"]),
            qualification.TIME_CONTROL_STATES,
        )
        self.assertEqual(
            set(contract["log"]["footer"]["reason"]),
            qualification.FOOTER_REASONS,
        )
        self.assertEqual(
            contract["matrix"]["cells"]["mvhevc-files-provider-sustained"]["minimum_elapsed_seconds"],
            qualification.SUSTAINED_SECONDS,
        )
        self.assertEqual(
            acceptance["full_length_completion_fraction"],
            qualification.FULL_LENGTH_COMPLETION_FRACTION,
        )
        self.assertEqual(
            acceptance["full_length_active_playback_fraction"],
            qualification.FULL_LENGTH_ACTIVE_PLAYBACK_FRACTION,
        )
        self.assertEqual(
            acceptance["full_length_sampled_media_progress_fraction"],
            qualification.FULL_LENGTH_MEDIA_PROGRESS_FRACTION,
        )
        self.assertEqual(
            acceptance["sustained_active_playback_fraction"],
            qualification.SUSTAINED_ACTIVE_PLAYBACK_FRACTION,
        )
        self.assertEqual(
            acceptance["sustained_sampled_media_progress_fraction"],
            qualification.SUSTAINED_MEDIA_PROGRESS_FRACTION,
        )
        self.assertEqual(
            acceptance["clock_span_tolerance"]["seconds"],
            qualification.CLOCK_SPAN_TOLERANCE_SECONDS,
        )
        self.assertEqual(
            acceptance["clock_span_tolerance"]["elapsed_fraction"],
            qualification.CLOCK_SPAN_TOLERANCE_FRACTION,
        )
        self.assertEqual(memory["minimum_samples"], qualification.MEMORY_MINIMUM_SAMPLES)
        self.assertEqual(
            memory["minimum_span_seconds"],
            qualification.MEMORY_MINIMUM_SPAN_SECONDS,
        )
        self.assertEqual(
            memory["minimum_post_warmup_samples"],
            qualification.MEMORY_MINIMUM_POST_WARMUP_SAMPLES,
        )
        self.assertEqual(
            memory["minimum_final_third_samples"],
            qualification.MEMORY_MINIMUM_FINAL_THIRD_SAMPLES,
        )
        self.assertEqual(memory["growth_bytes"], qualification.MEMORY_GROWTH_BYTES)
        self.assertEqual(
            memory["slope_mib_per_minute"],
            qualification.MEMORY_SLOPE_MIB_PER_MINUTE,
        )
        self.assertEqual(
            memory["final_third_range_bytes"],
            qualification.MEMORY_FINAL_THIRD_RANGE_BYTES,
        )
        self.assertEqual(
            waiting["local_max_unplanned_seconds"],
            qualification.LOCAL_MAX_UNPLANNED_WAITING_SECONDS,
        )
        self.assertEqual(
            waiting["provider_max_unplanned_seconds"],
            qualification.PROVIDER_MAX_UNPLANNED_WAITING_SECONDS,
        )
        self.assertEqual(
            waiting["provider_max_unrecovered_seconds"],
            qualification.PROVIDER_MAX_UNRECOVERED_WAITING_SECONDS,
        )
        self.assertEqual(
            waiting["provider_total_unplanned_seconds"],
            qualification.PROVIDER_TOTAL_UNPLANNED_WAITING_SECONDS,
        )
        self.assertEqual(
            battery["full_length_minimum_start_percent"],
            qualification.FULL_LENGTH_MINIMUM_BATTERY_PERCENT,
        )

    def test_validate_log_clean_summary_is_deterministic(self) -> None:
        path = self.write_log("clean.jsonl", media_id="mv", coverage="full_length", duration=600)
        first = qualification.validate_log(path)
        second = qualification.validate_log(path)
        self.assertEqual(first, second)
        self.assertEqual(first["sample_count"], 8)
        self.assertEqual(first["footer_reason"], "playback_finished")

    def test_validate_log_rejects_structure_and_privacy_errors(self) -> None:
        path = self.write_log("bad.jsonl", media_id="mv", coverage="full_length", duration=600)
        lines = path.read_text(encoding="utf-8").splitlines()
        records = [json.loads(line) for line in lines]
        records[1]["extra"] = {"nested": {"file_path": "/private/secret.mov"}}
        path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")
        with self.assertRaises(qualification.EvidenceError):
            qualification.validate_log(path)

    def test_cli_validate_log_json_is_machine_readable(self) -> None:
        path = self.write_log("cli.jsonl", media_id="mv", coverage="full_length", duration=600)
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = qualification.main(["validate-log", "--log", str(path), "--json"])
        self.assertEqual(exit_code, 0)
        self.assertTrue(json.loads(output.getvalue())["valid"])

    def test_validate_log_accepts_capture_time_records_written_out_of_order(self) -> None:
        path = self.write_log(
            "out-of-order.jsonl",
            media_id="mv",
            coverage="full_length",
            duration=600,
        )
        records = [json.loads(line) for line in path.read_text().splitlines()]
        first_index = next(index for index, record in enumerate(records) if record.get("event") == "seek_started")
        second_index = next(index for index, record in enumerate(records) if record.get("event") == "seek_completed")
        records[first_index], records[second_index] = (
            records[second_index],
            records[first_index],
        )
        path.write_text(
            "\n".join(json.dumps(record, sort_keys=True) for record in records) + "\n",
            encoding="utf-8",
        )
        summary = qualification.validate_log(path)
        self.assertEqual(summary["footer_reason"], "playback_finished")


class AcceptanceTests(QualificationTestCase):
    def test_clean_pass_and_independent_record_validation(self) -> None:
        logs = self.clean_logs()
        record = self.assemble(self.private_input(logs=logs))
        self.assertTrue(record["matrix"]["accepted"])
        self.assertTrue(all(cell["disposition"] == "accepted" for cell in record["cells"]))
        self.assertEqual(
            qualification.validate_record(self.root / "record.json")["matrix"]["accepted"],
            True,
        )

    def test_serious_thermal_is_accepted_limitation_and_critical_fails(self) -> None:
        logs = self.clean_logs()
        times = [0, 60, 120, 180, 240, 300, 360, 420, 599]
        serious_samples = [self.sample(float(time), thermal="serious") for time in times]
        critical_samples = [self.sample(float(time), thermal="critical") for time in times]
        logs["mvhevc-local-full"] = self.write_log(
            "serious.jsonl",
            media_id="mv",
            coverage="full_length",
            duration=600,
            samples=serious_samples,
        )
        logs["packed-sbs-local-full"] = self.write_log(
            "critical.jsonl",
            media_id="sbs",
            coverage="full_length",
            duration=600,
            samples=critical_samples,
        )
        record = self.assemble(self.private_input(logs=logs))
        self.assertEqual(record["cells"][0]["disposition"], "accepted_limitation")
        self.assertEqual(record["cells"][1]["disposition"], "product_failed")
        self.assertFalse(record["matrix"]["accepted"])

    def test_local_stall_and_provider_stall_rules(self) -> None:
        logs = self.clean_logs()
        local_events = [
            self.event("time_control_changed", 500, "playing->waiting"),
            self.event("time_control_changed", 506, "waiting->playing"),
        ]
        provider_events = []
        for start in [700, 720, 740, 760, 780, 800, 820]:
            provider_events.extend(
                [
                    self.event("time_control_changed", start, "playing->waiting"),
                    self.event("time_control_changed", start + 10, "waiting->playing"),
                ]
            )
        logs["mvhevc-local-full"] = self.write_log(
            "stall-local.jsonl",
            media_id="mv",
            coverage="full_length",
            duration=600,
            extra_events=local_events,
        )
        logs["mvhevc-files-provider-sustained"] = self.write_log(
            "stall-provider.jsonl",
            media_id="mv-provider",
            coverage="sustained",
            duration=1800,
            extra_events=provider_events,
        )
        record = self.assemble(self.private_input(logs=logs))
        self.assertEqual(record["cells"][0]["disposition"], "product_failed")
        self.assertIn("local_unplanned_waiting_over_5_seconds", record["cells"][0]["reasons"])
        self.assertEqual(record["cells"][2]["disposition"], "product_failed")
        self.assertIn("provider_unplanned_waiting_limit_exceeded", record["cells"][2]["reasons"])

    def test_provider_recovered_ten_second_stall_passes(self) -> None:
        logs = self.clean_logs()
        events = [
            self.event("time_control_changed", 700, "playing->waiting"),
            self.event("time_control_changed", 710, "waiting->playing"),
        ]
        logs["mvhevc-files-provider-sustained"] = self.write_log(
            "recovered.jsonl",
            media_id="mv-provider",
            coverage="sustained",
            duration=1800,
            extra_events=events,
        )
        record = self.assemble(self.private_input(logs=logs))
        cell = record["cells"][2]
        self.assertEqual(cell["disposition"], "accepted")
        self.assertEqual(cell["telemetry"]["unplanned_waiting_total_seconds"], 10.0)

    def test_bare_status_labels_close_waiting_intervals(self) -> None:
        intervals = qualification._waiting_intervals(
            [
                self.event("time_control_changed", 100, "waiting"),
                self.event("time_control_changed", 110, "playing"),
                self.event("time_control_changed", 200, "waiting"),
                self.event("time_control_changed", 205, "none"),
            ],
            600,
        )
        self.assertEqual(len(intervals), 2)
        self.assertEqual(intervals[0]["end_elapsed_seconds"], 110)
        self.assertTrue(intervals[0]["recovered"])
        self.assertEqual(intervals[1]["end_elapsed_seconds"], 205)
        self.assertFalse(intervals[1]["recovered"])

    def test_seek_and_pause_waits_are_excluded(self) -> None:
        logs = self.clean_logs()
        events = [
            self.event("seek_started", 100),
            self.event("time_control_changed", 101, "playing->waiting"),
            self.event("time_control_changed", 102, "waiting->playing"),
            self.event("seek_completed", 102.5),
            self.event("pause_requested", 200),
            self.event("time_control_changed", 201, "playing->waiting"),
            self.event("time_control_changed", 202, "waiting->paused"),
            self.event("play_requested", 202.5, "user_resume"),
            self.event("time_control_changed", 202.6, "playing"),
        ]
        logs["mvhevc-local-full"] = self.write_log(
            "excluded.jsonl",
            media_id="mv",
            coverage="full_length",
            duration=600,
            extra_events=events,
        )
        record = self.assemble(self.private_input(logs=logs))
        cell = record["cells"][0]
        self.assertEqual(cell["disposition"], "accepted")
        self.assertEqual(cell["telemetry"]["unplanned_waiting_total_seconds"], 0.0)
        self.assertEqual(len(cell["telemetry"]["waiting_intervals"]), 2)

    def test_missing_and_null_memory_evidence_fail_closed(self) -> None:
        logs = self.clean_logs()
        null_samples = [self.sample(float(time), physical=None) for time in [0, 60, 120, 180, 240, 300, 360, 420]]
        logs["mvhevc-local-full"] = self.write_log(
            "null-memory.jsonl",
            media_id="mv",
            coverage="full_length",
            duration=600,
            samples=null_samples,
        )
        record = self.assemble(self.private_input(logs=logs))
        cell = record["cells"][0]
        self.assertEqual(cell["disposition"], "evidence_failed")
        self.assertIn("null_physical_footprint", cell["reasons"])
        self.assertIn("insufficient_memory_samples", cell["reasons"])

    def test_memory_growth_slope_and_no_plateau_fail(self) -> None:
        samples = []
        for elapsed in range(0, 1801, 15):
            growth = max(0, elapsed - 600) * 15 * MIB // 60
            samples.append(self.sample(float(elapsed), physical=300 * MIB + growth))
        logs = self.clean_logs()
        logs["mvhevc-files-provider-sustained"] = self.write_log(
            "memory-growth.jsonl",
            media_id="mv-provider",
            coverage="sustained",
            duration=1800,
            samples=samples,
        )
        record = self.assemble(self.private_input(logs=logs))
        cell = record["cells"][2]
        self.assertEqual(cell["disposition"], "product_failed")
        self.assertIn("memory_growth_slope_no_plateau", cell["reasons"])

    def test_full_completion_threshold_and_battery_rules(self) -> None:
        logs = self.clean_logs()
        logs["mvhevc-local-full"] = self.write_log(
            "short-completion.jsonl",
            media_id="mv",
            coverage="full_length",
            duration=600,
            final_position=596.9,
        )
        batteries = {
            "packed-sbs-local-full": {
                "start_percent": 89,
                "end_percent": 80,
                "charging": False,
                "low_power_interruption": False,
            }
        }
        record = self.assemble(self.private_input(logs=logs, batteries=batteries))
        first, second = record["cells"][0], record["cells"][1]
        self.assertEqual(first["disposition"], "product_failed")
        self.assertIn("full_length_completion_below_99_5_percent", first["reasons"])
        self.assertEqual(second["disposition"], "product_failed")
        self.assertIn("battery_start_below_90_percent", second["reasons"])

    def test_optional_unavailable_and_mandatory_unavailable(self) -> None:
        logs = self.clean_logs()
        optional = {"mvhevc-files-provider-sustained", "packed-ou-local-sustained"}
        record = self.assemble(self.private_input(logs=logs, unavailable=optional))
        self.assertTrue(record["matrix"]["accepted"])
        self.assertTrue(all(record["cells"][index]["disposition"] == "unavailable" for index in [2, 3]))
        record = self.assemble(self.private_input(logs=logs, unavailable={"mvhevc-local-full"}))
        self.assertFalse(record["matrix"]["accepted"])
        self.assertIn("mandatory_cell_unavailable:mvhevc-local-full", record["matrix"]["reasons"])

    def test_provider_sha_mismatch_and_not_sure(self) -> None:
        logs = self.clean_logs()
        record = self.assemble(self.private_input(logs=logs, shas={"mvhevc-files-provider-sustained": SHA_B}))
        self.assertFalse(record["matrix"]["accepted"])
        self.assertIn("provider_mvhevc_sha256_mismatch", record["matrix"]["reasons"])
        record = self.assemble(
            self.private_input(
                logs=logs,
                observations={
                    "packed-ou-local-sustained": {
                        "picture": "not_sure",
                        "depth": "yes",
                        "eye_order": "yes",
                        "audio_sync": "yes",
                    }
                },
            )
        )
        self.assertEqual(record["cells"][3]["disposition"], "needs_review")
        self.assertFalse(record["matrix"]["accepted"])

    def test_battery_charging_and_low_power_interruption_fail_full_cell(self) -> None:
        logs = self.clean_logs()
        battery = {
            "start_percent": 95,
            "end_percent": 80,
            "charging": True,
            "low_power_interruption": True,
        }
        record = self.assemble(self.private_input(logs=logs, batteries={"mvhevc-local-full": battery}))
        reasons = record["cells"][0]["reasons"]
        self.assertIn("battery_was_charging", reasons)
        self.assertIn("battery_low_power_interruption", reasons)

    def test_deterministic_assembly_does_not_persist_private_paths_or_ids(self) -> None:
        logs = self.clean_logs()
        data = self.private_input(logs=logs)
        first = self.assemble(data)
        first_bytes = (self.root / "record.json").read_bytes()
        second = self.assemble(data)
        second_bytes = (self.root / "record.json").read_bytes()
        self.assertEqual(first, second)
        self.assertEqual(first_bytes, second_bytes)
        serialized = json.dumps(first)
        for path in logs.values():
            self.assertNotIn(str(path), serialized)
        self.assertNotIn("fake-device-identifier", serialized)
        self.assertNotIn("fake-udid", serialized)

    def test_deterministic_revalidation_across_hash_seeds(self) -> None:
        observations = {
            "mvhevc-local-full": {
                "picture": "no",
                "depth": "not_sure",
                "eye_order": "no",
                "audio_sync": "not_sure",
            }
        }
        self.assemble(self.private_input(logs=self.clean_logs(), observations=observations))
        command = [
            sys.executable,
            "-c",
            (
                "import json,sys; "
                "from scripts.qualify_visionos_sustained_playback import validate_record; "
                "print(json.dumps(validate_record(__import__('pathlib').Path(sys.argv[1])), sort_keys=True))"
            ),
            str(self.root / "record.json"),
        ]
        outputs = []
        for seed in ("1", "2", "3"):
            environment = {**os.environ, "PYTHONHASHSEED": seed}
            result = subprocess.run(
                command,
                cwd=Path(__file__).resolve().parents[1],
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )
            outputs.append(result.stdout)
        self.assertEqual(len(set(outputs)), 1)

    def test_run_identity_and_log_reuse_fail_closed(self) -> None:
        logs = self.clean_logs()
        logs["mvhevc-local-full"] = self.write_log(
            "wrong-run.jsonl",
            media_id="mv",
            coverage="full_length",
            duration=600,
            run_id="wrong-run",
        )
        record = self.assemble(self.private_input(logs=logs))
        self.assertEqual(record["cells"][0]["reasons"], ["run_id_mismatch"])

        data = self.private_input(logs=self.clean_logs())
        data["cells"][1]["log_path"] = data["cells"][0]["log_path"]
        with self.assertRaisesRegex(qualification.EvidenceError, "reuses a log_path"):
            self.assemble(data)

        data = self.private_input(logs=self.clean_logs())
        data["app_identity"]["build"] = "2"
        record = self.assemble(data)
        self.assertEqual(record["cells"][0]["reasons"], ["app_identity_mismatch"])

        data = self.private_input(logs=self.clean_logs())
        data["device_identity"]["os_build"] = "24M999"
        record = self.assemble(data)
        self.assertEqual(record["cells"][0]["reasons"], ["device_identity_mismatch"])

        logs = self.clean_logs()
        path = logs["mvhevc-local-full"]
        records = [json.loads(line) for line in path.read_text().splitlines()]
        records[0]["device"]["operating_system"] = "Version 27.0.1 (Build 24M0001)"
        path.write_text(
            "\n".join(json.dumps(record, sort_keys=True) for record in records) + "\n",
            encoding="utf-8",
        )
        record = self.assemble(self.private_input(logs=logs))
        self.assertEqual(record["cells"][0]["reasons"], ["device_identity_mismatch"])

    def test_ordered_user_interactions_are_required(self) -> None:
        logs = self.clean_logs()
        path = logs["mvhevc-local-full"]
        records = [json.loads(line) for line in path.read_text().splitlines()]
        for record in records:
            if record.get("event") == "pause_requested":
                record["detail"] = "scene_inactive"
            if record.get("event") in {"seek_started", "seek_completed"}:
                record["detail"] = "resume_restore"
        path.write_text(
            "\n".join(json.dumps(record, sort_keys=True) for record in records) + "\n",
            encoding="utf-8",
        )
        record = self.assemble(self.private_input(logs=logs))
        reasons = record["cells"][0]["reasons"]
        self.assertIn("missing_required_interaction:pause_resume", reasons)
        self.assertIn("missing_required_interaction:user_seek", reasons)

    def test_eye_order_failure_fails_even_after_a_completed_retry(self) -> None:
        logs = self.clean_logs()
        logs["packed-ou-local-sustained"] = self.write_log(
            "eye-order-failure.jsonl",
            media_id="ou",
            coverage="sustained",
            duration=1800,
            extra_events=[
                self.event("eye_order_change_started", 300, "eye_order_change"),
                self.event("eye_order_change_failed", 302, "failed"),
            ],
        )
        record = self.assemble(self.private_input(logs=logs))
        self.assertEqual(record["cells"][3]["disposition"], "product_failed")
        self.assertIn("eye_order_change_failed", record["cells"][3]["reasons"])

    def test_scene_pause_does_not_hide_a_later_stall(self) -> None:
        logs = self.clean_logs()
        logs["mvhevc-local-full"] = self.write_log(
            "scene-then-stall.jsonl",
            media_id="mv",
            coverage="full_length",
            duration=600,
            extra_events=[
                self.event("scene_inactive", 200, "scene_inactive"),
                self.event("pause_requested", 201, "user_pause"),
                self.event("scene_active", 220, "scene_active"),
                self.event("time_control_changed", 221, "playing"),
                self.event("time_control_changed", 400, "playing->waiting"),
                self.event("time_control_changed", 480, "waiting->playing"),
            ],
        )
        record = self.assemble(self.private_input(logs=logs))
        cell = record["cells"][0]
        self.assertEqual(cell["disposition"], "product_failed")
        self.assertEqual(cell["telemetry"]["unplanned_waiting_max_seconds"], 80.0)

    def test_control_overlap_excludes_only_the_overlapping_wait_time(self) -> None:
        logs = self.clean_logs()
        logs["mvhevc-local-full"] = self.write_log(
            "partial-scene-wait.jsonl",
            media_id="mv",
            coverage="full_length",
            duration=600,
            extra_events=[
                self.event("scene_inactive", 200, "scene_inactive"),
                self.event("time_control_changed", 201, "playing->waiting"),
                self.event("scene_active", 220, "scene_active"),
                self.event("time_control_changed", 240, "waiting->playing"),
            ],
        )
        record = self.assemble(self.private_input(logs=logs))
        interval = record["cells"][0]["telemetry"]["waiting_intervals"][0]
        self.assertEqual(interval["excluded_duration_seconds"], 19.0)
        self.assertEqual(interval["unplanned_duration_seconds"], 20.0)
        self.assertEqual(interval["classification"], "partially_excluded_control_interval")

    def test_unterminated_controls_do_not_exclude_later_waiting(self) -> None:
        for start_event in (
            "pause_requested",
            "seek_started",
            "eye_order_change_started",
            "scene_inactive",
        ):
            with self.subTest(start_event=start_event):
                intervals = qualification._waiting_intervals(
                    [
                        self.event(start_event, 200),
                        self.event("time_control_changed", 300, "playing->waiting"),
                    ],
                    600,
                )
                self.assertEqual(intervals[0]["excluded_duration_seconds"], 0.0)
                self.assertEqual(intervals[0]["unplanned_duration_seconds"], 300.0)
                self.assertEqual(intervals[0]["classification"], "unplanned_waiting")

    def test_sustained_playing_time_and_clock_span_fail_closed(self) -> None:
        logs = self.clean_logs()
        logs["mvhevc-files-provider-sustained"] = self.write_log(
            "mostly-paused.jsonl",
            media_id="mv-provider",
            coverage="sustained",
            duration=1800,
            extra_events=[self.event("time_control_changed", 100, "paused")],
        )
        record = self.assemble(self.private_input(logs=logs))
        self.assertIn("insufficient_active_playback", record["cells"][2]["reasons"])

        logs = self.clean_logs()
        path = logs["mvhevc-files-provider-sustained"]
        records = [json.loads(line) for line in path.read_text().splitlines()]
        records[-1]["captured_at"] = self.stamp(18_000)
        path.write_text(
            "\n".join(json.dumps(item, sort_keys=True) for item in records) + "\n",
            encoding="utf-8",
        )
        record = self.assemble(self.private_input(logs=logs))
        self.assertIn("clock_span_mismatch", record["cells"][2]["reasons"])

    def test_sustained_playing_status_requires_sampled_media_progress(self) -> None:
        logs = self.clean_logs()
        samples = [
            self.sample(
                1800 * index / 7,
                duration=1800,
                player_time=100,
            )
            for index in range(8)
        ]
        logs["mvhevc-files-provider-sustained"] = self.write_log(
            "wedged-playing.jsonl",
            media_id="mv-provider",
            coverage="sustained",
            duration=1800,
            samples=samples,
        )
        record = self.assemble(self.private_input(logs=logs))
        self.assertIn("insufficient_media_progress", record["cells"][2]["reasons"])

    def test_tamper_detection_covers_telemetry_disposition_and_hash(self) -> None:
        record = self.assemble(self.private_input(logs=self.clean_logs()))
        record["cells"][0]["telemetry"]["sample_count"] += 1
        (self.root / "record.json").write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaises(qualification.EvidenceError):
            qualification.validate_record(self.root / "record.json")
        record = self.assemble(self.private_input(logs=self.clean_logs()))
        record["cells"][0]["disposition"] = "product_failed"
        (self.root / "record.json").write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaises(qualification.EvidenceError):
            qualification.validate_record(self.root / "record.json")
        record = self.assemble(self.private_input(logs=self.clean_logs()))
        record["record_sha256"] = SHA_B
        (self.root / "record.json").write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaises(qualification.EvidenceError):
            qualification.validate_record(self.root / "record.json")

    def test_malformed_assembled_record_remains_independently_verifiable(self) -> None:
        logs = self.clean_logs()
        bad_log = self.root / "malformed.jsonl"
        bad_log.write_text("not-json\n", encoding="utf-8")
        logs["mvhevc-local-full"] = bad_log
        record = self.assemble(self.private_input(logs=logs))
        self.assertEqual(record["cells"][0]["disposition"], "evidence_failed")
        self.assertFalse(record["matrix"]["accepted"])
        self.assertFalse(qualification.validate_record(self.root / "record.json")["matrix"]["accepted"])

    def test_malformed_embedded_arrays_raise_evidence_error(self) -> None:
        record = self.assemble(self.private_input(logs=self.clean_logs()))
        record["cells"][0]["evidence_summary"]["samples"][0] = 1
        record["record_sha256"] = qualification._canonical_hash(
            {key: value for key, value in record.items() if key != "record_sha256"}
        )
        (self.root / "record.json").write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaises(qualification.EvidenceError):
            qualification.validate_record(self.root / "record.json")

    def test_cli_returns_nonzero_for_a_rejected_matrix(self) -> None:
        observations = {
            "mvhevc-local-full": {
                "picture": "no",
                "depth": "yes",
                "eye_order": "yes",
                "audio_sync": "yes",
            }
        }
        input_path = self.root / "private-input.json"
        record_path = self.root / "record.json"
        input_path.write_text(
            json.dumps(self.private_input(logs=self.clean_logs(), observations=observations)),
            encoding="utf-8",
        )
        with redirect_stdout(io.StringIO()):
            assemble_exit = qualification.main(
                [
                    "assemble",
                    "--input",
                    str(input_path),
                    "--output",
                    str(record_path),
                ]
            )
            validate_exit = qualification.main(["validate-record", "--record", str(record_path), "--json"])
        self.assertEqual(assemble_exit, 2)
        self.assertEqual(validate_exit, 2)


MIB = 1024 * 1024


if __name__ == "__main__":
    unittest.main()
