"""Vendored pgsrip runtime used for PGS subtitle OCR.

This is a BD_to_AVP-maintained local fork based on pgsrip 0.1.11 with selected
upstream 0.1.12 runtime fixes and app-specific tool resolution changes.
"""

__title__ = "pgsrip"
__version__ = "0.1.12+bd_to_avp"
__short_version__ = "0.1"
__author__ = "Rato"
__license__ = "MIT"
__url__ = "https://github.com/ratoaq2/pgsrip"

from . import api as pgsrip
from .media import Media, Pgs
from .mkv import Mkv
from .options import Options
from .sup import Sup
