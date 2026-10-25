"""Direct reading of unencrypted Blu-ray 3D images and folders through the shared reader.

``bd3d_reader`` (native/bd3d_reader) compiles the headset's Blu-ray 3D demuxer against the
bundled libbluray and libudfread. A source with an AACS or BD+ folder, a title that is not one
complete 3D clip, or a missing reader keeps today's MakeMKV route.
"""

import json
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from bd_to_avp.modules.command import run_process_capture
from bd_to_avp.modules.config import config
from bd_to_avp.observability import ObservabilityContext
from bd_to_avp.process_runner import CaptureOverflowPolicy, ProcessArtifactProbe, ProcessCancelled, ProcessRunnerError
from bd_to_avp.runtime import RunContext


BLURAY_TITLE_ID_PREFIX = "bluray:"
MAKEMKV_TITLE_ID_PREFIX = "makemkv:"
READER_TOOL_ID = "bd3d_reader"
BD_TICKS_PER_SECOND = 90_000
PRESENTATION_GRAPHICS_CODING_TYPE = 0x90
# Blu-ray video_format and frame_rate codes (BD-ROM part 3, STN stream attributes).
VIDEO_FORMATS = {
    1: ("720x480", True),
    2: ("720x576", True),
    3: ("720x480", False),
    4: ("1920x1080", True),
    5: ("1280x720", False),
    6: ("1920x1080", False),
    7: ("720x576", False),
}
FRAME_RATES = {1: "23.976", 2: "24", 3: "25", 4: "29.97", 6: "50", 7: "59.94"}
CROP_EXCERPT_SECONDS = 15


@dataclass(frozen=True)
class BlurayStream:
    pid: int
    coding_type: int
    language: str | None


@dataclass(frozen=True)
class BlurayTitle:
    playlist: int
    clip_id: str
    duration_seconds: float
    main_feature: bool
    readable: bool
    resolution: str
    frame_rate: str
    interlaced: bool
    audio: tuple[BlurayStream, ...]
    subtitles: tuple[BlurayStream, ...]

    @property
    def clip_number(self) -> int:
        return int(self.clip_id)


@dataclass(frozen=True)
class BlurayDisc:
    name: str
    titles: tuple[BlurayTitle, ...]


def reader_path() -> Path:
    return config.BD3D_READER_PATH


def is_bluray_source(source_path: Path | None) -> bool:
    if source_path is None:
        return False
    if source_path.is_dir():
        return True
    return source_path.suffix.lower() in config.IMAGE_EXTENSIONS


def parse_stream(data: dict[str, object]) -> BlurayStream:
    language = data.get("language")
    return BlurayStream(
        pid=int(str(data["pid"])),
        coding_type=int(str(data["coding_type"])),
        language=language if isinstance(language, str) else None,
    )


def parse_title(data: dict[str, object]) -> BlurayTitle:
    video = data.get("video")
    video_format = int(str(video.get("format", 0))) if isinstance(video, dict) else 0
    video_rate = int(str(video.get("rate", 0))) if isinstance(video, dict) else 0
    resolution, interlaced = VIDEO_FORMATS.get(video_format, ("1920x1080", False))
    audio = data.get("audio")
    subtitles = data.get("subtitles")
    return BlurayTitle(
        playlist=int(str(data["playlist"])),
        clip_id=str(data["clip_id"]),
        duration_seconds=int(str(data["duration_ticks"])) / BD_TICKS_PER_SECOND,
        main_feature=data.get("main_feature") is True,
        readable=data.get("readable") is True,
        resolution=resolution,
        frame_rate=FRAME_RATES.get(video_rate, "23.976"),
        interlaced=interlaced,
        audio=tuple(parse_stream(stream) for stream in audio if isinstance(stream, dict))
        if isinstance(audio, list)
        else (),
        subtitles=tuple(
            parse_stream(stream)
            for stream in subtitles
            if isinstance(stream, dict) and stream.get("coding_type") == PRESENTATION_GRAPHICS_CODING_TYPE
        )
        if isinstance(subtitles, list)
        else (),
    )


def disc_name(volume_id: object, source_path: Path) -> str:
    if isinstance(volume_id, str) and volume_id.strip():
        return volume_id.strip()
    folder = source_path.parent if source_path.name.upper() == "BDMV" else source_path
    return folder.stem


def parse_titles_output(output: str, source_path: Path) -> BlurayDisc:
    document = json.loads(output)
    if not isinstance(document, dict) or document.get("type") != "bd3d.titles":
        raise ValueError("The Blu-ray reader returned an unexpected title listing.")
    titles = document.get("titles")
    return BlurayDisc(
        name=disc_name(document.get("volume_id"), source_path),
        titles=tuple(parse_title(title) for title in titles if isinstance(title, dict))
        if isinstance(titles, list)
        else (),
    )


def read_bluray_disc(
    source_path: Path,
    *,
    run_context: RunContext | None = None,
    cancellation_event: threading.Event | None = None,
    observability_context: ObservabilityContext | None = None,
) -> BlurayDisc | None:
    """Lists the titles of an unencrypted Blu-ray, or returns None when MakeMKV must read it."""
    if not reader_path().is_file():
        return None
    try:
        result = run_process_capture(
            [reader_path(), "titles", source_path],
            "Read Blu-ray titles",
            tool_id=READER_TOOL_ID,
            run_context=run_context,
            cancellation_event=cancellation_event,
            observability_context=observability_context,
        )
        return parse_titles_output(result.stdout.text(), source_path)
    except ProcessCancelled:
        raise
    except (subprocess.CalledProcessError, ProcessRunnerError, OSError, ValueError, KeyError, TypeError):
        return None


def directly_readable_titles(disc: BlurayDisc) -> tuple[BlurayTitle, ...]:
    """One readable title per clip, so a clip's 2D and 3D playlists are listed once."""
    by_clip: dict[str, BlurayTitle] = {}
    for title in disc.titles:
        if not title.readable:
            continue
        current = by_clip.get(title.clip_id)
        if current is None or (title.main_feature, title.playlist) > (current.main_feature, current.playlist):
            by_clip[title.clip_id] = title
    return tuple(by_clip.values())


def disc_reads_directly(disc: BlurayDisc) -> bool:
    """The disc's main feature must be readable; otherwise its movie is a title only MakeMKV reads."""
    return any(title.main_feature and title.readable for title in disc.titles)


def direct_bluray_disc(
    selected_title_id: str | None,
    *,
    run_context: RunContext | None = None,
    cancellation_event: threading.Event | None = None,
    observability_context: ObservabilityContext | None = None,
) -> BlurayDisc | None:
    """The disc to read directly for the configured source, or None for the MakeMKV route."""
    source_path = config.source_path
    if not is_bluray_source(source_path) or source_path is None:
        return None
    if config.preview_range is not None:
        return None
    if selected_title_id is not None and selected_title_id.startswith(MAKEMKV_TITLE_ID_PREFIX):
        return None
    disc = read_bluray_disc(
        source_path,
        run_context=run_context,
        cancellation_event=cancellation_event,
        observability_context=observability_context,
    )
    if disc is None or not disc_reads_directly(disc):
        return None
    return disc


def selected_clip(title_id: str | None) -> int | None:
    """The clip a ``bluray:<clip>`` title ID names; MakeMKV lists the same clip in its segment map."""
    if title_id is None or not title_id.startswith(BLURAY_TITLE_ID_PREFIX):
        return None
    try:
        return int(title_id.removeprefix(BLURAY_TITLE_ID_PREFIX))
    except ValueError:
        return None


def sidecar_path(output_folder: Path, name: str) -> Path:
    return output_folder / f"{name}_audio_subtitles.ts"


def sidecar_stream_arguments(title: BlurayTitle) -> list[str]:
    arguments: list[str] = []
    if title.audio:
        arguments += ["--audio", ",".join(str(stream.pid) for stream in title.audio)]
    if title.subtitles:
        arguments += ["--subtitles", ",".join(str(stream.pid) for stream in title.subtitles)]
    return arguments


def mvc_stream_command(source_path: Path, title: BlurayTitle, sidecar: Path) -> list[str | Path]:
    """Streams the title's MVC video to stdout while writing its audio and subtitles to ``sidecar``.

    The reader is given the playlist, not the clip: playlists that share a clip can differ in
    coverage and streams, and only this playlist was checked as readable.
    """
    return [
        reader_path(),
        "stream",
        source_path,
        str(title.playlist),
        "--sidecar",
        sidecar,
        *sidecar_stream_arguments(title),
    ]


def create_sidecar(
    source_path: Path,
    title: BlurayTitle,
    sidecar: Path,
    *,
    run_context: RunContext | None = None,
    cancellation_event: threading.Event | None = None,
    observability_context: ObservabilityContext | None = None,
) -> None:
    """Writes the audio and subtitle sidecar without encoding video, for a resumed conversion."""
    run_process_capture(
        mvc_stream_command(source_path, title, sidecar),
        "Read Blu-ray audio and subtitles",
        tool_id=READER_TOOL_ID,
        stdout=subprocess.DEVNULL,
        artifacts=(ProcessArtifactProbe("audio_subtitle_sidecar", path=sidecar),),
        run_context=run_context,
        cancellation_event=cancellation_event,
        observability_context=observability_context,
        capture_overflow=CaptureOverflowPolicy.TRUNCATE,
        show_spinner=True,
    )


def create_excerpt(
    source_path: Path,
    title: BlurayTitle,
    output_path: Path,
    start_seconds: int,
    *,
    run_context: RunContext | None = None,
    cancellation_event: threading.Event | None = None,
    observability_context: ObservabilityContext | None = None,
) -> Path:
    """Writes a short 2D excerpt of the title for the color and crop probes."""
    with output_path.open("wb") as output_file:
        run_process_capture(
            [
                reader_path(),
                "excerpt",
                source_path,
                title.playlist,
                max(0, start_seconds),
                CROP_EXCERPT_SECONDS,
            ],
            "Read Blu-ray video excerpt",
            tool_id=READER_TOOL_ID,
            stdout=output_file,
            artifacts=(ProcessArtifactProbe("video_excerpt", path=output_path),),
            run_context=run_context,
            cancellation_event=cancellation_event,
            observability_context=observability_context,
            capture_overflow=CaptureOverflowPolicy.TRUNCATE,
        )
    return output_path
