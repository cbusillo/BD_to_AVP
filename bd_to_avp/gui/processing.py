import sys
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtWidgets import QWidget

from bd_to_avp.modules.disc import MKVCreationError
from bd_to_avp.modules.util import OutputHandler, terminate_process
from bd_to_avp.process import process
from ..modules.sub import SRTCreationError

if TYPE_CHECKING:
    from .main_window import MainWindow


class ProcessingSignals(QObject):
    progress_updated = Signal(str)


class ProcessingThread(QThread):
    error_occurred = Signal(Exception)
    mkv_creation_error = Signal(MKVCreationError)
    srt_creation_error = Signal(SRTCreationError)
    process_completed = Signal()

    def __init__(self, main_window: "MainWindow", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.signals = ProcessingSignals()
        self.output_handler = OutputHandler(self.signals.progress_updated.emit)
        self.main_window = main_window

    def run(self) -> None:
        sys.stdout = self.output_handler  # type: ignore

        try:
            process()
            self.process_completed.emit()
        except MKVCreationError as error:
            self.mkv_creation_error.emit(error)
        except SRTCreationError as error:
            self.srt_creation_error.emit(error)
        except (RuntimeError, ValueError) as error:
            self.error_occurred.emit(error)
        finally:
            sys.stdout = sys.__stdout__
            self.main_window.process_button.setText(self.main_window.START_PROCESSING_TEXT)

    def terminate(self) -> None:
        terminate_process()
        super().terminate()
