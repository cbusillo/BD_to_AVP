import os
import sys
import threading
import time
import unittest
from datetime import datetime
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication, QMessageBox

from bd_to_avp.gui.main_window import MainWindow
from bd_to_avp.gui.processing import (
    ProcessingConfigSnapshot,
    ProcessingController,
    ProcessingOutcome,
    ProcessingOutcomeKind,
    ProcessingRequest,
    ProcessingThread,
    ThreadScopedOutput,
)
from bd_to_avp.modules.config import Stage, config
from bd_to_avp.modules.command import get_spinner_update_func
from bd_to_avp.modules.disc import MKVCreationError
from bd_to_avp.modules.process import BatchProcessingError
from bd_to_avp.modules.sub import SRTCreationError


class ProcessingRequestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.request = ProcessingRequest(
            start_stage=Stage.CREATE_MKV,
            overwrite=False,
            continue_on_error=False,
            skip_subtitles=False,
        )

    def test_mkv_continuation_preserves_prior_policy(self) -> None:
        failed_source = Path("failed.m2ts")
        continuation = (
            ProcessingRequest(
                start_stage=self.request.start_stage,
                overwrite=self.request.overwrite,
                continue_on_error=self.request.continue_on_error,
                skip_subtitles=self.request.skip_subtitles,
                resume_source_path=failed_source,
            )
            .overwrite_existing_output()
            .continue_after_mkv_error()
        )

        self.assertEqual(continuation.start_stage, Stage.EXTRACT_MVC_AND_AUDIO)
        self.assertTrue(continuation.overwrite)
        self.assertTrue(continuation.continue_on_error)
        self.assertFalse(continuation.skip_subtitles)
        self.assertEqual(continuation.batch_start_stage, Stage.CREATE_MKV)
        self.assertEqual(continuation.resume_source_path, failed_source)
        self.assertEqual(self.request.start_stage, Stage.CREATE_MKV)

    def test_srt_continuations_select_skip_or_continue_policy(self) -> None:
        skip = self.request.continue_after_srt_error(skip_subtitles=True)
        continue_on_error = self.request.continue_after_srt_error(skip_subtitles=False)

        self.assertEqual(skip.start_stage, Stage.CREATE_LEFT_RIGHT_FILES)
        self.assertTrue(skip.skip_subtitles)
        self.assertFalse(skip.continue_on_error)
        self.assertEqual(continue_on_error.start_stage, Stage.CREATE_LEFT_RIGHT_FILES)
        self.assertFalse(continue_on_error.skip_subtitles)
        self.assertTrue(continue_on_error.continue_on_error)

    def test_config_snapshot_restores_all_run_values(self) -> None:
        source_path = Path("original.mkv")
        with (
            patch.object(config, "source_path", source_path),
            patch.object(config, "remove_original", False),
            patch.object(config, "start_stage", Stage.CREATE_MKV),
            patch.object(config, "overwrite", False),
            patch.object(config, "continue_on_error", False),
            patch.object(config, "skip_subtitles", False),
        ):
            snapshot = ProcessingConfigSnapshot.from_config()
            request = ProcessingRequest(
                start_stage=Stage.EXTRACT_SUBTITLES,
                overwrite=True,
                continue_on_error=True,
                skip_subtitles=False,
                config_snapshot=snapshot,
            )
            config.source_path = Path("replacement.mkv")
            config.remove_original = True

            request.apply()

            self.assertEqual(config.source_path, source_path)
            self.assertFalse(config.remove_original)
            self.assertEqual(config.start_stage, Stage.EXTRACT_SUBTITLES)
            self.assertTrue(config.overwrite)


class ProcessingThreadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.request = ProcessingRequest(
            start_stage=Stage.EXTRACT_SUBTITLES,
            overwrite=True,
            continue_on_error=False,
            skip_subtitles=False,
        )

    def test_thread_runs_explicit_request_and_restores_stdout(self) -> None:
        runner = Mock()
        previous_stdout = sys.stdout
        thread = ProcessingThread(self.request, process_runner=runner, stop_runner=Mock())

        with patch("bd_to_avp.gui.processing.Spinner.stop_all"):
            thread.run()

        runner.assert_called_once_with(self.request)
        self.assertEqual(thread.outcome, ProcessingOutcome(ProcessingOutcomeKind.COMPLETED))
        self.assertIs(sys.stdout, previous_stdout)

    def test_processing_exception_is_stored_as_error_outcome(self) -> None:
        error = RuntimeError("missing subtitle output")
        thread = ProcessingThread(
            self.request,
            process_runner=Mock(side_effect=error),
            stop_runner=Mock(),
        )

        with patch("bd_to_avp.gui.processing.Spinner.stop_all"):
            thread.run()

        self.assertEqual(thread.outcome, ProcessingOutcome(ProcessingOutcomeKind.ERROR, error))

    def test_known_processing_errors_have_specific_outcomes(self) -> None:
        cases = (
            (MKVCreationError("mkv"), ProcessingOutcomeKind.MKV_CREATION_ERROR),
            (SRTCreationError("srt"), ProcessingOutcomeKind.SRT_CREATION_ERROR),
            (FileExistsError("file"), ProcessingOutcomeKind.FILE_EXISTS_ERROR),
        )

        for error, expected_kind in cases:
            with self.subTest(expected_kind=expected_kind):
                thread = ProcessingThread(
                    self.request,
                    process_runner=Mock(side_effect=error),
                    stop_runner=Mock(),
                )
                with patch("bd_to_avp.gui.processing.Spinner.stop_all"):
                    thread.run()
                self.assertEqual(thread.outcome, ProcessingOutcome(expected_kind, error))

    def test_batch_error_source_is_preserved_in_outcome(self) -> None:
        source_path = Path("failed.m2ts")
        batch_sources = (Path("before.m2ts"), source_path, Path("after.m2ts"))
        error = MKVCreationError("mkv")
        thread = ProcessingThread(
            self.request,
            process_runner=Mock(side_effect=BatchProcessingError(source_path, error, batch_sources)),
            stop_runner=Mock(),
        )

        with patch("bd_to_avp.gui.processing.Spinner.stop_all"):
            thread.run()

        self.assertEqual(
            thread.outcome,
            ProcessingOutcome(ProcessingOutcomeKind.MKV_CREATION_ERROR, error, source_path, batch_sources),
        )

    def test_non_exception_worker_exit_is_stored_as_failed_outcome(self) -> None:
        error = SystemExit("boom")
        thread = ProcessingThread(
            self.request,
            process_runner=Mock(side_effect=error),
            stop_runner=Mock(),
        )

        with patch("bd_to_avp.gui.processing.Spinner.stop_all"):
            thread.run()

        self.assertEqual(thread.outcome, ProcessingOutcome(ProcessingOutcomeKind.FAILED, error))

    def test_cancel_requests_cooperative_stop_without_running_work(self) -> None:
        runner = Mock()
        stop_runner = Mock()
        thread = ProcessingThread(self.request, process_runner=runner, stop_runner=stop_runner)

        thread.request_cancel()
        thread.request_cancel()
        with patch("bd_to_avp.gui.processing.Spinner.stop_all"):
            thread.run()

        stop_runner.assert_called_once_with()
        runner.assert_not_called()
        self.assertEqual(thread.outcome, ProcessingOutcome(ProcessingOutcomeKind.CANCELLED))

    def test_cancel_during_real_thread_run_wins_over_completion(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def runner(_request: ProcessingRequest) -> None:
            started.set()
            release.wait(timeout=2)

        thread = ProcessingThread(self.request, process_runner=runner, stop_runner=release.set)
        thread.start()
        self.assertTrue(started.wait(timeout=1))

        thread.request_cancel()
        self.assertTrue(thread.wait(2000))

        self.assertEqual(thread.outcome, ProcessingOutcome(ProcessingOutcomeKind.CANCELLED))
        self.assertTrue(thread.cancel_requested)

    def test_stdout_router_only_redirects_owner_thread(self) -> None:
        owner_stream = StringIO()
        fallback_stream = StringIO()
        router = ThreadScopedOutput(threading.get_ident(), owner_stream, fallback_stream)
        other_thread = threading.Thread(target=router.write, args=("other",))

        router.write("owner")
        other_thread.start()
        other_thread.join()

        self.assertEqual(owner_stream.getvalue(), "owner")
        self.assertEqual(fallback_stream.getvalue(), "other")

    def test_stdout_router_exposes_owner_writer_for_child_threads(self) -> None:
        owner_stream = StringIO()
        fallback_stream = StringIO()
        router = ThreadScopedOutput(threading.get_ident(), owner_stream, fallback_stream)
        owner_writer = router.current_writer()
        child_thread = threading.Thread(target=owner_writer, args=("spinner",))

        child_thread.start()
        child_thread.join()

        self.assertEqual(owner_stream.getvalue(), "spinner")
        self.assertEqual(fallback_stream.getvalue(), "")

    def test_spinner_helper_captures_worker_output_writer(self) -> None:
        owner_stream = StringIO()
        fallback_stream = StringIO()
        router = ThreadScopedOutput(threading.get_ident(), owner_stream, fallback_stream)

        with patch("bd_to_avp.modules.command.sys.stdout", router):
            update_func = get_spinner_update_func()

        self.assertIsNotNone(update_func)
        child_thread = threading.Thread(target=update_func, args=("spinner",))
        child_thread.start()
        child_thread.join()

        self.assertEqual(owner_stream.getvalue(), "spinner")
        self.assertEqual(fallback_stream.getvalue(), "")


class ProcessingControllerTests(unittest.TestCase):
    app: QApplication

    @classmethod
    def setUpClass(cls) -> None:
        app = QApplication.instance()
        cls.app = app if isinstance(app, QApplication) else QApplication([])

    def setUp(self) -> None:
        self.config_snapshot = ProcessingConfigSnapshot.from_config()
        self.request = ProcessingRequest(
            start_stage=Stage.CREATE_MKV,
            overwrite=False,
            continue_on_error=False,
            skip_subtitles=False,
        )
        self.threads: list[_FakeProcessingThread] = []

    def tearDown(self) -> None:
        self.config_snapshot.apply()

    def make_thread(self, request: ProcessingRequest, parent: QObject | None = None) -> "_FakeProcessingThread":
        thread = _FakeProcessingThread(request, parent)
        self.threads.append(thread)
        return thread

    def test_controller_owns_one_attempt_and_dispatches_after_finish(self) -> None:
        controller = ProcessingController()
        completions: list[ProcessingRequest] = []
        controller.process_completed.connect(completions.append)  # type: ignore[arg-type]

        with patch("bd_to_avp.gui.processing.ProcessingThread", side_effect=self.make_thread):
            self.assertTrue(controller.start(self.request))
            self.assertTrue(controller.is_active)
            self.assertFalse(controller.start(self.request.overwrite_existing_output()))
            first_thread = self.threads[0]
            self.assertTrue(first_thread.started)

            first_thread.finish(ProcessingOutcome(ProcessingOutcomeKind.COMPLETED))

            self.assertFalse(controller.is_active)
            self.assertEqual(completions, [self.request])
            self.assertTrue(first_thread.deleted)
            self.assertTrue(controller.start(self.request.overwrite_existing_output()))
            self.assertIsNot(self.threads[1], first_thread)

    def test_controller_cancel_targets_only_active_attempt(self) -> None:
        controller = ProcessingController()

        with patch("bd_to_avp.gui.processing.ProcessingThread", side_effect=self.make_thread):
            self.assertFalse(controller.request_cancel())
            controller.start(self.request)
            self.assertTrue(controller.request_cancel())

        self.assertTrue(self.threads[0].cancel_requested)

    def test_controller_emits_idle_after_attempt_finishes(self) -> None:
        controller = ProcessingController()
        idle = Mock()
        controller.processing_became_idle.connect(idle)

        with patch("bd_to_avp.gui.processing.ProcessingThread", side_effect=self.make_thread):
            controller.start(self.request)
            self.threads[0].finish(ProcessingOutcome(ProcessingOutcomeKind.COMPLETED))

        idle.assert_called_once_with()

    def test_controller_late_cancel_overrides_completed_outcome(self) -> None:
        controller = ProcessingController()
        cancellations: list[ProcessingRequest] = []
        completions: list[ProcessingRequest] = []
        controller.process_cancelled.connect(cancellations.append)  # type: ignore[arg-type]
        controller.process_completed.connect(completions.append)  # type: ignore[arg-type]

        with patch("bd_to_avp.gui.processing.ProcessingThread", side_effect=self.make_thread):
            controller.start(self.request)
            controller.request_cancel()
            self.threads[0].finish(ProcessingOutcome(ProcessingOutcomeKind.COMPLETED))

        self.assertEqual(cancellations, [self.request])
        self.assertEqual(completions, [])

    def test_controller_shutdown_waits_for_active_attempt(self) -> None:
        controller = ProcessingController()

        with patch("bd_to_avp.gui.processing.ProcessingThread", side_effect=self.make_thread):
            controller.start(self.request)
            self.assertTrue(controller.shutdown(timeout_ms=250))

        thread = self.threads[0]
        self.assertTrue(thread.cancel_requested)
        self.assertEqual(thread.wait_timeouts, [250])
        self.assertTrue(thread.deleted)
        self.assertFalse(controller.is_active)

    def test_controller_shutdown_timeout_keeps_active_attempt(self) -> None:
        controller = ProcessingController()

        with patch("bd_to_avp.gui.processing.ProcessingThread", side_effect=self.make_thread):
            controller.start(self.request)
            thread = self.threads[0]
            thread.wait_result = False
            self.assertFalse(controller.shutdown(timeout_ms=250))

        self.assertTrue(thread.cancel_requested)
        self.assertEqual(thread.wait_timeouts, [250])
        self.assertFalse(thread.deleted)
        self.assertTrue(controller.is_active)

    def test_controller_relays_progress_and_error_outcome(self) -> None:
        controller = ProcessingController()
        progress: list[str] = []
        errors: list[tuple[ProcessingRequest, BaseException]] = []
        controller.progress_updated.connect(progress.append)  # type: ignore[arg-type]
        controller.error_occurred.connect(lambda request, error: errors.append((request, error)))
        error = RuntimeError("failed")

        with patch("bd_to_avp.gui.processing.ProcessingThread", side_effect=self.make_thread):
            controller.start(self.request)
            self.threads[0].progress_updated.emit("working")
            self.threads[0].finish(ProcessingOutcome(ProcessingOutcomeKind.ERROR, error))

        self.assertEqual(progress, ["working"])
        self.assertEqual(errors, [(self.request, error)])

    def test_controller_adds_failed_batch_source_to_emitted_request(self) -> None:
        controller = ProcessingController()
        failures: list[tuple[ProcessingRequest, BaseException]] = []
        controller.mkv_creation_error.connect(lambda request, error: failures.append((request, error)))
        source_path = Path("failed.m2ts")
        batch_sources = (Path("before.m2ts"), source_path, Path("after.m2ts"))
        error = MKVCreationError("failed")

        with patch("bd_to_avp.gui.processing.ProcessingThread", side_effect=self.make_thread):
            controller.start(self.request)
            self.threads[0].finish(
                ProcessingOutcome(
                    ProcessingOutcomeKind.MKV_CREATION_ERROR,
                    error,
                    source_path,
                    batch_sources,
                )
            )

        emitted_request, emitted_error = failures[0]
        self.assertEqual(emitted_request.resume_source_path, source_path)
        self.assertEqual(emitted_request.batch_sources, batch_sources)
        self.assertEqual(emitted_request.batch_start_stage, self.request.start_stage)
        self.assertIs(emitted_error, error)

    def test_real_controller_cancel_clears_active_thread(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def runner(_request: ProcessingRequest) -> None:
            started.set()
            release.wait(timeout=2)

        def make_real_thread(request: ProcessingRequest, parent: QObject | None = None) -> ProcessingThread:
            return ProcessingThread(request, parent, process_runner=runner, stop_runner=release.set)

        controller = ProcessingController()
        cancellations: list[ProcessingRequest] = []
        controller.process_cancelled.connect(cancellations.append)  # type: ignore[arg-type]

        with patch("bd_to_avp.gui.processing.ProcessingThread", side_effect=make_real_thread):
            self.assertTrue(controller.start(self.request))
            self.assertTrue(started.wait(timeout=1))
            self.assertTrue(controller.request_cancel())

            deadline = time.monotonic() + 2
            while controller.is_active and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.01)

        self.assertFalse(controller.is_active)
        self.assertEqual(cancellations, [self.request])


class MainWindowProcessingLifecycleTests(unittest.TestCase):
    app: QApplication

    @classmethod
    def setUpClass(cls) -> None:
        app = QApplication.instance()
        cls.app = app if isinstance(app, QApplication) else QApplication([])
        cls.app.setApplicationDisplayName("BD_to_AVP Test")

    def setUp(self) -> None:
        self.config_snapshot = ProcessingConfigSnapshot.from_config()
        self.window = MainWindow()
        self.request = ProcessingRequest(
            start_stage=Stage.CREATE_LEFT_RIGHT_FILES,
            overwrite=True,
            continue_on_error=False,
            skip_subtitles=False,
        )

    def tearDown(self) -> None:
        self.window.close()
        self.config_snapshot.apply()

    def test_continuation_starts_explicit_request_and_updates_button(self) -> None:
        with patch.object(self.window.processing_controller, "start", return_value=True) as start:
            self.window.start_processing(is_continuing=True, request=self.request)

        start.assert_called_once_with(self.request)
        self.assertTrue(self.window.process_button.isEnabled())
        self.assertEqual(self.window.process_button.text(), self.window.STOP_PROCESSING_TEXT)
        self.assertFalse(self.window.load_config_button.isEnabled())
        self.assertFalse(self.window.save_config_button.isEnabled())

    def test_continuation_requires_explicit_request(self) -> None:
        with self.assertRaisesRegex(ValueError, "continuation request"):
            self.window.start_processing(is_continuing=True)

    def test_stop_waits_for_cancelled_outcome_before_reset(self) -> None:
        self.window.process_start_time = datetime.now()
        with patch.object(self.window.processing_controller, "request_cancel", return_value=True):
            self.window.stop_processing()

        self.assertFalse(self.window.process_button.isEnabled())
        self.assertEqual(self.window.process_button.text(), self.window.STOPPING_PROCESSING_TEXT)

        self.window.processing_cancelled(self.request)

        self.assertTrue(self.window.process_button.isEnabled())
        self.assertEqual(self.window.process_button.text(), self.window.START_PROCESSING_TEXT)
        self.assertIn("Processing stopped", self.window.processing_output_textedit.toPlainText())

    def test_process_button_stops_real_active_attempt(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def runner(_request: ProcessingRequest) -> None:
            started.set()
            release.wait(timeout=2)

        def make_thread(request: ProcessingRequest, parent: QObject | None = None) -> ProcessingThread:
            return ProcessingThread(request, parent, process_runner=runner, stop_runner=release.set)

        with patch("bd_to_avp.gui.processing.ProcessingThread", side_effect=make_thread):
            self.window.process_start_time = datetime.now()
            self.window.start_processing(is_continuing=True, request=self.request)
            self.assertTrue(started.wait(timeout=1))

            self.window.process_button.click()
            self.assertEqual(self.window.process_button.text(), self.window.STOPPING_PROCESSING_TEXT)

            deadline = time.monotonic() + 2
            while self.window.processing_controller.is_active and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.01)

        self.assertFalse(self.window.processing_controller.is_active)
        self.assertEqual(self.window.process_button.text(), self.window.START_PROCESSING_TEXT)
        self.assertIn("Processing stopped", self.window.processing_output_textedit.toPlainText())

    def test_mkv_error_continuation_starts_transformed_request(self) -> None:
        error = MKVCreationError("mkv failed")
        with (
            patch.object(self.window, "notify_user_with_sound"),
            patch.object(QMessageBox, "critical", return_value=QMessageBox.StandardButton.Yes),
            patch.object(self.window, "start_processing") as start,
        ):
            self.window.handle_mkv_creation_error(self.request, error)

        start.assert_called_once_with(
            is_continuing=True,
            request=self.request.continue_after_mkv_error(),
        )

    def test_file_exists_continuation_starts_transformed_request(self) -> None:
        error = FileExistsError("exists")
        with (
            patch.object(self.window, "notify_user_with_sound"),
            patch.object(QMessageBox, "critical", return_value=QMessageBox.StandardButton.Yes),
            patch.object(self.window, "start_processing") as start,
        ):
            self.window.handle_file_exists_error(self.request, error)

        start.assert_called_once_with(
            is_continuing=True,
            request=self.request.overwrite_existing_output(),
        )

    def test_close_is_blocked_until_processing_shutdown_finishes(self) -> None:
        event = QCloseEvent()
        with (
            patch.object(self.window.processing_controller, "shutdown", return_value=False),
            patch.object(QMessageBox, "warning") as warning,
        ):
            self.window.closeEvent(event)

        self.assertFalse(event.isAccepted())
        warning.assert_called_once()


class _FakeProcessingThread(QObject):
    progress_updated = Signal(str)
    finished = Signal()

    def __init__(self, request: ProcessingRequest, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.request = request
        self.outcome: ProcessingOutcome | None = None
        self.started = False
        self.cancel_requested = False
        self.deleted = False
        self.wait_timeouts: list[int] = []
        self.wait_result = True

    def start(self) -> None:
        self.started = True

    def request_cancel(self) -> None:
        self.cancel_requested = True

    def deleteLater(self) -> None:
        self.deleted = True

    def wait(self, timeout_ms: int) -> bool:
        self.wait_timeouts.append(timeout_ms)
        return self.wait_result

    def finish(self, outcome: ProcessingOutcome) -> None:
        self.outcome = outcome
        self.finished.emit()


if __name__ == "__main__":
    unittest.main()
