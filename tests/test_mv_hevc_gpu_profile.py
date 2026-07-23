import subprocess
import tempfile
import unittest

from pathlib import Path

from scripts import profile_mv_hevc_gpu


class MVHEVCGPUProfileTests(unittest.TestCase):
    def test_xctrace_command_records_all_processes_with_metal_system_trace(self) -> None:
        command = profile_mv_hevc_gpu.xctrace_record_command(
            Path("phase.trace"),
            time_limit_seconds=90,
        )

        self.assertEqual(command[:4], ["xctrace", "record", "--template", "Metal System Trace"])
        self.assertIn("phase.trace", command)
        self.assertEqual(command[-1], "--all-processes")

    def test_process_recorder_captures_spawned_process_pid(self) -> None:
        with profile_mv_hevc_gpu.ProcessRecorder() as recorder:
            subprocess.run(["/usr/bin/true"], check=True)

        self.assertEqual(len(recorder.processes), 1)
        self.assertEqual(recorder.processes[0]["name"], "true")
        self.assertGreater(recorder.processes[0]["pid"], 0)

    def test_trace_toc_allows_all_processes_recording_without_launch_target(self) -> None:
        xml = """<?xml version="1.0"?>
<trace-toc><run number="1"><info><summary><duration>2.5</duration></summary></info>
<processes><process name="ffmpeg" pid="10" path="/usr/bin/ffmpeg"/></processes>
</run></trace-toc>
"""
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "toc.xml"
            path.write_text(xml, encoding="utf-8")

            toc = profile_mv_hevc_gpu.parse_trace_toc(path)

        self.assertIsNone(toc["target_pid"])
        self.assertEqual(toc["duration_seconds"], 2.5)
        self.assertEqual(toc["processes"][0]["pid"], 10)

    def test_parse_metal_gpu_intervals_resolves_duration_and_process_refs(self) -> None:
        xml = """<?xml version="1.0"?>
<trace-query-result><node>
<schema name="metal-gpu-intervals">
<col><mnemonic>start</mnemonic></col><col><mnemonic>duration</mnemonic></col>
<col><mnemonic>process</mnemonic></col>
</schema>
<row>
<start-time id="1">0</start-time><duration id="2">100</duration>
<process id="3" fmt="ffmpeg (10)"><pid>10</pid></process>
</row><row>
<start-time ref="1"/><duration ref="2"/><process ref="3"/>
</row></node></trace-query-result>
"""
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "gpu.xml"
            path.write_text(xml, encoding="utf-8")

            intervals = profile_mv_hevc_gpu.parse_metal_gpu_intervals(path)

        record = intervals[(10, "ffmpeg")]
        self.assertEqual(record["gpu_time_ns"], 100)
        self.assertEqual(record["gpu_interval_duration_sum_ns"], 200)
        self.assertEqual(record["gpu_interval_count"], 2)

    def test_parse_metal_gpu_intervals_rejects_unresolved_references(self) -> None:
        xml = """<?xml version="1.0"?>
<trace-query-result><node>
<schema name="metal-gpu-intervals">
<col><mnemonic>start</mnemonic></col><col><mnemonic>duration</mnemonic></col>
<col><mnemonic>process</mnemonic></col>
</schema>
<row><start-time>0</start-time><duration ref="missing"/>
<process fmt="ffmpeg (10)"><pid>10</pid></process></row>
</node></trace-query-result>
"""
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "gpu.xml"
            path.write_text(xml, encoding="utf-8")

            with self.assertRaisesRegex(
                profile_mv_hevc_gpu.QualificationFailure,
                "unresolved duration",
            ):
                profile_mv_hevc_gpu.parse_metal_gpu_intervals(path)

    def test_parse_metal_gpu_intervals_handles_real_shaped_rows(self) -> None:
        xml = """<?xml version="1.0"?>
<trace-query-result><node>
<schema name="metal-gpu-intervals">
<col><mnemonic>start</mnemonic></col><col><mnemonic>duration</mnemonic></col>
<col><mnemonic>frame-number</mnemonic></col><col><mnemonic>start-latency</mnemonic></col>
<col><mnemonic>event-label</mnemonic></col><col><mnemonic>process</mnemonic></col>
</schema>
<row>
<start-time id="1">10</start-time><duration id="2">5</duration><sentinel/>
<duration id="3">999</duration>
<formatted-label><process id="4" fmt="ffmpeg (10)"><pid>10</pid></process></formatted-label>
<process ref="4"/>
</row>
<row><start-time>20</start-time><duration>7</duration><sentinel/>
<duration ref="3"/><process ref="4"/></row>
<row><start-time>30</start-time><duration>8</duration><sentinel/></row>
</node></trace-query-result>
"""
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "gpu.xml"
            path.write_text(xml, encoding="utf-8")

            intervals = profile_mv_hevc_gpu.parse_metal_gpu_intervals(path)

        record = intervals[(10, "ffmpeg")]
        self.assertEqual(record["gpu_time_ns"], 12)
        self.assertEqual(record["gpu_interval_duration_sum_ns"], 12)
        self.assertEqual(record["gpu_interval_count"], 2)

    def test_manifest_fingerprints_canonical_summary_and_writes_detached_checksum(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            session_directory = Path(temporary_directory)
            summary_path = session_directory / "measurement-summary.json"
            summary_path.write_text('{"schema_version": 2}\n', encoding="utf-8")

            manifest_path, checksum_path, manifest_sha256 = profile_mv_hevc_gpu.write_evidence_manifest(
                session_directory,
                {"session_id": "test"},
            )
            manifest = profile_mv_hevc_gpu.load_json_object(manifest_path)

            self.assertIn(summary_path.name, manifest["artifacts"])
            self.assertEqual(
                checksum_path.read_text(encoding="utf-8"),
                f"{manifest_sha256}  {manifest_path.name}\n",
            )

    def test_videotoolbox_service_match_includes_encoder_and_decoder(self) -> None:
        self.assertTrue(profile_mv_hevc_gpu.is_videotoolbox_service("VTEncoderXPCService"))
        self.assertTrue(profile_mv_hevc_gpu.is_videotoolbox_service("VTDecoderXPCService"))
        self.assertFalse(profile_mv_hevc_gpu.is_videotoolbox_service("spatial-media-kit-tool"))

    def test_gpu_summary_reports_numeric_zero_for_observed_process(self) -> None:
        toc = {
            "duration_seconds": 2.0,
            "processes": [{"name": "ffmpeg", "pid": 10}],
        }

        summary = profile_mv_hevc_gpu.summarize_target_gpu(
            toc,
            {},
            [{"name": "ffmpeg", "pid": 10}],
            phase_elapsed_seconds=1.0,
        )

        self.assertTrue(summary["all_target_processes_observed"])
        self.assertEqual(summary["agx_gpu_time_ns"], 0)
        self.assertEqual(summary["agx_gpu_utilization_percent"], 0.0)


if __name__ == "__main__":
    unittest.main()
