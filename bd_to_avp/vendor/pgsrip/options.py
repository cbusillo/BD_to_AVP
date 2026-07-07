import enum
import typing
from datetime import timedelta

from babelfish import Language

from cleanit import Config


@enum.unique
class TesseractEngineMode(enum.Enum):
    LEGACY = 0
    NEURAL = 1
    LEGACY_AND_NEURAL = 2
    DEFAULT_AVAILABLE = 3


@enum.unique
class TesseractPageSegmentationMode(enum.Enum):
    OSD_ONLY = 0
    AUTOMATIC_PAGE_SEGMENTATION_WITH_OSD = 1
    AUTOMATIC_PAGE_SEGMENTATION_WITHOUT_OSD_OR_OCR = 2
    FULLY_AUTOMATIC_PAGE_SEGMENTATION_WITHOUT_OSD = 3
    SINGLE_COLUMN_OF_TEXT_OF_VARIABLE_SIZES = 4
    SINGLE_UNIFORM_BLOCK_OF_VERTICALLY_ALIGNED_TEXT = 5
    SINGLE_UNIFORM_BLOCK_OF_TEXT = 6
    SINGLE_TEXT_LINE = 7
    SINGLE_WORD = 8
    SINGLE_WORD_IN_CIRCLE = 9
    SINGLE_CHARACTER = 10
    SPARSE_TEXT = 11
    SPARSE_TEXT_WITH_OSD = 12
    RAW_LINE = 13


class Options:

    def __init__(self,
                 config_path: typing.Optional[str] = None,
                 languages: typing.Optional[typing.Set[Language]] = None,
                 tags: typing.Optional[typing.Set[str]] = None,
                 encoding: typing.Optional[str] = None,
                 overwrite=False,
                 one_per_lang=True,
                 keep_temp_files=False,
                 max_workers: typing.Optional[int] = None,
                 confidence: typing.Optional[int] = None,
                 tesseract_width: typing.Optional[int] = None,
                 tesseract_oem: typing.Optional[TesseractEngineMode] = None,
                 tesseract_psm: typing.Optional[TesseractPageSegmentationMode] = None,
                 age: typing.Optional[timedelta] = None,
                 srt_age: typing.Optional[timedelta] = None):
        self.config = Config.from_path(config_path) if config_path else Config()
        self.languages = languages or set()
        self.tags = tags or {'default'}
        self.encoding = encoding
        self.overwrite = overwrite
        self.one_per_lang = one_per_lang
        self.keep_temp_files = keep_temp_files
        self.max_workers = max_workers
        self.confidence = confidence
        self.tesseract_width = tesseract_width
        self.tesseract_oem = tesseract_oem
        self.tesseract_psm = tesseract_psm
        self.age = age
        self.srt_age = srt_age

    def __repr__(self):
        return f'<{self.__class__.__name__} [{self}]>'

    def __str__(self):
        return (f'languages:{self.languages}, '
                f'tags:{self.tags}, '
                f'encoding:{self.encoding}, '
                f'overwrite:{self.overwrite}, '
                f'one_per_lang:{self.one_per_lang}, '
                f'keep_temp_files:{self.keep_temp_files}, '
                f'max_workers:{self.max_workers}, '
                f'confidence:{self.confidence}, '
                f'tesseract_width:{self.tesseract_width}, '
                f'tesseract_oem:{self.tesseract_oem}, '
                f'tesseract_psm:{self.tesseract_psm}, '
                f'age:{self.age}, '
                f'srt_age:{self.srt_age}')
