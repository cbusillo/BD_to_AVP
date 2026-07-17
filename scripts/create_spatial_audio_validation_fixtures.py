from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from bd_to_avp.modules.audio import create_prepared_audio_file
from bd_to_avp.modules.audio_mode import AudioMode
from bd_to_avp.modules.config import Stage, config
from bd_to_avp.modules.container import extract_mvc_and_audio, mux_video_audio_subs


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = Path.home() / "Movies" / "BD to AVP Audio Validation"
DURATION_SECONDS = 24
VIDEO_WIDTH = 640
VIDEO_HEIGHT = 360
FRAME_RATE = 30


@dataclass(frozen=True)
class FixtureCase:
    filename: str
    label: str
    mode: AudioMode
    source_name: str
    expected_codecs: tuple[str, str]
    expected_action: str


@dataclass(frozen=True)
class CapturedWarning:
    message: str
    stage: str | None
    fields: dict[str, object]


class WarningRecorder:
    def __init__(self) -> None:
        self.warnings: list[CapturedWarning] = []

    def warning(self, message: str, *, stage: str | None = None, **fields: object) -> None:
        self.warnings.append(CapturedWarning(message=message, stage=stage, fields=fields))


FIXTURE_CASES = (
    FixtureCase(
        filename="01-Automatic-AAC-Copy.mov",
        label="Automatic — qualified AAC copy",
        mode=AudioMode.AUTOMATIC,
        source_name="qualified-aac.mkv",
        expected_codecs=("aac", "aac"),
        expected_action="copy_aac",
    ),
    FixtureCase(
        filename="02-Automatic-AAC-Fallback.mov",
        label="Automatic — whole-set AAC fallback",
        mode=AudioMode.AUTOMATIC,
        source_name="mixed-ac3-aac.mkv",
        expected_codecs=("aac", "aac"),
        expected_action="convert_aac",
    ),
    FixtureCase(
        filename="03-Convert-AAC.mov",
        label="Convert AAC — explicit whole-set conversion",
        mode=AudioMode.CONVERT_AAC,
        source_name="eac3-flac.mkv",
        expected_codecs=("aac", "aac"),
        expected_action="convert_aac",
    ),
    FixtureCase(
        filename="04-PCM.mov",
        label="PCM — lossless extraction",
        mode=AudioMode.PCM,
        source_name="eac3-flac.mkv",
        expected_codecs=("pcm_s24le", "pcm_s24le"),
        expected_action="extract_pcm",
    ),
)


def run(command: list[str | Path], *, capture_output: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(part) for part in command],
        check=True,
        cwd=REPO_ROOT,
        text=True,
        capture_output=capture_output,
    )


def require_tools() -> None:
    required_tools = (
        config.FFMPEG_PATH,
        config.FFPROBE_PATH,
        config.MP4BOX_PATH,
        config.SPATIAL_MEDIA_PATH,
    )
    missing = [path for path in required_tools if not path.is_file()]
    if missing:
        raise RuntimeError("Required validation tools are missing:\n" + "\n".join(str(path) for path in missing))


def prepare_output_directory(output_directory: Path, force: bool) -> None:
    resolved_output = output_directory.expanduser().resolve()
    if resolved_output in {Path("/"), Path.home().resolve()}:
        raise ValueError(f"Refusing to use unsafe output directory: {resolved_output}")
    if resolved_output.exists():
        if not force:
            raise FileExistsError(f"Output directory already exists; pass --force to replace it: {resolved_output}")
        shutil.rmtree(resolved_output)
    resolved_output.mkdir(parents=True)


def create_spatial_video(work_directory: Path) -> Path:
    left_path = work_directory / "left.mov"
    right_path = work_directory / "right.mov"
    spatial_path = work_directory / "spatial-video.mov"
    flash_filter = "drawbox=x=0:y=0:w=iw:h=ih:color=white@0.55:t=fill:enable='lt(mod(t,1),0.12)'"

    for output_path, crop_x in ((left_path, 0), (right_path, 16)):
        run(
            [
                config.FFMPEG_PATH,
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"testsrc2=size={VIDEO_WIDTH + 16}x{VIDEO_HEIGHT}:rate={FRAME_RATE}",
                "-t",
                str(DURATION_SECONDS),
                "-vf",
                f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT}:{crop_x}:0,{flash_filter}",
                "-c:v",
                "hevc_videotoolbox",
                "-tag:v",
                "hvc1",
                "-b:v",
                "2M",
                "-an",
                "-y",
                output_path,
            ]
        )

    run(
        [
            config.SPATIAL_MEDIA_PATH,
            "merge",
            "--left-file",
            left_path,
            "--right-file",
            right_path,
            "--quality",
            "60",
            "--left-is-primary",
            "--horizontal-field-of-view",
            "90",
            "--horizontal-disparity-adjustment",
            "0",
            "--output-file",
            spatial_path,
        ]
    )
    return spatial_path


def create_audio_beds(work_directory: Path) -> tuple[Path, Path]:
    surround_path = work_directory / "surround-5.1.wav"
    alternate_path = work_directory / "alternate-stereo.wav"
    channel_frequencies = (330, 440, 550, 110, 660, 770)
    surround_expressions = [
        f"0.32*sin(2*PI*{frequency}*t)*between(mod(t\\,6)\\,{index}\\,{index + 1})*lt(mod(t\\,1)\\,0.12)"
        for index, frequency in enumerate(channel_frequencies)
    ]
    stereo_expressions = (
        "0.26*sin(2*PI*990*t)*lt(mod(t\\,2)\\,1)*lt(mod(t\\,1)\\,0.12)",
        "0.26*sin(2*PI*1320*t)*between(mod(t\\,2)\\,1\\,2)*lt(mod(t\\,1)\\,0.12)",
    )

    run(
        [
            config.FFMPEG_PATH,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"aevalsrc={'|'.join(surround_expressions)}:s=48000:d={DURATION_SECONDS}:c=5.1",
            "-c:a",
            "pcm_s24le",
            "-y",
            surround_path,
        ]
    )
    run(
        [
            config.FFMPEG_PATH,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"aevalsrc={'|'.join(stereo_expressions)}:s=48000:d={DURATION_SECONDS}:c=stereo",
            "-c:a",
            "pcm_s24le",
            "-y",
            alternate_path,
        ]
    )
    return surround_path, alternate_path


def create_source_audio(
    output_path: Path,
    surround_path: Path,
    alternate_path: Path,
    codecs: tuple[str, str],
    bitrates: tuple[str | None, str | None],
) -> None:
    command: list[str | Path] = [
        config.FFMPEG_PATH,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        surround_path,
        "-i",
        alternate_path,
        "-map",
        "0:a:0",
        "-map",
        "1:a:0",
        "-c:a:0",
        codecs[0],
        "-c:a:1",
        codecs[1],
    ]
    for index, bitrate in enumerate(bitrates):
        if bitrate is not None:
            command.extend([f"-b:a:{index}", bitrate])
    command.extend(
        [
            "-metadata:s:a:0",
            "language=eng",
            "-metadata:s:a:0",
            "title=Validation Main 5.1",
            "-metadata:s:a:1",
            "language=fra",
            "-metadata:s:a:1",
            "title=Validation Alternate Stereo",
            "-disposition:a:0",
            "default",
            "-disposition:a:1",
            "0",
            "-y",
            output_path,
        ]
    )
    run(command)


def create_source_set(work_directory: Path) -> dict[str, Path]:
    surround_path, alternate_path = create_audio_beds(work_directory)
    source_directory = work_directory / "sources"
    source_directory.mkdir()
    sources = {
        "qualified-aac.mkv": source_directory / "qualified-aac.mkv",
        "mixed-ac3-aac.mkv": source_directory / "mixed-ac3-aac.mkv",
        "eac3-flac.mkv": source_directory / "eac3-flac.mkv",
    }
    create_source_audio(sources["qualified-aac.mkv"], surround_path, alternate_path, ("aac", "aac"), ("384k", "192k"))
    create_source_audio(sources["mixed-ac3-aac.mkv"], surround_path, alternate_path, ("ac3", "aac"), ("448k", "192k"))
    create_source_audio(sources["eac3-flac.mkv"], surround_path, alternate_path, ("eac3", "flac"), ("768k", None))
    return sources


def srt_timestamp(seconds: int) -> str:
    return f"00:00:{seconds:02d},000"


def write_subtitles(path: Path, case: FixtureCase) -> None:
    channels = ("Front Left", "Front Right", "Center", "LFE", "Surround Left", "Surround Right")
    cues = []
    for second in range(DURATION_SECONDS):
        channel = channels[second % len(channels)]
        cues.append(
            "\n".join(
                [
                    str(second + 1),
                    f"{srt_timestamp(second)} --> {srt_timestamp(second + 1)}",
                    case.label,
                    f"Default 5.1 track: {channel} beep should align with the white flash",
                ]
            )
        )
    path.write_text("\n\n".join(cues) + "\n", encoding="utf-8")


def probe(path: Path) -> dict[str, Any]:
    result = run(
        [
            config.FFPROBE_PATH,
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            path,
        ],
        capture_output=True,
    )
    return json.loads(result.stdout)


def packet_fingerprint(path: Path, audio_index: int) -> dict[str, object]:
    result = run(
        [
            config.FFPROBE_PATH,
            "-v",
            "error",
            "-select_streams",
            f"a:{audio_index}",
            "-show_packets",
            "-show_entries",
            "packet=data_hash",
            "-show_data_hash",
            "sha256",
            "-of",
            "json",
            path,
        ],
        capture_output=True,
    )
    packets = json.loads(result.stdout).get("packets", [])
    packet_hashes = [packet["data_hash"] for packet in packets if packet.get("data_hash")]
    digest = hashlib.sha256("\n".join(packet_hashes).encode()).hexdigest()
    return {"packet_count": len(packet_hashes), "sha256": digest}


def decoded_audio_fingerprint(path: Path, audio_index: int) -> str:
    result = run(
        [
            config.FFMPEG_PATH,
            "-v",
            "error",
            "-i",
            path,
            "-map",
            f"0:a:{audio_index}",
            "-c:a",
            "pcm_s24le",
            "-f",
            "hash",
            "-hash",
            "sha256",
            "-",
        ],
        capture_output=True,
    )
    return result.stdout.strip().removeprefix("SHA256=")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as input_file:
        for block in iter(lambda: input_file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def audio_streams(probe_data: dict[str, Any]) -> list[dict[str, Any]]:
    return [stream for stream in probe_data.get("streams", []) if stream.get("codec_type") == "audio"]


def stream_summary(stream: dict[str, Any]) -> dict[str, object]:
    tags = stream.get("tags", {})
    disposition = stream.get("disposition", {})
    return {
        "codec": stream.get("codec_name"),
        "profile": stream.get("profile"),
        "sample_rate": stream.get("sample_rate"),
        "channels": stream.get("channels"),
        "channel_layout": stream.get("channel_layout"),
        "language": tags.get("language"),
        "title": tags.get("title") or tags.get("handler_name"),
        "default": disposition.get("default"),
    }


def mp4box_track_section(path: Path, track_number: int) -> str:
    result = run([config.MP4BOX_PATH, "-info", path], capture_output=True)
    output = result.stdout + result.stderr
    marker = f"# Track {track_number} Info"
    section = output.partition(marker)[2]
    if not section:
        raise RuntimeError(f"{path.name}: MP4Box did not report track {track_number}")
    return section.partition("# Track ")[0]


def validate_fixture(
    case: FixtureCase,
    source_path: Path,
    prepared_audio_path: Path,
    final_path: Path,
    recorder: WarningRecorder,
    case_directory: Path,
) -> dict[str, object]:
    source_probe = probe(source_path)
    prepared_probe = probe(prepared_audio_path)
    final_probe = probe(final_path)
    prepared_audio_streams = audio_streams(prepared_probe)
    final_audio_streams = audio_streams(final_probe)
    final_video_streams = [stream for stream in final_probe.get("streams", []) if stream.get("codec_type") == "video"]
    final_subtitle_streams = [
        stream for stream in final_probe.get("streams", []) if stream.get("codec_type") == "subtitle"
    ]

    actual_codecs = tuple(str(stream.get("codec_name")) for stream in prepared_audio_streams)
    if actual_codecs != case.expected_codecs:
        raise RuntimeError(f"{case.filename}: expected prepared codecs {case.expected_codecs}, found {actual_codecs}")
    if tuple(str(stream.get("codec_name")) for stream in final_audio_streams) != case.expected_codecs:
        raise RuntimeError(f"{case.filename}: final mux changed the expected audio codecs")
    if [stream.get("channels") for stream in final_audio_streams] != [6, 2]:
        raise RuntimeError(f"{case.filename}: expected 5.1 and stereo audio tracks")
    if [stream.get("tags", {}).get("language") for stream in final_audio_streams] != ["eng", "fra"]:
        raise RuntimeError(f"{case.filename}: expected English and French audio metadata")
    expected_titles = ["Validation Main 5.1", "Validation Alternate Stereo"]
    if [stream_summary(stream)["title"] for stream in prepared_audio_streams] != expected_titles:
        raise RuntimeError(f"{case.filename}: prepared audio did not preserve source track titles")
    if len(final_video_streams) != 1 or final_video_streams[0].get("codec_name") != "hevc":
        raise RuntimeError(f"{case.filename}: expected one HEVC video stream")
    if len(final_subtitle_streams) != 1 or final_subtitle_streams[0].get("codec_name") != "mov_text":
        raise RuntimeError(f"{case.filename}: expected one selectable mov_text subtitle stream")
    final_duration = float(final_probe["format"]["duration"])
    if abs(final_duration - DURATION_SECONDS) > 0.05:
        raise RuntimeError(f"{case.filename}: expected a {DURATION_SECONDS}-second final movie")
    for stream in final_probe.get("streams", []):
        start_time = float(stream.get("start_time", 0))
        duration = float(stream.get("duration", final_duration))
        if abs(start_time) > 0.01 or abs(duration - DURATION_SECONDS) > 0.05:
            raise RuntimeError(f"{case.filename}: stream timing is not aligned for the lip-sync gate")

    main_track_info = mp4box_track_section(final_path, 2)
    alternate_track_info = mp4box_track_section(final_path, 3)
    if "Track flags: Enabled In Movie" not in main_track_info or "Alternate Group ID 1" not in main_track_info:
        raise RuntimeError(f"{case.filename}: main audio track is not the enabled alternate-group default")
    if (
        "Track flags: Disabled In Movie" not in alternate_track_info
        or "Alternate Group ID 1" not in alternate_track_info
    ):
        raise RuntimeError(f"{case.filename}: alternate audio track is not disabled in the audio alternate group")
    if "name: 'Validation Main 5.1'" not in main_track_info:
        raise RuntimeError(f"{case.filename}: final mux did not preserve the main audio title")
    if "name: 'Validation Alternate Stereo'" not in alternate_track_info:
        raise RuntimeError(f"{case.filename}: final mux did not preserve the alternate audio title")

    source_fingerprints = [packet_fingerprint(source_path, index) for index in range(2)]
    prepared_fingerprints = [packet_fingerprint(prepared_audio_path, index) for index in range(2)]
    final_fingerprints = [packet_fingerprint(final_path, index) for index in range(2)]
    prepared_decoded_fingerprints = [decoded_audio_fingerprint(prepared_audio_path, index) for index in range(2)]
    final_decoded_fingerprints = [decoded_audio_fingerprint(final_path, index) for index in range(2)]
    if prepared_decoded_fingerprints != final_decoded_fingerprints:
        raise RuntimeError(f"{case.filename}: final mux changed decoded audio samples")
    if case.mode is not AudioMode.PCM and prepared_fingerprints != final_fingerprints:
        raise RuntimeError(f"{case.filename}: final mux changed prepared audio packet payloads")
    if case.expected_action == "copy_aac" and source_fingerprints != prepared_fingerprints:
        raise RuntimeError(f"{case.filename}: Automatic did not preserve qualified AAC packet payloads")
    if case.filename.startswith("02-") and source_fingerprints[1] == prepared_fingerprints[1]:
        raise RuntimeError(f"{case.filename}: Automatic fallback did not convert the qualified AAC sibling track")

    warning_codes = [warning.fields.get("code") for warning in recorder.warnings]
    if case.filename.startswith("02-"):
        if warning_codes != ["audio_automatic_fallback_to_aac"]:
            raise RuntimeError(f"{case.filename}: missing structured Automatic fallback warning")
    elif warning_codes:
        raise RuntimeError(f"{case.filename}: unexpected warnings: {warning_codes}")

    split_directory = case_directory / "spatial-split"
    split_directory.mkdir()
    run(
        [
            config.SPATIAL_MEDIA_PATH,
            "split",
            "--input-file",
            final_path,
            "--output-dir",
            split_directory,
        ]
    )
    split_outputs = sorted(path.name for path in split_directory.glob("*.mov"))
    if len(split_outputs) != 2:
        raise RuntimeError(f"{case.filename}: spatial split did not produce left and right views")

    return {
        "file": final_path.name,
        "label": case.label,
        "audio_mode": case.mode.value,
        "expected_action": case.expected_action,
        "sha256": file_sha256(final_path),
        "bytes": final_path.stat().st_size,
        "duration_seconds": final_duration,
        "source_audio": [stream_summary(stream) for stream in audio_streams(source_probe)],
        "prepared_audio": [stream_summary(stream) for stream in prepared_audio_streams],
        "final_audio": [stream_summary(stream) for stream in final_audio_streams],
        "source_packet_fingerprints": source_fingerprints,
        "prepared_packet_fingerprints": prepared_fingerprints,
        "prepared_decoded_fingerprints": prepared_decoded_fingerprints,
        "final_decoded_fingerprints": final_decoded_fingerprints,
        "warnings": [asdict(warning) for warning in recorder.warnings],
        "spatial_split_outputs": split_outputs,
        "mp4box_audio_tracks_verified": True,
    }


def build_fixture(
    case: FixtureCase,
    source_path: Path,
    spatial_path: Path,
    output_directory: Path,
    work_directory: Path,
) -> dict[str, object]:
    case_directory = work_directory / case.filename.removesuffix(".mov")
    case_directory.mkdir()
    subtitle_path = case_directory / "validation.eng.srt"
    write_subtitles(subtitle_path, case)
    recorder = WarningRecorder()

    config.audio_mode = case.mode
    config.audio_bitrate = 384
    config.start_stage = Stage.TRANSCODE_AUDIO
    if case.mode is AudioMode.PCM:
        prepared_audio_path = case_directory / "validation_audio_PCM.mov"
        extract_mvc_and_audio(source_path, None, prepared_audio_path)
    else:
        prepared_audio_path = create_prepared_audio_file(source_path, case_directory, recorder)

    final_path = output_directory / case.filename
    mux_video_audio_subs(spatial_path, prepared_audio_path, final_path, case_directory)
    return validate_fixture(case, source_path, prepared_audio_path, final_path, recorder, case_directory)


def render_checklist(manifest_entries: list[dict[str, object]]) -> str:
    fixture_rows = "\n".join(f"| [ ] | `{entry['file']}` | {entry['label']} | Notes: |" for entry in manifest_entries)
    return f"""# Apple Vision Pro Audio Validation

Generated fixtures are {DURATION_SECONDS} seconds long. A white flash occurs once per second.

## Per-Fixture Gate

1. Open the movie and select **Open Spatial View**.
2. Confirm decode support is **Supported**, player and rendering are **Ready**, and actual presentation is **spatial**.
3. Confirm depth is comfortable and not inverted; the left eye must remain primary.
4. Enable English subtitles and play the default English 5.1 track. Each channel beep must align with the white flash.
5. Seek to **Beginning**, **Middle**, and **End** twice. Playback must resume without drift, silence,
   or loss of spatial mode.
6. Switch to the French alternate stereo track. The sound must change to alternating left/right higher-pitched beeps.
7. Switch back to English 5.1 and confirm the six-position sequence returns.

| Pass | Fixture | Pipeline path | Result |
| --- | --- | --- | --- |
{fixture_rows}

## Acceptance Notes

- Automatic copy has no fallback warning and preserves both qualified AAC packet payloads.
- Automatic fallback emits `audio_automatic_fallback_to_aac` and converts both the AC-3 track and its
  qualified AAC sibling.
- Convert AAC converts both E-AC-3 and FLAC source tracks to AAC.
- PCM extracts both source tracks as 24-bit PCM.
- `manifest.json` records hashes, stream metadata, packet fingerprints, warnings, and successful left/right
  spatial splits.

## Final Decision

- [ ] All four fixtures pass spatial presentation, seeking, lip-sync, track switching, and audible surround checks.
- [ ] No fixture produces a playback failure, unexplained silence, or mislabeled media option.

Notes:

"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create the physical Vision Pro audio validation fixture matrix.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--force", action="store_true", help="Replace an existing output directory.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_directory = args.output.expanduser().resolve()
    require_tools()
    prepare_output_directory(output_directory, args.force)
    config.configure_tool_environment()

    work_directory = output_directory / ".work"
    work_directory.mkdir()
    spatial_path = create_spatial_video(work_directory)
    sources = create_source_set(work_directory)
    entries = [
        build_fixture(case, sources[case.source_name], spatial_path, output_directory, work_directory)
        for case in FIXTURE_CASES
    ]
    manifest = {
        "schema_version": 1,
        "duration_seconds": DURATION_SECONDS,
        "video": {
            "codec": "MV-HEVC",
            "dimensions": f"{VIDEO_WIDTH}x{VIDEO_HEIGHT}",
            "frame_rate": FRAME_RATE,
            "left_is_primary": True,
            "horizontal_field_of_view": 90,
            "horizontal_disparity_adjustment": 0,
        },
        "fixtures": entries,
    }
    (output_directory / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (output_directory / "CHECKLIST.md").write_text(render_checklist(entries), encoding="utf-8")
    shutil.rmtree(work_directory)
    print(f"Created and verified {len(entries)} Vision Pro fixtures in {output_directory}")


if __name__ == "__main__":
    main()
