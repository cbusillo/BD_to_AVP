import signal
import subprocess
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

from bd_to_avp.modules import video
from bd_to_avp.modules.disc import DiscInfo
from bd_to_avp.observability import ObservabilityContext
from bd_to_avp.process_runner import (
    ProcessCancelled,
    ProcessExecutionError,
    ProcessOutputSnapshot,
    ProcessPipelineError,
    ProcessPipelineResult,
    ProcessPipelineStageResult,
    ProcessResult,
    ProcessTimeoutError,
)


class NativeMvcCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.disc_info = DiscInfo(name="Sample", frame_rate="24000/1001", resolution="1920x1080", color_depth=8)

    def test_native_splitter_command_uses_side_by_side_y4m_output(self) -> None:
        with patch.object(video.config, "EDGE264_TEST_PATH", Path("/tools/edge264_test")):
            command = video.generate_native_mvc_splitter_command(Path("movie_mvc.h264"))

        self.assertEqual(command, [Path("/tools/edge264_test"), Path("movie_mvc.h264"), "-Omk"])

    def test_native_splitter_command_can_force_single_threaded_decoding(self) -> None:
        with patch.object(video.config, "EDGE264_TEST_PATH", Path("/tools/edge264_test")):
            command = video.generate_native_mvc_splitter_command(Path("movie_mvc.h264"), single_threaded=True)

        self.assertEqual(command, [Path("/tools/edge264_test"), Path("movie_mvc.h264"), "-Osk"])

    def test_mvc_container_stream_command_emits_annex_b_to_stdout(self) -> None:
        with patch.object(video.config, "FFMPEG_PATH", Path("/tools/ffmpeg")):
            command = video.generate_mvc_annex_b_stream_command(Path("movie.mkv"))

        self.assertEqual(
            command,
            [
                Path("/tools/ffmpeg"),
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                Path("movie.mkv"),
                "-map",
                "0:v:0",
                "-c:v",
                "copy",
                "-bsf:v",
                "h264_mp4toannexb",
                "-f",
                "h264",
                "-",
            ],
        )

    def test_output_artifact_roles_distinguish_left_and_right_outputs(self) -> None:
        self.assertEqual(video.output_artifact_roles(1), ("stereo_video_output",))
        self.assertEqual(
            video.output_artifact_roles(2),
            ("left_eye_video_output", "right_eye_video_output"),
        )
        self.assertEqual(
            video.output_artifact_roles(3),
            ("video_output_1", "video_output_2", "video_output_3"),
        )

    def test_native_ffmpeg_command_splits_side_by_side_stream(self) -> None:
        with (
            patch.object(video.config, "left_right_bitrate", 12),
            patch.object(video.config, "software_encoder", False),
            patch.object(video.config, "swap_eyes", False),
            patch.object(video.config, "frame_rate", ""),
            patch.object(video.config, "resolution", ""),
        ):
            command = video.generate_native_mvc_ffmpeg_command(Path("left.mov"), Path("right.mov"), self.disc_info, "")

        self.assertEqual(Path(command[0]), Path(video.config.FFMPEG_PATH))
        self.assertIn("-f", command)
        self.assertIn("yuv4mpegpipe", command)
        self.assertIn("-filter_complex", command)
        filter_graph = command[command.index("-filter_complex") + 1]
        self.assertIn("split=2", filter_graph)
        self.assertIn("crop=1920:1080:0:0", filter_graph)
        self.assertIn("crop=1920:1080:1920:0", filter_graph)
        self.assertIn("hevc_videotoolbox", command)
        self.assertLess(command.index("file:left.mov"), command.index("file:right.mov"))

    def test_native_ffmpeg_command_swaps_eye_outputs(self) -> None:
        with (
            patch.object(video.config, "left_right_bitrate", 12),
            patch.object(video.config, "software_encoder", False),
            patch.object(video.config, "swap_eyes", True),
            patch.object(video.config, "frame_rate", ""),
            patch.object(video.config, "resolution", ""),
        ):
            command = video.generate_native_mvc_ffmpeg_command(Path("left.mov"), Path("right.mov"), self.disc_info, "")

        filter_graph = command[command.index("-filter_complex") + 1]
        map_labels = [command[index + 1] for index, value in enumerate(command) if value == "-map"]
        left_label, right_label = map_labels
        self.assertIn(f"crop=1920:1080:1920:0{left_label}", filter_graph)
        self.assertIn(f"crop=1920:1080:0:0{right_label}", filter_graph)

    def test_native_ffmpeg_command_rejects_10_bit_sources(self) -> None:
        self.disc_info.color_depth = 10

        with self.assertRaisesRegex(ValueError, "8-bit"):
            video.generate_native_mvc_ffmpeg_command(Path("left.mov"), Path("right.mov"), self.disc_info, "")

    def test_mv_hevc_merge_writes_neutral_stereo_disparity_metadata(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(video.config, "SPATIAL_MEDIA_PATH", Path("/tools/spatial-media-kit-tool")),
            patch.object(video.config, "mv_hevc_quality", 75),
            patch.object(video.config, "fov", 90),
            patch.object(
                video,
                "run_process_capture",
                return_value=process_result("spatial_media_kit_tool"),
            ) as run_command,
        ):
            video.combine_to_mv_hevc(Path("left.mov"), Path("right.mov"), Path(temp_dir) / "spatial.mov", 8)

        command = run_command.call_args.args[0]
        disparity_option = command.index("--horizontal-disparity-adjustment")
        self.assertEqual(str(command[disparity_option + 1]), "0")
        self.assertFalse(run_command.call_args.kwargs["merge_stderr"])


class NativeMvcSelectionTests(unittest.TestCase):
    def test_direct_pipeline_keeps_streamed_source_file(self) -> None:
        disc_info = DiscInfo(name="Sample")
        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = Path(temp_dir) / "source.mkv"
            source_path.touch()
            helper_path = Path(temp_dir) / "edge264_test"
            helper_path.touch(mode=0o755)
            with (
                patch.object(video.config, "EDGE264_TEST_PATH", helper_path),
                patch.object(video.config, "keep_files", False),
                patch.object(video.config, "MTS_EXTENSIONS", [".mts", ".m2ts"]),
                patch(
                    "bd_to_avp.modules.video.split_mvc_to_stereo_native",
                    return_value=(Path("left.mov"), Path("right.mov")),
                ),
            ):
                video.split_mvc_to_stereo(source_path, Path("left.mov"), Path("right.mov"), disc_info, "")

            self.assertTrue(source_path.exists())

    def test_split_uses_native_helper_when_present_for_extracted_mvc(self) -> None:
        disc_info = DiscInfo(name="Sample")
        with tempfile.NamedTemporaryFile() as helper_file:
            helper_path = Path(helper_file.name)
            helper_path.chmod(0o755)
            with (
                patch.object(video.config, "EDGE264_TEST_PATH", helper_path),
                patch.object(video.config, "source_path", Path("source.mkv")),
                patch.object(video.config, "keep_files", True),
                patch(
                    "bd_to_avp.modules.video.split_mvc_to_stereo_native",
                    return_value=(Path("left.mov"), Path("right.mov")),
                ) as native_split,
            ):
                result = video.split_mvc_to_stereo(
                    Path("movie_mvc.h264"), Path("left.mov"), Path("right.mov"), disc_info, ""
                )

        self.assertEqual(result, (Path("left.mov"), Path("right.mov")))
        native_split.assert_called_once_with(
            Path("movie_mvc.h264"),
            Path("left.mov"),
            Path("right.mov"),
            disc_info,
            "",
            run_context=None,
            cancellation_event=None,
            observability_context=None,
        )

    def test_split_uses_native_helper_for_mts_sources_when_present(self) -> None:
        disc_info = DiscInfo(name="Sample")
        with tempfile.NamedTemporaryFile() as helper_file:
            helper_path = Path(helper_file.name)
            helper_path.chmod(0o755)
            with (
                patch.object(video.config, "EDGE264_TEST_PATH", helper_path),
                patch.object(video.config, "source_path", Path("source.m2ts")),
                patch.object(video.config, "MTS_EXTENSIONS", [".mts", ".m2ts"]),
                patch.object(video.config, "keep_files", True),
                patch(
                    "bd_to_avp.modules.video.split_mvc_to_stereo_native",
                    return_value=(Path("left.mov"), Path("right.mov")),
                ) as native_split,
            ):
                result = video.split_mvc_to_stereo(
                    Path("movie_mvc.h264"), Path("left.mov"), Path("right.mov"), disc_info, ""
                )

        self.assertEqual(result, (Path("left.mov"), Path("right.mov")))
        native_split.assert_called_once()

    def test_split_rejects_10_bit_sources(self) -> None:
        disc_info = DiscInfo(name="Sample", color_depth=10)
        with tempfile.NamedTemporaryFile() as helper_file:
            helper_path = Path(helper_file.name)
            helper_path.chmod(0o755)
            with (
                patch.object(video.config, "EDGE264_TEST_PATH", helper_path),
                patch.object(video.config, "source_path", Path("source.mkv")),
                patch("bd_to_avp.modules.video.split_mvc_to_stereo_native") as native_split,
                self.assertRaisesRegex(RuntimeError, "8-bit Blu-ray 3D MVC sources only"),
            ):
                video.split_mvc_to_stereo(Path("movie_mvc.h264"), Path("left.mov"), Path("right.mov"), disc_info, "")

        native_split.assert_not_called()

    def test_split_rejects_sources_when_native_helper_is_missing(self) -> None:
        disc_info = DiscInfo(name="Sample")
        with (
            patch.object(video.config, "EDGE264_TEST_PATH", Path("/missing/edge264_test")),
            patch.object(video.config, "source_path", Path("source.m2ts")),
            patch("bd_to_avp.modules.video.split_mvc_to_stereo_native") as native_split,
            self.assertRaisesRegex(RuntimeError, "native MVC splitter is missing"),
        ):
            video.split_mvc_to_stereo(Path("movie_mvc.h264"), Path("left.mov"), Path("right.mov"), disc_info, "")

        native_split.assert_not_called()

    def test_has_native_mvc_splitter_repairs_missing_execute_bit(self) -> None:
        with tempfile.NamedTemporaryFile() as helper_file:
            helper_path = Path(helper_file.name)
            helper_path.chmod(0o644)
            with patch.object(video.config, "EDGE264_TEST_PATH", helper_path):
                self.assertTrue(video.has_native_mvc_splitter())
            self.assertTrue(helper_path.stat().st_mode & 0o111)

    def test_streaming_pipeline_registers_producer_splitter_encoder_and_outputs(self) -> None:
        left_output = Path("left.mov")
        right_output = Path("right.mov")
        run_context = Mock()
        cancellation_event = threading.Event()
        event_context = ObservabilityContext()
        with (
            patch.object(video, "should_stream_mvc_from_container", return_value=True),
            patch.object(video, "generate_mvc_annex_b_stream_command", return_value=["source-ffmpeg"]),
            patch.object(video, "generate_native_mvc_splitter_command", return_value=["edge264_test", "-", "-Omk"]),
            patch.object(video, "generate_native_mvc_ffmpeg_command", return_value=["encode-ffmpeg"]),
            patch.object(video.ProcessPipelineRunner, "run", return_value=pipeline_success(True)) as run,
        ):
            result = video.split_mvc_to_stereo_native(
                Path("source.mkv"),
                left_output,
                right_output,
                DiscInfo(),
                "",
                run_context=run_context,
                cancellation_event=cancellation_event,
                observability_context=event_context,
            )

        self.assertEqual(result, (left_output, right_output))
        stages = run.call_args.args[0]
        self.assertEqual([stage.spec.tool_id for stage in stages], ["ffmpeg", "edge264", "ffmpeg"])
        self.assertEqual([stage.spec.argv[0] for stage in stages], ["source-ffmpeg", "edge264_test", "encode-ffmpeg"])
        self.assertEqual([probe.path for probe in stages[-1].spec.artifacts], [left_output, right_output])
        self.assertEqual(
            [probe.role for probe in stages[-1].spec.artifacts],
            ["left_eye_video_output", "right_eye_video_output"],
        )
        self.assertTrue(all(stage.spec.event_context is event_context for stage in stages))
        self.assertIs(run.call_args.kwargs["run_context"], run_context)
        self.assertIs(run.call_args.kwargs["cancellation_event"], cancellation_event)

    def test_extracted_mvc_pipeline_has_splitter_and_encoder_stages(self) -> None:
        with (
            patch.object(video, "generate_native_mvc_splitter_command", return_value=["edge264_test", "movie.264"]),
            patch.object(video.ProcessPipelineRunner, "run", return_value=pipeline_success(False)) as run,
        ):
            video.run_native_mvc_split_attempt(
                Path("movie.264"),
                ["encode-ffmpeg"],
                (Path("output.mov"),),
                single_threaded=False,
            )

        stages = run.call_args.args[0]
        self.assertEqual([stage.spec.tool_id for stage in stages], ["edge264", "ffmpeg"])

    def test_pipeline_reports_producer_failure_before_pipe_cascade(self) -> None:
        producer_error = process_error(2, ["source-ffmpeg"])
        error = pipeline_failure(
            producer_present=True,
            producer_error=producer_error,
            splitter_error=process_error(-signal.SIGPIPE, ["edge264_test"]),
            ffmpeg_error=process_error(1, ["encode-ffmpeg"]),
        )
        with (
            patch.object(video, "generate_native_mvc_splitter_command", return_value=["edge264_test"]),
            patch.object(video.ProcessPipelineRunner, "run", side_effect=error),
            self.assertRaises(subprocess.CalledProcessError) as raised,
        ):
            video.run_native_mvc_split_attempt(
                Path("-"),
                ["encode-ffmpeg"],
                (Path("output.mov"),),
                producer_command=["source-ffmpeg"],
                single_threaded=False,
            )

        self.assertIs(raised.exception, producer_error)

    def test_pipeline_reports_encoder_failure_when_upstream_gets_sigpipe(self) -> None:
        ffmpeg_error = process_error(1, ["encode-ffmpeg"])
        error = pipeline_failure(
            producer_present=True,
            producer_error=process_error(-signal.SIGPIPE, ["source-ffmpeg"]),
            splitter_error=process_error(-signal.SIGPIPE, ["edge264_test"]),
            ffmpeg_error=ffmpeg_error,
        )
        with (
            patch.object(video, "generate_native_mvc_splitter_command", return_value=["edge264_test"]),
            patch.object(video.ProcessPipelineRunner, "run", side_effect=error),
            self.assertRaises(subprocess.CalledProcessError) as raised,
        ):
            video.run_native_mvc_split_attempt(
                Path("-"),
                ["encode-ffmpeg"],
                (Path("output.mov"),),
                producer_command=["source-ffmpeg"],
                single_threaded=False,
            )

        self.assertIs(raised.exception, ffmpeg_error)

    def test_pipeline_ignores_producer_sigpipe_after_successful_downstream_completion(self) -> None:
        error = pipeline_failure(
            producer_present=True,
            producer_error=process_error(-signal.SIGPIPE, ["source-ffmpeg"]),
        )
        with (
            patch.object(video, "generate_native_mvc_splitter_command", return_value=["edge264_test"]),
            patch.object(video.ProcessPipelineRunner, "run", side_effect=error),
        ):
            video.run_native_mvc_split_attempt(
                Path("-"),
                ["encode-ffmpeg"],
                (Path("output.mov"),),
                producer_command=["source-ffmpeg"],
                single_threaded=False,
            )

    def test_native_split_retries_single_threaded_when_splitter_sigaborts(self) -> None:
        with (
            patch.object(video.config, "EDGE264_TEST_PATH", Path("edge264_test")),
            patch.object(video, "should_stream_mvc_from_container", return_value=False),
            patch.object(video, "should_probe_native_multithread_splitter", return_value=False),
            patch.object(
                video.ProcessPipelineRunner,
                "run",
                side_effect=[
                    pipeline_failure(
                        splitter_error=process_error(-signal.SIGABRT, ["edge264_test"]),
                        ffmpeg_error=process_error(1, ["encode-ffmpeg"]),
                    ),
                    pipeline_success(False),
                ],
            ) as run,
            redirect_stdout(StringIO()),
        ):
            video.split_mvc_to_stereo_native(Path("movie.264"), Path("left.mov"), Path("right.mov"), DiscInfo(), "")

        self.assertEqual(run.call_count, 2)
        first_stages = run.call_args_list[0].args[0]
        second_stages = run.call_args_list[1].args[0]
        self.assertEqual(first_stages[0].spec.argv[-1], "-Omk")
        self.assertEqual(second_stages[0].spec.argv[-1], "-Osk")

    def test_native_split_does_not_retry_when_pipeline_is_cancelled(self) -> None:
        cancellation = ProcessCancelled("cancelled")
        with (
            patch.object(video, "should_stream_mvc_from_container", return_value=False),
            patch.object(video, "should_probe_native_multithread_splitter", return_value=False),
            patch.object(video.ProcessPipelineRunner, "run", side_effect=cancellation) as run,
            self.assertRaises(ProcessCancelled),
        ):
            video.split_mvc_to_stereo_native(Path("movie.264"), Path("left.mov"), Path("right.mov"), DiscInfo(), "")

        run.assert_called_once()

    def test_native_split_probe_crash_skips_multithreaded_attempt(self) -> None:
        with (
            patch.object(video, "should_stream_mvc_from_container", return_value=False),
            patch.object(video, "should_probe_native_multithread_splitter", return_value=True),
            patch.object(video, "native_multithread_splitter_probe_crashed", return_value=True),
            patch.object(video, "run_native_mvc_split_attempt") as run_attempt,
            redirect_stdout(StringIO()),
        ):
            video.run_native_mvc_encoding(Path("movie.264"), (Path("output.mov"),), ["encode-ffmpeg"])

        run_attempt.assert_called_once()
        self.assertTrue(run_attempt.call_args.kwargs["single_threaded"])

    def test_native_split_probe_pass_uses_multithreaded_attempt(self) -> None:
        stdout = StringIO()
        with (
            patch.object(video, "should_stream_mvc_from_container", return_value=False),
            patch.object(video, "should_probe_native_multithread_splitter", return_value=True),
            patch.object(video, "native_multithread_splitter_probe_crashed", return_value=False),
            patch.object(video, "run_native_mvc_split_attempt") as run_attempt,
            redirect_stdout(stdout),
        ):
            video.run_native_mvc_encoding(Path("movie.264"), (Path("output.mov"),), ["encode-ffmpeg"])

        self.assertFalse(run_attempt.call_args.kwargs["single_threaded"])
        self.assertIn("Native MVC splitter probe passed", stdout.getvalue())

    def test_native_split_does_not_probe_mkv_sources(self) -> None:
        with (
            patch.object(video, "should_stream_mvc_from_container", return_value=True),
            patch.object(video, "native_multithread_splitter_probe_crashed") as probe,
            patch.object(video, "run_native_mvc_split_attempt"),
        ):
            video.run_native_mvc_encoding(Path("movie.mkv"), (Path("output.mov"),), ["encode-ffmpeg"])

        probe.assert_not_called()

    def test_native_multithread_probe_returns_true_when_splitter_dies_by_signal(self) -> None:
        with (
            patch.object(video.config, "EDGE264_TEST_PATH", Path("edge264_test")),
            patch.object(
                video.ChildProcessRunner,
                "run",
                side_effect=process_error(-signal.SIGABRT, ["edge264_test"]),
            ),
        ):
            self.assertTrue(video.native_multithread_splitter_probe_crashed(Path("movie.264")))

    def test_native_multithread_probe_does_not_treat_sigterm_as_crash(self) -> None:
        error = process_error(-signal.SIGTERM, ["edge264_test"])
        with (
            patch.object(video.ChildProcessRunner, "run", side_effect=error),
            self.assertRaises(ProcessExecutionError) as raised,
        ):
            video.native_multithread_splitter_probe_crashed(Path("movie.264"))

        self.assertIs(raised.exception, error)

    def test_native_multithread_probe_returns_false_after_timeout(self) -> None:
        with patch.object(
            video.ChildProcessRunner,
            "run",
            side_effect=ProcessTimeoutError("probe timed out"),
        ):
            self.assertFalse(video.native_multithread_splitter_probe_crashed(Path("movie.264")))

    def test_native_multithread_probe_uses_bounded_runner_contract(self) -> None:
        run_context = Mock()
        cancellation_event = threading.Event()
        event_context = ObservabilityContext()
        with (
            patch.object(video, "generate_native_mvc_splitter_command", return_value=["edge264_test"]),
            patch.object(video.ChildProcessRunner, "run", return_value=process_result("edge264")) as run,
        ):
            result = video.native_multithread_splitter_probe_crashed(
                Path("movie.264"),
                run_context=run_context,
                cancellation_event=cancellation_event,
                observability_context=event_context,
            )

        self.assertFalse(result)
        spec = run.call_args.args[0]
        self.assertEqual(spec.stdout, subprocess.DEVNULL)
        self.assertFalse(spec.merge_stderr)
        self.assertEqual(spec.timeout_seconds, video.NATIVE_MVC_PROBE_TIMEOUT_SECONDS)
        self.assertIs(spec.event_context, event_context)
        self.assertIs(run.call_args.kwargs["run_context"], run_context)
        self.assertIs(run.call_args.kwargs["cancellation_event"], cancellation_event)

    def test_native_split_raises_clear_error_when_single_thread_retry_sigaborts(self) -> None:
        crash = pipeline_failure(
            splitter_error=process_error(-signal.SIGABRT, ["edge264_test"]),
            ffmpeg_error=process_error(1, ["encode-ffmpeg"]),
        )
        with (
            patch.object(video.config, "EDGE264_TEST_PATH", Path("edge264_test")),
            patch.object(video, "should_stream_mvc_from_container", return_value=False),
            patch.object(video, "should_probe_native_multithread_splitter", return_value=False),
            patch.object(video.ProcessPipelineRunner, "run", side_effect=[crash, crash]),
            self.assertRaisesRegex(video.NativeMvcSplitError, "SIGABRT.*diagnostic report"),
        ):
            video.split_mvc_to_stereo_native(Path("movie.264"), Path("left.mov"), Path("right.mov"), DiscInfo(), "")


def process_result(tool_id: str) -> ProcessResult:
    empty = ProcessOutputSnapshot(b"", b"", 0, 0, 0, 0)
    return ProcessResult(tool_run_id=f"{tool_id}-run", returncode=0, elapsed_ms=1, stdout=empty, stderr=empty)


def process_error(returncode: int, command: list[str | bytes]) -> ProcessExecutionError:
    empty = ProcessOutputSnapshot(b"", b"", 0, 0, 0, 0)
    return ProcessExecutionError(returncode, command, empty, empty)


def pipeline_success(producer_present: bool) -> ProcessPipelineResult:
    tool_ids = ["ffmpeg", "edge264", "ffmpeg"] if producer_present else ["edge264", "ffmpeg"]
    return ProcessPipelineResult(
        stages=tuple(
            ProcessPipelineStageResult(tool_id, process_result(tool_id), completed_before_final=True)
            for tool_id in tool_ids
        )
    )


def pipeline_failure(
    *,
    producer_present: bool = False,
    producer_error: BaseException | None = None,
    splitter_error: BaseException | None = None,
    ffmpeg_error: BaseException | None = None,
) -> ProcessPipelineError:
    stages: list[ProcessPipelineStageResult] = []
    if producer_present:
        stages.append(
            ProcessPipelineStageResult(
                "ffmpeg",
                None if producer_error else process_result("producer"),
                producer_error,
                completed_before_final=producer_error is not None,
            )
        )
    stages.extend(
        (
            ProcessPipelineStageResult(
                "edge264",
                None if splitter_error else process_result("splitter"),
                splitter_error,
                completed_before_final=splitter_error is not None,
            ),
            ProcessPipelineStageResult(
                "ffmpeg",
                None if ffmpeg_error else process_result("encoder"),
                ffmpeg_error,
                completed_before_final=ffmpeg_error is not None,
            ),
        )
    )
    return ProcessPipelineError(ProcessPipelineResult(tuple(stages)))


if __name__ == "__main__":
    unittest.main()
