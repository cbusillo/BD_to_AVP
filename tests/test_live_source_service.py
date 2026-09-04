import io
import json
import os
import shutil
import stat
import tempfile
import threading
import time
import unittest

from pathlib import Path
from unittest import mock

from bd_to_avp.worker.__main__ import run_worker
from bd_to_avp.worker.operations import WorkerOperationError, start_live_source
from bd_to_avp.worker.ownership import WorkerCancelled, WorkerProcessOwner
from bd_to_avp.worker.protocol import (
    JobSpec,
    LiveSourceOptions,
    WorkerActivityReporter,
    WorkerEventEmitter,
    WorkerOperation,
)
from bd_to_avp.worker.source_service import (
    ReplayStore,
    SERVICE_FLAG_RANDOM_ACCESS,
    SERVICE_FRAME_HEADER,
    SSIFLiveSourceService,
    ServiceFrameReader,
    ServiceRecord,
    ServiceRecordKind,
    SourceServiceError,
)


FIXTURE_PROBE = Path(__file__).parent / "fixtures/fake_ssif_probe.py"


def framed_record(
    kind: ServiceRecordKind,
    sequence: int,
    *,
    pts: int = 0,
    dts: int = 0,
    primary: bytes = b"",
    secondary: bytes = b"",
    random_access: bool = False,
) -> bytes:
    flags = SERVICE_FLAG_RANDOM_ACCESS if random_access else 0
    return (
        SERVICE_FRAME_HEADER.pack(
            b"SSFS",
            1,
            kind.value,
            flags,
            sequence,
            pts,
            dts,
            len(primary),
            len(secondary),
        )
        + primary
        + secondary
    )


def service_record(
    kind: ServiceRecordKind,
    sequence: int,
    dts: int,
    payload: bytes,
    *,
    random_access: bool = False,
    secondary: bytes = b"",
) -> ServiceRecord:
    return ServiceRecord(
        kind=kind,
        sequence=sequence,
        pts=dts,
        dts=dts,
        primary=payload,
        secondary=secondary,
        random_access=random_access,
    )


def live_options(
    *,
    replay_window_seconds: int = 120,
    replay_max_bytes: int = 1_048_576,
) -> LiveSourceOptions:
    return LiveSourceOptions(
        playlist=1005,
        audio_pid=0x1100,
        replay_window_seconds=replay_window_seconds,
        replay_max_bytes=replay_max_bytes,
    )


def live_request(source: Path, destination: Path) -> dict[str, object]:
    return {
        "protocol_version": 12,
        "type": "job.start",
        "job_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "operation": "start_live_source",
        "source": {"kind": "disc_image", "path": str(source)},
        "destination": {"path": str(destination)},
        "live_source": {
            "playlist": 1005,
            "audio_pid": 0x1100,
            "replay_window_seconds": 120,
            "replay_max_bytes": 1_048_576,
        },
    }


class FragmentedStream(io.BytesIO):
    def read(self, size: int = -1) -> bytes:
        return super().read(min(size, 3) if size >= 0 else 3)


class ServiceFrameReaderTests(unittest.TestCase):
    def test_reads_fragmented_video_audio_and_completion(self) -> None:
        payload = b"".join(
            [
                framed_record(
                    ServiceRecordKind.VIDEO,
                    0,
                    pts=90,
                    dts=90,
                    primary=b"base",
                    secondary=b"dependent",
                    random_access=True,
                ),
                framed_record(ServiceRecordKind.AUDIO, 1, pts=95, dts=95, primary=b"audio"),
                framed_record(ServiceRecordKind.COMPLETE, 2),
            ]
        )
        reader = ServiceFrameReader(FragmentedStream(payload))

        video = reader.read()
        audio = reader.read()
        complete = reader.read()

        self.assertEqual(video.kind, ServiceRecordKind.VIDEO)
        self.assertTrue(video.random_access)
        self.assertEqual(video.primary + video.secondary, b"basedependent")
        self.assertEqual(audio.kind, ServiceRecordKind.AUDIO)
        self.assertEqual(audio.primary, b"audio")
        self.assertEqual(complete.kind, ServiceRecordKind.COMPLETE)
        self.assertIsNone(reader.read())

    def test_rejects_skipped_sequence(self) -> None:
        reader = ServiceFrameReader(io.BytesIO(framed_record(ServiceRecordKind.COMPLETE, 1)))

        with self.assertRaisesRegex(SourceServiceError, "skipped record sequence 0"):
            reader.read()

    def test_rejects_truncated_payload(self) -> None:
        payload = framed_record(ServiceRecordKind.AUDIO, 0, primary=b"audio")[:-1]
        reader = ServiceFrameReader(io.BytesIO(payload))

        with self.assertRaisesRegex(SourceServiceError, "ended inside a media record"):
            reader.read()

    def test_times_out_when_started_helper_produces_no_frame(self) -> None:
        read_descriptor, write_descriptor = os.pipe()
        with os.fdopen(read_descriptor, "rb", buffering=0) as read_stream:
            try:
                reader = ServiceFrameReader(read_stream, read_timeout_seconds=0.01)

                with self.assertRaises(SourceServiceError) as raised:
                    reader.read()

                self.assertEqual(raised.exception.code, "source_stream_timeout")
            finally:
                os.close(write_descriptor)

    def test_times_out_when_started_helper_stalls_between_frames(self) -> None:
        read_descriptor, write_descriptor = os.pipe()
        with os.fdopen(read_descriptor, "rb", buffering=0) as read_stream:
            try:
                os.write(write_descriptor, framed_record(ServiceRecordKind.COMPLETE, 0))
                reader = ServiceFrameReader(read_stream, read_timeout_seconds=0.01)

                self.assertEqual(reader.read().kind, ServiceRecordKind.COMPLETE)
                with self.assertRaises(SourceServiceError) as raised:
                    reader.read()

                self.assertEqual(raised.exception.code, "source_stream_timeout")
            finally:
                os.close(write_descriptor)

    def test_rejects_invalid_kind_specific_shapes(self) -> None:
        malformed = {
            "unknown flags": SERVICE_FRAME_HEADER.pack(b"SSFS", 1, 1, 2, 0, 0, 0, 1, 1) + b"ab",
            "missing dependent view": SERVICE_FRAME_HEADER.pack(b"SSFS", 1, 1, 0, 0, 0, 0, 1, 0) + b"a",
            "audio secondary payload": SERVICE_FRAME_HEADER.pack(b"SSFS", 1, 2, 0, 0, 0, 0, 1, 1) + b"ab",
            "nonzero completion timestamp": SERVICE_FRAME_HEADER.pack(b"SSFS", 1, 3, 0, 0, 1, 0, 0, 0),
        }

        for label, payload in malformed.items():
            with self.subTest(label=label), self.assertRaises(SourceServiceError):
                ServiceFrameReader(io.BytesIO(payload)).read()


class ReplayStoreTests(unittest.TestCase):
    def make_store(self, root: Path, options: LiveSourceOptions | None = None) -> ReplayStore:
        return ReplayStore(
            root,
            options or live_options(),
            {"id": "mvc", "format": "annex_b", "timebase": 90_000},
            {"id": "audio:4352", "pid": 4352, "timebase": 90_000},
        )

    def test_writes_keyframe_led_gops_with_timing_and_audio(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "live"
            store = self.make_store(root)
            store.consume(service_record(ServiceRecordKind.AUDIO, 0, 80, b"audio-zero"))
            store.consume(
                service_record(
                    ServiceRecordKind.VIDEO,
                    1,
                    90,
                    b"base-zero",
                    secondary=b"dependent-zero",
                    random_access=True,
                )
            )
            store.consume(service_record(ServiceRecordKind.AUDIO, 2, 95, b"audio-one"))
            store.consume(service_record(ServiceRecordKind.VIDEO, 3, 100, b"video-one"))
            store.consume(
                service_record(
                    ServiceRecordKind.VIDEO,
                    4,
                    180_090,
                    b"base-two",
                    secondary=b"dependent-two",
                    random_access=True,
                )
            )
            store.consume(service_record(ServiceRecordKind.AUDIO, 5, 180_095, b"audio-two"))
            store.finish()

            manifest = json.loads(store.manifest_path.read_text(encoding="utf-8"))
            self.assertTrue(manifest["complete"])
            self.assertEqual(manifest["earliest_available_ticks"], 0)
            self.assertEqual(manifest["latest_available_ticks"], 180_005)
            self.assertEqual(len(manifest["gops"]), 2)
            self.assertEqual(manifest["video_stream"]["id"], "mvc")
            self.assertEqual(manifest["audio_stream"]["id"], "audio:4352")
            first = manifest["gops"][0]
            self.assertEqual((root / first["video_path"]).read_bytes(), b"base-zerodependent-zerovideo-one")
            self.assertEqual((root / first["audio_path"]).read_bytes(), b"audio-zeroaudio-one")
            index_rows = [json.loads(line) for line in (root / first["index_path"]).read_text().splitlines()]
            self.assertEqual([row["kind"] for row in index_rows], ["audio", "video", "video", "audio"])
            self.assertEqual(index_rows[0]["pts_ticks"], 0)
            self.assertEqual(index_rows[0]["dts_ticks"], 0)
            self.assertTrue(index_rows[1]["random_access"])

    def test_evicts_old_gops_and_reports_honest_earliest_position(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "live"
            store = self.make_store(root, live_options(replay_window_seconds=1))
            for sequence, dts in enumerate((90, 90_090, 180_090)):
                store.consume(
                    service_record(
                        ServiceRecordKind.VIDEO,
                        sequence,
                        dts,
                        f"video-{sequence}".encode(),
                        random_access=True,
                    )
                )
            store.finish()

            manifest = json.loads(store.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(len(manifest["gops"]), 2)
            self.assertEqual(manifest["earliest_available_ticks"], 90_000)
            self.assertEqual(manifest["latest_available_ticks"], 180_000)
            self.assertFalse(any(root.glob("gop-00000000000000000000.*")))

    def test_retires_evicted_files_one_manifest_generation_later(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "live"
            store = self.make_store(root, live_options(replay_window_seconds=1))
            for sequence, dts in enumerate((90, 90_090, 180_090, 270_090)):
                store.consume(
                    service_record(
                        ServiceRecordKind.VIDEO,
                        sequence,
                        dts,
                        f"video-{sequence}".encode(),
                        secondary=b"dependent",
                        random_access=True,
                    )
                )

            manifest = json.loads(store.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual([entry["sequence"] for entry in manifest["gops"]], [1, 2])
            self.assertTrue(any(root.glob("gop-00000000000000000000.*")))

            store.consume(
                service_record(
                    ServiceRecordKind.VIDEO,
                    4,
                    360_090,
                    b"video-4",
                    secondary=b"dependent",
                    random_access=True,
                )
            )

            self.assertFalse(any(root.glob("gop-00000000000000000000.*")))
            self.assertTrue(any(root.glob("gop-00000000000000000001.*")))
            store.cleanup()

    def test_rejects_unbounded_gop_duration_and_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            duration_store = self.make_store(
                Path(temporary_directory) / "duration",
                live_options(replay_window_seconds=1),
            )
            duration_store.consume(
                service_record(
                    ServiceRecordKind.VIDEO,
                    0,
                    90,
                    b"base",
                    secondary=b"dependent",
                    random_access=True,
                )
            )
            with self.assertRaisesRegex(SourceServiceError, "without another keyframe"):
                duration_store.consume(
                    service_record(
                        ServiceRecordKind.VIDEO,
                        1,
                        180_091,
                        b"base-two",
                        secondary=b"dependent-two",
                        random_access=True,
                    )
                )

            byte_store = self.make_store(
                Path(temporary_directory) / "bytes",
                live_options(replay_max_bytes=128),
            )
            with self.assertRaisesRegex(SourceServiceError, "byte limit"):
                byte_store.consume(
                    service_record(
                        ServiceRecordKind.VIDEO,
                        0,
                        90,
                        b"a" * 128,
                        secondary=b"b",
                        random_access=True,
                    )
                )
            duration_store.cleanup()
            byte_store.cleanup()

    def test_normalizes_33_bit_timestamp_wrap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = self.make_store(Path(temporary_directory) / "live")
            origin = (1 << 33) - 16
            store.consume(
                service_record(
                    ServiceRecordKind.VIDEO,
                    0,
                    origin,
                    b"base",
                    secondary=b"dependent",
                    random_access=True,
                )
            )
            store.consume(
                service_record(
                    ServiceRecordKind.VIDEO,
                    1,
                    16,
                    b"base-two",
                    secondary=b"dependent-two",
                )
            )
            store.finish()

            manifest = json.loads(store.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["latest_available_ticks"], 32)

    def test_requires_initial_random_access_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = self.make_store(Path(temporary_directory) / "live")

            with self.assertRaisesRegex(SourceServiceError, "begin at a replayable keyframe"):
                store.consume(service_record(ServiceRecordKind.VIDEO, 0, 90, b"video"))


class LiveSourceProtocolTests(unittest.TestCase):
    def request(self, source: Path, destination: Path) -> dict[str, object]:
        return live_request(source, destination)

    def test_parses_source_only_live_job(self) -> None:
        job = JobSpec.from_json_line(json.dumps(self.request(Path("/tmp/source.iso"), Path("/tmp/live-source"))))

        self.assertEqual(job.operation, WorkerOperation.START_LIVE_SOURCE)
        self.assertEqual(job.live_source.audio_pid, 0x1100)
        self.assertIsNone(job.encoding)
        self.assertIsNone(job.job)

    def test_accepts_playlist_zero(self) -> None:
        request = self.request(Path("/tmp/source.iso"), Path("/tmp/live-source"))
        live_source = request["live_source"]
        assert isinstance(live_source, dict)
        live_source["playlist"] = 0

        job = JobSpec.from_json_line(json.dumps(request))

        self.assertEqual(job.live_source.playlist, 0)

    def test_rejects_physical_disc_and_network_fields(self) -> None:
        request = self.request(Path("/tmp/source.iso"), Path("/tmp/live-source"))
        request["source"] = {"kind": "physical_disc", "path": "/dev/disk4"}
        with self.assertRaisesRegex(ValueError, "ISO image or Blu-ray folder"):
            JobSpec.from_json_line(json.dumps(request))

        request = self.request(Path("/tmp/source.iso"), Path("/tmp/live-source"))
        live_source = request["live_source"]
        assert isinstance(live_source, dict)
        live_source["session_id"] = "forbidden"
        with self.assertRaisesRegex(ValueError, "unsupported field"):
            JobSpec.from_json_line(json.dumps(request))

    def test_rejects_workspace_inside_blu_ray_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "disc"
            (source / "BDMV").mkdir(parents=True)
            destination = source / "live-source"
            request = self.request(source, destination)
            request["source"] = {"kind": "blu_ray_folder", "path": str(source)}
            job = JobSpec.from_json_line(json.dumps(request))
            activity = WorkerActivityReporter(WorkerEventEmitter(io.StringIO(), "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"))

            with self.assertRaises(WorkerOperationError) as raised:
                start_live_source(job, WorkerProcessOwner(), activity)

            self.assertEqual(raised.exception.code, "invalid_destination")

    def test_worker_emits_live_source_ready_and_terminal_result(self) -> None:
        request = self.request(Path("/tmp/source.iso"), Path("/tmp/live-source"))
        output = io.StringIO()

        def run_operation(job, owner, activity):
            self.assertEqual(job.operation, WorkerOperation.START_LIVE_SOURCE)
            self.assertFalse(owner.cancellation_event.is_set())
            activity.live_source_ready(
                {
                    "kind": "live_source_manifest",
                    "manifest_path": "/tmp/live-source/live-source.json",
                    "video_stream_id": "mvc",
                    "audio_stream_id": "audio:4352",
                }
            )
            return {
                "manifest_path": "/tmp/live-source/live-source.json",
                "video_stream_id": "mvc",
                "audio_stream_id": "audio:4352",
                "earliest_available_ticks": 0,
                "latest_available_ticks": 180_000,
                "retained_gops": 2,
            }

        exit_code = run_worker(
            io.StringIO(json.dumps(request) + "\n"),
            output,
            io.StringIO(),
            establish_session=False,
            heartbeat_interval=10,
            operation_runner=run_operation,
        )

        self.assertEqual(exit_code, 0)
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(
            [event["type"] for event in events],
            ["worker.ready", "job.started", "artifact.ready", "job.completed"],
        )
        self.assertEqual(events[2]["payload"]["live_source"]["video_stream_id"], "mvc")
        self.assertEqual(events[3]["payload"]["live_source_result"]["retained_gops"], 2)

    def test_live_source_completion_is_the_cancellation_commit_point(self) -> None:
        request = self.request(Path("/tmp/source.iso"), Path("/tmp/live-source"))
        output = io.StringIO()

        def run_operation(job, owner, activity):
            owner.cancellation_event.set()
            return {
                "manifest_path": "/tmp/live-source/live-source.json",
                "video_stream_id": "mvc",
                "audio_stream_id": "audio:4352",
                "earliest_available_ticks": 0,
                "latest_available_ticks": 180_000,
                "retained_gops": 2,
            }

        exit_code = run_worker(
            io.StringIO(json.dumps(request) + "\n"),
            output,
            io.StringIO(),
            establish_session=False,
            heartbeat_interval=10,
            operation_runner=run_operation,
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(output.getvalue().splitlines()[-1])["type"], "job.completed")


class SSIFLiveSourceServiceTests(unittest.TestCase):
    def make_probe(self, root: Path) -> Path:
        probe = root / "ssif_probe"
        shutil.copy2(FIXTURE_PROBE, probe)
        probe.chmod(probe.stat().st_mode | stat.S_IXUSR)
        return probe

    def make_activity(self) -> tuple[WorkerActivityReporter, io.StringIO]:
        output = io.StringIO()
        emitter = WorkerEventEmitter(output, "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        return WorkerActivityReporter(emitter), output

    def test_runs_helper_and_publishes_replay_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source.iso"
            source.touch()
            destination = root / "live"
            activity, output = self.make_activity()
            job = JobSpec.from_json_line(json.dumps(live_request(source, destination)))
            service = SSIFLiveSourceService(
                job,
                WorkerProcessOwner(),
                activity,
                probe_path=self.make_probe(root),
            )

            result = service.run()

            self.assertEqual(result["video_stream_id"], "mvc")
            self.assertEqual(result["audio_stream_id"], "audio:4352")
            manifest_path = Path(result["manifest_path"])
            self.assertTrue(manifest_path.is_file())
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertTrue(manifest["complete"])
            events = [json.loads(line) for line in output.getvalue().splitlines()]
            ready = next(event for event in events if event["type"] == "artifact.ready")
            self.assertEqual(ready["payload"]["live_source"]["kind"], "live_source_manifest")
            self.assertNotIn("session", json.dumps(ready))
            self.assertNotIn("network", json.dumps(ready))

    def test_completed_stream_is_not_removed_by_late_cancellation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source.iso"
            source.touch()
            destination = root / "live"
            owner = WorkerProcessOwner()
            activity, _ = self.make_activity()
            job = JobSpec.from_json_line(json.dumps(live_request(source, destination)))
            service = SSIFLiveSourceService(job, owner, activity, probe_path=root / "unused-probe")

            def complete_stream(store: ReplayStore) -> dict[str, object]:
                store.manifest_path.write_text("{}\n", encoding="utf-8")
                owner.cancellation_event.set()
                return {
                    "manifest_path": str(store.manifest_path),
                    "video_stream_id": "mvc",
                    "audio_stream_id": "audio:4352",
                    "earliest_available_ticks": 0,
                    "latest_available_ticks": 180_000,
                    "retained_gops": 2,
                }

            with (
                mock.patch.object(service, "_validate_tool"),
                mock.patch.object(service, "_inspect", return_value={}),
                mock.patch.object(
                    service,
                    "_stream_descriptors",
                    return_value=({"id": "mvc"}, {"id": "audio:4352"}),
                ),
                mock.patch.object(service, "_stream", side_effect=complete_stream),
            ):
                result = service.run()

            self.assertTrue(owner.cancellation_event.is_set())
            self.assertEqual(result["manifest_path"], str(destination / "live-source.json"))
            self.assertTrue((destination / "live-source.json").is_file())

    def test_primary_cancellation_wins_over_cleanup_timeout(self) -> None:
        cancellation = WorkerCancelled("cancelled")
        cleanup = SourceServiceError("source_cleanup_timeout", "cleanup timed out")

        with self.assertRaises(WorkerCancelled) as raised:
            SSIFLiveSourceService._raise_primary_or_cleanup(cancellation, cleanup)

        self.assertIs(raised.exception.__cause__, cleanup)

    def test_process_wait_retries_after_requesting_internal_cancellation(self) -> None:
        process_thread = mock.create_autospec(threading.Thread, instance=True)
        process_thread.is_alive.return_value = True
        internal_cancellation = threading.Event()

        cleanup_error = SSIFLiveSourceService._wait_for_process_thread(
            process_thread,
            internal_cancellation,
            initial_timeout=3,
        )

        self.assertTrue(internal_cancellation.is_set())
        self.assertEqual(process_thread.join.call_args_list, [mock.call(timeout=3), mock.call(timeout=10)])
        self.assertIsNotNone(cleanup_error)
        self.assertEqual(cleanup_error.code, "source_cleanup_timeout")

    def test_cancellation_terminates_helper_and_removes_partial_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "delay.iso"
            source.touch()
            destination = root / "live"
            owner = WorkerProcessOwner()
            activity, _ = self.make_activity()
            job = JobSpec.from_json_line(json.dumps(live_request(source, destination)))
            service = SSIFLiveSourceService(job, owner, activity, probe_path=self.make_probe(root))
            captured: list[BaseException] = []

            def run_service() -> None:
                try:
                    service.run()
                except BaseException as error:
                    captured.append(error)

            thread = threading.Thread(target=run_service)
            thread.start()
            deadline = time.monotonic() + 5
            while not destination.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            owner.cancellation_event.set()
            thread.join(timeout=5)

            self.assertFalse(thread.is_alive())
            self.assertEqual(len(captured), 1)
            self.assertIsInstance(captured[0], WorkerCancelled)
            self.assertFalse(destination.exists())

    def test_producer_failure_removes_partial_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "fail.iso"
            source.touch()
            destination = root / "live"
            activity, _ = self.make_activity()
            job = JobSpec.from_json_line(json.dumps(live_request(source, destination)))
            service = SSIFLiveSourceService(
                job,
                WorkerProcessOwner(),
                activity,
                probe_path=self.make_probe(root),
            )

            with self.assertRaises(SourceServiceError) as raised:
                service.run()

            self.assertEqual(raised.exception.code, "synthetic_producer_failure")
            self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
