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

from bd_to_avp.modules.config import config
from bd_to_avp.modules.disc import DiscInfo, MKVCreationError
from bd_to_avp.worker.__main__ import run_worker
from bd_to_avp.worker.operations import WorkerDecisionRequired, WorkerOperationError, convert_source, inspect_source
from bd_to_avp.worker.ownership import WorkerCancelled, WorkerProcessOwner
from bd_to_avp.worker.protocol import (
    MAX_EVENT_BYTES,
    MAX_REQUEST_BYTES,
    MAX_DETAIL_BYTES,
    PROTOCOL_VERSION,
    WorkerActivityReporter,
    JobSpec,
    WorkerEventEmitter,
    WorkerEventType,
    WorkerProtocolError,
)


def request_line(source_path: Path, **overrides: object) -> str:
    request: dict[str, object] = {
        "protocol_version": PROTOCOL_VERSION,
        "type": "job.start",
        "job_id": str(uuid4()),
        "operation": "inspect_source",
        "source": {"path": str(source_path)},
    }
    request.update(overrides)
    return json.dumps(request) + "\n"


def conversion_request_line(source_path: Path, destination_path: Path, **overrides: object) -> str:
    request: dict[str, object] = {
        "protocol_version": PROTOCOL_VERSION,
        "type": "job.start",
        "job_id": str(uuid4()),
        "operation": "convert_source",
        "source": {"path": str(source_path)},
        "destination": {"path": str(destination_path)},
        "encoding": {
            "transcode_audio": True,
            "audio_bitrate": 384,
            "left_right_bitrate": 20,
            "link_quality": True,
            "mv_hevc_quality": 75,
            "upscale_quality": 75,
            "fov": 90,
            "frame_rate": "",
            "resolution": "",
            "skip_subtitles": False,
            "crop_black_bars": False,
            "swap_eyes": False,
            "fx_upscale": False,
            "language_code": "eng",
            "remove_extra_languages": False,
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
            "output_length": "full_movie",
        },
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
        self.assertEqual(job.source.path, source_path)

    def test_parses_strict_conversion_request(self) -> None:
        source_path = Path("/tmp/movie.mkv")
        destination_path = Path("/tmp/output")

        job = JobSpec.from_json_line(conversion_request_line(source_path, destination_path))

        self.assertEqual(job.operation.value, "convert_source")
        self.assertEqual(job.source.path, source_path)
        self.assertEqual(job.destination.path if job.destination else None, destination_path)
        self.assertTrue(job.encoding.transcode_audio if job.encoding else False)
        self.assertEqual(job.job.start_stage if job.job else None, 1)
        self.assertEqual(job.job.output_length if job.job else None, "full_movie")

    def test_parses_shared_swift_conversion_fixture(self) -> None:
        fixture_path = Path(__file__).parent / "fixtures" / "native_worker_convert_v1.json"

        job = JobSpec.from_json_line(fixture_path.read_text(encoding="utf-8"))

        self.assertEqual(job.operation.value, "convert_source")
        self.assertEqual(job.source.path, Path("/tmp/movie.mkv"))
        self.assertEqual(job.destination.path if job.destination else None, Path("/tmp/output"))
        self.assertEqual(job.encoding.mv_hevc_quality if job.encoding else None, 75)

    def test_rejects_sample_conversion_request(self) -> None:
        request = json.loads(conversion_request_line(Path("/tmp/movie.mkv"), Path("/tmp/output")))
        request["job"]["output_length"] = "three_minutes"

        with self.assertRaises(WorkerProtocolError) as context:
            JobSpec.from_json_line(json.dumps(request) + "\n")

        self.assertEqual(context.exception.code, "invalid_job_options")

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
        fixture_path = Path(__file__).parent / "fixtures" / "native_worker_conversion_completed_v1.json"
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))

        self.assertEqual(decoded_events(output)[-1], fixture)

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
                    '"avg_frame_rate":"24000/1001","field_order":"progressive"}]}\'\n'
                )
                fake_ffprobe.chmod(fake_ffprobe.stat().st_mode | stat.S_IXUSR)

                with patch.object(config, "FFPROBE_PATH", fake_ffprobe):
                    result = inspect_source(source_path, WorkerProcessOwner())

                self.assertEqual(result["name"], "movie")
                self.assertEqual(result["resolution"], "1920x1080")
                self.assertEqual(result["frame_rate"], "24000/1001")
                self.assertFalse(result["interlaced"])
                self.assertEqual(result["size_bytes"], 5)

    def test_rejects_unsupported_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source_path = Path(temporary_directory) / "movie.mp4"
            source_path.touch()

            with self.assertRaisesRegex(WorkerOperationError, "supports ISO, MKV, MTS, and M2TS"):
                inspect_source(source_path, WorkerProcessOwner())

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
                    ),
                ),
            ):
                result = inspect_source(source_path, WorkerProcessOwner())

            self.assertEqual(result["name"], "Feature 3D")
            self.assertEqual(result["resolution"], "1920x1080")
            self.assertEqual(result["frame_rate"], "24000/1001")
            self.assertEqual(result["size_bytes"], 4)

    def test_iso_inspection_requires_makemkv(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source_path = Path(temporary_directory) / "movie.iso"
            source_path.touch()

            with (
                patch.object(config, "MAKEMKVCON_PATH", Path(temporary_directory) / "missing-makemkvcon"),
                self.assertRaises(WorkerOperationError) as context,
            ):
                inspect_source(source_path, WorkerProcessOwner())

            self.assertEqual(context.exception.code, "makemkv_missing")
            self.assertTrue(context.exception.retryable)


class SourceConversionTests(unittest.TestCase):
    def test_rejects_folder_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            source_path = temporary_path / "BDMV"
            source_path.mkdir()
            destination_path = temporary_path / "output"
            job = JobSpec.from_json_line(conversion_request_line(source_path, destination_path))
            emitter = WorkerEventEmitter(io.StringIO(), job.job_id)
            activity = WorkerActivityReporter(emitter)

            with self.assertRaises(WorkerOperationError) as context:
                convert_source(job, WorkerProcessOwner(), activity)

            self.assertEqual(context.exception.code, "source_not_file")

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
            process_each_mock.assert_called_once()

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
            previous_transcode_audio = config.transcode_audio
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
            self.assertEqual(config.transcode_audio, previous_transcode_audio)
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
                    "transcode_audio": False,
                    "audio_bitrate": 512,
                    "left_right_bitrate": 42,
                    "mv_hevc_quality": 88,
                    "upscale_quality": 66,
                    "fov": 110,
                    "frame_rate": "24000/1001",
                    "resolution": "1920x1080",
                    "skip_subtitles": True,
                    "crop_black_bars": True,
                    "swap_eyes": True,
                    "fx_upscale": True,
                    "language_code": "jpn",
                    "remove_extra_languages": True,
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
                        "transcode_audio": config.transcode_audio,
                        "audio_bitrate": config.audio_bitrate,
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
            self.assertFalse(observed["transcode_audio"])
            self.assertEqual(observed["audio_bitrate"], 512)
            self.assertEqual(observed["left_right_bitrate"], 42)
            self.assertEqual(observed["mv_hevc_quality"], 88)
            self.assertEqual(observed["upscale_quality"], 66)
            self.assertEqual(observed["fov"], 110)
            self.assertEqual(observed["frame_rate"], "24000/1001")
            self.assertEqual(observed["resolution"], "1920x1080")
            self.assertTrue(observed["skip_subtitles"])
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
