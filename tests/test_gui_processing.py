import unittest
from unittest.mock import patch

from bd_to_avp.gui.processing import ProcessingThread


class ProcessingThreadTests(unittest.TestCase):
    def test_processing_exception_emits_error_signal(self) -> None:
        thread = ProcessingThread()
        errors: list[Exception] = []
        failures: list[bool] = []
        thread.error_occurred.connect(errors.append)  # type: ignore[arg-type]
        thread.process_failed.connect(lambda: failures.append(True))

        with (
            patch("bd_to_avp.gui.processing.start_process", side_effect=StopIteration("missing subtitle output")),
            patch("bd_to_avp.gui.processing.Spinner.stop_all"),
        ):
            thread.run()

        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], StopIteration)
        self.assertEqual(failures, [])

    def test_non_exception_worker_exit_emits_failure_signal(self) -> None:
        thread = ProcessingThread()
        failures: list[bool] = []
        thread.process_failed.connect(lambda: failures.append(True))

        with (
            patch("bd_to_avp.gui.processing.start_process", side_effect=SystemExit("boom")),
            patch("bd_to_avp.gui.processing.Spinner.stop_all"),
        ):
            with self.assertRaises(SystemExit):
                thread.run()

        self.assertEqual(failures, [True])


if __name__ == "__main__":
    unittest.main()
