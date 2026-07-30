import copy
import hashlib
import io
import json
import plistlib
import stat
import subprocess
import tempfile
import unittest

from contextlib import nullcontext, redirect_stderr
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bd_to_avp.modules.video_route import DirectMVHEVCCapability, VideoRouteKind
from scripts import qualify_direct_mv_hevc_anchors as anchors


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PLAN_PATH = REPOSITORY_ROOT / "docs/qualification/direct-mv-hevc-anchor-plan-v1.json"


def plan_document() -> dict[str, object]:
    return json.loads(PLAN_PATH.read_text(encoding="utf-8"))


def anchor_plan() -> anchors.AnchorPlan:
    return replace(
        anchors.parse_anchor_plan(plan_document()),
        relative_path=PLAN_PATH.relative_to(REPOSITORY_ROOT).as_posix(),
    )


def artifact_record(
    plan: anchors.AnchorPlan,
    *,
    digest: str = "a" * 64,
    size_bytes: int = 1024,
) -> dict[str, object]:
    return {
        "sha256": digest,
        "size_bytes": size_bytes,
        "duration_seconds": plan.source.duration_seconds,
        "frame_count": plan.source.frame_count,
        "required_spatial_boxes": sorted(anchors.DIRECT_REQUIRED_BOX_TYPES),
        "observed_spatial_boxes": sorted(anchors.DIRECT_REQUIRED_BOX_TYPES | {"ftyp"}),
        "streams": [
            {
                "index": 0,
                "codec_name": "hevc",
                "codec_tag_string": "hvc1",
                "codec_type": "video",
                "profile": "Main",
                "level": 120,
                "width": plan.source.width,
                "height": plan.source.height,
                "channels": None,
                "channel_layout": None,
                "start_time": "0.000000",
                "duration": str(plan.source.duration_seconds),
                "language": None,
                "default": 0,
            },
            {
                "index": 1,
                "codec_name": "aac",
                "codec_tag_string": "mp4a",
                "codec_type": "audio",
                "profile": "LC",
                "level": None,
                "width": None,
                "height": None,
                "channels": 6,
                "channel_layout": "5.1",
                "start_time": "0.000000",
                "duration": str(plan.source.duration_seconds),
                "language": "eng",
                "default": 1,
            },
        ],
        "audio_fingerprints_sha256": ["f" * 64],
        "seek_positions": ["beginning", "middle", "end"],
        "audio_decode_positions": ["beginning", "middle", "end"],
        "apple_passthrough_stream_counts": {
            "source": {"audio": 1, "video": 1},
            "output": {"audio": 1, "video": 1},
        },
        "validation": {flag: True for flag in sorted(anchors.VALIDATION_FLAGS)},
    }


def complete_receipt(
    plan: anchors.AnchorPlan,
    artifacts: dict[str, dict[str, object]] | None = None,
) -> dict[str, object]:
    receipt = anchors._new_receipt(
        plan,
        "1" * 64,
        "2" * 40,
        {},
        {},
        {},
        {},
    )
    for candidate in plan.candidates:
        record = anchors._candidate_receipt(receipt, candidate.candidate_id)
        record["status"] = "complete"
        record["elapsed_seconds"] = 1.0
        record["route"] = {
            "intent": "automatic",
            "selected": "direct_mv_hevc",
            "reason": "full_length_quality_anchor_candidate",
            "rate_control": "quality",
            "quality": candidate.quality,
        }
        record["artifact"] = (artifacts or {}).get(candidate.candidate_id, artifact_record(plan))
    anchors._update_acceptance(receipt, plan)
    return receipt


class AnchorPlanTests(unittest.TestCase):
    def test_committed_plan_keeps_reviewed_anchor_contract(self) -> None:
        plan = anchor_plan()

        self.assertEqual([candidate.quality for candidate in plan.candidates], [0.7, 0.85, 0.4])
        self.assertEqual([candidate.position for candidate in plan.candidates], ["balanced", "high", "low"])
        self.assertEqual(plan.production_options.audio_bitrate_kbps, 384)
        self.assertEqual(plan.execution.reserve_free_bytes, 32 * 1024**3)

    def test_plan_rejects_unscoped_private_environment_variable(self) -> None:
        document = copy.deepcopy(plan_document())
        document["source"]["path_env"] = "HOME"

        with self.assertRaisesRegex(anchors.AnchorQualificationFailure, "BD_TO_AVP_ prefix"):
            anchors.parse_anchor_plan(document)

    def test_plan_rejects_weakened_storage_guards(self) -> None:
        document = copy.deepcopy(plan_document())
        document["execution"]["reserve_free_bytes"] = 1

        with self.assertRaisesRegex(anchors.AnchorQualificationFailure, "storage guards"):
            anchors.parse_anchor_plan(document)


class AnchorPolicyTests(unittest.TestCase):
    def test_candidate_route_overrides_only_reviewed_direct_quality(self) -> None:
        plan = anchor_plan()
        candidate = next(candidate for candidate in plan.candidates if candidate.candidate_id == "q085")
        job = anchors.build_job(plan, Path("source.mkv"), Path("output"), candidate)

        route = anchors.candidate_route(
            job,
            candidate,
            DirectMVHEVCCapability(True, "test", False),
        )

        self.assertIs(route.selected, VideoRouteKind.DIRECT_MV_HEVC)
        self.assertEqual(route.direct_quality, 0.85)
        self.assertIsNone(route.direct_bitrate_mbps)
        self.assertIsNone(route.generated_eye_bitrate_mbps)
        self.assertIsNone(route.generated_merge_quality)

    def test_storage_estimate_includes_two_transient_copies_and_reserve(self) -> None:
        plan = anchor_plan()
        candidate = next(candidate for candidate in plan.candidates if candidate.candidate_id == "q040")

        required = anchors.required_free_bytes(10_000, candidate, plan.execution)

        self.assertEqual(
            required,
            int(10_000 * candidate.maximum_size_ratio * plan.execution.transient_copy_count)
            + plan.execution.reserve_free_bytes
            + 1,
        )

    def test_acceptance_rejects_forged_complete_artifact(self) -> None:
        plan = anchor_plan()
        receipt = complete_receipt(plan)
        first = anchors._candidate_receipt(receipt, "q070")
        first["artifact"]["validation"].pop("apple_passthrough_passed")

        anchors._update_acceptance(receipt, plan)

        self.assertFalse(receipt["acceptance"]["automated_passed"])
        self.assertFalse(receipt["acceptance"]["artifacts_verified"])

    def test_automated_acceptance_never_claims_physical_or_signed_evidence(self) -> None:
        plan = anchor_plan()
        receipt = complete_receipt(plan)

        self.assertTrue(receipt["acceptance"]["automated_passed"])
        self.assertFalse(receipt["acceptance"]["physical_evidence_ready"])
        self.assertFalse(receipt["acceptance"]["signed_package_evidence"])
        self.assertFalse(receipt["acceptance"]["ladder_mapping_selected"])
        self.assertFalse(receipt["acceptance"]["passed"])

    def test_acceptance_rejects_inconsistent_audio_fingerprints(self) -> None:
        plan = anchor_plan()
        receipt = complete_receipt(plan)
        anchors._candidate_receipt(receipt, "q085")["artifact"]["audio_fingerprints_sha256"] = ["e" * 64]

        anchors._update_acceptance(receipt, plan)

        self.assertFalse(receipt["acceptance"]["audio_consistent"])
        self.assertFalse(receipt["acceptance"]["automated_passed"])


class AnchorEvidenceTests(unittest.TestCase):
    def test_privacy_guard_rejects_source_title_and_absolute_path(self) -> None:
        private_source = Path("/Volumes/private/Secret Movie.mkv")

        for document in ({"title": "Secret Movie"}, {"diagnostic": "/Users/example/private"}):
            with self.subTest(document=document):
                with self.assertRaisesRegex(anchors.AnchorQualificationFailure, "private"):
                    anchors._assert_private_values_absent(document, [private_source])

    def test_privacy_guard_does_not_match_generic_prior_receipt_basename(self) -> None:
        anchors._assert_private_values_absent(
            {"text": "Anchor receipt for the release movie."},
            [Path("/Volumes/private/Secret Feature Title.mkv"), Path("/Volumes/private/receipt.json")],
        )

    def test_finalization_is_idempotent_and_repairs_missing_handoff_file(self) -> None:
        plan = anchor_plan()
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            artifacts: dict[str, dict[str, object]] = {}
            for candidate in plan.candidates:
                payload = candidate.candidate_id.encode()
                artifact = root / "artifacts" / candidate.candidate_id / candidate.artifact_filename
                artifact.parent.mkdir(parents=True)
                artifact.write_bytes(payload)
                artifacts[candidate.candidate_id] = artifact_record(
                    plan,
                    digest=hashlib.sha256(payload).hexdigest(),
                    size_bytes=len(payload),
                )
            receipt = complete_receipt(plan, artifacts)
            receipt_path = root / anchors.RECEIPT_NAME
            anchors._atomic_write_json(receipt_path, receipt)
            anchors._atomic_write_json(root / anchors.ROOT_MARKER, {"schema_version": 1})

            anchors._finalize_outputs(plan, root, receipt_path, receipt, (), allow_create=True)
            checklist = root / anchors.CHECKLIST_NAME
            checklist.unlink()
            anchors._finalize_outputs(plan, root, receipt_path, receipt, (), allow_create=True)
            anchors._finalize_outputs(plan, root, receipt_path, receipt, (), allow_create=False)

            self.assertTrue(checklist.is_file())
            self.assertEqual(stat.S_IMODE(receipt_path.stat().st_mode), 0o444)
            checklist.chmod(0o644)
            with self.assertRaisesRegex(anchors.AnchorQualificationFailure, "not frozen"):
                anchors._finalize_outputs(plan, root, receipt_path, receipt, (), allow_create=False)

    def test_completed_candidate_resume_replays_full_validation(self) -> None:
        plan = anchor_plan()
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "work").mkdir()
            artifacts: dict[str, dict[str, object]] = {}
            for candidate in plan.candidates:
                payload = candidate.candidate_id.encode()
                artifact = root / "artifacts" / candidate.candidate_id / candidate.artifact_filename
                artifact.parent.mkdir(parents=True)
                artifact.write_bytes(payload)
                artifacts[candidate.candidate_id] = artifact_record(
                    plan,
                    digest=hashlib.sha256(payload).hexdigest(),
                    size_bytes=len(payload),
                )
            receipt = complete_receipt(plan, artifacts)

            def replay(_tools, artifact, _plan, _validation_root):
                return artifacts[artifact.parent.name]

            with (
                patch.object(anchors, "configured_package_tools", side_effect=lambda _tools: nullcontext()),
                patch.object(anchors, "validate_anchor_artifact", side_effect=replay) as validate,
                patch.object(anchors.shutil, "disk_usage", return_value=SimpleNamespace(free=10**15)),
            ):
                anchors._validate_completed_candidates(receipt, plan, MagicMock(), root)

            self.assertEqual(validate.call_count, 3)
            self.assertTrue(receipt["acceptance"]["automated_passed"])

    def test_completed_candidate_resume_rejects_receipt_validation_mismatch(self) -> None:
        plan = anchor_plan()
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "work").mkdir()
            artifacts: dict[str, dict[str, object]] = {}
            for candidate in plan.candidates:
                payload = candidate.candidate_id.encode()
                artifact = root / "artifacts" / candidate.candidate_id / candidate.artifact_filename
                artifact.parent.mkdir(parents=True)
                artifact.write_bytes(payload)
                artifacts[candidate.candidate_id] = artifact_record(
                    plan,
                    digest=hashlib.sha256(payload).hexdigest(),
                    size_bytes=len(payload),
                )
            receipt = complete_receipt(plan, artifacts)
            anchors._candidate_receipt(receipt, "q070")["artifact"] = {
                "sha256": artifacts["q070"]["sha256"],
                "size_bytes": artifacts["q070"]["size_bytes"],
            }

            with (
                patch.object(anchors, "configured_package_tools", side_effect=lambda _tools: nullcontext()),
                patch.object(
                    anchors,
                    "validate_anchor_artifact",
                    side_effect=lambda _tools, artifact, _plan, _validation_root: artifacts[artifact.parent.name],
                ),
                patch.object(anchors.shutil, "disk_usage", return_value=SimpleNamespace(free=10**15)),
                self.assertRaisesRegex(anchors.AnchorQualificationFailure, "validation evidence changed"),
            ):
                anchors._validate_completed_candidates(receipt, plan, MagicMock(), root)


class PublishedToolBundleTests(unittest.TestCase):
    def test_published_app_is_bound_to_exact_source_commit(self) -> None:
        source_commit = "a" * 40
        with tempfile.TemporaryDirectory() as temporary_directory:
            build_root = Path(temporary_directory) / source_commit
            app = build_root / "3D Blu-ray to Vision Pro.app"
            info_path = app / anchors.INFO_RELATIVE_PATH
            info_path.parent.mkdir(parents=True)
            with info_path.open("wb") as info_file:
                plistlib.dump(
                    {
                        "CFBundleIdentifier": "com.example.bd-to-avp",
                        "CFBundleShortVersionString": "0.3.0b10",
                        "CFBundleVersion": "10",
                    },
                    info_file,
                )
            package_bin = app / anchors.PACKAGE_BIN_RELATIVE_PATH
            package_bin.mkdir(parents=True)
            for name in ("mv-hevc-encoder", "edge264_test", "ffmpeg", "ffprobe", "MP4Box", "spatial-media-kit-tool"):
                tool = package_bin / name
                tool.write_bytes(b"tool")
                tool.chmod(0o755)
            tree_sha256 = "c" * 64
            (build_root / anchors.BUILD_METADATA_NAME).write_text(
                json.dumps(
                    {
                        "app_tree_sha256": tree_sha256,
                        "base_main_commit": "b" * 40,
                        "build_version": "10",
                        "built_at_utc": "2026-07-30T00:00:00Z",
                        "release_status": "not a production-signed release",
                        "short_version": "0.3.0b10",
                        "signing": "ad-hoc local",
                        "source_commit": source_commit,
                        "stable_path": "/not-recorded",
                    }
                ),
                encoding="utf-8",
            )

            with (
                patch.object(anchors, "app_tree_sha256", return_value=tree_sha256),
                patch.object(anchors, "_code_signature_class", return_value="ad_hoc"),
            ):
                tools = anchors.inspect_packaged_app(app, source_commit)
                with self.assertRaisesRegex(anchors.AnchorQualificationFailure, "recorded source commit"):
                    anchors.inspect_packaged_app(app, "d" * 40)

            self.assertEqual(tools.source_commit, source_commit)
            self.assertEqual(tools.code_signature, "ad_hoc")

    def test_capability_probe_rejects_unknown_fields(self) -> None:
        probe = {
            "schema_version": 1,
            "stereo_mv_hevc_encode_supported": True,
            "metalfx_2x_mv_hevc_supported": False,
            "metalfx_spatial_scaling_supported": False,
            "pixel_transfer_2x_mv_hevc_supported": True,
            "private_path": "/private/tool",
        }

        with (
            patch.object(anchors, "_probe_json", return_value=probe),
            self.assertRaisesRegex(anchors.AnchorQualificationFailure, "schema is unexpected"),
        ):
            anchors.probe_packaged_capability(SimpleNamespace(encoder=Path("encoder")))

    def test_spatial_box_probe_uses_recorded_packaged_mp4box(self) -> None:
        completed = subprocess.CompletedProcess([], 0, stdout='<Box Type="eyes"/>', stderr="")
        with patch.object(anchors.subprocess, "run", return_value=completed) as run:
            observed = anchors._packaged_box_types(Path("/bundle/MP4Box"), Path("movie.mov"))

        self.assertEqual(observed, {"eyes"})
        self.assertEqual(run.call_args.args[0][0], Path("/bundle/MP4Box"))


class AnchorOrchestrationTests(unittest.TestCase):
    def test_run_candidate_records_route_before_moving_completed_artifact(self) -> None:
        plan = anchor_plan()
        candidate = plan.candidates[0]
        receipt = anchors._new_receipt(plan, "1" * 64, "2" * 40, {}, {}, {}, {})
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "work").mkdir()
            (root / "artifacts").mkdir()
            receipt_path = root / anchors.RECEIPT_NAME
            anchors._atomic_write_json(receipt_path, receipt)
            artifact_path = root / "artifacts" / candidate.candidate_id / candidate.artifact_filename
            writes: list[tuple[str, object, object, bool]] = []
            original_write = anchors._atomic_write_json

            def write_receipt(path, document):
                record = anchors._candidate_receipt(document, candidate.candidate_id)
                writes.append(
                    (
                        str(record["status"]),
                        copy.deepcopy(record.get("route")),
                        record.get("elapsed_seconds"),
                        artifact_path.exists(),
                    )
                )
                original_write(path, document)

            def start_process():
                completed = root / "work" / candidate.candidate_id / "output" / "completed.mov"
                completed.write_bytes(b"encoded")
                return completed

            with (
                patch.object(anchors.shutil, "disk_usage", return_value=SimpleNamespace(free=10**15)),
                patch.object(anchors, "configured_package_tools", side_effect=lambda _tools: nullcontext()),
                patch.object(anchors, "configured_conversion", side_effect=lambda _job, _source: nullcontext()),
                patch.object(anchors, "apply_video_route_to_config") as apply_route,
                patch.object(anchors, "start_process", side_effect=lambda **_kwargs: start_process()),
                patch.object(anchors, "validate_anchor_artifact", return_value=artifact_record(plan)),
                patch.object(anchors, "_atomic_write_json", side_effect=write_receipt),
            ):
                anchors._run_candidate(
                    receipt,
                    receipt_path,
                    plan,
                    candidate,
                    MagicMock(),
                    DirectMVHEVCCapability(True, "test", False),
                    Path("source.mkv"),
                    root,
                )

            pre_move = [entry for entry in writes if entry[0] == "encoding" and entry[1] is not None]
            self.assertEqual(len(pre_move), 1)
            self.assertIsInstance(pre_move[0][2], float)
            self.assertFalse(pre_move[0][3])
            self.assertTrue(artifact_path.is_file())
            apply_route.assert_called_once()

    def test_run_candidate_resumes_moved_artifact_with_recorded_encode_metadata(self) -> None:
        plan = anchor_plan()
        candidate = plan.candidates[0]
        receipt = anchors._new_receipt(plan, "1" * 64, "2" * 40, {}, {}, {}, {})
        record = anchors._candidate_receipt(receipt, candidate.candidate_id)
        record["status"] = "encoding"
        record["elapsed_seconds"] = 42.0
        record["route"] = {
            "intent": "automatic",
            "selected": "direct_mv_hevc",
            "reason": "full_length_quality_anchor_candidate",
            "rate_control": "quality",
            "quality": candidate.quality,
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "work").mkdir()
            artifact = root / "artifacts" / candidate.candidate_id / candidate.artifact_filename
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b"encoded")
            receipt_path = root / anchors.RECEIPT_NAME
            anchors._atomic_write_json(receipt_path, receipt)

            with (
                patch.object(anchors, "configured_package_tools", side_effect=lambda _tools: nullcontext()),
                patch.object(anchors, "validate_anchor_artifact", return_value=artifact_record(plan)),
                patch.object(anchors, "start_process") as start_process,
            ):
                anchors._run_candidate(
                    receipt,
                    receipt_path,
                    plan,
                    candidate,
                    MagicMock(),
                    DirectMVHEVCCapability(True, "test", False),
                    Path("source.mkv"),
                    root,
                )

            start_process.assert_not_called()
            self.assertEqual(record["status"], "complete")

    def test_run_candidate_preserves_half_state_without_encode_metadata(self) -> None:
        plan = anchor_plan()
        candidate = plan.candidates[0]
        receipt = anchors._new_receipt(plan, "1" * 64, "2" * 40, {}, {}, {}, {})
        record = anchors._candidate_receipt(receipt, candidate.candidate_id)
        record["status"] = "encoding"
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "work").mkdir()
            artifact = root / "artifacts" / candidate.candidate_id / candidate.artifact_filename
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b"encoded")
            receipt_path = root / anchors.RECEIPT_NAME
            anchors._atomic_write_json(receipt_path, receipt)

            with self.assertRaisesRegex(anchors.AnchorQualificationFailure, "preserving it"):
                anchors._run_candidate(
                    receipt,
                    receipt_path,
                    plan,
                    candidate,
                    MagicMock(),
                    DirectMVHEVCCapability(True, "test", False),
                    Path("source.mkv"),
                    root,
                )

            self.assertTrue(artifact.is_file())

    def test_run_candidate_rechecks_balanced_capacity_immediately(self) -> None:
        plan = anchor_plan()
        candidate = plan.candidates[0]
        receipt = anchors._new_receipt(plan, "1" * 64, "2" * 40, {}, {}, {}, {})
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "work").mkdir()
            (root / "artifacts").mkdir()
            receipt_path = root / anchors.RECEIPT_NAME
            anchors._atomic_write_json(receipt_path, receipt)

            with (
                patch.object(anchors.shutil, "disk_usage", return_value=SimpleNamespace(free=1)),
                patch.object(anchors, "start_process") as start_process,
                self.assertRaisesRegex(anchors.AnchorQualificationFailure, "Insufficient free space"),
            ):
                anchors._run_candidate(
                    receipt,
                    receipt_path,
                    plan,
                    candidate,
                    MagicMock(),
                    DirectMVHEVCCapability(True, "test", False),
                    Path("source.mkv"),
                    root,
                )

            start_process.assert_not_called()


class AnchorPathAndCliTests(unittest.TestCase):
    def test_output_root_rejects_overlap_with_private_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_root = Path(temporary_directory) / "anchors"
            private_source = output_root / "source.mkv"
            with (
                patch.object(anchors, "_has_symlink_component", return_value=False),
                self.assertRaisesRegex(anchors.AnchorQualificationFailure, "overlaps"),
            ):
                anchors._prepare_anchor_root(
                    output_root,
                    {"schema_version": 1},
                    forbidden_paths=(private_source,),
                    allow_create=True,
                )

    def test_symlink_component_detection_checks_parent_chain(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            real = root / "real"
            real.mkdir()
            link = root / "link"
            link.symlink_to(real, target_is_directory=True)

            self.assertTrue(anchors._has_symlink_component(link / "anchors"))

    def test_cli_suppresses_unexpected_private_exception_details(self) -> None:
        stderr = io.StringIO()
        with (
            patch.object(anchors, "create_anchors", side_effect=OSError("/Volumes/private/Secret Movie.mkv")),
            redirect_stderr(stderr),
        ):
            exit_code = anchors.main(["create", "--app", "/tmp/app", "--output-root", "/tmp/output"])

        self.assertEqual(exit_code, 2)
        self.assertIn("private details were suppressed", stderr.getvalue())
        self.assertNotIn("Secret Movie", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
