"""Audio, subtitle and final mux checks that run the real MP4Box, ffmpeg, ffprobe and OCR.

The command-shape tests in ``test_container`` mock MP4Box and ffprobe. These
tests run the production audio hand-off, PGS subtitle rip and final mux on tiny
real media and inspect the file a viewer would open, because the user-reported
failures in this area (QuickTime seeking, audio-less sources, missing
subtitles) only show up in real tool output.
"""

import json
import os
import platform
import shutil
import struct
import subprocess
import tempfile
import threading
import unittest
import xml.etree.ElementTree as ElementTree

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

import cv2
import numpy as np

from bd_to_avp.modules import audio, container, sub
from bd_to_avp.modules.audio_mode import AudioMode
from bd_to_avp.modules.config import Stage, config
from bd_to_avp.modules.video_mode import VideoMode
from bd_to_avp.vendor.pgsrip.ocr import AppleVisionOcr, OcrError, OcrWord
from scripts import build_mv_hevc_encoder_macos


TOOL_TIMEOUT_SECONDS = 60
SOURCE_SECONDS = "2"
# 3GPP TS 26.245 / QuickTime text sample entry display flag meaning "all
# samples in this track are forced"; Apple players show such tracks without a
# subtitle selection.
TX3G_ALL_SAMPLES_FORCED = 0x80000000
SUBTITLE_CUES = ("Hello there", "General Kenobi")
FORCED_SUBTITLE_CUES = ("Forced line one", "Forced line two")
# Bitmap cues for the PGS rip; upper case Hershey glyphs keep OCR unambiguous.
PGS_CUES = (("HELLO THERE", 500, 1500), ("GENERAL KENOBI", 2000, 3000))
FORCED_PGS_CUES = (("FORCED WORDS", 500, 1500),)
# Generous bound for a cold Vision OCR model load on a fresh runner.
SUBTITLE_RIP_TIMEOUT_SECONDS = 180
# A source audio title with a space and a colon, which MP4Box option strings use
# as a separator; the final mux must carry it through unchanged.
SOURCE_AUDIO_TITLE = "Main: English Stereo"


@dataclass(frozen=True)
class MuxedTrack:
    handler: str
    sync_sample_count: int | None
    tx3g_display_flags: int | None


def run_tool(command: list[object]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(argument) for argument in command],
        check=True,
        capture_output=True,
        text=True,
        timeout=TOOL_TIMEOUT_SECONDS,
    )


def muxed_tracks(path: Path) -> list[MuxedTrack]:
    """Read track handler, sync sample table and tx3g flags from MP4Box's box dump."""
    dump = run_tool([config.MP4BOX_PATH, "-diso", path, "-std"]).stdout
    root = ElementTree.fromstring(dump)
    tracks = []
    for track in root.findall(".//{*}TrackBox"):
        handler = track.find("{*}MediaBox/{*}HandlerBox")
        assert handler is not None
        sync_samples = track.find(".//{*}SyncSampleBox")
        text_entry = track.find(".//{*}Tx3gSampleEntryBox")
        tracks.append(
            MuxedTrack(
                handler=str(handler.get("hdlrType")),
                sync_sample_count=None if sync_samples is None else int(str(sync_samples.get("EntryCount"))),
                tx3g_display_flags=None if text_entry is None else int(str(text_entry.get("displayFlags")), 16),
            )
        )
    return tracks


def stream_languages(path: Path) -> list[tuple[str, str | None]]:
    """Return (codec type, language) per stream; video language is the encoder's, so it is left out."""
    return [
        (stream["codec_type"], None if stream["codec_type"] == "video" else stream.get("tags", {}).get("language"))
        for stream in probe_streams(path)
    ]


def stream_names(path: Path) -> list[str | None]:
    """Return the track name a player shows for each stream (the MP4 udta name)."""
    return [stream.get("tags", {}).get("name") for stream in probe_streams(path)]


def probe_streams(path: Path) -> list[dict[str, Any]]:
    probe = run_tool(
        [
            config.FFPROBE_PATH,
            "-v",
            "error",
            "-show_entries",
            "stream=index,codec_type:stream_tags=language,name",
            "-of",
            "json",
            path,
        ]
    )
    return list(json.loads(probe.stdout)["streams"])


def subtitle_text(path: Path, subtitle_index: int) -> str:
    return run_tool(
        [config.FFMPEG_PATH, "-v", "error", "-i", path, "-map", f"0:s:{subtitle_index}", "-f", "srt", "-"]
    ).stdout


def write_srt(path: Path, cues: tuple[str, ...]) -> None:
    blocks = [
        f"{number}\n00:00:0{number - 1},000 --> 00:00:0{number - 1},500\n{text}\n"
        for number, text in enumerate(cues, start=1)
    ]
    path.write_text("\n".join(blocks), encoding="utf-8")


def pgs_segment(segment_type: int, milliseconds: int, payload: bytes) -> bytes:
    timestamp = milliseconds * 90
    return b"PG" + struct.pack(">IIBH", timestamp, timestamp, segment_type, len(payload)) + payload


def pgs_rle_line(row: np.ndarray) -> bytes:
    encoded = bytearray()
    position = 0
    while position < len(row):
        color = int(row[position])
        end = position
        while end < len(row) and row[end] == color and end - position < 0x3FFF:
            end += 1
        length = end - position
        if color == 0:
            encoded += bytes([0, length]) if length < 64 else bytes([0, 0x40 | (length >> 8), length & 0xFF])
        elif length == 1:
            encoded.append(color)
        elif length < 64:
            encoded += bytes([0, 0x80 | length, color])
        else:
            encoded += bytes([0, 0xC0 | (length >> 8), length & 0xFF, color])
        position = end
    return bytes(encoded) + b"\x00\x00"


def pgs_display_sets(text: str, start_ms: int, end_ms: int, composition: int, *, forced: bool) -> bytes:
    """Encode one Blu-ray PGS cue (show and clear display sets) per the HDMV PG stream layout."""
    video_width, video_height = 1280, 720
    bitmap = np.zeros((80, 640), np.uint8)
    cv2.putText(bitmap, text, (10, 56), cv2.FONT_HERSHEY_SIMPLEX, 1.5, 1, 3, cv2.LINE_8)
    height, width = bitmap.shape
    x, y = (video_width - width) // 2, video_height - height - 40
    object_flags = 0x40 if forced else 0
    epoch_start = struct.pack(">HHBHBBBB", video_width, video_height, 0x10, composition, 0x80, 0, 0, 1)
    composition_object = struct.pack(">HBBHH", 0, 0, object_flags, x, y)
    window = bytes([1, 0]) + struct.pack(">HHHH", x, y, width, height)
    # Palette 0 transparent, palette 1 opaque white (Y, Cr, Cb, alpha).
    palette = bytes([0, 0, 0, 16, 128, 128, 0, 1, 235, 128, 128, 255])
    rle = b"".join(pgs_rle_line(row) for row in bitmap)
    picture = struct.pack(">HBB", 0, 0, 0xC0) + (len(rle) + 4).to_bytes(3, "big") + struct.pack(">HH", width, height)
    clear = struct.pack(">HHBHBBBB", video_width, video_height, 0x10, composition + 1, 0, 0, 0, 0)
    return b"".join(
        [
            pgs_segment(0x16, start_ms, epoch_start + composition_object),
            pgs_segment(0x17, start_ms, window),
            pgs_segment(0x14, start_ms, palette),
            pgs_segment(0x15, start_ms, picture + rle),
            pgs_segment(0x80, start_ms, b""),
            pgs_segment(0x16, end_ms, clear),
            pgs_segment(0x17, end_ms, window),
            pgs_segment(0x80, end_ms, b""),
        ]
    )


def write_sup(path: Path, cues: tuple[tuple[str, int, int], ...], *, forced: bool) -> None:
    path.write_bytes(
        b"".join(
            pgs_display_sets(text, start, end, index * 2, forced=forced)
            for index, (text, start, end) in enumerate(cues)
        )
    )


def srt_cue_texts(path: Path) -> list[str]:
    blocks = [block.splitlines() for block in path.read_text(encoding="utf-8").strip().split("\n\n")]
    return [" ".join(block[2:]).strip().upper() for block in blocks if len(block) > 2]


def make_source_mkv(path: Path, *, with_audio: bool) -> None:
    command: list[object] = [
        config.FFMPEG_PATH,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=64x64:rate=24",
    ]
    if with_audio:
        command += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"]
    command += ["-t", SOURCE_SECONDS, "-c:v", "mpeg4"]
    if with_audio:
        command += [
            "-c:a",
            "pcm_s16le",
            "-metadata:s:a:0",
            "language=eng",
            "-metadata:s:a:0",
            f"title={SOURCE_AUDIO_TITLE}",
        ]
    command += ["-y", path]
    run_tool(command)


def real_tools_available() -> bool:
    return (
        platform.system() == "Darwin"
        and platform.machine() == "arm64"
        and shutil.which("xcrun") is not None
        and config.MP4BOX_PATH.is_file()
        and config.FFMPEG_PATH.is_file()
        and config.FFPROBE_PATH.is_file()
    )


@unittest.skipUnless(
    real_tools_available(),
    "real final mux tests require macOS arm64, Xcode, MP4Box, ffmpeg and ffprobe",
)
class FinalMuxRealToolTests(unittest.TestCase):
    temporary_directory: tempfile.TemporaryDirectory[str]
    mv_hevc_path: Path
    source_sync_samples: int
    audio_mkv_path: Path
    silent_mkv_path: Path

    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary_directory = tempfile.TemporaryDirectory(prefix="final-mux-real-tools-")
        try:
            root = Path(cls.temporary_directory.name)
            encoder = root / "mv-hevc-encoder"
            build_mv_hevc_encoder_macos.build_encoder(encoder)
            probe = subprocess.run(
                [str(encoder), "--capability-probe"],
                capture_output=True,
                timeout=TOOL_TIMEOUT_SECONDS,
            )
            if probe.returncode == 2 and json.loads(probe.stdout).get("stereo_mv_hevc_encode_supported") is False:
                raise unittest.SkipTest("this Mac cannot create the bounded MV-HEVC fixture")
            if probe.returncode != 0:
                raise RuntimeError(f"MV-HEVC capability probe failed:\n{probe.stderr.decode(errors='replace')}")

            cls.mv_hevc_path = root / "Movie_MV-HEVC.mov"
            side_by_side = subprocess.run(
                [
                    str(config.FFMPEG_PATH),
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=128x64:rate=24",
                    "-t",
                    SOURCE_SECONDS,
                    "-pix_fmt",
                    "yuv420p",
                    "-f",
                    "yuv4mpegpipe",
                    "-",
                ],
                check=True,
                capture_output=True,
                timeout=TOOL_TIMEOUT_SECONDS,
            ).stdout
            subprocess.run(
                [str(encoder), "--output", str(cls.mv_hevc_path)],
                input=side_by_side,
                check=True,
                capture_output=True,
                timeout=TOOL_TIMEOUT_SECONDS,
            )
            (source_track,) = muxed_tracks(cls.mv_hevc_path)
            if source_track.sync_sample_count is None or source_track.sync_sample_count < 2:
                raise RuntimeError(
                    f"MV-HEVC fixture needs several sync samples to test seeking; got {source_track.sync_sample_count}"
                )
            cls.source_sync_samples = source_track.sync_sample_count

            cls.audio_mkv_path = root / "with-audio.mkv"
            cls.silent_mkv_path = root / "no-audio.mkv"
            make_source_mkv(cls.audio_mkv_path, with_audio=True)
            make_source_mkv(cls.silent_mkv_path, with_audio=False)
        except BaseException:
            cls.temporary_directory.cleanup()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary_directory.cleanup()

    def convert_audio_and_mux(self, source_mkv: Path, output_folder: Path, audio_mode: AudioMode) -> Path:
        """Run the production audio hand-off and final mux exactly as process.py chains them."""
        with (
            patch.object(config, "audio_mode", audio_mode),
            patch.object(config, "audio_preferred_language", None),
            patch.object(config, "keep_files", False),
            patch.object(config, "start_stage", Stage.CREATE_MKV),
            patch.object(config, "video_mode", VideoMode.MV_HEVC),
        ):
            audio_path, _ = container.create_mvc_and_audio("Movie", source_mkv, output_folder)
            audio_path = audio.create_transcoded_audio_file(audio_path, output_folder)
            return container.create_muxed_file(audio_path, self.mv_hevc_path, output_folder, "Movie")

    def test_final_mux_is_seekable_with_audio_and_forced_and_regular_subtitles(self) -> None:
        with tempfile.TemporaryDirectory(dir=self.temporary_directory.name) as folder:
            output_folder = Path(folder)
            regular_srt = output_folder / "Movie.en.srt"
            write_srt(regular_srt, SUBTITLE_CUES)
            write_srt(regular_srt.with_stem(sub.forced_subtitle_stem(regular_srt)), FORCED_SUBTITLE_CUES)

            muxed_path = self.convert_audio_and_mux(self.audio_mkv_path, output_folder, AudioMode.AUTOMATIC)

            tracks = muxed_tracks(muxed_path)
            self.assertEqual([track.handler for track in tracks], ["vide", "soun", "sbtl", "sbtl"])
            # QuickTime and Vision Pro seek to sync samples; collapsing them to one
            # makes scrubbing decode from the start of the movie (#10, #29).
            self.assertEqual(tracks[0].sync_sample_count, self.source_sync_samples)

            self.assertEqual(
                stream_languages(muxed_path),
                [("video", None), ("audio", "eng"), ("subtitle", "eng"), ("subtitle", "eng")],
            )

            forced_flags = []
            for subtitle_index, track in enumerate(tracks[2:]):
                assert track.tx3g_display_flags is not None
                forced = bool(track.tx3g_display_flags & TX3G_ALL_SAMPLES_FORCED)
                forced_flags.append(forced)
                text = subtitle_text(muxed_path, subtitle_index)
                for cue in FORCED_SUBTITLE_CUES if forced else SUBTITLE_CUES:
                    self.assertIn(cue, text, f"subtitle track {subtitle_index} (forced={forced})")
            # Only the track ripped from a forced source track is marked forced.
            self.assertEqual(sorted(forced_flags), [False, True])

            # Players list tracks by these names; they must read back exactly,
            # with no quote marks from the mux command (#846).
            subtitle_names = ["English Forced Subtitles" if forced else "English Subtitles" for forced in forced_flags]
            self.assertEqual(stream_names(muxed_path), [None, SOURCE_AUDIO_TITLE, *subtitle_names])

    def test_real_probe_reports_audio_streams_only_when_the_source_has_audio(self) -> None:
        self.assertEqual(container.get_audio_stream_data(self.silent_mkv_path), [])
        self.assertEqual(
            [stream["codec_type"] for stream in container.get_audio_stream_data(self.audio_mkv_path)],
            ["audio"],
        )

    def test_audio_less_source_produces_seekable_video_only_file_in_every_audio_mode(self) -> None:
        # Sources without audio failed to convert (#502); every mode must hand the
        # final mux nothing to add and still keep the video seekable.
        for audio_mode in AudioMode:
            with (
                self.subTest(audio_mode=audio_mode),
                tempfile.TemporaryDirectory(dir=self.temporary_directory.name) as folder,
            ):
                muxed_path = self.convert_audio_and_mux(self.silent_mkv_path, Path(folder), audio_mode)

                tracks = muxed_tracks(muxed_path)
                self.assertEqual([track.handler for track in tracks], ["vide"])
                self.assertEqual(tracks[0].sync_sample_count, self.source_sync_samples)

    def test_audio_source_keeps_its_audio_track_in_every_audio_mode(self) -> None:
        for audio_mode in AudioMode:
            with (
                self.subTest(audio_mode=audio_mode),
                tempfile.TemporaryDirectory(dir=self.temporary_directory.name) as folder,
            ):
                muxed_path = self.convert_audio_and_mux(self.audio_mkv_path, Path(folder), audio_mode)

                self.assertEqual(stream_languages(muxed_path), [("video", None), ("audio", "eng")])

    def test_pgs_subtitles_rip_to_srt_and_reach_the_final_mux_with_forced_flag(self) -> None:
        # Probe actual OCR even on the affected runner, so a recovered image
        # resumes full coverage. Only its recorded false/no-NSError failure
        # may skip; other failures remain fatal.
        reason = os.environ.get("BD_TO_AVP_HOSTED_VISION_OCR_SKIP_REASON")
        if not reason or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted":
            self.check_pgs_subtitles_reach_final_mux()
            return

        failures: list[OcrError] = []
        recognize = AppleVisionOcr.image_to_data

        def capture_failure(backend: AppleVisionOcr, image: np.ndarray, language: Any = None) -> dict[str, list[Any]]:
            try:
                return recognize(backend, image, language)
            except OcrError as error:
                failures.append(error)
                raise

        with patch.object(AppleVisionOcr, "image_to_data", new=capture_failure):
            try:
                self.check_pgs_subtitles_reach_final_mux()
            except AssertionError:
                if failures and all(str(error) == "Apple Vision OCR failed: None" for error in failures):
                    self.skipTest(reason)
                raise

    def test_real_pgs_extraction_and_mux_with_bitmap_checked_ocr(self) -> None:
        # Keep the real PGS container, decode, SRT and mux path exercised even
        # where the OS OCR backend cannot run. Only recognition is substituted;
        # its answer depends on receiving the exact decoded fixture bitmap.
        def recognize(image: np.ndarray, _language: object = None) -> dict[str, list[Any]]:
            for text, _, _ in (*PGS_CUES, *FORCED_PGS_CUES):
                bitmap = np.zeros((80, 640), np.uint8)
                cv2.putText(bitmap, text, (10, 56), cv2.FONT_HERSHEY_SIMPLEX, 1.5, 1, 3, cv2.LINE_8)
                expected = np.where(bitmap, 235, 255).astype(np.uint8)
                if np.array_equal(image, expected):
                    return AppleVisionOcr._tsv_data_from_words([OcrWord(text, 100, 10, 10, 600, 60, 1, 1)])
            raise AssertionError("OCR received an unexpected decoded PGS bitmap")

        with patch.object(AppleVisionOcr, "image_to_data", side_effect=recognize):
            self.check_pgs_subtitles_reach_final_mux()

    def check_pgs_subtitles_reach_final_mux(self) -> None:
        # Subtitle tracks went missing between the disc and the output (#19, #21,
        # #28, #458); follow real PGS tracks through rip, OCR and mux.
        with tempfile.TemporaryDirectory(dir=self.temporary_directory.name) as folder:
            output_folder = Path(folder)
            regular_sup = output_folder / "regular.sup"
            forced_sup = output_folder / "forced.sup"
            write_sup(regular_sup, PGS_CUES, forced=False)
            write_sup(forced_sup, FORCED_PGS_CUES, forced=True)
            subtitle_mkv = Path(self.temporary_directory.name) / f"{output_folder.name}-subtitles.mkv"
            run_tool(
                [
                    config.FFMPEG_PATH,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=64x64:rate=24",
                    "-i",
                    regular_sup,
                    "-i",
                    forced_sup,
                    "-t",
                    "4",
                    "-map",
                    "0:v",
                    "-map",
                    "1",
                    "-map",
                    "2",
                    "-c:v",
                    "mpeg4",
                    "-c:s",
                    "copy",
                    "-metadata:s:s:0",
                    "language=eng",
                    "-metadata:s:s:1",
                    "language=eng",
                    "-disposition:s:1",
                    "forced",
                    "-y",
                    subtitle_mkv,
                ]
            )
            regular_sup.unlink()
            forced_sup.unlink()

            warnings: list[str] = []
            failures: list[Exception] = []

            def rip() -> None:
                try:
                    sub.extract_subtitle_to_srt(subtitle_mkv, output_folder, warnings.append)
                except Exception as error:
                    failures.append(error)

            with (
                patch.object(config, "skip_subtitles", False),
                patch.object(config, "remove_extra_languages", False),
                patch.object(config, "start_stage", Stage.CREATE_MKV),
            ):
                rip_thread = threading.Thread(target=rip, daemon=True)
                rip_thread.start()
                rip_thread.join(SUBTITLE_RIP_TIMEOUT_SECONDS)
            self.assertFalse(rip_thread.is_alive(), "PGS subtitle rip did not finish")
            if failures:
                raise failures[0]
            self.assertEqual(warnings, [])

            srt_files = sorted(output_folder.glob("*.srt"))
            forced_files = [path for path in srt_files if ".forced." in path.stem]
            regular_files = [path for path in srt_files if ".forced." not in path.stem]
            self.assertEqual(len(forced_files), 1, srt_files)
            self.assertEqual(len(regular_files), 1, srt_files)
            self.assertEqual(srt_cue_texts(regular_files[0]), [text for text, _, _ in PGS_CUES])
            self.assertEqual(srt_cue_texts(forced_files[0]), [text for text, _, _ in FORCED_PGS_CUES])

            muxed_path = self.convert_audio_and_mux(self.silent_mkv_path, output_folder, AudioMode.AUTOMATIC)

            tracks = muxed_tracks(muxed_path)
            self.assertEqual([track.handler for track in tracks], ["vide", "sbtl", "sbtl"])
            forced_flags = []
            for subtitle_index, track in enumerate(tracks[1:]):
                assert track.tx3g_display_flags is not None
                forced = bool(track.tx3g_display_flags & TX3G_ALL_SAMPLES_FORCED)
                forced_flags.append(forced)
                expected_cues = FORCED_PGS_CUES if forced else PGS_CUES
                text = subtitle_text(muxed_path, subtitle_index).upper()
                for cue, _, _ in expected_cues:
                    self.assertIn(cue, text, f"subtitle track {subtitle_index} (forced={forced})")
            self.assertEqual(sorted(forced_flags), [False, True])


if __name__ == "__main__":
    unittest.main()
