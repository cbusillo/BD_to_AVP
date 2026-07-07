import sys

from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtWidgets import QWidget

from bd_to_avp.modules.disc import MKVCreationError
from .util import OutputHandler
from ..modules.command import terminate_process, Spinner
from bd_to_avp.modules.process import start_process
from ..modules.config import Stage
from ..modules.sub import SRTCreationError


class ProcessingSignals(QObject):
    progress_updated = Signal(str)


class ProcessingThread(QThread):
    error_occurred = Signal(Exception)
    mkv_creation_error = Signal(MKVCreationError)
    srt_creation_error = Signal(SRTCreationError)
    file_exists_error = Signal(FileExistsError)
    process_completed = Signal()
    process_failed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.signals = ProcessingSignals()
        self.output_handler = OutputHandler(self.signals.progress_updated.emit)
        self.start_stage = Stage.CREATE_MKV

    def run(self) -> None:
        sys.stdout = self.output_handler  # type: ignore
        signal_emitted = False

        try:
            start_process(self.start_stage)
            self.process_completed.emit()
            signal_emitted = True
        except MKVCreationError as error:
            self.mkv_creation_error.emit(error)
            signal_emitted = True
        except SRTCreationError as error:
            self.srt_creation_error.emit(error)
            signal_emitted = True
        except FileExistsError as error:
            self.file_exists_error.emit(error)
            signal_emitted = True
        except (RuntimeError, ValueError, KeyError) as error:
            self.error_occurred.emit(error)
            signal_emitted = True
        finally:
            Spinner.stop_all()
            sys.stdout = sys.__stdout__
            if not signal_emitted:
                self.process_failed.emit()

    def terminate(self) -> None:
        terminate_process()
        super().terminate()
