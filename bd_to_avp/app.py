import sys

from PySide6.QtWidgets import (
    QApplication,
)

from bd_to_avp.gui.main_window import MainWindow
from bd_to_avp.gui.util import load_app_info_from_pyproject


def start_gui() -> None:
    app = QApplication(sys.argv)
    load_app_info_from_pyproject(app)

    window = MainWindow()
    window.show()

    window.create_menu_bar()
    window.setMenuBar(window.menuBar())

    sys.exit(app.exec())


if __name__ == "__main__":
    start_gui()
