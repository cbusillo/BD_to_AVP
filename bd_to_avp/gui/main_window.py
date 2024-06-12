from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QFont, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QGroupBox,
    QMainWindow,
    QLabel,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QHBoxLayout,
    QCheckBox,
    QTextEdit,
    QStatusBar,
    QWidget,
    QMessageBox,
)

from bd_to_avp.gui.dialog import AboutDialog
from bd_to_avp.gui.processing import ProcessingThread
from bd_to_avp.gui.widget import FileFolderPicker, LabeledComboBox, LabeledLineEdit, LabeledSpinBox
from bd_to_avp.modules.config import config, Stage
from bd_to_avp.modules.disc import DiscInfo, MKVCreationError
from bd_to_avp.modules.util import Spinner, get_common_language_options


# noinspection PyAttributeOutsideInit
# type: ignore[attr-defined-outside-init]
class MainWindow(QMainWindow):
    START_PROCESSING_TEXT = "Start Processing (⌘+P)"
    STOP_PROCESSING_TEXT = "Stop Processing (⌘+P)"
    MAIN_WIDGET_MIN_WIDTH = 300
    SPLITTER_INITIAL_SIZES = [400, 400]
    SPLITTER_MINIMUM_SIZE = 300
    WINDOW_GEOMETRY = (100, 100, 800, 600)
    LAYOUT_SPACING = 5

    def __init__(self) -> None:
        super().__init__()
        self.setup_window()
        self.create_main_layout()
        self.create_menu_bar()

    def setup_window(self) -> None:
        app = QApplication.instance()
        if not isinstance(app, QApplication):
            raise RuntimeError("No QApplication instance found.")
        self.setWindowTitle(app.applicationDisplayName())
        self.setGeometry(*self.WINDOW_GEOMETRY)

    def create_main_layout(self) -> None:
        main_widget = QWidget()
        main_widget.setMinimumWidth(self.SPLITTER_MINIMUM_SIZE)
        main_layout = QVBoxLayout(main_widget)
        main_layout.setSpacing(self.LAYOUT_SPACING)
        self.create_save_load_layout(main_layout)

        source_output_group = self.create_group_box("Source and Output", self.create_input_output_layout)
        main_layout.addWidget(source_output_group)

        self.create_config_layout(main_layout)
        self.create_processing_button(main_layout)
        self.create_processing_output(main_widget)
        self.create_status_bar()

    @staticmethod
    def create_group_box(title: str, box_contents: Callable[[QVBoxLayout], None]) -> QGroupBox:
        group_box = QGroupBox(title)
        groub_box_layout = QVBoxLayout()
        box_contents(groub_box_layout)
        group_box.setLayout(groub_box_layout)
        return group_box

    def create_save_load_layout(self, main_layout: QVBoxLayout) -> None:
        save_load_layout = QHBoxLayout()
        self.load_config_button = QPushButton("Load Config (⌘+L)")
        self.load_config_button.clicked.connect(self.load_config_and_update_ui)
        self.load_config_button.setShortcut("Ctrl+L")
        save_load_layout.addWidget(self.load_config_button)

        self.save_config_button = QPushButton("Save Config (⌘+S)")
        self.save_config_button.clicked.connect(self.save_config_to_file)
        self.save_config_button.setShortcut("Ctrl+S")
        save_load_layout.addWidget(self.save_config_button)

        main_layout.addLayout(save_load_layout)

    def create_input_output_layout(self, main_layout: QVBoxLayout) -> None:
        self.read_from_disc_checkbox = self.create_checkbox("Read from Disc")
        self.source_folder_widget = FileFolderPicker("Source Folder")
        self.source_file_widget = FileFolderPicker("Source File", for_files=True)
        self.output_folder_widget = FileFolderPicker("Output Folder")

        main_layout.addWidget(self.read_from_disc_checkbox)
        main_layout.addWidget(self.source_folder_widget)
        main_layout.addWidget(self.source_file_widget)
        main_layout.addWidget(self.output_folder_widget)

    def create_config_layout(self, main_layout: QVBoxLayout) -> None:
        config_options_layout = QVBoxLayout()
        quality_group = self.create_group_box("Quality Options", self.create_quality_options)
        misc_group = self.create_group_box("Misc Options", self.create_misc_options)
        processing_group = self.create_group_box("Processing Options", self.create_processing_options)

        config_options_layout.addWidget(quality_group)
        config_options_layout.addWidget(misc_group)
        config_options_layout.addWidget(processing_group)
        main_layout.addLayout(config_options_layout)

    def create_quality_options(self, config_layout: QVBoxLayout) -> None:
        self.left_right_bitrate_spinbox = LabeledSpinBox(
            "Left/Right Bitrate (Mbps)", default_value=config.left_right_bitrate
        )
        self.audio_bitrate_spinbox = LabeledSpinBox(
            "Audio Bitrate (kbps)", max_value=1000, default_value=config.audio_bitrate
        )
        self.mv_hevc_quality_spinbox = LabeledSpinBox("MV-HEVC Quality (0-100)", default_value=config.mv_hevc_quality)
        self.fov_spinbox = LabeledSpinBox("Field of View", max_value=360, default_value=config.fov)
        self.frame_rate_entry = LabeledLineEdit(
            "Frame Rate (Leave blank to use source value)", config.frame_rate, DiscInfo.frame_rate
        )
        self.resolution_entry = LabeledLineEdit(
            "Resolution (Leave blank to use source value)", config.resolution, DiscInfo.resolution
        )

        config_layout.addWidget(self.left_right_bitrate_spinbox)
        config_layout.addWidget(self.audio_bitrate_spinbox)
        config_layout.addWidget(self.mv_hevc_quality_spinbox)
        config_layout.addWidget(self.fov_spinbox)
        config_layout.addWidget(self.frame_rate_entry)
        config_layout.addWidget(self.resolution_entry)

    def create_misc_options(self, config_layout: QVBoxLayout) -> None:

        self.crop_black_bars_checkbox = self.create_checkbox("Crop Black Bars", config.crop_black_bars)
        self.swap_eyes_checkbox = self.create_checkbox("Swap Eyes", config.swap_eyes)
        self.keep_files_checkbox = self.create_checkbox("Keep Temporary Files", config.keep_files)
        self.output_commands_checkbox = self.create_checkbox("Output Commands", config.output_commands)
        self.software_encoder_checkbox = self.create_checkbox("Use Software Encoder", config.software_encoder)
        self.fx_upscale_checkbox = self.create_checkbox("AI FX Upscale (2x resolution)", config.fx_upscale)
        self.remove_original_checkbox = self.create_checkbox("Remove Original", config.remove_original)
        self.overwrite_checkbox = self.create_checkbox("Overwrite", config.overwrite)
        self.transcode_audio_checkbox = self.create_checkbox("Transcode Audio", config.transcode_audio)
        self.continue_on_error = self.create_checkbox("Continue Processing On Error", config.continue_on_error)

        config_layout.addWidget(self.crop_black_bars_checkbox)
        config_layout.addWidget(self.swap_eyes_checkbox)
        config_layout.addWidget(self.keep_files_checkbox)
        config_layout.addWidget(self.output_commands_checkbox)
        config_layout.addWidget(self.software_encoder_checkbox)
        config_layout.addWidget(self.fx_upscale_checkbox)
        config_layout.addWidget(self.remove_original_checkbox)
        config_layout.addWidget(self.overwrite_checkbox)
        config_layout.addWidget(self.transcode_audio_checkbox)
        config_layout.addWidget(self.continue_on_error)

    def create_processing_options(self, config_layout: QVBoxLayout) -> None:
        self.start_stage_combobox = LabeledComboBox("Start Stage", Stage.list(), config.start_stage.name)
        config_layout.addWidget(self.start_stage_combobox)

    def create_processing_button(self, main_layout: QVBoxLayout) -> None:
        self.process_button = QPushButton(self.START_PROCESSING_TEXT)
        self.process_button.clicked.connect(self.toggle_processing)
        self.process_button.setShortcut("Ctrl+P")
        main_layout.addWidget(self.process_button)

    def create_processing_output(self, main_widget: QWidget) -> None:
        self.processing_output_textedit = QTextEdit()
        self.processing_output_textedit.setReadOnly(True)
        self.processing_output_textedit.setFont(QFont("Helvetica", 10))

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.addWidget(main_widget)
        self.splitter.addWidget(self.processing_output_textedit)
        self.splitter.setSizes(self.SPLITTER_INITIAL_SIZES)  # Adjust the sizes as needed

        self.setCentralWidget(self.splitter)

    def create_status_bar(self) -> None:
        self.processing_status_label = QLabel("Processing Status")
        self.processing_status_label.hide()

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.hide()

        self.splitter.splitterMoved.connect(self.update_status_bar)

        self.processing_thread = ProcessingThread(main_window=self)
        self.processing_thread.signals.progress_updated.connect(self.update_processing_output)
        self.processing_thread.error_occurred.connect(self.handle_processing_error)
        self.processing_thread.mkv_creation_error.connect(self.handle_mkv_creation_error)

    def create_menu_bar(self) -> None:
        menu_bar = self.menuBar()
        self.setMenuBar(self.menuBar())

        app_menu = menu_bar.addMenu(QApplication.applicationName())
        about_action = QAction(f"About {QApplication.applicationName()}", self)
        about_action.triggered.connect(self.show_about_dialog)
        app_menu.addAction(about_action)

        file_menu = menu_bar.addMenu("File")
        file_menu.addAction(QAction("Open", self))

        help_menu = menu_bar.addMenu("Help")
        update_action = QAction("Update", self)
        update_action.triggered.connect(self.show_about_dialog)
        help_menu.addAction(update_action)

        self.setMenuBar(menu_bar)

    def handle_processing_error(self, error: Exception) -> None:
        QMessageBox.warning(self, "Warning", "Failure in processing.")
        self.update_processing_output(str(error))
        self.stop_processing()
        self.process_button.setText(self.START_PROCESSING_TEXT)

    def handle_mkv_creation_error(self, error: MKVCreationError) -> None:

        result = QMessageBox.critical(
            self,
            "MKV Creation Error",
            "Do you want to continue?\n\n" + str(error),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Abort,
        )

        if result == QMessageBox.StandardButton.Yes:
            config.continue_on_error = True
            config.start_stage = Stage.EXTRACT_MVC_AUDIO_AND_SUB
            self.start_processing(is_continuing=True)
            return

        self.handle_processing_error(error)

    def toggle_read_from_disc(self) -> None:
        self.source_folder_widget.setEnabled(not self.read_from_disc_checkbox.isChecked())
        self.source_file_widget.setEnabled(not self.read_from_disc_checkbox.isChecked())

    def update_status_bar(self) -> None:
        splitter_sizes = self.splitter.sizes()
        if splitter_sizes[-1] == 0:
            last_line = self.processing_output_textedit.toPlainText().strip().split("\n")[-1]
            self.status_bar.showMessage(last_line)
            self.processing_status_label.show()
            self.status_bar.show()
        else:
            self.status_bar.clearMessage()
            self.processing_status_label.hide()
            self.status_bar.hide()

    def load_config_and_update_ui(self) -> None:
        config.load_config_from_file()
        self.source_folder_widget.set_text(config.source_folder_path.as_posix() if config.source_folder_path else "")
        self.source_file_widget.set_text(config.source_path.as_posix() if config.source_path else "")
        self.output_folder_widget.set_text(config.output_root_path.as_posix())
        self.left_right_bitrate_spinbox.set_value(config.left_right_bitrate)
        self.audio_bitrate_spinbox.set_value(config.audio_bitrate)
        self.mv_hevc_quality_spinbox.set_value(config.mv_hevc_quality)
        self.fov_spinbox.set_value(config.fov)
        self.frame_rate_entry.set_text(config.frame_rate)
        self.resolution_entry.set_text(config.resolution)
        self.crop_black_bars_checkbox.setChecked(config.crop_black_bars)
        self.swap_eyes_checkbox.setChecked(config.swap_eyes)
        self.keep_files_checkbox.setChecked(config.keep_files)
        self.output_commands_checkbox.setChecked(config.output_commands)
        self.software_encoder_checkbox.setChecked(config.software_encoder)
        self.fx_upscale_checkbox.setChecked(config.fx_upscale)
        self.remove_original_checkbox.setChecked(config.remove_original)
        self.overwrite_checkbox.setChecked(config.overwrite)
        self.transcode_audio_checkbox.setChecked(config.transcode_audio)
        self.start_stage_combobox.set_current_text(config.start_stage.name)
        self.continue_on_error.setChecked(config.continue_on_error)

    def toggle_processing(self) -> None:
        if self.process_button.text() == self.START_PROCESSING_TEXT:
            self.processing_output_textedit.clear()
            source_folder_set = bool(self.source_folder_widget.text())
            source_file_set = bool(self.source_file_widget.text())
            if (source_folder_set and source_file_set) or (not source_folder_set and not source_file_set):
                QMessageBox.warning(self, "Warning", "Either Source Folder or Source File must be set, but not both.")
                return
            self.start_processing()
            self.process_button.setText(self.STOP_PROCESSING_TEXT)
        else:
            self.stop_processing()
            self.process_button.setText(self.START_PROCESSING_TEXT)

        self.process_button.setShortcut("Ctrl+P")
        # self.process_button.clicked.connect(self.toggle_processing)

    def start_processing(self, is_continuing: bool = False) -> None:
        if not is_continuing:
            self.save_config()

        self.processing_thread.start()

    def save_config_to_file(self) -> None:
        self.save_config()
        config.save_config_to_file()

    def save_config(self) -> None:
        if self.read_from_disc_checkbox.isChecked():
            config.source_str = "disc:0"
            config.source_folder_path = None
            config.source_path = None
        else:
            config.source_folder_path = (
                Path(self.source_folder_widget.text()) if self.source_folder_widget.text() else None
            )
            config.source_path = Path(self.source_file_widget.text()) if self.source_file_widget.text() else None
        config.output_root_path = Path(self.output_folder_widget.text())
        config.left_right_bitrate = self.left_right_bitrate_spinbox.value()
        config.audio_bitrate = self.audio_bitrate_spinbox.value()
        config.mv_hevc_quality = self.mv_hevc_quality_spinbox.value()
        config.fov = self.fov_spinbox.value()
        config.frame_rate = self.frame_rate_entry.text()
        config.resolution = self.resolution_entry.text()
        config.crop_black_bars = self.crop_black_bars_checkbox.isChecked()
        config.swap_eyes = self.swap_eyes_checkbox.isChecked()
        config.keep_files = self.keep_files_checkbox.isChecked()
        config.output_commands = self.output_commands_checkbox.isChecked()
        config.software_encoder = self.software_encoder_checkbox.isChecked()
        config.fx_upscale = self.fx_upscale_checkbox.isChecked()
        config.remove_original = self.remove_original_checkbox.isChecked()
        config.overwrite = self.overwrite_checkbox.isChecked()
        config.transcode_audio = self.transcode_audio_checkbox.isChecked()
        selected_stage = int(self.start_stage_combobox.current_text().split(" - ")[0])
        config.start_stage = Stage.get_stage(selected_stage)
        config.continue_on_error = self.continue_on_error.isChecked()

    def stop_processing(self) -> None:
        self.processing_thread.terminate()

    def update_processing_output(self, message: str) -> None:
        output_textedit = self.processing_output_textedit
        output_textedit_scrollbar = output_textedit.verticalScrollBar()
        is_output_at_end = output_textedit_scrollbar.value == output_textedit_scrollbar.maximum()

        last_line_of_textedit = output_textedit.toPlainText().rsplit("\n", 1)[-1]

        spinner_dict = str.maketrans("", "", "".join(Spinner.symbols))
        message_stripped = message.translate(spinner_dict).strip()
        last_line_stripped = last_line_of_textedit.translate(spinner_dict).strip()

        cursor = output_textedit.textCursor()

        if any(symbol in last_line_of_textedit for symbol in Spinner.symbols):
            cursor.movePosition(QTextCursor.MoveOperation.End)
            cursor.deletePreviousChar()

        if any(symbol in message for symbol in Spinner.symbols):
            if message_stripped == last_line_stripped:
                cursor.movePosition(QTextCursor.MoveOperation.End)
                cursor.select(QTextCursor.SelectionType.LineUnderCursor)
                cursor.removeSelectedText()
                cursor.deletePreviousChar()
                output_textedit.append(message.strip())

        else:
            output_textedit.append(message.strip())

        if is_output_at_end:
            output_textedit_scrollbar.setValue(output_textedit_scrollbar.maximum())

        self.status_bar.showMessage(message.strip().rsplit("\n", 1)[-1])

    def show_about_dialog(self) -> None:
        dialog = AboutDialog(self)
        dialog.exec()

    @staticmethod
    def create_checkbox(label: str, default_value: bool = False) -> QCheckBox:
        check_box = QCheckBox(label)
        check_box.setChecked(default_value)
        return check_box
