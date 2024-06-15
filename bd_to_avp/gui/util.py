import io
import sys
from pathlib import Path
from typing import Callable, Iterable

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from bd_to_avp.modules.config import config
from bd_to_avp.modules.util import get_pyproject_data


def load_app_info_from_pyproject(app: QApplication) -> None:
    poetry, briefcase = get_pyproject_data()
    if not (poetry and briefcase):
        raise FileNotFoundError("poetry and briefcase data not found in pyproject.toml")

    try:
        app.setApplicationName(poetry["name"])
        app.setOrganizationName(briefcase["organization"])
        app.setApplicationVersion(config.app.code_version)
        app.setOrganizationDomain(briefcase["bundle"])
        app.setApplicationDisplayName(briefcase["project_name"])

        briefcase_icon_path = Path(briefcase["icon"])
        icon_path = Path(*briefcase_icon_path.parts[1:]).with_suffix(".icns")
        icon_absolute_path = Path(__file__).parent.parent / icon_path
        app.setWindowIcon(QIcon(icon_absolute_path.as_posix()))

        app.setProperty("authors", poetry.get("authors", []))
        app.setProperty("url", poetry.get("homepage"))
    except KeyError as error:
        raise KeyError(f"Key not found in pyproject.toml: {error}")


class OutputHandler(io.TextIOBase):
    def __init__(self, emit_signal: Callable[[str], None]) -> None:
        self.emit_signal = emit_signal

    def write(self, text: str) -> int:
        if text:
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
