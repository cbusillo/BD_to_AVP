import json
import platform
import struct
import subprocess
import sys
import tempfile
import unittest

from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SERVICE_HEADER = struct.Struct(">4sBBHQQQII")


def encode_timestamp(value: int, prefix: int) -> bytes:
    return bytes(
        [
            (prefix << 4) | (((value >> 30) & 0x07) << 1) | 1,
            (value >> 22) & 0xFF,
            (((value >> 15) & 0x7F) << 1) | 1,
            (value >> 7) & 0xFF,
            ((value & 0x7F) << 1) | 1,
        ]
    )


def m2ts_pes_packet(pid: int, dts: int, payload: bytes) -> bytes:
    presentation_timestamp = encode_timestamp(dts, 0x03)
    decode_timestamp = encode_timestamp(dts, 0x01)
    pes = b"\x00\x00\x01\xe0\x00\x00\x80\xc0\x0a" + presentation_timestamp + decode_timestamp + payload
    adaptation_length = 183 - len(pes)
    if adaptation_length < 0:
        raise ValueError("Synthetic PES payload is too large for one M2TS packet")
    adaptation = bytes([adaptation_length])
    if adaptation_length > 0:
        adaptation += b"\x00" + (b"\xff" * (adaptation_length - 1))
    transport_header = bytes([0x47, 0x40 | ((pid >> 8) & 0x1F), pid & 0xFF, 0x30])
    packet = b"\x00\x00\x00\x00" + transport_header + adaptation + pes
    if len(packet) != 192:
        raise AssertionError(f"Synthetic M2TS packet has invalid size: {len(packet)}")
    return packet


class SsifProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if platform.system() != "Darwin" or platform.machine() != "arm64":
            raise unittest.SkipTest("The SSIF probe build requires macOS arm64")
        cls.helper_path = REPOSITORY_ROOT / "bd_to_avp/bin/ssif_probe"
        build_result = subprocess.run(
            [
                sys.executable,
                "scripts/build_ssif_probe_macos.py",
                "--verify-only",
            ],
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if build_result.returncode != 0:
            raise RuntimeError(
                f"SSIF probe build failed:\nstdout:\n{build_result.stdout}\nstderr:\n{build_result.stderr}"
            )

    def run_demux(self, packets: list[bytes], maximum_pairs: int | None = None) -> subprocess.CompletedProcess[bytes]:
        with tempfile.TemporaryDirectory() as temporary_directory:
            input_path = Path(temporary_directory) / "sample.m2ts"
            input_path.write_bytes(b"".join(packets))
            command = [str(self.helper_path), "demux-file", str(input_path)]
            if maximum_pairs is not None:
                command.append(str(maximum_pairs))
            return subprocess.run(command, check=False, capture_output=True, timeout=30)

    def run_service_demux(
        self,
        packets: list[bytes],
        audio_pid: int,
        maximum_pairs: int | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        with tempfile.TemporaryDirectory() as temporary_directory:
            input_path = Path(temporary_directory) / "sample.m2ts"
            input_path.write_bytes(b"".join(packets))
            command = [str(self.helper_path), "demux-service-file", str(input_path), str(audio_pid)]
            if maximum_pairs is not None:
                command.append(str(maximum_pairs))
            return subprocess.run(command, check=False, capture_output=True, timeout=30)

    def parse_service_records(self, payload: bytes) -> list[dict[str, object]]:
        records: list[dict[str, object]] = []
        offset = 0
        while offset < len(payload):
            header = payload[offset : offset + SERVICE_HEADER.size]
            self.assertEqual(len(header), SERVICE_HEADER.size)
            magic, version, kind, flags, sequence, pts, dts, primary_length, secondary_length = SERVICE_HEADER.unpack(
                header
            )
            offset += SERVICE_HEADER.size
            primary = payload[offset : offset + primary_length]
            offset += primary_length
            secondary = payload[offset : offset + secondary_length]
            offset += secondary_length
            records.append(
                {
                    "magic": magic,
                    "version": version,
                    "kind": kind,
                    "flags": flags,
                    "sequence": sequence,
                    "pts": pts,
                    "dts": dts,
                    "primary": primary,
                    "secondary": secondary,
                }
            )
        return records

    def test_version_reports_contract(self) -> None:
        result = subprocess.run(
            [str(self.helper_path), "--version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )

        self.assertEqual(result.stdout, "ssif_probe contract 2\n")
        self.assertEqual(result.stderr, "")

    def test_inspect_rejects_missing_source(self) -> None:
        result = subprocess.run(
            [str(self.helper_path), "inspect", "/missing/source.iso", "1005"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(json.loads(result.stderr)["code"], "invalid_source")

    def test_stream_rejects_negative_pair_bound(self) -> None:
        result = subprocess.run(
            [str(self.helper_path), "stream-mvc", "/missing/source.iso", "1005", "-1"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stderr)["code"], "invalid_arguments")

    def test_demux_pairs_base_before_dependent_by_dts(self) -> None:
        base_one = b"\x00\x00\x00\x01\x65base-one"
        base_two = b"\x00\x00\x00\x01\x41base-two"
        dependent_one = b"\x00\x00\x00\x01\x74dependent-one"
        dependent_two = b"\x00\x00\x00\x01\x74dependent-two"
        packets = [
            m2ts_pes_packet(0x1012, 90, dependent_one),
            m2ts_pes_packet(0x1012, 180, dependent_two),
            m2ts_pes_packet(0x1011, 90, base_one),
            m2ts_pes_packet(0x1011, 180, base_two),
        ]

        result = self.run_demux(packets)

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(result.stdout, base_one + dependent_one + base_two + dependent_two)
        status = json.loads(result.stderr)
        self.assertEqual(status["type"], "stream.complete")
        self.assertEqual(status["pairs"], 2)
        self.assertGreater(status["maximum_pending_bytes"], 0)

    def test_maximum_pairs_stops_at_complete_pair(self) -> None:
        base_one = b"\x00\x00\x00\x01\x65base-one"
        dependent_one = b"\x00\x00\x00\x01\x74dependent-one"
        packets = [
            m2ts_pes_packet(0x1012, 90, dependent_one),
            m2ts_pes_packet(0x1012, 180, b"dependent-two"),
            m2ts_pes_packet(0x1011, 90, base_one),
            m2ts_pes_packet(0x1011, 180, b"base-two"),
        ]

        result = self.run_demux(packets, maximum_pairs=1)

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(result.stdout, base_one + dependent_one)
        self.assertEqual(json.loads(result.stderr)["pairs"], 1)

    def test_regular_demux_ignores_pid_zero_when_audio_is_disabled(self) -> None:
        base = b"\x00\x00\x00\x01\x65base"
        dependent = b"\x00\x00\x00\x01\x74dependent"

        result = self.run_demux(
            [
                m2ts_pes_packet(0, 45, b"not-a-selected-stream"),
                m2ts_pes_packet(0x1012, 90, dependent),
                m2ts_pes_packet(0x1011, 90, base),
            ]
        )

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(result.stdout, base + dependent)

    def test_demux_rejects_unmatched_pes(self) -> None:
        result = self.run_demux([m2ts_pes_packet(0x1012, 90, b"dependent")])

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stderr)["code"], "unmatched_mvc_pes")

    def test_demux_rejects_invalid_packet_sync(self) -> None:
        result = self.run_demux([bytes(192)])

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stderr)["code"], "invalid_m2ts_packet")

    def test_demux_rejects_transport_without_mvc_pids(self) -> None:
        result = self.run_demux([m2ts_pes_packet(0x1100, 90, b"audio")])

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stderr)["code"], "mvc_pids_unavailable")

    def test_demux_rejects_eof_before_requested_pair_count(self) -> None:
        result = self.run_demux(
            [
                m2ts_pes_packet(0x1012, 90, b"dependent"),
                m2ts_pes_packet(0x1011, 90, b"base"),
            ],
            maximum_pairs=2,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stderr)["code"], "insufficient_mvc_pairs")

    def test_demux_rejects_timestamp_flags_without_header_bytes(self) -> None:
        packet = bytearray(192)
        packet[4:8] = bytes([0x47, 0x40 | ((0x1011 >> 8) & 0x1F), 0x1011 & 0xFF, 0x30])
        packet[8] = 174
        packet[9] = 0
        packet[183:192] = b"\x00\x00\x01\xe0\x00\x00\x80\xc0\x00"

        result = self.run_demux([bytes(packet)])

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stderr)["code"], "invalid_pes")

    def test_service_demux_emits_framed_mvc_audio_and_completion(self) -> None:
        audio_pid = 0x1100
        base_one = b"\x00\x00\x00\x01\x65base-one"
        base_two = b"\x00\x00\x00\x01\x41base-two"
        dependent_one = b"\x00\x00\x00\x01\x74dependent-one"
        dependent_two = b"\x00\x00\x00\x01\x74dependent-two"
        audio_one = b"audio-one"
        audio_two = b"audio-two"
        result = self.run_service_demux(
            [
                m2ts_pes_packet(audio_pid, 90, audio_one),
                m2ts_pes_packet(0x1012, 90, dependent_one),
                m2ts_pes_packet(0x1011, 90, base_one),
                m2ts_pes_packet(audio_pid, 180, audio_two),
                m2ts_pes_packet(0x1012, 180, dependent_two),
                m2ts_pes_packet(0x1011, 180, base_two),
            ],
            audio_pid,
        )

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        records = self.parse_service_records(result.stdout)
        self.assertEqual([record["sequence"] for record in records], list(range(len(records))))
        self.assertEqual([record["kind"] for record in records], [2, 1, 1, 2, 3])
        self.assertEqual(records[0]["primary"], audio_one)
        self.assertEqual(records[1]["primary"], base_one)
        self.assertEqual(records[1]["secondary"], dependent_one)
        self.assertEqual(records[1]["flags"], 1)
        self.assertEqual(records[2]["primary"], base_two)
        self.assertEqual(records[2]["secondary"], dependent_two)
        self.assertEqual(records[2]["flags"], 0)
        self.assertEqual(records[3]["primary"], audio_two)
        self.assertTrue(all(record["magic"] == b"SSFS" for record in records))
        self.assertTrue(all(record["version"] == 1 for record in records))
        status = json.loads(result.stderr)
        self.assertEqual(status["pairs"], 2)
        self.assertEqual(status["audio_samples"], 2)

    def test_service_demux_requires_selected_audio(self) -> None:
        result = self.run_service_demux(
            [
                m2ts_pes_packet(0x1012, 90, b"dependent"),
                m2ts_pes_packet(0x1011, 90, b"\x00\x00\x00\x01\x65base"),
            ],
            0x1100,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stderr)["code"], "selected_audio_unavailable")

    def test_service_demux_marks_minimal_three_byte_idr_prefix(self) -> None:
        result = self.run_service_demux(
            [
                m2ts_pes_packet(0x1100, 90, b"audio"),
                m2ts_pes_packet(0x1012, 90, b"\x00\x00\x01\x74"),
                m2ts_pes_packet(0x1011, 90, b"\x00\x00\x01\x65"),
            ],
            0x1100,
        )

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        video = next(record for record in self.parse_service_records(result.stdout) if record["kind"] == 1)
        self.assertEqual(video["flags"], 1)

    def test_bounded_service_flushes_audio_through_final_video_dts(self) -> None:
        audio_pid = 0x1100
        result = self.run_service_demux(
            [
                m2ts_pes_packet(audio_pid, 80, b"audio-before-cutoff"),
                m2ts_pes_packet(0x1012, 90, b"dependent-one"),
                m2ts_pes_packet(0x1012, 180, b"dependent-two"),
                m2ts_pes_packet(0x1011, 90, b"\x00\x00\x01\x65base-one"),
                m2ts_pes_packet(0x1011, 180, b"base-two"),
                m2ts_pes_packet(audio_pid, 100, b"audio-after-cutoff"),
            ],
            audio_pid,
            maximum_pairs=1,
        )

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        records = self.parse_service_records(result.stdout)
        self.assertEqual([record["kind"] for record in records], [1, 2, 3])
        self.assertEqual(records[1]["dts"], 80)
        self.assertEqual(records[1]["primary"], b"audio-before-cutoff")
        status = json.loads(result.stderr)
        self.assertEqual(status["pairs"], 1)
        self.assertEqual(status["audio_samples"], 1)


if __name__ == "__main__":
    unittest.main()
