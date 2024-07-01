import re
from dataclasses import dataclass
from pathlib import Path

import ffmpeg

from bd_to_avp.modules.config import config, Stage
from bd_to_avp.modules.file import find_largest_file_with_extensions, sanitize_filename
from bd_to_avp.modules.command import run_command


class MKVCreationError(Exception):
    pass


@dataclass
class DiscInfo:
    name: str = "Unknown"
    frame_rate: str = "23.976"
    resolution: str = "1920x1080"
    color_depth: int = 8
    main_title_number: int = 0
    is_interlaced: bool = False


@dataclass
class TitleInfo:
    index: int
    duration: int = 0
    has_mvc: bool = False
    resolution: str | None = None
    frame_rate: str | None = None


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
        elif line.startswith("SINFO:") and current_title is not None:
            if any(term in line.lower() for term in ["mvc-3d", "mpeg4-mvc", "mvc video", "mvc high", "mpeg4 mvc"]):
                titles[current_title].has_mvc = True
            resolution_match = re.search(r"SINFO:\d+,1,19,0,\"(\d+x\d+)\"", line)
            if resolution_match:
                titles[current_title].resolution = resolution_match.group(1)
            frame_rate_match = re.search(r"SINFO:\d+,1,21,0,\"(.*?)\"", line)
            if frame_rate_match:
                titles[current_title].frame_rate = frame_rate_match.group(1)

    return disc_name, list(titles.values())


def get_disc_and_mvc_video_info() -> DiscInfo:
    source = config.source_path.as_posix() if config.source_path else config.source_str
    if not source:
        raise ValueError("No source path provided.")
    if any(source.lower().endswith(ext) for ext in config.MTS_EXTENSIONS):
        filename = Path(source).stem
        disc_info = DiscInfo(name=filename)

        ffmpeg_probe_output = ffmpeg.probe(source)["streams"][0]
        if all(key in ffmpeg_probe_output for key in ["width", "height"]):
            disc_info.resolution = f"{ffmpeg_probe_output.get('width')}x{ffmpeg_probe_output.get('height')}"
        if "avg_frame_rate" in ffmpeg_probe_output:
            disc_info.frame_rate = ffmpeg_probe_output.get("avg_frame_rate")
        if ffmpeg_probe_output.get("field_order") != "progressive":
            disc_info.is_interlaced = True
        return disc_info

    command = [config.MAKEMKVCON_PATH, "--robot", "info", source]
    output = run_command(command, "Get disc and MVC video properties")

    disc_name, titles = parse_makemkv_output(output)

    disc_info = DiscInfo(name=disc_name)

    mvc_titles = [title for title in titles if title.has_mvc]
    if not mvc_titles:
        raise ValueError("No MVC video found in disc info.")

    longest_mvc_title = max(mvc_titles, key=lambda x: x.duration)
    disc_info.main_title_number = longest_mvc_title.index
    disc_info.resolution = longest_mvc_title.resolution or disc_info.resolution
    disc_info.frame_rate = longest_mvc_title.frame_rate or disc_info.frame_rate
    if "/" in disc_info.frame_rate:
        disc_info.frame_rate = disc_info.frame_rate.split(" ")[0]

    return disc_info


def rip_disc_to_mkv(output_folder: Path, disc_info: DiscInfo, language_code: str) -> None:
    custom_profile_path = output_folder / "custom_profile.mmcp.xml"
    create_custom_makemkv_profile(custom_profile_path, language_code)

    if config.source_path and config.source_path.suffix.lower() in config.IMAGE_EXTENSIONS:
        source = f"iso:{config.source_path}"
    elif config.source_path:
        source = config.source_path.as_posix()
    elif config.source_str:
        source = config.source_str
    else:
        raise ValueError("No source provided.")
    command = [
        config.MAKEMKVCON_PATH,
        f"--profile={custom_profile_path}",
        "mkv",
        source,
        disc_info.main_title_number,
        output_folder,
    ]
    mkv_output = run_command(command, "Rip disc to MKV file.")
    if config.continue_on_error or all(error not in mkv_output for error in config.MKV_ERROR_CODES):
        return
    filtered_output = filter_lines_from_output(mkv_output, config.MKV_ERROR_FILTERS)

    raise MKVCreationError(f"Error occurred while ripping disc to MKV.\n\n{filtered_output}")


def filter_lines_from_output(output: str, filter_strings: list[str]) -> str:
    output_lines = output.splitlines()
    filtered_output = ""
    for line in output_lines:
        if not any(filter_string in line for filter_string in filter_strings):
            filtered_output += line + "\n"
    return filtered_output


def create_custom_makemkv_profile(custom_profile_path: Path, language_code: str) -> None:
    template_profile_path = Path(__file__).parent.parent / "resources" / "makemkv.xml"
    if not template_profile_path.exists():
        raise FileNotFoundError(f"Custom MakeMKV profile not found at {template_profile_path}")
    custom_profile_content = template_profile_path.read_text().format(language_code=language_code)
    if config.remove_extra_languages:
        custom_profile_content = custom_profile_content.replace("+sel:all", "-sel:all")

    custom_profile_path.write_text(custom_profile_content)
    print(f"Custom MakeMKV profile created at {custom_profile_path}")


def create_mkv_file(output_folder: Path, disc_info: DiscInfo, language_code: str) -> Path:
    if config.source_path and config.source_path.suffix.lower() in config.MTS_EXTENSIONS + [".mkv"]:
        return config.source_path

    if config.start_stage.value <= Stage.CREATE_MKV.value:
        rip_disc_to_mkv(output_folder, disc_info, language_code)

    if mkv_file := find_largest_file_with_extensions(output_folder, [".mkv"]):
        return mkv_file
    raise ValueError("No MKV file created.")
