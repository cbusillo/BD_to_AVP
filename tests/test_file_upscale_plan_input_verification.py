import dataclasses
import tempfile
import unittest
from pathlib import Path

from scripts.qualify_direct_mv_hevc import QualificationFailure
from scripts.qualify_file_upscale_quality_mapping_selection import (
    DEFAULT_SELECTION_PLAN,
    load_mapping_selection_plan,
    verify_current_mapping_selection_inputs,
)
from scripts.qualify_file_upscale_quality_repeatability_calibration import (
    DEFAULT_CALIBRATION_PLAN,
    load_repeatability_calibration_plan,
    verify_current_repeatability_inputs,
)
from scripts.qualify_file_upscale_quality_sweep import (
    DEFAULT_SWEEP_PLAN,
    FileBinding,
    load_sweep_plan,
    verify_current_sweep_inputs,
)
from scripts.qualify_mv_hevc_quality_match import sha256_file


class PlanInputVerificationTests(unittest.TestCase):
    """Loading verifies the document; only a measurement run needs the live inputs."""

    def setUp(self) -> None:
        self.loaded = (
            (load_sweep_plan(DEFAULT_SWEEP_PLAN)[0], verify_current_sweep_inputs),
            (load_mapping_selection_plan(DEFAULT_SELECTION_PLAN)[0], verify_current_mapping_selection_inputs),
            (load_repeatability_calibration_plan(DEFAULT_CALIBRATION_PLAN)[0], verify_current_repeatability_inputs),
        )

    def test_a_run_is_refused_when_a_bound_tool_differs_from_the_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for plan, verify in self.loaded:
                current = self._plan_with_matching_inputs(plan, Path(directory))
                bound = current.bundled_tools["edge264_test"]
                bound.path.write_bytes(b"a different build of the tool")
                with self.subTest(plan=plan.experiment_id):
                    with self.assertRaisesRegex(QualificationFailure, "bundled tool edge264_test"):
                        verify(current)

    def test_a_run_is_refused_when_a_bound_tool_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for plan, verify in self.loaded:
                current = self._plan_with_matching_inputs(plan, Path(directory))
                current.bundled_tools["mp4box"].path.unlink()
                with self.subTest(plan=plan.experiment_id):
                    with self.assertRaisesRegex(QualificationFailure, "bundled tool mp4box"):
                        verify(current)

    def test_a_run_is_accepted_when_every_bound_input_matches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for plan, verify in self.loaded:
                with self.subTest(plan=plan.experiment_id):
                    verify(self._plan_with_matching_inputs(plan, Path(directory)))

    @classmethod
    def _plan_with_matching_inputs(cls, plan, directory: Path):
        matching = {
            field.name: cls._matching_binding(directory, field.name)
            for field in dataclasses.fields(plan)
            if isinstance(getattr(plan, field.name), FileBinding)
        }
        matching["bundled_tools"] = {key: cls._matching_binding(directory, key) for key in plan.bundled_tools}
        return dataclasses.replace(plan, **matching)

    @staticmethod
    def _matching_binding(directory: Path, name: str) -> FileBinding:
        path = directory / name
        path.write_bytes(name.encode())
        return FileBinding(path, sha256_file(path))


if __name__ == "__main__":
    unittest.main()
