import re
from dataclasses import dataclass
from pathlib import Path

import ffmpeg

from bd_to_avp.modules.config import config, Stage
from bd_to_avp.modules.file import find_largest_file_with_extensions, sanitize_filename
from bd_to_avp.modules.util import run_command


class MKVCreationError(Exception):
    pass


@dataclass
class DiscInfo:
    name: str = "Unknown"
    frame_rate: str = "23.976"
    resolution: str = "1920x1080"
    color_depth: int = 8
    main_title_number: int = 0


def get_disc_and_mvc_video_info() -> DiscInfo:
    source = config.source_path.as_posix() if config.source_path else config.source_str
    if not source:
        raise ValueError("No source path provided.")
    if source.lower().endswith(".mts"):
        filename = Path(source).stem
        disc_info = DiscInfo(name=filename)

        ffmpeg_probe_output = ffmpeg.probe(source)["streams"][0]
        disc_info.resolution = f"{ffmpeg_probe_output.get('width', 1920)}x{ffmpeg_probe_output.get('height')}"
        disc_info.frame_rate = ffmpeg_probe_output.get("r_frame_rate")
        disc_info.color_depth = 10 if "10" in ffmpeg_probe_output.get("pix_fmt") else 8
        return disc_info

    command = [config.MAKEMKVCON_PATH, "--robot", "info", source]
    output = run_command(command, "Get disc and MVC video properties")

    disc_info = DiscInfo()

    disc_name_match = re.search(r"CINFO:2,0,\"(.*?)\"", output)
    if disc_name_match:
        disc_info.name = sanitize_filename(disc_name_match.group(1))

    mvc_video_matches = list(
        re.finditer(
            r"SINFO:\d+,1,19,0,\"(\d+x\d+)\".*?SINFO:\d+,1,21,0,\"(.*?)\"",
            output,
            re.DOTALL,
        )
    )

    if not mvc_video_matches:
        print("No MVC video found in disc info.")
        raise ValueError("No MVC video found in disc info.")

    first_match = mvc_video_matches[0]
    disc_info.resolution = first_match.group(1)
    disc_info.frame_rate = first_match.group(2)
    if "/" in disc_info.frame_rate:
        disc_info.frame_rate = disc_info.frame_rate.split(" ")[0]

    title_info_pattern = re.compile(r'TINFO:(?P<index>\d+),\d+,\d+,"(?P<duration>\d+:\d+:\d+)"')
    longest_duration = 0
    main_feature_index = 0

    for match in title_info_pattern.finditer(output):
        title_index = int(match.group("index"))
        h, m, s = map(int, match.group("duration").split(":"))
        duration_seconds = h * 3600 + m * 60 + s

        if duration_seconds > longest_duration:
            longest_duration = duration_seconds
            main_feature_index = title_index

    disc_info.main_title_number = main_feature_index

    return disc_info


def rip_disc_to_mkv(output_folder: Path, disc_info: DiscInfo) -> None:
    custom_profile_path = output_folder / "custom_profile.mmcp.xml"
    create_custom_makemkv_profile(custom_profile_path)

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
    raise MKVCreationError(f"Error occurred while ripping disc to MKV.\n\n{mkv_output}")


def create_custom_makemkv_profile(custom_profile_path: Path) -> None:
    custom_profile_content = """<?xml version="1.0" encoding="UTF-8"?>
<profile>
    <name lang="mogz">:5086</name>
    <Profile name="CustomProfile" description="Custom profile to include MVC tracks">
        <trackSettings input="default">
            <output outputSettingsName="copy"
                defaultSelection="+sel:all,+sel:mvcvideo">
            </output>
        </trackSettings>
    </Profile>
</profile>"""
    custom_profile_path.write_text(custom_profile_content)
    print(f"Custom MakeMKV profile created at {custom_profile_path}")


def create_mkv_file(output_folder: Path, disc_info: DiscInfo) -> Path:
    if config.source_path and config.source_path.suffix.lower() in [
        ".mkv",
        ".mts",
    ]:
        return config.source_path

    if config.start_stage.value <= Stage.CREATE_MKV.value:
        rip_disc_to_mkv(output_folder, disc_info)

    if mkv_file := find_largest_file_with_extensions(output_folder, [".mkv"]):
        return mkv_file
    raise ValueError("No MKV file created.")
