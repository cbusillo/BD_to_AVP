import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bd_to_avp import preflight
from bd_to_avp.modules import process
from bd_to_avp.modules.config import Stage


class ProcessPreflightTests(unittest.TestCase):
    def test_batch_processing_aborts_on_dependency_preflight_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source_folder = Path(temp_dir)
            (source_folder / "movie.m2ts").touch()

            with (
                patch.object(process.config, "source_folder_path", source_folder),
                patch.object(process, "process_each", side_effect=preflight.DependencyPreflightError("missing tool")),
                self.assertRaisesRegex(preflight.DependencyPreflightError, "missing tool"),
            ):
                process.process(Stage.CREATE_MKV)


if __name__ == "__main__":
    unittest.main()
