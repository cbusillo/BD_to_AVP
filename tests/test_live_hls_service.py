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

from dataclasses import replace
from pathlib import Path
from typing import Callable, ClassVar

from bd_to_avp.observability import ObservabilityEmitter
from bd_to_avp.runtime import ObservabilityStream, RunContext
from bd_to_avp.worker import live_hls_service
from bd_to_avp.worker.__main__ import run_worker
from bd_to_avp.worker.live_hls_service import (
    HLSPublisher,
    SSIFLiveHLSService,
    alternate_audio_filter,
    parse_staged_playlist,
)
from bd_to_avp.worker.ownership import WorkerCancelled, WorkerProcessOwner
from bd_to_avp.worker.protocol import (
    LIVE_HLS_MAXIMUM_ALTERNATE_AUDIO,
    LIVE_HLS_MINIMUM_RETENTION_BYTES,
    LIVE_HLS_MINIMUM_RETENTION_SECONDS,
    LIVE_HLS_MINIMUM_VIEWER_LEAD_SECONDS,
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


def live_hls_request(
    source: Path, destination: Path, *, source_identity: object = None, **options: object
) -> dict[str, object]:
    source_object: dict[str, object] = {"kind": "disc_image", "path": str(source)}
    if source_identity is not None:
        source_object["identity"] = source_identity
    return {
        "protocol_version": PROTOCOL_VERSION,
        "type": "job.start",
        "job_id": JOB_ID,
        "operation": "start_live_hls",
        "source": source_object,
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
        self.now = 1_000_000.0

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def publisher(self, *, seconds: int = 30, max_bytes: int = 1_000_000) -> HLSPublisher:
        return HLSPublisher(
            self.root,
            self.encoder.staging,
            retention_seconds=seconds,
            retention_max_bytes=max_bytes,
            streams={"video_stream": {"id": "mv-hevc"}},
            clock=lambda: self.now,
        )

    def on_disk(self) -> list[str]:
        return sorted(path.name for path in self.root.glob("segment-*.m4s"))

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

    def test_time_limit_evicts_whole_segments_and_retires_them(self) -> None:
        publisher = self.publisher()
        for _ in range(20):
            self.encoder.segment(100)
            publisher.poll()

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
        self.assertEqual(playlist_uris(publisher.playlist_path.read_text()), [segment["uri"] for segment in retained])
        retired = [segment["uri"] for segment in manifest["retired_segments"]]
        self.assertEqual(len(retired), manifest["media_sequence"])
        self.assertEqual(self.on_disk(), sorted(retired + [segment["uri"] for segment in retained]))

    def test_byte_limit_evicts_and_keeps_earliest_position_honest(self) -> None:
        self.encoder.target_duration = 1
        publisher = self.publisher(seconds=3_600, max_bytes=10 + 4 * 1_000)
        for _ in range(10):
            self.encoder.segment(1_000, duration=1.0)
            publisher.poll()

        manifest = json.loads(publisher.manifest_path.read_text(encoding="utf-8"))
        self.assertLessEqual(manifest["retained_bytes"], publisher.retention_max_bytes)
        self.assertLessEqual(publisher.result()["peak_retained_bytes"], publisher.retention_max_bytes)
        retained_uris = [segment["uri"] for segment in manifest["segments"]]
        self.assertEqual(
            manifest["retained_bytes"], sum((self.root / uri).stat().st_size for uri in retained_uris) + 10
        )
        self.assertEqual(len(manifest["segments"]), 4)
        self.assertEqual(manifest["earliest_available_microseconds"], 6_000_000)
        self.assertEqual(manifest["latest_available_microseconds"], 10_000_000)
        self.assertLessEqual(manifest["retired_bytes"], manifest["retired_allowance_bytes"])

    def test_segment_from_previous_playlist_stays_readable_for_its_grace_period(self) -> None:
        publisher = self.publisher()
        previous_playlist = ""
        for _ in range(50):
            previous_playlist = publisher.playlist_path.read_text() if publisher.has_published else ""
            self.encoder.segment(100)
            publisher.poll()
            if publisher.retired:
                break
        self.assertTrue(publisher.retired, "the time limit never evicted a segment")
        evicted = publisher.retired[0]
        self.assertIn(evicted.uri, playlist_uris(previous_playlist))
        self.assertNotIn(evicted.uri, playlist_uris(publisher.playlist_path.read_text()))
        grace_seconds = evicted.delete_after_unix - self.now
        self.assertGreaterEqual(grace_seconds, publisher.retention_seconds)

        self.now += grace_seconds - 0.001
        publisher.poll()
        self.assertEqual((self.root / evicted.uri).read_bytes(), b"s" * 100)

        self.now += 0.002
        publisher.poll()
        self.assertFalse((self.root / evicted.uri).exists())
        manifest = json.loads(publisher.manifest_path.read_text(encoding="utf-8"))
        self.assertNotIn(evicted.uri, [segment["uri"] for segment in manifest["retired_segments"]])

    def test_publication_holds_instead_of_deleting_retired_media_early(self) -> None:
        self.encoder.target_duration = 1
        publisher = self.publisher(seconds=3_600, max_bytes=10 + 4 * 1_000)
        for _ in range(30):
            self.encoder.segment(1_000, duration=1.0)
            publisher.poll()

        self.assertTrue(publisher.held_for_grace)
        manifest = json.loads(publisher.manifest_path.read_text(encoding="utf-8"))
        retired = list(publisher.retired)
        self.assertLessEqual(manifest["retired_bytes"], manifest["retired_allowance_bytes"])
        self.assertTrue(all((self.root / segment.uri).is_file() for segment in retired))
        self.assertEqual(publisher.staged_backlog, 30 - publisher.next_sequence)
        self.assertEqual(len(list(self.encoder.staging.glob("*.m4s"))), publisher.staged_backlog)
        held_playlist = publisher.playlist_path.read_text(encoding="utf-8")
        publisher.poll()
        self.assertEqual(publisher.playlist_path.read_text(encoding="utf-8"), held_playlist)

        self.now = retired[0].delete_after_unix - 0.001
        publisher.poll()
        self.assertTrue((self.root / retired[0].uri).is_file())
        self.assertEqual(publisher.playlist_path.read_text(encoding="utf-8"), held_playlist)

        self.now = retired[0].delete_after_unix
        published_before = publisher.next_sequence
        publisher.poll()
        self.assertFalse((self.root / retired[0].uri).exists())
        self.assertGreater(publisher.next_sequence, published_before)
        unexpired = [segment for segment in retired if segment.delete_after_unix > self.now]
        self.assertTrue(all((self.root / segment.uri).is_file() for segment in unexpired))

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

    def test_alternate_rendition_gates_the_manifest_and_completion(self) -> None:
        publisher = self.publisher()
        alternate_staging = StagingEncoder(self.root / ".pipeline/audio-4353", target_duration=2)
        publisher.add_alternate(
            HLSPublisher(
                self.root,
                alternate_staging.staging,
                retention_seconds=30,
                retention_max_bytes=1_000_000,
                streams={"source_pid": 4353},
                clock=lambda: self.now,
                name_prefix="audio-4353-",
            )
        )
        self.encoder.segment(100)
        publisher.poll()
        self.assertFalse(publisher.has_published)
        self.assertFalse(publisher.manifest_path.exists(), "the viewer must not see a rendition without media")

        alternate_staging.segment(10, 1.92)
        publisher.poll()
        manifest = json.loads(publisher.manifest_path.read_text(encoding="utf-8"))
        (rendition,) = manifest["audio_renditions"]
        self.assertEqual(rendition["source_pid"], 4353)
        self.assertEqual(rendition["initialization"], "audio-4353-init.mp4")
        self.assertEqual([segment["uri"] for segment in rendition["segments"]], ["audio-4353-segment-000000000.m4s"])
        self.assertTrue((self.root / "audio-4353-segment-000000000.m4s").exists())
        self.assertTrue((self.root / "audio-4353-init.mp4").exists())
        self.assertNotIn("audio-4353", publisher.playlist_path.read_text(encoding="utf-8"))

        self.encoder.end()
        publisher.poll()
        self.assertFalse(publisher.complete, "the stream completes only when every rendition has")
        alternate_staging.end()
        publisher.finish()
        manifest = json.loads(publisher.manifest_path.read_text(encoding="utf-8"))
        self.assertTrue(manifest["complete"])
        self.assertTrue(manifest["audio_renditions"][0]["complete"])

    def test_alternate_audio_starts_on_the_video_timeline(self) -> None:
        self.assertEqual(alternate_audio_filter(9_000), "aresample=48000,adelay=delays=4800S:all=1")
        self.assertEqual(alternate_audio_filter(0), "aresample=48000,adelay=delays=0S:all=1")
        self.assertEqual(alternate_audio_filter(-9_000), "aresample=48000,atrim=start_sample=4800,asetpts=PTS-STARTPTS")

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

    def test_parses_shared_mac_start_fixture(self) -> None:
        fixture = FIXTURES / f"native_worker_start_live_hls_v{PROTOCOL_VERSION}.json"
        raw = json.loads(fixture.read_text(encoding="utf-8"))

        job = JobSpec.from_json_line(fixture.read_text(encoding="utf-8"))

        assert job.live_hls is not None and job.source.identity is not None
        assert job.source.descriptor_handoff is not None
        self.assertEqual(job.live_hls.viewer_lead_seconds, raw["live_hls"]["viewer_lead_seconds"])
        self.assertEqual(job.source.identity.inode, raw["source"]["identity"]["inode"])
        self.assertEqual(
            job.source.identity.modification_nanoseconds,
            raw["source"]["identity"]["modification_nanoseconds"],
        )
        self.assertEqual(job.source.descriptor_handoff.token, raw["source"]["descriptor_handoff"]["token"])
        self.assertEqual(
            str(job.source.descriptor_handoff.socket_path), raw["source"]["descriptor_handoff"]["socket_path"]
        )

    def test_parses_alternate_audio_pids(self) -> None:
        request = live_hls_request(Path("/tmp/a.iso"), Path("/tmp/live"), alternate_audio_pids=[0x1101, 0x1102])

        job = JobSpec.from_json_line(json.dumps(request))

        assert job.live_hls is not None
        self.assertEqual(job.live_hls.alternate_audio_pids, (0x1101, 0x1102))
        default = JobSpec.from_json_line(json.dumps(live_hls_request(Path("/tmp/a.iso"), Path("/tmp/live"))))
        assert default.live_hls is not None
        self.assertEqual(default.live_hls.alternate_audio_pids, ())

    def test_rejects_invalid_alternate_audio_pids(self) -> None:
        cases = {
            "selected pid": [0x1100],
            "duplicate": [0x1101, 0x1101],
            "out of range": [0],
            "not an integer": ["4353"],
            "boolean": [True],
            "too many": list(range(0x1101, 0x1101 + LIVE_HLS_MAXIMUM_ALTERNATE_AUDIO + 1)),
            "not a list": 0x1101,
        }
        for label, pids in cases.items():
            request = live_hls_request(Path("/tmp/a.iso"), Path("/tmp/live"), alternate_audio_pids=pids)
            with self.subTest(label), self.assertRaises(WorkerProtocolError):
                JobSpec.from_json_line(json.dumps(request))

    def test_rejects_invalid_live_hls_requests(self) -> None:
        cases = {
            "retention below minimum": live_hls_request(
                Path("/tmp/a.iso"), Path("/tmp/live"), retention_seconds=LIVE_HLS_MINIMUM_RETENTION_SECONDS - 1
            ),
            "bytes below minimum": live_hls_request(
                Path("/tmp/a.iso"), Path("/tmp/live"), retention_max_bytes=LIVE_HLS_MINIMUM_RETENTION_BYTES - 1
            ),
            "unknown option": live_hls_request(Path("/tmp/a.iso"), Path("/tmp/live"), relay_port=1),
            "lead below minimum": live_hls_request(
                Path("/tmp/a.iso"), Path("/tmp/live"), viewer_lead_seconds=LIVE_HLS_MINIMUM_VIEWER_LEAD_SECONDS - 1
            ),
            "lead above half the window": live_hls_request(
                Path("/tmp/a.iso"),
                Path("/tmp/live"),
                retention_seconds=60,
                viewer_lead_seconds=31,
            ),
            "identity missing a field": live_hls_request(
                Path("/tmp/a.iso"), Path("/tmp/live"), source_identity={"device": 1, "inode": 2}
            ),
            "identity with a negative inode": live_hls_request(
                Path("/tmp/a.iso"),
                Path("/tmp/live"),
                source_identity={
                    "device": 1,
                    "inode": -2,
                    "size_bytes": 3,
                    "modification_seconds": 4,
                    "modification_nanoseconds": 5,
                },
            ),
        }
        physical = live_hls_request(Path("/tmp/a.iso"), Path("/tmp/live"))
        physical["source"] = {"kind": "physical_disc", "path": "/dev/disk9"}
        cases["physical disc"] = physical
        removed = live_hls_request(Path("/tmp/a.iso"), Path("/tmp/live"))
        assert isinstance(removed["live_hls"], dict)
        removed["live_hls"]["source_identity"] = {
            "device": 1,
            "inode": 2,
            "size_bytes": 3,
            "modification_seconds": 4,
            "modification_nanoseconds": 5,
        }
        cases["identity under live_hls"] = removed
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
        self,
        source_name: str = "source.iso",
        *,
        retention_max_bytes: int | None = None,
        clock: Callable[[], float] = time.time,
        viewer_position: Callable[[], int | None] | None = None,
        **options: object,
    ) -> tuple[SSIFLiveHLSService, WorkerProcessOwner, io.StringIO, Path]:
        source = self.root / source_name
        if not source.exists(follow_symlinks=False):
            source.touch()
        destination = self.root / "live"
        output = io.StringIO()
        run_context = RunContext(
            observability=ObservabilityStream(ObservabilityEmitter.WORKER), viewer_position=viewer_position
        )
        activity = WorkerActivityReporter(WorkerEventEmitter(output, JOB_ID), run_context)
        owner = WorkerProcessOwner()
        job = JobSpec.from_json_line(json.dumps(live_hls_request(source, destination, **options)))
        if retention_max_bytes is not None:
            assert job.live_hls is not None
            # Below the protocol minimum so a few small fake segments exercise eviction.
            job = replace(job, live_hls=replace(job.live_hls, retention_max_bytes=retention_max_bytes))
        service = SSIFLiveHLSService(
            job,
            owner,
            activity,
            probe_path=self.tools / "ssif_probe",
            edge264_path=self.tools / "edge264_test",
            ffmpeg_path=self.tools / "ffmpeg",
            encoder_path=self.tools / "mv-hevc-encoder",
            clock=clock,
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

    def test_alternate_tracks_become_renditions_from_the_same_source_read(self) -> None:
        service, _, output, destination = self.service("alternates.iso", alternate_audio_pids=[0x1101, 0x1102])

        result = service.run()

        probe_arguments = json.loads((self.records / "probe-arguments.json").read_text())
        self.assertEqual(probe_arguments[3], "4352,4353", "the LPCM track 4354 is never requested")
        self.assertEqual((self.records / "audio-4353.bin").read_bytes(), b"other-zeroother-one")
        self.assertEqual((self.records / "audio.bin").read_bytes(), b"audio-zeroaudio-one")
        alternate_arguments = json.loads((self.records / "audio-4353-arguments.json").read_text())
        self.assertEqual(alternate_arguments[alternate_arguments.index("-af") + 1], alternate_audio_filter(-1))
        manifest = json.loads(Path(str(result["manifest_path"])).read_text(encoding="utf-8"))
        self.assertTrue(manifest["complete"])
        (rendition,) = manifest["audio_renditions"]
        self.assertEqual(rendition["source_pid"], 0x1101)
        # The fake video encoder targets 3 s and the audio 2 s; one multivariant playlist needs one target.
        self.assertEqual(rendition["target_duration_seconds"], manifest["target_duration_seconds"])
        self.assertIn(
            f"#EXT-X-TARGETDURATION:{manifest['target_duration_seconds']}\n",
            Path(str(result["playlist_path"])).read_text(),
        )
        self.assertEqual(result["encoder_summary"]["output_mode"], "hls", "the summary is the video encoder's")
        self.assertEqual(rendition["source_language"], "deu")
        published = {path.name for path in destination.iterdir()}
        self.assertIn(rendition["initialization"], published)
        self.assertTrue({segment["uri"] for segment in rendition["segments"]} <= published)
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len([event for event in events if event["type"] == "artifact.ready"]), 1)
        warnings = [event["payload"] for event in events if event["type"] == "warning"]
        self.assertEqual([warning["code"] for warning in warnings], ["unsupported_live_alternate_audio"])
        self.assertEqual(warnings[0]["details"], "pid 4354")

    def test_video_keeps_publishing_after_an_alternate_track_finishes(self) -> None:
        with mock.patch.dict(os.environ, {"FAKE_LIVE_HLS_TAIL_CHECK": "1"}):
            service, _, _, _ = self.service("alternates.iso", alternate_audio_pids=[0x1101])
            result = service.run()

        self.assertEqual((self.records / "tail-published").read_text(), "True")
        self.assertEqual(result["published_segments"], 2)
        self.assertEqual(result["encoder_summary"]["output_mode"], "hls")

    def test_alternate_track_that_starts_late_is_left_out(self) -> None:
        service, _, output, _ = self.service("latealternates.iso", alternate_audio_pids=[0x1101])

        result = service.run()

        manifest = json.loads(Path(str(result["manifest_path"])).read_text(encoding="utf-8"))
        self.assertTrue(manifest["complete"])
        self.assertNotIn("audio_renditions", manifest)
        self.assertFalse((self.records / "audio-4353.bin").exists())
        warnings = [json.loads(line)["payload"] for line in output.getvalue().splitlines()]
        self.assertIn("live_alternate_audio_unavailable", [warning.get("code") for warning in warnings])

    def test_bounded_run_drops_trailing_partial_gop(self) -> None:
        service, _, _, _ = self.service("bounded.iso", maximum_pairs=4)

        service.run()

        self.assertEqual((self.records / "video.bin").read_bytes(), b"base-zerodependent-zerobase-onedependent-one")

    def test_cancellation_stops_every_child_and_removes_workspace(self) -> None:
        service, owner, _, destination = self.service("delay.iso")
        captured: list[Exception] = []

        def run_service() -> None:
            try:
                service.run()
            except Exception as error:
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

    def run_in_thread(self, service: SSIFLiveHLSService) -> tuple[threading.Thread, list[Exception]]:
        captured: list[Exception] = []

        def run_service() -> None:
            try:
                service.run()
            except Exception as error:
                captured.append(error)

        thread = threading.Thread(target=run_service)
        thread.start()
        return thread, captured

    def test_segments_publish_while_the_source_is_still_being_read(self) -> None:
        with mock.patch.dict(os.environ, {"FAKE_LIVE_HLS_MODE": "streaming"}):
            service, owner, output, _ = self.service("slow.iso")
            thread, _ = self.run_in_thread(service)
            deadline = time.monotonic() + 10
            while '"live_hls"' not in output.getvalue() and time.monotonic() < deadline:
                time.sleep(0.02)
            source_running = not service.children[0].done.is_set()
            owner.cancellation_event.set()
            thread.join(timeout=20)

        self.assertIn('"live_hls"', output.getvalue())
        self.assertTrue(source_running)

    def test_encoder_that_stops_publishing_fails_before_source_end(self) -> None:
        with mock.patch.dict(os.environ, {"FAKE_LIVE_HLS_MODE": "stall"}):
            service, _, _, destination = self.service("slow.iso")
            service.no_progress_seconds = 1
            started = time.monotonic()
            thread, captured = self.run_in_thread(service)
            thread.join(timeout=20)
            elapsed = time.monotonic() - started

        self.assertFalse(thread.is_alive())
        self.assertLess(elapsed, 15, "the stall waited for the delayed source to end")
        self.assertEqual(len(captured), 1)
        self.assertIsInstance(captured[0], SourceServiceError)
        self.assertEqual(captured[0].code, "live_hls_pipeline_stalled")
        self.assertFalse(destination.exists())
        self.assertTrue(all(child.done.is_set() for child in service.children))
        with self.assertRaises(ProcessLookupError):
            os.kill(int((self.records / "encoder.pid").read_text()), 0)

    MEDIA_ENCODER: ClassVar[dict[str, str]] = {
        "FAKE_LIVE_HLS_MODE": "media",
        "FAKE_LIVE_HLS_FRAMES_PER_SEGMENT": "48",
        "FAKE_LIVE_HLS_SEGMENTS": "2.0:1000",
    }
    # Five two-second segments of 1000 bytes plus the four-byte fake init segment.
    WINDOW_BYTES = 5 * 1_000 + 4

    def peak_staged(self) -> int:
        return int((self.records / "peak-staged").read_text())

    def test_grace_hold_caps_staging_keeps_watchdogs_quiet_and_still_cancels(self) -> None:
        with mock.patch.dict(os.environ, self.MEDIA_ENCODER):
            service, owner, _, destination = self.service(
                "long.iso", retention_max_bytes=self.WINDOW_BYTES, clock=lambda: 1_000.0
            )
            service.no_progress_seconds = 1
            thread, captured = self.run_in_thread(service)
            deadline = time.monotonic() + 15
            while not (service.publisher and service.publisher.held_for_grace) and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(service.publisher and service.publisher.held_for_grace)
            time.sleep(3 * service.no_progress_seconds)
            still_running = thread.is_alive() and not captured
            source_blocked = not service.children[0].done.is_set()
            cancel_started = time.monotonic()
            owner.cancellation_event.set()
            thread.join(timeout=10)
            cancel_seconds = time.monotonic() - cancel_started

        self.assertTrue(still_running, captured)
        self.assertTrue(source_blocked, "the source was read to the end instead of being held")
        self.assertLess(cancel_seconds, 5)
        self.assertEqual(len(captured), 1)
        self.assertIsInstance(captured[0], WorkerCancelled)
        self.assertFalse(destination.exists())
        self.assertTrue(all(child.done.is_set() for child in service.children))
        self.assertLessEqual(self.peak_staged(), live_hls_service.STAGING_SEGMENT_CAP)
        self.assertGreater(service.held_seconds, 0)

    def test_held_stream_completes_once_retired_media_expires(self) -> None:
        with mock.patch.dict(os.environ, self.MEDIA_ENCODER):
            # Grace periods pass ten times faster than real time; the fake encoder is faster still.
            service, _, _, _ = self.service(
                "long.iso", retention_max_bytes=self.WINDOW_BYTES, clock=lambda: time.monotonic() * 10
            )

            result = service.run()

        manifest = json.loads(Path(str(result["manifest_path"])).read_text(encoding="utf-8"))
        self.assertTrue(manifest["complete"])
        self.assertEqual(result["published_segments"], 20)
        self.assertGreater(result["grace_holds"], 0)
        self.assertLessEqual(self.peak_staged(), live_hls_service.STAGING_SEGMENT_CAP)
        self.assertLessEqual(result["peak_retired_bytes"], manifest["retired_allowance_bytes"])

    @staticmethod
    def wait_for(condition: Callable[[], bool], seconds: float = 15) -> bool:
        deadline = time.monotonic() + seconds
        while not condition() and time.monotonic() < deadline:
            time.sleep(0.02)
        return condition()

    @staticmethod
    def viewer_held(service: SSIFLiveHLSService) -> bool:
        return service.publisher is not None and service._held_for_viewer() and service.viewer_held_seconds > 0

    def test_viewer_lead_holds_production_until_the_viewer_advances(self) -> None:
        position: list[int | None] = [None]
        with mock.patch.dict(os.environ, self.MEDIA_ENCODER):
            service, _, _, destination = self.service(
                "long.iso", viewer_position=lambda: position[0], viewer_lead_seconds=10, clock=lambda: 1_000.0
            )
            service.no_progress_seconds = 1
            started = time.monotonic()
            thread, captured = self.run_in_thread(service)
            self.assertTrue(self.wait_for(lambda: self.viewer_held(service)))
            # Several no-progress deadlines pass while the viewer does not move.
            time.sleep(3 * service.no_progress_seconds)
            assert service.publisher is not None
            held_edge = service.publisher.next_start_microseconds
            held_alive = thread.is_alive() and not captured
            source_blocked = not service.children[0].done.is_set()
            held_manifest = json.loads((destination / "live-hls.json").read_text(encoding="utf-8"))
            position[0] = 30_000_000
            thread.join(timeout=20)
            elapsed = time.monotonic() - started

        self.assertTrue(held_alive, captured)
        self.assertTrue(source_blocked, "the source was read to the end instead of being held")
        self.assertGreaterEqual(held_edge, 10_000_000)
        # The hold engages at the lead; at most the staged budget and one GOP publish after it.
        self.assertLess(held_edge, 20_000_000)
        self.assertLess(held_manifest["latest_available_microseconds"], 20_000_000)
        self.assertFalse(thread.is_alive())
        self.assertEqual(captured, [])
        manifest = json.loads((destination / "live-hls.json").read_text(encoding="utf-8"))
        self.assertTrue(manifest["complete"])
        self.assertGreater(manifest["latest_available_microseconds"], 39_000_000)
        producer = manifest["producer"]
        self.assertEqual(producer["produced_microseconds"], manifest["latest_available_microseconds"])
        self.assertGreaterEqual(producer["viewer_held_seconds"], 2 * service.no_progress_seconds)
        self.assertGreaterEqual(producer["held_seconds"], producer["viewer_held_seconds"])
        self.assertGreaterEqual(producer["wall_seconds"], producer["held_seconds"])
        self.assertLessEqual(producer["wall_seconds"], elapsed)

    def test_paused_viewer_hold_still_cancels_promptly(self) -> None:
        with mock.patch.dict(os.environ, self.MEDIA_ENCODER):
            service, owner, _, destination = self.service(
                "long.iso", viewer_position=lambda: 0, viewer_lead_seconds=10, clock=lambda: 1_000.0
            )
            thread, captured = self.run_in_thread(service)
            self.assertTrue(self.wait_for(lambda: self.viewer_held(service)))
            cancel_started = time.monotonic()
            owner.cancellation_event.set()
            thread.join(timeout=10)
            cancel_seconds = time.monotonic() - cancel_started

        self.assertLess(cancel_seconds, 5)
        self.assertEqual(len(captured), 1)
        self.assertIsInstance(captured[0], WorkerCancelled)
        self.assertFalse(destination.exists())

    def test_viewer_hold_and_grace_hold_both_release(self) -> None:
        position: list[int | None] = [None]

        def advancing_viewer() -> int | None:
            # A viewer that stays ten seconds behind whatever has been published.
            assert service.publisher is not None
            position[0] = max(0, service.publisher.next_start_microseconds - 9_000_000)
            return position[0]

        with mock.patch.dict(os.environ, self.MEDIA_ENCODER):
            service, _, _, _ = self.service(
                "long.iso",
                retention_max_bytes=self.WINDOW_BYTES,
                viewer_position=advancing_viewer,
                viewer_lead_seconds=10,
                clock=lambda: time.monotonic() * 10,
            )

            result = service.run()

        self.assertEqual(result["published_segments"], 20)
        self.assertGreater(result["grace_holds"], 0)

    def test_source_identity_must_match_the_approved_file(self) -> None:
        source = self.root / "source.iso"
        source.write_bytes(b"disc")
        info = source.stat()
        identity = {
            "device": info.st_dev & 0xFFFF_FFFF,
            "inode": info.st_ino,
            "size_bytes": info.st_size,
            "modification_seconds": info.st_mtime_ns // 1_000_000_000,
            "modification_nanoseconds": info.st_mtime_ns % 1_000_000_000,
        }
        service, _, _, approved_workspace = self.service(source_identity=identity)
        self.assertTrue(service.run()["retained_segments"])
        shutil.rmtree(approved_workspace)

        cases = {
            "modified": {**identity, "modification_nanoseconds": (identity["modification_nanoseconds"] + 1) % 10**9},
            "replaced": {**identity, "inode": identity["inode"] + 1},
            "resized": {**identity, "size_bytes": identity["size_bytes"] + 1},
        }
        for label, changed in cases.items():
            with self.subTest(label):
                service, _, _, destination = self.service(source_identity=changed)
                with self.assertRaises(SourceServiceError) as raised:
                    service.run()
                self.assertEqual(raised.exception.code, "live_source_changed")
                self.assertFalse(destination.exists())
                self.assertEqual(service.children, [], "the source was read after the identity check failed")

        link = self.root / "link.iso"
        link.symlink_to(source)
        service, _, _, _ = self.service("link.iso", source_identity=identity)
        with self.assertRaises(SourceServiceError) as raised:
            service.run()
        self.assertEqual(raised.exception.code, "live_source_changed")

    def test_failures_remove_the_workspace_with_the_failing_stage_code(self) -> None:
        cases = {
            "fail.iso": ({}, "synthetic_producer_failure"),
            "latefail.iso": ({}, "synthetic_producer_failure"),
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
        # The shared fixtures the Mac decodes carry exactly the keys the worker emits.
        ready = next(event for event in events if event["type"] == "artifact.ready")
        for event, family in ((ready, "live_hls_ready"), (completed, "live_hls_completed")):
            fixture = json.loads((FIXTURES / f"native_worker_{family}_v{PROTOCOL_VERSION}.json").read_text())
            self.assertEqual(set(event), set(fixture))
            self.assertEqual(set(event["payload"]), set(fixture["payload"]))
            (payload_key,) = fixture["payload"]
            self.assertEqual(set(event["payload"][payload_key]), set(fixture["payload"][payload_key]))


if __name__ == "__main__":
    unittest.main()
