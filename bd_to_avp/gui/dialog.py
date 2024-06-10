from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class CustomWarningDialog(QDialog):
    def __init__(self, parent: QWidget | None = None, message: str = "") -> None:
        super().__init__(parent)

        self.setWindowTitle("Warning")
        self.setFixedSize(400, 100)

        # Setup layout
        layout = QVBoxLayout()
        content_layout = QHBoxLayout()

        # Icon
        icon_label = QLabel()
        icon = QIcon.fromTheme("dialog-warning")
        icon_label.setPixmap(icon.pixmap(64, 64))
        content_layout.addWidget(icon_label, 0, Qt.AlignmentFlag.AlignTop)

        # Message
        message_label = QLabel(message)
        message_label.setWordWrap(True)
        content_layout.addWidget(
            message_label, 1, Qt.AlignmentFlag.AlignLeft
        )  # Add message to layout

        layout.addLayout(content_layout)

        # OK Button
        ok_button = QPushButton("OK")
        ok_button.clicked.connect(self.accept)
        layout.addWidget(ok_button)

        self.setLayout(layout)

        # Center dialog on parent window
        if parent is not None:
            parent_center = parent.frameGeometry().center()
            dialog_x = parent_center.x() - self.width() // 2
            dialog_y = parent_center.y() - self.height() // 2
            self.move(dialog_x, dialog_y)


