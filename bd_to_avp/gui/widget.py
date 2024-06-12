from pathlib import Path

from PySide6.QtWidgets import QComboBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSpinBox, QWidget


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


class LabeledComboBox(QWidget):
    def __init__(self, label: str, options: list[str], default_value: str | None = None, parent=None) -> None:
        super().__init__()

        self.combo_layout = QHBoxLayout(parent)
        self.combo_layout.setContentsMargins(0, 0, 0, 0)
        self.label = QLabel(label)
        self.combobox = QComboBox()
        self.combobox.addItems(options)
        if default_value:
            self.combobox.setCurrentText(default_value)

        self.combo_layout.addWidget(self.label)
        self.combo_layout.addWidget(self.combobox)
        self.setLayout(self.combo_layout)

    def current_text(self) -> str:
        return self.combobox.currentText()

    def current_index(self) -> int:
        return self.combobox.currentIndex()

    def set_current_index(self, index: int) -> None:
        self.combobox.setCurrentIndex(index)

    def set_current_text(self, text: str) -> None:
        self.combobox.setCurrentText(text)


class LabeledLineEdit(QWidget):
    def __init__(
        self, label: str, default_value: str | None = None, placeholder_text: str | None = None, parent=None
    ) -> None:
        super().__init__()

        self.line_layout = QHBoxLayout(parent)
        self.line_layout.setContentsMargins(0, 0, 0, 0)
        self.label = QLabel(label)
        self.line_edit = QLineEdit()
        self.line_edit.setMaximumWidth(100)
        if placeholder_text:
            self.line_edit.setPlaceholderText(placeholder_text)
        if default_value:
            self.line_edit.setText(default_value)

        self.line_layout.addWidget(self.line_edit)
        self.line_layout.addWidget(self.label)
        self.setLayout(self.line_layout)

    def text(self) -> str:
        return self.line_edit.text()

    def set_text(self, text: str) -> None:
        self.line_edit.setText(text)


class LabeledSpinBox(QWidget):
    def __init__(
        self, label: str, min_value: int = 0, max_value: int = 100, default_value: int | None = None, parent=None
    ) -> None:
        super().__init__()

        self.spinbox_layout = QHBoxLayout(parent)
        self.spinbox_layout.setContentsMargins(0, 0, 0, 0)
        self.label = QLabel(label)
        self.spinbox = QSpinBox()
        self.spinbox.setRange(min_value, max_value)
        self.spinbox.setMaximumWidth(75)

        if default_value:
            self.spinbox.setValue(default_value)

        self.spinbox_layout.addWidget(self.spinbox)
        self.spinbox_layout.addWidget(self.label)
        self.setLayout(self.spinbox_layout)

    def value(self) -> int:
        return self.spinbox.value()

    def set_value(self, value: int) -> None:
        self.spinbox.setValue(value)
