import sys
from pathlib import Path

from PyQt6.QtGui import QTextCursor
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
from bd_to_avp.disc import DiscInfo
from bd_to_avp.main import process
from bd_to_avp.util import OutputHandler, Spinner


class Testing:
    source_path = "/Users/cbusillo/TEMP/Gamer.iso"
    output_root_path = "/Users/cbusillo/TEMP"


class ProcessingSignals(QObject):
    progress_updated = pyqtSignal(str)


class ProcessingThread(QThread):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.signals = ProcessingSignals()
        # noinspection PyUnresolvedReferences
        self.output_handler = OutputHandler(self.signals.progress_updated.emit)

    def run(self) -> None:
        sys.stdout = self.output_handler  # type: ignore

        try:
            process()
        finally:
            sys.stdout = sys.__stdout__
            # noinspection PyUnresolvedReferences
            self.signals.progress_updated.emit("Process Completed.")


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

        self.source_file_entry.setText(Testing.source_path)

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

        self.output_folder_entry.setText(Testing.output_root_path)

        main_layout.addLayout(output_layout)

        # Configuration options
        config_layout = QVBoxLayout()

        self.left_right_bitrate_label = QLabel("Left/Right Bitrate (Mbps):")
        self.left_right_bitrate_spinbox = QSpinBox()
        self.left_right_bitrate_spinbox.setRange(1, 100)
        self.left_right_bitrate_spinbox.setValue(config.left_right_bitrate)
        self.left_right_bitrate_spinbox.setMaximumWidth(75)
        config_layout.addWidget(self.left_right_bitrate_label)
        config_layout.addWidget(self.left_right_bitrate_spinbox)

        self.audio_bitrate_label = QLabel("Audio Bitrate (kbps):")
        self.audio_bitrate_spinbox = QSpinBox()
        self.audio_bitrate_spinbox.setRange(0, 1000)
        self.audio_bitrate_spinbox.setValue(config.audio_bitrate)
        self.audio_bitrate_spinbox.setMaximumWidth(75)
        config_layout.addWidget(self.audio_bitrate_label)
        config_layout.addWidget(self.audio_bitrate_spinbox)

        self.mv_hevc_quality_label = QLabel("MV-HEVC Quality (0-100):")
        self.mv_hevc_quality_spinbox = QSpinBox()
        self.mv_hevc_quality_spinbox.setRange(0, 100)
        self.mv_hevc_quality_spinbox.setValue(config.mv_hevc_quality)
        self.mv_hevc_quality_spinbox.setMaximumWidth(75)
        config_layout.addWidget(self.mv_hevc_quality_label)
        config_layout.addWidget(self.mv_hevc_quality_spinbox)

        self.fov_label = QLabel("Field of View:")
        self.fov_spinbox = QSpinBox()
        self.fov_spinbox.setRange(0, 360)
        self.fov_spinbox.setValue(config.fov)
        self.fov_spinbox.setMaximumWidth(75)
        config_layout.addWidget(self.fov_label)
        config_layout.addWidget(self.fov_spinbox)

        self.frame_rate_label = QLabel("Frame Rate (Leave blank to use source value):")
        self.frame_rate_entry = QLineEdit()
        self.frame_rate_entry.setText(config.frame_rate)
        self.frame_rate_entry.setMaximumWidth(75)
        self.frame_rate_entry.setPlaceholderText(DiscInfo.frame_rate)
        config_layout.addWidget(self.frame_rate_label)
        config_layout.addWidget(self.frame_rate_entry)

        self.resolution_label = QLabel("Resolution (Leave blank to use source value):")
        self.resolution_entry = QLineEdit()
        self.resolution_entry.setText(config.resolution)
        self.resolution_entry.setPlaceholderText(DiscInfo.resolution)
        self.resolution_entry.setMaximumWidth(150)
        config_layout.addWidget(self.resolution_label)
        config_layout.addWidget(self.resolution_entry)

        self.crop_black_bars_checkbox = QCheckBox("Crop Black Bars")
        self.crop_black_bars_checkbox.setChecked(config.crop_black_bars)
        config_layout.addWidget(self.crop_black_bars_checkbox)

        self.swap_eyes_checkbox = QCheckBox("Swap Eyes")
        self.swap_eyes_checkbox.setChecked(config.swap_eyes)
        config_layout.addWidget(self.swap_eyes_checkbox)

        self.keep_files_checkbox = QCheckBox("Keep Temporary Files")
        self.keep_files_checkbox.setChecked(config.keep_files)
        config_layout.addWidget(self.keep_files_checkbox)

        self.output_commands_checkbox = QCheckBox("Output Commands")
        self.output_commands_checkbox.setChecked(config.output_commands)
        config_layout.addWidget(self.output_commands_checkbox)

        self.software_encoder_checkbox = QCheckBox("Use Software Encoder")
        self.software_encoder_checkbox.setChecked(config.software_encoder)
        config_layout.addWidget(self.software_encoder_checkbox)

        self.fx_upscale_checkbox = QCheckBox("FX Upscale")
        self.fx_upscale_checkbox.setChecked(config.fx_upscale)
        config_layout.addWidget(self.fx_upscale_checkbox)

        self.remove_original_checkbox = QCheckBox("Remove Original")
        self.remove_original_checkbox.setChecked(config.remove_original)
        config_layout.addWidget(self.remove_original_checkbox)

        self.overwrite_checkbox = QCheckBox("Overwrite")
        self.overwrite_checkbox.setChecked(config.overwrite)
        config_layout.addWidget(self.overwrite_checkbox)

        self.transcode_audio_checkbox = QCheckBox("Transcode Audio")
        self.transcode_audio_checkbox.setChecked(config.transcode_audio)
        config_layout.addWidget(self.transcode_audio_checkbox)

        self.start_stage_label = QLabel("Start Stage:")
        self.start_stage_combobox = QComboBox()
        self.start_stage_combobox.addItems(Stage.list())
        self.start_stage_combobox.setCurrentText(config.start_stage.name)
        config_layout.addWidget(self.start_stage_label)
        config_layout.addWidget(self.start_stage_combobox)

        # Add more configuration options as needed

        main_layout.addLayout(config_layout)

        # Processing button
        self.process_button = QPushButton("Start Processing")
        # noinspection PyUnresolvedReferences
        self.process_button.clicked.connect(self.toggle_processing)
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

    def toggle_processing(self) -> None:
        if self.process_button.text() == "Start Processing":
            self.start_processing()
            self.process_button.setText("Stop Processing")
        else:
            self.stop_processing()
            self.process_button.setText("Start Processing")

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
        selected_stage = int(self.start_stage_combobox.currentText().split(" - ")[0])
        config.start_stage = Stage.get_stage(selected_stage)

        # Start the processing thread
        self.processing_thread.start()

    def stop_processing(self) -> None:
        self.processing_thread.terminate()

    def update_processing_output(self, message: str) -> None:
        cursor = self.processing_output_textedit.textCursor()

        if any(symbol in message for symbol in Spinner.symbols):
            cursor.movePosition(QTextCursor.MoveOperation.End)
            cursor.select(QTextCursor.SelectionType.BlockUnderCursor)
            cursor.removeSelectedText()
            cursor.insertText(message)
        else:
            self.processing_output_textedit.append(message.strip())

        cursor.movePosition(QTextCursor.MoveOperation.Start)
        while cursor.movePosition(QTextCursor.MoveOperation.Down):
            cursor.select(QTextCursor.SelectionType.LineUnderCursor)
            if cursor.selectedText().strip() == "":
                cursor.deleteChar()
            if any(symbol in cursor.selectedText() for symbol in Spinner.symbols):
                if cursor.movePosition(QTextCursor.MoveOperation.Down):
                    cursor.movePosition(QTextCursor.MoveOperation.Up)
                    cursor.select(QTextCursor.SelectionType.LineUnderCursor)
                    cursor.removeSelectedText()
                else:
                    break

        self.processing_output_textedit.setTextCursor(cursor)


def start_gui() -> None:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    start_gui()
