import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from bd_to_avp import preflight
from bd_to_avp.modules import container, disc, process, sub
from bd_to_avp.modules.audio_mode import AudioMode
from bd_to_avp.modules.bluray import READER_TOOL_ID, disc_reads_directly, parse_titles_output
from bd_to_avp.modules.config import Stage, config
from bd_to_avp.modules.preview_range import PreviewRange
from bd_to_avp.modules.video_mode import VideoMode
from bd_to_avp.modules.video_route import ResolvedVideoRoute, VideoRouteKind
from bd_to_avp.worker.protocol import VideoRouteIntent
from bd_to_avp.process_runner import ProcessOutputSnapshot, ProcessResult


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
RAINFOREST_ISO_ENV = "BD_TO_AVP_RAINFOREST_ISO"
RAINFOREST_MAIN_CLIP = "00007"
RAINFOREST_MAIN_PLAYLIST = "1005"
RAINFOREST_SHORT_TITLE_ID = "bluray:5"
# Decoded frames of the first 100 Rainforest stereo pairs, shared with the ssif_probe test.
RAINFOREST_FIRST_100_FRAME_LINES_SHA256 = "186a33d0a66c39619b94d354f77fbe364ee24c2e441ee25d90a8902b75199166"


def make_stream(pid: int, coding_type: int, language: str | None) -> dict[str, object]:
    return {"pid": pid, "coding_type": coding_type, "format": 6, "rate": 1, "language": language}


def make_title(
    playlist: int, clip_id: str, seconds: int, *, main: bool = False, readable: bool = True
) -> dict[str, object]:
    return {
        "playlist": playlist,
        "duration_ticks": seconds * 90_000,
        "main_feature": main,
        "clip_id": clip_id,
        "clip_count": 1,
        "angle_count": 1,
        "readable": readable,
        "base_view_right": False,
        "video": {"pid": 4113, "coding_type": 27, "format": 6, "rate": 1, "language": None},
        "audio": [make_stream(4352, 134, "deu"), make_stream(4353, 134, "eng")],
        "subtitles": [make_stream(4608, 0x90, "eng"), make_stream(4609, 0x92, "eng")],
    }


def titles_listing(*, main_readable: bool = True) -> dict[str, object]:
    return {
        "type": "bd3d.titles",
        "contract": 1,
        "volume_id": "Rain Forest 3D",
        "content_3d": True,
        "titles": [
            make_title(5, "00007", 3240),
            make_title(1005, "00007", 3240, main=True, readable=main_readable),
            make_title(1004, "00005", 60),
            make_title(13, "00023", 84, readable=False),
        ],
    }


FAKE_READER = """
import json, os, sys
if len(sys.argv) == 3 and sys.argv[1] == "titles":
    if "encrypted" in sys.argv[2]:
        sys.stderr.write('{"type":"error","code":"encrypted","message":"encrypted"}\\n')
        sys.exit(1)
    sys.stdout.write(open(os.environ["FAKE_BD3D_TITLES"]).read())
    sys.exit(0)
sys.exit(2)
"""

MAKEMKV_INFO = "\n".join(
    [
        'CINFO:2,0,"Rain Forest 3D"',
        'TINFO:0,9,0,"0:54:00"',
        'TINFO:0,26,0,"7/8"',
        'SINFO:0,1,6,0,"Mpeg4-MVC-3D"',
        'TINFO:1,9,0,"0:01:00"',
        'TINFO:1,26,0,"5/6"',
        'SINFO:1,1,6,0,"Mpeg4-MVC-3D"',
    ]
)


def makemkv_result() -> ProcessResult:
    payload = MAKEMKV_INFO.encode()
    return ProcessResult(
        tool_run_id="run-id",
        returncode=0,
        elapsed_ms=1,
        stdout=ProcessOutputSnapshot(payload, b"", len(payload), len(payload), 0, 0),
        stderr=ProcessOutputSnapshot(b"", b"", 0, 0, 0, 0),
    )


class DirectBlurayRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.reader = self.root / "bd3d_reader"
        self.reader.write_text(f"#!{sys.executable}\n{FAKE_READER}", encoding="utf-8")
        self.reader.chmod(0o755)
        self.listing = self.root / "titles.json"
        self.write_listing(titles_listing())
        self.source = self.root / "Movie.iso"
        self.source.write_bytes(b"")
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(config, "BD3D_READER_PATH", self.reader))
        self.stack.enter_context(patch.object(config, "source_path", self.source))
        self.stack.enter_context(patch.object(config, "source_str", None))
        self.stack.enter_context(patch.object(config, "preview_range", None))
        self.stack.enter_context(patch.object(config, "start_stage", Stage.CREATE_MKV))
        self.stack.enter_context(patch.dict(os.environ, {"FAKE_BD3D_TITLES": str(self.listing)}))
        original_run = disc.run_process_capture
        self.makemkv_calls: list[list[object]] = []

        def run_process_capture(command: list[object], *args: object, **kwargs: object) -> ProcessResult:
            if kwargs.get("tool_id") == "makemkvcon":
                self.makemkv_calls.append(command)
                return makemkv_result()
            return original_run(command, *args, **kwargs)  # type: ignore[arg-type]

        self.stack.enter_context(patch.object(disc, "run_process_capture", side_effect=run_process_capture))

    def write_listing(self, listing: dict[str, object]) -> None:
        self.listing.write_text(json.dumps(listing), encoding="utf-8")

    def test_unencrypted_disc_lists_each_readable_clip_once_without_makemkv(self) -> None:
        info = disc.get_disc_and_mvc_video_info()

        self.assertEqual(self.makemkv_calls, [])
        self.assertEqual([title.id for title in info.titles], ["bluray:7", "bluray:5"])
        self.assertEqual(info.name, "Rain Forest 3D")
        self.assertEqual((info.resolution, info.frame_rate, info.duration_seconds), ("1920x1080", "23.976", 3240))
        assert info.bluray_title is not None
        self.assertEqual((info.bluray_title.playlist, info.bluray_title.clip_id), (1005, "00007"))
        self.assertEqual([stream.language for stream in info.bluray_title.audio], ["deu", "eng"])
        self.assertEqual([stream.pid for stream in info.bluray_title.subtitles], [4608])
        self.assertFalse(preflight.needs_makemkv())

        extra = disc.get_disc_and_mvc_video_info("bluray:5")
        assert extra.bluray_title is not None
        self.assertEqual(extra.bluray_title.clip_id, "00005")

    def test_encrypted_source_keeps_the_makemkv_rip(self) -> None:
        encrypted = self.root / "encrypted.iso"
        encrypted.write_bytes(b"")
        with patch.object(config, "source_path", encrypted):
            info = disc.get_disc_and_mvc_video_info()
            self.assertTrue(preflight.needs_makemkv())

        self.assertEqual(len(self.makemkv_calls), 1)
        self.assertIsNone(info.bluray_title)
        self.assertEqual([title.id for title in info.titles], ["makemkv:0", "makemkv:1"])

    def test_main_feature_the_reader_cannot_read_keeps_the_makemkv_rip(self) -> None:
        self.write_listing(titles_listing(main_readable=False))

        info = disc.get_disc_and_mvc_video_info()

        self.assertEqual(len(self.makemkv_calls), 1)
        self.assertIsNone(info.bluray_title)

    def test_makemkv_title_from_an_earlier_analysis_keeps_the_makemkv_rip(self) -> None:
        info = disc.get_disc_and_mvc_video_info("makemkv:1")

        self.assertEqual(len(self.makemkv_calls), 1)
        self.assertEqual(info.main_title_number, 1)

    def test_preview_of_a_direct_title_rips_the_makemkv_title_with_the_same_clip(self) -> None:
        with patch.object(config, "preview_range", PreviewRange(0, 30, 60)):
            info = disc.get_disc_and_mvc_video_info("bluray:5")
            self.assertTrue(preflight.needs_makemkv("bluray:5"))

        self.assertEqual(len(self.makemkv_calls), 1)
        self.assertIsNone(info.bluray_title)
        self.assertEqual(info.main_title_number, 1)

    def test_direct_read_moves_audio_and_subtitle_stages_after_the_video_stages(self) -> None:
        with patch.object(config, "skip_subtitles", False), patch.object(config, "fx_upscale", False):
            direct_plan = process.conversion_stage_plan()
            with patch.object(config, "source_path", self.root / "encrypted.iso"):
                (self.root / "encrypted.iso").write_bytes(b"")
                makemkv_plan = process.conversion_stage_plan()

        self.assertNotIn("create_mkv", direct_plan)
        self.assertLess(direct_plan.index("create_left_right_files"), direct_plan.index("extract_mvc_and_audio"))
        self.assertLess(direct_plan.index("extract_subtitles"), direct_plan.index("create_final_file"))
        self.assertIn("create_mkv", makemkv_plan)
        self.assertLess(makemkv_plan.index("extract_subtitles"), makemkv_plan.index("create_left_right_files"))


class DirectBlurayProcessTests(unittest.TestCase):
    def test_resume_after_the_video_stages_still_extracts_audio_and_subtitles(self) -> None:
        # The direct read extracts audio and subtitles after the video stages, so a run that
        # failed while combining has neither; resuming there must extract both, on every route.
        direct_route = ResolvedVideoRoute(
            intent=VideoRouteIntent.AUTOMATIC,
            selected=VideoRouteKind.DIRECT_MV_HEVC,
            reason="test",
            output_mode=VideoMode.MV_HEVC,
            direct_quality=0.7,
        )
        for name, video_mode, route in (
            ("generated MV-HEVC", VideoMode.MV_HEVC, None),
            ("direct MV-HEVC", VideoMode.MV_HEVC, direct_route),
            ("AV1", VideoMode.AV1_SBS, None),
        ):
            with self.subTest(route=name), tempfile.TemporaryDirectory() as temporary_directory, ExitStack() as stack:
                root = Path(temporary_directory)
                source = root / "Movie.iso"
                source.write_bytes(b"")
                output_folder = root / "Movie"
                output_folder.mkdir()
                bluray_disc = parse_titles_output(json.dumps(titles_listing()), source)
                disc_info = disc.direct_disc_info(bluray_disc, None)
                sidecar = output_folder / "Rain Forest 3D_audio_subtitles.ts"
                sidecar.write_bytes(b"sidecar")
                stack.enter_context(patch.object(process, "direct_bluray_disc", return_value=bluray_disc))
                for setting, value in (
                    ("source_path", source),
                    ("source_str", None),
                    ("output_root_path", root),
                    ("overwrite", True),
                    ("keep_files", False),
                    ("audio_mode", AudioMode.PCM),
                    ("video_mode", video_mode),
                    ("fx_upscale", False),
                    ("skip_subtitles", False),
                    ("start_stage", Stage.COMBINE_TO_MV_HEVC),
                    ("preview_range", None),
                    ("remove_original", False),
                ):
                    stack.enter_context(patch.object(process.config, setting, value))
                stack.enter_context(patch.object(process.preflight, "verify_runtime_ready"))
                stack.enter_context(patch.object(process, "get_disc_and_mvc_video_info", return_value=disc_info))
                stack.enter_context(
                    patch.object(process, "prepare_output_folder_for_source", return_value=output_folder)
                )
                stack.enter_context(patch.object(process, "file_exists_normalized", return_value=False))
                stack.enter_context(patch.object(process, "create_excerpt"))
                stack.enter_context(patch.object(process, "get_video_color_depth", return_value=8))
                stack.enter_context(patch.object(process, "detect_crop_parameters", return_value=""))
                stack.enter_context(
                    patch.object(process, "create_left_right_files", return_value=(root / "l.mov", root / "r.mov"))
                )
                stack.enter_context(
                    patch.object(process, "resolve_generated_mv_hevc_expectations", return_value=((1920, 1080), 3240.0))
                )
                stack.enter_context(patch.object(process, "create_mv_hevc_file", return_value=root / "mv.mov"))
                stack.enter_context(patch.object(process, "create_direct_mv_hevc_file", return_value=root / "mv.mov"))
                stack.enter_context(patch.object(process, "create_av1_sbs_file", return_value=root / "sbs.mp4"))
                stack.enter_context(patch.object(process, "create_av1_stereo_file", return_value=root / "av1.mp4"))
                stack.enter_context(patch.object(process, "create_upscaled_file", return_value=root / "mv.mov"))
                stack.enter_context(patch.object(process, "create_transcoded_audio_file", return_value=root / "a.mov"))
                stack.enter_context(patch.object(process, "create_muxed_file", return_value=root / "Movie_AVP.mov"))
                stack.enter_context(patch.object(process, "move_completed_conversion", return_value=root / "o.mov"))
                stack.enter_context(
                    patch.object(container, "get_audio_stream_data", return_value=[{"index": 1, "tags": {}}])
                )
                extract_audio = stack.enter_context(patch.object(container, "extract_mvc_and_audio"))
                extract_subtitles = stack.enter_context(patch.object(sub, "extract_subtitle_to_srt"))
                plan = process.conversion_stage_plan(route)
                if route is None:
                    process.process_each()
                else:
                    process.process_each(video_route=route)

                self.assertEqual(extract_audio.call_args.args[0], sidecar)
                self.assertEqual(extract_subtitles.call_args.args[0], sidecar)
                self.assertIn("extract_mvc_and_audio", plan)
                self.assertIn("extract_subtitles", plan)

    def test_video_pass_streams_from_the_reader_and_audio_and_subtitles_use_its_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "Movie.iso"
            source.write_bytes(b"")
            output_folder = root / "Movie"
            output_folder.mkdir()
            listing = parse_titles_output(json.dumps(titles_listing()), source)
            disc_info = disc.direct_disc_info(listing, None)
            sidecar = output_folder / "Rain Forest 3D_audio_subtitles.ts"
            calls: list[str] = []

            def record(name: str, result: object) -> object:
                def stage(*args: object, **kwargs: object) -> object:
                    calls.append(name)
                    if name == "video":
                        producer = kwargs["mvc_producer"]
                        self.assertEqual(producer.tool_id, READER_TOOL_ID)  # type: ignore[attr-defined]
                        self.assertEqual(
                            [str(part) for part in producer.command[1:]],  # type: ignore[attr-defined]
                            [
                                "stream",
                                str(source),
                                "1005",
                                "--sidecar",
                                str(sidecar),
                                "--audio",
                                "4352,4353",
                                "--subtitles",
                                "4608",
                            ],
                        )
                        sidecar.write_bytes(b"sidecar")
                    if name in {"audio", "subtitles"}:
                        self.assertEqual(args[1] if name == "audio" else args[0], sidecar)
                    return result

                return stage

            with ExitStack() as stack:
                for name, value in (
                    ("source_path", source),
                    ("source_str", None),
                    ("output_root_path", root),
                    ("overwrite", True),
                    ("keep_files", False),
                    ("audio_mode", AudioMode.CONVERT_AAC),
                    ("video_mode", VideoMode.MV_HEVC),
                    ("fx_upscale", False),
                    ("skip_subtitles", False),
                    ("start_stage", Stage.CREATE_MKV),
                    ("preview_range", None),
                    ("remove_original", False),
                ):
                    stack.enter_context(patch.object(process.config, name, value))
                stack.enter_context(patch.object(process.preflight, "verify_runtime_ready"))
                stack.enter_context(patch.object(process, "get_disc_and_mvc_video_info", return_value=disc_info))
                stack.enter_context(
                    patch.object(process, "prepare_output_folder_for_source", return_value=output_folder)
                )
                stack.enter_context(patch.object(process, "file_exists_normalized", return_value=False))
                create_mkv = stack.enter_context(patch.object(process, "create_mkv_file"))
                excerpt = stack.enter_context(
                    patch.object(process, "create_excerpt", side_effect=record("excerpt", None))
                )
                stack.enter_context(patch.object(process, "get_video_color_depth", return_value=8))
                crop = stack.enter_context(patch.object(process, "detect_crop_parameters", return_value=""))
                stack.enter_context(
                    patch.object(
                        process,
                        "create_left_right_files",
                        side_effect=record("video", (root / "left.mov", root / "right.mov")),
                    )
                )
                stack.enter_context(
                    patch.object(process, "resolve_generated_mv_hevc_expectations", return_value=((1920, 1080), 3240.0))
                )
                stack.enter_context(
                    patch.object(process, "create_mv_hevc_file", side_effect=record("combine", root / "mv.mov"))
                )
                stack.enter_context(
                    patch.object(process, "create_mvc_and_audio", side_effect=record("audio", (sidecar, sidecar)))
                )
                stack.enter_context(patch.object(process, "create_srt_from_mkv", side_effect=record("subtitles", None)))
                stack.enter_context(patch.object(process, "create_upscaled_file", return_value=root / "mv.mov"))
                stack.enter_context(patch.object(process, "create_transcoded_audio_file", return_value=root / "a.m4a"))
                stack.enter_context(patch.object(process, "create_muxed_file", return_value=root / "Movie_AVP.mov"))
                stack.enter_context(patch.object(process, "move_completed_conversion", return_value=root / "out.mov"))

                process.process_each()

        create_mkv.assert_not_called()
        self.assertEqual(excerpt.call_args.args[2:4], (output_folder / "Rain Forest 3D_excerpt.m2ts", 600))
        self.assertEqual(crop.call_args.args[0], output_folder / "Rain Forest 3D_excerpt.m2ts")
        self.assertEqual(crop.call_args.kwargs["start_seconds"], 0)
        self.assertEqual(calls, ["excerpt", "video", "combine", "audio", "subtitles"])


def framemd5_frame_lines_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for line in handle:
            if not line.startswith(b"#"):
                digest.update(line)
    return digest.hexdigest()


@unittest.skipUnless(os.environ.get(RAINFOREST_ISO_ENV), f"Set {RAINFOREST_ISO_ENV} to run real-media tests")
class RainforestDirectReadTests(unittest.TestCase):
    source_path: Path
    reader: Path

    @classmethod
    def setUpClass(cls) -> None:
        cls.source_path = Path(os.environ[RAINFOREST_ISO_ENV])
        if not cls.source_path.is_file():
            raise unittest.SkipTest(f"Rainforest ISO is unavailable: {cls.source_path}")
        cls.reader = REPOSITORY_ROOT / "bd_to_avp/bin/bd3d_reader"

    def test_reader_lists_the_main_feature_as_one_readable_clip(self) -> None:
        result = subprocess.run(
            [str(self.reader), "titles", str(self.source_path)], capture_output=True, text=True, check=True, timeout=60
        )
        listing = json.loads(result.stdout)

        main = next(title for title in listing["titles"] if title["main_feature"])
        self.assertEqual((main["clip_id"], main["readable"]), (RAINFOREST_MAIN_CLIP, True))
        self.assertEqual([stream["language"] for stream in main["audio"]], ["deu", "eng"])

    def test_reader_video_decodes_to_the_rainforest_frames_and_sidecar_keeps_every_audio_track(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            sidecar = Path(temporary_directory) / "sidecar.ts"
            framemd5 = Path(temporary_directory) / "first-100.framemd5"
            reader = subprocess.Popen(
                [
                    str(self.reader),
                    "stream",
                    str(self.source_path),
                    RAINFOREST_MAIN_PLAYLIST,
                    "--sidecar",
                    str(sidecar),
                    "--audio",
                    "4352,4353",
                    "--maximum-pairs",
                    "116",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            assert reader.stdout is not None
            edge264 = subprocess.Popen(
                [str(config.EDGE264_TEST_PATH), "-", "-Osk"],
                stdin=reader.stdout,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            reader.stdout.close()
            assert edge264.stdout is not None
            ffmpeg = subprocess.run(
                [
                    str(config.FFMPEG_PATH),
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "yuv4mpegpipe",
                    "-i",
                    "pipe:0",
                    "-frames:v",
                    "100",
                    "-f",
                    "framemd5",
                    "-y",
                    str(framemd5),
                ],
                stdin=edge264.stdout,
                capture_output=True,
                check=False,
                timeout=120,
            )
            edge264.stdout.close()
            edge264_stderr = edge264.communicate(timeout=30)[1]
            reader_stderr = reader.communicate(timeout=30)[1]

            self.assertEqual(ffmpeg.returncode, 0, ffmpeg.stderr.decode())
            self.assertIn(edge264.returncode, {0, -signal.SIGPIPE}, edge264_stderr.decode())
            self.assertIn(reader.returncode, {0, -signal.SIGPIPE}, reader_stderr.decode())
            self.assertEqual(framemd5_frame_lines_sha256(framemd5), RAINFOREST_FIRST_100_FRAME_LINES_SHA256)

            probe = json.loads(
                subprocess.run(
                    [str(config.FFPROBE_PATH), "-v", "error", "-show_streams", "-of", "json", str(sidecar)],
                    capture_output=True,
                    check=True,
                    timeout=60,
                ).stdout
            )
        audio = [
            (stream["codec_name"], stream.get("profile"), stream["tags"]["language"])
            for stream in probe["streams"]
            if stream["codec_type"] == "audio"
        ]
        self.assertEqual(
            audio,
            [("dts", "DTS-HD MA", "deu"), ("dts", "DTS", "deu"), ("dts", "DTS-HD MA", "eng"), ("dts", "DTS", "eng")],
        )

    def test_short_title_converts_without_makemkv(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory, ExitStack() as stack:
            output_root = Path(temporary_directory)
            for name, value in (
                ("source_path", self.source_path),
                ("source_str", None),
                ("source_folder_path", None),
                ("output_root_path", output_root),
                ("overwrite", True),
                ("keep_files", False),
                ("audio_mode", AudioMode.AUTOMATIC),
                ("video_mode", VideoMode.MV_HEVC),
                ("fx_upscale", False),
                ("start_stage", Stage.CREATE_MKV),
                ("preview_range", None),
                ("remove_original", False),
                ("keep_awake", False),
                ("MAKEMKVCON_PATH", output_root / "missing-makemkvcon"),
            ):
                stack.enter_context(patch.object(config, name, value))
            config.configure_tool_environment()

            final_path = process.start_process(selected_title_id=RAINFOREST_SHORT_TITLE_ID)

            assert final_path is not None
            probe = json.loads(
                subprocess.run(
                    [
                        str(config.FFPROBE_PATH),
                        "-v",
                        "error",
                        "-show_streams",
                        "-show_format",
                        "-of",
                        "json",
                        str(final_path),
                    ],
                    capture_output=True,
                    check=True,
                    timeout=60,
                ).stdout
            )
        video = [stream for stream in probe["streams"] if stream["codec_type"] == "video"]
        audio = [stream["tags"]["language"] for stream in probe["streams"] if stream["codec_type"] == "audio"]
        self.assertEqual([stream["codec_name"] for stream in video], ["hevc"])
        self.assertEqual(audio, ["deu", "deu"])
        self.assertAlmostEqual(float(probe["format"]["duration"]), 60.06, delta=1.0)


class TrueHDCoreLanguageTests(unittest.TestCase):
    def test_truehd_core_on_the_same_pid_takes_the_truehd_language_into_every_output(self) -> None:
        streams = [
            {"index": 1, "id": "0x1100", "codec_name": "truehd", "tags": {"language": "deu"}},
            {"index": 2, "id": "0x1100", "codec_name": "ac3"},
            {"index": 3, "id": "0x1102", "codec_name": "eac3"},
        ]
        probe = {"streams": [{"codec_type": "audio", **stream} for stream in streams]}

        with patch("bd_to_avp.modules.container.run_ffprobe", return_value=probe):
            audio = container.get_audio_stream_data(Path("sidecar.ts"))
        options = container.audio_handler_metadata_options(Path("sidecar.ts"), selected_streams=audio)

        self.assertEqual([stream["tags"].get("language") for stream in audio], ["deu", "deu", None])
        self.assertEqual(options["metadata:s:1"], "language=deu")
        self.assertNotIn("metadata:s:2", options)


class MultiClipDirectReadTests(unittest.TestCase):
    """Playlists made of several clips (#1142), read from real discs named by environment.

    Both discs append a short clip non-seamlessly, which MakeMKV lists as a separate title too
    short to keep, so their main feature is the first clip; Fast X's starts a second into it.
    """

    def check_disc(self, environment: str, playlist: str, seconds: float) -> None:
        source = os.environ.get(environment)
        if not source or not Path(source).is_file():
            self.skipTest(f"Set {environment} to a readable disc image to run real-media tests")
        reader = REPOSITORY_ROOT / "bd_to_avp/bin/bd3d_reader"
        listing = json.loads(
            subprocess.run(
                [str(reader), "titles", source], capture_output=True, text=True, check=True, timeout=120
            ).stdout
        )
        main = next(title for title in listing["titles"] if title["main_feature"])
        self.assertEqual((str(main["playlist"]), main["clip_count"], main["readable"]), (playlist, 1, True))
        self.assertAlmostEqual(main["duration_ticks"] / 90_000, seconds, places=2)
        self.assertTrue(disc_reads_directly(parse_titles_output(json.dumps(listing), Path(source))))

        with tempfile.TemporaryDirectory() as temporary_directory:
            framemd5 = Path(temporary_directory) / "first-48.framemd5"
            stream = subprocess.Popen(
                [str(reader), "stream", source, playlist, "--maximum-pairs", "48"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            assert stream.stdout is not None
            edge264 = subprocess.Popen(
                [str(config.EDGE264_TEST_PATH), "-", "-Osk"],
                stdin=stream.stdout,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stream.stdout.close()
            assert edge264.stdout is not None
            ffmpeg = subprocess.run(
                [
                    str(config.FFMPEG_PATH),
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "yuv4mpegpipe",
                    "-i",
                    "pipe:0",
                    "-f",
                    "framemd5",
                    "-y",
                    str(framemd5),
                ],
                stdin=edge264.stdout,
                capture_output=True,
                check=False,
                timeout=300,
            )
            edge264.stdout.close()
            edge264.communicate(timeout=30)
            stream_stderr = stream.communicate(timeout=30)[1]

            self.assertEqual(ffmpeg.returncode, 0, ffmpeg.stderr.decode())
            self.assertEqual(stream.returncode, 0, stream_stderr.decode())
            frames = [line for line in framemd5.read_text().splitlines() if not line.startswith("#")]
            # Each stereo pair decodes to one side-by-side frame from the first one on, even where the
            # playlist enters its clip partway through.
            self.assertEqual(len(frames), 48)

    def test_ocean_dreams_main_feature_reads_directly(self) -> None:
        self.check_disc("BD_TO_AVP_OCEAN_DREAMS_ISO", "22", 5630.875)

    def test_fast_x_main_feature_reads_directly(self) -> None:
        self.check_disc("BD_TO_AVP_FAST_X_ISO", "0", 8449.441)

    def stream_pairs(self, environment: str, playlist: str) -> int:
        source = os.environ.get(environment)
        if not source or not Path(source).is_file():
            self.skipTest(f"Set {environment} to a readable disc image to run real-media tests")
        reader = REPOSITORY_ROOT / "bd_to_avp/bin/bd3d_reader"
        result = subprocess.run(
            [str(reader), "stream", source, playlist],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=True,
            timeout=3600,
        )
        return int(json.loads(result.stderr.decode().strip().splitlines()[-1])["video_pairs"])

    def test_ocean_dreams_main_feature_has_the_frame_count_makemkv_rips(self) -> None:
        # MakeMKV's rip of 00022.mpls has 135,006 frames. The image has 13 zero sectors inside the
        # feature; the reader finds the packets again after them and keeps every frame.
        self.assertEqual(self.stream_pairs("BD_TO_AVP_OCEAN_DREAMS_ISO", "22"), 135_006)

    def test_fast_x_main_feature_has_the_frame_count_makemkv_rips_after_its_in_point(self) -> None:
        # The playlist plays clip 00000 from a second in, to its end, and not the one-second clip
        # it joins non-seamlessly. MakeMKV's rip of the same title has 202,608 frames: these 202,584
        # plus the 24 black frames before the playlist's in point, which it keeps.
        self.assertEqual(self.stream_pairs("BD_TO_AVP_FAST_X_ISO", "0"), 202_584)
