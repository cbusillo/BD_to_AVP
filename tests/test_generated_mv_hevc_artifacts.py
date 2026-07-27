import tempfile
import unittest

from pathlib import Path
from unittest.mock import Mock, patch

from bd_to_avp.modules import video
from bd_to_avp.modules.config import Stage
from bd_to_avp.modules.disc import DiscInfo
from bd_to_avp.process_runner import ProcessCancelled, ProcessOutputSnapshot, ProcessResult


VALID_BOX_XML = "".join(f'<Box Type="{box_type}"/>' for box_type in sorted(video.GENERATED_MV_HEVC_REQUIRED_BOX_TYPES))


def process_result(stdout: str = "", *, tool_id: str = "tool") -> ProcessResult:
    stdout_bytes = stdout.encode("utf-8")
    stdout_snapshot = ProcessOutputSnapshot(
        stdout_bytes,
        stdout_bytes,
        len(stdout_bytes),
        len(stdout_bytes),
        0,
        0,
    )
    empty_snapshot = ProcessOutputSnapshot(b"", b"", 0, 0, 0, 0)
    return ProcessResult(
        tool_run_id=f"{tool_id}-run",
        returncode=0,
        elapsed_ms=1,
        stdout=stdout_snapshot,
        stderr=empty_snapshot,
    )


def valid_probe(
    *,
    duration: float = 100.0,
    width: int = 1920,
    height: int = 1080,
    codec_tag: str = "hvc1",
    average_frame_rate: str = "24000/1001",
) -> dict[str, object]:
    return {
        "streams": [
            {
                "codec_name": "hevc",
                "codec_tag_string": codec_tag,
                "width": width,
                "height": height,
                "duration": str(duration),
                "avg_frame_rate": average_frame_rate,
            }
        ],
        "format": {"duration": str(duration)},
    }


class GeneratedMVHEVCArtifactTests(unittest.TestCase):
    def test_helper_success_without_output_removes_stale_canonical_and_partial(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_path = Path(temporary_directory) / "Movie_MV-HEVC.mov"
            partial_path = video.generated_mv_hevc_partial_path(output_path)
            output_path.write_bytes(b"stale canonical")
            partial_path.write_bytes(b"stale partial")

            with (
                patch.object(video, "run_process_capture", return_value=process_result()),
                self.assertRaises(video.GeneratedMVHEVCArtifactError) as context,
            ):
                video.combine_to_mv_hevc(Path("left.mov"), Path("right.mov"), output_path, 8)

            self.assertIn("did not produce a readable file", str(context.exception))
            self.assertFalse(output_path.exists())
            self.assertFalse(partial_path.exists())

    def test_valid_output_is_published_only_after_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_path = Path(temporary_directory) / "Movie_MV-HEVC.mov"
            partial_path = video.generated_mv_hevc_partial_path(output_path)
            output_path.write_bytes(b"stale canonical")
            partial_path.write_bytes(b"stale partial")
            merge_output_paths: list[Path] = []

            def run_tool(command, _name, *, tool_id, **_kwargs):
                if tool_id == "spatial_media_kit_tool":
                    helper_output = Path(command[command.index("--output-file") + 1])
                    merge_output_paths.append(helper_output)
                    helper_output.write_bytes(b"validated artifact")
                    self.assertFalse(output_path.exists())
                    return process_result(tool_id=tool_id)
                self.assertEqual(tool_id, "mp4box")
                self.assertEqual(Path(command[2]), partial_path)
                self.assertFalse(output_path.exists())
                return process_result(VALID_BOX_XML, tool_id=tool_id)

            with (
                patch.object(video, "run_process_capture", side_effect=run_tool),
                patch.object(video, "run_ffprobe", return_value=valid_probe()),
            ):
                video.combine_to_mv_hevc(
                    Path("left.mov"),
                    Path("right.mov"),
                    output_path,
                    8,
                    expected_dimensions=(1920, 1080),
                    expected_duration_seconds=100.0,
                )

            self.assertEqual(merge_output_paths, [partial_path])
            self.assertEqual(output_path.read_bytes(), b"validated artifact")
            self.assertFalse(partial_path.exists())

    def test_unparseable_output_is_not_published(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_path = Path(temporary_directory) / "Movie_MV-HEVC.mov"
            partial_path = video.generated_mv_hevc_partial_path(output_path)

            def run_merge(command, _name, *, tool_id, **_kwargs):
                self.assertEqual(tool_id, "spatial_media_kit_tool")
                Path(command[command.index("--output-file") + 1]).write_bytes(b"truncated")
                return process_result(tool_id=tool_id)

            with (
                patch.object(video, "run_process_capture", side_effect=run_merge),
                patch.object(video, "run_ffprobe", side_effect=RuntimeError("invalid data")),
                self.assertRaises(video.GeneratedMVHEVCArtifactError) as context,
            ):
                video.combine_to_mv_hevc(Path("left.mov"), Path("right.mov"), output_path, 8)

            self.assertIn("FFprobe could not parse", str(context.exception))
            self.assertFalse(output_path.exists())
            self.assertFalse(partial_path.exists())

    def test_duration_mismatch_is_rejected_before_box_inspection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            artifact_path = Path(temporary_directory) / "Movie_MV-HEVC.mov"
            artifact_path.write_bytes(b"artifact")
            with (
                patch.object(video, "run_ffprobe", return_value=valid_probe(duration=80.0)),
                patch.object(video, "run_process_capture") as inspect_boxes,
                self.assertRaises(video.GeneratedMVHEVCArtifactError) as context,
            ):
                video.validate_generated_mv_hevc_artifact(
                    artifact_path,
                    expected_dimensions=(1920, 1080),
                    expected_duration_seconds=100.0,
                )

            self.assertIn("expected 100.000s", str(context.exception))
            inspect_boxes.assert_not_called()

    def test_dimension_mismatch_is_rejected_before_box_inspection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            artifact_path = Path(temporary_directory) / "Movie_MV-HEVC.mov"
            artifact_path.write_bytes(b"artifact")
            with (
                patch.object(video, "run_ffprobe", return_value=valid_probe(width=1280, height=720)),
                patch.object(video, "run_process_capture") as inspect_boxes,
                self.assertRaises(video.GeneratedMVHEVCArtifactError) as context,
            ):
                video.validate_generated_mv_hevc_artifact(
                    artifact_path,
                    expected_dimensions=(1920, 1080),
                    expected_duration_seconds=100.0,
                )

            self.assertIn("expected 1920x1080", str(context.exception))
            inspect_boxes.assert_not_called()

    def test_missing_required_boxes_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            artifact_path = Path(temporary_directory) / "Movie_MV-HEVC.mov"
            artifact_path.write_bytes(b"artifact")
            incomplete_boxes = VALID_BOX_XML.replace('<Box Type="lhvC"/>', "")
            with (
                patch.object(video, "run_ffprobe", return_value=valid_probe()),
                patch.object(video, "run_process_capture", return_value=process_result(incomplete_boxes)),
                self.assertRaises(video.GeneratedMVHEVCArtifactError) as context,
            ):
                video.validate_generated_mv_hevc_artifact(
                    artifact_path,
                    expected_dimensions=(1920, 1080),
                    expected_duration_seconds=100.0,
                )

            self.assertIn("lhvC", str(context.exception))

    def test_non_hvc1_track_is_rejected_for_apple_playback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            artifact_path = Path(temporary_directory) / "Movie_MV-HEVC.mov"
            artifact_path.write_bytes(b"artifact")
            with (
                patch.object(video, "run_ffprobe", return_value=valid_probe(codec_tag="hev1")),
                patch.object(video, "run_process_capture") as inspect_boxes,
                self.assertRaises(video.GeneratedMVHEVCArtifactError) as context,
            ):
                video.validate_generated_mv_hevc_artifact(
                    artifact_path,
                    expected_dimensions=(1920, 1080),
                    expected_duration_seconds=100.0,
                )

            self.assertIn("not tagged hvc1", str(context.exception))
            inspect_boxes.assert_not_called()

    def test_cancellation_removes_partial_without_publishing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_path = Path(temporary_directory) / "Movie_MV-HEVC.mov"
            partial_path = video.generated_mv_hevc_partial_path(output_path)

            def run_merge(command, _name, *, tool_id, **_kwargs):
                Path(command[command.index("--output-file") + 1]).write_bytes(b"partial")
                return process_result(tool_id=tool_id)

            cancellation = ProcessCancelled("cancelled")
            with (
                patch.object(video, "run_process_capture", side_effect=run_merge),
                patch.object(video, "run_ffprobe", side_effect=cancellation),
                self.assertRaises(ProcessCancelled) as context,
            ):
                video.combine_to_mv_hevc(Path("left.mov"), Path("right.mov"), output_path, 8)

            self.assertIs(context.exception, cancellation)
            self.assertFalse(output_path.exists())
            self.assertFalse(partial_path.exists())

    def test_replace_failure_removes_validated_partial(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_path = Path(temporary_directory) / "Movie_MV-HEVC.mov"
            partial_path = video.generated_mv_hevc_partial_path(output_path)

            def run_tool(command, _name, *, tool_id, **_kwargs):
                if tool_id == "spatial_media_kit_tool":
                    Path(command[command.index("--output-file") + 1]).write_bytes(b"validated artifact")
                    return process_result(tool_id=tool_id)
                return process_result(VALID_BOX_XML, tool_id=tool_id)

            with (
                patch.object(video, "run_process_capture", side_effect=run_tool),
                patch.object(video, "run_ffprobe", return_value=valid_probe()),
                patch.object(Path, "replace", side_effect=OSError("rename failed")),
                self.assertRaises(video.GeneratedMVHEVCArtifactError) as context,
            ):
                video.combine_to_mv_hevc(Path("left.mov"), Path("right.mov"), output_path, 8)

            self.assertIn("could not be published atomically", str(context.exception))
            self.assertFalse(output_path.exists())
            self.assertFalse(partial_path.exists())

    def test_invalid_resume_keeps_left_and_right_eye_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_folder = Path(temporary_directory)
            left_path = output_folder / "Movie_left_movie.mov"
            right_path = output_folder / "Movie_right_movie.mov"
            left_path.write_bytes(b"left")
            right_path.write_bytes(b"right")
            (output_folder / "Movie_MV-HEVC.mov").write_bytes(b"")

            with (
                patch.object(video.config, "start_stage", Stage.UPSCALE_VIDEO),
                patch.object(video.config, "keep_files", False),
                patch.object(video.config, "resolution", ""),
                patch.object(video.config, "preview_range", None),
                self.assertRaises(video.GeneratedMVHEVCArtifactError),
            ):
                video.create_mv_hevc_file(
                    left_path,
                    right_path,
                    output_folder,
                    DiscInfo(name="Movie", duration_seconds=100.0),
                )

            self.assertTrue(left_path.exists())
            self.assertTrue(right_path.exists())

    def test_valid_resume_revalidates_before_cleaning_eye_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_folder = Path(temporary_directory)
            left_path = output_folder / "Movie_left_movie.mov"
            right_path = output_folder / "Movie_right_movie.mov"
            output_path = output_folder / "Movie_MV-HEVC.mov"
            left_path.write_bytes(b"left")
            right_path.write_bytes(b"right")
            output_path.write_bytes(b"valid")

            with (
                patch.object(video.config, "start_stage", Stage.UPSCALE_VIDEO),
                patch.object(video.config, "keep_files", False),
                patch.object(video.config, "resolution", ""),
                patch.object(video.config, "preview_range", None),
                patch.object(video, "run_ffprobe", return_value=valid_probe()),
                patch.object(video, "run_process_capture", return_value=process_result(VALID_BOX_XML)),
            ):
                result = video.create_mv_hevc_file(
                    left_path,
                    right_path,
                    output_folder,
                    DiscInfo(name="Movie", duration_seconds=100.0),
                )

            self.assertEqual(result, output_path)
            self.assertTrue(output_path.exists())
            self.assertFalse(left_path.exists())
            self.assertFalse(right_path.exists())

    def test_stage_five_expectations_use_the_encoded_left_eye_duration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            left_path = temporary_path / "Movie_left_movie.mov"
            output_path = temporary_path / "Movie_MV-HEVC.mov"
            left_path.write_bytes(b"left")
            with (
                patch.object(video.config, "start_stage", Stage.COMBINE_TO_MV_HEVC),
                patch.object(video.config, "frame_rate", "30"),
                patch.object(video.config, "resolution", ""),
                patch.object(video.config, "preview_range", None),
                patch.object(
                    video,
                    "run_ffprobe",
                    return_value=valid_probe(duration=80.0, width=1280, height=720),
                ),
            ):
                dimensions, duration_seconds = video.resolve_generated_mv_hevc_expectations(
                    left_path,
                    None,
                    output_path,
                    DiscInfo(name="Movie", frame_rate="24", duration_seconds=100.0),
                    "",
                )

            self.assertEqual(dimensions, (1280, 720))
            self.assertEqual(duration_seconds, 80.0)

    def test_resume_expectations_adjust_source_duration_for_frame_rate_override(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            reference_path = temporary_path / "Movie.mkv"
            output_path = temporary_path / "Movie_MV-HEVC.mov"
            reference_path.write_bytes(b"source")
            with (
                patch.object(video.config, "start_stage", Stage.UPSCALE_VIDEO),
                patch.object(video.config, "frame_rate", "30"),
                patch.object(video.config, "resolution", ""),
                patch.object(video.config, "preview_range", None),
                patch.object(
                    video,
                    "run_ffprobe",
                    return_value=valid_probe(duration=100.0, average_frame_rate="24000/1001"),
                ),
            ):
                dimensions, duration_seconds = video.resolve_generated_mv_hevc_expectations(
                    temporary_path / "missing-left.mov",
                    reference_path,
                    output_path,
                    DiscInfo(name="Movie", frame_rate="24000/1001", duration_seconds=100.0),
                    "",
                )

            self.assertEqual(dimensions, (1920, 1080))
            self.assertAlmostEqual(duration_seconds, 79.92008, delta=0.00001)

    def test_resume_after_upscale_validates_successor_when_base_was_consumed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_folder = Path(temporary_directory)
            base_path = output_folder / "Movie_MV-HEVC.mov"
            upscaled_path = output_folder / "Movie_MV-HEVC Upscaled.mov"
            upscaled_path.write_bytes(b"upscaled")
            with (
                patch.object(video.config, "start_stage", Stage.TRANSCODE_AUDIO),
                patch.object(video.config, "fx_upscale", True),
                patch.object(video.config, "keep_files", False),
                patch.object(video, "run_ffprobe", return_value=valid_probe(width=3840, height=2160)),
                patch.object(video, "run_process_capture", return_value=process_result(VALID_BOX_XML)),
            ):
                inherited_path = video.create_mv_hevc_file(
                    output_folder / "Movie_left_movie.mov",
                    output_folder / "Movie_right_movie.mov",
                    output_folder,
                    DiscInfo(name="Movie", duration_seconds=100.0),
                    expected_dimensions=(1920, 1080),
                    expected_duration_seconds=100.0,
                )
                result = video.create_upscaled_file(
                    inherited_path,
                    expected_generated_mv_hevc_dimensions=(3840, 2160),
                    expected_generated_mv_hevc_duration_seconds=100.0,
                )

            self.assertEqual(inherited_path, base_path)
            self.assertEqual(result, upscaled_path)
            self.assertFalse(base_path.exists())

    def test_post_upscale_resume_preserves_existing_base_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_folder = Path(temporary_directory)
            base_path = output_folder / "Movie_MV-HEVC.mov"
            upscaled_path = output_folder / "Movie_MV-HEVC Upscaled.mov"
            base_path.write_bytes(b"base")
            upscaled_path.write_bytes(b"upscaled")
            with (
                patch.object(video.config, "start_stage", Stage.TRANSCODE_AUDIO),
                patch.object(video.config, "fx_upscale", True),
                patch.object(video.config, "keep_files", False),
                patch.object(video, "run_ffprobe", return_value=valid_probe(width=3840, height=2160)),
                patch.object(video, "run_process_capture", return_value=process_result(VALID_BOX_XML)),
            ):
                result = video.create_upscaled_file(
                    base_path,
                    expected_generated_mv_hevc_dimensions=(3840, 2160),
                    expected_generated_mv_hevc_duration_seconds=100.0,
                )

            self.assertEqual(result, upscaled_path)
            self.assertTrue(base_path.exists())

    def test_post_upscale_resume_preserves_eye_artifacts_when_successor_is_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_folder = Path(temporary_directory)
            left_path = output_folder / "Movie_left_movie.mov"
            right_path = output_folder / "Movie_right_movie.mov"
            upscaled_path = output_folder / "Movie_MV-HEVC Upscaled.mov"
            left_path.write_bytes(b"left")
            right_path.write_bytes(b"right")
            upscaled_path.write_bytes(b"")
            with (
                patch.object(video.config, "start_stage", Stage.TRANSCODE_AUDIO),
                patch.object(video.config, "fx_upscale", True),
                patch.object(video.config, "keep_files", False),
                self.assertRaises(video.GeneratedMVHEVCArtifactError),
            ):
                inherited_path = video.create_mv_hevc_file(
                    left_path,
                    right_path,
                    output_folder,
                    DiscInfo(name="Movie", duration_seconds=100.0),
                    expected_dimensions=(1920, 1080),
                    expected_duration_seconds=100.0,
                )
                video.create_upscaled_file(
                    inherited_path,
                    expected_generated_mv_hevc_dimensions=(3840, 2160),
                    expected_generated_mv_hevc_duration_seconds=100.0,
                )

            self.assertTrue(left_path.exists())
            self.assertTrue(right_path.exists())

    def test_invalid_upscaled_output_preserves_validated_base_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_folder = Path(temporary_directory)
            base_path = output_folder / "Movie_MV-HEVC.mov"
            base_path.write_bytes(b"base")

            def run_upscale(command, _name, *, tool_id, **_kwargs):
                self.assertEqual(tool_id, "fx_upscale")
                Path(command[-1]).with_stem(f"{Path(command[-1]).stem} Upscaled").write_bytes(b"")
                return process_result(tool_id=tool_id)

            with (
                patch.object(video.config, "start_stage", Stage.UPSCALE_VIDEO),
                patch.object(video.config, "fx_upscale", True),
                patch.object(video.config, "keep_files", False),
                patch.object(video, "run_process_capture", side_effect=run_upscale),
                self.assertRaises(video.GeneratedMVHEVCArtifactError) as context,
            ):
                video.create_upscaled_file(
                    base_path,
                    expected_generated_mv_hevc_dimensions=(3840, 2160),
                    expected_generated_mv_hevc_duration_seconds=100.0,
                )

            self.assertEqual(context.exception.stage_number, 6)
            self.assertTrue(base_path.exists())

    def test_resume_expectation_probe_failure_uses_sanitized_title_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            reference_path = temporary_path / "Movie.mkv"
            output_path = temporary_path / "Movie_MV-HEVC.mov"
            reference_path.write_bytes(b"source")
            with (
                patch.object(video.config, "start_stage", Stage.UPSCALE_VIDEO),
                patch.object(video.config, "frame_rate", ""),
                patch.object(video.config, "resolution", ""),
                patch.object(video.config, "preview_range", None),
                patch.object(video, "run_ffprobe", side_effect=RuntimeError("private path")),
                patch.object(video, "cli_message") as message,
            ):
                dimensions, duration_seconds = video.resolve_generated_mv_hevc_expectations(
                    temporary_path / "missing-left.mov",
                    reference_path,
                    output_path,
                    DiscInfo(name="Movie", duration_seconds=100.0),
                    "",
                )

            self.assertEqual(dimensions, (1920, 1080))
            self.assertEqual(duration_seconds, 100.0)
            self.assertNotIn("private path", message.call_args.args[0])

    def test_cleanup_failure_is_reported_without_replacing_substantive_error(self) -> None:
        path = Mock(spec=Path)
        path.name = "Movie.partial.mov"
        path.unlink.side_effect = OSError("permission denied")
        substantive_error = RuntimeError("validation failed")

        with patch.object(video, "cli_message") as message:
            video.report_generated_mv_hevc_cleanup_failure(path, substantive_error, run_context=None)

        self.assertEqual(str(substantive_error), "validation failed")
        self.assertIn("permission denied", substantive_error.__notes__[0])
        message.assert_called_once()
