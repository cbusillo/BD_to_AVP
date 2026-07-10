import sys
from copy import deepcopy
from dataclasses import dataclass, replace
from enum import Enum, auto
from pathlib import Path
from threading import Event, get_ident
from typing import Callable, Iterable, Protocol

from PySide6.QtCore import QObject, QThread, Signal, Slot

from bd_to_avp.modules.command import Spinner, terminate_process
from bd_to_avp.modules.config import Stage, config
from bd_to_avp.modules.disc import MKVCreationError
from bd_to_avp.modules.process import BatchProcessingError, start_process
from bd_to_avp.modules.sub import SRTCreationError
from .util import OutputHandler


class TextOutputStream(Protocol):
    def write(self, text: str, /) -> int:
        raise NotImplementedError

    def writelines(self, lines: Iterable[str], /) -> None:
        raise NotImplementedError

    def flush(self) -> None:
        raise NotImplementedError


class NullOutput:
    def write(self, text: str, /) -> int:
        return len(text)

    def writelines(self, lines: Iterable[str], /) -> None:
        for line in lines:
            self.write(line)

    def flush(self) -> None:
        pass


class ThreadScopedOutput:
    def __init__(
        self,
        owner_thread_id: int,
        owner_stream: TextOutputStream,
        fallback_stream: TextOutputStream,
    ) -> None:
        self._owner_thread_id = owner_thread_id
        self._owner_stream = owner_stream
        self._fallback_stream = fallback_stream

    def _stream(self) -> TextOutputStream:
        return self._owner_stream if get_ident() == self._owner_thread_id else self._fallback_stream

    def current_writer(self) -> Callable[[str], int]:
        return self._stream().write

    def write(self, text: str) -> int:
        return self._stream().write(text)

    def writelines(self, lines: Iterable[str]) -> None:
        self._stream().writelines(lines)

    def flush(self) -> None:
        self._stream().flush()

    def __getattr__(self, name: str) -> object:
        return getattr(self._fallback_stream, name)


@dataclass(frozen=True)
class ProcessingConfigSnapshot:
    values: tuple[tuple[str, object], ...]

    @classmethod
    def from_config(cls) -> "ProcessingConfigSnapshot":
        return cls(tuple((name, deepcopy(value)) for name, value in vars(config).items() if name != "app"))

    def apply(self) -> None:
        for name, value in self.values:
            setattr(config, name, deepcopy(value))


@dataclass(frozen=True)
class ProcessingRequest:
    start_stage: Stage
    overwrite: bool
    continue_on_error: bool
    skip_subtitles: bool
    config_snapshot: ProcessingConfigSnapshot | None = None
    batch_start_stage: Stage | None = None
    resume_source_path: Path | None = None
    batch_sources: tuple[Path, ...] | None = None

    def __post_init__(self) -> None:
        if self.batch_start_stage is None:
            object.__setattr__(self, "batch_start_stage", self.start_stage)

    @classmethod
    def from_config(cls) -> "ProcessingRequest":
        return cls(
            start_stage=config.start_stage,
            overwrite=config.overwrite,
            continue_on_error=config.continue_on_error,
            skip_subtitles=config.skip_subtitles,
            config_snapshot=ProcessingConfigSnapshot.from_config(),
        )

    def apply(self) -> None:
        if self.config_snapshot is not None:
            self.config_snapshot.apply()
        config.start_stage = self.start_stage
        config.overwrite = self.overwrite
        config.continue_on_error = self.continue_on_error
        config.skip_subtitles = self.skip_subtitles

    def continue_after_mkv_error(self) -> "ProcessingRequest":
        return replace(self, start_stage=Stage.EXTRACT_MVC_AND_AUDIO, continue_on_error=True)

    def overwrite_existing_output(self) -> "ProcessingRequest":
        return replace(self, overwrite=True)

    def continue_after_srt_error(self, *, skip_subtitles: bool) -> "ProcessingRequest":
        return replace(
            self,
            start_stage=Stage.CREATE_LEFT_RIGHT_FILES,
            skip_subtitles=self.skip_subtitles or skip_subtitles,
            continue_on_error=self.continue_on_error or not skip_subtitles,
        )


class ProcessingOutcomeKind(Enum):
    COMPLETED = auto()
    CANCELLED = auto()
    MKV_CREATION_ERROR = auto()
    SRT_CREATION_ERROR = auto()
    FILE_EXISTS_ERROR = auto()
    ERROR = auto()
    FAILED = auto()


@dataclass(frozen=True)
class ProcessingOutcome:
    kind: ProcessingOutcomeKind
    error: BaseException | None = None
    source_path: Path | None = None
    batch_sources: tuple[Path, ...] | None = None


ProcessRunner = Callable[[ProcessingRequest], None]
StopRunner = Callable[[], None]


def run_processing_request(request: ProcessingRequest, cancellation_event: Event) -> None:
    start_process(
        request.start_stage,
        cancellation_event=cancellation_event,
        resume_source_path=request.resume_source_path,
        batch_start_stage=request.batch_start_stage,
        batch_sources=request.batch_sources,
    )


class ProcessingThread(QThread):
    progress_updated = Signal(str)

    def __init__(
        self,
        request: ProcessingRequest,
        parent: QObject | None = None,
        *,
        process_runner: ProcessRunner | None = None,
        stop_runner: StopRunner | None = None,
    ) -> None:
        super().__init__(parent)
        self.request = request
        self.outcome: ProcessingOutcome | None = None
        self.output_handler = OutputHandler(self.progress_updated.emit)
        self._process_runner = process_runner
        self._stop_runner = stop_runner or terminate_process
        self._cancellation_event = Event()

    @property
    def cancel_requested(self) -> bool:
        return self._cancellation_event.is_set()

    def run(self) -> None:
        previous_stdout = sys.stdout
        fallback_stdout = previous_stdout or NullOutput()
        sys.stdout = ThreadScopedOutput(get_ident(), self.output_handler, fallback_stdout)  # type: ignore[assignment]
        try:
            if self.cancel_requested:
                self.outcome = ProcessingOutcome(ProcessingOutcomeKind.CANCELLED)
                return
            if self._process_runner is None:
                run_processing_request(self.request, self._cancellation_event)
            else:
                self._process_runner(self.request)
            outcome_kind = ProcessingOutcomeKind.CANCELLED if self.cancel_requested else ProcessingOutcomeKind.COMPLETED
            self.outcome = ProcessingOutcome(outcome_kind)
        except Exception as error:
            self.outcome = self._outcome_for_error(error)
        except (SystemExit, KeyboardInterrupt, GeneratorExit) as error:
            self.outcome = ProcessingOutcome(ProcessingOutcomeKind.FAILED, error)
        finally:
            if self.cancel_requested:
                self.outcome = ProcessingOutcome(ProcessingOutcomeKind.CANCELLED)
            Spinner.stop_all()
            sys.stdout = previous_stdout

    def request_cancel(self) -> None:
        if self.cancel_requested:
            return
        self._cancellation_event.set()
        self.requestInterruption()
        self._stop_runner()

    def _outcome_for_error(self, error: BaseException) -> ProcessingOutcome:
        source_path = None
        batch_sources = None
        if isinstance(error, BatchProcessingError):
            source_path = error.source_path
            batch_sources = error.batch_sources
            error = error.error
        if self.cancel_requested:
            return ProcessingOutcome(ProcessingOutcomeKind.CANCELLED)
        if isinstance(error, MKVCreationError):
            return ProcessingOutcome(ProcessingOutcomeKind.MKV_CREATION_ERROR, error, source_path, batch_sources)
        if isinstance(error, SRTCreationError):
            return ProcessingOutcome(ProcessingOutcomeKind.SRT_CREATION_ERROR, error, source_path, batch_sources)
        if isinstance(error, FileExistsError):
            return ProcessingOutcome(ProcessingOutcomeKind.FILE_EXISTS_ERROR, error, source_path, batch_sources)
        if isinstance(error, Exception):
            return ProcessingOutcome(ProcessingOutcomeKind.ERROR, error, source_path, batch_sources)
        return ProcessingOutcome(ProcessingOutcomeKind.FAILED, error, source_path, batch_sources)


class ProcessingController(QObject):
    progress_updated = Signal(str)
    error_occurred = Signal(object, object)
    mkv_creation_error = Signal(object, object)
    srt_creation_error = Signal(object, object)
    file_exists_error = Signal(object, object)
    process_completed = Signal(object)
    process_cancelled = Signal(object)
    process_failed = Signal(object, object)
    processing_became_idle = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread: ProcessingThread | None = None

    @property
    def is_active(self) -> bool:
        return self._thread is not None

    def start(self, request: ProcessingRequest) -> bool:
        if self._thread is not None:
            return False
        request.apply()
        thread = ProcessingThread(request, parent=self)
        self._thread = thread
        thread.progress_updated.connect(self.progress_updated.emit)
        thread.finished.connect(self._thread_finished)
        thread.start()
        return True

    def request_cancel(self) -> bool:
        if self._thread is None:
            return False
        self._thread.request_cancel()
        return True

    def shutdown(self, timeout_ms: int = 5000) -> bool:
        thread = self._thread
        if thread is None:
            return True
        thread.request_cancel()
        if not thread.wait(timeout_ms):
            return False
        if self._thread is thread:
            self._thread = None
            thread.deleteLater()
            self.processing_became_idle.emit()
        return True

    @Slot()
    def _thread_finished(self) -> None:
        thread = self._thread
        if thread is None:
            return
        self._thread = None
        self.processing_became_idle.emit()
        outcome = thread.outcome or ProcessingOutcome(
            ProcessingOutcomeKind.FAILED,
            RuntimeError("Processing thread exited without an outcome."),
        )
        if thread.cancel_requested:
            outcome = ProcessingOutcome(ProcessingOutcomeKind.CANCELLED)
        request = thread.request
        if outcome.source_path is not None:
            request = replace(
                request,
                resume_source_path=outcome.source_path,
                batch_sources=outcome.batch_sources,
            )
        thread.deleteLater()
        self._emit_outcome(request, outcome)

    def _emit_outcome(self, request: ProcessingRequest, outcome: ProcessingOutcome) -> None:
        if outcome.kind == ProcessingOutcomeKind.COMPLETED:
            self.process_completed.emit(request)
        elif outcome.kind == ProcessingOutcomeKind.CANCELLED:
            self.process_cancelled.emit(request)
        elif outcome.kind == ProcessingOutcomeKind.MKV_CREATION_ERROR:
            self.mkv_creation_error.emit(request, outcome.error)
        elif outcome.kind == ProcessingOutcomeKind.SRT_CREATION_ERROR:
            self.srt_creation_error.emit(request, outcome.error)
        elif outcome.kind == ProcessingOutcomeKind.FILE_EXISTS_ERROR:
            self.file_exists_error.emit(request, outcome.error)
        elif outcome.kind == ProcessingOutcomeKind.ERROR:
            self.error_occurred.emit(request, outcome.error)
        else:
            self.process_failed.emit(request, outcome.error)
