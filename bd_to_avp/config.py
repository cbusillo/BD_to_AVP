import argparse

from enum import Enum, auto
from importlib.metadata import version
from pathlib import Path


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
    MAKEMKVCON_PATH = Path("/Applications/MakeMKV.app/Contents/MacOS/makemkvcon")
    SCRIPT_PATH = Path(__file__).parent

    HOMEBREW_PREFIX = Path("/opt/homebrew")
    WINE_PATH = HOMEBREW_PREFIX / "bin/wine"
    FRIM_PATH = SCRIPT_PATH / "bin" / "FRIM_x64_version_1.31" / "x64"
    FRIMDECODE_PATH = FRIM_PATH / "FRIMDecode64.exe"
    SPATIAL_PATH = HOMEBREW_PREFIX / "bin" / "spatial"
    SPATIAL_MEDIA_PATH = SCRIPT_PATH / "bin" / "spatial-media-kit-tool"
    MKVEXTRACT_PATH = HOMEBREW_PREFIX / "bin" / "mkvextract"
    MP4BOX_PATH = Path("/Applications/GPAC.app/Contents/MacOS/MP4Box")
    FX_UPSCALE_PATH = HOMEBREW_PREFIX / "bin" / "fx-upscale"

    FINAL_FILE_TAG = "_AVP"
    IMAGE_EXTENSIONS = [".iso", ".img", ".bin"]

    def __init__(self) -> None:
        self.source_str: str | None = None
        self.source_path: Path | None = None
        self.source_folder: Path | None = None
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
            version=f"BD-to_AVP Version {version('bd_to_avp')}",
        )
        parser.add_argument(
            "--fx-upscale",
            action="store_true",
            help="Use the FX Upscale plugin for AI 4K upscaling.",
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
