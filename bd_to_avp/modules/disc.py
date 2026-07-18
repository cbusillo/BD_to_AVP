import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Callable

from bd_to_avp.observability import ObservabilityProgress
from bd_to_avp.modules.config import config, is_direct_pipeline_source_reused, Stage
from bd_to_avp.modules.file import find_largest_file_with_extensions, sanitize_filename
from bd_to_avp.modules.command import run_command, run_ffprobe
from bd_to_avp.process_runner import CaptureOverflowPolicy, ProcessArtifactProbe
from bd_to_avp.runtime import RunContext


class MKVCreationError(Exception):
    pass


class DiscTitleSelectionError(ValueError):
    pass


@dataclass(frozen=True)
class DiscTitleInfo:
    id: str
    title_number: int
    name: str
    output_name: str
    duration_seconds: float
    resolution: str
    frame_rate: str
    main_feature: bool


@dataclass
class DiscInfo:
    name: str = "Unknown"
    frame_rate: str = "23.976"
    resolution: str = "1920x1080"
    color_depth: int = 8
    main_title_number: int = 0
    is_interlaced: bool = False
    duration_seconds: float = 0
    titles: tuple[DiscTitleInfo, ...] = ()


@dataclass
class TitleInfo:
    index: int
    duration: int = 0
    has_mvc: bool = False
    resolution: str | None = None
    frame_rate: str | None = None


ProgressCallback = Callable[[float, float], object]
MAKEMKV_PROGRESS_PATTERN = re.compile(r"^PRGV:(\d+),(\d+),(\d+)$")


def parse_makemkv_progress(line: str) -> tuple[int, int] | None:
    match = MAKEMKV_PROGRESS_PATTERN.match(line.strip())
    if match is None:
        return None
    total_progress = int(match.group(2))
    maximum_progress = int(match.group(3))
    if maximum_progress <= 0:
        return None
    return min(max(total_progress, 0), maximum_progress), maximum_progress


def parse_makemkv_observability_progress(line: str) -> ObservabilityProgress | None:
    progress = parse_makemkv_progress(line)
    if progress is None:
        return None
    completed, total = progress
    return ObservabilityProgress(
        fraction=completed / total,
        completed_units=completed,
        total_units=total,
        unit="makemkv_progress",
    )


def parse_makemkv_output(output: str) -> tuple[str, list[TitleInfo]]:
    titles: dict[int, TitleInfo] = {}
    current_title: int | None = None
    disc_name: str = DiscInfo.name

    for line in output.splitlines():
        if line.startswith("CINFO:2,0,"):
            disc_name_match = re.search(r"CINFO:2,0,\"(.*?)\"", line)
            if disc_name_match:
                disc_name = sanitize_filename(disc_name_match.group(1))
        elif line.startswith("TINFO:"):
            title_match = re.match(r"TINFO:(\d+),", line)
            if title_match:
                current_title = int(title_match.group(1))
                if current_title not in titles:
                    titles[current_title] = TitleInfo(index=current_title)

            duration_match = re.search(r"TINFO:\d+,9,0,\"(\d+:\d+:\d+)\"", line)
            if duration_match and current_title is not None:
                h, m, s = map(int, duration_match.group(1).split(":"))
                titles[current_title].duration = h * 3600 + m * 60 + s
        elif line.startswith("SINFO:"):
            stream_title_match = re.match(r"SINFO:(\d+),", line)
            if not stream_title_match:
                continue
            current_title = int(stream_title_match.group(1))
            if current_title not in titles:
                titles[current_title] = TitleInfo(index=current_title)
            if any(term in line.lower() for term in ["mvc-3d", "mpeg4-mvc", "mvc video", "mvc high", "mpeg4 mvc"]):
                titles[current_title].has_mvc = True
            resolution_match = re.search(r"SINFO:\d+,1,19,0,\"(\d+x\d+)\"", line)
            if resolution_match:
                titles[current_title].resolution = resolution_match.group(1)
            frame_rate_match = re.search(r"SINFO:\d+,1,21,0,\"(.*?)\"", line)
            if frame_rate_match:
                titles[current_title].frame_rate = frame_rate_match.group(1)

    return disc_name, list(titles.values())


def build_disc_title_catalog(disc_name: str, titles: list[TitleInfo]) -> tuple[DiscTitleInfo, ...]:
    mvc_titles = [title for title in titles if title.has_mvc]
    if not mvc_titles:
        raise ValueError("No MVC video found in disc info.")

    main_title = max(mvc_titles, key=lambda title: (title.duration, -title.index))
    ordered_titles = [
        main_title,
        *sorted(
            (title for title in mvc_titles if title.index != main_title.index),
            key=lambda title: (-title.duration, title.index),
        ),
    ]

    catalog: list[DiscTitleInfo] = []
    extra_number = 1
    for title in ordered_titles:
        is_main = title.index == main_title.index
        display_name = "Main Movie" if is_main else f"3D Video {extra_number}"
        output_name = disc_name if is_main else sanitize_filename(f"{disc_name} - {display_name}")
        frame_rate = title.frame_rate or DiscInfo.frame_rate
        if "/" in frame_rate:
            frame_rate = frame_rate.split(" ")[0]
        catalog.append(
            DiscTitleInfo(
                id=f"makemkv:{title.index}",
                title_number=title.index,
                name=display_name,
                output_name=output_name,
                duration_seconds=float(title.duration),
                resolution=title.resolution or DiscInfo.resolution,
                frame_rate=frame_rate,
                main_feature=is_main,
            )
        )
        if not is_main:
            extra_number += 1
    return tuple(catalog)


def select_disc_title(titles: tuple[DiscTitleInfo, ...], title_id: str | None) -> DiscTitleInfo:
    if title_id is None:
        return next(title for title in titles if title.main_feature)
    if selected_title := next((title for title in titles if title.id == title_id), None):
        return selected_title
    raise DiscTitleSelectionError("The selected 3D video is no longer available. Analyze the source again.")


def get_disc_and_mvc_video_info(
    selected_title_id: str | None = None,
    *,
    run_context: RunContext | None = None,
    cancellation_event: Event | None = None,
) -> DiscInfo:
    source_path = config.source_path
    source = source_path.as_posix() if source_path else config.source_str
    if not source:
        raise ValueError("No source path provided.")
    if source_path and source_path.suffix.lower() in {*config.MTS_EXTENSIONS, ".mkv"}:
        filename = source_path.stem
        disc_info = DiscInfo(name=filename)

        probe = run_ffprobe(
            source_path,
            run_context=run_context,
            cancellation_event=cancellation_event,
        )
        streams = probe.get("streams", [])
        ffmpeg_probe_output = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
        if ffmpeg_probe_output is None:
            raise ValueError("No video stream found in source metadata.")
        if all(key in ffmpeg_probe_output for key in ["width", "height"]):
            disc_info.resolution = f"{ffmpeg_probe_output.get('width')}x{ffmpeg_probe_output.get('height')}"
        if "avg_frame_rate" in ffmpeg_probe_output:
            disc_info.frame_rate = ffmpeg_probe_output.get("avg_frame_rate")
        if ffmpeg_probe_output.get("field_order") not in {None, "progressive", "unknown"}:
            disc_info.is_interlaced = True
        duration = probe.get("format", {}).get("duration") or ffmpeg_probe_output.get("duration")
        if duration is not None:
            disc_info.duration_seconds = float(duration)
        return disc_info

    source = get_makemkv_source()

    command = [
        config.MAKEMKVCON_PATH,
        "--robot",
        "--minlength=0",
        "--noscan" if makemkv_source_supports_noscan(source) else None,
        "info",
        source,
    ]
    output = run_command(
        command,
        "Get disc and MVC video properties",
        run_context=run_context,
        cancellation_event=cancellation_event,
        tool_id="makemkvcon",
        capture_overflow=CaptureOverflowPolicy.FAIL,
    )

    disc_name, raw_titles = parse_makemkv_output(output)
    try:
        titles = build_disc_title_catalog(disc_name, raw_titles)
    except ValueError as error:
        if selected_title_id is not None:
            raise DiscTitleSelectionError(
                "The selected 3D video is no longer available. Analyze the source again."
            ) from error
        raise
    selected_title = select_disc_title(titles, selected_title_id)

    return DiscInfo(
        name=selected_title.output_name,
        frame_rate=selected_title.frame_rate,
        resolution=selected_title.resolution,
        main_title_number=selected_title.title_number,
        duration_seconds=selected_title.duration_seconds,
        titles=titles,
    )


def rip_disc_to_mkv(
    output_folder: Path,
    disc_info: DiscInfo,
    progress_callback: ProgressCallback | None = None,
    *,
    run_context: RunContext | None = None,
    cancellation_event: Event | None = None,
) -> None:
    custom_profile_path = output_folder / "custom_profile.mmcp.xml"
    create_custom_makemkv_profile(custom_profile_path)

    source = get_makemkv_source()
    command = [
        config.MAKEMKVCON_PATH,
        "--robot",
        f"--profile={custom_profile_path}",
        "--minlength=0",
        "--progress=-same",
        "--noscan" if makemkv_source_supports_noscan(source) else None,
        "mkv",
        source,
        disc_info.main_title_number,
        output_folder,
    ]

    def report_progress(line: str) -> None:
        if progress_callback is None:
            return
        progress = parse_makemkv_progress(line)
        if progress is not None:
            progress_callback(*progress)

    baseline: dict[Path, tuple[int, int]] = {}
    for candidate in output_folder.glob("*.mkv"):
        try:
            status = candidate.stat()
        except OSError:
            continue
        if candidate.is_file():
            baseline[candidate] = (status.st_size, status.st_mtime_ns)

    def active_mkv() -> Path | None:
        candidates: list[tuple[int, Path]] = []
        for candidate in output_folder.glob("*.mkv"):
            try:
                status = candidate.stat()
            except OSError:
                continue
            previous = baseline.get(candidate)
            if previous is None or previous != (status.st_size, status.st_mtime_ns):
                candidates.append((status.st_mtime_ns, candidate))
        if not candidates:
            return None
        return max(candidates)[1]

    mkv_output = run_command(
        command,
        "Rip disc to MKV file.",
        line_handler=report_progress,
        progress_parser=parse_makemkv_observability_progress,
        run_context=run_context,
        cancellation_event=cancellation_event,
        tool_id="makemkvcon",
        artifacts=(ProcessArtifactProbe("intermediate_mkv", resolver=active_mkv),),
        capture_overflow=CaptureOverflowPolicy.FAIL,
    )
    if config.continue_on_error or all(error not in mkv_output for error in config.MKV_ERROR_CODES):
        return
    filtered_output = filter_lines_from_output(mkv_output, config.MKV_ERROR_FILTERS)

    raise MKVCreationError(f"Error occurred while ripping disc to MKV.\n\n{filtered_output}")


def get_makemkv_source() -> str:
    if config.source_path and config.source_path.suffix.lower() in config.IMAGE_EXTENSIONS:
        return f"iso:{config.source_path}"
    if config.source_path and config.source_path.is_dir():
        return f"file:{config.source_path}"
    if config.source_path:
        return config.source_path.as_posix()
    if config.source_str:
        return config.source_str
    raise ValueError("No source provided.")


def makemkv_source_supports_noscan(source: str) -> bool:
    return not source.startswith(("disc:", "dev:"))


def filter_lines_from_output(output: str, filter_strings: list[str]) -> str:
    output_lines = output.splitlines()
    filtered_output = ""
    for line in output_lines:
        if not any(filter_string in line for filter_string in filter_strings):
            filtered_output += line + "\n"
    return filtered_output


def create_custom_makemkv_profile(custom_profile_path: Path) -> None:
    template_profile_path = Path(__file__).parent.parent / "resources" / "makemkv.xml"
    if not template_profile_path.exists():
        raise FileNotFoundError(f"Custom MakeMKV profile not found at {template_profile_path}")
    custom_profile_content = template_profile_path.read_text()

    custom_profile_path.write_text(custom_profile_content)
    print(f"Custom MakeMKV profile created at {custom_profile_path}")


def create_mkv_file(
    output_folder: Path,
    disc_info: DiscInfo,
    progress_callback: ProgressCallback | None = None,
    *,
    run_context: RunContext | None = None,
    cancellation_event: Event | None = None,
) -> Path:
    if config.source_path and config.source_path.suffix.lower() in [*config.MTS_EXTENSIONS, ".mkv"]:
        if not config.source_path.is_file():
            raise FileNotFoundError(f"Source file not found: {config.source_path}")
        if is_direct_pipeline_source_reused():
            return config.source_path
        destination_path = output_folder / config.source_path.name
        if config.source_path.resolve() == destination_path.resolve():
            return config.source_path
        if config.start_stage.value <= Stage.CREATE_MKV.value:
            shutil.copy2(config.source_path, destination_path)
        if mkv_file := find_largest_file_with_extensions(output_folder, [".mkv", *config.MTS_EXTENSIONS]):
            return mkv_file

    if config.start_stage.value <= Stage.CREATE_MKV.value:
        rip_disc_to_mkv(
            output_folder,
            disc_info,
            progress_callback,
            run_context=run_context,
            cancellation_event=cancellation_event,
        )

    if mkv_file := find_largest_file_with_extensions(output_folder, [".mkv"]):
        return mkv_file
    raise ValueError("No MKV file created.")
