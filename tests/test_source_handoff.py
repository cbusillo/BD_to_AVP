"""The worker receives the Mac-authorized source descriptor and never opens its path."""

import errno
import io
import json
import os
import secrets
import shutil
import socket
import stat
import tempfile
import threading
import time
import unittest
import unittest.mock as mock

from typing import Any

from pathlib import Path

from bd_to_avp.worker import source_handoff
from bd_to_avp.worker.__main__ import run_worker
from bd_to_avp.worker.live_hls_service import SSIFLiveHLSService
from bd_to_avp.worker.live_title_inspection import LiveTitleInspector
from bd_to_avp.worker.operations import start_live_hls
from bd_to_avp.worker.ownership import WorkerCancelled, WorkerProcessOwner
from bd_to_avp.worker.protocol import (
    LIVE_HLS_MINIMUM_RETENTION_BYTES,
    LIVE_HLS_MINIMUM_RETENTION_SECONDS,
    PROTOCOL_VERSION,
    JobSpec,
    WorkerActivityReporter,
    WorkerEventEmitter,
    WorkerProtocolError,
)
from bd_to_avp.worker.source_handoff import receive_source
from bd_to_avp.worker.source_service import SourceServiceError


FIXTURES = Path(__file__).parent / "fixtures"
JOB_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"


def identity_of(path: Path) -> dict[str, int]:
    info = path.stat()
    return {
        "device": info.st_dev & 0xFFFF_FFFF,
        "inode": info.st_ino,
        "size_bytes": info.st_size,
        "modification_seconds": info.st_mtime_ns // 1_000_000_000,
        "modification_nanoseconds": info.st_mtime_ns % 1_000_000_000,
    }


class FakeMacHandoff:
    """The Mac end: one listener, one accepted connection, the descriptor sent on a matching token."""

    def __init__(self, source: Path) -> None:
        # sun_path is short on macOS, so keep the directory near the root.
        self.directory = Path(tempfile.mkdtemp(prefix="bdh-", dir="/tmp"))
        self.socket_path = self.directory / "s"
        self.token = secrets.token_hex(32)
        self.source = source
        self.sent = threading.Event()
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(self.socket_path))
        self.listener.listen(1)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        try:
            connection, _ = self.listener.accept()
        except OSError:
            return
        finally:
            # Exactly one connection: a second one is refused.
            self.listener.close()
        with connection:
            received = b""
            connection.settimeout(5)
            while len(received) < 64:
                chunk = connection.recv(64 - len(received))
                if not chunk:
                    break
                received += chunk
            if received != self.token.encode("ascii"):
                return
            descriptor = os.open(self.source, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                socket.send_fds(connection, [b"\0"], [descriptor])
            finally:
                os.close(descriptor)
            self.sent.set()

    def close(self) -> None:
        self.listener.close()
        self.thread.join(timeout=5)
        shutil.rmtree(self.directory, ignore_errors=True)

    def handoff(self, token: str | None = None) -> dict[str, str]:
        return {"socket_path": str(self.socket_path), "token": token or self.token}


class SourceHandoffTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        # The real file, which only the fake Mac opens. Its contents name the fake helper's behavior.
        self.source = self.root / "approved" / "source.iso"
        self.source.parent.mkdir()
        self.source.write_bytes(b"source.iso")
        # The canonical path the job names: inside a directory nothing may enter.
        self.locked = self.root / "locked"
        self.locked.mkdir()
        self.canonical = self.locked / "Rainforest.iso"
        self.locked.chmod(0)
        self.addCleanup(self.locked.chmod, stat.S_IRWXU)
        self.mac = FakeMacHandoff(self.source)
        self.addCleanup(self.mac.close)

    def source_object(self, *, identity: dict[str, int] | None = None, token: str | None = None) -> dict[str, object]:
        return {
            "kind": "disc_image",
            "path": str(self.canonical),
            "identity": identity or identity_of(self.source),
            "descriptor_handoff": self.mac.handoff(token),
        }

    def job(self, operation: str, **source: object) -> JobSpec:
        request: dict[str, object] = {
            "protocol_version": PROTOCOL_VERSION,
            "type": "job.start",
            "job_id": JOB_ID,
            "operation": operation,
            "source": self.source_object(**source),  # type: ignore[arg-type]
        }
        if operation == "start_live_hls":
            request["destination"] = {"path": str(self.root / "live")}
            request["live_hls"] = {
                "playlist": 1005,
                "audio_pid": 0x1100,
                "retention_seconds": LIVE_HLS_MINIMUM_RETENTION_SECONDS,
                "retention_max_bytes": LIVE_HLS_MINIMUM_RETENTION_BYTES,
            }
        return JobSpec.from_json_line(json.dumps(request))


class ReceiveSourceTests(SourceHandoffTestCase):
    def test_received_descriptor_passes_the_identity_check(self) -> None:
        held = receive_source(self.job("inspect_live_title").source)
        assert held is not None
        with held:
            held.verify()
            self.assertEqual(os.fstat(held.descriptor).st_ino, self.source.stat().st_ino)
            self.assertIs(os.get_inheritable(held.descriptor), False)
            self.assertEqual(held.device_path, f"/dev/fd/{held.descriptor}")

    def test_wrong_token_gets_nothing(self) -> None:
        with self.assertRaises(SourceServiceError) as raised:
            receive_source(self.job("inspect_live_title", token="0" * 64).source)

        self.assertEqual(raised.exception.code, "live_source_handoff_failed")
        self.assertFalse(self.mac.sent.is_set())

    def test_second_connection_gets_nothing(self) -> None:
        job = self.job("inspect_live_title")
        held = receive_source(job.source)
        assert held is not None
        held.close()

        with self.assertRaises(SourceServiceError) as raised:
            receive_source(job.source)

        self.assertEqual(raised.exception.code, "live_source_handoff_failed")

    def test_mismatched_descriptor_is_closed_and_reported_as_changed(self) -> None:
        identity = {**identity_of(self.source), "size_bytes": self.source.stat().st_size + 1}
        received: list[int] = []
        original = source_handoff._receive_descriptor

        def recording(*arguments: object, **keywords: object) -> int:
            descriptor = original(*arguments, **keywords)  # type: ignore[arg-type]
            received.append(descriptor)
            return descriptor

        with mock.patch.object(source_handoff, "_receive_descriptor", recording):
            with self.assertRaises(SourceServiceError) as raised:
                receive_source(self.job("inspect_live_title", identity=identity).source)

        self.assertEqual(raised.exception.code, "live_source_changed")
        assert_closed(self, received)

    def test_job_without_a_handoff_receives_nothing(self) -> None:
        request = {
            "protocol_version": PROTOCOL_VERSION,
            "type": "job.start",
            "job_id": JOB_ID,
            "operation": "inspect_live_title",
            "source": {"kind": "disc_image", "path": "/tmp/a.iso"},
        }
        self.assertIsNone(receive_source(JobSpec.from_json_line(json.dumps(request)).source))


class HandoffProtocolTests(unittest.TestCase):
    @staticmethod
    def request(operation: str, source: dict[str, object]) -> str:
        request: dict[str, object] = {
            "protocol_version": PROTOCOL_VERSION,
            "type": "job.start",
            "job_id": JOB_ID,
            "operation": operation,
            "source": source,
        }
        if operation == "start_live_hls":
            request["destination"] = {"path": "/tmp/live"}
            request["live_hls"] = {
                "playlist": 1005,
                "audio_pid": 0x1100,
                "retention_seconds": LIVE_HLS_MINIMUM_RETENTION_SECONDS,
                "retention_max_bytes": LIVE_HLS_MINIMUM_RETENTION_BYTES,
            }
        return json.dumps(request)

    def test_invalid_handoffs_are_protocol_errors(self) -> None:
        identity = {
            "device": 1,
            "inode": 2,
            "size_bytes": 3,
            "modification_seconds": 4,
            "modification_nanoseconds": 5,
        }
        handoff = {"socket_path": "/tmp/bdh-x/s", "token": "a" * 64}
        base = {"kind": "disc_image", "path": "/tmp/a.iso"}
        cases = {
            "handoff without identity": ("start_live_hls", {**base, "descriptor_handoff": handoff}),
            "inspection handoff without identity": ("inspect_live_title", {**base, "descriptor_handoff": handoff}),
            "handoff for a folder": (
                "inspect_live_title",
                {"kind": "blu_ray_folder", "path": "/tmp/disc", "identity": identity, "descriptor_handoff": handoff},
            ),
            "handoff for inspection of a source": (
                "inspect_source",
                {**base, "identity": identity, "descriptor_handoff": handoff},
            ),
            "uppercase token": (
                "start_live_hls",
                {**base, "identity": identity, "descriptor_handoff": {**handoff, "token": "A" * 64}},
            ),
            "short token": (
                "start_live_hls",
                {**base, "identity": identity, "descriptor_handoff": {**handoff, "token": "a" * 63}},
            ),
            "relative socket": (
                "start_live_hls",
                {**base, "identity": identity, "descriptor_handoff": {**handoff, "socket_path": "s"}},
            ),
            "socket path too long for sun_path": (
                "start_live_hls",
                {**base, "identity": identity, "descriptor_handoff": {**handoff, "socket_path": "/" + "s" * 103}},
            ),
            "extra handoff key": (
                "start_live_hls",
                {**base, "identity": identity, "descriptor_handoff": {**handoff, "pid": 1}},
            ),
        }
        for label, (operation, source) in cases.items():
            with self.subTest(label), self.assertRaises(WorkerProtocolError) as raised:
                JobSpec.from_json_line(self.request(operation, source))
            self.assertEqual(raised.exception.code, "invalid_source")


def assert_closed(test: unittest.TestCase, descriptors: list[int]) -> None:
    test.assertEqual(len(descriptors), 1)
    with test.assertRaises(OSError) as raised:
        os.fstat(descriptors[0])
    test.assertEqual(raised.exception.errno, errno.EBADF)


class HandoffOperationTests(SourceHandoffTestCase):
    """The real operations, with fake tools, while the canonical path cannot be touched."""

    def setUp(self) -> None:
        super().setUp()
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
        self.descriptor_log = self.root / "descriptors.log"
        environment = mock.patch.dict(
            os.environ,
            {
                "FAKE_LIVE_HLS_RECORD_DIR": str(self.records),
                "FAKE_TOOLS_FD_LOG": str(self.descriptor_log),
                "BD_TO_AVP_SSIF_PROBE_PATH": str(self.tools / "ssif_probe"),
                "BD_TO_AVP_EDGE264_TEST_PATH": str(self.tools / "edge264_test"),
                "BD_TO_AVP_FFMPEG_PATH": str(self.tools / "ffmpeg"),
                "BD_TO_AVP_MV_HEVC_ENCODER_PATH": str(self.tools / "mv-hevc-encoder"),
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.received: list[int] = []
        original = source_handoff._receive_descriptor

        def recording(*arguments: object, **keywords: object) -> int:
            descriptor = original(*arguments, **keywords)  # type: ignore[arg-type]
            self.received.append(descriptor)
            return descriptor

        recorder = mock.patch.object(source_handoff, "_receive_descriptor", recording)
        recorder.start()
        self.addCleanup(recorder.stop)

    def run_job(self, job: JobSpec) -> list[dict[str, Any]]:
        line = json.dumps(self.raw_request(job))
        output = io.StringIO()
        run_worker(io.StringIO(line + "\n"), output, io.StringIO())
        return [json.loads(event) for event in output.getvalue().splitlines()]

    def raw_request(self, job: JobSpec) -> dict[str, object]:
        request: dict[str, object] = {
            "protocol_version": PROTOCOL_VERSION,
            "type": "job.start",
            "job_id": JOB_ID,
            "operation": job.operation.value,
            "source": self.last_source,
        }
        if job.live_hls is not None and job.destination is not None:
            request["destination"] = {"path": str(job.destination.path)}
            request["live_hls"] = {
                "playlist": job.live_hls.playlist,
                "audio_pid": job.live_hls.audio_pid,
                "retention_seconds": job.live_hls.retention_seconds,
                "retention_max_bytes": job.live_hls.retention_max_bytes,
            }
        return request

    def job(self, operation: str, **source: object) -> JobSpec:
        self.last_source = self.source_object(**source)  # type: ignore[arg-type]
        return super().job(operation, **source)

    def descriptor_records(self) -> list[list[str]]:
        if not self.descriptor_log.exists():
            return []
        return [line.split() for line in self.descriptor_log.read_text(encoding="utf-8").splitlines()]

    def assert_only_source_reads_inherit(self, source_subcommands: set[str]) -> None:
        (descriptor,) = self.received
        handed = str(descriptor)
        seen: set[str] = set()
        for tool, subcommand, argument, inherited in self.descriptor_records():
            descriptors = set(inherited.split(","))
            if tool == "ssif_probe" and subcommand in source_subcommands:
                seen.add(subcommand)
                self.assertEqual(argument, f"/dev/fd/{handed}")
                self.assertIn(handed, descriptors, subcommand)
            else:
                self.assertNotIn(handed, descriptors, f"{tool} {subcommand} inherited the source")
        self.assertEqual(seen, source_subcommands)

    def test_canonical_path_is_unreachable(self) -> None:
        with self.assertRaises(PermissionError):
            os.stat(self.canonical)

    def test_live_hls_completes_through_the_handoff(self) -> None:
        events = self.run_job(self.job("start_live_hls"))

        completed = events[-1]
        self.assertEqual(completed["type"], "job.completed", completed)
        self.assertTrue(Path(completed["payload"]["live_hls_result"]["playlist_path"]).is_file())
        self.assert_only_source_reads_inherit({"inspect", "stream-service"})
        records = {(record[0], record[1]) for record in self.descriptor_records()}
        self.assertLessEqual({("edge264_test", "-"), ("ffmpeg", "-"), ("mv-hevc-encoder", "-")}, records)
        assert_closed(self, self.received)

    def test_live_title_inspection_completes_through_the_handoff(self) -> None:
        events = self.run_job(self.job("inspect_live_title"))

        completed = events[-1]
        self.assertEqual(completed["type"], "job.completed", completed)
        self.assertTrue(completed["payload"]["live_title_result"]["eligible"])
        self.assert_only_source_reads_inherit({"titles"})
        assert_closed(self, self.received)

    def test_each_operation_reports_a_mismatched_descriptor(self) -> None:
        for operation in ("start_live_hls", "inspect_live_title"):
            with self.subTest(operation):
                self.mac.close()
                self.mac = FakeMacHandoff(self.source)
                self.addCleanup(self.mac.close)
                self.received.clear()
                self.descriptor_log.unlink(missing_ok=True)
                changed = {**identity_of(self.source), "inode": self.source.stat().st_ino + 1}

                events = self.run_job(self.job(operation, identity=changed))

                failed = events[-1]
                self.assertEqual(failed["type"], "job.failed", failed)
                self.assertEqual(failed["payload"]["error"]["code"], "live_source_changed")
                source_reads = [
                    record
                    for record in self.descriptor_records()
                    if record[0] == "ssif_probe" and record[1] != "--version"
                ]
                self.assertEqual(source_reads, [], "the source was read after the identity check failed")
                assert_closed(self, self.received)

    def test_each_service_rechecks_the_held_descriptor(self) -> None:
        """A file changed after the handoff is caught by the services' own fstat checks."""
        for operation in ("start_live_hls", "inspect_live_title"):
            with self.subTest(operation):
                self.mac.close()
                self.mac = FakeMacHandoff(self.source)
                self.addCleanup(self.mac.close)
                self.descriptor_log.unlink(missing_ok=True)
                job = self.job(operation)
                held = receive_source(job.source)
                assert held is not None
                with held:
                    # Same inode, new size: only fstat on the held descriptor can see it.
                    with self.source.open("ab") as handle:
                        handle.write(b"\n")
                    activity = WorkerActivityReporter(WorkerEventEmitter(io.StringIO(), JOB_ID))
                    service: SSIFLiveHLSService | LiveTitleInspector
                    if operation == "start_live_hls":
                        service = SSIFLiveHLSService(job, WorkerProcessOwner(), activity, held_source=held)
                    else:
                        service = LiveTitleInspector(job, WorkerProcessOwner(), activity, held_source=held)
                    with self.assertRaises(SourceServiceError) as raised:
                        service.run()
                self.assertEqual(raised.exception.code, "live_source_changed")
                source_reads = [
                    record
                    for record in self.descriptor_records()
                    if record[0] == "ssif_probe" and record[1] != "--version"
                ]
                self.assertEqual(source_reads, [])
                self.source.write_bytes(b"source.iso")

    def test_cancellation_closes_the_descriptor(self) -> None:
        self.source.write_bytes(b"delay.iso")
        job = self.job("start_live_hls")
        owner = WorkerProcessOwner()
        activity = WorkerActivityReporter(WorkerEventEmitter(io.StringIO(), JOB_ID))

        def cancel_once_streaming() -> None:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if any(record[1] == "stream-service" for record in self.descriptor_records()):
                    break
                time.sleep(0.05)
            owner.request_cancel()

        canceller = threading.Thread(target=cancel_once_streaming, daemon=True)
        canceller.start()
        with self.assertRaises(WorkerCancelled):
            start_live_hls(job, owner, activity)
        canceller.join(timeout=5)

        assert_closed(self, self.received)


if __name__ == "__main__":
    unittest.main()
