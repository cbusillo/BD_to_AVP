import json
import platform
import select
import shutil
import signal
import subprocess
import tempfile
import time
import unittest

from contextlib import suppress
from pathlib import Path
from unittest.mock import Mock

from scripts import build_mv_hevc_encoder_macos, qualify_direct_mv_hevc


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
            (Path("MVHEVCEncoder.swift"), Path("FrameUpscaler.swift")),
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
        self.assertIn("FrameUpscaler.swift", command)
        self.assertEqual(
            command[-12:],
            [
                "-framework",
                "AVFoundation",
                "-framework",
                "CoreMedia",
                "-framework",
                "CoreVideo",
                "-framework",
                "Metal",
                "-framework",
                "MetalFX",
                "-framework",
                "VideoToolbox",
            ],
        )

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

    def test_segmented_hls_source_contract_uses_asset_writer_segmentation(self) -> None:
        source = build_mv_hevc_encoder_macos.SOURCE_PATH.read_text(encoding="utf-8")

        self.assertIn("--hls-directory", source)
        self.assertIn("--segment-duration", source)
        self.assertIn("Specify exactly one of --output or --hls-directory.", source)
        self.assertIn("AVAssetWriter(contentType: .mpeg4Movie)", source)
        self.assertIn("writer.outputFileTypeProfile = .mpeg4AppleHLS", source)
        self.assertIn("writer.preferredOutputSegmentInterval", source)
        self.assertIn("AVAssetWriterDelegate", source)
        self.assertIn('#EXT-X-MAP:URI=\\"init.mp4\\"', source)
        self.assertIn('endList ? "VOD" : "EVENT"', source)
        self.assertIn("#EXT-X-ENDLIST", source)
        self.assertIn("segment-%05d.m4s", source)

    def test_segmented_throughput_probe_contract_is_bounded_and_machine_readable(self) -> None:
        source = (REPOSITORY_ROOT / "scripts/measure_segmented_mv_hevc_throughput.py").read_text(encoding="utf-8")

        self.assertIn('RAINFOREST_ISO_ENV = "BD_TO_AVP_RAINFOREST_ISO"', source)
        self.assertIn('"--y4m"', source)
        self.assertIn('"--rainforest"', source)
        self.assertIn('"--max-frames"', source)
        self.assertIn('"hardware_fingerprint_sha256"', source)
        self.assertIn('"storage_fingerprint_sha256"', source)
        self.assertIn('"tool_hashes"', source)
        self.assertIn('"realtime_ratio"', source)

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
            if probe.returncode == 2 and probe_payload.get("stereo_mv_hevc_encode_supported") is False:
                raise unittest.SkipTest("this Mac cannot create the bounded MV-HEVC fixture")
            if probe.returncode != 0:
                diagnostic = probe.stderr.decode(errors="replace")
                raise RuntimeError(f"MV-HEVC capability probe failed:\n{diagnostic}")
            if probe_payload.get("schema_version") != 1 or not probe_payload.get("stereo_mv_hevc_encode_supported"):
                raise RuntimeError(f"unexpected MV-HEVC capability probe output: {probe_payload!r}")
            cls.metalfx_supported = bool(probe_payload.get("metalfx_spatial_scaling_supported"))
            cls.metalfx_upscale_supported = bool(probe_payload.get("metalfx_2x_mv_hevc_supported"))
            cls.pixel_transfer_upscale_supported = bool(probe_payload.get("pixel_transfer_2x_mv_hevc_supported"))
            cls.segmented_hls_supported = bool(probe_payload.get("segmented_hls_mv_hevc_encode_supported"))
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

    def hls_directory(self, name: str) -> Path:
        path = Path(self.temporary_directory.name) / name
        shutil.rmtree(path, ignore_errors=True)
        for partial in path.parent.glob(f".{path.name}.partial-*"):
            shutil.rmtree(partial, ignore_errors=True)
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
        upscale_mode: str | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        command = [str(self.encoder), "--output", str(output_path)]
        if bitrate_mbps is not None:
            command.extend(["--bitrate-mbps", str(bitrate_mbps)])
        if quality is not None:
            command.extend(["--quality", str(quality)])
        if expected_frames is not None:
            command.extend(["--expected-frames", str(expected_frames)])
        if upscale_mode is not None:
            command.extend(["--upscale-mode", upscale_mode])
        if overwrite:
            command.append("--overwrite")
        return subprocess.run(
            command,
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )

    def run_hls_encoder(
        self,
        directory: Path,
        input_bytes: bytes,
        *,
        segment_duration: float = 0.5,
        expected_frames: int | None = None,
        overwrite: bool = False,
    ) -> subprocess.CompletedProcess[bytes]:
        command = [
            str(self.encoder),
            "--hls-directory",
            str(directory),
            "--segment-duration",
            str(segment_duration),
        ]
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

    def wait_for_event_playlist(self, playlist_path: Path) -> str:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if playlist_path.is_file():
                playlist = playlist_path.read_text(encoding="utf-8")
                if "#EXT-X-PLAYLIST-TYPE:EVENT" in playlist and "segment-" in playlist:
                    return playlist
            time.sleep(0.02)
        self.fail("encoder did not publish an EVENT playlist within ten seconds")

    @staticmethod
    def target_duration(playlist: str) -> int:
        prefix = "#EXT-X-TARGETDURATION:"
        return int(next(line.removeprefix(prefix) for line in playlist.splitlines() if line.startswith(prefix)))

    def frame_pts(self, path: Path) -> list[float]:
        assert FFPROBE is not None
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
        assert FFMPEG is not None
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
                "metalfx_2x_mv_hevc_supported": self.metalfx_upscale_supported,
                "metalfx_spatial_scaling_supported": self.metalfx_supported,
                "pixel_transfer_2x_mv_hevc_supported": self.pixel_transfer_upscale_supported,
                "schema_version": 1,
                "segmented_hls_mv_hevc_encode_supported": self.segmented_hls_supported,
                "stereo_mv_hevc_encode_supported": True,
            },
        )

    def test_help_documents_mutually_exclusive_segmented_output(self) -> None:
        completed = subprocess.run(
            [str(self.encoder), "--help"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        self.assertIn(b"--hls-directory DIR", completed.stdout)
        self.assertIn(b"--segment-duration SECONDS", completed.stdout)
        self.assertIn(b"exclusive with --hls-directory", completed.stdout)

    def test_rejects_multiple_output_modes_before_creating_output(self) -> None:
        output_path = self.output_path("exclusive.mov")
        hls_directory = self.hls_directory("exclusive-hls")

        completed = subprocess.run(
            [
                str(self.encoder),
                "--output",
                str(output_path),
                "--hls-directory",
                str(hls_directory),
            ],
            input=y4m_header(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )

        self.assertEqual(completed.returncode, 1)
        self.assertIn(b"exactly one", completed.stderr)
        self.assertFalse(output_path.exists())
        self.assertFalse(hls_directory.exists())

    def test_rejects_non_positive_hls_segment_duration_before_creating_output(self) -> None:
        hls_directory = self.hls_directory("invalid-segment-duration")

        completed = self.run_hls_encoder(hls_directory, y4m_header(), segment_duration=0)

        self.assertEqual(completed.returncode, 1)
        self.assertIn(b"positive number of seconds", completed.stderr)
        self.assertFalse(hls_directory.exists())

    def test_rejects_segment_duration_without_hls_output(self) -> None:
        output_path = self.output_path("segment-duration.mov")

        completed = subprocess.run(
            [str(self.encoder), "--output", str(output_path), "--segment-duration", "2"],
            input=y4m_header(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )

        self.assertEqual(completed.returncode, 1)
        self.assertIn(b"requires --hls-directory", completed.stderr)
        self.assertFalse(output_path.exists())

    def test_hls_output_writes_cmaf_segments_and_final_vod_playlist(self) -> None:
        if not self.segmented_hls_supported:
            self.skipTest("this Mac cannot create segmented HLS MV-HEVC output")
        hls_directory = self.hls_directory("segmented-hls")
        input_bytes = y4m_header() + b"".join(y4m_frame(index) for index in range(48))

        completed = self.run_hls_encoder(
            hls_directory,
            input_bytes,
            segment_duration=0.5,
            expected_frames=48,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        summary = json.loads(completed.stdout)
        self.assertEqual(summary["output_mode"], "hls")
        self.assertEqual(summary["frame_count"], 48)
        self.assertGreater(summary["hls_segment_count"], 0)
        initialization_segment = hls_directory / "init.mp4"
        playlist_path = hls_directory / "media.m3u8"
        self.assertGreater(initialization_segment.stat().st_size, 0)
        playlist = playlist_path.read_text(encoding="utf-8")
        self.assertIn("#EXTM3U", playlist)
        self.assertIn("#EXT-X-PLAYLIST-TYPE:VOD", playlist)
        self.assertIn('#EXT-X-MAP:URI="init.mp4"', playlist)
        self.assertIn("#EXT-X-ENDLIST", playlist)
        target_duration = self.target_duration(playlist)
        self.assertEqual(target_duration, 2)
        durations = [
            float(line.removeprefix("#EXTINF:").removesuffix(","))
            for line in playlist.splitlines()
            if line.startswith("#EXTINF:")
        ]
        self.assertTrue(durations)
        self.assertTrue(all(duration <= target_duration for duration in durations))
        segment_names = sorted(path.name for path in hls_directory.glob("segment-*.m4s"))
        self.assertEqual(len(segment_names), summary["hls_segment_count"])
        for segment_name in segment_names:
            self.assertIn(segment_name, playlist)
            self.assertGreater((hls_directory / segment_name).stat().st_size, 0)

    def test_hls_target_duration_is_stable_from_event_to_vod(self) -> None:
        if not self.segmented_hls_supported:
            self.skipTest("this Mac cannot create segmented HLS MV-HEVC output")
        hls_directory = self.hls_directory("growing-hls")
        process = subprocess.Popen(
            [
                str(self.encoder),
                "--hls-directory",
                str(hls_directory),
                "--segment-duration",
                "0.5",
                "--expected-frames",
                "96",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert process.stdin is not None
        process.stdin.write(y4m_header())
        for frame_index in range(48):
            process.stdin.write(y4m_frame(frame_index))
        process.stdin.flush()

        event_playlist = self.wait_for_event_playlist(hls_directory / "media.m3u8")
        self.assertNotIn("#EXT-X-ENDLIST", event_playlist)
        event_target_duration = self.target_duration(event_playlist)

        for frame_index in range(48, 96):
            process.stdin.write(y4m_frame(frame_index))
        process.stdin.close()
        process.stdin = None
        stdout, stderr = process.communicate(timeout=30)

        self.assertEqual(process.returncode, 0, stderr.decode())
        self.assertEqual(json.loads(stdout)["frame_count"], 96)
        final_playlist = (hls_directory / "media.m3u8").read_text(encoding="utf-8")
        self.assertIn("#EXT-X-PLAYLIST-TYPE:VOD", final_playlist)
        self.assertIn("#EXT-X-ENDLIST", final_playlist)
        self.assertEqual(self.target_duration(final_playlist), event_target_duration)

    def test_hls_failed_overwrite_restores_existing_directory(self) -> None:
        if not self.segmented_hls_supported:
            self.skipTest("this Mac cannot create segmented HLS MV-HEVC output")
        hls_directory = self.hls_directory("preserve-hls")
        hls_directory.mkdir()
        marker = hls_directory / "existing.txt"
        marker.write_text("existing output", encoding="utf-8")

        completed = self.run_hls_encoder(
            hls_directory,
            y4m_header() + b"FRAME\ntruncated",
            expected_frames=1,
            overwrite=True,
        )

        self.assertEqual(completed.returncode, 1)
        self.assertIn(b"incomplete frame", completed.stderr)
        self.assertEqual(marker.read_text(encoding="utf-8"), "existing output")
        self.assertEqual(list(hls_directory.parent.glob(f".{hls_directory.name}.previous-*")), [])

    def test_hls_failure_removes_partial_directory(self) -> None:
        if not self.segmented_hls_supported:
            self.skipTest("this Mac cannot create segmented HLS MV-HEVC output")
        hls_directory = self.hls_directory("truncated-hls")

        completed = self.run_hls_encoder(
            hls_directory,
            y4m_header() + b"FRAME\ntruncated",
            expected_frames=1,
        )

        self.assertEqual(completed.returncode, 1)
        self.assertIn(b"incomplete frame", completed.stderr)
        self.assertFalse(hls_directory.exists())

    def test_hls_sigterm_removes_partial_directory(self) -> None:
        if not self.segmented_hls_supported:
            self.skipTest("this Mac cannot create segmented HLS MV-HEVC output")
        hls_directory = self.hls_directory("cancelled-hls")
        process = subprocess.Popen(
            [str(self.encoder), "--hls-directory", str(hls_directory), "--expected-frames", "2"],
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
        self.assertEqual(json.loads(cancellation_line)["event"], "encoder.cancellation_requested")
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
        self.assertFalse(hls_directory.exists())

    def test_rejects_unknown_upscale_mode(self) -> None:
        output_path = self.output_path("invalid-upscale.mov")

        completed = self.run_encoder(output_path, b"", upscale_mode="unknown")

        self.assertEqual(completed.returncode, 1)
        self.assertIn(b"must be metalfx or pixel-transfer", completed.stderr)
        self.assertFalse(output_path.exists())

    @unittest.skipUnless(FFMPEG and FFPROBE, "upscaled MV-HEVC validation requires ffmpeg and ffprobe")
    def test_upscale_modes_produce_bounded_4k_stereo_output(self) -> None:
        input_width = 3_840
        input_height = 1_080
        input_bytes = y4m_header(width=input_width, height=input_height)
        input_bytes += y4m_frame(0, width=input_width, height=input_height)
        input_bytes += y4m_frame(1, width=input_width, height=input_height)
        for mode in ("pixel-transfer", "metalfx"):
            if mode == "metalfx" and not self.metalfx_upscale_supported:
                continue
            if mode == "pixel-transfer" and not self.pixel_transfer_upscale_supported:
                continue
            with self.subTest(mode=mode):
                output_path = self.output_path(f"upscaled-{mode}.mov")
                completed = self.run_encoder(
                    output_path,
                    input_bytes,
                    expected_frames=2,
                    upscale_mode=mode,
                )

                self.assertEqual(completed.returncode, 0, completed.stderr.decode())
                summary = json.loads(completed.stdout)
                self.assertEqual(summary["input_eye_width"], 1_920)
                self.assertEqual(summary["input_eye_height"], 1_080)
                self.assertEqual(summary["eye_width"], 3_840)
                self.assertEqual(summary["eye_height"], 2_160)
                self.assertEqual(summary["frame_count"], 2)
                self.assertEqual(summary["upscale_mode"], mode)
                self.assertEqual(summary["upscale_source_buffer_limit"], 4)
                self.assertEqual(summary["upscale_converted_input_buffer_limit_per_eye"], 2)
                self.assertEqual(summary["upscale_output_buffer_limit_per_eye"], 4)
                if mode == "metalfx":
                    initial_allocated = summary["metalfx_device_initial_allocated_size_bytes"]
                    peak_allocated = summary["metalfx_device_peak_allocated_size_bytes"]
                    final_allocated = summary["metalfx_device_final_allocated_size_bytes"]
                    self.assertGreaterEqual(peak_allocated, initial_allocated)
                    self.assertGreaterEqual(peak_allocated, final_allocated)
                    self.assertEqual(
                        summary["metalfx_device_peak_delta_bytes"],
                        peak_allocated - initial_allocated,
                    )
                else:
                    self.assertNotIn("metalfx_device_peak_delta_bytes", summary)
                stream = qualify_direct_mv_hevc.ffprobe_stream(
                    FFPROBE,
                    output_path,
                )
                self.assertEqual(stream["width"], 3_840)
                self.assertEqual(stream["height"], 2_160)
                self.assertEqual(stream["nb_read_frames"], "2")
                self.assertEqual(stream["color_range"], "tv")
                self.assertEqual(stream["color_space"], "bt709")
                self.assertEqual(stream["color_transfer"], "bt709")
                self.assertEqual(stream["color_primaries"], "bt709")
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
                split_directory = Path(self.temporary_directory.name) / f"split-{mode}"
                left_path, right_path = qualify_direct_mv_hevc.split_mv_hevc(
                    output_path,
                    split_directory,
                )
                left_pts = self.frame_pts(left_path)
                right_pts = self.frame_pts(right_path)
                self.assertEqual(left_pts, right_pts)
                self.assertEqual(len(left_pts), 2)
                self.assertLess(left_pts[0], left_pts[1])
                self.assertLess(self.first_frame_gray_value(left_path), self.first_frame_gray_value(right_path))
                self.assert_no_partial_output(output_path)

    def test_upscale_rejects_non_1080p_eyes_without_partial_output(self) -> None:
        modes = ["pixel-transfer"]
        if self.metalfx_upscale_supported:
            modes.append("metalfx")
        for mode in modes:
            with self.subTest(mode=mode):
                output_path = self.output_path(f"invalid-upscale-size-{mode}.mov")

                completed = self.run_encoder(
                    output_path,
                    y4m_header(),
                    upscale_mode=mode,
                )

                self.assertEqual(completed.returncode, 1)
                self.assertIn(b"requires each SBS eye to be 1920x1080", completed.stderr)
                self.assertFalse(output_path.exists())
                self.assert_no_partial_output(output_path)

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

    def assert_sigterm_cleanup(self, *, upscale_mode: str | None) -> None:
        if upscale_mode == "metalfx" and not self.metalfx_upscale_supported:
            self.skipTest("this Mac cannot run the MetalFX MV-HEVC prototype")
        output_name = "upscale-cancelled.mov" if upscale_mode else "cancelled.mov"
        output_path = self.output_path(output_name)
        command = [str(self.encoder), "--output", str(output_path), "--expected-frames", "2"]
        if upscale_mode is not None:
            command.extend(["--upscale-mode", upscale_mode])
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert process.stdin is not None
        assert process.stderr is not None
        if upscale_mode is None:
            process.stdin.write(y4m_header())
        else:
            process.stdin.write(y4m_header(width=3_840, height=1_080))
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
        self.assert_sigterm_cleanup(upscale_mode=None)

    def test_metalfx_sigterm_cancels_and_releases_partial_resources(self) -> None:
        self.assert_sigterm_cleanup(upscale_mode="metalfx")


if __name__ == "__main__":
    unittest.main()
