import json
import signal
import subprocess
import tempfile
import unittest

from pathlib import Path
from unittest.mock import Mock, patch

from bd_to_avp.modules.video_route import AUTOMATIC_DIRECT_QUALITY
from scripts.verify_packaged_mv_hevc_routes import (
    METALFX_UNAVAILABLE_HELPER_SOURCE,
    PROTOCOL_VERSION,
    UNAVAILABLE_HELPER_SOURCE,
    PackagedRouteFailure,
    WorkerResult,
    _preview_duration,
    _terminate_worker_process,
    _terminal_result,
    _validate_stream_contract,
    build_worker_request,
    parse_worker_events,
    validate_qualification_paths,
    validate_stage_contract,
    validate_route_pair,
    verify_packaged_routes,
)


def event(
    sequence: int,
    *,
    job_id: str,
    payload: dict[str, object] | None = None,
    event_type: str = "log",
) -> dict[str, object]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "type": event_type,
        "job_id": job_id,
        "sequence": sequence,
        "payload": payload or {},
    }


def worker_result(
    route: dict[str, object],
    *,
    operation: str,
    event_code: str,
) -> WorkerResult:
    route_event = event(
        0,
        job_id="job",
        payload={"code": event_code, "video_route": route},
    )
    return WorkerResult(
        operation=operation,
        route=route,
        output_path=Path("/tmp/output.mov"),
        events=(route_event,),
        preview={"position": "middle"} if operation == "preview_source" else None,
    )


class PackagedRequestTests(unittest.TestCase):
    def test_full_request_uses_protocol_v11_automatic_direct_intent(self) -> None:
        request = build_worker_request(
            "convert_source",
            Path("/tmp/source.mkv"),
            Path("/tmp/output"),
            job_id="full-job",
        )

        self.assertEqual(request["protocol_version"], 11)
        self.assertEqual(request["operation"], "convert_source")
        self.assertEqual(request["encoding"]["video"]["route_intent"], "automatic")
        self.assertEqual(request["encoding"]["video"]["quality_intent"], {"mode": "custom"})
        self.assertEqual(request["encoding"]["video"]["direct_bitrate"], {"mode": "automatic"})
        self.assertEqual(
            request["encoding"]["video"]["generated_fallback"],
            {"eye_bitrate": {"mode": "automatic"}, "merge_quality": 75},
        )
        self.assertNotIn("preview", request)

    def test_preview_request_preserves_parent_and_duration(self) -> None:
        request = build_worker_request(
            "preview_source",
            Path("/tmp/source.mkv"),
            Path("/tmp/output"),
            job_id="preview-job",
            parent_job_id="full-job",
            preview_duration_seconds=45,
        )

        self.assertEqual(
            request["preview"],
            {"parent_job_id": "full-job", "position": "middle", "duration_seconds": 45},
        )

    def test_preview_request_requires_parent_job(self) -> None:
        with self.assertRaisesRegex(ValueError, "parent job"):
            build_worker_request(
                "preview_source",
                Path("/tmp/source.mkv"),
                Path("/tmp/output"),
                job_id="preview-job",
            )

    def test_upscale_request_uses_production_4k_profile_shape(self) -> None:
        request = build_worker_request(
            "convert_source",
            Path("/tmp/source.mkv"),
            Path("/tmp/output"),
            job_id="4k-job",
            upscale_enabled=True,
        )

        self.assertEqual(request["encoding"]["upscale"], {"enabled": True, "quality": 80})
        self.assertEqual(request["encoding"]["resolution"], "")
        self.assertFalse(request["encoding"]["crop_black_bars"])


class PackagedEventTests(unittest.TestCase):
    def test_event_parser_requires_contiguous_protocol_v11_stream(self) -> None:
        stdout = "\n".join(
            json.dumps(item)
            for item in (
                event(0, job_id="job"),
                event(1, job_id="job"),
            )
        )

        parsed = parse_worker_events(stdout, job_id="job")

        self.assertEqual([item["sequence"] for item in parsed], [0, 1])

    def test_event_parser_rejects_sequence_gap(self) -> None:
        stdout = "\n".join(
            json.dumps(item)
            for item in (
                event(0, job_id="job"),
                event(2, job_id="job"),
            )
        )

        with self.assertRaisesRegex(PackagedRouteFailure, "contiguous"):
            parse_worker_events(stdout, job_id="job")

    def test_event_parser_rejects_wrong_job(self) -> None:
        with self.assertRaisesRegex(PackagedRouteFailure, "wrong job"):
            parse_worker_events(json.dumps(event(0, job_id="other")), job_id="job")

    def test_event_parser_rejects_non_json_output(self) -> None:
        with self.assertRaisesRegex(PackagedRouteFailure, "valid JSON"):
            parse_worker_events("not json", job_id="job")

    def test_terminal_decision_reports_safe_id_and_prompt(self) -> None:
        events = (
            event(
                0,
                job_id="job",
                event_type="job.decision_required",
                payload={
                    "decision": {
                        "id": "subtitle_decision_required",
                        "prompt": "Subtitle extraction did not produce usable subtitle files.",
                    }
                },
            ),
        )

        with self.assertRaisesRegex(PackagedRouteFailure, "subtitle_decision_required"):
            _terminal_result(events, "preview_source")


class PackagedMediaContractTests(unittest.TestCase):
    def test_preview_timing_matches_requested_middle_range(self) -> None:
        result = WorkerResult(
            operation="preview_source",
            route={},
            output_path=Path("preview.mov"),
            events=(),
            preview={
                "duration_seconds": 60.435,
                "position": "middle",
                "source_duration_seconds": 65.649,
                "start_seconds": 2.544,
            },
        )

        self.assertAlmostEqual(_preview_duration(result, 65.649), 60.435)

    def test_stream_contract_rejects_missing_subtitles(self) -> None:
        streams = (
            {"codec_name": "hevc", "codec_type": "video", "language": None},
            {"codec_name": "aac", "codec_type": "audio", "language": "eng"},
        )

        with self.assertRaisesRegex(PackagedRouteFailure, "exactly one"):
            _validate_stream_contract(
                streams,
                {
                    "audio": ("aac", "eng"),
                    "subtitle": ("mov_text", "eng"),
                    "video": ("hevc", None),
                },
            )

    def test_timeout_cleanup_signals_worker_group(self) -> None:
        process = Mock(pid=123)
        process.poll.return_value = None
        process.communicate.side_effect = [
            subprocess.TimeoutExpired(cmd="worker", timeout=30),
            ("", ""),
        ]
        root = Mock(pid=123)
        child = Mock(pid=124)

        with (
            patch(
                "scripts.verify_packaged_mv_hevc_routes._process_tree",
                return_value=[root, child],
            ),
            patch(
                "scripts.verify_packaged_mv_hevc_routes._process_is_running",
                return_value=False,
            ),
            patch(
                "scripts.verify_packaged_mv_hevc_routes.psutil.wait_procs",
                return_value=([child], []),
            ),
            patch("scripts.verify_packaged_mv_hevc_routes.os.killpg") as killpg,
        ):
            _terminate_worker_process(process)

        killpg.assert_any_call(123, signal.SIGTERM)
        killpg.assert_any_call(123, signal.SIGKILL)
        child.terminate.assert_called_once_with()

    def test_qualification_paths_reject_source_and_destination_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            app = root / "Qualification.app"
            app.mkdir()
            source = root / "source.mkv"
            source.write_bytes(b"source")

            with self.assertRaisesRegex(PackagedRouteFailure, "representative source"):
                validate_qualification_paths(app, source, source, None)
            with self.assertRaisesRegex(PackagedRouteFailure, "must be distinct"):
                validate_qualification_paths(app, source, root / "evidence.json", root / "evidence.json")
            with self.assertRaisesRegex(PackagedRouteFailure, "outside the app bundle"):
                validate_qualification_paths(app, source, app / "evidence.json", None)

    def test_qualification_paths_reject_symlink_destinations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            app = root / "Qualification.app"
            app.mkdir()
            source = root / "source.mkv"
            source.write_bytes(b"source")
            real_output = root / "real-evidence.json"
            real_output.write_text("{}", encoding="utf-8")
            linked_output = root / "evidence.json"
            linked_output.symlink_to(real_output)

            with self.assertRaisesRegex(PackagedRouteFailure, "must not be a symlink"):
                validate_qualification_paths(app, source, linked_output, None)

    def test_route_verifier_rejects_wrong_source_hash_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "source.mkv"
            source.write_bytes(b"source")

            with (
                patch("scripts.verify_packaged_mv_hevc_routes.read_app_bundle", return_value=Mock()),
                self.assertRaisesRegex(PackagedRouteFailure, "SHA-256"),
            ):
                verify_packaged_routes(
                    Path("Qualification.app"),
                    source,
                    fixture_output=None,
                    expected_source_sha256="0" * 64,
                )


class PackagedRouteParityTests(unittest.TestCase):
    def test_direct_full_and_preview_require_identical_route(self) -> None:
        route = {
            "intent": "automatic",
            "selected": "direct_mv_hevc",
            "reason": "direct_eligible",
            "rate_control": "quality",
            "quality": AUTOMATIC_DIRECT_QUALITY,
        }

        validate_route_pair(
            worker_result(route, operation="convert_source", event_code="video_route_selected"),
            worker_result(route, operation="preview_source", event_code="video_route_selected"),
            expected_selected="direct_mv_hevc",
            expected_fallback_reason=None,
        )

    def test_fallback_requires_visible_pre_input_reason(self) -> None:
        route = {
            "intent": "automatic",
            "selected": "generated_mv_hevc",
            "reason": "direct_capability_unavailable",
            "eye_bitrate_mbps": 20,
            "merge_quality": 75,
            "fallback_reason": "stereo_mv_hevc_encode_unavailable",
            "fallback_timing": "pre_input",
        }

        validate_route_pair(
            worker_result(route, operation="convert_source", event_code="video_route_fallback"),
            worker_result(route, operation="preview_source", event_code="video_route_fallback"),
            expected_selected="generated_mv_hevc",
            expected_fallback_reason="stereo_mv_hevc_encode_unavailable",
        )

    def test_fallback_requires_automatic_generated_settings(self) -> None:
        route = {
            "intent": "automatic",
            "selected": "generated_mv_hevc",
            "reason": "direct_capability_unavailable",
            "eye_bitrate_mbps": 18,
            "merge_quality": 75,
            "fallback_reason": "stereo_mv_hevc_encode_unavailable",
            "fallback_timing": "pre_input",
        }

        with self.assertRaisesRegex(PackagedRouteFailure, "eye bitrate"):
            validate_route_pair(
                worker_result(route, operation="convert_source", event_code="video_route_fallback"),
                worker_result(route, operation="preview_source", event_code="video_route_fallback"),
                expected_selected="generated_mv_hevc",
                expected_fallback_reason="stereo_mv_hevc_encode_unavailable",
            )

    def test_metalfx_route_pair_requires_explicit_upscale_mode(self) -> None:
        route = {
            "intent": "automatic",
            "selected": "direct_mv_hevc",
            "reason": "direct_upscale_eligible",
            "rate_control": "quality",
            "quality": 0.6,
            "upscale_mode": "metalfx",
        }

        validate_route_pair(
            worker_result(route, operation="convert_source", event_code="video_route_selected"),
            worker_result(route, operation="preview_source", event_code="video_route_selected"),
            expected_selected="direct_mv_hevc",
            expected_fallback_reason=None,
            expected_upscale_mode="metalfx",
        )

    def test_stage_contract_rejects_file_upscale_for_direct_metalfx(self) -> None:
        result = WorkerResult(
            operation="convert_source",
            route={},
            output_path=Path("/tmp/output.mov"),
            events=(
                event(
                    0,
                    job_id="job",
                    event_type="stage.started",
                    payload={"stage": "create_left_right_files"},
                ),
                event(
                    1,
                    job_id="job",
                    event_type="stage.started",
                    payload={"stage": "upscale_video"},
                ),
            ),
            preview=None,
        )

        with self.assertRaisesRegex(PackagedRouteFailure, "forbidden stages"):
            validate_stage_contract(
                result,
                required={"create_left_right_files"},
                forbidden={"combine_to_mv_hevc", "upscale_video"},
            )

    def test_route_pair_rejects_preview_drift(self) -> None:
        full_route = {"selected": "direct_mv_hevc"}
        preview_route = {"selected": "generated_mv_hevc"}

        with self.assertRaisesRegex(PackagedRouteFailure, "different video routes"):
            validate_route_pair(
                worker_result(full_route, operation="convert_source", event_code="video_route_selected"),
                worker_result(preview_route, operation="preview_source", event_code="video_route_selected"),
                expected_selected="direct_mv_hevc",
                expected_fallback_reason=None,
            )

    def test_direct_route_pair_rejects_fixed_automatic_bitrate(self) -> None:
        route = {
            "intent": "automatic",
            "selected": "direct_mv_hevc",
            "reason": "direct_eligible",
            "rate_control": "average_bitrate",
            "bitrate_mbps": 40,
        }

        with self.assertRaisesRegex(PackagedRouteFailure, "quality rate control"):
            validate_route_pair(
                worker_result(route, operation="convert_source", event_code="video_route_selected"),
                worker_result(route, operation="preview_source", event_code="video_route_selected"),
                expected_selected="direct_mv_hevc",
                expected_fallback_reason=None,
            )

    def test_unavailable_helper_implements_only_the_probe_contract(self) -> None:
        self.assertIn('stereo_mv_hevc_encode_supported\\":false', UNAVAILABLE_HELPER_SOURCE)
        self.assertIn("return 2", UNAVAILABLE_HELPER_SOURCE)
        self.assertIn("must not encode", UNAVAILABLE_HELPER_SOURCE)

    def test_metalfx_unavailable_helper_preserves_ordinary_direct_support(self) -> None:
        self.assertIn('stereo_mv_hevc_encode_supported\\":true', METALFX_UNAVAILABLE_HELPER_SOURCE)
        self.assertIn('metalfx_2x_mv_hevc_supported\\":false', METALFX_UNAVAILABLE_HELPER_SOURCE)
        self.assertIn("return 0", METALFX_UNAVAILABLE_HELPER_SOURCE)
        self.assertIn("must not encode", METALFX_UNAVAILABLE_HELPER_SOURCE)


if __name__ == "__main__":
    unittest.main()
