"""Vendored pgsrip runtime used for PGS subtitle OCR."""

__title__ = "pgsrip"
__version__ = "0.1.11"
__short_version__ = "0.1"
__author__ = "Rato"
__license__ = "MIT"
__url__ = "https://github.com/ratoaq2/pgsrip"

from . import api as pgsrip
from .media import Media, Pgs
from .mkv import Mkv
from .options import Options
from .sup import Sup
