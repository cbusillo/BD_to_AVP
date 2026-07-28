import json
import tempfile
import unittest

from pathlib import Path
from unittest.mock import Mock, patch

from bd_to_avp.modules.aac_layout_policy import LAYOUT_CHANNELS
from scripts.verify_packaged_aac_layouts import (
    DEFAULT_MANIFEST,
    REQUIRED_COVERAGE,
    PackagedAacFailure,
    WorkerExecution,
    _build_request,
    _execute_worker,
    _failed_case,
    _identity_evidence,
    _policy_evidence,
    _render_evidence,
    _validate_streams,
    load_fixture_manifest,
    validate_paths,
)
from scripts.verify_packaged_mv_hevc_routes import PROTOCOL_VERSION


def worker_event(
    sequence: int,
    event_type: str,
    payload: dict[str, object],
    *,
    job_id: str = "job",
) -> dict[str, object]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "type": event_type,
        "job_id": job_id,
        "sequence": sequence,
        "payload": payload,
    }


class PackagedAacManifestTests(unittest.TestCase):
    def test_committed_manifest_covers_release_gate(self) -> None:
        manifest = load_fixture_manifest()
        covered = {coverage for case in manifest.cases for coverage in case.coverage}
        actions = {
            track.policy.action.value
            for case in manifest.cases
            for track in case.selected_tracks
            if track.policy is not None
        }

        self.assertEqual(covered, set(REQUIRED_COVERAGE))
        self.assertEqual(actions, {"preserve", "remap", "downmix"})
        self.assertEqual(
            {case.case_id for case in manifest.cases if case.expected_rejection is not None},
            {"fail-missing-layout", "fail-pce-layout", "fail-unknown-layout"},
        )

    def test_committed_manifest_checks_multilingual_metadata(self) -> None:
        case = next(case for case in load_fixture_manifest().cases if case.case_id == "preserve-multilingual")

        self.assertEqual([track.language for track in case.tracks], ["eng", "jpn"])
        self.assertEqual([track.handler_title for track in case.tracks], ["English Stereo", "Japanese 5.1"])
        self.assertEqual([track.default for track in case.tracks], [True, False])

    def test_committed_manifest_uses_layout_carrying_lossless_sources(self) -> None:
        cases = {case.case_id: case for case in load_fixture_manifest().cases}

        self.assertEqual(cases["remap-5_1-side"].tracks[0].codec_name, "flac")
        self.assertEqual(cases["downmix-7_1"].tracks[0].codec_name, "flac")
        unqualified = cases["fail-unknown-layout"].tracks[0]
        self.assertEqual(unqualified.codec_name, "flac")
        self.assertEqual(unqualified.source_layout, "3.1.2")
        self.assertNotIn(unqualified.source_layout, LAYOUT_CHANNELS)

    def test_manifest_rejects_policy_drift(self) -> None:
        document = json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))
        document["cases"][1]["tracks"][0]["policy_id"] = "wrong"

        with tempfile.TemporaryDirectory() as temporary_directory:
            manifest_path = Path(temporary_directory) / "manifest.json"
            manifest_path.write_text(json.dumps(document), encoding="utf-8")

            with self.assertRaisesRegex(PackagedAacFailure, "require policy ids"):
                load_fixture_manifest(manifest_path)

    def test_manifest_rejects_fixture_path_escape(self) -> None:
        document = json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))
        document["cases"][0]["source_file"] = "../private.mkv"

        with tempfile.TemporaryDirectory() as temporary_directory:
            manifest_path = Path(temporary_directory) / "manifest.json"
            manifest_path.write_text(json.dumps(document), encoding="utf-8")

            with self.assertRaisesRegex(PackagedAacFailure, "unsafe or duplicate"):
                load_fixture_manifest(manifest_path)

    def test_manifest_rejects_false_coverage_label(self) -> None:
        document = json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))
        document["cases"][0]["coverage"].remove("preserve-stereo")
        document["cases"][1]["coverage"].append("preserve-stereo")

        with tempfile.TemporaryDirectory() as temporary_directory:
            manifest_path = Path(temporary_directory) / "manifest.json"
            manifest_path.write_text(json.dumps(document), encoding="utf-8")

            with self.assertRaisesRegex(PackagedAacFailure, "wrong policy"):
                load_fixture_manifest(manifest_path)

    def test_manifest_rejects_unqualified_policy_digest(self) -> None:
        document = json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))
        document["identity_signal"]["policy_sha256"] = "0" * 64

        with tempfile.TemporaryDirectory() as temporary_directory:
            manifest_path = Path(temporary_directory) / "manifest.json"
            manifest_path.write_text(json.dumps(document), encoding="utf-8")

            with self.assertRaisesRegex(PackagedAacFailure, "Apple-qualified"):
                load_fixture_manifest(manifest_path)

    def test_manifest_rejects_qualified_layout_as_unknown_coverage(self) -> None:
        document = json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))
        case = next(case for case in document["cases"] if case["id"] == "fail-unknown-layout")
        case["tracks"][0]["source_layout"] = "quad"
        case["tracks"][0]["channels"] = 4

        with tempfile.TemporaryDirectory() as temporary_directory:
            manifest_path = Path(temporary_directory) / "manifest.json"
            manifest_path.write_text(json.dumps(document), encoding="utf-8")

            with self.assertRaisesRegex(PackagedAacFailure, "unqualified layout"):
                load_fixture_manifest(manifest_path)


class PackagedAacRequestTests(unittest.TestCase):
    def test_request_uses_current_protocol_and_disables_subtitles(self) -> None:
        case = next(case for case in load_fixture_manifest().cases if case.case_id == "remap-5_1-side")

        request = _build_request(case, Path("/tmp/source.mkv"), Path("/tmp/output"), "job")

        self.assertEqual(request["protocol_version"], 10)
        self.assertEqual(
            request["encoding"]["audio"],
            {"mode": "automatic", "bitrate": 384, "preferred_language": "eng"},
        )
        self.assertEqual(request["encoding"]["subtitles"], {"mode": "off", "preferred_language": None})
        self.assertEqual(request["encoding"]["video"]["route_intent"], "automatic")

    def test_worker_execution_preserves_structured_failure(self) -> None:
        process = Mock(returncode=1)
        process.communicate.return_value = (
            json.dumps(worker_event(0, "job.failed", {"error": {"code": "conversion_failed"}})),
            "diagnostic",
        )

        with (
            tempfile.TemporaryDirectory() as temporary_directory,
            patch("scripts.verify_packaged_aac_layouts.subprocess.Popen", return_value=process),
        ):
            execution = _execute_worker(
                Mock(worker=Path("/tmp/worker")),
                {"job_id": "job", "operation": "convert_source"},
                home_directory=Path(temporary_directory) / "home",
            )

        self.assertEqual(execution.returncode, 1)
        self.assertEqual(execution.events[-1]["type"], "job.failed")


class PackagedAacEvidenceTests(unittest.TestCase):
    def test_evidence_renderer_matches_persisted_format(self) -> None:
        evidence = {"cases": [{"passed": True}], "schema_version": 1}

        self.assertEqual(_render_evidence(evidence), json.dumps(evidence, indent=2, sort_keys=True) + "\n")

    def test_remap_case_requires_exact_policy_warning(self) -> None:
        case = next(case for case in load_fixture_manifest().cases if case.case_id == "remap-5_1-side")
        execution = WorkerExecution(
            0,
            (
                worker_event(
                    0,
                    "warning",
                    {
                        "code": "audio_layout_policy_applied",
                        "layout_decisions": [
                            {
                                "action": "remap",
                                "source_layout": "5.1(side)",
                                "target_layout": "5.1",
                                "policy_id": "ch06-5_1_side",
                            }
                        ],
                    },
                ),
            ),
        )

        evidence = _policy_evidence(case, execution)

        self.assertEqual(evidence[0]["policy_id"], "ch06-5_1_side")

    def test_remap_case_rejects_wrong_policy_warning(self) -> None:
        case = next(case for case in load_fixture_manifest().cases if case.case_id == "remap-5_1-side")
        execution = WorkerExecution(
            0,
            (
                worker_event(
                    0,
                    "warning",
                    {
                        "code": "audio_layout_policy_applied",
                        "layout_decisions": [
                            {
                                "action": "preserve",
                                "source_layout": "5.1(side)",
                                "target_layout": "5.1(side)",
                                "policy_id": "wrong",
                            }
                        ],
                    },
                ),
            ),
        )

        with self.assertRaisesRegex(PackagedAacFailure, "wrong AAC decision"):
            _policy_evidence(case, execution)

    def test_failed_case_requires_visible_rejection_without_final_mov(self) -> None:
        case = next(case for case in load_fixture_manifest().cases if case.case_id == "fail-pce-layout")
        execution = WorkerExecution(
            1,
            (
                worker_event(
                    0,
                    "warning",
                    {
                        "code": "audio_layout_policy_rejected",
                        "layout_decisions": [{"reason": "channel_layout_missing"}],
                    },
                ),
                worker_event(1, "job.failed", {"error": {"code": "conversion_failed"}}),
            ),
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            evidence = _failed_case(case, execution, Path(temporary_directory))

        self.assertTrue(evidence["passed"])
        self.assertEqual(evidence["rejection_reason"], "channel_layout_missing")

    def test_output_stream_contract_checks_metadata_and_layout(self) -> None:
        case = next(case for case in load_fixture_manifest().cases if case.case_id == "preserve-multilingual")
        expected = [
            {
                "codec_name": "aac",
                "profile": "LC",
                "sample_rate": 48000,
                "channels": 2,
                "channel_layout": "stereo",
                "aac_channel_configuration": 2,
                "language": "eng",
                "handler_title": "English Stereo",
                "default": True,
            },
            {
                "codec_name": "aac",
                "profile": "LC",
                "sample_rate": 48000,
                "channels": 6,
                "channel_layout": "5.1",
                "aac_channel_configuration": 6,
                "language": "jpn",
                "handler_title": "Japanese 5.1",
                "default": False,
            },
        ]

        _validate_streams(case, expected, expected)

        observed = [dict(stream) for stream in expected]
        observed[1]["default"] = True
        with self.assertRaisesRegex(PackagedAacFailure, "default"):
            _validate_streams(case, observed, expected)

    def test_identity_evidence_rejects_apple_channel_count_mismatch(self) -> None:
        case = next(case for case in load_fixture_manifest().cases if case.case_id == "remap-5_1-side")
        report = Mock(output_streams={"audio": 1})

        with tempfile.TemporaryDirectory() as temporary_directory:
            work_directory = Path(temporary_directory)
            with (
                patch("scripts.verify_packaged_aac_layouts.inspect_apple_media_conversion", return_value=report),
                patch(
                    "scripts.verify_packaged_aac_layouts._probe",
                    return_value={"streams": [{"codec_type": "audio", "channels": 2}]},
                ),
                patch("scripts.verify_packaged_aac_layouts._decode_track") as decode_track,
            ):
                with self.assertRaisesRegex(PackagedAacFailure, "channel count changed"):
                    _identity_evidence(Mock(), case, work_directory / "output.mov", work_directory)

        decode_track.assert_not_called()


class PackagedAacPathTests(unittest.TestCase):
    def test_outputs_must_not_overlap_fixture_or_app(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            app = root / "App.app"
            fixtures = root / "fixtures"
            app.mkdir()
            fixtures.mkdir()

            with self.assertRaisesRegex(PackagedAacFailure, "fixture directory"):
                validate_paths(app, fixtures, fixtures / "evidence.json", root / "artifacts")
            with self.assertRaisesRegex(PackagedAacFailure, "app bundle"):
                validate_paths(app, fixtures, root / "evidence.json", app / "artifacts")

    def test_artifact_directory_must_be_new(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            app = root / "App.app"
            fixtures = root / "fixtures"
            artifacts = root / "artifacts"
            app.mkdir()
            fixtures.mkdir()
            artifacts.mkdir()

            with self.assertRaisesRegex(PackagedAacFailure, "must not already exist"):
                validate_paths(app, fixtures, root / "evidence.json", artifacts)

    def test_artifacts_must_not_be_nested_under_evidence_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            app = root / "App.app"
            fixtures = root / "fixtures"
            output = root / "evidence.json"
            app.mkdir()
            fixtures.mkdir()

            with self.assertRaisesRegex(PackagedAacFailure, "outside the artifact directory"):
                validate_paths(app, fixtures, output, output / "artifacts")


if __name__ == "__main__":
    unittest.main()
