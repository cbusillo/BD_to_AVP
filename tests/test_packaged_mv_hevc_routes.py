import json
import unittest

from pathlib import Path

from scripts.verify_packaged_mv_hevc_routes import (
    PROTOCOL_VERSION,
    UNAVAILABLE_HELPER_SOURCE,
    PackagedRouteFailure,
    WorkerResult,
    build_worker_request,
    parse_worker_events,
    validate_route_pair,
)


def event(sequence: int, *, job_id: str, payload: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "type": "log",
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
            "bitrate_mbps": 40,
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

    def test_unavailable_helper_implements_only_the_probe_contract(self) -> None:
        self.assertIn('stereo_mv_hevc_encode_supported\\":false', UNAVAILABLE_HELPER_SOURCE)
        self.assertIn("return 2", UNAVAILABLE_HELPER_SOURCE)
        self.assertIn("must not encode", UNAVAILABLE_HELPER_SOURCE)


if __name__ == "__main__":
    unittest.main()
