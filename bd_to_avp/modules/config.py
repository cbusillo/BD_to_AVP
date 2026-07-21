import argparse
import configparser
import os
import shutil
import sys

from enum import Enum, auto
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import ClassVar, Iterable

from bd_to_avp.modules.audio_mode import AudioMode
from bd_to_avp.modules.preview_range import PreviewRange
from bd_to_avp.modules.languages import LanguageCodeError, normalize_language_code
from bd_to_avp.modules.util import get_pyproject_data
from bd_to_avp.modules.video_mode import VideoMode


SCRIPT_PATH = Path(__file__).parent.parent
SCRIPT_PATH_BIN = SCRIPT_PATH / "bin"
HOMEBREW_PREFIX = Path("/opt/homebrew")
HOMEBREW_PREFIX_BIN = HOMEBREW_PREFIX / "bin"
MAKEMKV_APP_BUNDLE_BIN = Path("/Applications/MakeMKV.app/Contents/MacOS")


def tool_env_var(tool_name: str) -> str:
    env_tool_name = tool_name.upper().replace("-", "_")
    return f"BD_TO_AVP_{env_tool_name}_PATH"


def resolve_tool_path(
    tool_name: str,
    *,
    env_var: str | None = None,
    bundled_name: str | None = None,
    extra_paths: Iterable[Path] = (),
    script_bin_path: Path = SCRIPT_PATH_BIN,
    homebrew_bin_path: Path = HOMEBREW_PREFIX_BIN,
) -> Path:
    configured_path = os.environ.get(env_var or tool_env_var(tool_name))
    if configured_path:
        return Path(configured_path).expanduser()

    bundled_path = script_bin_path / (bundled_name or tool_name)
    if bundled_path.exists():
        return bundled_path

    path_tool = shutil.which(tool_name)
    if path_tool:
        return Path(path_tool)

    for extra_path in extra_paths:
        if extra_path.exists():
            return extra_path

    return homebrew_bin_path / tool_name


def resolve_makemkvcon_path() -> Path:
    app_bundle_tool = MAKEMKV_APP_BUNDLE_BIN / "makemkvcon"
    return resolve_tool_path("makemkvcon", extra_paths=[app_bundle_tool])


class Stage(Enum):
    CREATE_MKV = auto()
    EXTRACT_MVC_AND_AUDIO = auto()
    EXTRACT_SUBTITLES = auto()
    CREATE_LEFT_RIGHT_FILES = auto()
    COMBINE_TO_MV_HEVC = auto()
    UPSCALE_VIDEO = auto()
    TRANSCODE_AUDIO = auto()
    CREATE_FINAL_FILE = auto()
    MOVE_FILES = auto()

    def __str__(self) -> str:
        return f"{self.value} - {self.human_readable()}"

    def human_readable(self) -> str:
        return {
            "CREATE_MKV": "Create MKV",
            "EXTRACT_MVC_AND_AUDIO": "Extract MVC and Audio",
            "EXTRACT_SUBTITLES": "Extract Subtitles",
            "CREATE_LEFT_RIGHT_FILES": "Create Left Right Files",
            "UPSCALE_VIDEO": "Upscale Video",
            "COMBINE_TO_MV_HEVC": "Combine to MV HEVC",
            "TRANSCODE_AUDIO": "Prepare Audio",
            "CREATE_FINAL_FILE": "Create Final File",
            "MOVE_FILES": "Move Files",
        }[self.name]

    @classmethod
    def list(cls) -> list[str]:
        return list(map(str, cls))

    @classmethod
    def get_stage(cls, value: int) -> "Stage":
        for stage in Stage:
            if stage.value == value:
                return stage
        raise ValueError(f"Invalid stage value: {value}")


class StageEnumAction(argparse.Action):
    def __init__(self, **kwargs) -> None:
        self.enum_type = kwargs.pop("type", None)
        super(StageEnumAction, self).__init__(**kwargs)

    def __call__(self, parser, namespace, values, option_string=None) -> None:
        if self.enum_type and not isinstance(values, self.enum_type):
            enum_value = self.enum_type[values.upper()]
            setattr(namespace, self.dest, enum_value)
        else:
            setattr(namespace, self.dest, values)


class Config:
    class App:
        def __init__(self) -> None:
            try:
                project, briefcase = get_pyproject_data()
                self.fullname = briefcase.get("project_name", "3D Blu-ray to Vision Pro")
                self.shortname = project.get("name", "bd_to_avp")
            except FileNotFoundError:
                self.fullname = "3D Blu-ray to Vision Pro"
                self.shortname = "bd_to_avp"

            self.config_path = Path.home() / "Library" / "Application Support" / self.shortname
            self.config_file = (self.config_path / "config.ini").with_suffix(".ini")

            if not self.config_path.exists():
                self.config_path.mkdir(parents=True)
            if not self.config_file.exists():
                self.config_file.touch()

            self.is_gui = len(sys.argv) == 1

        @property
        def code_version(self) -> str:
            try:
                project, _ = get_pyproject_data()
                return project["version"]
            except (FileNotFoundError, KeyError):
                pass

            try:
                return version(self.shortname)
            except PackageNotFoundError:
                return "0.0.0"

    MKV_ERROR_CODES: ClassVar[list[str]] = [
        "corrupt or invalid",
        "video frame timecode differs",
        "secondary stream video frame timecode differs",
    ]
    MKV_ERROR_FILTERS: ClassVar[list[str]] = [
        "which is less than minimum title length",
        "Debug logging",
        "AnyDVD",
        "MakeMKV",
        "Do you want to continue anyway",
        "AACS directory not present",
        "Evaluation version",
        "Using direct disc access mode",
        "Program reads data faster than it can write to disk",
    ]

    SCRIPT_PATH = SCRIPT_PATH
    SCRIPT_PATH_BIN = SCRIPT_PATH_BIN

    FFMPEG_PATH = resolve_tool_path("ffmpeg")
    FFPROBE_PATH = resolve_tool_path("ffprobe")
    MAKEMKVCON_PATH = resolve_makemkvcon_path()
    MP4BOX_PATH = resolve_tool_path("MP4Box")
    EDGE264_TEST_PATH = SCRIPT_PATH_BIN / "edge264_test"
    SPATIAL_MEDIA_PATH = SCRIPT_PATH_BIN / "spatial-media-kit-tool"
    FX_UPSCALE_PATH = SCRIPT_PATH_BIN / "fx-upscale"

    FINAL_FILE_TAG = "_AVP"
    AV1_FINAL_FILE_TAG = "_AV1_Stereo"
    AV1_PRESET = 9
    IMAGE_EXTENSIONS: ClassVar[list[str]] = [".iso", ".img", ".bin"]
    MTS_EXTENSIONS: ClassVar[list[str]] = [".mts", ".m2ts"]

    def __init__(self) -> None:
        self.app = self.App()

        self.source_str: str | None = None
        self.source_path: Path | None = None
        self.source_folder_path: Path | None = None
        self.output_root_path = Path.home() / "Movies"
        self.overwrite = False
        self.audio_mode = AudioMode.AUTOMATIC
        self.audio_bitrate = 384
        self.audio_preferred_language: str | None = None
        self.video_mode = VideoMode.MV_HEVC
        self.av1_crf = 32
        self.left_right_bitrate = 20
        self.link_quality = True
        self.mv_hevc_quality = 75
        self.upscale_quality = self.mv_hevc_quality
        self.fov = 90
        self.frame_rate = ""
        self.resolution = ""
        self.keep_files = False
        self.start_stage = Stage.CREATE_MKV
        self.remove_original = False
        self.swap_eyes = False
        self.skip_subtitles = False
        self.crop_black_bars = False
        self.output_commands = False
        self.software_encoder = False
        self.fx_upscale = False
        self.continue_on_error = False
        self.language_code = "eng"
        self.remove_extra_languages = False
        self.keep_awake = True
        self.preview_range: PreviewRange | None = None
        self.smoke_apple_vision_ocr = False

    @property
    def transcode_audio(self) -> bool:
        return self.audio_mode is AudioMode.CONVERT_AAC

    @transcode_audio.setter
    def transcode_audio(self, value: bool) -> None:
        self.audio_mode = AudioMode.CONVERT_AAC if value else AudioMode.PCM

    def configure_tool_environment(self) -> None:
        configured_dirs = [
            self.FFMPEG_PATH.parent,
            self.FFPROBE_PATH.parent,
            self.MAKEMKVCON_PATH.parent,
            self.MP4BOX_PATH.parent,
        ]
        existing_path = os.environ.get("PATH", "")
        existing_dirs = [path for path in existing_path.split(os.pathsep) if path]
        prepend_dirs: list[str] = []

        for path_dir in configured_dirs:
            if not path_dir.exists():
                continue
            path_dir_str = path_dir.as_posix()
            if path_dir_str not in prepend_dirs and path_dir_str not in existing_dirs:
                prepend_dirs.append(path_dir_str)

        script_bin_path = self.SCRIPT_PATH_BIN.as_posix()
        if (
            self.SCRIPT_PATH_BIN.exists()
            and script_bin_path not in prepend_dirs
            and script_bin_path not in existing_dirs
        ):
            prepend_dirs.append(script_bin_path)

        if prepend_dirs:
            os.environ["PATH"] = os.pathsep.join([*prepend_dirs, *existing_dirs])

        os.environ.setdefault("FFMPEG_BINARY", self.FFMPEG_PATH.as_posix())
        os.environ.setdefault("FFPROBE_BINARY", self.FFPROBE_PATH.as_posix())

    def save_config_to_file(self) -> None:
        config_parser = configparser.ConfigParser()
        config_parser.read(self.app.config_file)

        if not config_parser.has_section("Paths"):
            config_parser.add_section("Paths")
        if not config_parser.has_section("Options"):
            config_parser.add_section("Options")
        config_parser.remove_option("Options", "transcode_audio")

        for key, value in self.__dict__.items():
            if key == "app":
                continue
            elif "_path" in key:
                if value:
                    config_parser.set("Paths", key, value.as_posix())
                else:
                    config_parser.remove_option("Paths", key)
            else:
                if value:
                    config_parser.set("Options", key, str(value))
                elif value is False:
                    config_parser.set("Options", key, "False")
                else:
                    config_parser.remove_option("Options", key)

        with open(self.app.config_file, "w") as config_file:
            config_parser.write(config_file)

    def load_config_from_file(self) -> None:
        config_parser = configparser.ConfigParser()
        config_parser.read(self.app.config_file)

        if config_parser.has_section("Paths"):
            for key, value in config_parser.items("Paths"):
                if key in self.__dict__:
                    setattr(self, key, Path(value))

        if config_parser.has_section("Options"):
            if config_parser.has_option("Options", "transcode_audio") and not config_parser.has_option(
                "Options", "audio_mode"
            ):
                self.transcode_audio = config_parser.getboolean("Options", "transcode_audio")
            for key, _value in config_parser.items("Options"):
                if key in self.__dict__:
                    attribute_type = type(getattr(self, key))
                    if attribute_type is bool:
                        setattr(self, key, config_parser.getboolean("Options", key))
                    elif attribute_type is int:
                        setattr(self, key, config_parser.getint("Options", key))
                    elif attribute_type is Stage:
                        stage_value = config_parser.get("Options", key).split(" - ")[0]
                        setattr(self, key, Stage.get_stage(int(stage_value)))
                    elif attribute_type is AudioMode:
                        setattr(self, key, AudioMode(config_parser.get("Options", key)))
                    elif attribute_type is VideoMode:
                        setattr(self, key, VideoMode(config_parser.get("Options", key)))
                    else:
                        value = config_parser.get("Options", key)
                        if key in {"language_code", "audio_preferred_language"}:
                            value = normalize_language_code(value)
                        setattr(self, key, value)

    def parse_args(self) -> None:
        parser = argparse.ArgumentParser(
            description="Process 3D Blu-ray to MV-HEVC compatible with the Apple Vision Pro."
        )
        parser.add_argument(
            "--smoke-apple-vision-ocr",
            action="store_true",
            help=argparse.SUPPRESS,
        )
        source_group = parser.add_mutually_exclusive_group(required=False)

        source_group.add_argument(
            "--source",
            "-s",
            help="Source for a single disc number, MKV file path, or ISO image path.",
        )
        source_group.add_argument(
            "--source-folder",
            "-f",
            type=Path,
            help="Directory containing multiple image files or MKVs for processing (will search recusively).",
        )

        parser.add_argument(
            "--remove-original",
            "-r",
            action="store_true",
            help="Remove the original source after processing completes successfully.",
        )
        parser.add_argument(
            "--overwrite",
            action="store_true",
            help="Overwrite existing output file.",
        )
        parser.add_argument(
            "--output-root-folder",
            "-o",
            type=Path,
            help="Output folder path. Defaults to current directory.",
        )
        parser.add_argument(
            "--transcode-audio",
            action="store_true",
            default=None,
            help="Legacy alias for --audio-mode convert_aac.",
        )
        parser.add_argument(
            "--audio-mode",
            choices=[mode.value for mode in AudioMode],
            help="Audio handling mode: automatic, convert_aac, or pcm (default: automatic).",
        )
        parser.add_argument(
            "--audio-bitrate",
            type=int,
            help="Audio bitrate for transcoding in kilobits.",
        )
        parser.add_argument(
            "--audio-preferred-language",
            help=(
                "Keep audio tracks matching this language. Accepts ISO 639 alpha-2, alpha-3/B, or alpha-3/T codes; "
                "omit to keep all audio languages."
            ),
        )
        parser.add_argument(
            "--skip-freaking-subtitles-because-I-dont-care",
            "--skip-subtitles",
            action="store_true",
            help="Skip extracting subtitles from MKV.",
        )
        parser.add_argument(
            "--video-mode",
            choices=[mode.value for mode in VideoMode],
            help="Video output mode: mv_hevc for native Apple spatial video or av1_sbs for software AV1 stereo.",
        )
        parser.add_argument(
            "--av1-crf",
            type=int,
            choices=range(0, 64),
            metavar="0-63",
            help="SVT-AV1 constant-rate factor for AV1 stereo output. Lower values produce higher quality.",
        )
        parser.add_argument(
            "--left-right-bitrate",
            type=int,
            help="Bitrate for MV-HEVC encoding in megabits.",
        )
        parser.add_argument(
            "--mv-hevc-quality",
            type=int,
            help="Quality factor for MV-HEVC encoding with a scale of 0 to 100.",
        )
        parser.add_argument(
            "--upscale-quality",
            type=int,
            help="Quality factor for AI upscaling with a scale of 0 to 100.",
        )
        parser.add_argument(
            "--fov",
            type=int,
            help="Horizontal field of view for MV-HEVC.",
        )
        parser.add_argument(
            "--frame_rate",
            help="Video frame rate. Detected automatically if not provided.",
        )
        parser.add_argument(
            "--resolution",
            help="Video resolution. Detected automatically if not provided.",
        )
        parser.add_argument(
            "--crop-black-bars",
            action="store_true",
            help="Automatically Crop black bars.",
        )
        parser.add_argument(
            "--swap-eyes",
            action="store_true",
            help="Swap left and right eye video streams.",
        )
        parser.add_argument(
            "--keep-files",
            action="store_true",
            help="Use durable stage files for restart, inspection, and external processing.",
        )
        parser.add_argument(
            "--start-stage",
            type=Stage,
            action=StageEnumAction,
            help="Stage at which to start the process. Options: " + ", ".join([stage.name for stage in Stage]),
        )
        parser.add_argument(
            "--output-commands",
            action="store_true",
            help="Output commands for debugging.",
        )
        parser.add_argument(
            "--software-encoder",
            action="store_true",
            help="Use software encoder for HEVC encoding.",
        )
        parser.add_argument(
            "--version",
            action="version",
            version=f"BD-to_AVP Version {self.app.code_version}",
        )
        parser.add_argument(
            "--fx-upscale",
            action="store_true",
            help="Use the FX Upscale plugin for AI 4K upscaling.",
        )
        parser.add_argument(
            "--continue-on-error",
            action="store_true",
            help="Continue processing after an error occurs.",
        )
        parser.add_argument(
            "--language-code",
            help=(
                "Preferred subtitle language. Defaults to 'eng'; accepts ISO 639 alpha-2, alpha-3/B, "
                "or alpha-3/T codes."
            ),
        )
        parser.add_argument(
            "--remove-extra-languages",
            action="store_true",
            help="Remove all subtitle languages except the one specified by --language-code.",
        )
        parser.add_argument(
            "--no-keep-awake",
            dest="keep_awake",
            action="store_false",
            help="Prevent the computer from sleeping during processing.",
        )
        args = parser.parse_args()

        if not args.smoke_apple_vision_ocr and not args.source and not args.source_folder:
            parser.error("one of the arguments --source/-s --source-folder/-f is required")

        for key, value in vars(args).items():
            if value is not None:
                setattr(self, key, value)

        self.source_path = (
            Path(args.source).expanduser() if args.source and not args.source.startswith("disc:") else None
        )
        self.output_root_path = (
            Path(args.output_root_folder).expanduser() if args.output_root_folder else self.output_root_path
        )
        self.skip_subtitles = args.skip_freaking_subtitles_because_I_dont_care
        if args.language_code:
            try:
                self.language_code = normalize_language_code(args.language_code)
            except LanguageCodeError as error:
                parser.error(str(error))
        if args.audio_preferred_language:
            try:
                self.audio_preferred_language = normalize_language_code(args.audio_preferred_language)
            except LanguageCodeError as error:
                parser.error(str(error))
        if args.audio_mode:
            self.audio_mode = AudioMode(args.audio_mode)
        elif args.transcode_audio:
            self.audio_mode = AudioMode.CONVERT_AAC
        if args.video_mode:
            self.video_mode = VideoMode(args.video_mode)

    @property
    def final_file_tag(self) -> str:
        return self.AV1_FINAL_FILE_TAG if self.video_mode is VideoMode.AV1_SBS else self.FINAL_FILE_TAG


config = Config()


def is_direct_pipeline_source_reused() -> bool:
    return bool(
        not config.keep_files
        and config.source_path
        and config.source_path.suffix.lower() in [*config.MTS_EXTENSIONS, ".mkv"]
    )


def is_audio_m4a_preparation_enabled() -> bool:
    return config.audio_mode.prepares_m4a


def is_direct_audio_transcode_enabled() -> bool:
    return config.audio_mode is AudioMode.CONVERT_AAC and not config.keep_files


def is_direct_mvc_stream_enabled() -> bool:
    return not config.keep_files
