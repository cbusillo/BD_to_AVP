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
    def __init__(self, parent: QWidget, message, title: str = "") -> None:
        super().__init__(parent)
        if not title:
            title = "Warning"

        self.setWindowTitle(title)

        # Setup layout
        layout = QVBoxLayout()
        content_layout = QHBoxLayout()

        # Icon
        icon_label = QLabel()
        icon = QIcon.fromTheme("dialog-warning")
        icon_label.setPixmap(icon.pixmap(64, 64))
        icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        content_layout.addWidget(icon_label, 0, Qt.AlignmentFlag.AlignTop)

        # Message
        message_label = QLabel(message)
        message_label.setWordWrap(True)
        content_layout.addWidget(message_label, 1, Qt.AlignmentFlag.AlignLeft)

        layout.addLayout(content_layout)

        # OK Button
        ok_button = QPushButton("OK")
        ok_button.clicked.connect(self.accept)
        layout.addWidget(ok_button)

        self.setLayout(layout)
        self.adjustSize()


class MKVCreationErrorDialog(QDialog):
    def __init__(self, parent: QWidget | None = None, message: str = "") -> None:
        super().__init__(parent)

        self.setWindowTitle("MKV Creation Error")

        # Setup layout
        layout = QVBoxLayout()

        icon_label = QLabel()
        icon = QIcon.fromTheme("dialog-error")
        icon_label.setPixmap(icon.pixmap(64, 64))
        icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(icon_label, 0, Qt.AlignmentFlag.AlignTop)

        message_label = QLabel(message)
        message_label.setWordWrap(True)
        layout.addWidget(message_label, 1, Qt.AlignmentFlag.AlignLeft)

        button_layout = QHBoxLayout()

        # Continue Button
        continue_button = QPushButton("Continue")
        continue_button.clicked.connect(self.accept)
        button_layout.addWidget(continue_button)

        # Abort Button
        abort_button = QPushButton("Abort")
        abort_button.clicked.connect(self.reject)
        button_layout.addWidget(abort_button)

        layout.addLayout(button_layout)
        self.setLayout(layout)
        self.adjustSize()
