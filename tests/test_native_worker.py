import io
import json
import os
import signal
import stat
import subprocess
import tempfile
import time
import unittest

from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4

from bd_to_avp.modules.config import Stage, config
from bd_to_avp.modules.audio_mode import AudioMode
from bd_to_avp.modules.disc import DiscInfo, DiscTitleInfo, MKVCreationError
from bd_to_avp.modules.process import process_each
from bd_to_avp.modules.sub import SRTCreationError
from bd_to_avp.modules.video_mode import VideoMode
from bd_to_avp.worker.__main__ import run_worker
from bd_to_avp.worker.operations import (
    WorkerDecisionRequired,
    WorkerOperationError,
    configured_conversion,
    convert_source,
    inspect_source,
    preview_source,
)
from bd_to_avp.worker.ownership import WorkerCancelled, WorkerProcessOwner
from bd_to_avp.worker.protocol import (
    JobSource,
    MAX_EVENT_BYTES,
    MAX_REQUEST_BYTES,
    MAX_DETAIL_BYTES,
    PROTOCOL_VERSION,
    WorkerActivityReporter,
    JobSpec,
    WorkerEventEmitter,
    WorkerEventType,
    WorkerOperation,
    PreviewPosition,
    WorkerProtocolError,
    WorkerSourceKind,
)


def source_kind_for_path(source_path: Path) -> str:
    if source_path.suffix.lower() == ".iso":
        return WorkerSourceKind.DISC_IMAGE.value
    if source_path.is_dir():
        return WorkerSourceKind.BLU_RAY_FOLDER.value
    return WorkerSourceKind.DIRECT_FILE.value


def request_line(source_path: Path, *, source_kind: str | None = None, **overrides: object) -> str:
    request: dict[str, object] = {
        "protocol_version": PROTOCOL_VERSION,
        "type": "job.start",
        "job_id": str(uuid4()),
        "operation": "inspect_source",
        "source": {"kind": source_kind or source_kind_for_path(source_path), "path": str(source_path)},
    }
    request.update(overrides)
    return json.dumps(request) + "\n"


def conversion_request_line(
    source_path: Path,
    destination_path: Path,
    *,
    source_kind: str | None = None,
    **overrides: object,
) -> str:
    resolved_source_kind = source_kind or source_kind_for_path(source_path)
    source: dict[str, object] = {"kind": resolved_source_kind, "path": str(source_path)}
    if resolved_source_kind != WorkerSourceKind.DIRECT_FILE.value:
        source["title_id"] = "makemkv:0"
    request: dict[str, object] = {
        "protocol_version": PROTOCOL_VERSION,
        "type": "job.start",
        "job_id": str(uuid4()),
        "operation": "convert_source",
        "source": source,
        "destination": {"path": str(destination_path)},
        "encoding": {
            "audio": {"mode": "convert_aac", "bitrate": 384},
            "video_mode": "mv_hevc",
            "av1_crf": 32,
            "left_right_bitrate": 20,
            "link_quality": True,
            "mv_hevc_quality": 75,
            "upscale_quality": 75,
            "fov": 90,
            "frame_rate": "",
            "resolution": "",
            "crop_black_bars": False,
            "swap_eyes": False,
            "fx_upscale": False,
            "subtitles": {
                "mode": "preferred_plus_others",
                "preferred_language": "eng",
            },
        },
        "job": {
            "start_stage": 1,
            "keep_files": False,
            "overwrite": False,
            "remove_original": False,
            "continue_on_error": False,
            "software_encoder": False,
            "output_commands": False,
            "keep_awake": False,
        },
    }
    request.update(overrides)
    return json.dumps(request) + "\n"


def preview_request_line(
    source_path: Path,
    destination_path: Path,
    *,
    position: str = "middle",
    duration_seconds: int = 60,
    **overrides: object,
) -> str:
    request = json.loads(conversion_request_line(source_path, destination_path))
    request["operation"] = "preview_source"
    request["job"]["overwrite"] = True
    request["preview"] = {
        "parent_job_id": str(uuid4()),
        "position": position,
        "duration_seconds": duration_seconds,
    }
    request.update(overrides)
    return json.dumps(request) + "\n"


def decoded_events(output: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in output.getvalue().splitlines()]


class JobSpecTests(unittest.TestCase):
    def test_parses_valid_request(self) -> None:
        source_path = Path("/tmp/movie.m2ts")
        job = JobSpec.from_json_line(request_line(source_path))

        self.assertEqual(job.protocol_version, PROTOCOL_VERSION)
        self.assertEqual(job.operation.value, "inspect_source")
        self.assertEqual(job.source.kind, WorkerSourceKind.DIRECT_FILE)
        self.assertEqual(job.source.path, source_path)

    def test_parses_physical_disc_request(self) -> None:
        source_path = Path("/dev/disk9")

        job = JobSpec.from_json_line(request_line(source_path, source_kind=WorkerSourceKind.PHYSICAL_DISC.value))

        self.assertEqual(job.source.kind, WorkerSourceKind.PHYSICAL_DISC)
        self.assertEqual(job.source.path, source_path)

    def test_parses_strict_conversion_request(self) -> None:
        source_path = Path("/tmp/movie.mkv")
        destination_path = Path("/tmp/output")

        job = JobSpec.from_json_line(conversion_request_line(source_path, destination_path))

        self.assertEqual(job.operation.value, "convert_source")
        self.assertEqual(job.source.path, source_path)
        self.assertEqual(job.destination.path if job.destination else None, destination_path)
        self.assertEqual(job.encoding.audio.mode if job.encoding else None, AudioMode.CONVERT_AAC)
        self.assertEqual(job.encoding.audio.bitrate if job.encoding else None, 384)
        self.assertEqual(job.encoding.subtitles.mode.value if job.encoding else None, "preferred_plus_others")
        self.assertEqual(job.encoding.subtitles.preferred_language if job.encoding else None, "eng")
        self.assertEqual(job.job.start_stage if job.job else None, 1)

    def test_parses_opaque_title_id_for_disc_conversion(self) -> None:
        source_path = Path("/tmp/movie.iso")
        destination_path = Path("/tmp/output")
        request = json.loads(conversion_request_line(source_path, destination_path))
        request["source"]["title_id"] = "provider:playlist-01005"

        job = JobSpec.from_json_line(json.dumps(request))

        self.assertEqual(job.source.title_id, "provider:playlist-01005")

    def test_requires_title_id_for_disc_conversion(self) -> None:
        request = json.loads(conversion_request_line(Path("/tmp/movie.iso"), Path("/tmp/output")))
        request["source"].pop("title_id")

        with self.assertRaises(WorkerProtocolError) as context:
            JobSpec.from_json_line(json.dumps(request))

        self.assertEqual(context.exception.code, "invalid_title_selection")

    def test_rejects_title_id_for_direct_file(self) -> None:
        request = json.loads(conversion_request_line(Path("/tmp/movie.mkv"), Path("/tmp/output")))
        request["source"]["title_id"] = "makemkv:0"

        with self.assertRaises(WorkerProtocolError) as context:
            JobSpec.from_json_line(json.dumps(request))

        self.assertEqual(context.exception.code, "invalid_title_selection")

    def test_rejects_null_title_id_for_direct_file(self) -> None:
        request = json.loads(conversion_request_line(Path("/tmp/movie.mkv"), Path("/tmp/output")))
        request["source"]["title_id"] = None

        with self.assertRaises(WorkerProtocolError) as context:
            JobSpec.from_json_line(json.dumps(request))

        self.assertEqual(context.exception.code, "invalid_title_selection")

    def test_rejects_title_id_for_inspection(self) -> None:
        request = json.loads(request_line(Path("/tmp/movie.iso")))
        request["source"]["title_id"] = "makemkv:0"

        with self.assertRaises(WorkerProtocolError) as context:
            JobSpec.from_json_line(json.dumps(request))

        self.assertEqual(context.exception.code, "invalid_source")

    def test_rejects_null_title_id_for_inspection(self) -> None:
        request = json.loads(request_line(Path("/tmp/movie.iso")))
        request["source"]["title_id"] = None

        with self.assertRaises(WorkerProtocolError) as context:
            JobSpec.from_json_line(json.dumps(request))

        self.assertEqual(context.exception.code, "invalid_source")

    def test_parses_shared_swift_conversion_fixture(self) -> None:
        fixture_path = Path(__file__).parent / "fixtures" / "native_worker_convert_v7.json"

        job = JobSpec.from_json_line(fixture_path.read_text(encoding="utf-8"))

        self.assertEqual(job.operation.value, "convert_source")
        self.assertEqual(job.source.kind, WorkerSourceKind.DIRECT_FILE)
        self.assertEqual(job.source.path, Path("/tmp/movie.mkv"))
        self.assertEqual(job.destination.path if job.destination else None, Path("/tmp/output"))
        self.assertEqual(job.encoding.mv_hevc_quality if job.encoding else None, 75)
        self.assertEqual(job.encoding.video_mode if job.encoding else None, VideoMode.MV_HEVC)
        self.assertEqual(job.encoding.av1_crf if job.encoding else None, 32)

    def test_rejects_av1_export_with_fx_upscale(self) -> None:
        request = json.loads(conversion_request_line(Path("/tmp/movie.mkv"), Path("/tmp/output")))
        request["encoding"]["video_mode"] = "av1_sbs"
        request["encoding"]["fx_upscale"] = True

        with self.assertRaisesRegex(WorkerProtocolError, "does not support AI FX upscale"):
            JobSpec.from_json_line(json.dumps(request))

    def test_rejects_av1_export_with_resolution_override(self) -> None:
        request = json.loads(conversion_request_line(Path("/tmp/movie.mkv"), Path("/tmp/output")))
        request["encoding"]["video_mode"] = "av1_sbs"
        request["encoding"]["resolution"] = "3840x2160"

        with self.assertRaisesRegex(WorkerProtocolError, "full source resolution"):
            JobSpec.from_json_line(json.dumps(request))

    def test_parses_shared_swift_physical_disc_fixture(self) -> None:
        fixture_path = Path(__file__).parent / "fixtures" / "native_worker_convert_physical_disc_v7.json"

        job = JobSpec.from_json_line(fixture_path.read_text(encoding="utf-8"))

        self.assertEqual(job.source.kind, WorkerSourceKind.PHYSICAL_DISC)
        self.assertEqual(job.source.path, Path("/dev/disk9"))
        self.assertEqual(job.source.title_id, "makemkv:0")
        self.assertFalse(job.job.remove_original if job.job else True)

    def test_parses_shared_swift_preview_fixture(self) -> None:
        fixture_path = Path(__file__).parent / "fixtures" / "native_worker_preview_v7.json"

        job = JobSpec.from_json_line(fixture_path.read_text(encoding="utf-8"))

        self.assertEqual(job.operation, WorkerOperation.PREVIEW_SOURCE)
        self.assertEqual(job.preview.position if job.preview else None, PreviewPosition.MIDDLE)
        self.assertEqual(job.preview.duration_seconds if job.preview else None, 60)

    def test_rejects_remove_original_for_physical_disc(self) -> None:
        request = json.loads(
            conversion_request_line(
                Path("/dev/disk9"),
                Path("/tmp/output"),
                source_kind=WorkerSourceKind.PHYSICAL_DISC.value,
            )
        )
        request["job"]["remove_original"] = True

        with self.assertRaises(WorkerProtocolError) as context:
            JobSpec.from_json_line(json.dumps(request))

        self.assertEqual(context.exception.code, "invalid_job_options")
        self.assertIn("physical discs", context.exception.message)

    def test_parses_preview_child_job(self) -> None:
        request = preview_request_line(Path("/tmp/movie.mkv"), Path("/tmp/output"))

        job = JobSpec.from_json_line(request)

        self.assertEqual(job.operation.value, "preview_source")
        self.assertEqual(job.preview.position if job.preview else None, PreviewPosition.MIDDLE)
        self.assertEqual(job.preview.duration_seconds if job.preview else None, 60)

    def test_rejects_preview_that_can_modify_source(self) -> None:
        request = json.loads(preview_request_line(Path("/tmp/movie.mkv"), Path("/tmp/output")))
        request["job"]["remove_original"] = True

        with self.assertRaises(WorkerProtocolError) as context:
            JobSpec.from_json_line(json.dumps(request) + "\n")

        self.assertEqual(context.exception.code, "invalid_preview_options")

    def test_rejects_preview_for_bluray_folder(self) -> None:
        request = preview_request_line(
            Path("/tmp/Disc"),
            Path("/tmp/output"),
            source={"kind": "blu_ray_folder", "path": "/tmp/Disc"},
        )

        with self.assertRaises(WorkerProtocolError) as context:
            JobSpec.from_json_line(request)

        self.assertEqual(context.exception.code, "invalid_preview_source")

    def test_rejects_preview_with_unknown_position(self) -> None:
        with self.assertRaises(WorkerProtocolError) as context:
            JobSpec.from_json_line(
                preview_request_line(Path("/tmp/movie.mkv"), Path("/tmp/output"), position="credits")
            )

        self.assertEqual(context.exception.code, "invalid_preview_options")

    def test_accepts_zero_field_of_view_used_by_existing_ui(self) -> None:
        request = json.loads(conversion_request_line(Path("/tmp/movie.mkv"), Path("/tmp/output")))
        request["encoding"]["fov"] = 0

        job = JobSpec.from_json_line(json.dumps(request) + "\n")

        self.assertEqual(job.encoding.fov if job.encoding else None, 0)

    def test_rejects_relative_conversion_destination_path(self) -> None:
        with self.assertRaises(WorkerProtocolError) as context:
            JobSpec.from_json_line(conversion_request_line(Path("/tmp/movie.mkv"), Path("output")))

        self.assertEqual(context.exception.code, "destination_not_absolute")

    def test_rejects_unknown_conversion_option(self) -> None:
        request = json.loads(conversion_request_line(Path("/tmp/movie.mkv"), Path("/tmp/output")))
        request["encoding"]["surprise"] = True

        with self.assertRaises(WorkerProtocolError) as context:
            JobSpec.from_json_line(json.dumps(request) + "\n")

        self.assertEqual(context.exception.code, "invalid_request")

    def test_rejects_legacy_audio_fields_in_protocol_v6(self) -> None:
        request = json.loads(conversion_request_line(Path("/tmp/movie.mkv"), Path("/tmp/output")))
        request["encoding"]["transcode_audio"] = False
        request["encoding"]["audio_bitrate"] = 384

        with self.assertRaises(WorkerProtocolError) as context:
            JobSpec.from_json_line(json.dumps(request) + "\n")

        self.assertEqual(context.exception.code, "invalid_request")

    def test_rejects_unknown_audio_mode(self) -> None:
        request = json.loads(conversion_request_line(Path("/tmp/movie.mkv"), Path("/tmp/output")))
        request["encoding"]["audio"]["mode"] = "keep_original"

        with self.assertRaises(WorkerProtocolError) as context:
            JobSpec.from_json_line(json.dumps(request) + "\n")

        self.assertEqual(context.exception.code, "invalid_encoding_options")

    def test_rejects_unknown_nested_audio_field_with_stable_error_code(self) -> None:
        request = json.loads(conversion_request_line(Path("/tmp/movie.mkv"), Path("/tmp/output")))
        request["encoding"]["audio"]["surprise"] = True

        with self.assertRaises(WorkerProtocolError) as context:
            JobSpec.from_json_line(json.dumps(request) + "\n")

        self.assertEqual(context.exception.code, "invalid_encoding_options")

    def test_normalizes_subtitle_language_aliases(self) -> None:
        request = json.loads(conversion_request_line(Path("/tmp/movie.mkv"), Path("/tmp/output")))
        request["encoding"]["subtitles"] = {
            "mode": "preferred_only",
            "preferred_language": "dut",
        }

        job = JobSpec.from_json_line(json.dumps(request) + "\n")

        self.assertEqual(job.encoding.subtitles.preferred_language if job.encoding else None, "nld")

    def test_requires_null_language_when_subtitles_are_off(self) -> None:
        request = json.loads(conversion_request_line(Path("/tmp/movie.mkv"), Path("/tmp/output")))
        request["encoding"]["subtitles"] = {
            "mode": "off",
            "preferred_language": "eng",
        }

        with self.assertRaises(WorkerProtocolError) as context:
            JobSpec.from_json_line(json.dumps(request) + "\n")

        self.assertEqual(context.exception.code, "invalid_encoding_options")

    def test_rejects_invalid_preferred_subtitle_language(self) -> None:
        request = json.loads(conversion_request_line(Path("/tmp/movie.mkv"), Path("/tmp/output")))
        request["encoding"]["subtitles"] = {
            "mode": "preferred_only",
            "preferred_language": "xyz",
        }

        with self.assertRaises(WorkerProtocolError) as context:
            JobSpec.from_json_line(json.dumps(request) + "\n")

        self.assertEqual(context.exception.code, "invalid_encoding_options")

    def test_rejects_unknown_subtitle_mode(self) -> None:
        request = json.loads(conversion_request_line(Path("/tmp/movie.mkv"), Path("/tmp/output")))
        request["encoding"]["subtitles"] = {
            "mode": "forced_only",
            "preferred_language": "eng",
        }

        with self.assertRaises(WorkerProtocolError) as context:
            JobSpec.from_json_line(json.dumps(request) + "\n")

        self.assertEqual(context.exception.code, "invalid_encoding_options")

    def test_rejects_wrong_type_subtitle_mode_with_stable_error_code(self) -> None:
        request = json.loads(conversion_request_line(Path("/tmp/movie.mkv"), Path("/tmp/output")))
        request["encoding"]["subtitles"]["mode"] = 1

        with self.assertRaises(WorkerProtocolError) as context:
            JobSpec.from_json_line(json.dumps(request) + "\n")

        self.assertEqual(context.exception.code, "invalid_encoding_options")

    def test_rejects_missing_nested_subtitle_fields_with_stable_error_code(self) -> None:
        for missing_field in ("mode", "preferred_language"):
            with self.subTest(missing_field=missing_field):
                request = json.loads(conversion_request_line(Path("/tmp/movie.mkv"), Path("/tmp/output")))
                del request["encoding"]["subtitles"][missing_field]

                with self.assertRaises(WorkerProtocolError) as context:
                    JobSpec.from_json_line(json.dumps(request) + "\n")

                self.assertEqual(context.exception.code, "invalid_encoding_options")

    def test_rejects_unknown_nested_subtitle_field_with_stable_error_code(self) -> None:
        request = json.loads(conversion_request_line(Path("/tmp/movie.mkv"), Path("/tmp/output")))
        request["encoding"]["subtitles"]["surprise"] = True

        with self.assertRaises(WorkerProtocolError) as context:
            JobSpec.from_json_line(json.dumps(request) + "\n")

        self.assertEqual(context.exception.code, "invalid_encoding_options")

    def test_rejects_legacy_subtitle_fields_in_protocol_v5(self) -> None:
        request = json.loads(conversion_request_line(Path("/tmp/movie.mkv"), Path("/tmp/output")))
        request["encoding"]["skip_subtitles"] = False

        with self.assertRaises(WorkerProtocolError) as context:
            JobSpec.from_json_line(json.dumps(request) + "\n")

        self.assertEqual(context.exception.code, "invalid_request")

    def test_rejects_missing_conversion_options(self) -> None:
        request = json.loads(conversion_request_line(Path("/tmp/movie.mkv"), Path("/tmp/output")))
        del request["job"]["overwrite"]

        with self.assertRaises(WorkerProtocolError) as context:
            JobSpec.from_json_line(json.dumps(request) + "\n")

        self.assertEqual(context.exception.code, "invalid_job_options")

    def test_rejects_protocol_mismatch_with_job_id(self) -> None:
        job_id = str(uuid4())
        with self.assertRaises(WorkerProtocolError) as context:
            JobSpec.from_json_line(
                request_line(
                    Path("/tmp/movie.m2ts"),
                    protocol_version=PROTOCOL_VERSION + 1,
                    job_id=job_id,
                )
            )

        self.assertEqual(context.exception.code, "protocol_mismatch")
        self.assertEqual(context.exception.job_id, job_id)

    def test_rejects_boolean_protocol_version(self) -> None:
        with self.assertRaises(WorkerProtocolError) as context:
            JobSpec.from_json_line(request_line(Path("/tmp/movie.m2ts"), protocol_version=True))

        self.assertEqual(context.exception.code, "protocol_mismatch")

    def test_rejects_relative_source_path(self) -> None:
        with self.assertRaises(WorkerProtocolError) as context:
            JobSpec.from_json_line(request_line(Path("movie.m2ts")))

        self.assertEqual(context.exception.code, "source_not_absolute")

    def test_rejects_missing_source_kind(self) -> None:
        request = json.loads(request_line(Path("/tmp/movie.m2ts")))
        del request["source"]["kind"]

        with self.assertRaises(WorkerProtocolError) as context:
            JobSpec.from_json_line(json.dumps(request) + "\n")

        self.assertEqual(context.exception.code, "invalid_source")

    def test_rejects_unknown_source_kind(self) -> None:
        with self.assertRaises(WorkerProtocolError) as context:
            JobSpec.from_json_line(request_line(Path("/tmp/movie.m2ts"), source_kind="optical_disc"))

        self.assertEqual(context.exception.code, "invalid_source")

    def test_rejects_oversized_request(self) -> None:
        oversized = "{" + (" " * MAX_REQUEST_BYTES) + "}"
        with self.assertRaises(WorkerProtocolError) as context:
            JobSpec.from_json_line(oversized)

        self.assertEqual(context.exception.code, "request_too_large")


class WorkerEventEmitterTests(unittest.TestCase):
    def test_sequences_events_and_stops_after_terminal(self) -> None:
        output = io.StringIO()
        emitter = WorkerEventEmitter(output, str(uuid4()))

        emitter.emit(WorkerEventType.WORKER_READY)
        emitter.emit(WorkerEventType.JOB_COMPLETED, {"result": {"name": "movie"}})

        events = decoded_events(output)
        self.assertEqual([event["sequence"] for event in events], [0, 1])
        self.assertTrue(emitter.terminal_emitted)
        with self.assertRaises(RuntimeError):
            emitter.emit(WorkerEventType.HEARTBEAT)

    def test_oversized_event_does_not_consume_sequence(self) -> None:
        output = io.StringIO()
        emitter = WorkerEventEmitter(output, str(uuid4()))
        emitter.emit(WorkerEventType.WORKER_READY)

        with self.assertRaisesRegex(RuntimeError, "size limit"):
            emitter.emit(WorkerEventType.LOG, {"message": "x" * MAX_EVENT_BYTES})
        emitter.fail("internal_error", "The event could not be encoded.")

        events = decoded_events(output)
        self.assertEqual([event["sequence"] for event in events], [0, 1])
        self.assertEqual(events[-1]["type"], "job.failed")


class WorkerActivityReporterTests(unittest.TestCase):
    def test_warning_emits_shared_automatic_fallback_fixture(self) -> None:
        output = io.StringIO()
        job_id = "11111111-1111-4111-8111-111111111111"
        activity = WorkerActivityReporter(WorkerEventEmitter(output, job_id))

        activity.warning(
            "Automatic audio selected AAC conversion because one or more selected tracks are not qualified AAC.",
            stage="transcode_audio",
            code="audio_automatic_fallback_to_aac",
            audio_mode="automatic",
            action="convert_aac",
            source_codecs=["aac", "ac3"],
            unqualified_streams=[
                {
                    "index": 1,
                    "codec": "ac3",
                    "profile": None,
                    "sample_rate": 48_000,
                    "channels": 6,
                    "channel_layout": "5.1(side)",
                    "reason": "codec_not_allowed",
                }
            ],
        )

        fixture_path = Path(__file__).parent / "fixtures" / "native_worker_audio_fallback_warning_v7.json"
        expected = json.loads(fixture_path.read_text())
        self.assertEqual(decoded_events(output), [expected])

    def test_stage_plan_emits_shared_progress_fixture(self) -> None:
        output = io.StringIO()
        job_id = "11111111-1111-4111-8111-111111111111"
        activity = WorkerActivityReporter(WorkerEventEmitter(output, job_id))
        activity.set_stage_plan(("configure", "create_mkv"))

        activity.stage_started("configure", "Preparing conversion settings")

        fixture_path = Path(__file__).parent / "fixtures" / "native_worker_stage_started_progress_v7.json"
        expected = json.loads(fixture_path.read_text())
        self.assertEqual(decoded_events(output), [expected])

    def test_heartbeat_carries_current_stage_fraction(self) -> None:
        output = io.StringIO()
        activity = WorkerActivityReporter(WorkerEventEmitter(output, str(uuid4())))
        activity.set_stage_plan(("configure", "create_mkv"))
        activity.stage_started("configure", "Preparing conversion settings")
        activity.stage_progress(25, 100)

        payload = activity.heartbeat_payload(12)

        self.assertEqual(payload["elapsed_seconds"], 12)
        self.assertEqual(
            payload["progress"],
            {"current_stage": 1, "total_stages": 2, "stage_fraction": 0.25},
        )

        activity.emit_heartbeat(13)
        events = decoded_events(output)
        self.assertEqual([event["type"] for event in events], ["stage.started", "heartbeat"])
        self.assertEqual(events[-1]["payload"]["progress"], payload["progress"])

    def test_new_stage_resets_fraction_and_plan_mismatch_disables_progress(self) -> None:
        output = io.StringIO()
        activity = WorkerActivityReporter(WorkerEventEmitter(output, str(uuid4())))
        activity.set_stage_plan(("configure", "create_mkv"))
        activity.stage_started("configure", "Preparing conversion settings")
        activity.stage_progress(120, 100)
        activity.stage_started("create_mkv", "Preparing source video")
        activity.stage_started("unexpected", "Unexpected stage")

        events = decoded_events(output)
        self.assertEqual(events[1]["payload"]["progress"], {"current_stage": 2, "total_stages": 2})
        self.assertNotIn("progress", events[2]["payload"])
        self.assertNotIn("progress", activity.heartbeat_payload(13))


class WorkerRuntimeTests(unittest.TestCase):
    def test_success_emits_structured_terminal_event_and_redirects_prints(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source_path = Path(temporary_directory) / "movie.m2ts"
            source_path.touch()
            output = io.StringIO()
            diagnostics = io.StringIO()

            def operation(
                job: JobSpec,
                _owner: WorkerProcessOwner,
                activity: WorkerActivityReporter,
            ) -> dict[str, object]:
                print("legacy output")
                self.assertEqual(job.source.path, source_path)
                activity.log("structured progress")
                time.sleep(0.02)
                return {"name": "movie", "resolution": "1920x1080"}

            exit_code = run_worker(
                io.StringIO(request_line(source_path)),
                output,
                diagnostics,
                establish_session=False,
                heartbeat_interval=0.005,
                operation_runner=operation,
            )

        events = decoded_events(output)
        self.assertEqual(exit_code, 0)
        self.assertEqual(events[0]["type"], "worker.ready")
        self.assertEqual(events[-1]["type"], "job.completed")
        self.assertIn("heartbeat", [event["type"] for event in events])
        self.assertIn("log", [event["type"] for event in events])
        self.assertNotIn("legacy output", output.getvalue())
        self.assertIn("legacy output", diagnostics.getvalue())

    def test_cancellation_wins_over_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source_path = Path(temporary_directory) / "movie.m2ts"
            source_path.touch()
            output = io.StringIO()

            def operation(
                _job: JobSpec,
                owner: WorkerProcessOwner,
                _activity: WorkerActivityReporter,
            ) -> dict[str, object]:
                owner.request_cancel()
                owner.check_cancelled()
                return {"name": "unreachable"}

            exit_code = run_worker(
                io.StringIO(request_line(source_path)),
                output,
                io.StringIO(),
                establish_session=False,
                operation_runner=operation,
            )

        events = decoded_events(output)
        self.assertEqual(exit_code, 130)
        self.assertEqual(events[-1]["type"], "job.cancelled")
        self.assertNotIn("job.completed", [event["type"] for event in events])

    def test_malformed_request_emits_stable_failure(self) -> None:
        output = io.StringIO()

        exit_code = run_worker(io.StringIO("not-json\n"), output, io.StringIO(), establish_session=False)

        events = decoded_events(output)
        self.assertEqual(exit_code, 2)
        self.assertEqual(events[-1]["type"], "job.failed")
        self.assertEqual(events[-1]["payload"]["error"]["code"], "invalid_json")

    def test_oversized_completion_falls_back_to_contiguous_failure(self) -> None:
        source_path = Path("/tmp/movie.m2ts")

        exit_code = run_worker(
            io.StringIO(request_line(source_path)),
            io_output := io.StringIO(),
            io.StringIO(),
            establish_session=False,
            operation_runner=lambda _job, _owner, _activity: {"blob": "x" * MAX_EVENT_BYTES},
        )

        events = decoded_events(io_output)
        self.assertEqual(exit_code, 1)
        self.assertEqual([event["sequence"] for event in events], list(range(len(events))))
        self.assertEqual(events[-1]["type"], "job.failed")
        self.assertEqual(events[-1]["payload"]["error"]["code"], "internal_error")

    def test_emits_shared_swift_conversion_completion_fixture(self) -> None:
        output = io.StringIO()
        emitter = WorkerEventEmitter(output, "11111111-1111-4111-8111-111111111111")
        emitter.emit(WorkerEventType.WORKER_READY)
        emitter.emit(WorkerEventType.JOB_STARTED, {"operation": "convert_source"})
        emitter.emit(
            WorkerEventType.JOB_COMPLETED,
            {
                "conversion_result": {
                    "source_path": "/tmp/movie.mkv",
                    "destination_path": "/tmp/output",
                    "output_path": "/tmp/output/movie_AVP.mov",
                    "size_bytes": 1024,
                }
            },
        )
        fixture_path = Path(__file__).parent / "fixtures" / "native_worker_conversion_completed_v7.json"
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))

        self.assertEqual(decoded_events(output)[-1], fixture)

    def test_preview_emits_artifact_before_completion(self) -> None:
        source_path = Path("/tmp/movie.mkv")
        destination_path = Path("/tmp/previews/job")

        def operation(
            _job: JobSpec,
            _owner: WorkerProcessOwner,
            activity: WorkerActivityReporter,
        ) -> dict[str, object]:
            artifact = {
                "output_path": str(destination_path / "movie_AVP.mov"),
                "duration_seconds": 60,
            }
            activity.artifact_ready(artifact)
            return artifact

        exit_code = run_worker(
            io.StringIO(preview_request_line(source_path, destination_path)),
            output := io.StringIO(),
            io.StringIO(),
            establish_session=False,
            operation_runner=operation,
        )

        events = decoded_events(output)
        self.assertEqual(exit_code, 0)
        self.assertEqual(events[-2]["type"], "artifact.ready")
        self.assertEqual(events[-1]["type"], "job.completed")
        self.assertEqual(events[-1]["payload"]["preview_result"]["duration_seconds"], 60)

    def test_oversized_error_details_are_truncated_and_terminal(self) -> None:
        source_path = Path("/tmp/movie.m2ts")

        def operation(
            _job: JobSpec,
            _owner: WorkerProcessOwner,
            _activity: WorkerActivityReporter,
        ) -> dict[str, object]:
            raise WorkerOperationError("tool_failed", "A conversion helper failed.", "x" * MAX_EVENT_BYTES)

        exit_code = run_worker(
            io.StringIO(request_line(source_path)),
            output := io.StringIO(),
            io.StringIO(),
            establish_session=False,
            operation_runner=operation,
        )

        events = decoded_events(output)
        details = events[-1]["payload"]["error"]["details"]
        self.assertEqual(exit_code, 1)
        self.assertEqual(events[-1]["type"], "job.failed")
        self.assertLessEqual(len(details.encode("utf-8")), MAX_DETAIL_BYTES)
        self.assertTrue(details.endswith("details truncated"))

    def test_decision_required_is_terminal(self) -> None:
        source_path = Path("/tmp/movie.m2ts")

        def operation(
            _job: JobSpec,
            _owner: WorkerProcessOwner,
            _activity: WorkerActivityReporter,
        ) -> dict[str, object]:
            from bd_to_avp.worker.operations import WorkerDecisionRequired

            raise WorkerDecisionRequired("subtitle_decision_required", "Choose how to continue.", "details")

        exit_code = run_worker(
            io.StringIO(request_line(source_path)),
            io_output := io.StringIO(),
            io.StringIO(),
            establish_session=False,
            operation_runner=operation,
        )

        events = decoded_events(io_output)
        self.assertEqual(exit_code, 3)
        self.assertEqual(events[-1]["type"], "job.decision_required")
        self.assertEqual(events[-1]["payload"]["decision"]["id"], "subtitle_decision_required")
        self.assertEqual(events[-1]["payload"]["decision"]["prompt"], "Choose how to continue.")
        self.assertEqual(events[-1]["payload"]["decision"]["details"], "details")

    def test_conversion_completion_uses_conversion_result_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            source_path = temporary_path / "movie.mkv"
            source_path.touch()
            output = io.StringIO()

            def operation(
                _job: JobSpec,
                _owner: WorkerProcessOwner,
                _activity: WorkerActivityReporter,
            ) -> dict[str, object]:
                return {"output_path": "/tmp/Movie_AVP.mov", "size_bytes": 10}

            exit_code = run_worker(
                io.StringIO(conversion_request_line(source_path, temporary_path / "output")),
                output,
                io.StringIO(),
                establish_session=False,
                operation_runner=operation,
            )

        events = decoded_events(output)
        self.assertEqual(exit_code, 0)
        self.assertEqual(events[-1]["type"], "job.completed")
        self.assertEqual(events[-1]["payload"]["conversion_result"]["output_path"], "/tmp/Movie_AVP.mov")
        self.assertNotIn("result", events[-1]["payload"])


class SourceInspectionTests(unittest.TestCase):
    def test_inspects_direct_video_sources_with_production_probe_path(self) -> None:
        for extension in (".m2ts", ".mkv"):
            with self.subTest(extension=extension), tempfile.TemporaryDirectory() as temporary_directory:
                temporary_path = Path(temporary_directory)
                source_path = temporary_path / f"movie{extension}"
                source_path.write_bytes(b"video")
                fake_ffprobe = temporary_path / "ffprobe"
                fake_ffprobe.write_text(
                    "#!/bin/sh\n"
                    'printf \'%s\\n\' \'{"streams":[{"codec_type":"audio"},'
                    '{"codec_type":"video","width":1920,"height":1080,'
                    '"avg_frame_rate":"24000/1001","field_order":"progressive"}],'
                    '"format":{"duration":"7200.0"}}\'\n'
                )
                fake_ffprobe.chmod(fake_ffprobe.stat().st_mode | stat.S_IXUSR)

                with patch.object(config, "FFPROBE_PATH", fake_ffprobe):
                    result = inspect_source(
                        JobSource(kind=WorkerSourceKind.DIRECT_FILE, path=source_path),
                        WorkerProcessOwner(),
                    )

                self.assertEqual(result["name"], "movie")
                self.assertEqual(result["resolution"], "1920x1080")
                self.assertEqual(result["frame_rate"], "24000/1001")
                self.assertFalse(result["interlaced"])
                self.assertEqual(result["size_bytes"], 5)
                self.assertEqual(result["duration_seconds"], 7200)

    def test_rejects_unsupported_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source_path = Path(temporary_directory) / "movie.mp4"
            source_path.touch()

            with self.assertRaises(WorkerOperationError) as context:
                inspect_source(
                    JobSource(kind=WorkerSourceKind.DIRECT_FILE, path=source_path),
                    WorkerProcessOwner(),
                )

            self.assertEqual(context.exception.code, "source_kind_mismatch")

    def test_inspects_iso_with_makemkv_disc_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            source_path = temporary_path / "movie.iso"
            source_path.write_bytes(b"disc")
            fake_makemkv = temporary_path / "makemkvcon"
            fake_makemkv.touch()

            with (
                patch.object(config, "MAKEMKVCON_PATH", fake_makemkv),
                patch(
                    "bd_to_avp.worker.operations.get_disc_and_mvc_video_info",
                    return_value=DiscInfo(
                        name="Feature 3D",
                        resolution="1920x1080",
                        frame_rate="24000/1001",
                        duration_seconds=7200,
                        titles=(
                            DiscTitleInfo(
                                id="makemkv:0",
                                title_number=0,
                                name="Main Movie",
                                output_name="Feature 3D",
                                duration_seconds=7200,
                                resolution="1920x1080",
                                frame_rate="24000/1001",
                                main_feature=True,
                            ),
                            DiscTitleInfo(
                                id="makemkv:2",
                                title_number=2,
                                name="3D Video 1",
                                output_name="Feature 3D - 3D Video 1",
                                duration_seconds=600,
                                resolution="1920x1080",
                                frame_rate="24000/1001",
                                main_feature=False,
                            ),
                        ),
                    ),
                ),
            ):
                result = inspect_source(
                    JobSource(kind=WorkerSourceKind.DISC_IMAGE, path=source_path),
                    WorkerProcessOwner(),
                )

            self.assertEqual(result["name"], "Feature 3D")
            self.assertEqual(result["resolution"], "1920x1080")
            self.assertEqual(result["frame_rate"], "24000/1001")
            self.assertEqual(result["size_bytes"], 4)
            self.assertEqual(len(result["titles"]), 2)
            self.assertEqual(result["titles"][0]["id"], "makemkv:0")
            self.assertTrue(result["titles"][0]["main_feature"])
            self.assertEqual(result["duration_seconds"], 7200)

    def test_iso_inspection_requires_makemkv(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source_path = Path(temporary_directory) / "movie.iso"
            source_path.touch()

            with (
                patch.object(config, "MAKEMKVCON_PATH", Path(temporary_directory) / "missing-makemkvcon"),
                self.assertRaises(WorkerOperationError) as context,
            ):
                inspect_source(
                    JobSource(kind=WorkerSourceKind.DISC_IMAGE, path=source_path),
                    WorkerProcessOwner(),
                )

            self.assertEqual(context.exception.code, "makemkv_missing")
            self.assertTrue(context.exception.retryable)

    def test_iso_inspection_preserves_makemkv_byte_output_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            source_path = temporary_path / "movie.iso"
            source_path.touch()
            fake_makemkv = temporary_path / "makemkvcon"
            fake_makemkv.touch()

            with (
                patch.object(config, "MAKEMKVCON_PATH", fake_makemkv),
                patch(
                    "bd_to_avp.worker.operations.get_disc_and_mvc_video_info",
                    side_effect=subprocess.CalledProcessError(
                        returncode=1,
                        cmd=[fake_makemkv],
                        output=b"Disc metadata could not be read.\n",
                    ),
                ),
                self.assertRaises(WorkerOperationError) as context,
            ):
                inspect_source(
                    JobSource(kind=WorkerSourceKind.DISC_IMAGE, path=source_path),
                    WorkerProcessOwner(),
                )

            self.assertEqual(context.exception.code, "disc_inspection_failed")
            self.assertEqual(context.exception.details, "Disc metadata could not be read.\n")
            self.assertTrue(context.exception.retryable)

    def test_inspects_physical_disc_through_device_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            fake_makemkv = Path(temporary_directory) / "makemkvcon"
            fake_makemkv.touch()
            source_path = Path("/dev/disk9")
            observed: dict[str, object] = {}

            def inspect_disc() -> DiscInfo:
                observed["source_path"] = config.source_path
                observed["source_str"] = config.source_str
                return DiscInfo(name="Physical 3D", resolution="1920x1080", frame_rate="24000/1001")

            with (
                patch.object(config, "MAKEMKVCON_PATH", fake_makemkv),
                patch("bd_to_avp.worker.operations.physical_disc_device_is_available", return_value=True),
                patch("bd_to_avp.worker.operations.get_disc_and_mvc_video_info", side_effect=inspect_disc),
            ):
                result = inspect_source(
                    JobSource(kind=WorkerSourceKind.PHYSICAL_DISC, path=source_path),
                    WorkerProcessOwner(),
                )

            self.assertEqual(result["name"], "Physical 3D")
            self.assertNotIn("size_bytes", result)
            self.assertIsNone(observed["source_path"])
            self.assertEqual(observed["source_str"], "dev:/dev/disk9")

    def test_reports_unavailable_physical_disc_as_retryable(self) -> None:
        with (
            patch("bd_to_avp.worker.operations.physical_disc_device_is_available", return_value=False),
            self.assertRaises(WorkerOperationError) as context,
        ):
            inspect_source(
                JobSource(kind=WorkerSourceKind.PHYSICAL_DISC, path=Path("/dev/disk9")),
                WorkerProcessOwner(),
            )

        self.assertEqual(context.exception.code, "disc_unavailable")
        self.assertTrue(context.exception.retryable)

    def test_rejects_non_device_path_for_physical_disc(self) -> None:
        with self.assertRaises(WorkerOperationError) as context:
            inspect_source(
                JobSource(kind=WorkerSourceKind.PHYSICAL_DISC, path=Path("/tmp/disk9")),
                WorkerProcessOwner(),
            )

        self.assertEqual(context.exception.code, "source_kind_mismatch")

    def test_accepts_raw_device_path_for_physical_disc(self) -> None:
        source_path = Path("/dev/rdisk9")
        with tempfile.TemporaryDirectory() as temporary_directory:
            fake_makemkv = Path(temporary_directory) / "makemkvcon"
            fake_makemkv.touch()
            with (
                patch.object(config, "MAKEMKVCON_PATH", fake_makemkv),
                patch("bd_to_avp.worker.operations.physical_disc_device_is_available", return_value=True),
                patch(
                    "bd_to_avp.worker.operations.get_disc_and_mvc_video_info",
                    return_value=DiscInfo(name="Raw Device 3D"),
                ),
            ):
                inspected = inspect_source(
                    JobSource(kind=WorkerSourceKind.PHYSICAL_DISC, path=source_path),
                    WorkerProcessOwner(),
                )

        self.assertEqual(inspected["name"], "Raw Device 3D")

    def test_inspects_bluray_folder_without_reporting_directory_size(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source_path = Path(temporary_directory) / "Movie"
            (source_path / "BDMV").mkdir(parents=True)
            fake_makemkv = Path(temporary_directory) / "makemkvcon"
            fake_makemkv.touch()

            with (
                patch.object(config, "MAKEMKVCON_PATH", fake_makemkv),
                patch(
                    "bd_to_avp.worker.operations.get_disc_and_mvc_video_info",
                    return_value=DiscInfo(name="Folder 3D", resolution="1920x1080", frame_rate="24/1"),
                ),
            ):
                result = inspect_source(
                    JobSource(kind=WorkerSourceKind.BLU_RAY_FOLDER, path=source_path),
                    WorkerProcessOwner(),
                )

            self.assertEqual(result["name"], "Folder 3D")
            self.assertNotIn("size_bytes", result)

    def test_normalizes_selected_bdmv_folder_to_disc_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source_path = Path(temporary_directory) / "Movie"
            bdmv_path = source_path / "BDMV"
            bdmv_path.mkdir(parents=True)
            fake_makemkv = Path(temporary_directory) / "makemkvcon"
            fake_makemkv.touch()
            observed: dict[str, object] = {}

            def inspect_disc() -> DiscInfo:
                observed["source_path"] = config.source_path
                observed["source_str"] = config.source_str
                return DiscInfo(name="Folder 3D")

            with (
                patch.object(config, "MAKEMKVCON_PATH", fake_makemkv),
                patch("bd_to_avp.worker.operations.get_disc_and_mvc_video_info", side_effect=inspect_disc),
            ):
                inspect_source(
                    JobSource(kind=WorkerSourceKind.BLU_RAY_FOLDER, path=bdmv_path),
                    WorkerProcessOwner(),
                )

            self.assertEqual(observed["source_path"], source_path)
            self.assertEqual(observed["source_str"], f"file:{source_path}")

    def test_rejects_folder_without_bdmv_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source_path = Path(temporary_directory) / "Not a Disc"
            source_path.mkdir()

            with self.assertRaises(WorkerOperationError) as context:
                inspect_source(
                    JobSource(kind=WorkerSourceKind.BLU_RAY_FOLDER, path=source_path),
                    WorkerProcessOwner(),
                )

            self.assertEqual(context.exception.code, "invalid_bluray_folder")


class SourceConversionTests(unittest.TestCase):
    def test_subtitles_off_maps_to_legacy_engine_flags_without_audio_filtering(self) -> None:
        source_path = Path("/tmp/movie.mkv")
        request = json.loads(conversion_request_line(source_path, Path("/tmp/output")))
        request["encoding"]["subtitles"] = {"mode": "off", "preferred_language": None}
        job = JobSpec.from_json_line(json.dumps(request) + "\n")

        with configured_conversion(job, source_path):
            self.assertTrue(config.skip_subtitles)
            self.assertEqual(config.language_code, "eng")
            self.assertFalse(config.remove_extra_languages)

    def test_preview_uses_resolved_range_and_emits_owned_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            source_path = temporary_path / "movie.mkv"
            source_path.write_bytes(b"source")
            destination_path = temporary_path / "preview"
            final_path = destination_path / "movie_AVP.mov"
            final_path.parent.mkdir()
            final_path.write_bytes(b"final")
            job = JobSpec.from_json_line(preview_request_line(source_path, destination_path))
            output = io.StringIO()
            activity = WorkerActivityReporter(WorkerEventEmitter(output, job.job_id))
            observed: dict[str, object] = {}

            def process_each(*_args: object, **_kwargs: object) -> Path:
                observed["preview_range"] = config.preview_range
                return final_path

            with (
                patch(
                    "bd_to_avp.worker.operations.inspect_source",
                    return_value={"duration_seconds": 7200.0},
                ),
                patch.object(config, "configure_tool_environment"),
                patch("bd_to_avp.modules.process.process_each", side_effect=process_each),
            ):
                result = preview_source(job, WorkerProcessOwner(), activity)

            preview_range = observed["preview_range"]
            self.assertEqual(preview_range.start_seconds, 3570)
            self.assertEqual(preview_range.duration_seconds, 60)
            self.assertEqual(result["output_path"], final_path.as_posix())
            self.assertEqual(result["parent_job_id"], job.preview.parent_job_id if job.preview else None)
            self.assertEqual(decoded_events(output)[-1]["type"], "artifact.ready")

    def test_iso_preview_preserves_selected_title_id_through_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            source_path = temporary_path / "movie.iso"
            source_path.write_bytes(b"disc")
            destination_path = temporary_path / "preview"
            final_path = destination_path / "movie_AVP.mov"
            final_path.parent.mkdir()
            final_path.write_bytes(b"final")
            job = JobSpec.from_json_line(preview_request_line(source_path, destination_path))
            output = io.StringIO()
            activity = WorkerActivityReporter(WorkerEventEmitter(output, job.job_id))
            observed: dict[str, object] = {}

            def process_each(*_args: object, **kwargs: object) -> Path:
                observed["selected_title_id"] = kwargs.get("selected_title_id")
                return final_path

            with (
                patch(
                    "bd_to_avp.worker.operations.inspect_source",
                    return_value={"duration_seconds": 7200.0},
                ),
                patch.object(config, "configure_tool_environment"),
                patch("bd_to_avp.modules.process.process_each", side_effect=process_each),
            ):
                result = preview_source(job, WorkerProcessOwner(), activity)

            self.assertEqual(job.source.title_id, "makemkv:0")
            self.assertEqual(observed["selected_title_id"], "makemkv:0")
            self.assertEqual(result["title_id"], "makemkv:0")
            artifact = decoded_events(output)[-1]
            self.assertEqual(artifact["type"], "artifact.ready")
            self.assertEqual(artifact["payload"]["artifact"]["title_id"], "makemkv:0")

    def test_process_each_bridges_makemkv_progress_to_worker_heartbeat(self) -> None:
        class StopAfterProgress(Exception):
            pass

        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            source_path = temporary_path / "movie.iso"
            source_path.write_bytes(b"disc")
            output = io.StringIO()
            activity = WorkerActivityReporter(WorkerEventEmitter(output, str(uuid4())))
            activity.set_stage_plan(["preflight", "inspect_source", "create_mkv"])

            def create_mkv_file(
                _output_folder: Path,
                _disc_info: DiscInfo,
                progress_callback: object | None = None,
            ) -> Path:
                self.assertTrue(callable(progress_callback))
                progress_callback(50, 100)
                raise StopAfterProgress

            with (
                patch.object(config, "source_path", source_path),
                patch.object(config, "source_str", None),
                patch.object(config, "output_root_path", temporary_path / "output"),
                patch.object(config, "overwrite", True),
                patch.object(config, "start_stage", Stage.CREATE_MKV),
                patch.object(config, "preview_range", None),
                patch("bd_to_avp.modules.process.preflight.verify_runtime_ready"),
                patch("bd_to_avp.modules.process.get_disc_and_mvc_video_info", return_value=DiscInfo(name="Movie")),
                patch("bd_to_avp.modules.process.create_mkv_file", side_effect=create_mkv_file),
                self.assertRaises(StopAfterProgress),
            ):
                process_each(activity=activity, selected_title_id="makemkv:0")

            activity.emit_heartbeat(1)
            heartbeat = decoded_events(output)[-1]
            self.assertEqual(heartbeat["type"], "heartbeat")
            self.assertEqual(heartbeat["payload"]["progress"]["stage_fraction"], 0.5)

    def test_physical_disc_conversion_uses_unowned_device_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            source_path = Path("/dev/disk9")
            destination_path = temporary_path / "output"
            final_path = destination_path / "movie_AVP.mov"
            final_path.parent.mkdir()
            final_path.write_bytes(b"final")
            job = JobSpec.from_json_line(
                conversion_request_line(
                    source_path,
                    destination_path,
                    source_kind=WorkerSourceKind.PHYSICAL_DISC.value,
                )
            )
            emitter = WorkerEventEmitter(io.StringIO(), job.job_id)
            activity = WorkerActivityReporter(emitter)
            observed: dict[str, object] = {}

            def process_each(*_args: object, **_kwargs: object) -> Path:
                observed["source_path"] = config.source_path
                observed["source_str"] = config.source_str
                observed["remove_original"] = config.remove_original
                return final_path

            with (
                patch("bd_to_avp.worker.operations.physical_disc_device_is_available", return_value=True),
                patch.object(config, "configure_tool_environment"),
                patch("bd_to_avp.modules.process.process_each", side_effect=process_each),
            ):
                result = convert_source(job, WorkerProcessOwner(), activity)

            self.assertEqual(result["source_path"], source_path.as_posix())
            self.assertIsNone(observed["source_path"])
            self.assertEqual(observed["source_str"], "dev:/dev/disk9")
            self.assertFalse(observed["remove_original"])

    def test_bluray_folder_conversion_passes_explicit_file_source_to_engine(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            source_path = temporary_path / "Movie"
            (source_path / "BDMV").mkdir(parents=True)
            destination_path = temporary_path / "output"
            final_path = destination_path / "movie_AVP.mov"
            final_path.parent.mkdir()
            final_path.write_bytes(b"final")
            job = JobSpec.from_json_line(conversion_request_line(source_path, destination_path))
            emitter = WorkerEventEmitter(io.StringIO(), job.job_id)
            activity = WorkerActivityReporter(emitter)
            observed: dict[str, object] = {}

            def process_each(*_args: object, **_kwargs: object) -> Path:
                observed["source_path"] = config.source_path
                observed["source_str"] = config.source_str
                return final_path

            with (
                patch.object(config, "configure_tool_environment"),
                patch("bd_to_avp.modules.process.process_each", side_effect=process_each),
            ):
                result = convert_source(job, WorkerProcessOwner(), activity)

            self.assertEqual(result["output_path"], final_path.as_posix())
            self.assertEqual(observed["source_path"], source_path)
            self.assertEqual(observed["source_str"], f"file:{source_path}")

    def test_bluray_folder_destination_must_be_outside_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source_path = Path(temporary_directory) / "Movie"
            (source_path / "BDMV").mkdir(parents=True)
            job = JobSpec.from_json_line(conversion_request_line(source_path, source_path / "output"))
            emitter = WorkerEventEmitter(io.StringIO(), job.job_id)

            with self.assertRaises(WorkerOperationError) as context:
                convert_source(job, WorkerProcessOwner(), WorkerActivityReporter(emitter))

            self.assertEqual(context.exception.code, "destination_inside_source")

    def test_iso_conversion_passes_source_to_existing_engine(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            source_path = temporary_path / "movie.iso"
            source_path.write_bytes(b"disc")
            destination_path = temporary_path / "output"
            final_path = destination_path / "movie_AVP.mov"
            final_path.parent.mkdir()
            final_path.write_bytes(b"final")
            job = JobSpec.from_json_line(conversion_request_line(source_path, destination_path))
            emitter = WorkerEventEmitter(io.StringIO(), job.job_id)
            activity = WorkerActivityReporter(emitter)

            with (
                patch.object(config, "configure_tool_environment"),
                patch("bd_to_avp.modules.process.process_each", return_value=final_path) as process_each_mock,
            ):
                result = convert_source(job, WorkerProcessOwner(), activity)

            self.assertEqual(result["output_path"], final_path.as_posix())
            self.assertEqual(result["title_id"], "makemkv:0")
            process_each_mock.assert_called_once()
            self.assertIs(process_each_mock.call_args.kwargs["activity"], activity)
            self.assertEqual(process_each_mock.call_args.kwargs["selected_title_id"], "makemkv:0")

    def test_iso_makemkv_failure_requests_recovery_decision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            source_path = temporary_path / "movie.iso"
            source_path.write_bytes(b"disc")
            destination_path = temporary_path / "output"
            job = JobSpec.from_json_line(conversion_request_line(source_path, destination_path))
            emitter = WorkerEventEmitter(io.StringIO(), job.job_id)
            activity = WorkerActivityReporter(emitter)

            with (
                patch.object(config, "configure_tool_environment"),
                patch("bd_to_avp.modules.process.process_each", side_effect=MKVCreationError("read error")),
                self.assertRaises(WorkerDecisionRequired) as context,
            ):
                convert_source(job, WorkerProcessOwner(), activity)

            self.assertEqual(context.exception.code, "mkv_creation_decision_required")
            self.assertEqual(context.exception.choices, ("retry_continue_on_error", "cancel"))
            self.assertIn("Extract MVC and Audio", context.exception.details or "")

    def test_subtitle_failure_requests_skip_subtitles_decision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            source_path = temporary_path / "movie.iso"
            source_path.write_bytes(b"disc")
            destination_path = temporary_path / "output"
            job = JobSpec.from_json_line(conversion_request_line(source_path, destination_path))
            emitter = WorkerEventEmitter(io.StringIO(), job.job_id)
            activity = WorkerActivityReporter(emitter)

            with (
                patch.object(config, "configure_tool_environment"),
                patch("bd_to_avp.modules.process.process_each", side_effect=SRTCreationError("OCR error")),
                self.assertRaises(WorkerDecisionRequired) as context,
            ):
                convert_source(job, WorkerProcessOwner(), activity)

            self.assertEqual(context.exception.code, "subtitle_decision_required")
            self.assertEqual(context.exception.choices, ("retry_without_subtitles", "cancel"))
            self.assertIn("Turn off Include subtitles", context.exception.details or "")

    def test_conversion_restores_global_config_and_tool_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            source_path = temporary_path / "movie.mkv"
            source_path.write_bytes(b"source")
            destination_path = temporary_path / "output"
            final_path = destination_path / "movie_AVP.mov"
            final_path.parent.mkdir()
            final_path.write_bytes(b"final")
            job = JobSpec.from_json_line(conversion_request_line(source_path, destination_path))
            emitter = WorkerEventEmitter(io.StringIO(), job.job_id)
            activity = WorkerActivityReporter(emitter)
            previous_source_path = config.source_path
            previous_output_root_path = config.output_root_path
            previous_audio_mode = config.audio_mode
            previous_remove_original = config.remove_original
            previous_environment = {
                key: os.environ.get(key) for key in ("PATH", "FFMPEG_BINARY", "FFPROBE_BINARY", "TMPDIR")
            }

            def mutate_environment() -> None:
                os.environ.update(
                    {
                        "PATH": "worker-path",
                        "FFMPEG_BINARY": "worker-ffmpeg",
                        "FFPROBE_BINARY": "worker-ffprobe",
                        "TMPDIR": "worker-tmp",
                    }
                )

            with (
                patch.object(config, "configure_tool_environment", side_effect=mutate_environment),
                patch("bd_to_avp.modules.process.process_each", return_value=final_path) as process_each_mock,
            ):
                result = convert_source(job, WorkerProcessOwner(), activity)

            self.assertEqual(result["output_path"], final_path.as_posix())
            self.assertEqual(result["destination_path"], destination_path.as_posix())
            process_each_mock.assert_called_once()
            self.assertEqual(config.source_path, previous_source_path)
            self.assertEqual(config.output_root_path, previous_output_root_path)
            self.assertEqual(config.audio_mode, previous_audio_mode)
            self.assertEqual(config.remove_original, previous_remove_original)
            for key, value in previous_environment.items():
                self.assertEqual(os.environ.get(key), value)

    def test_conversion_passes_request_options_to_existing_engine(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            source_path = temporary_path / "movie.m2ts"
            source_path.write_bytes(b"source")
            destination_path = temporary_path / "output"
            final_path = destination_path / "movie_AVP.mov"
            final_path.parent.mkdir()
            final_path.write_bytes(b"final")
            request = json.loads(conversion_request_line(source_path, destination_path))
            request["encoding"].update(
                {
                    "audio": {"mode": "pcm", "bitrate": 512},
                    "video_mode": "mv_hevc",
                    "av1_crf": 27,
                    "left_right_bitrate": 42,
                    "mv_hevc_quality": 88,
                    "upscale_quality": 66,
                    "fov": 110,
                    "frame_rate": "24000/1001",
                    "resolution": "1920x1080",
                    "crop_black_bars": True,
                    "swap_eyes": True,
                    "fx_upscale": True,
                    "subtitles": {
                        "mode": "preferred_only",
                        "preferred_language": "jpn",
                    },
                }
            )
            request["job"].update(
                {
                    "start_stage": 7,
                    "keep_files": True,
                    "overwrite": True,
                    "remove_original": True,
                    "continue_on_error": True,
                    "software_encoder": True,
                    "output_commands": True,
                    "keep_awake": True,
                }
            )
            job = JobSpec.from_json_line(json.dumps(request) + "\n")
            emitter = WorkerEventEmitter(io.StringIO(), job.job_id)
            activity = WorkerActivityReporter(emitter)
            observed: dict[str, object] = {}

            def fake_process_each(_cancellation_event: object, activity: object | None = None) -> Path:
                observed.update(
                    {
                        "source_path": config.source_path,
                        "output_root_path": config.output_root_path,
                        "audio_mode": config.audio_mode,
                        "audio_bitrate": config.audio_bitrate,
                        "video_mode": config.video_mode,
                        "av1_crf": config.av1_crf,
                        "left_right_bitrate": config.left_right_bitrate,
                        "mv_hevc_quality": config.mv_hevc_quality,
                        "upscale_quality": config.upscale_quality,
                        "fov": config.fov,
                        "frame_rate": config.frame_rate,
                        "resolution": config.resolution,
                        "skip_subtitles": config.skip_subtitles,
                        "crop_black_bars": config.crop_black_bars,
                        "swap_eyes": config.swap_eyes,
                        "fx_upscale": config.fx_upscale,
                        "language_code": config.language_code,
                        "remove_extra_languages": config.remove_extra_languages,
                        "start_stage": config.start_stage.value,
                        "keep_files": config.keep_files,
                        "overwrite": config.overwrite,
                        "remove_original": config.remove_original,
                        "continue_on_error": config.continue_on_error,
                        "software_encoder": config.software_encoder,
                        "output_commands": config.output_commands,
                        "keep_awake": config.keep_awake,
                        "activity": activity,
                    }
                )
                return final_path

            with (
                patch.object(config, "configure_tool_environment"),
                patch("bd_to_avp.modules.process.process_each", side_effect=fake_process_each),
            ):
                convert_source(job, WorkerProcessOwner(), activity)

            self.assertEqual(observed["source_path"], source_path)
            self.assertEqual(observed["output_root_path"], destination_path)
            self.assertEqual(observed["audio_mode"], AudioMode.PCM)
            self.assertEqual(observed["audio_bitrate"], 512)
            self.assertEqual(observed["video_mode"], VideoMode.MV_HEVC)
            self.assertEqual(observed["av1_crf"], 27)
            self.assertEqual(observed["left_right_bitrate"], 42)
            self.assertEqual(observed["mv_hevc_quality"], 88)
            self.assertEqual(observed["upscale_quality"], 66)
            self.assertEqual(observed["fov"], 110)
            self.assertEqual(observed["frame_rate"], "24000/1001")
            self.assertEqual(observed["resolution"], "1920x1080")
            self.assertFalse(observed["skip_subtitles"])
            self.assertTrue(observed["crop_black_bars"])
            self.assertTrue(observed["swap_eyes"])
            self.assertTrue(observed["fx_upscale"])
            self.assertEqual(observed["language_code"], "jpn")
            self.assertTrue(observed["remove_extra_languages"])
            self.assertEqual(observed["start_stage"], 7)
            self.assertTrue(observed["keep_files"])
            self.assertTrue(observed["overwrite"])
            self.assertTrue(observed["remove_original"])
            self.assertTrue(observed["continue_on_error"])
            self.assertTrue(observed["software_encoder"])
            self.assertTrue(observed["output_commands"])
            self.assertTrue(observed["keep_awake"])
            self.assertIs(observed["activity"], activity)

    def test_conversion_maps_cancellation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            source_path = temporary_path / "movie.mkv"
            source_path.write_bytes(b"source")
            destination_path = temporary_path / "output"
            job = JobSpec.from_json_line(conversion_request_line(source_path, destination_path))
            emitter = WorkerEventEmitter(io.StringIO(), job.job_id)
            activity = WorkerActivityReporter(emitter)
            owner = WorkerProcessOwner()

            def cancel(_cancellation_event: object, activity: object | None = None) -> Path:
                owner.request_cancel()
                from bd_to_avp.modules.process import ProcessingCancelled

                raise ProcessingCancelled("stop")

            with (
                patch.object(config, "configure_tool_environment"),
                patch("bd_to_avp.modules.process.process_each", side_effect=cancel),
                self.assertRaises(WorkerCancelled),
            ):
                convert_source(job, owner, activity)

    def test_conversion_maps_engine_failure_without_losing_error_type(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            source_path = temporary_path / "movie.mkv"
            source_path.write_bytes(b"source")
            destination_path = temporary_path / "output"
            job = JobSpec.from_json_line(conversion_request_line(source_path, destination_path))
            emitter = WorkerEventEmitter(io.StringIO(), job.job_id)
            activity = WorkerActivityReporter(emitter)
            previous_source_path = config.source_path
            previous_output_root_path = config.output_root_path
            previous_tmpdir = os.environ.get("TMPDIR")

            with (
                patch.object(
                    config,
                    "configure_tool_environment",
                    side_effect=lambda: os.environ.__setitem__("TMPDIR", "worker-failure-tmp"),
                ),
                patch("bd_to_avp.modules.process.process_each", side_effect=RuntimeError("bad source")),
                self.assertRaises(WorkerOperationError) as context,
            ):
                convert_source(job, WorkerProcessOwner(), activity)

            self.assertEqual(context.exception.code, "conversion_failed")
            self.assertEqual(context.exception.details, "bad source")
            self.assertEqual(config.source_path, previous_source_path)
            self.assertEqual(config.output_root_path, previous_output_root_path)
            self.assertEqual(os.environ.get("TMPDIR"), previous_tmpdir)


class WorkerProcessOwnerTests(unittest.TestCase):
    def test_cancellation_terminates_owned_descendant(self) -> None:
        child = subprocess.Popen(["/bin/sleep", "30"])
        try:
            owner = WorkerProcessOwner()
            owner.request_cancel()

            child.wait(timeout=3)
            self.assertIsNotNone(child.returncode)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=3)

    def test_repeated_signals_do_not_reenter_descendant_cleanup(self) -> None:
        child = subprocess.Popen(["/bin/sh", "-c", "trap '' TERM; exec /bin/sleep 30"])
        try:
            owner = WorkerProcessOwner()

            owner._handle_signal(signal.SIGTERM, None)
            owner._handle_signal(signal.SIGTERM, None)

            self.assertTrue(owner.cancellation_event.is_set())
            self.assertIsNone(child.poll())
            owner.terminate_descendants(timeout=0.1)
            child.wait(timeout=3)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=3)


if __name__ == "__main__":
    unittest.main()
