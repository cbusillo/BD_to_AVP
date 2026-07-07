import unittest
from unittest.mock import patch

from bd_to_avp.gui.processing import ProcessingThread


class ProcessingThreadTests(unittest.TestCase):
    def test_unhandled_processing_error_emits_failure_signal(self) -> None:
        thread = ProcessingThread()
        failures = []
        thread.process_failed.connect(lambda: failures.append(True))

        with (
            patch("bd_to_avp.gui.processing.start_process", side_effect=AssertionError("boom")),
            patch("bd_to_avp.gui.processing.Spinner.stop_all"),
        ):
            with self.assertRaises(AssertionError):
                thread.run()

        self.assertEqual(failures, [True])


if __name__ == "__main__":
    unittest.main()
