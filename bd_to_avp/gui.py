import sys
from pathlib import Path

from PyQt6.QtWidgets import (
    QApplication,
    QMainWindow,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QHBoxLayout,
    QWidget,
    QFileDialog,
    QCheckBox,
    QSpinBox,
    QTextEdit,
    QComboBox,
)
from PyQt6.QtCore import QObject, QThread, pyqtSignal

from bd_to_avp.config import config, Stage
from bd_to_avp.main import process_each
from bd_to_avp.util import OutputHandler, Spinner


class ProcessingSignals(QObject):
    progress_updated = pyqtSignal(str)


class ProcessingThread(QThread):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.signals = ProcessingSignals()
        self.output_handler = OutputHandler(self.signals.progress_updated.emit)

    def run(self) -> None:
        sys.stdout = self.output_handler

        try:
            process_each()
        finally:
            sys.stdout = sys.__stdout__


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("3D Blu-ray (and mts) to AVP")
        self.setGeometry(100, 100, 800, 600)

        # Create the main layout
        main_layout = QVBoxLayout()

        # Source and output folder selection
        source_layout = QHBoxLayout()
        self.source_folder_label = QLabel("Source Folder:")
        self.source_folder_entry = QLineEdit()
        self.source_folder_button = QPushButton("Browse")
        # noinspection PyUnresolvedReferences
        self.source_folder_button.clicked.connect(self.browse_source_folder)
        source_layout.addWidget(self.source_folder_label)
        source_layout.addWidget(self.source_folder_entry)
        source_layout.addWidget(self.source_folder_button)

        self.source_file_label = QLabel("Source File:")
        self.source_file_entry = QLineEdit()
        self.source_file_button = QPushButton("Browse")
        # noinspection PyUnresolvedReferences
        self.source_file_button.clicked.connect(self.browse_source_file)
        source_layout.addWidget(self.source_file_label)
        source_layout.addWidget(self.source_file_entry)
        source_layout.addWidget(self.source_file_button)

        self.source_file_entry.setText("/Users/cbusillo/TEMP/Gamer.iso")

        main_layout.addLayout(source_layout)

        output_layout = QHBoxLayout()
        self.output_folder_label = QLabel("Output Folder:")
        self.output_folder_entry = QLineEdit()
        self.output_folder_button = QPushButton("Browse")
        # noinspection PyUnresolvedReferences
        self.output_folder_button.clicked.connect(self.browse_output_folder)
        output_layout.addWidget(self.output_folder_label)
        output_layout.addWidget(self.output_folder_entry)
        output_layout.addWidget(self.output_folder_button)

        main_layout.addLayout(output_layout)

        # Configuration options
        config_layout = QVBoxLayout()

        self.remove_original_checkbox = QCheckBox("Remove Original")
        self.remove_original_checkbox.setChecked(config.remove_original)
        config_layout.addWidget(self.remove_original_checkbox)

        self.overwrite_checkbox = QCheckBox("Overwrite")
        self.overwrite_checkbox.setChecked(config.overwrite)
        config_layout.addWidget(self.overwrite_checkbox)

        self.transcode_audio_checkbox = QCheckBox("Transcode Audio")
        self.transcode_audio_checkbox.setChecked(config.transcode_audio)
        config_layout.addWidget(self.transcode_audio_checkbox)

        self.audio_bitrate_label = QLabel("Audio Bitrate:")
        self.audio_bitrate_spinbox = QSpinBox()
        self.audio_bitrate_spinbox.setRange(0, 1000)
        self.audio_bitrate_spinbox.setValue(config.audio_bitrate)
        config_layout.addWidget(self.audio_bitrate_label)
        config_layout.addWidget(self.audio_bitrate_spinbox)

        self.start_stage_label = QLabel("Start Stage:")
        self.start_stage_combobox = QComboBox()
        self.start_stage_combobox.addItems([stage.name for stage in Stage])
        self.start_stage_combobox.setCurrentText(config.start_stage.name)
        config_layout.addWidget(self.start_stage_label)
        config_layout.addWidget(self.start_stage_combobox)

        # Add more configuration options as needed

        main_layout.addLayout(config_layout)

        # Processing button
        self.process_button = QPushButton("Start Processing")
        # noinspection PyUnresolvedReferences
        self.process_button.clicked.connect(self.start_processing)
        main_layout.addWidget(self.process_button)

        # Processing status and output
        self.processing_status_label = QLabel("Processing Status:")
        main_layout.addWidget(self.processing_status_label)

        self.processing_output_textedit = QTextEdit()
        self.processing_output_textedit.setReadOnly(True)
        main_layout.addWidget(self.processing_output_textedit)

        # Set the main layout in a central widget
        central_widget = QWidget()
        central_widget.setLayout(main_layout)
        self.setCentralWidget(central_widget)

        # Create the processing thread
        self.processing_thread = ProcessingThread()
        # noinspection PyUnresolvedReferences
        self.processing_thread.signals.progress_updated.connect(
            self.update_processing_output
        )

    def browse_source_folder(self) -> None:
        source_folder = QFileDialog.getExistingDirectory(self, "Select Source Folder")
        if source_folder:
            self.source_folder_entry.setText(source_folder)

    def browse_source_file(self) -> None:
        source_file, _ = QFileDialog.getOpenFileName(self, "Select Source File")
        if source_file:
            self.source_file_entry.setText(source_file)

    def browse_output_folder(self) -> None:
        output_folder = QFileDialog.getExistingDirectory(self, "Select Output Folder")
        if output_folder:
            self.output_folder_entry.setText(output_folder)

    def start_processing(self) -> None:
        config.source_folder = (
            Path(self.source_folder_entry.text())
            if self.source_folder_entry.text()
            else None
        )
        config.source_path = (
            Path(self.source_file_entry.text())
            if self.source_file_entry.text()
            else None
        )
        config.output_root_path = Path(self.output_folder_entry.text())
        config.remove_original = self.remove_original_checkbox.isChecked()
        config.overwrite = self.overwrite_checkbox.isChecked()
        config.transcode_audio = self.transcode_audio_checkbox.isChecked()
        config.audio_bitrate = self.audio_bitrate_spinbox.value()
        config.start_stage = Stage[self.start_stage_combobox.currentText()]

        # Start the processing thread
        self.processing_thread.start()

    def update_processing_output(self, message: str) -> None:
        current_text = self.processing_output_textedit.toPlainText()
        if any(symbol for symbol in Spinner.symbols if symbol in message):
            lines = current_text.split("\n")
            if any(symbol in lines[-1] for symbol in Spinner.symbols):
                lines += message
            else:
                lines += message
            output_message = "\n".join(lines)
            self.processing_output_textedit.setPlainText(output_message)
        else:
            for symbol in Spinner.symbols:
                current_text = current_text.replace(symbol, "")
            self.processing_output_textedit.setPlainText(f"{current_text}\n{message}")


def start_gui() -> None:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    start_gui()
