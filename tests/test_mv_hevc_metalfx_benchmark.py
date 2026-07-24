import subprocess
import unittest

from pathlib import Path
from unittest.mock import Mock

from scripts import benchmark_mv_hevc_metalfx


class MVHEVCMetalFXBenchmarkTests(unittest.TestCase):
    def test_encoder_command_selects_explicit_upscale_mode(self) -> None:
        command = benchmark_mv_hevc_metalfx.encoder_command(
            Path("mv-hevc-encoder"),
            Path("output.mov"),
            frames=240,
            bitrate_mbps=20,
            upscale_mode="metalfx",
        )

        self.assertEqual(command[-2:], ["--upscale-mode", "metalfx"])

    def test_declared_pool_payload_counts_per_eye_pools(self) -> None:
        source = 1_920 * 1_080 * 3 // 2 * 4
        output = 3_840 * 2_160 * 4 * 4 * 2
        converted = 1_920 * 1_080 * 4 * 2 * 2
        intermediate = 3_840 * 2_160 * 4 * 2

        self.assertEqual(
            benchmark_mv_hevc_metalfx.declared_pool_payload_bytes("pixel-transfer"),
            source + output,
        )
        self.assertEqual(
            benchmark_mv_hevc_metalfx.declared_pool_payload_bytes("metalfx"),
            source + converted + intermediate + output,
        )
        self.assertEqual(
            benchmark_mv_hevc_metalfx.declared_metal_payload_bytes("metalfx"),
            converted + intermediate + output,
        )

    def test_boundedness_gate_uses_duration_growth_and_baseline(self) -> None:
        stream: dict[str, object] = {}
        summary: dict[str, object] = {}
        short_metal_summary: dict[str, object] = {"metalfx_device_peak_delta_bytes": 100}
        long_metal_summary: dict[str, object] = {"metalfx_device_peak_delta_bytes": 150}
        baseline_short = benchmark_mv_hevc_metalfx.BenchmarkRun(1, 200, "baseline", 1, stream, summary)
        baseline_long = benchmark_mv_hevc_metalfx.BenchmarkRun(10, 480, "baseline", 1, stream, summary)
        short = benchmark_mv_hevc_metalfx.BenchmarkRun(1, 220, "metalfx", 1, stream, short_metal_summary)
        stable_long = benchmark_mv_hevc_metalfx.BenchmarkRun(
            20,
            460,
            "metalfx",
            1,
            stream,
            long_metal_summary,
        )
        growing_long = benchmark_mv_hevc_metalfx.BenchmarkRun(
            20,
            480 + benchmark_mv_hevc_metalfx.declared_pool_payload_bytes("metalfx") + 101,
            "metalfx",
            1,
            stream,
            long_metal_summary,
        )

        self.assertTrue(
            benchmark_mv_hevc_metalfx.boundedness_result(
                baseline_short,
                baseline_long,
                short,
                stable_long,
                mode="metalfx",
                max_unmodeled_growth_bytes=100,
            )["passed"]
        )
        self.assertFalse(
            benchmark_mv_hevc_metalfx.boundedness_result(
                baseline_short,
                baseline_long,
                short,
                growing_long,
                mode="metalfx",
                max_unmodeled_growth_bytes=100,
            )["passed"]
        )

    def test_kill_process_reports_an_unreaped_child_and_closes_streams(self) -> None:
        streams = [Mock(closed=False) for _ in range(3)]
        process = Mock(pid=357, stdin=streams[0], stdout=streams[1], stderr=streams[2])
        process.poll.return_value = None
        process.wait.side_effect = subprocess.TimeoutExpired("encoder", 30)

        with self.assertRaisesRegex(benchmark_mv_hevc_metalfx.QualificationFailure, "Failed to reap process 357"):
            benchmark_mv_hevc_metalfx.kill_process(process)

        process.kill.assert_called_once_with()
        for stream in streams:
            stream.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
