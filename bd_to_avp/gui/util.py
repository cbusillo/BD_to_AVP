import io
import sys
from pathlib import Path
from typing import Callable, Iterable

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from bd_to_avp.modules.config import config
from bd_to_avp.modules.util import get_pyproject_data


DEFAULT_APP_NAME = "bd_to_avp"
DEFAULT_DISPLAY_NAME = "3D Blu-ray to Vision Pro"
DEFAULT_ORGANIZATION = "Shiny Computers"
DEFAULT_DOMAIN = "com.shinycomputers"
DEFAULT_HOMEPAGE = "https://github.com/cbusillo/BD_to_AVP"


def normalize_authors(authors: object) -> list[str]:
    normalized: list[str] = []
    if not isinstance(authors, list):
        return normalized

    for author in authors:
        if isinstance(author, str):
            normalized.append(author)
            continue

        if not isinstance(author, dict):
            continue

        name = str(author.get("name", "")).strip()
        email = str(author.get("email", "")).strip()
        if name and email:
            normalized.append(f"{name} <{email}>")
        elif name:
            normalized.append(name)

    return normalized


def load_app_info_from_pyproject(app: QApplication) -> None:
    try:
        project, briefcase = get_pyproject_data()
    except FileNotFoundError:
        project = {}
        briefcase = {}

    app.setApplicationName(project.get("name", DEFAULT_APP_NAME))
    app.setOrganizationName(briefcase.get("organization", DEFAULT_ORGANIZATION))
    app.setApplicationVersion(config.app.code_version)
    app.setOrganizationDomain(briefcase.get("bundle", DEFAULT_DOMAIN))
    app.setApplicationDisplayName(briefcase.get("project_name", DEFAULT_DISPLAY_NAME))

    briefcase_icon_path = Path(briefcase.get("icon", "bd_to_avp/resources/app_icon"))
    icon_path = Path(*briefcase_icon_path.parts[1:]).with_suffix(".icns")
    icon_absolute_path = Path(__file__).parent.parent / icon_path
    if icon_absolute_path.exists():
        app.setWindowIcon(QIcon(icon_absolute_path.as_posix()))

    app.setProperty("authors", normalize_authors(project.get("authors", [])))
    app.setProperty("url", project.get("urls", {}).get("Homepage", DEFAULT_HOMEPAGE))


class OutputHandler(io.TextIOBase):
    def __init__(self, emit_signal: Callable[[str], None]) -> None:
        self.emit_signal = emit_signal

    def write(self, text: str) -> int:
        if text:
            if sys.__stdout__ is not None:
                sys.__stdout__.write(text)

            if self.emit_signal is not None:
                self.emit_signal(text.rstrip("\n"))

        return len(text)

    def writelines(self, lines: Iterable[str]) -> None:  # type: ignore
        for line in lines:
            self.write(line)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass
