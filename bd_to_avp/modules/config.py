import argparse
import configparser
import tomllib

from enum import Enum, auto
from importlib.metadata import version
from pathlib import Path

CONFIG_FILE = Path.home() / ".bd_to_avp.ini"


class Stage(Enum):
    CREATE_MKV = auto()
    EXTRACT_MVC_AUDIO_AND_SUB = auto()
    CREATE_LEFT_RIGHT_FILES = auto()
    UPSCALE_VIDEO = auto()
    COMBINE_TO_MV_HEVC = auto()
    TRANSCODE_AUDIO = auto()
    CREATE_FINAL_FILE = auto()
    MOVE_FILES = auto()

    def __str__(self) -> str:
        return f"{self.value} - {self.human_readable()}"

    def human_readable(self) -> str:
        return {
            "CREATE_MKV": "Create MKV",
            "EXTRACT_MVC_AUDIO_AND_SUB": "Extract MVC Audio and Sub",
            "CREATE_LEFT_RIGHT_FILES": "Create Left Right Files",
            "UPSCALE_VIDEO": "Upscale Video",
            "COMBINE_TO_MV_HEVC": "Combine to MV HEVC",
            "TRANSCODE_AUDIO": "Transcode Audio",
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
    BREW_CASKS_TO_INSTALL = [
        "makemkv",
        "wine-stable",
    ]
    BREW_PACKAGES_TO_INSTALL = [
        "python@3.12",
        "ffmpeg",
        "mkvtoolnix",
        "tesseract",
        "finnvoor/tools/fx-upscale",
    ]
    PROCESS_NAMES_TO_KILL = [
        "ffmpeg",
        "makemkvcon",
        "wine",
        "FRIMDecode64.exe",
        "spatial",
        "spatial-media-kit-tool",
        "mkvextract",
        "MP4Box",
        "fx-upscale",
    ]
    MKV_ERROR_CODES = [
        "corrupt or invalid",
        "video frame timecode differs",
        "secondary stream video frame timecode differs",
    ]

    MAKEMKVCON_PATH = Path("/Applications/MakeMKV.app/Contents/MacOS/makemkvcon")
    SCRIPT_PATH = Path(__file__).parent.parent
    SCRIPT_PATH_BIN = SCRIPT_PATH / "bin"

    HOMEBREW_PREFIX = Path("/opt/homebrew")
    HOMEBREW_PREFIX_BIN = HOMEBREW_PREFIX / "bin"

    WINE_PATH = HOMEBREW_PREFIX_BIN / "wine"
    FRIM_PATH = SCRIPT_PATH_BIN / "FRIM_x64_version_1.31" / "x64"
    FRIMDECODE_PATH = FRIM_PATH / "FRIMDecode64.exe"
    SPATIAL_PATH = HOMEBREW_PREFIX_BIN / "spatial"
    SPATIAL_MEDIA_PATH = SCRIPT_PATH_BIN / "spatial-media-kit-tool"
    MKVEXTRACT_PATH = HOMEBREW_PREFIX_BIN / "mkvextract"
    MP4BOX_VERSION = "2.2.1"
    MP4BOX_PATH = Path("/Applications/GPAC.app/Contents/MacOS/MP4Box")
    FX_UPSCALE_PATH = HOMEBREW_PREFIX_BIN / "fx-upscale"

    FINAL_FILE_TAG = "_AVP"
    IMAGE_EXTENSIONS = [".iso", ".img", ".bin"]

    def __init__(self) -> None:
        self.source_str: str | None = None
        self.source_path: Path | None = None
        self.source_folder_path: Path | None = None
        self.output_root_path = Path.cwd()
        self.overwrite = False
        self.transcode_audio = False
        self.audio_bitrate = 384
        self.left_right_bitrate = 20
        self.mv_hevc_quality = 75
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

    @property
    def code_version(self) -> str:
        pyproject_path = Path("pyproject.toml")
        if pyproject_path.exists():
            with open(pyproject_path, "rb") as pyproject_file:
                pyproject_data = tomllib.load(pyproject_file)

            return pyproject_data["tool"]["poetry"]["version"]

        project_version = version(__package__.split(".")[0])
        return project_version

    @staticmethod
    def load_version_from_file() -> str | None:
        config_file = configparser.ConfigParser()
        config_file.read(CONFIG_FILE)
        if (
            "Application" in config_file
            and "installed_version" in config_file["Application"]
        ):
            return config_file.get("Application", "installed_version")
        return None

    def save_version_from_file(self) -> None:
        config_file = configparser.ConfigParser()
        config_file.read(CONFIG_FILE)

        if not config_file.has_section("Application"):
            config_file.add_section("Application")
        config_file.set("Application", "installed_version", self.code_version)

        with open(CONFIG_FILE, "w") as configfile:
            config_file.write(configfile)

    def save_config_to_file(self) -> None:
        config_file = configparser.ConfigParser()
        config_file.read(CONFIG_FILE)

        if not config_file.has_section("Paths"):
            config_file.add_section("Paths")
        if not config_file.has_section("Options"):
            config_file.add_section("Options")

        for key, value in self.__dict__.items():
            if "_path" in key:
                if not value:
                    continue
                config_file.set("Paths", key, value.as_posix())
            else:
                if not value:
                    continue
                config_file.set("Options", key, str(value))

        with open(CONFIG_FILE, "w") as configfile:
            config_file.write(configfile)

    def load_config_from_file(self) -> None:
        config_file = configparser.ConfigParser()
        config_file.read(CONFIG_FILE)

        if config_file.has_section("Paths"):
            for key, value in config_file.items("Paths"):
                if key in self.__dict__:
                    setattr(self, key, Path(value))

        if config_file.has_section("Options"):
            for key, value in config_file.items("Options"):
                if key in self.__dict__:
                    attribute_type = type(getattr(self, key))
                    if attribute_type == bool:
                        setattr(self, key, config_file.getboolean("Options", key))
                    elif attribute_type == int:
                        setattr(self, key, config_file.getint("Options", key))
                    elif attribute_type == Stage:
                        stage_value = config_file.get("Options", key).split(" - ")[0]
                        setattr(self, key, Stage.get_stage(int(stage_value)))
                    else:
                        setattr(self, key, config_file.get("Options", key))

    def parse_args(self) -> None:
        parser = argparse.ArgumentParser(
            description="Process 3D Blu-ray to MV-HEVC compatible with the Apple Vision Pro."
        )
        source_group = parser.add_mutually_exclusive_group(required=True)

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
            help="Remove original file after processing.",
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
            help="Transcode audio to AAC format.",
        )
        parser.add_argument(
            "--audio-bitrate",
            type=int,
            help="Audio bitrate for transcoding in kilobits.",
        )
        parser.add_argument(
            "--skip-freaking-subtitles-because-I-dont-care",
            "--skip-subtitles",
            action="store_true",
            help="Skip extracting subtitles from MKV.",
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
            help="Keep temporary files after processing.",
        )
        parser.add_argument(
            "--start-stage",
            type=Stage,
            action=StageEnumAction,
            help="Stage at which to start the process. Options: "
            + ", ".join([stage.name for stage in Stage]),
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
            version=f"BD-to_AVP Version {self.code_version}",
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

        args = parser.parse_args()

        for key, value in vars(args).items():
            if value is not None:
                setattr(self, key, value)

        self.source_path = (
            Path(args.source).expanduser()
            if args.source and not args.source.startswith("disc:")
            else None
        )
        self.output_root_path = (
            Path(args.output_root_folder).expanduser()
            if args.output_root_folder
            else self.output_root_path
        )
        self.skip_subtitles = args.skip_freaking_subtitles_because_I_dont_care


config = Config()
