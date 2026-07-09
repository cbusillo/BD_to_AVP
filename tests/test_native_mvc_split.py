import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

from bd_to_avp.modules.disc import DiscInfo
from bd_to_avp.modules import video


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

        self.assertEqual(Path(command[0]), Path(video.config.FFMPEG_PATH))
        filter_graph = command[command.index("-filter_complex") + 1]
        map_labels = [command[index + 1] for index, value in enumerate(command) if value == "-map"]
        left_label, right_label = map_labels

        self.assertIn(f"crop=1920:1080:1920:0{left_label}", filter_graph)
        self.assertIn(f"crop=1920:1080:0:0{right_label}", filter_graph)

    def test_native_ffmpeg_command_rejects_10_bit_sources(self) -> None:
        self.disc_info.color_depth = 10

        with self.assertRaisesRegex(ValueError, "8-bit"):
            video.generate_native_mvc_ffmpeg_command(Path("left.mov"), Path("right.mov"), self.disc_info, "")


class NativeMvcSelectionTests(unittest.TestCase):
    def test_split_uses_native_helper_when_present_for_extracted_mvc(self) -> None:
        disc_info = DiscInfo(name="Sample")

        with tempfile.NamedTemporaryFile() as helper_file:
            helper_path = Path(helper_file.name)
            helper_path.chmod(0o755)

            with (
                patch.object(video.config, "EDGE264_TEST_PATH", helper_path),
                patch.object(video.config, "source_path", Path("source.mkv")),
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
        native_split.assert_called_once_with(Path("movie_mvc.h264"), Path("left.mov"), Path("right.mov"), disc_info, "")

    def test_split_rejects_10_bit_sources(self) -> None:
        disc_info = DiscInfo(name="Sample", color_depth=10)

        with tempfile.NamedTemporaryFile() as helper_file:
            helper_path = Path(helper_file.name)
            helper_path.chmod(0o755)

            with (
                patch.object(video.config, "EDGE264_TEST_PATH", helper_path),
                patch.object(video.config, "source_path", Path("source.mkv")),
                patch.object(video.config, "keep_files", True),
                patch("bd_to_avp.modules.video.split_mvc_to_stereo_native") as native_split,
            ):
                with self.assertRaisesRegex(RuntimeError, "8-bit Blu-ray 3D MVC sources only"):
                    video.split_mvc_to_stereo(
                        Path("movie_mvc.h264"), Path("left.mov"), Path("right.mov"), disc_info, ""
                    )

        native_split.assert_not_called()

    def test_split_rejects_sources_when_native_helper_is_missing(self) -> None:
        disc_info = DiscInfo(name="Sample")

        with (
            patch.object(video.config, "EDGE264_TEST_PATH", Path("/missing/edge264_test")),
            patch.object(video.config, "source_path", Path("source.m2ts")),
            patch.object(video.config, "MTS_EXTENSIONS", [".mts", ".m2ts"]),
            patch.object(video.config, "keep_files", True),
            patch("bd_to_avp.modules.video.split_mvc_to_stereo_native") as native_split,
        ):
            with self.assertRaisesRegex(RuntimeError, "native MVC splitter is missing"):
                video.split_mvc_to_stereo(Path("movie_mvc.h264"), Path("left.mov"), Path("right.mov"), disc_info, "")

        native_split.assert_not_called()

    def test_has_native_mvc_splitter_repairs_missing_execute_bit(self) -> None:
        with tempfile.NamedTemporaryFile() as helper_file:
            helper_path = Path(helper_file.name)
            helper_path.chmod(0o644)

            with patch.object(video.config, "EDGE264_TEST_PATH", helper_path):
                self.assertTrue(video.has_native_mvc_splitter())

            self.assertTrue(helper_path.stat().st_mode & 0o111)

    def test_native_split_raises_when_ffmpeg_fails(self) -> None:
        splitter = _FakeProcess(returncode=None, stdout=Mock(), returncode_after_wait=-15, poll_results=[None])
        ffmpeg_process = _FakeProcess(returncode=1, stdout=None, stderr=None)

        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch("bd_to_avp.modules.video.generate_native_mvc_splitter_command", return_value=["edge264_test"]),
            patch("bd_to_avp.modules.video.generate_native_mvc_ffmpeg_command", return_value=["ffmpeg"]),
            patch("bd_to_avp.modules.video.subprocess.Popen", side_effect=[splitter, ffmpeg_process]),
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                video.split_mvc_to_stereo_native(
                    Path("movie_mvc.h264"), Path(temp_dir) / "left.mov", Path(temp_dir) / "right.mov", DiscInfo(), ""
                )

        splitter.terminate.assert_called_once()

    def test_native_split_retries_single_threaded_when_splitter_sigaborts(self) -> None:
        splitter_crash = _FakeProcess(returncode=None, stdout=Mock(), returncode_after_wait=-6, poll_results=[-6])
        ffmpeg_broken_pipe = _FakeProcess(returncode=1, stdout=None, stderr=None)
        splitter_retry = _FakeProcess(returncode=None, stdout=Mock(), returncode_after_wait=0, poll_results=[0])
        ffmpeg_retry = _FakeProcess(returncode=0, stdout=None, stderr=None)

        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch(
                "bd_to_avp.modules.video.generate_native_mvc_splitter_command",
                side_effect=[
                    ["edge264_test", "-Omk"],
                    ["edge264_test", "-Osk"],
                ],
            ) as splitter_command,
            patch("bd_to_avp.modules.video.generate_native_mvc_ffmpeg_command", return_value=["ffmpeg"]),
            patch(
                "bd_to_avp.modules.video.subprocess.Popen",
                side_effect=[splitter_crash, ffmpeg_broken_pipe, splitter_retry, ffmpeg_retry],
            ),
        ):
            stdout = StringIO()
            with redirect_stdout(stdout):
                result = video.split_mvc_to_stereo_native(
                    Path("movie_mvc.h264"), Path(temp_dir) / "left.mov", Path(temp_dir) / "right.mov", DiscInfo(), ""
                )

        self.assertEqual(result, (Path(temp_dir) / "left.mov", Path(temp_dir) / "right.mov"))
        self.assertEqual(
            [command_call.kwargs for command_call in splitter_command.call_args_list],
            [{"single_threaded": False}, {"single_threaded": True}],
        )
        self.assertIn("Running native MVC split and encode (multi-threaded).", stdout.getvalue())
        self.assertIn("Native MVC splitter crashed; retrying once in single-threaded mode.", stdout.getvalue())
        self.assertIn("Running native MVC split and encode (single-threaded).", stdout.getvalue())

    def test_native_split_does_not_retry_when_splitter_is_cancelled(self) -> None:
        splitter_cancelled = _FakeProcess(returncode=None, stdout=Mock(), returncode_after_wait=-15, poll_results=[-15])
        ffmpeg_broken_pipe = _FakeProcess(returncode=1, stdout=None, stderr=None)

        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(video.config, "EDGE264_TEST_PATH", Path("edge264_test")),
            patch("bd_to_avp.modules.video.generate_native_mvc_splitter_command", return_value=["edge264_test"]),
            patch("bd_to_avp.modules.video.generate_native_mvc_ffmpeg_command", return_value=["ffmpeg"]),
            patch(
                "bd_to_avp.modules.video.subprocess.Popen", side_effect=[splitter_cancelled, ffmpeg_broken_pipe]
            ) as popen,
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                video.split_mvc_to_stereo_native(
                    Path("movie_mvc.h264"), Path(temp_dir) / "left.mov", Path(temp_dir) / "right.mov", DiscInfo(), ""
                )

        self.assertEqual(popen.call_count, 2)

    def test_native_split_probes_iso_sources_and_skips_doomed_multithread_attempt(self) -> None:
        splitter_retry = _FakeProcess(returncode=None, stdout=Mock(), returncode_after_wait=0, poll_results=[0])
        ffmpeg_retry = _FakeProcess(returncode=0, stdout=None, stderr=None)

        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(video.config, "source_path", Path("movie.iso")),
            patch.object(video.config, "IMAGE_EXTENSIONS", [".iso"]),
            patch("bd_to_avp.modules.video.native_multithread_splitter_probe_crashed", return_value=True) as probe,
            patch(
                "bd_to_avp.modules.video.generate_native_mvc_splitter_command",
                return_value=["edge264_test", "-Osk"],
            ) as splitter_command,
            patch("bd_to_avp.modules.video.generate_native_mvc_ffmpeg_command", return_value=["ffmpeg"]),
            patch("bd_to_avp.modules.video.subprocess.Popen", side_effect=[splitter_retry, ffmpeg_retry]),
        ):
            stdout = StringIO()
            with redirect_stdout(stdout):
                result = video.split_mvc_to_stereo_native(
                    Path("movie_mvc.h264"), Path(temp_dir) / "left.mov", Path(temp_dir) / "right.mov", DiscInfo(), ""
                )

        self.assertEqual(result, (Path(temp_dir) / "left.mov", Path(temp_dir) / "right.mov"))
        probe.assert_called_once()
        self.assertEqual([call.kwargs for call in splitter_command.call_args_list], [{"single_threaded": True}])
        self.assertIn("Checking native MVC splitter with a short multi-threaded probe.", stdout.getvalue())
        self.assertIn("Native MVC splitter probe crashed; using slower single-threaded mode.", stdout.getvalue())
        self.assertIn("Running native MVC split and encode (single-threaded).", stdout.getvalue())

    def test_native_split_does_not_probe_mkv_sources(self) -> None:
        splitter = _FakeProcess(returncode=None, stdout=Mock(), returncode_after_wait=0, poll_results=[0])
        ffmpeg_process = _FakeProcess(returncode=0, stdout=None, stderr=None)

        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(video.config, "source_path", Path("movie.mkv")),
            patch.object(video.config, "IMAGE_EXTENSIONS", [".iso"]),
            patch("bd_to_avp.modules.video.native_multithread_splitter_probe_crashed") as probe,
            patch(
                "bd_to_avp.modules.video.generate_native_mvc_splitter_command",
                return_value=["edge264_test", "-Omk"],
            ) as splitter_command,
            patch("bd_to_avp.modules.video.generate_native_mvc_ffmpeg_command", return_value=["ffmpeg"]),
            patch("bd_to_avp.modules.video.subprocess.Popen", side_effect=[splitter, ffmpeg_process]),
        ):
            video.split_mvc_to_stereo_native(
                Path("movie_mvc.h264"), Path(temp_dir) / "left.mov", Path(temp_dir) / "right.mov", DiscInfo(), ""
            )

        probe.assert_not_called()
        self.assertEqual([call.kwargs for call in splitter_command.call_args_list], [{"single_threaded": False}])

    def test_native_multithread_probe_returns_true_when_splitter_dies_by_signal(self) -> None:
        splitter_crash = _FakeProcess(returncode=None, stdout=None, returncode_after_wait=-6)

        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch("bd_to_avp.modules.video.generate_native_mvc_splitter_command", return_value=["edge264_test"]),
            patch("bd_to_avp.modules.video.subprocess.Popen", return_value=splitter_crash),
        ):
            result = video.native_multithread_splitter_probe_crashed(
                Path("movie_mvc.264"), Path(temp_dir) / "split.native_mvc.log"
            )

        self.assertTrue(result)

    def test_native_multithread_probe_does_not_treat_sigterm_as_crash(self) -> None:
        splitter_cancelled = _FakeProcess(returncode=None, stdout=None, returncode_after_wait=-15)

        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(video.config, "EDGE264_TEST_PATH", Path("edge264_test")),
            patch("bd_to_avp.modules.video.generate_native_mvc_splitter_command", return_value=["edge264_test"]),
            patch("bd_to_avp.modules.video.subprocess.Popen", return_value=splitter_cancelled),
            self.assertRaises(subprocess.CalledProcessError) as raised,
        ):
            video.native_multithread_splitter_probe_crashed(
                Path("movie_mvc.264"), Path(temp_dir) / "split.native_mvc.log"
            )

        self.assertEqual(raised.exception.returncode, -15)

    def test_native_multithread_probe_returns_false_after_timeout(self) -> None:
        splitter = _FakeProcess(returncode=None, stdout=None, returncode_after_wait=-15, raises_timeout_once=True)

        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch("bd_to_avp.modules.video.generate_native_mvc_splitter_command", return_value=["edge264_test"]),
            patch("bd_to_avp.modules.video.subprocess.Popen", return_value=splitter),
        ):
            result = video.native_multithread_splitter_probe_crashed(
                Path("movie_mvc.264"), Path(temp_dir) / "split.native_mvc.log"
            )

        self.assertFalse(result)
        splitter.terminate.assert_called_once()
        splitter.kill.assert_not_called()

    def test_native_multithread_probe_kills_splitter_when_timeout_cleanup_stalls(self) -> None:
        splitter = _FakeProcess(
            returncode=None,
            stdout=None,
            returncode_after_wait=-9,
            raises_timeout_count=2,
        )

        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch("bd_to_avp.modules.video.generate_native_mvc_splitter_command", return_value=["edge264_test"]),
            patch("bd_to_avp.modules.video.subprocess.Popen", return_value=splitter),
        ):
            result = video.native_multithread_splitter_probe_crashed(
                Path("movie_mvc.264"), Path(temp_dir) / "split.native_mvc.log"
            )

        self.assertFalse(result)
        splitter.terminate.assert_called_once()
        splitter.kill.assert_called_once()

    def test_native_split_raises_clear_error_when_single_thread_retry_sigaborts(self) -> None:
        splitter_crash = _FakeProcess(returncode=None, stdout=Mock(), returncode_after_wait=-6, poll_results=[-6])
        ffmpeg_broken_pipe = _FakeProcess(returncode=1, stdout=None, stderr=None)
        splitter_retry_crash = _FakeProcess(returncode=None, stdout=Mock(), returncode_after_wait=-6, poll_results=[-6])
        ffmpeg_retry_broken_pipe = _FakeProcess(returncode=1, stdout=None, stderr=None)

        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(video.config, "EDGE264_TEST_PATH", Path("edge264_test")),
            patch(
                "bd_to_avp.modules.video.generate_native_mvc_splitter_command",
                side_effect=[
                    ["edge264_test", "-Omk"],
                    ["edge264_test", "-Osk"],
                ],
            ),
            patch("bd_to_avp.modules.video.generate_native_mvc_ffmpeg_command", return_value=["ffmpeg"]),
            patch(
                "bd_to_avp.modules.video.subprocess.Popen",
                side_effect=[splitter_crash, ffmpeg_broken_pipe, splitter_retry_crash, ffmpeg_retry_broken_pipe],
            ),
        ):
            with self.assertRaisesRegex(video.NativeMvcSplitError, "SIGABRT"):
                video.split_mvc_to_stereo_native(
                    Path("movie_mvc.h264"), Path(temp_dir) / "left.mov", Path(temp_dir) / "right.mov", DiscInfo(), ""
                )


class _FakeProcess:
    def __init__(
        self,
        returncode: int | None,
        stdout,
        stderr=None,
        returncode_after_wait: int | None = None,
        poll_results: list[int | None] | None = None,
        raises_timeout: bool = False,
        raises_timeout_once: bool = False,
        raises_timeout_count: int = 0,
    ) -> None:
        self.returncode = returncode
        self.returncode_after_wait = returncode_after_wait if returncode_after_wait is not None else returncode
        self.poll_results = poll_results or []
        self.raises_timeout = raises_timeout
        self.raises_timeout_once = raises_timeout_once
        self.raises_timeout_count = raises_timeout_count
        self.stdout = stdout
        self.stderr = stderr
        self.terminate = Mock()
        self.kill = Mock()

    def wait(self, timeout=None) -> int:
        if self.raises_timeout:
            raise subprocess.TimeoutExpired("edge264_test", timeout or 0)
        if self.raises_timeout_count > 0:
            self.raises_timeout_count -= 1
            raise subprocess.TimeoutExpired("edge264_test", timeout or 0)
        if self.raises_timeout_once:
            self.raises_timeout_once = False
            raise subprocess.TimeoutExpired("edge264_test", timeout or 0)
        self.returncode = self.returncode_after_wait
        if self.returncode is None:
            raise AssertionError("test fake process still running after wait")
        return self.returncode

    def poll(self) -> int | None:
        if self.poll_results:
            return self.poll_results.pop(0)
        return self.returncode


if __name__ == "__main__":
    unittest.main()
