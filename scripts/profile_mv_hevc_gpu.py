#!/usr/bin/env python3

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import platform
import re
import select
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeVar

from scripts import build_mv_hevc_encoder_macos
from scripts.qualify_direct_mv_hevc import (
    FixtureOptions,
    QualificationFailure,
    command_path,
    encode_current,
    encode_direct,
    metric_dict,
)


VIDEOTOOLBOX_SERVICE_SUFFIX = "XPCService"
METAL_CONTROL_NAME = "bd-avp-gpu-ctl"
METAL_GPU_XPATH = '/trace-toc/run[@number="1"]/data/table[@schema="metal-gpu-intervals"]'
DEFAULT_TRACE_LIMIT_SECONDS = 60
DEFAULT_POSITIVE_CONTROL_SECONDS = 4
WorkloadResult = TypeVar("WorkloadResult")

METAL_CONTROL_SOURCE = r'''
import Foundation
import Metal

let seconds = CommandLine.arguments.count > 1 ? Double(CommandLine.arguments[1]) ?? 4.0 : 4.0
guard let device = MTLCreateSystemDefaultDevice(), let queue = device.makeCommandQueue() else {
    fputs("Metal unavailable\n", stderr)
    exit(2)
}
let source = """
#include <metal_stdlib>
using namespace metal;
kernel void spin(device float *values [[buffer(0)]], uint index [[thread_position_in_grid]]) {
    float value = values[index] + float(index & 255u) * 0.0001f;
    for (uint iteration = 0; iteration < 128; ++iteration) {
        value = fma(value, 1.000001f, 0.000001f);
    }
    values[index] = value;
}
"""
let library = try device.makeLibrary(source: source, options: nil)
let function = library.makeFunction(name: "spin")!
let pipeline = try device.makeComputePipelineState(function: function)
let count = 65_536
let buffer = device.makeBuffer(
    length: count * MemoryLayout<Float>.stride,
    options: .storageModeShared
)!
let deadline = Date().timeIntervalSinceReferenceDate + seconds
while Date().timeIntervalSinceReferenceDate < deadline {
    autoreleasepool {
        let commandBuffer = queue.makeCommandBuffer()!
        let encoder = commandBuffer.makeComputeCommandEncoder()!
        encoder.setComputePipelineState(pipeline)
        encoder.setBuffer(buffer, offset: 0, index: 0)
        let width = min(pipeline.maxTotalThreadsPerThreadgroup, 256)
        encoder.dispatchThreads(
            MTLSize(width: count, height: 1, depth: 1),
            threadsPerThreadgroup: MTLSize(width: width, height: 1, depth: 1)
        )
        encoder.endEncoding()
        commandBuffer.commit()
        commandBuffer.waitUntilCompleted()
    }
}
'''


def xctrace_record_command(
    trace_path: Path,
    *,
    time_limit_seconds: int,
) -> list[str]:
    return [
        "xctrace",
        "record",
        "--template",
        "Metal System Trace",
        "--time-limit",
        f"{time_limit_seconds}s",
        "--output",
        str(trace_path),
        "--no-prompt",
        "--all-processes",
    ]


def run_checked(
    command: list[str],
    *,
    cwd: Path,
    timeout_seconds: int,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as error:
        raise QualificationFailure(f"Command timed out: {command[0]}") from error
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise QualificationFailure(f"Command failed ({command[0]}):\n{detail}")
    return result


def build_metal_control(output_path: Path, *, cwd: Path) -> None:
    run_checked(
        [
            "xcrun",
            "swiftc",
            "-O",
            "-framework",
            "Metal",
            "-o",
            str(output_path),
            "-",
        ],
        cwd=cwd,
        timeout_seconds=180,
        input_text=METAL_CONTROL_SOURCE,
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fingerprint_path(path: Path) -> dict[str, object]:
    if path.is_file():
        return {
            "bytes": path.stat().st_size,
            "kind": "file",
            "sha256": sha256_file(path),
        }
    if not path.is_dir():
        raise QualificationFailure(f"Evidence artifact is unavailable: {path}")
    digest = hashlib.sha256()
    total_bytes = 0
    file_count = 0
    for file_path in sorted(candidate for candidate in path.rglob("*") if candidate.is_file()):
        relative_path = file_path.relative_to(path).as_posix()
        file_digest = sha256_file(file_path)
        file_size = file_path.stat().st_size
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_digest.encode("ascii"))
        digest.update(b"\0")
        digest.update(str(file_size).encode("ascii"))
        digest.update(b"\0")
        total_bytes += file_size
        file_count += 1
    return {
        "bytes": total_bytes,
        "file_count": file_count,
        "kind": "directory",
        "sha256": digest.hexdigest(),
    }


def command_text(command: list[str], *, cwd: Path) -> str:
    return run_checked(command, cwd=cwd, timeout_seconds=30).stdout.strip()


def collect_provenance(
    encoder_path: Path,
    *,
    cwd: Path,
    session_name: str,
) -> dict[str, object]:
    return {
        "argv": sys.argv,
        "encoder_sha256": sha256_file(encoder_path),
        "git_head": command_text(["git", "rev-parse", "HEAD"], cwd=cwd),
        "hardware_model": command_text(["sysctl", "-n", "hw.model"], cwd=cwd),
        "machine": platform.machine(),
        "macos_version": platform.mac_ver()[0],
        "profiler_sha256": sha256_file(Path(__file__).resolve()),
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "session_id": session_name,
        "xctrace_version": command_text(["xctrace", "version"], cwd=cwd),
    }


def write_evidence_manifest(
    session_directory: Path,
    provenance: dict[str, object],
) -> tuple[Path, Path, str]:
    artifacts = {
        path.name: fingerprint_path(path)
        for path in sorted(session_directory.iterdir())
        if path.name not in {"evidence-manifest.json", "evidence-manifest.sha256"}
    }
    manifest = {
        "artifacts": artifacts,
        "provenance": provenance,
        "schema_version": 1,
    }
    manifest_path = session_directory / "evidence-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    manifest_sha256 = sha256_file(manifest_path)
    checksum_path = session_directory / "evidence-manifest.sha256"
    checksum_path.write_text(f"{manifest_sha256}  {manifest_path.name}\n", encoding="utf-8")
    return manifest_path, checksum_path, manifest_sha256


def record_trace(
    trace_path: Path,
    workload: Callable[[], WorkloadResult],
    *,
    cwd: Path,
    time_limit_seconds: int,
) -> WorkloadResult:
    if trace_path.exists():
        raise QualificationFailure(f"Refusing to replace existing trace evidence: {trace_path}")
    process = subprocess.Popen(
        xctrace_record_command(
            trace_path,
            time_limit_seconds=time_limit_seconds,
        ),
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    assert process.stdout is not None
    output_lines: list[str] = []
    workload_result: WorkloadResult | None = None
    workload_error: BaseException | None = None
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            readable, _, _ = select.select([process.stdout], [], [], 0.5)
            if not readable:
                continue
            line = process.stdout.readline()
            if not line:
                continue
            output_lines.append(line)
            if "Ctrl-C to stop the recording" in line:
                break
        else:
            raise QualificationFailure("xctrace did not report that recording started.")
        if not any("Ctrl-C to stop the recording" in line for line in output_lines):
            raise QualificationFailure("xctrace exited before recording the workload.")
        workload_result = workload()
        if process.poll() is not None:
            raise QualificationFailure("xctrace ended before the workload completed.")
        time.sleep(0.5)
    except BaseException as error:
        workload_error = error
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
        try:
            remaining_output, _ = process.communicate(timeout=time_limit_seconds + 300)
        except subprocess.TimeoutExpired:
            process.kill()
            remaining_output, _ = process.communicate(timeout=30)
            if workload_error is None:
                workload_error = QualificationFailure("xctrace did not stop after the bounded workload.")
        if remaining_output:
            output_lines.append(remaining_output)

    output = "".join(output_lines)
    if not trace_path.exists():
        trace_error = QualificationFailure(f"xctrace did not create {trace_path}:\n{output.strip()}")
        if workload_error is not None:
            raise trace_error from workload_error
        raise trace_error
    if process.returncode != 0 and "Output file saved as:" not in output:
        trace_error = QualificationFailure(f"xctrace failed:\n{output.strip()}")
        if workload_error is not None:
            raise trace_error from workload_error
        raise trace_error
    if workload_error is not None:
        raise workload_error
    assert workload_result is not None
    return workload_result


def export_trace(trace_path: Path, export_directory: Path, *, cwd: Path) -> tuple[Path, Path]:
    toc_path = export_directory / f"{trace_path.stem}-toc.xml"
    gpu_path = export_directory / f"{trace_path.stem}-gpu.xml"
    run_checked(
        [
            "xctrace",
            "export",
            "--input",
            str(trace_path),
            "--toc",
            "--output",
            str(toc_path),
        ],
        cwd=cwd,
        timeout_seconds=300,
    )
    run_checked(
        [
            "xctrace",
            "export",
            "--input",
            str(trace_path),
            "--xpath",
            METAL_GPU_XPATH,
            "--output",
            str(gpu_path),
        ],
        cwd=cwd,
        timeout_seconds=300,
    )
    return toc_path, gpu_path


def parse_process_element(element: ET.Element) -> tuple[str, int] | None:
    name_and_pid = element.get("fmt", "")
    pid_element = element.find("pid")
    pid: int | None = None
    if pid_element is not None and pid_element.text:
        pid = int(pid_element.text)
    if pid is None:
        match = re.search(r" \((\d+)\)$", name_and_pid)
        if match:
            pid = int(match.group(1))
    if pid is None:
        return None
    suffix = f" ({pid})"
    name = name_and_pid.removesuffix(suffix)
    return name, pid


def is_videotoolbox_service(name: str) -> bool:
    return name.startswith("VT") and name.endswith(VIDEOTOOLBOX_SERVICE_SUFFIX)


def parse_trace_toc(path: Path) -> dict[str, object]:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as error:
        raise QualificationFailure(f"Could not parse Metal trace TOC: {path}") from error
    duration_element = root.find(".//summary/duration")
    if duration_element is None or not duration_element.text:
        raise QualificationFailure("Metal trace TOC does not contain a duration.")
    target_element = root.find(".//target/process")
    target_name = target_element.get("name", "") if target_element is not None else None
    target_pid = int(target_element.get("pid", "0")) if target_element is not None else None
    processes: list[dict[str, object]] = []
    for element in root.findall(".//run/processes/process"):
        pid_text = element.get("pid")
        if not pid_text:
            continue
        processes.append(
            {
                "name": element.get("name", ""),
                "path": element.get("path", ""),
                "pid": int(pid_text),
            }
        )
    return {
        "duration_seconds": float(duration_element.text),
        "processes": processes,
        "target_name": target_name,
        "target_pid": target_pid,
    }


def merged_interval_duration(intervals: list[tuple[int, int]]) -> int:
    if not intervals:
        return 0
    merged_total = 0
    current_start, current_end = sorted(intervals)[0]
    for start, end in sorted(intervals)[1:]:
        if start <= current_end:
            current_end = max(current_end, end)
        else:
            merged_total += current_end - current_start
            current_start, current_end = start, end
    return merged_total + current_end - current_start


def parse_metal_gpu_intervals(path: Path) -> dict[tuple[int, str], dict[str, object]]:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as error:
        raise QualificationFailure(f"Could not parse Metal GPU interval export: {path}") from error
    schema = root.find(".//schema[@name='metal-gpu-intervals']")
    if schema is None:
        raise QualificationFailure("Metal GPU export does not contain the expected schema.")
    mnemonics = {element.text for element in schema.findall("./col/mnemonic") if element.text}
    required_mnemonics = {"start", "duration", "process"}
    if not required_mnemonics.issubset(mnemonics):
        raise QualificationFailure("Metal GPU export is missing required schema columns.")

    starts_by_id: dict[str, int] = {}
    durations_by_id: dict[str, int] = {}
    processes_by_id: dict[str, tuple[str, int]] = {}
    for element in root.iter():
        element_id = element.get("id")
        if not element_id:
            continue
        if element.tag in {"start-time", "duration"} and element.text:
            try:
                value = int(element.text)
            except ValueError as error:
                raise QualificationFailure(f"Metal GPU export contains an invalid {element.tag} value.") from error
            if element.tag == "start-time":
                starts_by_id[element_id] = value
            else:
                durations_by_id[element_id] = value
        elif element.tag == "process":
            parsed = parse_process_element(element)
            if parsed is not None:
                processes_by_id[element_id] = parsed

    def resolve_integer(element: ET.Element, values_by_id: dict[str, int], label: str) -> int:
        if element.text:
            try:
                return int(element.text)
            except ValueError as error:
                raise QualificationFailure(f"Metal GPU export contains an invalid {label} value.") from error
        reference = element.get("ref")
        if not reference or reference not in values_by_id:
            raise QualificationFailure(f"Metal GPU export contains an unresolved {label} reference.")
        return values_by_id[reference]

    by_process: dict[tuple[int, str], dict[str, object]] = {}
    for row in root.iter("row"):
        start_element = row.find("start-time")
        duration_element = row.find("duration")
        process_element = row.find("process")
        if start_element is None or duration_element is None:
            raise QualificationFailure("Metal GPU export contains an incomplete interval row.")
        if process_element is None:
            continue
        start_ns = resolve_integer(start_element, starts_by_id, "start-time")
        duration_ns = resolve_integer(duration_element, durations_by_id, "duration")
        process_ref = process_element.get("ref")
        if process_ref:
            if process_ref not in processes_by_id:
                raise QualificationFailure("Metal GPU export contains an unresolved process reference.")
            process = processes_by_id[process_ref]
        else:
            process = parse_process_element(process_element)
        if process is None:
            raise QualificationFailure("Metal GPU export contains an invalid process value.")
        name, pid = process
        record = by_process.setdefault(
            (pid, name),
            {
                "gpu_interval_count": 0,
                "gpu_interval_duration_sum_ns": 0,
                "intervals": [],
                "name": name,
                "pid": pid,
            },
        )
        record["gpu_interval_count"] = int(record["gpu_interval_count"]) + 1
        record["gpu_interval_duration_sum_ns"] = int(record["gpu_interval_duration_sum_ns"]) + duration_ns
        intervals = record["intervals"]
        assert isinstance(intervals, list)
        intervals.append((start_ns, start_ns + duration_ns))

    for record in by_process.values():
        intervals = record["intervals"]
        assert isinstance(intervals, list)
        record["gpu_time_ns"] = merged_interval_duration(intervals)
    return by_process


def summarize_target_gpu(
    toc: dict[str, object],
    gpu_by_process: dict[tuple[int, str], dict[str, object]],
    targets: list[dict[str, object]],
    *,
    phase_elapsed_seconds: float,
) -> dict[str, object]:
    trace_processes = toc["processes"]
    assert isinstance(trace_processes, list)
    trace_processes_by_identity = {
        (int(process["pid"]), str(process["name"]))
        for process in trace_processes
        if isinstance(process, dict) and isinstance(process.get("pid"), int) and isinstance(process.get("name"), str)
    }
    normalized: list[dict[str, object]] = []
    aggregate_intervals: list[tuple[int, int]] = []
    total_interval_duration_sum_ns = 0
    total_intervals = 0
    for target in sorted(targets, key=lambda value: (str(value["name"]), int(value["pid"]))):
        pid = int(target["pid"])
        name = str(target["name"])
        record = gpu_by_process.get((pid, name), {})
        gpu_time_ns = int(record.get("gpu_time_ns", 0))
        interval_duration_sum_ns = int(record.get("gpu_interval_duration_sum_ns", 0))
        interval_count = int(record.get("gpu_interval_count", 0))
        intervals = record.get("intervals", [])
        if isinstance(intervals, list):
            aggregate_intervals.extend(intervals)
        total_interval_duration_sum_ns += interval_duration_sum_ns
        total_intervals += interval_count
        normalized.append(
            {
                "agx_gpu_interval_duration_sum_ns": interval_duration_sum_ns,
                "agx_gpu_interval_count": interval_count,
                "agx_gpu_time_ns": gpu_time_ns,
                "agx_gpu_utilization_percent": (100 * gpu_time_ns / (phase_elapsed_seconds * 1_000_000_000)),
                "name": name,
                "observed_in_trace": (pid, name) in trace_processes_by_identity,
                "pid": pid,
            }
        )
    total_gpu_time_ns = merged_interval_duration(aggregate_intervals)
    return {
        "agx_gpu_interval_duration_sum_ns": total_interval_duration_sum_ns,
        "agx_gpu_interval_count": total_intervals,
        "agx_gpu_time_ns": total_gpu_time_ns,
        "agx_gpu_utilization_percent": (100 * total_gpu_time_ns / (phase_elapsed_seconds * 1_000_000_000)),
        "all_target_processes_observed": all(bool(process["observed_in_trace"]) for process in normalized),
        "processes": normalized,
        "trace_duration_seconds": toc["duration_seconds"],
    }


class ProcessRecorder:
    def __init__(self) -> None:
        self.processes: list[dict[str, object]] = []
        self._original_popen = subprocess.Popen

    def __enter__(self) -> ProcessRecorder:
        original_popen = self._original_popen

        def recording_popen(*args, **kwargs):
            process = original_popen(*args, **kwargs)
            command = args[0] if args else kwargs.get("args")
            executable = command[0] if isinstance(command, (list, tuple)) else command
            self.processes.append(
                {
                    "name": Path(str(executable)).name,
                    "pid": process.pid,
                }
            )
            return process

        subprocess.Popen = recording_popen
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        subprocess.Popen = self._original_popen


def run_phase(
    phase: str,
    phase_directory: Path,
    result_path: Path,
    encoder_path: Path,
    options: FixtureOptions,
) -> None:
    ffmpeg = command_path("ffmpeg")
    phase_directory.mkdir(parents=True, exist_ok=True)
    with ProcessRecorder() as recorder:
        if phase == "current":
            output_path = phase_directory / "current.mov"
            metrics, intermediate_bytes = encode_current(
                ffmpeg,
                output_path,
                phase_directory,
                options,
            )
            phase_result: dict[str, object] = {
                **metric_dict(metrics),
                "final_bytes": output_path.stat().st_size,
                "peak_eye_intermediate_bytes": intermediate_bytes,
            }
        else:
            output_path = phase_directory / "direct.mov"
            encoder_summary, metrics = encode_direct(
                ffmpeg,
                encoder_path,
                output_path,
                options,
            )
            phase_result = {
                **metric_dict(metrics),
                "encoder_summary": encoder_summary,
                "final_bytes": output_path.stat().st_size,
                "peak_eye_intermediate_bytes": 0,
            }
    phase_result["observed_descendants"] = recorder.processes
    result_path.write_text(json.dumps(phase_result, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_json_object(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise QualificationFailure(f"Could not read phase evidence: {path}") from error
    if not isinstance(value, dict):
        raise QualificationFailure(f"Expected a JSON object in {path}")
    return value


def target_descendants(
    phase_result: dict[str, object],
    expected_names: set[str],
) -> list[dict[str, object]]:
    descendants = phase_result.get("observed_descendants", [])
    if not isinstance(descendants, list):
        raise QualificationFailure("Phase result has invalid descendant process evidence.")
    targets = [
        process for process in descendants if isinstance(process, dict) and process.get("name") in expected_names
    ]
    observed_names = {str(process["name"]) for process in targets}
    missing = expected_names - observed_names
    if missing:
        raise QualificationFailure(
            f"Process sampler did not observe expected phase processes: {', '.join(sorted(missing))}"
        )
    return targets


def profile_trace(
    label: str,
    workload: Callable[[], WorkloadResult],
    session_directory: Path,
    *,
    cwd: Path,
    trace_limit_seconds: int,
) -> tuple[WorkloadResult, dict[str, object], dict[tuple[int, str], dict[str, object]]]:
    trace_path = session_directory / f"{label}.trace"
    workload_result = record_trace(
        trace_path,
        workload,
        cwd=cwd,
        time_limit_seconds=trace_limit_seconds,
    )
    toc_path, gpu_path = export_trace(trace_path, session_directory, cwd=cwd)
    toc = parse_trace_toc(toc_path)
    intervals = parse_metal_gpu_intervals(gpu_path)
    compressed_gpu_path = gpu_path.with_suffix(f"{gpu_path.suffix}.gz")
    with gpu_path.open("rb") as source, gzip.open(compressed_gpu_path, "wb") as destination:
        shutil.copyfileobj(source, destination)
    gpu_path.unlink()
    return workload_result, toc, intervals


def profile_gpu(
    encoder_path: Path,
    options: FixtureOptions,
    raw_output_directory: Path,
    *,
    trace_limit_seconds: int,
    positive_control_seconds: int,
) -> dict[str, object]:
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise QualificationFailure("MV-HEVC GPU profiling requires macOS arm64.")
    for command in ("xctrace", "xcrun"):
        if shutil.which(command) is None:
            raise QualificationFailure(f"{command} is required for GPU profiling.")
    if not encoder_path.is_file():
        build_mv_hevc_encoder_macos.build_encoder(encoder_path)

    cwd = Path(__file__).resolve().parents[1]
    session_name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    session_directory = raw_output_directory.resolve() / session_name
    session_directory.mkdir(parents=True)

    with tempfile.TemporaryDirectory(prefix="bd-avp-gpu-control-") as control_directory:
        control_path = Path(control_directory) / METAL_CONTROL_NAME
        build_metal_control(control_path, cwd=cwd)

        def run_control() -> dict[str, object]:
            started = time.perf_counter()
            process = subprocess.Popen(
                [str(control_path), str(positive_control_seconds)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                _, stderr = process.communicate(timeout=positive_control_seconds + 30)
            except subprocess.TimeoutExpired as error:
                process.kill()
                process.wait(timeout=30)
                raise QualificationFailure("Metal positive control timed out.") from error
            if process.returncode != 0:
                raise QualificationFailure(
                    f"Metal positive control failed:\n{stderr.strip() or f'exit {process.returncode}'}"
                )
            return {
                "elapsed_seconds": time.perf_counter() - started,
                "pid": process.pid,
            }

        control_result, control_toc, control_intervals = profile_trace(
            "positive-control",
            run_control,
            session_directory,
            cwd=cwd,
            trace_limit_seconds=positive_control_seconds + 10,
        )
    control_pid = int(control_result["pid"])
    control_elapsed_seconds = float(control_result["elapsed_seconds"])
    control_record = control_intervals.get((control_pid, METAL_CONTROL_NAME), {})
    control_gpu_time_ns = int(control_record.get("gpu_time_ns", 0))
    control_interval_duration_sum_ns = int(control_record.get("gpu_interval_duration_sum_ns", 0))
    control_interval_count = int(control_record.get("gpu_interval_count", 0))
    positive_control = {
        "agx_gpu_interval_duration_sum_ns": control_interval_duration_sum_ns,
        "agx_gpu_interval_count": control_interval_count,
        "agx_gpu_time_ns": control_gpu_time_ns,
        "agx_gpu_utilization_percent": (100 * control_gpu_time_ns / (control_elapsed_seconds * 1_000_000_000)),
        "elapsed_seconds": control_elapsed_seconds,
        "passed": control_gpu_time_ns > 0 and control_interval_count > 0,
        "pid": control_pid,
        "trace_duration_seconds": control_toc["duration_seconds"],
    }
    if not bool(positive_control["passed"]):
        raise QualificationFailure(
            "Metal System Trace did not observe the known Metal positive control; zero encoder values are inconclusive."
        )

    phase_evidence: dict[str, dict[str, object]] = {}
    for phase, expected_names in (
        ("current", {"ffmpeg", "spatial-media-kit-tool"}),
        ("direct", {"ffmpeg", encoder_path.name}),
    ):
        phase_directory = session_directory / phase
        phase_directory.mkdir()
        result_path = phase_directory / "phase-result.json"

        def run_phase_workload(
            phase_name: str = phase,
            workload_directory: Path = phase_directory,
            workload_result_path: Path = result_path,
        ) -> dict[str, object]:
            run_phase(
                phase_name,
                workload_directory,
                workload_result_path,
                encoder_path,
                options,
            )
            return load_json_object(workload_result_path)

        phase_result, toc, intervals = profile_trace(
            phase,
            run_phase_workload,
            session_directory,
            cwd=cwd,
            trace_limit_seconds=trace_limit_seconds,
        )
        targets = target_descendants(phase_result, expected_names)
        elapsed_seconds = float(phase_result["elapsed_seconds"])
        gpu = summarize_target_gpu(
            toc,
            intervals,
            targets,
            phase_elapsed_seconds=elapsed_seconds,
        )
        trace_processes = toc["processes"]
        assert isinstance(trace_processes, list)
        service_targets = [
            {
                "name": str(process["name"]),
                "pid": int(process["pid"]),
            }
            for process in trace_processes
            if isinstance(process, dict)
            and isinstance(process.get("name"), str)
            and is_videotoolbox_service(str(process["name"]))
            and isinstance(process.get("pid"), int)
        ]
        gpu["video_toolbox_service_agx"] = summarize_target_gpu(
            toc,
            intervals,
            service_targets,
            phase_elapsed_seconds=elapsed_seconds,
        )
        gpu["video_toolbox_service_attribution"] = (
            "System-wide VideoToolbox service processes observed during the trace are reported separately because "
            "Metal System Trace does not expose a supported client-PID linkage for the dedicated media engine."
        )
        phase_result["gpu"] = gpu
        phase_evidence[phase] = phase_result

    measurement_complete = all(
        bool(phase_evidence[phase]["gpu"]["all_target_processes_observed"]) for phase in ("current", "direct")
    )
    result: dict[str, object] = {
        "acceptance": {
            "measurement_complete": measurement_complete,
            "positive_control_passed": bool(positive_control["passed"]),
        },
        "current_path": phase_evidence["current"],
        "direct_path": phase_evidence["direct"],
        "fixture": {
            "bitrate_mbps": options.bitrate_mbps,
            "disparity_pixels": options.disparity_pixels,
            "duration_seconds": options.duration_seconds,
            "eye_height": options.eye_height,
            "eye_width": options.eye_width,
            "frame_count": options.frame_count,
            "frame_rate": options.frame_rate,
        },
        "method": {
            "agx_gpu_metric": ("union of process/PID Metal GPU intervals divided by phase wall time"),
            "command": "xctrace record --template 'Metal System Trace' --all-processes",
            "media_engine_limitation": (
                "Metal System Trace measures AGX GPU work. VideoToolbox's dedicated Apple media-engine "
                "utilization is not exposed by a supported per-process API."
            ),
            "positive_control": METAL_CONTROL_NAME,
            "raw_evidence_directory": str(session_directory),
            "trace_limit_seconds": trace_limit_seconds,
        },
        "positive_control": positive_control,
        "schema_version": 2,
    }
    provenance = collect_provenance(
        encoder_path,
        cwd=cwd,
        session_name=session_name,
    )
    method = result["method"]
    assert isinstance(method, dict)
    method["evidence_manifest"] = "evidence-manifest.json"
    method["evidence_manifest_sha256_file"] = "evidence-manifest.sha256"
    result["provenance"] = provenance
    summary_path = session_directory / "measurement-summary.json"
    summary_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    manifest_path, checksum_path, _ = write_evidence_manifest(
        session_directory,
        provenance,
    )
    if (
        manifest_path.name != method["evidence_manifest"]
        or checksum_path.name != method["evidence_manifest_sha256_file"]
    ):
        raise QualificationFailure("Evidence manifest filenames do not match the canonical measurement summary.")
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Measure PID-specific AGX GPU time for current and direct MV-HEVC encoder paths."
    )
    parser.add_argument(
        "--encoder",
        type=Path,
        default=Path("build/mv-hevc-encoder/mv-hevc-encoder"),
    )
    parser.add_argument("--eye-width", type=int, default=1280)
    parser.add_argument("--eye-height", type=int, default=720)
    parser.add_argument("--frame-rate", type=int, default=30)
    parser.add_argument("--duration", type=int, default=6)
    parser.add_argument("--disparity-pixels", type=int, default=16)
    parser.add_argument("--bitrate-mbps", type=float, default=16.0)
    parser.add_argument(
        "--trace-limit-seconds",
        type=int,
        default=DEFAULT_TRACE_LIMIT_SECONDS,
    )
    parser.add_argument(
        "--positive-control-seconds",
        type=int,
        default=DEFAULT_POSITIVE_CONTROL_SECONDS,
    )
    parser.add_argument(
        "--raw-output-directory",
        type=Path,
        default=Path("build/direct-mv-hevc-gpu/raw"),
    )
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--phase", choices=("current", "direct"), help=argparse.SUPPRESS)
    parser.add_argument("--phase-directory", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--phase-result", type=Path, help=argparse.SUPPRESS)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.eye_width <= 0 or args.eye_height <= 0 or args.eye_width % 2 or args.eye_height % 2:
        parser.error("eye dimensions must be positive even integers")
    if args.frame_rate <= 0 or args.duration <= 0:
        parser.error("frame rate and duration must be positive")
    if args.disparity_pixels < 0 or args.disparity_pixels % 2:
        parser.error("disparity pixels must be a non-negative even integer")
    if args.bitrate_mbps <= 0:
        parser.error("bitrate must be positive")
    if args.trace_limit_seconds < 10 or args.trace_limit_seconds > 600:
        parser.error("trace limit must be between 10 and 600 seconds")
    if args.positive_control_seconds < 2 or args.positive_control_seconds > 30:
        parser.error("positive control duration must be between 2 and 30 seconds")

    options = FixtureOptions(
        eye_width=args.eye_width,
        eye_height=args.eye_height,
        frame_rate=args.frame_rate,
        duration_seconds=args.duration,
        disparity_pixels=args.disparity_pixels,
        bitrate_mbps=args.bitrate_mbps,
    )
    encoder_path = args.encoder.resolve()
    try:
        if args.phase:
            if args.phase_directory is None or args.phase_result is None:
                parser.error("internal phase mode requires output paths")
            run_phase(
                args.phase,
                args.phase_directory.resolve(),
                args.phase_result.resolve(),
                encoder_path,
                options,
            )
            return 0
        result = profile_gpu(
            encoder_path,
            options,
            args.raw_output_directory,
            trace_limit_seconds=args.trace_limit_seconds,
            positive_control_seconds=args.positive_control_seconds,
        )
    except QualificationFailure as error:
        parser.exit(1, f"error: {error}\n")

    text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0 if bool(result["acceptance"]["measurement_complete"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
