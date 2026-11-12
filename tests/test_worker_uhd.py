import io
import json
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

import pytest

from bd_to_avp.modules.config import config
from bd_to_avp.modules.video_mode import VideoMode
from bd_to_avp.modules.video_route import (
    DirectMVHEVCCapability,
    VideoRouteKind,
    VideoRoutePreflightError,
    resolve_video_route,
)
from bd_to_avp.worker.__main__ import run_worker
from bd_to_avp.worker.controls import WorkerControls
from bd_to_avp.worker.protocol import JobSpec, WorkerEventEmitter, WorkerProtocolError

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize("version", [13, 14])
def test_worker_replies_in_request_version(version: int, tmp_path: Path) -> None:
    request = json.loads((FIXTURES / f"native_worker_convert_v{version}.json").read_text())
    request.update(operation="inspect_source", source={"kind": "direct_file", "path": str(tmp_path / "missing.mkv")})
    for key in ("encoding", "destination", "job"):
        request.pop(key)
    output = io.StringIO()
    run_worker(
        input_stream=io.StringIO(json.dumps(request) + "\n"),
        output_stream=output,
        diagnostic_stream=io.StringIO(),
        establish_session=False,
    )
    events = [json.loads(line) for line in output.getvalue().splitlines()]
    assert events[0]["type"] == "worker.ready"
    assert events[-1]["type"] == "job.failed"
    assert all(event["protocol_version"] == version for event in events)


def test_4k_copy_reaches_hdr_route_without_generated_fallback() -> None:
    request = json.loads((FIXTURES / "native_worker_convert_v14.json").read_text())
    request["uhd_reference"] = "/Movies/4k.mkv"
    job = JobSpec.from_json_line(json.dumps(request))
    probe = Mock(return_value=DirectMVHEVCCapability(supported=True, reason="supported"))
    assert job.encoding is not None and job.job is not None
    route = resolve_video_route(job.encoding, job.job, uhd_reference=job.uhd_reference, capability_probe=probe)
    assert route.selected is VideoRouteKind.UHD_HDR_MV_HEVC
    assert route.uhd_reference == Path(request["uhd_reference"])
    assert route.fallback_reason is None
    probe.assert_called_once()


@pytest.mark.parametrize("value", [42, "relative.mkv", ""])
def test_4k_copy_requires_absolute_file_path(value: object) -> None:
    request = json.loads((FIXTURES / "native_worker_convert_v14.json").read_text())
    request["uhd_reference"] = value
    with pytest.raises(WorkerProtocolError):
        JobSpec.from_json_line(json.dumps(request))


def test_v13_cannot_silently_ignore_a_4k_copy() -> None:
    request = json.loads((FIXTURES / "native_worker_convert_v13.json").read_text())
    request["uhd_reference"] = "/Movies/4k.mkv"
    with pytest.raises(WorkerProtocolError):
        JobSpec.from_json_line(json.dumps(request))


@pytest.mark.parametrize(
    "case", json.loads((FIXTURES / "uhd_route_cases.json").read_text()), ids=lambda case: case["name"]
)
def test_shared_4k_route_cases(case: dict[str, Any]) -> None:
    job = JobSpec.from_json_line((FIXTURES / "native_worker_convert_uhd_v14.json").read_text())
    assert job.encoding is not None and job.job is not None
    encoding = replace(
        job.encoding,
        video=replace(job.encoding.video, mode=VideoMode.AV1_SBS if case.get("mode") == "av1" else VideoMode.MV_HEVC),
        upscale=replace(job.encoding.upscale, enabled=case.get("upscale", False)),
        swap_eyes=case.get("swap_eyes", False),
        frame_rate=case.get("frame_rate", ""),
        resolution=case.get("resolution", ""),
        crop_black_bars=case.get("crop_black_bars", False),
    )
    options = replace(
        job.job,
        start_stage=case.get("start_stage", 1),
        software_encoder=case.get("software_encoder", False),
        keep_files=case.get("keep_files", False),
    )

    def probe() -> DirectMVHEVCCapability:
        return DirectMVHEVCCapability(supported=True, reason="supported")

    if case["accepted"]:
        route = resolve_video_route(encoding, options, uhd_reference=job.uhd_reference, capability_probe=probe)
        assert route.selected is VideoRouteKind.UHD_HDR_MV_HEVC
    else:
        with pytest.raises(VideoRoutePreflightError):
            resolve_video_route(encoding, options, uhd_reference=job.uhd_reference, capability_probe=probe)


def test_converter_refusal_reaches_queue_item_message(tmp_path: Path) -> None:
    request = json.loads((FIXTURES / "native_worker_convert_uhd_v14.json").read_text())
    source = tmp_path / "3d.mkv"
    source.write_bytes(b"test source")
    request["source"]["path"] = str(source)
    request["destination"]["path"] = str(tmp_path / "output")
    refusal = "These copies are different cuts of the movie. Choose the same cut."
    output = io.StringIO()
    with (
        patch.object(config, "configure_tool_environment"),
        patch(
            "bd_to_avp.modules.video_route.probe_direct_mv_hevc_capability",
            return_value=DirectMVHEVCCapability(supported=True, reason="supported"),
        ),
        patch("bd_to_avp.modules.process.process_each", side_effect=VideoRoutePreflightError(refusal)),
    ):
        run_worker(io.StringIO(json.dumps(request) + "\n"), output, io.StringIO(), establish_session=False)
    failure = json.loads(output.getvalue().splitlines()[-1])
    assert failure["type"] == "job.failed"
    assert failure["payload"]["error"]["message"] == refusal


@pytest.mark.parametrize(
    "fixture",
    [
        path
        for path in sorted(FIXTURES.glob("native_worker_*_v13.json"))
        if json.loads(path.read_text()).get("type") == "job.start"
    ],
    ids=lambda path: path.name,
)
def test_all_v13_job_shapes_still_load(fixture: Path) -> None:
    request = json.loads(fixture.read_text())
    job = JobSpec.from_json_line(json.dumps(request))
    assert job.operation.value == request["operation"]
    assert job.source.path == Path(request["source"]["path"])
    assert job.uhd_reference is None


def test_v13_keep_waiting_command_still_reaches_runner() -> None:
    command = json.loads((FIXTURES / "native_worker_keep_waiting_v13.json").read_text())
    controls = WorkerControls(
        command["job_id"],
        WorkerEventEmitter(io.StringIO(), command["job_id"], protocol_version=command["protocol_version"]),
    )
    controls.register_run(command["tool_run_id"])
    controls.receive_line(json.dumps(command).encode())
    received = controls.take_commands(command["tool_run_id"])
    assert len(received) == 1
    assert received[0].command_id == command["command_id"]
