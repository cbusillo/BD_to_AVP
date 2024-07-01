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


def get_disc_and_mvc_video_info() -> DiscInfo:
    source = config.source_path.as_posix() if config.source_path else config.source_str
    if not source:
        raise ValueError("No source path provided.")
    if any(source.lower().endswith(ext) for ext in config.MTS_EXTENSIONS):
        filename = Path(source).stem
        disc_info = DiscInfo(name=filename)

        ffmpeg_probe_output = ffmpeg.probe(source)["streams"][0]
        disc_info.resolution = f"{ffmpeg_probe_output.get('width', 1920)}x{ffmpeg_probe_output.get('height',1080)}"
        disc_info.frame_rate = ffmpeg_probe_output.get("avg_frame_rate")
        disc_info.color_depth = 10 if "10" in ffmpeg_probe_output.get("pix_fmt") else 8
        if ffmpeg_probe_output.get("field_order") != "progressive":
            disc_info.is_interlaced = True
        return disc_info

    command = [config.MAKEMKVCON_PATH, "--robot", "info", source]
    output = run_command(command, "Get disc and MVC video properties")

    disc_info = DiscInfo()

    disc_name_match = re.search(r"CINFO:2,0,\"(.*?)\"", output)
    if disc_name_match:
        disc_info.name = sanitize_filename(disc_name_match.group(1))

    disc_info.main_title_number = get_first_mvc_title(output)

    mvc_video_match = re.search(
        rf"SINFO:{disc_info.main_title_number},1,19,0,\"(\d+x\d+)\".*?SINFO:{disc_info.main_title_number},1,21,0,\"(.*?)\"",
        output,
        re.DOTALL,
    )
    if mvc_video_match:
        disc_info.resolution = mvc_video_match.group(1)
        disc_info.frame_rate = mvc_video_match.group(2)
        if "/" in disc_info.frame_rate:
            disc_info.frame_rate = disc_info.frame_rate.split(" ")[0]
    else:
        print(f"Warning: Could not find resolution and frame rate for title {disc_info.main_title_number}")

    return disc_info


def get_first_mvc_title(output: str) -> int:
    current_title = None
    for line in output.splitlines():
        if line.startswith("TINFO:"):
            title_match = re.search(r"TINFO:(\d+),", line)
            if title_match:
                current_title = int(title_match.group(1))
        elif "MVC" in line and current_title is not None:
            return current_title

    return 0


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
