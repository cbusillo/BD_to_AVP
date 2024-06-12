from pathlib import Path

from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QWidget


class FileFolderPicker(QWidget):
    def __init__(self, label: str, for_files: bool = False, parent=None) -> None:
        super().__init__(parent)
        self.dialog_title = f"Please select the {label}"
        self.for_files = for_files

        self.file_selector_layout = QHBoxLayout(self)
        self.file_selector_layout.setContentsMargins(0, 0, 0, 0)

        self.file_selector_label = QLabel(label)
        self.file_selector_layout.addWidget(self.file_selector_label)

        self.line_edit = QLineEdit(self)
        self.file_selector_layout.addWidget(self.line_edit)

        self.button = QPushButton("Select", self)
        self.button.clicked.connect(self.browse)
        self.file_selector_layout.addWidget(self.button)

    def browse(self) -> None:
        current_text = self.line_edit.text()

        current_path = Path(current_text) if current_text else Path.home() / "Movies"

        if self.for_files:
            selected_object, _ = QFileDialog.getOpenFileName(self, self.dialog_title, current_path.as_posix())
        else:
            selected_object = QFileDialog.getExistingDirectory(self, self.dialog_title, current_path.as_posix())
        if selected_object:
            self.line_edit.setText(selected_object)

    def text(self) -> str:
        return self.line_edit.text()

    def set_text(self, text: str) -> None:
        self.line_edit.setText(text)
