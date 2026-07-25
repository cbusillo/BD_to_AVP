import json
import unittest

from pathlib import Path

from scripts.verify_packaged_mv_hevc_routes import (
    METALFX_UNAVAILABLE_HELPER_SOURCE,
    PROTOCOL_VERSION,
    UNAVAILABLE_HELPER_SOURCE,
    PackagedRouteFailure,
    WorkerResult,
    build_worker_request,
    parse_worker_events,
    validate_stage_contract,
    validate_route_pair,
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
    def test_full_request_uses_protocol_v10_automatic_direct_intent(self) -> None:
        request = build_worker_request(
            "convert_source",
            Path("/tmp/source.mkv"),
            Path("/tmp/output"),
            job_id="full-job",
        )

        self.assertEqual(request["protocol_version"], 10)
        self.assertEqual(request["operation"], "convert_source")
        self.assertEqual(request["encoding"]["video"]["route_intent"], "automatic")
        self.assertEqual(request["encoding"]["video"]["direct_bitrate"], {"mode": "automatic"})
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
    def test_event_parser_requires_contiguous_protocol_v10_stream(self) -> None:
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


class PackagedRouteParityTests(unittest.TestCase):
    def test_direct_full_and_preview_require_identical_route(self) -> None:
        route = {
            "intent": "automatic",
            "selected": "direct_mv_hevc",
            "reason": "direct_eligible",
            "rate_control": "quality",
            "quality": 0.7,
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
