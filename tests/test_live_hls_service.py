import io
import json
import os
import shutil
import stat
import tempfile
import threading
import time
import unittest
import unittest.mock as mock

from pathlib import Path

from bd_to_avp.worker import live_hls_service
from bd_to_avp.worker.__main__ import run_worker
from bd_to_avp.worker.live_hls_service import (
    HLSPublisher,
    SSIFLiveHLSService,
    parse_staged_playlist,
)
from bd_to_avp.worker.ownership import WorkerCancelled, WorkerProcessOwner
from bd_to_avp.worker.protocol import (
    LIVE_HLS_MINIMUM_RETENTION_BYTES,
    LIVE_HLS_MINIMUM_RETENTION_SECONDS,
    PROTOCOL_VERSION,
    JobSpec,
    WorkerActivityReporter,
    WorkerEventEmitter,
    WorkerOperation,
    WorkerProtocolError,
)
from bd_to_avp.worker.source_service import SourceServiceError


FIXTURES = Path(__file__).parent / "fixtures"
JOB_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def live_hls_request(source: Path, destination: Path, **options: int) -> dict[str, object]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "type": "job.start",
        "job_id": JOB_ID,
        "operation": "start_live_hls",
        "source": {"kind": "disc_image", "path": str(source)},
        "destination": {"path": str(destination)},
        "live_hls": {
            "playlist": 1005,
            "audio_pid": 0x1100,
            "retention_seconds": LIVE_HLS_MINIMUM_RETENTION_SECONDS,
            "retention_max_bytes": LIVE_HLS_MINIMUM_RETENTION_BYTES,
            **options,
        },
    }


class StagingEncoder:
    """Writes the encoder's private staging output the way the native encoder does."""

    def __init__(self, staging: Path, *, target_duration: int = 3) -> None:
        self.staging = staging
        self.target_duration = target_duration
        self.entries: list[tuple[str, float]] = []
        staging.mkdir(parents=True)
        (staging / "init.mp4").write_bytes(b"i" * 10)

    def segment(self, size: int, duration: float = 2.0) -> None:
        name = f"segment-{len(self.entries) + 1:05d}.m4s"
        (self.staging / name).write_bytes(b"s" * size)
        self.entries.append((name, duration))
        self._write_playlist(end=False)

    def end(self) -> None:
        self._write_playlist(end=True)

    def _write_playlist(self, *, end: bool) -> None:
        lines = ["#EXTM3U", f"#EXT-X-TARGETDURATION:{self.target_duration}", '#EXT-X-MAP:URI="init.mp4"']
        for name, duration in self.entries:
            lines.extend([f"#EXTINF:{duration:.6f},", name])
        if end:
            lines.append("#EXT-X-ENDLIST")
        (self.staging / "media.m3u8").write_text("\n".join(lines) + "\n", encoding="utf-8")


def playlist_uris(text: str) -> list[str]:
    return [line for line in text.splitlines() if line and not line.startswith("#")]


class HLSPublisherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name) / "live"
        self.root.mkdir()
        self.encoder = StagingEncoder(self.root / ".pipeline/encoder")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def publisher(self, *, seconds: int = 30, max_bytes: int = 1_000_000) -> HLSPublisher:
        return HLSPublisher(
            self.root,
            self.encoder.staging,
            retention_seconds=seconds,
            retention_max_bytes=max_bytes,
            streams={"video_stream": {"id": "mv-hevc"}},
        )

    def test_publishes_segments_before_the_playlist_that_names_them(self) -> None:
        publisher = self.publisher()
        original_write = live_hls_service._atomic_write
        observed: list[tuple[str, list[str]]] = []

        def checking_write(path: Path, text: str) -> None:
            if path.name == "media.m3u8":
                missing = [uri for uri in playlist_uris(text) if not (self.root / uri).is_file()]
                observed.append((text, missing))
            original_write(path, text)

        for _ in range(3):
            self.encoder.segment(100)
        with mock.patch.object(live_hls_service, "_atomic_write", side_effect=checking_write):
            self.assertTrue(publisher.poll())

        self.assertEqual(len(observed), 3)
        self.assertTrue(all(not missing for _, missing in observed))
        self.assertTrue((self.root / "init.mp4").is_file())
        self.assertEqual(sorted(path.name for path in self.encoder.staging.glob("*.m4s")), [])
        manifest = json.loads(publisher.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual([segment["uri"] for segment in manifest["segments"]], playlist_uris(observed[-1][0]))
        self.assertFalse(manifest["complete"])
        self.assertNotIn("#EXT-X-ENDLIST", publisher.playlist_path.read_text(encoding="utf-8"))

    def test_time_limit_evicts_whole_segments_after_republishing(self) -> None:
        publisher = self.publisher()
        original_write = live_hls_service._atomic_write
        evicted_still_present: list[bool] = []

        def checking_write(path: Path, text: str) -> None:
            if path.name == "media.m3u8" and "#EXT-X-MEDIA-SEQUENCE:0\n" not in text:
                evicted_still_present.append((self.root / "segment-000000000.m4s").exists())
            original_write(path, text)

        with mock.patch.object(live_hls_service, "_atomic_write", side_effect=checking_write):
            for _ in range(20):
                self.encoder.segment(100)
                publisher.poll()

        self.assertTrue(evicted_still_present[0], "evicted media was removed before the playlist dropped it")
        manifest = json.loads(publisher.manifest_path.read_text(encoding="utf-8"))
        retained = manifest["segments"]
        self.assertLessEqual(
            manifest["latest_available_microseconds"] - manifest["earliest_available_microseconds"],
            publisher.retention_microseconds,
        )
        self.assertEqual(manifest["earliest_available_microseconds"], retained[0]["start_microseconds"])
        self.assertEqual(manifest["media_sequence"], retained[0]["sequence"])
        self.assertGreater(manifest["media_sequence"], 0)
        self.assertIn(f"#EXT-X-MEDIA-SEQUENCE:{manifest['media_sequence']}\n", publisher.playlist_path.read_text())
        on_disk = sorted(path.name for path in self.root.glob("segment-*.m4s"))
        self.assertEqual(on_disk, [segment["uri"] for segment in retained])

    def test_byte_limit_evicts_and_keeps_earliest_position_honest(self) -> None:
        self.encoder.target_duration = 1
        publisher = self.publisher(seconds=3_600, max_bytes=10 + 4 * 1_000)
        for _ in range(10):
            self.encoder.segment(1_000, duration=1.0)
            publisher.poll()

        manifest = json.loads(publisher.manifest_path.read_text(encoding="utf-8"))
        self.assertLessEqual(manifest["retained_bytes"], publisher.retention_max_bytes)
        self.assertLessEqual(publisher.result()["peak_retained_bytes"], publisher.retention_max_bytes)
        self.assertEqual(manifest["retained_bytes"], sum(path.stat().st_size for path in self.root.glob("*.m4s")) + 10)
        self.assertEqual(len(manifest["segments"]), 4)
        self.assertEqual(manifest["earliest_available_microseconds"], 6_000_000)
        self.assertEqual(manifest["latest_available_microseconds"], 10_000_000)

    def test_rejects_segment_larger_than_byte_limit(self) -> None:
        publisher = self.publisher(max_bytes=500)
        self.encoder.segment(1_000)

        with self.assertRaises(SourceServiceError) as raised:
            publisher.poll()

        self.assertEqual(raised.exception.code, "live_hls_segment_too_large")

    def test_rejects_byte_limit_that_cannot_hold_three_target_durations(self) -> None:
        publisher = self.publisher(max_bytes=10 + 2 * 1_000)

        with self.assertRaises(SourceServiceError) as raised:
            for _ in range(5):
                self.encoder.segment(1_000)
                publisher.poll()

        self.assertEqual(raised.exception.code, "live_hls_retention_too_small")

    def test_encoder_endlist_completes_playlist_and_manifest(self) -> None:
        publisher = self.publisher()
        self.encoder.segment(100)
        publisher.poll()
        self.encoder.segment(100)
        self.encoder.end()

        publisher.finish()

        self.assertTrue(publisher.playlist_path.read_text(encoding="utf-8").endswith("#EXT-X-ENDLIST\n"))
        self.assertTrue(json.loads(publisher.manifest_path.read_text(encoding="utf-8"))["complete"])
        self.assertEqual(publisher.result()["published_segments"], 2)

    def test_finish_requires_encoder_endlist(self) -> None:
        publisher = self.publisher()
        self.encoder.segment(100)

        with self.assertRaises(SourceServiceError) as raised:
            publisher.finish()

        self.assertEqual(raised.exception.code, "incomplete_live_hls")

    def test_rejects_changed_target_duration(self) -> None:
        publisher = self.publisher()
        self.encoder.segment(100)
        publisher.poll()
        self.encoder.target_duration = 4
        self.encoder.segment(100)

        with self.assertRaises(SourceServiceError) as raised:
            publisher.poll()

        self.assertEqual(raised.exception.code, "invalid_encoder_playlist")

    def test_staged_playlist_parser_rejects_unsafe_or_inconsistent_entries(self) -> None:
        header = '#EXTM3U\n#EXT-X-TARGETDURATION:3\n#EXT-X-MAP:URI="init.mp4"\n'
        malformed = {
            "path traversal": header + "#EXTINF:2.0,\n../segment-00001.m4s\n",
            "segment without duration": header + "segment-00001.m4s\n",
            "segment over target": header + "#EXTINF:3.5,\nsegment-00001.m4s\n",
            "unexpected map": '#EXTM3U\n#EXT-X-TARGETDURATION:3\n#EXT-X-MAP:URI="other.mp4"\n',
            "media after endlist": header + "#EXT-X-ENDLIST\n#EXTINF:2.0,\nsegment-00001.m4s\n",
            "negative duration": header + "#EXTINF:-2.0,\nsegment-00001.m4s\n",
        }
        for label, text in malformed.items():
            with self.subTest(label), self.assertRaises(SourceServiceError):
                parse_staged_playlist(text)


class LiveHLSProtocolTests(unittest.TestCase):
    def test_parses_live_hls_job(self) -> None:
        job = JobSpec.from_json_line(json.dumps(live_hls_request(Path("/tmp/a.iso"), Path("/tmp/live"))))

        self.assertIs(job.operation, WorkerOperation.START_LIVE_HLS)
        assert job.live_hls is not None
        self.assertEqual(job.live_hls.retention_seconds, LIVE_HLS_MINIMUM_RETENTION_SECONDS)
        self.assertIsNone(job.live_source)

    def test_rejects_invalid_live_hls_requests(self) -> None:
        cases = {
            "retention below minimum": live_hls_request(
                Path("/tmp/a.iso"), Path("/tmp/live"), retention_seconds=LIVE_HLS_MINIMUM_RETENTION_SECONDS - 1
            ),
            "bytes below minimum": live_hls_request(
                Path("/tmp/a.iso"), Path("/tmp/live"), retention_max_bytes=LIVE_HLS_MINIMUM_RETENTION_BYTES - 1
            ),
            "unknown option": live_hls_request(Path("/tmp/a.iso"), Path("/tmp/live"), relay_port=1),
        }
        physical = live_hls_request(Path("/tmp/a.iso"), Path("/tmp/live"))
        physical["source"] = {"kind": "physical_disc", "path": "/dev/disk9"}
        cases["physical disc"] = physical
        for label, request in cases.items():
            with self.subTest(label), self.assertRaises(WorkerProtocolError):
                JobSpec.from_json_line(json.dumps(request))


class SSIFLiveHLSServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.tools = self.root / "tools"
        self.tools.mkdir()
        self.records = self.root / "records"
        self.records.mkdir()
        for name, fixture in (
            ("ssif_probe", "fake_ssif_probe.py"),
            ("edge264_test", "fake_live_hls_tools.py"),
            ("ffmpeg", "fake_live_hls_tools.py"),
            ("mv-hevc-encoder", "fake_live_hls_tools.py"),
        ):
            path = self.tools / name
            shutil.copy2(FIXTURES / fixture, path)
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
        environment = mock.patch.dict(os.environ, {"FAKE_LIVE_HLS_RECORD_DIR": str(self.records)})
        environment.start()
        self.addCleanup(environment.stop)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def service(
        self, source_name: str = "source.iso", **options: int
    ) -> tuple[SSIFLiveHLSService, WorkerProcessOwner, io.StringIO, Path]:
        source = self.root / source_name
        source.touch()
        destination = self.root / "live"
        output = io.StringIO()
        activity = WorkerActivityReporter(WorkerEventEmitter(output, JOB_ID))
        owner = WorkerProcessOwner()
        job = JobSpec.from_json_line(json.dumps(live_hls_request(source, destination, **options)))
        service = SSIFLiveHLSService(
            job,
            owner,
            activity,
            probe_path=self.tools / "ssif_probe",
            edge264_path=self.tools / "edge264_test",
            ffmpeg_path=self.tools / "ffmpeg",
            encoder_path=self.tools / "mv-hevc-encoder",
        )
        return service, owner, output, destination

    def test_one_source_read_feeds_video_and_audio_into_a_completed_workspace(self) -> None:
        service, _, output, destination = self.service()

        result = service.run()

        self.assertEqual((self.records / "video.bin").read_bytes(), b"base-zerodependent-zerobase-twodependent-two")
        self.assertEqual((self.records / "audio.bin").read_bytes(), b"audio-zeroaudio-one")
        arguments = json.loads((self.records / "encoder-arguments.json").read_text())
        self.assertEqual(arguments[arguments.index("--audio-start-ticks") + 1], "-10")
        manifest = json.loads(Path(str(result["manifest_path"])).read_text(encoding="utf-8"))
        self.assertTrue(manifest["complete"])
        self.assertEqual(manifest["audio_stream"]["source_pid"], 0x1100)
        self.assertEqual(
            sorted(path.name for path in destination.iterdir()),
            sorted(["init.mp4", "live-hls.json", "media.m3u8", *[segment["uri"] for segment in manifest["segments"]]]),
        )
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        ready = [event for event in events if event["type"] == "artifact.ready"]
        self.assertEqual(len(ready), 1)
        self.assertEqual(ready[0]["payload"]["live_hls"]["kind"], "live_hls_manifest")

    def test_bounded_run_drops_trailing_partial_gop(self) -> None:
        service, _, _, _ = self.service("bounded.iso", maximum_pairs=4)

        service.run()

        self.assertEqual((self.records / "video.bin").read_bytes(), b"base-zerodependent-zerobase-onedependent-one")

    def test_cancellation_stops_every_child_and_removes_workspace(self) -> None:
        service, owner, _, destination = self.service("delay.iso")
        captured: list[BaseException] = []

        def run_service() -> None:
            try:
                service.run()
            except BaseException as error:
                captured.append(error)

        thread = threading.Thread(target=run_service)
        thread.start()
        deadline = time.monotonic() + 10
        while not (self.records / "encoder.pid").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        encoder_pid = int((self.records / "encoder.pid").read_text())
        owner.cancellation_event.set()
        thread.join(timeout=20)

        self.assertFalse(thread.is_alive())
        self.assertEqual(len(captured), 1)
        self.assertIsInstance(captured[0], WorkerCancelled)
        self.assertFalse(destination.exists())
        self.assertTrue(all(child.done.is_set() for child in service.children))
        with self.assertRaises(ProcessLookupError):
            os.kill(encoder_pid, 0)

    def test_failures_remove_the_workspace_with_the_failing_stage_code(self) -> None:
        cases = {
            "fail.iso": ({}, "synthetic_producer_failure"),
            "encoder.iso": ({"FAKE_LIVE_HLS_ENCODER_FAIL": "1"}, "live_video_encode_failed"),
            "lpcm.iso": ({}, "unsupported_live_audio"),
            "interlaced.iso": ({}, "unsupported_live_video"),
            "noaudio.iso": ({}, "selected_audio_unavailable"),
            "nokey.iso": ({}, "missing_initial_keyframe"),
        }
        for source_name, (environment, code) in cases.items():
            with self.subTest(source_name), mock.patch.dict(os.environ, environment):
                service, _, _, destination = self.service(source_name)

                with self.assertRaises(SourceServiceError) as raised:
                    service.run()

                self.assertEqual(raised.exception.code, code)
                self.assertFalse(destination.exists())

    def test_worker_reports_live_hls_result(self) -> None:
        source = self.root / "source.iso"
        source.touch()
        destination = self.root / "live"
        tool_environment = {
            "BD_TO_AVP_SSIF_PROBE_PATH": str(self.tools / "ssif_probe"),
            "BD_TO_AVP_EDGE264_TEST_PATH": str(self.tools / "edge264_test"),
            "BD_TO_AVP_FFMPEG_PATH": str(self.tools / "ffmpeg"),
            "BD_TO_AVP_MV_HEVC_ENCODER_PATH": str(self.tools / "mv-hevc-encoder"),
        }
        output = io.StringIO()
        with mock.patch.dict(os.environ, tool_environment):
            exit_code = run_worker(
                io.StringIO(json.dumps(live_hls_request(source, destination)) + "\n"),
                output,
                io.StringIO(),
            )

        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(exit_code, 0)
        completed = events[-1]
        self.assertEqual(completed["type"], "job.completed")
        self.assertTrue(Path(completed["payload"]["live_hls_result"]["playlist_path"]).is_file())


if __name__ == "__main__":
    unittest.main()
