from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from bd_to_avp.modules.config import config
from bd_to_avp.modules.util import load_data_from_pyproject


def load_app_info_from_pyproject(app: QApplication) -> None:
    pyproject_data = load_data_from_pyproject()
    if not pyproject_data:
        return

    tool = pyproject_data.get("tool", {})
    poetry = tool.get("poetry", {})
    briefcase = tool.get("briefcase", {})

    app.setApplicationName(poetry.get("name"))
    app.setOrganizationName(briefcase.get("organization"))
    app.setApplicationVersion(config.code_version)
    app.setOrganizationDomain(briefcase.get("bundle"))
    app.setApplicationDisplayName(briefcase.get("project_name"))

    briefcase_icon_path = Path(briefcase.get("icon"))
    icon_path = Path(*briefcase_icon_path.parts[1:]).with_suffix(".icns")
    icon_absolute_path = Path(__file__).parent.parent / icon_path
    app.setWindowIcon(QIcon(icon_absolute_path.as_posix()))

    app.setProperty("authors", poetry.get("authors", []))
    app.setProperty("url", poetry.get("homepage"))
