import io
import json
import os
import plistlib
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
import unittest.mock as mock

from pathlib import Path

from bd_to_avp.worker.__main__ import run_worker
from bd_to_avp.worker.live_title_inspection import LiveTitleInspector
from bd_to_avp.worker.ownership import WorkerProcessOwner
from bd_to_avp.worker.protocol import (
    PROTOCOL_VERSION,
    JobSpec,
    WorkerActivityReporter,
    WorkerEventEmitter,
    WorkerOperation,
    WorkerProtocolError,
)
from bd_to_avp.worker.source_service import SourceServiceError


FIXTURES = Path(__file__).parent / "fixtures"
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
JOB_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
RAINFOREST_ISO_ENV = "BD_TO_AVP_RAINFOREST_ISO"
EVIDENCE_DIRECTORY_ENV = "BD_TO_AVP_LIVE_TITLE_EVIDENCE_DIR"


def inspect_live_title_request(source: Path, kind: str = "disc_image") -> dict[str, object]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "type": "job.start",
        "job_id": JOB_ID,
        "operation": "inspect_live_title",
        "source": {"kind": kind, "path": str(source)},
    }


def completed_result(output: str) -> dict[str, object]:
    events = [json.loads(line) for line in output.splitlines()]
    completed = events[-1]
    assert completed["type"] == "job.completed", completed
    return completed["payload"]["live_title_result"]


class InspectLiveTitleProtocolTests(unittest.TestCase):
    def test_parses_shared_swift_inspect_live_title_fixture(self) -> None:
        fixture = FIXTURES / f"native_worker_inspect_live_title_v{PROTOCOL_VERSION}.json"

        job = JobSpec.from_json_line(fixture.read_text(encoding="utf-8"))

        self.assertIs(job.operation, WorkerOperation.INSPECT_LIVE_TITLE)
        self.assertIsNone(job.destination)

    def test_rejects_invalid_inspect_live_title_requests(self) -> None:
        physical = inspect_live_title_request(Path("/dev/disk9"), kind="physical_disc")
        direct = inspect_live_title_request(Path("/tmp/a.mkv"), kind="direct_file")
        titled = inspect_live_title_request(Path("/tmp/a.iso"))
        titled["source"] = {"kind": "disc_image", "path": "/tmp/a.iso", "title_id": "0"}
        with_destination = inspect_live_title_request(Path("/tmp/a.iso"))
        with_destination["destination"] = {"path": "/tmp/live"}
        cases = {
            "physical disc": physical,
            "direct file": direct,
            "title selection": titled,
            "destination": with_destination,
        }
        for label, request in cases.items():
            with self.subTest(label), self.assertRaises(WorkerProtocolError):
                JobSpec.from_json_line(json.dumps(request))


class LiveTitleInspectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.probe = self.root / "ssif_probe"
        shutil.copy2(FIXTURES / "fake_ssif_probe.py", self.probe)
        self.probe.chmod(self.probe.stat().st_mode | stat.S_IXUSR)

    def inspect(self, source_name: str) -> dict[str, object]:
        source = self.root / source_name
        source.touch()
        job = JobSpec.from_json_line(json.dumps(inspect_live_title_request(source)))
        activity = WorkerActivityReporter(WorkerEventEmitter(io.StringIO(), JOB_ID))
        return LiveTitleInspector(job, WorkerProcessOwner(), activity, probe_path=self.probe).run()

    def test_eligible_source_reports_longest_admitted_3d_playlist_and_supported_audio(self) -> None:
        result = self.inspect("source.iso")

        self.assertEqual(
            result,
            {
                "eligible": True,
                "unsupported_reason": None,
                "playlist": 1005,
                "duration_seconds": 5400.0,
                "audio_streams": [
                    {"pid": 0x1100, "coding_type": 0x86, "format": 6, "rate": 12, "language": "eng"},
                    {"pid": 0x1102, "coding_type": 0x81, "format": 6, "rate": 12, "language": "eng"},
                ],
            },
        )

    def test_ineligible_sources_report_their_reason(self) -> None:
        cases = {
            "interlaced.iso": "unsupported_live_video",
            "lpcm.iso": "unsupported_live_audio",
            "encrypted.iso": "encrypted_source_unsupported",
            "multiangle.iso": "multiple_angles_unsupported",
            "multiclip.iso": "multiple_clips_unsupported",
            "notitles.iso": "no_live_titles",
            "truncated.iso": "live_title_search_incomplete",
        }
        for source_name, reason in cases.items():
            with self.subTest(source_name):
                result = self.inspect(source_name)

                self.assertFalse(result["eligible"])
                self.assertEqual(result["unsupported_reason"], reason)
                self.assertEqual(result["audio_streams"], [])

    def test_truncated_listing_still_reports_a_listed_admitted_title(self) -> None:
        result = self.inspect("truncated-found.iso")

        self.assertTrue(result["eligible"])
        self.assertEqual(result["playlist"], 810)

    def test_helper_failures_are_reported(self) -> None:
        cases = {
            "oldhelper.iso": "ssif_probe_incompatible",
            "malformed.iso": "invalid_source_inspection",
            "brokenjson.iso": "invalid_source_inspection",
            "nocompleteness.iso": "invalid_source_inspection",
            "unreadable.iso": "source_open_failed",
        }
        for source_name, code in cases.items():
            with self.subTest(source_name), self.assertRaises(SourceServiceError) as raised:
                self.inspect(source_name)

            self.assertEqual(raised.exception.code, code)

    def test_worker_reports_live_title_result(self) -> None:
        source = self.root / "source.iso"
        source.touch()
        output = io.StringIO()
        with mock.patch.dict(os.environ, {"BD_TO_AVP_SSIF_PROBE_PATH": str(self.probe)}):
            exit_code = run_worker(
                io.StringIO(json.dumps(inspect_live_title_request(source)) + "\n"),
                output,
                io.StringIO(),
            )

        self.assertEqual(exit_code, 0)
        # The Swift decoder test reads this fixture, so both sides agree on the result shape.
        fixture = json.loads(
            (FIXTURES / f"native_worker_live_title_completed_v{PROTOCOL_VERSION}.json").read_text(encoding="utf-8")
        )
        self.assertEqual(completed_result(output.getvalue()), fixture["payload"]["live_title_result"])

    def test_worker_reports_incompatible_helper_as_job_failure(self) -> None:
        source = self.root / "oldhelper.iso"
        source.touch()
        output = io.StringIO()
        with mock.patch.dict(os.environ, {"BD_TO_AVP_SSIF_PROBE_PATH": str(self.probe)}):
            run_worker(
                io.StringIO(json.dumps(inspect_live_title_request(source)) + "\n"),
                output,
                io.StringIO(),
            )

        failed = json.loads(output.getvalue().splitlines()[-1])
        self.assertEqual(failed["type"], "job.failed")
        self.assertEqual(failed["payload"]["error"]["code"], "ssif_probe_incompatible")


def mount_read_only(image: Path, parent: Path) -> Path:
    result = subprocess.run(
        [
            "hdiutil",
            "attach",
            "-readonly",
            "-nobrowse",
            "-noverify",
            "-noautofsck",
            "-mountrandom",
            str(parent),
            "-plist",
            str(image),
        ],
        input=b"y\n",
        capture_output=True,
        check=True,
        timeout=120,
    )
    plist = result.stdout[result.stdout.index(b"<?xml") :]
    entities = plistlib.loads(plist)["system-entities"]
    return Path(next(entity["mount-point"] for entity in entities if "mount-point" in entity))


def many_long_rejected_playlists(mounted: Path, root: Path, copies: int) -> Path:
    """A BDMV folder of links into the mounted disc, without its 3,240-second feature
    playlists and with more than 64 longer copies of the feature that point at a missing
    clip. The only eligible titles left are the shorter extras."""
    source = mounted / "BDMV"
    folder = root / "many-long-rejected"
    target = folder / "BDMV"
    for directory in ("PLAYLIST", "CLIPINF", "STREAM/SSIF"):
        (target / directory).mkdir(parents=True)
    for name in ("index.bdmv", "MovieObject.bdmv"):
        (target / name).symlink_to(source / name)
    for directory, pattern in (("CLIPINF", "*.clpi"), ("STREAM", "*.m2ts"), ("STREAM/SSIF", "*.ssif")):
        for path in (source / directory).glob(pattern):
            (target / directory / path.name).symlink_to(path)
    for path in (source / "PLAYLIST").glob("*.mpls"):
        if path.name not in {"00005.mpls", "01005.mpls"}:
            (target / "PLAYLIST" / path.name).symlink_to(path)
    feature = (source / "PLAYLIST/01005.mpls").read_bytes()
    missing_clip = feature.replace(b"00007M2TS", b"00099M2TS")
    (target / "CLIPINF/00099.clpi").symlink_to(source / "CLIPINF/00007.clpi")
    for playlist in range(100, 100 + copies):
        (target / "PLAYLIST" / f"{playlist:05d}.mpls").write_bytes(missing_clip)
    return folder


@unittest.skipUnless(os.environ.get(RAINFOREST_ISO_ENV), f"Set {RAINFOREST_ISO_ENV} to run real-media tests")
class RainforestLiveTitleInspectionTests(unittest.TestCase):
    source: Path

    @classmethod
    def setUpClass(cls) -> None:
        cls.source = Path(os.environ[RAINFOREST_ISO_ENV])
        if not cls.source.is_file():
            raise unittest.SkipTest(f"Rainforest ISO is unavailable: {cls.source}")
        subprocess.run(
            ["uv", "run", "python", "scripts/build_ssif_probe_macos.py", "--verify-only"],
            cwd=REPOSITORY_ROOT,
            check=True,
            capture_output=True,
            timeout=120,
        )

    def inspect(self, source: Path, kind: str = "disc_image") -> tuple[dict[str, object], float]:
        output = io.StringIO()
        environment = {"BD_TO_AVP_SSIF_PROBE_PATH": str(REPOSITORY_ROOT / "bd_to_avp/bin/ssif_probe")}
        started = time.monotonic()
        with (
            mock.patch.dict(os.environ, environment),
            mock.patch(
                "bd_to_avp.worker.operations.get_disc_and_mvc_video_info",
                side_effect=AssertionError("live title inspection must not run MakeMKV"),
            ),
        ):
            exit_code = run_worker(
                io.StringIO(json.dumps(inspect_live_title_request(source, kind)) + "\n"),
                output,
                io.StringIO(),
            )
        elapsed_seconds = time.monotonic() - started
        self.assertEqual(exit_code, 0, output.getvalue())
        return completed_result(output.getvalue()), elapsed_seconds

    def test_rainforest_is_eligible_without_makemkv(self) -> None:
        result, elapsed_seconds = self.inspect(self.source)

        self.assertTrue(result["eligible"])
        self.assertEqual(result["playlist"], 1005)
        audio_streams = result["audio_streams"]
        assert isinstance(audio_streams, list)
        self.assertEqual([stream["pid"] for stream in audio_streams], [0x1100, 0x1101])
        evidence_directory = os.environ.get(EVIDENCE_DIRECTORY_ENV)
        if evidence_directory:
            Path(evidence_directory).mkdir(parents=True, exist_ok=True)
            (Path(evidence_directory) / "rainforest-inspect-live-title.json").write_text(
                json.dumps({"elapsed_seconds": round(elapsed_seconds, 3), "result": result}, indent=2) + "\n",
                encoding="utf-8",
            )

    def test_shorter_eligible_title_behind_more_than_64_longer_rejected_ones_is_found(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mount_parent = root / "mount"
            mount_parent.mkdir()
            mounted = mount_read_only(self.source, mount_parent)
            try:
                folder = many_long_rejected_playlists(mounted, root, copies=70)
                result, _ = self.inspect(folder, kind="blu_ray_folder")
            finally:
                subprocess.run(["hdiutil", "detach", str(mounted)], check=True, capture_output=True, timeout=120)

        self.assertTrue(result["eligible"], result)
        self.assertEqual(result["playlist"], 10)
        self.assertEqual(result["duration_seconds"], 135.135)


if __name__ == "__main__":
    unittest.main()
