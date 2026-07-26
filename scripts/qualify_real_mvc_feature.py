#!/usr/bin/env python3

from __future__ import annotations

import argparse
import itertools
import json
import math
import os
import platform
import queue
import re
import shutil
import signal
import statistics
import subprocess
import threading
import time
import uuid

from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from typing import BinaryIO, Mapping, Sequence, TextIO

import psutil

from bd_to_avp.observability import (
    ObservabilityContext,
    ObservabilityEmitter,
    ObservabilityEvent,
    ObservabilityStage,
)
from bd_to_avp.process_runner import (
    CaptureOverflowPolicy,
    ProcessArtifactProbe,
    ProcessCancelled,
    ProcessPipelineRunner,
    ProcessPipelineStage,
    ProcessSpec,
)
from bd_to_avp.runtime import CancellationToken, ObservabilityStream, RunContext
from bd_to_avp.modules.video import resolve_direct_mv_hevc_artifact
from scripts.benchmark_mv_hevc_metalfx import (
    aligned_4k_bgra_texture_bytes,
    declared_metal_payload_bytes,
)
from scripts.qualify_direct_mv_hevc import DIRECT_REQUIRED_BOX_TYPES, box_types, verify_seeks
from scripts.qualify_mv_hevc_quality_match import sha256_file
from scripts.verify_apple_media import verify_apple_media_compatible
from scripts.verify_packaged_mv_hevc_routes import (
    AppBundle,
    app_tree_sha256,
    build_worker_request,
    parse_worker_events,
    read_app_bundle,
)


MAX_EVIDENCE_BYTES = 512 * 1024
DEFAULT_FRAME_RATE = "24000/1001"
DEFAULT_MAX_RSS_GROWTH_MIB = 64
DEFAULT_CANCEL_AFTER_MIB = 32
AGGREGATE_RSS_GROWTH_MULTIPLIER = 2
METALFX_INTERNAL_TEXTURE_ALLOWANCE = 3
METALFX_DURATION_TEXTURE_GROWTH_ALLOWANCE = 1
MAX_FEATURE_METAL_BYTES = 1024 * 1024 * 1024
MAX_FEATURE_PROCESS_RSS_BYTES = 2 * 1024 * 1024 * 1024
DIRECT_ARTIFACT_NO_GROWTH_SECONDS = 120
PACKAGE_BIN_RELATIVE_PATH = Path("Contents/Resources/app/bd_to_avp/bin")
TERMINAL_TOOL_EVENTS = {"tool.cancelled", "tool.completed", "tool.failed"}
THERMAL_NAMES = {0: "nominal", 1: "fair", 2: "serious", 3: "critical"}
POWERMETRICS_THERMAL_PATTERN = re.compile(
    r"^Current pressure level:\s*(Nominal|Fair|Serious|Critical)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


class FeatureQualificationFailure(RuntimeError):
    pass


@dataclass(frozen=True)
class PackagedTools:
    ffmpeg: Path
    ffprobe: Path
    edge264: Path
    encoder: Path


def packaged_tools(app: AppBundle) -> PackagedTools:
    binary_root = app.path / PACKAGE_BIN_RELATIVE_PATH
    tools = PackagedTools(
        ffmpeg=binary_root / "ffmpeg",
        ffprobe=binary_root / "ffprobe",
        edge264=binary_root / "edge264_test",
        encoder=app.helper,
    )
    for path in (tools.ffmpeg, tools.ffprobe, tools.edge264, tools.encoder):
        if not path.is_file() or not os.access(path, os.X_OK):
            raise FeatureQualificationFailure(f"Packaged qualification tool is unavailable: {path.name}")
    return tools


def thermal_state() -> int:
    try:
        from Foundation import NSProcessInfo  # type: ignore[import-untyped]

        state = int(NSProcessInfo.processInfo().thermalState())
    except (ImportError, TypeError, ValueError) as error:
        raise FeatureQualificationFailure("Could not read the macOS thermal pressure state.") from error
    if state not in THERMAL_NAMES:
        raise FeatureQualificationFailure(f"macOS reported an unknown thermal pressure state: {state}")
    return state


def powermetrics_thermal_summary(trace_text: str) -> dict[str, object]:
    thermal_section_count = trace_text.count("**** Thermal pressure ****")
    states_by_name = {name: state for state, name in THERMAL_NAMES.items()}
    observed_names = [match.lower() for match in POWERMETRICS_THERMAL_PATTERN.findall(trace_text)]
    if thermal_section_count <= 0 or len(observed_names) != thermal_section_count:
        raise FeatureQualificationFailure("powermetrics trace did not contain complete thermal pressure values.")
    states = [states_by_name[name] for name in observed_names]
    max_state = max(states)
    return {
        "all_nominal": max_state == 0,
        "counts": {name: observed_names.count(name) for name in THERMAL_NAMES.values()},
        "max_state": max_state,
        "max_state_name": THERMAL_NAMES[max_state],
        "sample_count": len(states),
    }


def require_nominal_powermetrics_thermal(trace_text: str) -> dict[str, object]:
    thermal = powermetrics_thermal_summary(trace_text)
    if not require_bool_value(thermal, "all_nominal"):
        raise FeatureQualificationFailure("powermetrics reported non-nominal thermal pressure.")
    return thermal


def percentile(values: Sequence[int], fraction: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * fraction) - 1))
    return ordered[index]


def quartile_peaks(values: Sequence[int]) -> list[int]:
    if not values:
        return [0, 0, 0, 0]
    peaks: list[int] = []
    for index in range(4):
        start = index * len(values) // 4
        end = (index + 1) * len(values) // 4
        peaks.append(max(values[start : max(start + 1, end)], default=0))
    return peaks


def process_role(tool_id: str, process_id: int, count: int) -> str:
    try:
        command = psutil.Process(process_id).cmdline()
    except (psutil.AccessDenied, psutil.NoSuchProcess):
        command = []
    if tool_id == "ffmpeg":
        if "-bsf:v" in command:
            return "source_ffmpeg"
        if "yuv4mpegpipe" in command:
            return "normalizer_ffmpeg"
    return f"{tool_id}_{count}"


def require_mapping_value(document: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = document.get(key)
    if not isinstance(value, Mapping):
        raise FeatureQualificationFailure(f"Qualification evidence omitted mapping {key!r}.")
    return value


def require_int_value(document: Mapping[str, object], key: str) -> int:
    value = document.get(key)
    if type(value) is not int:
        raise FeatureQualificationFailure(f"Qualification evidence omitted integer {key!r}.")
    return value


def require_number_value(document: Mapping[str, object], key: str) -> float:
    value = document.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FeatureQualificationFailure(f"Qualification evidence omitted number {key!r}.")
    return float(value)


def require_bool_value(document: Mapping[str, object], key: str) -> bool:
    value = document.get(key)
    if type(value) is not bool:
        raise FeatureQualificationFailure(f"Qualification evidence omitted boolean {key!r}.")
    return value


def require_int_list_value(document: Mapping[str, object], key: str, *, length: int) -> list[int]:
    value = document.get(key)
    if not isinstance(value, list) or len(value) != length or any(type(item) is not int for item in value):
        raise FeatureQualificationFailure(f"Qualification evidence omitted integer list {key!r}.")
    return [item for item in value if isinstance(item, int)]


class QualificationEventSink:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active_pids: set[int] = set()
        self._process_labels: dict[int, str] = {}
        self._tool_counts: dict[str, int] = {}
        self._artifact_samples: list[dict[str, object]] = []
        self._failure_codes: list[str] = []

    def emit(self, event: ObservabilityEvent) -> None:
        process = event.context.process
        with self._lock:
            process_id = process.pid if process is not None else None
            if isinstance(process_id, int) and event.kind == "tool.started":
                self._active_pids.add(process_id)
                tool_id = event.context.tool.id if event.context.tool is not None else "process"
                count = self._tool_counts.get(tool_id, 0) + 1
                self._tool_counts[tool_id] = count
                self._process_labels[process_id] = process_role(tool_id, process_id, count)
            if isinstance(process_id, int) and event.kind in TERMINAL_TOOL_EVENTS:
                self._active_pids.discard(process_id)
            if event.data.failure is not None:
                self._failure_codes.append(event.data.failure.code)
            artifact = event.data.artifact
            if artifact is not None:
                self._artifact_samples.append(
                    {
                        "elapsed_seconds": round((event.elapsed_ms or 0) / 1000, 3),
                        "growth_bytes_per_second": artifact.growth_bytes_per_second,
                        "role": artifact.role,
                        "size_bytes": artifact.size_bytes,
                        "state": artifact.state,
                    }
                )

    def active_pids(self) -> tuple[int, ...]:
        with self._lock:
            return tuple(self._active_pids)

    def active_processes(self) -> tuple[tuple[int, str], ...]:
        with self._lock:
            return tuple(
                (process_id, self._process_labels.get(process_id, "process")) for process_id in self._active_pids
            )

    def summary(self) -> dict[str, object]:
        with self._lock:
            samples = list(self._artifact_samples)
            failure_codes = list(self._failure_codes)
        stereo_samples = [sample for sample in samples if sample["role"] == "stereo_video_output"]
        growth_samples = [
            sample
            for sample in stereo_samples
            if isinstance(sample["growth_bytes_per_second"], int) and sample["growth_bytes_per_second"] > 0
        ]
        increasing_samples: list[dict[str, object]] = []
        prior_size = 0
        for sample in stereo_samples:
            size = sample["size_bytes"]
            if isinstance(size, int) and size > prior_size:
                increasing_samples.append(sample)
                prior_size = size
        growth_gaps = [
            require_number_value(current, "elapsed_seconds") - require_number_value(previous, "elapsed_seconds")
            for previous, current in itertools.pairwise(increasing_samples)
        ]
        return {
            "artifact_sample_count": len(stereo_samples),
            "failure_codes": sorted(set(failure_codes)),
            "increasing_artifact_sample_count": len(increasing_samples),
            "max_artifact_growth_gap_seconds": round(max(growth_gaps, default=0), 3),
            "positive_growth_sample_count": len(growth_samples),
        }


class ResourceSampler:
    def __init__(self, event_sink: QualificationEventSink, *, interval_seconds: float = 0.5) -> None:
        self._event_sink = event_sink
        self._interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._rss_samples: list[int] = []
        self._thermal_samples: list[int] = []
        self._process_samples: list[tuple[float, dict[str, int]]] = []
        self._error: BaseException | None = None
        self._started_at = time.monotonic()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> dict[str, object]:
        self._stop.set()
        self._thread.join(timeout=max(5.0, self._interval_seconds * 4))
        if self._thread.is_alive():
            raise FeatureQualificationFailure("Resource sampler did not stop.")
        if self._error is not None:
            raise FeatureQualificationFailure("Resource sampler failed during qualification.") from self._error
        rss_samples = [sample for sample in self._rss_samples if sample > 0]
        thermal_samples = list(self._thermal_samples)
        sample_count = len(rss_samples)
        total_quartile_peaks = quartile_peaks(rss_samples)
        duration_seconds = time.monotonic() - self._started_at
        expected_samples = max(1, int(duration_seconds / self._interval_seconds) - 1)
        process_labels = sorted({label for _elapsed, sample in self._process_samples for label in sample})
        process_summaries: dict[str, object] = {}
        for label in process_labels:
            samples = [sample[label] for _elapsed, sample in self._process_samples if sample.get(label, 0) > 0]
            label_quartile_peaks = quartile_peaks(samples)
            process_summaries[label] = {
                "final_rss_bytes": samples[-1] if samples else 0,
                "first_quarter_peak_rss_bytes": label_quartile_peaks[0],
                "last_quarter_peak_rss_bytes": label_quartile_peaks[-1],
                "median_rss_bytes": int(statistics.median(samples)) if samples else 0,
                "peak_rss_bytes": max(samples, default=0),
                "quartile_peak_rss_bytes": label_quartile_peaks,
                "sample_count": len(samples),
            }
        timeline_limit = 64
        if len(self._process_samples) <= timeline_limit:
            timeline_samples = self._process_samples
        else:
            timeline_indices = {
                round(index * (len(self._process_samples) - 1) / (timeline_limit - 1))
                for index in range(timeline_limit)
            }
            timeline_samples = [
                sample for index, sample in enumerate(self._process_samples) if index in timeline_indices
            ]
        return {
            "coverage_fraction": round(min(1.0, sample_count / expected_samples), 6),
            "duration_seconds": round(duration_seconds, 3),
            "final_rss_bytes": rss_samples[-1] if rss_samples else 0,
            "first_quarter_peak_rss_bytes": total_quartile_peaks[0],
            "last_quarter_peak_rss_bytes": total_quartile_peaks[-1],
            "median_rss_bytes": int(statistics.median(rss_samples)) if rss_samples else 0,
            "peak_rss_bytes": max(rss_samples, default=0),
            "p95_rss_bytes": percentile(rss_samples, 0.95),
            "processes": process_summaries,
            "quartile_peak_rss_bytes": total_quartile_peaks,
            "sample_count": sample_count,
            "thermal": {
                "counts": {
                    name: sum(sample == state for sample in thermal_samples) for state, name in THERMAL_NAMES.items()
                },
                "max_state": max(thermal_samples, default=0),
                "max_state_name": THERMAL_NAMES[max(thermal_samples, default=0)],
                "sample_count": len(thermal_samples),
            },
            "timeline": [
                {
                    "elapsed_seconds": round(elapsed, 3),
                    "process_rss_bytes": process_rss,
                    "total_rss_bytes": sum(process_rss.values()),
                }
                for elapsed, process_rss in timeline_samples
            ],
        }

    def _run(self) -> None:
        try:
            while not self._stop.wait(self._interval_seconds):
                process_rss = {
                    label: process_id_tree_rss(process_id) for process_id, label in self._event_sink.active_processes()
                }
                total_rss = sum(process_rss.values())
                self._rss_samples.append(total_rss)
                self._process_samples.append((time.monotonic() - self._started_at, process_rss))
                self._thermal_samples.append(thermal_state())
        except Exception as error:
            self._error = error
            self._stop.set()


class PowerTrace:
    def __init__(self, output_path: Path) -> None:
        self.output_path = output_path
        self.process: subprocess.Popen[bytes] | None = None
        self.output_file: BinaryIO | None = None

    def start(self) -> None:
        if shutil.which("powermetrics") is None or shutil.which("sudo") is None:
            raise FeatureQualificationFailure("powermetrics and sudo are required for feature-length thermal evidence.")
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_path.write_bytes(b"")
        self.output_path.chmod(0o600)
        self.output_file = self.output_path.open("wb")
        self.process = subprocess.Popen(
            [
                "sudo",
                "-n",
                "powermetrics",
                "--samplers",
                "thermal,gpu_power",
                "--sample-rate",
                "5000",
                "--show-process-gpu",
            ],
            stdout=self.output_file,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        time.sleep(0.5)
        if self.process.poll() is not None:
            self.output_file.close()
            raise FeatureQualificationFailure("powermetrics could not start without an interactive authorization.")

    def stop(self) -> dict[str, object]:
        if self.process is None:
            raise FeatureQualificationFailure("powermetrics trace was not started.")
        if self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGTERM)
        try:
            self.process.wait(timeout=30)
        except subprocess.TimeoutExpired as error:
            os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait(timeout=10)
            raise FeatureQualificationFailure("powermetrics did not stop cleanly.") from error
        finally:
            if self.output_file is not None and not self.output_file.closed:
                self.output_file.close()
        if self.process.returncode not in {0, -signal.SIGTERM}:
            raise FeatureQualificationFailure(
                f"powermetrics exited unexpectedly with status {self.process.returncode}."
            )
        if not self.output_path.is_file() or self.output_path.stat().st_size == 0:
            raise FeatureQualificationFailure("powermetrics did not produce a thermal trace.")
        self.output_path.chmod(0o600)
        trace_text = self.output_path.read_text(encoding="utf-8", errors="replace")
        sample_count = trace_text.count("*** Sampled system activity")
        thermal = require_nominal_powermetrics_thermal(trace_text)
        if sample_count <= 0:
            raise FeatureQualificationFailure("powermetrics trace did not contain thermal samples.")
        return {
            "sample_count": sample_count,
            "sha256": sha256_file(self.output_path),
            "size_bytes": self.output_path.stat().st_size,
            "thermal": thermal,
        }


def producer_command(ffmpeg: Path, source: Path) -> list[str | Path]:
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        source,
        "-map",
        "0:v:0",
        "-c:v",
        "copy",
        "-bsf:v",
        "h264_mp4toannexb",
        "-f",
        "h264",
        "-",
    ]


def normalizer_command(ffmpeg: Path, *, frame_rate: str) -> list[str | Path]:
    filter_complex = (
        "[0:v]split=2[left_source][right_source];"
        "[left_source]crop=1920:1080:0:0[left];"
        "[right_source]crop=1920:1080:1920:0[right];"
        "[left][right]hstack=inputs=2,format=yuv420p[stereo]"
    )
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "yuv4mpegpipe",
        "-r",
        frame_rate,
        "-i",
        "-",
        "-filter_complex",
        filter_complex,
        "-map",
        "[stereo]",
        "-f",
        "yuv4mpegpipe",
        "-pix_fmt",
        "yuv420p",
        "-r",
        frame_rate,
        "-",
    ]


def encoder_command(encoder: Path, output: Path) -> list[str | Path]:
    return [
        encoder,
        "--output",
        output,
        "--quality",
        "0.6",
        "--upscale-mode",
        "metalfx",
        "--fov",
        "90",
        "--disparity-adjustment",
        "0",
        "--overwrite",
    ]


def safe_encoder_summary(summary: Mapping[str, object]) -> dict[str, object]:
    keys = (
        "frame_count",
        "metalfx_device_final_allocated_size_bytes",
        "metalfx_device_initial_allocated_size_bytes",
        "metalfx_device_peak_allocated_size_bytes",
        "metalfx_device_peak_delta_bytes",
        "upscale_converted_input_buffer_limit_per_eye",
        "upscale_mode",
        "upscale_output_buffer_limit_per_eye",
        "upscale_source_buffer_limit",
    )
    return {key: summary[key] for key in keys if key in summary}


def parse_encoder_summary(stdout: str) -> Mapping[str, object]:
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        raise FeatureQualificationFailure("MV-HEVC encoder did not emit a summary.")
    try:
        summary = json.loads(lines[-1])
    except json.JSONDecodeError as error:
        raise FeatureQualificationFailure("MV-HEVC encoder emitted an invalid summary.") from error
    if not isinstance(summary, Mapping):
        raise FeatureQualificationFailure("MV-HEVC encoder summary was not a JSON object.")
    if summary.get("upscale_mode") != "metalfx" or not isinstance(summary.get("frame_count"), int):
        raise FeatureQualificationFailure("MV-HEVC encoder summary omitted the MetalFX frame contract.")
    for key in (
        "metalfx_device_peak_allocated_size_bytes",
        "metalfx_device_peak_delta_bytes",
    ):
        if not isinstance(summary.get(key), int) or int(summary[key]) < 0:
            raise FeatureQualificationFailure(f"MV-HEVC encoder summary omitted valid {key} telemetry.")
    return summary


def probe_direct_artifact(tools: PackagedTools, path: Path) -> dict[str, object]:
    verify_apple_media_compatible(path)
    observed_boxes = box_types(path)
    missing_boxes = sorted(DIRECT_REQUIRED_BOX_TYPES - observed_boxes)
    if missing_boxes:
        raise FeatureQualificationFailure("Direct artifact is missing spatial boxes: " + ", ".join(missing_boxes))
    completed = subprocess.run(
        [
            str(tools.ffprobe),
            "-v",
            "error",
            "-show_entries",
            "format=duration,size:stream=codec_name,codec_type,width,height",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    probe = json.loads(completed.stdout)
    duration_seconds = float(probe["format"]["duration"])
    verify_seeks(str(tools.ffmpeg), path, max(1, math.ceil(duration_seconds)))
    video_streams = [stream for stream in probe.get("streams", []) if stream.get("codec_type") == "video"]
    if len(video_streams) != 1 or (video_streams[0].get("width"), video_streams[0].get("height")) != (3840, 2160):
        raise FeatureQualificationFailure("Direct feature artifact did not preserve 3840x2160 per-eye output.")
    return {
        "duration_seconds": duration_seconds,
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
        "video_dimensions": {"height": 2160, "width": 3840},
    }


def run_direct_workload(
    tools: PackagedTools,
    source: Path,
    output: Path,
    *,
    frame_rate: str,
    trace_path: Path | None = None,
    timeout_seconds: float,
) -> dict[str, object]:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.unlink(missing_ok=True)
    for partial in output.parent.glob(f".{output.name}.partial-*"):
        partial.unlink(missing_ok=True)

    context = ObservabilityContext(stage=ObservabilityStage("feature_length_direct"))
    sink = QualificationEventSink()
    run_context = RunContext(ObservabilityStream(ObservabilityEmitter.WORKER, sink), CancellationToken())
    environment = os.environ.copy()
    stages = (
        ProcessPipelineStage(
            ProcessSpec(
                argv=tuple(producer_command(tools.ffmpeg, source)),
                tool_id="ffmpeg",
                display_name="Extract real MVC stream",
                env=environment,
                event_context=context,
                capture_overflow=CaptureOverflowPolicy.TRUNCATE,
            )
        ),
        ProcessPipelineStage(
            ProcessSpec(
                argv=(tools.edge264, Path("-"), "-Omk"),
                tool_id="edge264",
                display_name="Split real MVC stream",
                env=environment,
                event_context=context,
                capture_overflow=CaptureOverflowPolicy.TRUNCATE,
            )
        ),
        ProcessPipelineStage(
            ProcessSpec(
                argv=tuple(normalizer_command(tools.ffmpeg, frame_rate=frame_rate)),
                tool_id="ffmpeg",
                display_name="Normalize real stereo frames",
                env=environment,
                event_context=context,
                capture_overflow=CaptureOverflowPolicy.TRUNCATE,
            )
        ),
        ProcessPipelineStage(
            ProcessSpec(
                argv=tuple(encoder_command(tools.encoder, output)),
                tool_id="mv_hevc_encoder",
                display_name="Encode direct 4K MetalFX MV-HEVC",
                env=environment,
                event_context=context,
                artifacts=(
                    ProcessArtifactProbe(
                        "stereo_video_output",
                        resolver=lambda: resolve_direct_mv_hevc_artifact(output),
                    ),
                ),
                artifact_no_growth_timeout_seconds=DIRECT_ARTIFACT_NO_GROWTH_SECONDS,
                capture_overflow=CaptureOverflowPolicy.TRUNCATE,
            )
        ),
    )
    power_trace = PowerTrace(trace_path) if trace_path is not None else None
    sampler = ResourceSampler(sink)
    timed_out = threading.Event()

    def cancel_after_timeout() -> None:
        timed_out.set()
        run_context.cancellation.cancel()

    started_at = time.monotonic()
    timeout_timer: threading.Timer | None = None
    power_trace_started = False
    sampler_started = False
    resources: dict[str, object] | None = None
    trace_evidence: dict[str, object] | None = None
    try:
        if power_trace is not None:
            power_trace.start()
            power_trace_started = True
        sampler.start()
        sampler_started = True
        timeout_timer = threading.Timer(timeout_seconds, cancel_after_timeout)
        timeout_timer.daemon = True
        timeout_timer.start()
        try:
            result = ProcessPipelineRunner(exit_grace_seconds=5).run(stages, run_context=run_context)
        except ProcessCancelled as error:
            if timed_out.is_set():
                raise FeatureQualificationFailure("Direct feature workload exceeded its configured timeout.") from error
            raise
    finally:
        if timeout_timer is not None:
            timeout_timer.cancel()
        try:
            if sampler_started:
                resources = sampler.stop()
        finally:
            if power_trace is not None and power_trace_started:
                trace_evidence = power_trace.stop()
    if resources is None:
        raise FeatureQualificationFailure("Direct feature workload did not record resource evidence.")
    elapsed_seconds = time.monotonic() - started_at
    final_result = result.stages[-1].result
    if final_result is None:
        raise FeatureQualificationFailure("Direct pipeline did not return an encoder result.")
    summary = parse_encoder_summary(final_result.stdout.text())
    artifact = probe_direct_artifact(tools, output)
    observability = sink.summary()
    thermal = require_mapping_value(resources, "thermal")
    artifact_progress = require_int_value(observability, "increasing_artifact_sample_count") > 0
    no_tool_failures = not observability["failure_codes"]
    acceptable_thermal = require_int_value(thermal, "max_state") < 2
    resource_samples = require_int_value(resources, "sample_count") > 0
    thermal_samples = require_int_value(thermal, "sample_count") > 0
    resource_coverage = require_number_value(resources, "coverage_fraction") >= 0.8
    passed = (
        no_tool_failures
        and artifact_progress
        and require_number_value(observability, "max_artifact_growth_gap_seconds") <= DIRECT_ARTIFACT_NO_GROWTH_SECONDS
        and acceptable_thermal
        and resource_samples
        and thermal_samples
        and resource_coverage
    )
    evidence: dict[str, object] = {
        "acceptance": {
            "artifact_progress": artifact_progress,
            "no_tool_failures": no_tool_failures,
            "no_unacceptable_thermal_pressure": acceptable_thermal,
            "passed": passed,
            "resource_samples_recorded": resource_samples,
            "resource_sampling_coverage": resource_coverage,
            "thermal_samples_recorded": thermal_samples,
        },
        "artifact": artifact,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "encoder": safe_encoder_summary(summary),
        "observability": observability,
        "resources": resources,
    }
    if trace_evidence is not None:
        evidence["powermetrics_trace"] = trace_evidence
    return evidence


def resource_boundedness(
    short_run: Mapping[str, object],
    feature_run: Mapping[str, object],
    *,
    max_rss_growth_bytes: int,
) -> dict[str, object]:
    short_resources = require_mapping_value(short_run, "resources")
    feature_resources = require_mapping_value(feature_run, "resources")
    short_encoder = require_mapping_value(short_run, "encoder")
    feature_encoder = require_mapping_value(feature_run, "encoder")
    short_peak_rss = require_int_value(short_resources, "peak_rss_bytes")
    feature_peak_rss = require_int_value(feature_resources, "peak_rss_bytes")
    short_duration = require_number_value(short_resources, "duration_seconds")
    feature_duration = require_number_value(feature_resources, "duration_seconds")
    feature_quartile_peaks = require_int_list_value(feature_resources, "quartile_peak_rss_bytes", length=4)
    feature_processes = require_mapping_value(feature_resources, "processes")
    rss_growth = max(0, feature_peak_rss - short_peak_rss)
    feature_plateau_applicable = feature_duration >= short_duration * 4
    process_plateau_growth: dict[str, int] = {}
    for process_name, process_value in feature_processes.items():
        if not isinstance(process_name, str) or not isinstance(process_value, Mapping):
            raise FeatureQualificationFailure("Feature process evidence reported an invalid process summary.")
        process_quartiles = require_int_list_value(process_value, "quartile_peak_rss_bytes", length=4)
        process_plateau_growth[process_name] = max(0, process_quartiles[3] - process_quartiles[2])
    feature_plateau_growth = max(process_plateau_growth.values(), default=0)
    feature_total_plateau_growth = max(0, feature_quartile_peaks[3] - feature_quartile_peaks[2])
    feature_aggregate_plateau_allowance = max_rss_growth_bytes * AGGREGATE_RSS_GROWTH_MULTIPLIER
    feature_process_plateau_passed = not feature_plateau_applicable or (
        bool(process_plateau_growth)
        and all(growth <= max_rss_growth_bytes for growth in process_plateau_growth.values())
    )
    feature_total_plateau_passed = (
        not feature_plateau_applicable or feature_total_plateau_growth <= feature_aggregate_plateau_allowance
    )
    feature_plateau_passed = feature_process_plateau_passed and feature_total_plateau_passed
    feature_process_peak_passed = feature_peak_rss <= MAX_FEATURE_PROCESS_RSS_BYTES
    short_metal_peak = require_int_value(short_encoder, "metalfx_device_peak_delta_bytes")
    feature_metal_peak = require_int_value(feature_encoder, "metalfx_device_peak_delta_bytes")
    feature_metal_final = require_int_value(feature_encoder, "metalfx_device_final_allocated_size_bytes")
    metal_growth = max(0, feature_metal_peak - short_metal_peak)
    metal_growth_allowance = aligned_4k_bgra_texture_bytes(METALFX_DURATION_TEXTURE_GROWTH_ALLOWANCE)
    metal_growth_passed = metal_growth <= metal_growth_allowance
    legacy_modeled_metal_peak_ceiling = declared_metal_payload_bytes("metalfx") + aligned_4k_bgra_texture_bytes(
        METALFX_INTERNAL_TEXTURE_ALLOWANCE
    )
    feature_metal_peak_passed = (
        feature_metal_peak <= MAX_FEATURE_METAL_BYTES and feature_metal_final <= MAX_FEATURE_METAL_BYTES
    )
    passed = (
        feature_plateau_passed and feature_process_peak_passed and feature_metal_peak_passed and metal_growth_passed
    )
    return {
        "feature_metal_final_allocated_size_bytes": feature_metal_final,
        "feature_peak_rss_bytes": feature_peak_rss,
        "feature_plateau_applicable": feature_plateau_applicable,
        "feature_aggregate_plateau_allowance_bytes": feature_aggregate_plateau_allowance,
        "feature_plateau_growth_bytes": feature_plateau_growth,
        "feature_plateau_passed": feature_plateau_passed,
        "feature_process_plateau_passed": feature_process_plateau_passed,
        "feature_process_plateau_growth_bytes": process_plateau_growth,
        "feature_metal_peak_delta_bytes": feature_metal_peak,
        "feature_metal_duration_growth_passed": metal_growth_passed,
        "feature_metal_peak_passed": feature_metal_peak_passed,
        "feature_process_peak_passed": feature_process_peak_passed,
        "feature_quartile_peak_rss_bytes": feature_quartile_peaks,
        "feature_total_plateau_growth_bytes": feature_total_plateau_growth,
        "feature_total_plateau_passed": feature_total_plateau_passed,
        "legacy_modeled_metal_peak_ceiling_bytes": legacy_modeled_metal_peak_ceiling,
        "max_feature_metal_bytes": MAX_FEATURE_METAL_BYTES,
        "max_feature_process_rss_bytes": MAX_FEATURE_PROCESS_RSS_BYTES,
        "max_rss_growth_bytes": max_rss_growth_bytes,
        "metal_duration_growth_allowance_bytes": metal_growth_allowance,
        "metal_duration_growth_bytes": metal_growth,
        "passed": passed,
        "rss_duration_growth_bytes": rss_growth,
        "short_peak_rss_bytes": short_peak_rss,
        "short_metal_peak_delta_bytes": short_metal_peak,
    }


def process_tree_rss(process: psutil.Process) -> int:
    total = 0
    try:
        total += process.memory_info().rss
        descendants = process.children(recursive=True)
    except (psutil.AccessDenied, psutil.NoSuchProcess):
        return total
    for descendant in descendants:
        try:
            total += descendant.memory_info().rss
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
    return total


def process_id_tree_rss(process_id: int) -> int:
    try:
        process = psutil.Process(process_id)
    except psutil.NoSuchProcess:
        return 0
    return process_tree_rss(process)


def read_worker_lines(stream: TextIO, stream_name: str, output: queue.Queue[tuple[str, str | None]]) -> None:
    try:
        for line in stream:
            output.put((stream_name, line))
    finally:
        output.put((stream_name, None))


def _artifact_from_worker_event(event: Mapping[str, object]) -> Mapping[str, object] | None:
    if event.get("type") != "observability":
        return None
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        return None
    observability = payload.get("event")
    if not isinstance(observability, Mapping) or observability.get("kind") != "tool.artifact":
        return None
    data = observability.get("data")
    if not isinstance(data, Mapping):
        return None
    artifact = data.get("artifact")
    return artifact if isinstance(artifact, Mapping) else None


def _process_id_from_worker_event(event: Mapping[str, object]) -> int | None:
    if event.get("type") != "observability":
        return None
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        return None
    observability = payload.get("event")
    if not isinstance(observability, Mapping):
        return None
    context = observability.get("context")
    if not isinstance(context, Mapping):
        return None
    process = context.get("process")
    if not isinstance(process, Mapping):
        return None
    process_id = process.get("pid")
    return process_id if isinstance(process_id, int) and process_id > 0 else None


def record_process_identity(process: psutil.Process, identities: dict[int, float]) -> None:
    try:
        identities.setdefault(process.pid, process.create_time())
    except (psutil.AccessDenied, psutil.NoSuchProcess):
        return


def process_identity_is_running(process_id: int, create_time: float) -> bool:
    try:
        process = psutil.Process(process_id)
        return (
            process.create_time() == create_time and process.is_running() and process.status() != psutil.STATUS_ZOMBIE
        )
    except (psutil.AccessDenied, psutil.NoSuchProcess):
        return False


def remove_private_work_directory(work_directory: Path) -> bool:
    try:
        shutil.rmtree(work_directory)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return not work_directory.exists()


def process_group_is_running(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def terminate_packaged_worker(process: subprocess.Popen[str], process_group_id: int | None) -> None:
    if process_group_id is not None:
        with suppress(ProcessLookupError):
            os.killpg(process_group_id, signal.SIGTERM)
    elif process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        if process_group_id is not None:
            with suppress(ProcessLookupError):
                os.killpg(process_group_id, signal.SIGKILL)
        elif process.poll() is None:
            process.kill()
        process.wait(timeout=10)
    if process_group_id is None:
        return
    deadline = time.monotonic() + 5
    while process_group_is_running(process_group_id) and time.monotonic() < deadline:
        time.sleep(0.1)
    if process_group_is_running(process_group_id):
        try:
            os.killpg(process_group_id, signal.SIGKILL)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + 5
        while process_group_is_running(process_group_id) and time.monotonic() < deadline:
            time.sleep(0.1)
    if process_group_is_running(process_group_id):
        raise FeatureQualificationFailure("Packaged worker process group survived forced termination.")


def run_packaged_cancellation(
    app: AppBundle,
    source: Path,
    work_directory: Path,
    *,
    cancel_after_bytes: int,
    timeout_seconds: float,
) -> dict[str, object]:
    destination = work_directory / "destination"
    home = work_directory / "home"
    if work_directory.exists():
        raise FeatureQualificationFailure("Packaged cancellation work directory must be newly created.")
    destination.mkdir(parents=True, mode=0o700)
    home.mkdir(parents=True, mode=0o700)
    work_directory.chmod(0o700)
    sentinel_path = destination / f"{source.stem}_AVP.mov"
    sentinel_bytes = b"bd-to-avp-overwrite-preservation-v1\n"
    sentinel_path.write_bytes(sentinel_bytes)
    sentinel_sha256 = sha256_file(sentinel_path)

    job_id = str(uuid.uuid4())
    request = build_worker_request(
        "convert_source",
        source,
        destination,
        job_id=job_id,
        upscale_enabled=True,
    )
    encoding = request.get("encoding")
    if not isinstance(encoding, dict):
        raise FeatureQualificationFailure("Packaged cancellation request omitted encoding settings.")
    encoding["subtitles"] = {"mode": "off", "preferred_language": None}
    environment = os.environ.copy()
    environment["HOME"] = str(home)
    process = subprocess.Popen(
        [str(app.worker)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=environment,
    )
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    process.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
    process.stdin.close()

    lines: queue.Queue[tuple[str, str | None]] = queue.Queue()
    readers = [
        threading.Thread(target=read_worker_lines, args=(process.stdout, "stdout", lines), daemon=True),
        threading.Thread(target=read_worker_lines, args=(process.stderr, "stderr", lines), daemon=True),
    ]
    for reader in readers:
        reader.start()

    events: list[Mapping[str, object]] = []
    stderr_bytes = 0
    worker_process = psutil.Process(process.pid)
    observed_processes: dict[int, float] = {}
    record_process_identity(worker_process, observed_processes)
    peak_rss_bytes = 0
    thermal_samples: list[int] = []
    heartbeat_times: list[float] = []
    stages: list[str] = []
    process_group_id: int | None = None
    cancellation_sent_at: float | None = None
    trigger_size_bytes = 0
    streams_closed: set[str] = set()
    started_at = time.monotonic()

    try:
        while process.poll() is None or streams_closed != {"stdout", "stderr"}:
            elapsed = time.monotonic() - started_at
            if elapsed > timeout_seconds:
                raise FeatureQualificationFailure("Packaged cancellation workload timed out.")
            try:
                stream_name, line = lines.get(timeout=0.25)
            except queue.Empty:
                stream_name, line = "", ""
            if line is None:
                streams_closed.add(stream_name)
            elif stream_name == "stderr":
                stderr_bytes += len(line.encode("utf-8", errors="replace"))
            elif stream_name == "stdout" and line.strip():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as error:
                    raise FeatureQualificationFailure(
                        "Packaged worker emitted invalid JSON during cancellation."
                    ) from error
                if not isinstance(event, Mapping):
                    raise FeatureQualificationFailure("Packaged worker cancellation event was not an object.")
                events.append(event)
                payload = event.get("payload")
                if event.get("type") == "worker.ready" and isinstance(payload, Mapping):
                    ready_group = payload.get("process_group_id")
                    if isinstance(ready_group, int) and ready_group > 0:
                        try:
                            launched_group = os.getpgid(process.pid)
                        except OSError as error:
                            raise FeatureQualificationFailure(
                                "Worker exited before its process group could be validated."
                            ) from error
                        if ready_group != process.pid or ready_group != launched_group:
                            raise FeatureQualificationFailure(
                                "Worker-reported process group does not belong to the launched worker."
                            )
                        process_group_id = ready_group
                event_process_id = _process_id_from_worker_event(event)
                if event_process_id is not None:
                    with suppress(psutil.NoSuchProcess):
                        record_process_identity(psutil.Process(event_process_id), observed_processes)
                if event.get("type") == "heartbeat":
                    heartbeat_times.append(elapsed)
                if event.get("type") == "stage.started" and isinstance(payload, Mapping):
                    stage = payload.get("stage")
                    if isinstance(stage, str):
                        stages.append(stage)
                artifact = _artifact_from_worker_event(event)
                if artifact is not None:
                    role = artifact.get("role")
                    size = artifact.get("size_bytes")
                    if role == "stereo_video_output" and isinstance(size, int):
                        trigger_size_bytes = max(trigger_size_bytes, size)
                        if size >= cancel_after_bytes and cancellation_sent_at is None:
                            if process_group_id is None:
                                raise FeatureQualificationFailure(
                                    "Worker did not report its process group before cancellation."
                                )
                            os.killpg(process_group_id, signal.SIGTERM)
                            cancellation_sent_at = time.monotonic()
            with suppress(psutil.AccessDenied, psutil.NoSuchProcess):
                for descendant in worker_process.children(recursive=True):
                    record_process_identity(descendant, observed_processes)
            peak_rss_bytes = max(peak_rss_bytes, process_tree_rss(worker_process))
            thermal_samples.append(thermal_state())
            if (
                cancellation_sent_at is not None
                and time.monotonic() - cancellation_sent_at > 30
                and process.poll() is None
            ):
                raise FeatureQualificationFailure("Packaged worker did not exit within 30 seconds of cancellation.")
    except (Exception, KeyboardInterrupt) as error:
        termination_error: BaseException | None = None
        try:
            terminate_packaged_worker(process, process_group_id)
        except (Exception, KeyboardInterrupt) as cleanup_error:
            termination_error = cleanup_error
        cleanup_complete = remove_private_work_directory(work_directory)
        if termination_error is not None or not cleanup_complete:
            raise FeatureQualificationFailure(
                "Packaged cancellation failed to terminate descendants or remove its private work directory."
            ) from (termination_error or error)
        raise
    finally:
        for reader in readers:
            reader.join(timeout=5)
        for stream in (process.stdout, process.stderr):
            if not stream.closed:
                stream.close()

    try:
        if cancellation_sent_at is None:
            raise FeatureQualificationFailure("Packaged cancellation workload never produced the target artifact size.")
        parsed_events = parse_worker_events("\n".join(json.dumps(event) for event in events), job_id=job_id)
        terminal = parsed_events[-1]
        heartbeat_gaps = [current - previous for previous, current in itertools.pairwise(heartbeat_times)]
        partial_files = [
            path
            for path in destination.rglob("*")
            if path.is_file() and (".partial-" in path.name or path.name.endswith((".part", ".part.mov", ".part.mkv")))
        ]
        sentinel_preserved = sentinel_path.is_file() and sha256_file(sentinel_path) == sentinel_sha256
        descendants_deadline = time.monotonic() + 5
        live_processes = [
            process_id
            for process_id, create_time in observed_processes.items()
            if process_identity_is_running(process_id, create_time)
        ]
        while live_processes and time.monotonic() < descendants_deadline:
            time.sleep(0.1)
            live_processes = [
                process_id
                for process_id, create_time in observed_processes.items()
                if process_identity_is_running(process_id, create_time)
            ]
        if process_group_id is None:
            process_group_gone = False
        else:
            try:
                os.killpg(process_group_id, 0)
            except ProcessLookupError:
                process_group_gone = True
            except PermissionError:
                process_group_gone = False
            else:
                process_group_gone = False
    except (Exception, KeyboardInterrupt) as error:
        if not remove_private_work_directory(work_directory):
            raise FeatureQualificationFailure(
                "Packaged cancellation could not remove its private work directory after validation failed."
            ) from error
        raise
    cleanup_complete = remove_private_work_directory(work_directory)
    passed = (
        process.returncode == 130
        and terminal.get("type") == "job.cancelled"
        and not any(event.get("type") == "job.completed" for event in parsed_events)
        and "create_left_right_files" in stages
        and sentinel_preserved
        and not partial_files
        and not live_processes
        and process_group_gone
        and cleanup_complete
        and max(thermal_samples, default=0) < 2
    )
    evidence = {
        "acceptance": {
            "entered_direct_encode_stage": "create_left_right_files" in stages,
            "no_completed_event": not any(event.get("type") == "job.completed" for event in parsed_events),
            "no_partial_files": not partial_files,
            "no_surviving_processes": not live_processes and process_group_gone,
            "overwrite_sentinel_preserved": sentinel_preserved,
            "passed": passed,
            "private_work_directory_removed": cleanup_complete,
            "terminal_cancelled": terminal.get("type") == "job.cancelled" and process.returncode == 130,
            "thermal_pressure_acceptable": max(thermal_samples, default=0) < 2,
        },
        "cancel_after_bytes": cancel_after_bytes,
        "elapsed_seconds": round(time.monotonic() - started_at, 3),
        "event_count": len(parsed_events),
        "heartbeat": {
            "count": len(heartbeat_times),
            "max_gap_seconds": round(max(heartbeat_gaps, default=0), 3),
        },
        "observed_artifact_bytes_before_cancel": trigger_size_bytes,
        "peak_process_tree_rss_bytes": peak_rss_bytes,
        "processes_observed": len(observed_processes),
        "stages": stages,
        "stderr_bytes_drained": stderr_bytes,
        "thermal": {
            "max_state": max(thermal_samples, default=0),
            "max_state_name": THERMAL_NAMES[max(thermal_samples, default=0)],
            "sample_count": len(thermal_samples),
        },
    }
    return evidence


def capability_evidence(tools: PackagedTools) -> dict[str, object]:
    completed = subprocess.run(
        [str(tools.encoder), "--capability-probe"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    capability = json.loads(completed.stdout)
    required = (
        "metalfx_2x_mv_hevc_supported",
        "metalfx_spatial_scaling_supported",
        "stereo_mv_hevc_encode_supported",
    )
    if any(capability.get(key) is not True for key in required):
        raise FeatureQualificationFailure("Packaged helper does not support the required direct MetalFX route.")
    return {key: capability[key] for key in sorted(capability) if isinstance(capability[key], (bool, int, str))}


def source_video_info(ffprobe: Path, source: Path) -> dict[str, object]:
    completed = subprocess.run(
        [
            str(ffprobe),
            "-v",
            "error",
            "-count_packets",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=avg_frame_rate,nb_read_packets:format=duration",
            "-of",
            "json",
            str(source),
        ],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    probe = json.loads(completed.stdout)
    streams = probe.get("streams")
    format_data = probe.get("format")
    if not isinstance(streams, list) or len(streams) != 1 or not isinstance(streams[0], Mapping):
        raise FeatureQualificationFailure("Feature source probe did not report one video stream.")
    if not isinstance(format_data, Mapping):
        raise FeatureQualificationFailure("Feature source probe did not report format timing.")
    frame_rate = streams[0].get("avg_frame_rate")
    packet_count = streams[0].get("nb_read_packets")
    duration = format_data.get("duration")
    if not isinstance(frame_rate, str) or not isinstance(packet_count, str) or duration is None:
        raise FeatureQualificationFailure("Feature source probe omitted frame count or frame rate.")
    try:
        parsed_frame_rate = Fraction(frame_rate)
        parsed_packet_count = int(packet_count)
        parsed_duration = float(duration)
    except (ValueError, ZeroDivisionError) as error:
        raise FeatureQualificationFailure("Feature source probe reported invalid timing values.") from error
    if parsed_frame_rate <= 0 or parsed_packet_count <= 0 or parsed_duration <= 0:
        raise FeatureQualificationFailure("Feature source probe reported non-positive timing values.")
    return {
        "container_duration_seconds": parsed_duration,
        "frame_rate": frame_rate,
        "packet_count": parsed_packet_count,
        "video_duration_seconds": parsed_packet_count / float(parsed_frame_rate),
    }


def validate_qualification_paths(
    app: AppBundle,
    source: Path,
    segment: Path,
    output: Path,
    work_directory: Path,
    *,
    requested_work_directory: Path,
) -> None:
    if requested_work_directory.is_symlink():
        raise FeatureQualificationFailure("Feature work directory must not be a symlink.")
    protected_paths = {source, segment, app.path, app.worker, app.helper}
    if output in protected_paths:
        raise FeatureQualificationFailure("Feature evidence output must be distinct from media and package inputs.")
    if work_directory in protected_paths or work_directory in {Path("/"), Path.home().resolve()}:
        raise FeatureQualificationFailure("Feature work directory is not a safe dedicated destination.")
    if any(path.is_relative_to(work_directory) for path in protected_paths):
        raise FeatureQualificationFailure("Feature work directory must not contain media or package inputs.")
    if output.is_relative_to(work_directory):
        raise FeatureQualificationFailure("Feature evidence output must remain outside the transient work directory.")
    if output.is_relative_to(app.path):
        raise FeatureQualificationFailure("Feature evidence output must remain outside the packaged app.")
    if work_directory.exists() and any(work_directory.iterdir()):
        raise FeatureQualificationFailure("Feature work directory must be new or empty.")


def remove_direct_workload_artifacts(*output_paths: Path) -> None:
    for output_path in output_paths:
        output_path.unlink(missing_ok=True)
        for partial in output_path.parent.glob(f".{output_path.name}.partial-*"):
            partial.unlink(missing_ok=True)


def qualify(args: argparse.Namespace) -> dict[str, object]:
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise FeatureQualificationFailure("Real-MVC MetalFX qualification requires macOS arm64.")
    if args.max_rss_growth_mib < 0 or args.cancel_after_mib <= 0 or args.timeout_hours <= 0:
        raise FeatureQualificationFailure("Resource, cancellation, and timeout settings must be positive.")
    app = read_app_bundle(args.app)
    tools = packaged_tools(app)
    source = args.source.expanduser().resolve()
    segment = args.segment.expanduser().resolve()
    output = args.output.expanduser().resolve()
    work_directory = args.work_directory.expanduser().resolve()
    validate_qualification_paths(
        app,
        source,
        segment,
        output,
        work_directory,
        requested_work_directory=args.work_directory.expanduser(),
    )
    if not source.is_file() or not segment.is_file():
        raise FeatureQualificationFailure("Feature source and deterministic segment must both be available.")
    source_sha256 = sha256_file(source)
    segment_sha256 = sha256_file(segment)
    if source_sha256 != args.expected_source_sha256.lower():
        raise FeatureQualificationFailure("Feature source SHA-256 does not match the expected parent source.")
    if segment_sha256 != args.expected_segment_sha256.lower():
        raise FeatureQualificationFailure("Deterministic segment SHA-256 does not match the expected replacement.")
    source_info = source_video_info(tools.ffprobe, source)
    try:
        source_frame_rate = Fraction(str(source_info["frame_rate"]))
        requested_frame_rate = Fraction(args.frame_rate)
    except (ValueError, ZeroDivisionError) as error:
        raise FeatureQualificationFailure("Configured or source frame rate is invalid.") from error
    source_packet_count = require_int_value(source_info, "packet_count")
    source_video_duration = require_number_value(source_info, "video_duration_seconds")
    expected_frames = (
        source_packet_count
        if requested_frame_rate == source_frame_rate
        else round(source_video_duration * float(requested_frame_rate))
    )
    expected_output_duration = expected_frames / float(requested_frame_rate)

    work_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    work_directory.chmod(0o700)
    short_output = work_directory / "short-direct-4k.mov"
    feature_output = work_directory / "feature-direct-4k.mov"
    trace_path = work_directory / f"powermetrics-feature-{uuid.uuid4().hex}.txt"
    workload_timeout_seconds = args.timeout_hours * 3600
    try:
        short_run = run_direct_workload(
            tools,
            segment,
            short_output,
            frame_rate=args.frame_rate,
            timeout_seconds=workload_timeout_seconds,
        )
        feature_run = run_direct_workload(
            tools,
            source,
            feature_output,
            frame_rate=args.frame_rate,
            trace_path=trace_path,
            timeout_seconds=workload_timeout_seconds,
        )
        boundedness = resource_boundedness(
            short_run,
            feature_run,
            max_rss_growth_bytes=args.max_rss_growth_mib * 1024 * 1024,
        )
        cancellation = run_packaged_cancellation(
            app,
            source,
            work_directory / f"cancellation-{uuid.uuid4().hex}",
            cancel_after_bytes=args.cancel_after_mib * 1024 * 1024,
            timeout_seconds=workload_timeout_seconds,
        )
    finally:
        remove_direct_workload_artifacts(short_output, feature_output)

    short_acceptance = require_mapping_value(short_run, "acceptance")
    feature_acceptance = require_mapping_value(feature_run, "acceptance")
    feature_encoder = require_mapping_value(feature_run, "encoder")
    feature_artifact = require_mapping_value(feature_run, "artifact")
    cancellation_acceptance = require_mapping_value(cancellation, "acceptance")
    feature_frame_count = require_int_value(feature_encoder, "frame_count")
    feature_duration = require_number_value(feature_artifact, "duration_seconds")
    complete_feature_frames = feature_frame_count == expected_frames
    duration_tolerance_seconds = max(0.1, 2 / float(requested_frame_rate))
    complete_feature_duration = abs(feature_duration - expected_output_duration) <= duration_tolerance_seconds
    short_passed = require_bool_value(short_acceptance, "passed")
    feature_passed = require_bool_value(feature_acceptance, "passed")
    boundedness_passed = require_bool_value(boundedness, "passed")
    cancellation_passed = require_bool_value(cancellation_acceptance, "passed")
    passed = (
        short_passed
        and feature_passed
        and boundedness_passed
        and cancellation_passed
        and complete_feature_frames
        and complete_feature_duration
    )
    evidence = {
        "acceptance": {
            "complete_feature_length_duration": complete_feature_duration,
            "complete_feature_length_frame_count": complete_feature_frames,
            "packaged_cancellation": cancellation_passed,
            "passed": passed,
            "resource_boundedness": boundedness_passed,
            "short_direct_workload": short_passed,
            "feature_direct_workload": feature_passed,
        },
        "boundedness": boundedness,
        "cancellation": cancellation,
        "configuration": {
            "automatic_direct_4k_quality": 0.6,
            "cancel_after_mib": args.cancel_after_mib,
            "frame_rate": args.frame_rate,
            "max_rss_growth_mib": args.max_rss_growth_mib,
            "output_duration_tolerance_seconds": duration_tolerance_seconds,
        },
        "feature_run": feature_run,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "package": {
            "app_tree_sha256": app_tree_sha256(app.path),
            "bundle_identifier": app.bundle_identifier,
            "capability": capability_evidence(tools),
            "helper_sha256": sha256_file(app.helper),
            "version": app.version,
            "worker_sha256": sha256_file(app.worker),
        },
        "platform": {
            "hardware_model": subprocess.check_output(["sysctl", "-n", "hw.model"], text=True).strip(),
            "macos_version": platform.mac_ver()[0],
        },
        "schema_version": 2,
        "short_run": short_run,
        "sources": {
            "feature": {
                **source_info,
                "sha256": source_sha256,
                "size_bytes": source.stat().st_size,
            },
            "segment": {
                "sha256": segment_sha256,
                "size_bytes": segment.stat().st_size,
            },
        },
    }
    evidence_text = json.dumps(evidence, indent=2, sort_keys=True) + "\n"
    encoded = evidence_text.encode("utf-8")
    if len(encoded) > MAX_EVIDENCE_BYTES:
        raise FeatureQualificationFailure("Feature-length qualification evidence exceeded its bounded size limit.")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_output = output.with_suffix(output.suffix + ".tmp")
    temporary_output.write_text(evidence_text, encoding="utf-8")
    temporary_output.replace(output)
    return evidence


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Qualify feature-length real MVC direct 4K resources, thermal behavior, progress, and cancellation."
    )
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--segment", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--work-directory", type=Path, required=True)
    parser.add_argument("--expected-source-sha256", required=True)
    parser.add_argument("--expected-segment-sha256", required=True)
    parser.add_argument("--frame-rate", default=DEFAULT_FRAME_RATE)
    parser.add_argument("--max-rss-growth-mib", type=int, default=DEFAULT_MAX_RSS_GROWTH_MIB)
    parser.add_argument("--cancel-after-mib", type=int, default=DEFAULT_CANCEL_AFTER_MIB)
    parser.add_argument("--timeout-hours", type=float, default=6)
    return parser.parse_args()


def main() -> int:
    try:
        evidence = qualify(parse_args())
    except (FeatureQualificationFailure, OSError, subprocess.SubprocessError) as error:
        raise SystemExit(f"error: {error}") from error
    acceptance = require_mapping_value(evidence, "acceptance")
    print(json.dumps(acceptance, indent=2, sort_keys=True))
    return 0 if require_bool_value(acceptance, "passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
