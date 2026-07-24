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

from scripts import benchmark_mv_hevc_metalfx, build_mv_hevc_encoder_macos, qualify_direct_mv_hevc


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MP4BOX = REPOSITORY_ROOT / "bd_to_avp/bin/MP4Box"
FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


def y4m_header(
    *,
    chroma: str = "420jpeg",
    width: int = 128,
    height: int = 64,
    frame_rate: int = 24,
) -> bytes:
    return f"YUV4MPEG2 W{width} H{height} F{frame_rate}:1 Ip A0:0 C{chroma}\n".encode()


def y4m_frame(frame_index: int = 0, *, width: int = 128, height: int = 64) -> bytes:
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
            (Path("MVHEVCEncoder.swift"), Path("MetalFXSpatialUpscaler.swift")),
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
        self.assertIn("MVHEVCEncoder.swift", command)
        self.assertIn("MetalFXSpatialUpscaler.swift", command)
        self.assertEqual(command[-16::2], ["-framework"] * 8)
        self.assertEqual(command[-15::2], list(build_mv_hevc_encoder_macos.FRAMEWORKS))

    def test_capability_probe_checks_the_real_asset_writer_settings(self) -> None:
        source = build_mv_hevc_encoder_macos.SOURCE_PATH.read_text(encoding="utf-8")
        probe_start = source.index("private func isStereoMVHEVCOutputConfigurationSupported(")
        probe_end = source.index("private func fillPixelBuffer", probe_start)
        probe_source = source[probe_start:probe_end]

        self.assertIn("VTIsStereoMVHEVCEncodeSupported()", probe_source)
        self.assertIn("for quality in [Double?.none, 0.7]", probe_source)
        self.assertIn("makeOutputSettings(options: options, header: header)", probe_source)
        self.assertIn("writer.canApply(outputSettings: outputSettings, forMediaType: .video)", probe_source)
        self.assertIn("let supported = try isStereoMVHEVCOutputConfigurationSupported()", source)

    def test_metalfx_prototype_is_explicit_bounded_and_throwing(self) -> None:
        encoder_source = build_mv_hevc_encoder_macos.SOURCE_PATH.read_text(encoding="utf-8")
        scaler_source = build_mv_hevc_encoder_macos.METALFX_SOURCE_PATH.read_text(encoding="utf-8")

        self.assertIn('case "--prototype-metalfx-upscale"', encoder_source)
        self.assertIn("static let sourceBufferLimit = 2", scaler_source)
        self.assertIn("static let inputBufferLimit = 2", scaler_source)
        self.assertIn("static let outputBufferLimit = 8", scaler_source)
        self.assertIn("allocationThreshold: MetalFXPrototypeContract.sourceBufferLimit", scaler_source)
        self.assertIn("allocationThreshold: MetalFXPrototypeContract.inputBufferLimit", scaler_source)
        self.assertIn("allocationThreshold: MetalFXPrototypeContract.outputBufferLimit", scaler_source)
        self.assertIn("throw MetalFXSpatialUpscalerFailure.command", scaler_source)
        self.assertNotIn("return pixelBuffer", scaler_source)

    def test_metalfx_benchmark_compares_exact_fixture_shapes_and_pool_payload(self) -> None:
        command = benchmark_mv_hevc_metalfx.generator_command(
            "/tools/ffmpeg",
            frames=120,
            frame_rate=24,
        )

        self.assertIn("testsrc2=size=1920x1080:rate=24", command)
        self.assertEqual(command[command.index("-frames:v") + 1], "120")
        self.assertEqual(
            benchmark_mv_hevc_metalfx.declared_pool_payload_bytes(),
            (1_920 * 1_080 * 3 // 2 * 2) + (1_920 * 1_080 * 4 * 2) + (3_840 * 2_160 * 4 * 2) + (3_840 * 2_160 * 4 * 8),
        )

    def test_metalfx_boundedness_gate_uses_peak_rss_growth_not_duration(self) -> None:
        stream: dict[str, object] = {}
        summary: dict[str, object] = {}
        declared_payload = benchmark_mv_hevc_metalfx.declared_pool_payload_bytes()
        baseline_run = benchmark_mv_hevc_metalfx.BenchmarkRun(1, 500, 1, stream, summary)
        short_run = benchmark_mv_hevc_metalfx.BenchmarkRun(1, 500, 1, stream, summary)
        passing_long_run = benchmark_mv_hevc_metalfx.BenchmarkRun(
            10,
            500 + declared_payload + 50,
            1,
            stream,
            summary,
        )
        failing_long_run = benchmark_mv_hevc_metalfx.BenchmarkRun(
            10,
            500 + declared_payload + 101,
            1,
            stream,
            summary,
        )

        self.assertTrue(
            benchmark_mv_hevc_metalfx.boundedness_result(
                baseline_run,
                short_run,
                passing_long_run,
                max_unmodeled_growth_bytes=100,
            )["passed"]
        )
        self.assertFalse(
            benchmark_mv_hevc_metalfx.boundedness_result(
                baseline_run,
                short_run,
                failing_long_run,
                max_unmodeled_growth_bytes=100,
            )["passed"]
        )

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
        prototype_metalfx_upscale: bool = False,
        quality: float | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        command = [str(self.encoder), "--output", str(output_path)]
        if bitrate_mbps is not None:
            command.extend(["--bitrate-mbps", str(bitrate_mbps)])
        if quality is not None:
            command.extend(["--quality", str(quality)])
        if expected_frames is not None:
            command.extend(["--expected-frames", str(expected_frames)])
        if prototype_metalfx_upscale:
            command.append("--prototype-metalfx-upscale")
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

    def require_metalfx_support(self) -> dict[str, object]:
        completed = subprocess.run(
            [str(self.encoder), "--capability-probe", "--prototype-metalfx-upscale"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
        payload = json.loads(completed.stdout)
        if completed.returncode == 2 and payload.get("prototype_metalfx_upscale_supported") is False:
            self.skipTest("this Mac cannot run the MetalFX MV-HEVC prototype")
        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        return payload

    def frame_pts(self, path: Path) -> list[float]:
        completed = subprocess.run(
            [
                FFPROBE,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_frames",
                "-show_entries",
                "frame=best_effort_timestamp_time",
                "-of",
                "json",
                str(path),
            ],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
        return [float(frame["best_effort_timestamp_time"]) for frame in json.loads(completed.stdout)["frames"]]

    def first_frame_gray_value(self, path: Path) -> int:
        completed = subprocess.run(
            [
                FFMPEG,
                "-v",
                "error",
                "-i",
                str(path),
                "-frames:v",
                "1",
                "-vf",
                "scale=1:1,format=gray",
                "-f",
                "rawvideo",
                "-",
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
        self.assertEqual(len(completed.stdout), 1)
        return completed.stdout[0]

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

    def test_metalfx_capability_probe_reports_both_required_capabilities(self) -> None:
        completed = subprocess.run(
            [str(self.encoder), "--capability-probe", "--prototype-metalfx-upscale"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )

        self.assertIn(completed.returncode, {0, 2}, completed.stderr.decode())
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["schema_version"], 1)
        self.assertIsInstance(payload["metalfx_spatial_scaler_supported"], bool)
        self.assertIsInstance(payload["stereo_mv_hevc_encode_supported"], bool)
        self.assertEqual(
            payload["prototype_metalfx_upscale_supported"],
            payload["metalfx_spatial_scaler_supported"] and payload["stereo_mv_hevc_encode_supported"],
        )
        self.assertEqual(completed.returncode == 0, payload["prototype_metalfx_upscale_supported"])

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

    @unittest.skipUnless(FFMPEG and FFPROBE, "MetalFX output validation requires ffmpeg and ffprobe")
    def test_metalfx_upscales_paired_eyes_with_matching_pts_and_color_metadata(self) -> None:
        self.require_metalfx_support()
        output_path = self.output_path("metalfx.mov")
        input_width = 3_840
        input_height = 1_080
        input_bytes = y4m_header(width=input_width, height=input_height)
        input_bytes += y4m_frame(0, width=input_width, height=input_height)
        input_bytes += y4m_frame(1, width=input_width, height=input_height)

        completed = self.run_encoder(
            output_path,
            input_bytes,
            expected_frames=2,
            prototype_metalfx_upscale=True,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        summary = json.loads(completed.stdout)
        self.assertEqual(summary["frame_count"], 2)
        self.assertEqual(summary["input_eye_width"], 1_920)
        self.assertEqual(summary["input_eye_height"], 1_080)
        self.assertEqual(summary["eye_width"], 3_840)
        self.assertEqual(summary["eye_height"], 2_160)
        self.assertEqual(summary["metalfx_source_buffer_limit"], 2)
        self.assertEqual(summary["metalfx_input_buffer_limit"], 2)
        self.assertEqual(summary["metalfx_output_buffer_limit"], 8)
        self.assertEqual(summary["upscale_mode"], "metalfx_spatial_prototype")

        stream = qualify_direct_mv_hevc.ffprobe_stream(FFPROBE, output_path)
        self.assertEqual(stream["width"], 3_840)
        self.assertEqual(stream["height"], 2_160)
        self.assertEqual(stream["nb_read_frames"], "2")
        self.assertEqual(stream["color_range"], "tv")
        self.assertEqual(stream["color_space"], "bt709")
        self.assertEqual(stream["color_transfer"], "bt709")
        self.assertEqual(stream["color_primaries"], "bt709")

        left_path, right_path = qualify_direct_mv_hevc.split_mv_hevc(
            output_path,
            Path(self.temporary_directory.name) / "metalfx-split",
        )
        left_stream = qualify_direct_mv_hevc.ffprobe_stream(FFPROBE, left_path)
        right_stream = qualify_direct_mv_hevc.ffprobe_stream(FFPROBE, right_path)
        for eye_stream in (left_stream, right_stream):
            self.assertEqual(eye_stream["width"], 3_840)
            self.assertEqual(eye_stream["height"], 2_160)
            self.assertEqual(eye_stream["nb_read_frames"], "2")

        left_pts = self.frame_pts(left_path)
        right_pts = self.frame_pts(right_path)
        self.assertEqual(left_pts, right_pts)
        self.assertEqual(len(left_pts), 2)
        self.assertLess(left_pts[0], left_pts[1])
        self.assertLess(self.first_frame_gray_value(left_path), self.first_frame_gray_value(right_path))
        self.assert_no_partial_output(output_path)

    def test_metalfx_rejects_non_1080p_eyes_without_partial_output(self) -> None:
        output_path = self.output_path("metalfx-invalid-size.mov")

        completed = self.run_encoder(
            output_path,
            y4m_header(),
            prototype_metalfx_upscale=True,
        )

        self.assertEqual(completed.returncode, 1)
        self.assertIn(b"requires each SBS eye to be 1920x1080", completed.stderr)
        self.assertFalse(output_path.exists())
        self.assert_no_partial_output(output_path)

    def test_metalfx_failure_after_scaled_frame_removes_partial_output(self) -> None:
        self.require_metalfx_support()
        output_path = self.output_path("metalfx-truncated.mov")
        input_width = 3_840
        input_height = 1_080
        input_bytes = y4m_header(width=input_width, height=input_height)
        input_bytes += y4m_frame(0, width=input_width, height=input_height)
        input_bytes += b"FRAME\ntruncated"

        completed = self.run_encoder(
            output_path,
            input_bytes,
            expected_frames=2,
            prototype_metalfx_upscale=True,
        )

        self.assertEqual(completed.returncode, 1)
        self.assertIn(b"incomplete frame", completed.stderr)
        self.assertFalse(output_path.exists())
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

    def assert_sigterm_cleanup(self, *, prototype_metalfx_upscale: bool) -> None:
        if prototype_metalfx_upscale:
            self.require_metalfx_support()
        output_name = "metalfx-cancelled.mov" if prototype_metalfx_upscale else "cancelled.mov"
        output_path = self.output_path(output_name)
        command = [str(self.encoder), "--output", str(output_path), "--expected-frames", "2"]
        if prototype_metalfx_upscale:
            command.append("--prototype-metalfx-upscale")
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert process.stdin is not None
        assert process.stderr is not None
        if prototype_metalfx_upscale:
            process.stdin.write(y4m_header(width=3_840, height=1_080))
        else:
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

    def test_sigterm_cancels_and_removes_partial_output(self) -> None:
        self.assert_sigterm_cleanup(prototype_metalfx_upscale=False)

    def test_metalfx_sigterm_cancels_and_releases_partial_resources(self) -> None:
        self.assert_sigterm_cleanup(prototype_metalfx_upscale=True)


if __name__ == "__main__":
    unittest.main()
