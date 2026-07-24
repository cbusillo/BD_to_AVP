import json
import platform
import select
import shutil
import signal
import subprocess
import tempfile
import unittest

from contextlib import suppress
from pathlib import Path
from unittest.mock import Mock

from scripts import build_mv_hevc_encoder_macos, qualify_direct_mv_hevc


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MP4BOX = REPOSITORY_ROOT / "bd_to_avp/bin/MP4Box"


def y4m_header(*, chroma: str = "420jpeg") -> bytes:
    return f"YUV4MPEG2 W128 H64 F24:1 Ip A0:0 C{chroma}\n".encode()


def y4m_frame(frame_index: int = 0) -> bytes:
    width = 128
    height = 64
    eye_width = width // 2
    luma = bytearray(width * height)
    for row in range(height):
        offset = row * width
        luma[offset : offset + eye_width] = bytes([48 + frame_index]) * eye_width
        luma[offset + eye_width : offset + width] = bytes([176 - frame_index]) * eye_width
    chroma_plane_bytes = (width // 2) * (height // 2)
    return b"FRAME\n" + bytes(luma) + (bytes([96]) * chroma_plane_bytes) + (bytes([160]) * chroma_plane_bytes)


class MVHEVCEncoderBuilderTests(unittest.TestCase):
    def test_build_command_pins_swift_target_and_frameworks(self) -> None:
        command = build_mv_hevc_encoder_macos.build_command(
            "/toolchain/swiftc",
            Path("MVHEVCEncoder.swift"),
            Path("mv-hevc-encoder"),
            Path("/SDKs/MacOSX.sdk"),
        )

        self.assertEqual(
            command[:7],
            [
                "/toolchain/swiftc",
                "-swift-version",
                "6",
                "-parse-as-library",
                "-O",
                "-whole-module-optimization",
                "-warnings-as-errors",
            ],
        )
        self.assertIn(
            f"arm64-apple-macosx{build_mv_hevc_encoder_macos.MINIMUM_MACOS}",
            command,
        )
        self.assertIn("/SDKs/MacOSX.sdk", command)
        self.assertEqual(
            command[-8:],
            [
                "-framework",
                "AVFoundation",
                "-framework",
                "CoreMedia",
                "-framework",
                "CoreVideo",
                "-framework",
                "VideoToolbox",
            ],
        )

    def test_capability_probe_checks_the_real_asset_writer_settings(self) -> None:
        source = build_mv_hevc_encoder_macos.SOURCE_PATH.read_text(encoding="utf-8")
        probe_start = source.index("private func isStereoMVHEVCOutputConfigurationSupported()")
        probe_end = source.index("private func fillPixelBuffer", probe_start)
        probe_source = source[probe_start:probe_end]

        self.assertIn("VTIsStereoMVHEVCEncodeSupported()", probe_source)
        self.assertIn("for quality in [Double?.none, 0.7]", probe_source)
        self.assertIn("makeOutputSettings(options: options, header: header)", probe_source)
        self.assertIn("writer.canApply(outputSettings: outputSettings, forMediaType: .video)", probe_source)
        self.assertIn("let supported = try isStereoMVHEVCOutputConfigurationSupported()", source)

    def test_box_requirements_distinguish_candidate_from_current_baseline(self) -> None:
        self.assertIn("proj", qualify_direct_mv_hevc.DIRECT_REQUIRED_BOX_TYPES)
        self.assertNotIn("proj", qualify_direct_mv_hevc.CURRENT_REQUIRED_BOX_TYPES)
        self.assertEqual(
            qualify_direct_mv_hevc.CURRENT_REQUIRED_BOX_TYPES,
            qualify_direct_mv_hevc.DIRECT_REQUIRED_BOX_TYPES - {"proj"},
        )

    def test_pipeline_failure_prefers_generator_for_truncated_input(self) -> None:
        failure = qualify_direct_mv_hevc.select_pipeline_failure(
            1,
            "generator root cause",
            1,
            "error: Y4M stream ended in an incomplete frame.",
        )

        self.assertEqual(failure, "Fixture generator failed:\ngenerator root cause")

    def test_pipeline_failure_prefers_encoder_over_upstream_sigpipe(self) -> None:
        failure = qualify_direct_mv_hevc.select_pipeline_failure(
            141,
            "broken pipe",
            1,
            "error: unsupported output settings",
        )

        self.assertEqual(failure, "Direct MV-HEVC encoder failed:\nerror: unsupported output settings")

    def test_kill_and_reap_attempts_every_process_after_wait_timeout(self) -> None:
        first = Mock(stdin=None, stdout=None, stderr=None)
        first.poll.return_value = None
        first.wait.side_effect = subprocess.TimeoutExpired("first", 30)
        second = Mock(stdin=None, stdout=None, stderr=None)
        second.poll.return_value = None
        second.wait.return_value = 0

        with self.assertRaisesRegex(qualify_direct_mv_hevc.QualificationFailure, "Failed to reap 1"):
            qualify_direct_mv_hevc.kill_and_reap(first, second)

        first.kill.assert_called_once_with()
        second.kill.assert_called_once_with()
        first.wait.assert_called_once_with(timeout=30)
        second.wait.assert_called_once_with(timeout=30)


@unittest.skipUnless(
    platform.system() == "Darwin" and platform.machine() == "arm64" and shutil.which("xcrun"),
    "native MV-HEVC encoder tests require macOS arm64 and Xcode",
)
class MVHEVCEncoderIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary_directory = tempfile.TemporaryDirectory(prefix="mv-hevc-encoder-tests-")
        cls.encoder = Path(cls.temporary_directory.name) / "mv-hevc-encoder"
        try:
            build_mv_hevc_encoder_macos.build_encoder(cls.encoder)
            probe = subprocess.run(
                [str(cls.encoder), "--capability-probe"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
            )
            probe_payload = json.loads(probe.stdout)
            if probe.returncode == 2 and probe_payload == {
                "schema_version": 1,
                "stereo_mv_hevc_encode_supported": False,
            }:
                raise unittest.SkipTest("this Mac cannot create the bounded MV-HEVC fixture")
            if probe.returncode != 0:
                diagnostic = probe.stderr.decode(errors="replace")
                raise RuntimeError(f"MV-HEVC capability probe failed:\n{diagnostic}")
            if probe_payload != {
                "schema_version": 1,
                "stereo_mv_hevc_encode_supported": True,
            }:
                raise RuntimeError(f"unexpected MV-HEVC capability probe output: {probe_payload!r}")
        except BaseException:
            cls.temporary_directory.cleanup()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary_directory.cleanup()

    def output_path(self, name: str) -> Path:
        path = Path(self.temporary_directory.name) / name
        path.unlink(missing_ok=True)
        for partial in path.parent.glob(f".{path.name}.partial-*"):
            partial.unlink()
        return path

    def run_encoder(
        self,
        output_path: Path,
        input_bytes: bytes,
        *,
        bitrate_mbps: float | None = None,
        expected_frames: int | None = None,
        overwrite: bool = False,
        quality: float | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        command = [str(self.encoder), "--output", str(output_path)]
        if bitrate_mbps is not None:
            command.extend(["--bitrate-mbps", str(bitrate_mbps)])
        if quality is not None:
            command.extend(["--quality", str(quality)])
        if expected_frames is not None:
            command.extend(["--expected-frames", str(expected_frames)])
        if overwrite:
            command.append("--overwrite")
        return subprocess.run(
            command,
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )

    def assert_no_partial_output(self, output_path: Path) -> None:
        self.assertEqual(list(output_path.parent.glob(f".{output_path.name}.partial-*")), [])

    def test_capability_probe_reports_support_without_input(self) -> None:
        completed = subprocess.run(
            [str(self.encoder), "--capability-probe"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        self.assertEqual(
            json.loads(completed.stdout),
            {
                "schema_version": 1,
                "stereo_mv_hevc_encode_supported": True,
            },
        )

    def test_encodes_bounded_mv_hevc_and_spatial_metadata(self) -> None:
        output_path = self.output_path("valid.mov")

        completed = self.run_encoder(
            output_path,
            y4m_header() + y4m_frame(0) + y4m_frame(1),
            expected_frames=2,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        summary = json.loads(completed.stdout)
        self.assertEqual(summary["frame_count"], 2)
        self.assertEqual(summary["eye_width"], 64)
        self.assertEqual(summary["eye_height"], 64)
        self.assertEqual(summary["bitrate_mbps"], 8)
        self.assertEqual(summary["rate_control"], "average_bitrate")
        self.assertNotIn("quality", summary)
        self.assertTrue(output_path.is_file())
        boxes = subprocess.run(
            [str(MP4BOX), "-diso", str(output_path), "-std"],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        ).stdout
        observed = set(qualify_direct_mv_hevc.BOX_TYPE_PATTERN.findall(boxes))
        self.assertLessEqual(qualify_direct_mv_hevc.DIRECT_REQUIRED_BOX_TYPES, observed)
        self.assert_no_partial_output(output_path)

    def test_encodes_with_quality_rate_control(self) -> None:
        output_path = self.output_path("quality.mov")

        completed = self.run_encoder(
            output_path,
            y4m_header() + y4m_frame(0) + y4m_frame(1),
            expected_frames=2,
            quality=0.5,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        summary = json.loads(completed.stdout)
        self.assertEqual(summary["quality"], 0.5)
        self.assertEqual(summary["rate_control"], "quality")
        self.assertNotIn("bitrate_mbps", summary)
        self.assertTrue(output_path.is_file())
        self.assert_no_partial_output(output_path)

    def test_rejects_conflicting_rate_control_options(self) -> None:
        output_path = self.output_path("conflicting-rate-control.mov")

        completed = self.run_encoder(
            output_path,
            y4m_header(),
            bitrate_mbps=8,
            quality=0.5,
        )

        self.assertEqual(completed.returncode, 1)
        self.assertIn(b"mutually exclusive", completed.stderr)
        self.assertFalse(output_path.exists())

    def test_rejects_out_of_range_quality(self) -> None:
        output_path = self.output_path("invalid-quality.mov")

        completed = self.run_encoder(output_path, y4m_header(), quality=1.1)

        self.assertEqual(completed.returncode, 1)
        self.assertIn(b"between 0 and 1", completed.stderr)
        self.assertFalse(output_path.exists())

    def test_rejects_high_bit_depth_y4m_before_writing_output(self) -> None:
        output_path = self.output_path("high-bit-depth.mov")

        completed = self.run_encoder(output_path, y4m_header(chroma="420p10"))

        self.assertEqual(completed.returncode, 1)
        self.assertIn(b"requires 8-bit 4:2:0 Y4M", completed.stderr)
        self.assertFalse(output_path.exists())
        self.assert_no_partial_output(output_path)

    def test_failed_overwrite_preserves_existing_output(self) -> None:
        output_path = self.output_path("preserve.mov")
        output_path.write_bytes(b"existing output")

        completed = self.run_encoder(
            output_path,
            y4m_header() + b"FRAME\ntruncated",
            expected_frames=1,
            overwrite=True,
        )

        self.assertEqual(completed.returncode, 1)
        self.assertIn(b"incomplete frame", completed.stderr)
        self.assertEqual(output_path.read_bytes(), b"existing output")
        self.assert_no_partial_output(output_path)

    def test_rejects_frames_beyond_expected_count_and_cleans_up(self) -> None:
        output_path = self.output_path("too-many.mov")

        completed = self.run_encoder(
            output_path,
            y4m_header() + y4m_frame(0) + y4m_frame(1),
            expected_frames=1,
        )

        self.assertEqual(completed.returncode, 1)
        self.assertIn(b"received more", completed.stderr)
        self.assertFalse(output_path.exists())
        self.assert_no_partial_output(output_path)

    def test_sigterm_cancels_and_removes_partial_output(self) -> None:
        output_path = self.output_path("cancelled.mov")
        process = subprocess.Popen(
            [str(self.encoder), "--output", str(output_path), "--expected-frames", "2"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert process.stdin is not None
        assert process.stderr is not None
        process.stdin.write(y4m_header())
        process.stdin.flush()
        readable, _, _ = select.select([process.stderr], [], [], 5)
        if not readable:
            process.kill()
            process.wait(timeout=5)
            self.fail("encoder did not emit readiness within five seconds")
        ready_line = process.stderr.readline()
        self.assertEqual(json.loads(ready_line)["event"], "encoder.ready")
        process.send_signal(signal.SIGTERM)
        readable, _, _ = select.select([process.stderr], [], [], 5)
        if not readable:
            process.kill()
            process.wait(timeout=5)
            self.fail("encoder did not acknowledge cancellation within five seconds")
        cancellation_line = process.stderr.readline()
        self.assertEqual(
            json.loads(cancellation_line)["event"],
            "encoder.cancellation_requested",
        )
        with suppress(BrokenPipeError):
            process.stdin.write(b"FRAME\n")
            process.stdin.flush()
        with suppress(BrokenPipeError):
            process.stdin.close()
        status = process.wait(timeout=10)
        stderr = ready_line + cancellation_line + process.stderr.read()
        process.stderr.close()
        if process.stdout:
            process.stdout.close()

        self.assertEqual(status, 130, stderr.decode())
        self.assertIn(b"Encoding cancelled", stderr)
        self.assertFalse(output_path.exists())
        self.assert_no_partial_output(output_path)


if __name__ == "__main__":
    unittest.main()
