"""Run real regular/forced PGS OCR and final mux with native request diagnostics.

This is a diagnostic experiment, not a production backend selector. Each mode
must run in a fresh process; a nonzero exit means qualification did not pass.
"""

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import subprocess
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from bd_to_avp.modules.config import config
from bd_to_avp.vendor.pgsrip.ocr import AppleVisionOcr
from tests.test_final_mux_real_tools import FORCED_PGS_CUES, PGS_CUES, FinalMuxRealToolTests


def emit(event: str, **fields: Any) -> None:
    print(json.dumps({"event": event, **fields}, default=str), flush=True)


def request_details(request: Any) -> dict[str, Any]:
    details = {
        "revision": request.revision(),
        "recognition_level": request.recognitionLevel(),
        "languages": list(request.recognitionLanguages()),
        "language_correction": bool(request.usesLanguageCorrection()),
    }
    try:
        devices, error = request.supportedComputeStageDevicesAndReturnError_(None)
        details["compute_devices"] = str(devices)
        details["compute_devices_error"] = str(error)
    except (AttributeError, RuntimeError) as error:
        details["compute_devices_unavailable"] = str(error)
    return details


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("default", "cpu-only", "fast"), default="default")
    parser.add_argument(
        "--dump-bitmaps",
        type=Path,
        help="Save each exact grayscale bitmap and request outcome for scripts/vision_ocr_control.swift",
    )
    args = parser.parse_args()
    if platform.system() != "Darwin":
        parser.error("requires macOS with Apple Vision")

    # A diagnostic run must never inherit the hosted skip or silently pass a
    # missing-tool prerequisite. All recognition and mux assertions stay live.
    os.environ.pop("BD_TO_AVP_HOSTED_VISION_OCR_SKIP_REASON", None)
    os.environ["BD_TO_AVP_REQUIRE_REAL_PGS_TESTS"] = "1"
    emit(
        "environment",
        mode=args.mode,
        macos=subprocess.check_output(["sw_vers"], text=True).strip(),
        architecture=platform.machine(),
        hardware_model=subprocess.check_output(["sysctl", "-n", "hw.model"], text=True).strip(),
        python=platform.python_version(),
        pyobjc_vision=importlib.metadata.version("pyobjc-framework-Vision"),
        runner_image=os.environ.get("ImageOS"),
        runner_image_version=os.environ.get("ImageVersion"),
        source_sha=os.environ.get("GITHUB_SHA"),
        run_id=os.environ.get("GITHUB_RUN_ID"),
        run_attempt=os.environ.get("GITHUB_RUN_ATTEMPT"),
        ffmpeg=str(config.FFMPEG_PATH),
        ffmpeg_version=subprocess.check_output([str(config.FFMPEG_PATH), "-version"], text=True).strip(),
    )
    vision, quartz = AppleVisionOcr._load_frameworks()
    handler_class = vision.VNImageRequestHandler
    observed_requests = 0
    dumped_bitmaps: list[dict[str, Any]] = []
    if args.dump_bitmaps:
        args.dump_bitmaps.mkdir(parents=True, exist_ok=True)

    def dump_bitmap(image: Any, request: Any, success: bool, error: Any, results: list[str]) -> None:
        # Raw provider bytes, not a re-encoded image, so the Swift control
        # rebuilds byte-identical input.
        data = bytes(quartz.CGDataProviderCopyData(quartz.CGImageGetDataProvider(image)))
        name = f"{len(dumped_bitmaps):03d}.gray"
        (args.dump_bitmaps / name).write_bytes(data)
        dumped_bitmaps.append(
            {
                "file": name,
                "width": quartz.CGImageGetWidth(image),
                "height": quartz.CGImageGetHeight(image),
                "bytes_per_row": quartz.CGImageGetBytesPerRow(image),
                "sha256": hashlib.sha256(data).hexdigest(),
                "languages": list(request.recognitionLanguages()),
                "uses_language_correction": bool(request.usesLanguageCorrection()),
                "python_success": success,
                "python_error": None if error is None else str(error),
                "python_results": results,
            }
        )
        # Rewritten per request so a crash still leaves a usable manifest.
        (args.dump_bitmaps / "manifest.json").write_text(json.dumps(dumped_bitmaps, indent=2))

    class DiagnosticHandler:
        @classmethod
        def alloc(cls) -> "DiagnosticHandler":
            return cls()

        def initWithCGImage_options_(self, image: Any, options: Any) -> "DiagnosticHandler":
            self.image = image
            self.handler = handler_class.alloc().initWithCGImage_options_(image, options)
            return self

        def performRequests_error_(self, requests: list[Any], error_out: Any) -> Any:
            nonlocal observed_requests
            for request in requests:
                if args.mode == "cpu-only":
                    import CoreML

                    stages, error = request.supportedComputeStageDevicesAndReturnError_(None)
                    if error or not stages:
                        raise RuntimeError(f"Cannot enumerate CPU compute stages: {error}")
                    for stage, devices in stages.items():
                        cpu = next(
                            (device for device in devices if isinstance(device, CoreML.MLCPUComputeDevice)), None
                        )
                        if cpu is None:
                            raise RuntimeError(f"No supported CPU device for {stage}")
                        request.setComputeDevice_forComputeStage_(cpu, stage)
                        selected = request.computeDeviceForComputeStage_(stage)
                        if not isinstance(selected, CoreML.MLCPUComputeDevice):
                            raise RuntimeError(f"CPU assignment not confirmed for {stage}: {selected}")
                        emit("compute_assignment", stage=str(stage), device=str(selected))
                elif args.mode == "fast":
                    request.setRecognitionLevel_(vision.VNRequestTextRecognitionLevelFast)
                    if request.recognitionLevel() != vision.VNRequestTextRecognitionLevelFast:
                        raise RuntimeError("Fast recognition setting not confirmed")
                emit("request", thread=threading.current_thread().name, **request_details(request))
                observed_requests += 1
            success, error = self.handler.performRequests_error_(requests, error_out)
            results = [
                [
                    str(candidate.string())
                    for observation in request.results() or []
                    for candidate in observation.topCandidates_(1)
                ]
                for request in requests
            ]
            emit("response", success=bool(success), error=None if error is None else str(error), results=results)
            if args.dump_bitmaps:
                for request, request_results in zip(requests, results, strict=True):
                    dump_bitmap(self.image, request, bool(success), error, request_results)
            return success, error

    test = FinalMuxRealToolTests("test_pgs_subtitles_rip_to_srt_and_reach_the_final_mux_with_forced_flag")
    with patch.object(vision, "VNImageRequestHandler", DiagnosticHandler):
        result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite([test]))
    minimum_requests = len(PGS_CUES) + len(FORCED_PGS_CUES)
    passed = (
        result.wasSuccessful() and result.testsRun == 1 and not result.skipped and observed_requests >= minimum_requests
    )
    emit(
        "qualification",
        mode=args.mode,
        passed=passed,
        tests_run=result.testsRun,
        skips=len(result.skipped),
        observed_requests=observed_requests,
        minimum_requests=minimum_requests,
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
