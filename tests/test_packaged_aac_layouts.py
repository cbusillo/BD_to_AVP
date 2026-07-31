import array
import hashlib
import json
import stat
import subprocess
import tempfile
import unittest

from pathlib import Path
from unittest.mock import Mock, patch

from bd_to_avp.modules.aac_layout_policy import LAYOUT_CHANNELS
from scripts.verify_apple_media import AppleMediaFailure
from scripts.verify_packaged_aac_layouts import (
    DEFAULT_MANIFEST,
    EVIDENCE_SCHEMA_VERSION,
    FIXTURE_RECEIPT_SCHEMA_VERSION,
    REQUIRED_COVERAGE,
    FixtureReceipt,
    PackagedAacFailure,
    WorkerExecution,
    _audio_streams,
    _build_request,
    _direct_video_route_evidence,
    _execute_worker,
    _failed_case,
    _identity_evidence,
    _inspect_apple_media_conversion,
    _load_fixture_receipt,
    _managed_artifact_directory,
    _policy_evidence,
    _render_evidence,
    _run,
    _validate_streams,
    _verified_fixture_identity,
    _verified_fixture_sources,
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


def fixture_receipt_document() -> dict[str, object]:
    manifest = load_fixture_manifest()
    return {
        "schema_version": FIXTURE_RECEIPT_SCHEMA_VERSION,
        "generated_at_utc": "2026-07-28T00:00:00+00:00",
        "fixture_set_id": manifest.fixture_set_id,
        "manifest_sha256": hashlib.sha256(DEFAULT_MANIFEST.read_bytes()).hexdigest(),
        "source": {
            "sha256": "b" * 64,
            "size_bytes": 1,
            "video": {
                "codec_name": "h264",
                "duration_seconds": 4.213,
                "direct_mvc_route_proven": False,
            },
        },
        "tools": {
            "ffmpeg": {"sha256": "c" * 64, "version": "ffmpeg test"},
            "ffprobe": {"sha256": "d" * 64, "version": "ffprobe test"},
        },
        "fixtures": [
            {
                "id": case.case_id,
                "file": case.source_file,
                "sha256": "a" * 64,
                "size_bytes": 1,
                "audio_streams": [],
            }
            for case in manifest.cases
        ],
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

    def test_manifest_rejects_failure_coverage_on_success_case(self) -> None:
        document = json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))
        document["cases"][0]["coverage"].append("fail-missing-layout")
        failure_case = next(case for case in document["cases"] if case["id"] == "fail-missing-layout")
        failure_case["coverage"] = ["handler-titles"]

        with tempfile.TemporaryDirectory() as temporary_directory:
            manifest_path = Path(temporary_directory) / "manifest.json"
            manifest_path.write_text(json.dumps(document), encoding="utf-8")

            with self.assertRaisesRegex(PackagedAacFailure, "must not claim failure coverage"):
                load_fixture_manifest(manifest_path)

    def test_manifest_rejects_failure_coverage_reason_mismatch(self) -> None:
        document = json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))
        failure_case = next(case for case in document["cases"] if case["id"] == "fail-unknown-layout")
        failure_case["expected_rejection"] = "channel_layout_missing"

        with tempfile.TemporaryDirectory() as temporary_directory:
            manifest_path = Path(temporary_directory) / "manifest.json"
            manifest_path.write_text(json.dumps(document), encoding="utf-8")

            with self.assertRaisesRegex(PackagedAacFailure, "does not match its failure coverage"):
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


class PackagedAacReceiptTests(unittest.TestCase):
    def test_receipt_binds_checked_manifest_and_all_cases(self) -> None:
        manifest = load_fixture_manifest()
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            (root / "fixture-receipt.json").write_text(
                json.dumps(fixture_receipt_document()),
                encoding="utf-8",
            )

            receipt = _load_fixture_receipt(root, manifest, DEFAULT_MANIFEST)

        self.assertEqual(set(receipt.entries), {case.case_id for case in manifest.cases})
        self.assertEqual(len(receipt.sha256), 64)

    def test_receipt_requires_generator_source_and_tool_provenance(self) -> None:
        manifest = load_fixture_manifest()
        for missing_field in ("source", "tools"):
            document = fixture_receipt_document()
            del document[missing_field]
            with tempfile.TemporaryDirectory() as temporary_directory:
                root = Path(temporary_directory).resolve()
                (root / "fixture-receipt.json").write_text(json.dumps(document), encoding="utf-8")

                with self.subTest(missing_field=missing_field):
                    with self.assertRaisesRegex(PackagedAacFailure, missing_field):
                        _load_fixture_receipt(root, manifest, DEFAULT_MANIFEST)

    def test_receipt_rejects_stale_manifest_digest(self) -> None:
        manifest = load_fixture_manifest()
        document = fixture_receipt_document()
        document["manifest_sha256"] = "0" * 64
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            (root / "fixture-receipt.json").write_text(json.dumps(document), encoding="utf-8")

            with self.assertRaisesRegex(PackagedAacFailure, "checked manifest"):
                _load_fixture_receipt(root, manifest, DEFAULT_MANIFEST)

    def test_fixture_identity_rejects_tampered_bytes(self) -> None:
        case = load_fixture_manifest().cases[0]
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / case.source_file
            source.write_bytes(b"fixture")
            entry = {
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "size_bytes": source.stat().st_size,
            }
            self.assertEqual(_verified_fixture_identity(case, source, entry), (entry["sha256"], 7))
            source.write_bytes(b"tampered")

            with self.assertRaisesRegex(PackagedAacFailure, "generator receipt"):
                _verified_fixture_identity(case, source, entry)

    def test_all_fixture_hashes_are_checked_before_execution(self) -> None:
        manifest = load_fixture_manifest()
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            entries: dict[str, dict[str, object]] = {}
            for case in manifest.cases:
                source = root / case.source_file
                source.write_bytes(case.case_id.encode("utf-8"))
                entries[case.case_id] = {
                    "file": case.source_file,
                    "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                    "size_bytes": source.stat().st_size,
                }
            (root / manifest.cases[-1].source_file).write_bytes(b"tampered later fixture")
            receipt = FixtureReceipt("a" * 64, entries)

            with self.assertRaisesRegex(PackagedAacFailure, "generator receipt"):
                _verified_fixture_sources(root, manifest, receipt)

    def test_evidence_schema_versions_the_gate_split(self) -> None:
        self.assertEqual(EVIDENCE_SCHEMA_VERSION, 2)


class PackagedAacSafetyTests(unittest.TestCase):
    def test_command_failure_redacts_private_paths(self) -> None:
        private_source = Path("/Users/example/private/fixture.mkv")
        error = subprocess.CalledProcessError(
            1,
            ["ffprobe", private_source.as_posix()],
            stderr=f"{private_source}: Invalid data found",
        )

        with patch("scripts.verify_packaged_aac_layouts.subprocess.run", side_effect=error):
            with self.assertRaises(PackagedAacFailure) as context:
                _run([Path("/private/tools/ffprobe"), private_source])

        self.assertNotIn(private_source.as_posix(), str(context.exception))
        self.assertIn("<redacted-path>", str(context.exception))

    def test_apple_media_failure_redacts_private_paths(self) -> None:
        source = Path("/Users/example/private/output.mov")
        converted = Path("/Users/example/private/apple.mov")
        with patch(
            "scripts.verify_packaged_aac_layouts.inspect_apple_media_conversion",
            side_effect=AppleMediaFailure(f"Could not open {source}"),
        ):
            with self.assertRaises(PackagedAacFailure) as context:
                _inspect_apple_media_conversion(source, converted)

        self.assertNotIn(source.as_posix(), str(context.exception))
        self.assertIn("<redacted-path>", str(context.exception))

    def test_artifact_directory_is_private_and_cleans_interruptions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            artifacts = Path(temporary_directory) / "artifacts"
            interrupt = Mock(side_effect=KeyboardInterrupt)
            with self.assertRaises(KeyboardInterrupt):
                with _managed_artifact_directory(artifacts):
                    self.assertEqual(stat.S_IMODE(artifacts.stat().st_mode), 0o700)
                    (artifacts / "private.mov").write_bytes(b"private")
                    interrupt()

            interrupt.assert_called_once_with()
            self.assertFalse(artifacts.exists())


class PackagedAacRequestTests(unittest.TestCase):
    def test_request_uses_current_protocol_and_disables_subtitles(self) -> None:
        case = next(case for case in load_fixture_manifest().cases if case.case_id == "remap-5_1-side")

        request = _build_request(case, Path("/tmp/source.mkv"), Path("/tmp/output"), "job")

        self.assertEqual(request["protocol_version"], 11)
        self.assertEqual(
            request["encoding"]["audio"],
            {"mode": "automatic", "bitrate": 384, "preferred_language": "eng"},
        )
        self.assertEqual(request["encoding"]["subtitles"], {"mode": "off", "preferred_language": None})
        self.assertEqual(request["encoding"]["video"]["route_intent"], "automatic")
        self.assertEqual(request["encoding"]["video"]["quality_intent"], {"mode": "custom"})

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

    def test_worker_interrupt_terminates_process_group(self) -> None:
        process = Mock(returncode=None)
        process.communicate.side_effect = KeyboardInterrupt

        with (
            tempfile.TemporaryDirectory() as temporary_directory,
            patch("scripts.verify_packaged_aac_layouts.subprocess.Popen", return_value=process),
            patch("scripts.verify_packaged_aac_layouts._terminate_worker_process") as terminate,
        ):
            with self.assertRaises(KeyboardInterrupt):
                _execute_worker(
                    Mock(worker=Path("/tmp/worker")),
                    {"job_id": "job", "operation": "convert_source"},
                    home_directory=Path(temporary_directory) / "home",
                )

        terminate.assert_called_once_with(process)


class PackagedAacEvidenceTests(unittest.TestCase):
    def test_completed_case_requires_direct_mvc_route(self) -> None:
        case = next(case for case in load_fixture_manifest().cases if case.case_id == "remap-5_1-side")

        with self.assertRaisesRegex(PackagedAacFailure, "direct MVC package route"):
            _direct_video_route_evidence(case, {"video_route": {"selected": "generated_mv_hevc"}})

        evidence = _direct_video_route_evidence(
            case,
            {
                "video_route": {
                    "intent": "automatic",
                    "selected": "direct_mv_hevc",
                    "reason": "direct_eligible",
                    "rate_control": "quality",
                    "quality": 0.7,
                }
            },
        )
        self.assertEqual(evidence["selected"], "direct_mv_hevc")

    def test_audio_streams_parse_configuration_only_for_aac(self) -> None:
        probe = {
            "streams": [
                {
                    "codec_type": "audio",
                    "codec_name": "flac",
                    "channels": 6,
                    "channel_layout": "5.1(side)",
                    "extradata": "\n00000000: 1200 1200 0000 2000\n",
                },
                {
                    "codec_type": "audio",
                    "codec_name": "aac",
                    "channels": 2,
                    "channel_layout": "stereo",
                    "extradata": "\n00000000: 1190 56e5 00\n",
                },
            ]
        }

        streams = _audio_streams(probe)

        self.assertIsNone(streams[0]["aac_channel_configuration"])
        self.assertEqual(streams[1]["aac_channel_configuration"], 2)

    def test_evidence_renderer_matches_persisted_format(self) -> None:
        evidence = {"cases": [{"passed": True}], "schema_version": EVIDENCE_SCHEMA_VERSION}

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
        report = Mock(source_streams={"audio": 1}, output_streams={"audio": 1})

        with tempfile.TemporaryDirectory() as temporary_directory:
            work_directory = Path(temporary_directory)
            with (
                patch("scripts.verify_packaged_aac_layouts._run"),
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

    def test_identity_evidence_converts_each_track_separately(self) -> None:
        case = next(case for case in load_fixture_manifest().cases if case.case_id == "preserve-multilingual")
        report = Mock(source_streams={"audio": 1}, output_streams={"audio": 1})
        probes = [
            {"streams": [{"codec_type": "audio", "channels": 2}]},
            {"streams": [{"codec_type": "audio", "channels": 6}]},
        ]

        with tempfile.TemporaryDirectory() as temporary_directory:
            work_directory = Path(temporary_directory)
            app = Mock(ffmpeg=Path("/app/ffmpeg"))
            with (
                patch("scripts.verify_packaged_aac_layouts._run") as run,
                patch(
                    "scripts.verify_packaged_aac_layouts.inspect_apple_media_conversion",
                    return_value=report,
                ) as inspect,
                patch("scripts.verify_packaged_aac_layouts._probe", side_effect=probes),
                patch(
                    "scripts.verify_packaged_aac_layouts._decode_track",
                    return_value=array.array("f"),
                ) as decode,
                patch(
                    "scripts.verify_packaged_aac_layouts.analyze_identity_samples",
                    return_value={"status": "passed"},
                ),
            ):
                evidence = _identity_evidence(app, case, work_directory / "output.mov", work_directory)

        self.assertEqual([item["track_index"] for item in evidence], [0, 1])
        self.assertEqual(run.call_count, 2)
        self.assertIn("0:a:0", run.call_args_list[0].args[0])
        self.assertIn("0:a:1", run.call_args_list[1].args[0])
        self.assertEqual(inspect.call_count, 2)
        self.assertEqual(
            [call.args[0].name for call in inspect.call_args_list],
            ["apple-input-0.mov", "apple-input-1.mov"],
        )
        self.assertEqual(
            [call.args[1].name for call in inspect.call_args_list],
            ["apple-lpcm-0.mov", "apple-lpcm-1.mov"],
        )
        self.assertEqual([call.args[2] for call in decode.call_args_list], [0, 0])


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
