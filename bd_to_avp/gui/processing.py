import sys
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtWidgets import QWidget

from bd_to_avp.modules.disc import MKVCreationError
from .util import OutputHandler
from ..modules.command import terminate_process, Spinner
from bd_to_avp.modules.process import start_process
from ..modules.sub import SRTCreationError

if TYPE_CHECKING:
    from .main_window import MainWindow


class ProcessingSignals(QObject):
    progress_updated = Signal(str)


class ProcessingThread(QThread):
    error_occurred = Signal(Exception)
    mkv_creation_error = Signal(MKVCreationError)
    srt_creation_error = Signal(SRTCreationError)
    file_exists_error = Signal(FileExistsError)
    process_completed = Signal()

    def __init__(self, main_window: "MainWindow", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.signals = ProcessingSignals()
        self.output_handler = OutputHandler(self.signals.progress_updated.emit)
        self.main_window = main_window

    def run(self) -> None:
        sys.stdout = self.output_handler  # type: ignore

        try:
            start_process()
            self.process_completed.emit()
        except MKVCreationError as error:
            self.mkv_creation_error.emit(error)
        except SRTCreationError as error:
            self.srt_creation_error.emit(error)
        except FileExistsError as error:
            self.file_exists_error.emit(error)
        except (RuntimeError, ValueError, KeyError) as error:
            self.error_occurred.emit(error)
        finally:
            Spinner.stop_all()
            sys.stdout = sys.__stdout__
            self.main_window.process_button.setText(self.main_window.START_PROCESSING_TEXT)

    def terminate(self) -> None:
        terminate_process()
        super().terminate()
