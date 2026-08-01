import stat
import subprocess
import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from bd_to_avp.modules.video_quality_defaults import (
    DIRECT_METALFX_2X_QUALITY_BY_STEP,
    DIRECT_QUALITY_BY_STEP,
    FILE_UPSCALE_QUALITY_BY_STEP,
    QUALITY_STEP_IDS,
)
from scripts.qualify_packaged_video_quality_routes import (
    HARNESS_PATHS,
    PREVIEW_DURATION_SECONDS,
    REPOSITORY_ROOT,
    _clone_directory,
    _harness_evidence,
    _validate_fixture_directory,
    existing_artifact_cases,
    qualification_cases,
    run_qualification,
)
from scripts.verify_packaged_mv_hevc_routes import PackagedRouteFailure


class PackagedQualityCaseTests(unittest.TestCase):
    def test_cases_cover_every_supported_route_mapping(self) -> None:
        cases = qualification_cases()
        ordinary = [case for case in cases if case.target == "direct_mv_hevc"]
        metalfx = [case for case in cases if case.target == "direct_mv_hevc_metalfx_2x"]
        generated = [case for case in cases if case.target == "generated_mv_hevc"]
        file_upscale = existing_artifact_cases()

        self.assertEqual([case.step_id for case in ordinary], list(QUALITY_STEP_IDS))
        self.assertEqual([case.step_id for case in metalfx], list(QUALITY_STEP_IDS))
        self.assertEqual([case.step_id for case in generated], ["balanced"])
        self.assertEqual([case.step_id for case in file_upscale], ["balanced", "detailed"])
        self.assertEqual(len(cases) * 2 + len(file_upscale), 32)
        self.assertEqual(PREVIEW_DURATION_SECONDS, 12)

    def test_direct_cases_bind_exact_quality_and_balanced_only_fallback(self) -> None:
        for case in qualification_cases():
            if case.target not in {"direct_mv_hevc", "direct_mv_hevc_metalfx_2x"}:
                continue
            expected_quality = (
                DIRECT_METALFX_2X_QUALITY_BY_STEP[case.step_id]
                if case.target == "direct_mv_hevc_metalfx_2x"
                else DIRECT_QUALITY_BY_STEP[case.step_id]
            )
            self.assertEqual(case.video_options["direct_quality"], expected_quality)
            self.assertEqual(case.expected_route["quality"], expected_quality)
            if case.step_id == "balanced":
                self.assertIn("generated_fallback", case.video_options)
            else:
                self.assertNotIn("generated_fallback", case.video_options)

    def test_metalfx_cases_include_file_fallback_quality_only_for_balanced(self) -> None:
        metalfx = [case for case in qualification_cases() if case.target == "direct_mv_hevc_metalfx_2x"]

        for case in metalfx:
            self.assertTrue(case.upscale_options["enabled"])
            if case.step_id == "balanced":
                self.assertEqual(
                    case.upscale_options["quality"],
                    FILE_UPSCALE_QUALITY_BY_STEP["balanced"],
                )
            else:
                self.assertNotIn("quality", case.upscale_options)

    def test_existing_artifact_cases_are_full_only_checked_upscales(self) -> None:
        for case in existing_artifact_cases():
            expected_quality = FILE_UPSCALE_QUALITY_BY_STEP[case.step_id]
            self.assertEqual(case.video_options["route_intent"], "existing_artifact")
            self.assertEqual(case.upscale_options, {"enabled": True, "quality": expected_quality})
            self.assertEqual(case.expected_route["upscale_quality"], expected_quality)


class PackagedQualityReceiptTests(unittest.TestCase):
    def test_clone_directory_copies_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source"
            source.mkdir()
            (source / "artifact.mov").write_bytes(b"artifact")
            destination = root / "destination"

            _clone_directory(source, destination)

            self.assertEqual((destination / "artifact.mov").read_bytes(), b"artifact")

    def test_clone_directory_falls_back_to_ditto(self) -> None:
        clone_failure = subprocess.CompletedProcess(["cp"], 1, stdout="", stderr="clone failed")
        ditto_success = subprocess.CompletedProcess(["ditto"], 0, stdout="", stderr="")

        with patch(
            "scripts.qualify_packaged_video_quality_routes.subprocess.run",
            side_effect=[clone_failure, ditto_success],
        ) as run:
            _clone_directory(Path("source"), Path("destination"))

        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[1].args[0][0], "ditto")

    def test_fixture_directory_expands_home_before_freshness_check(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fixture_directory = root / "fixtures"
            fixture_directory.mkdir()

            with (
                patch.dict("os.environ", {"HOME": root.as_posix()}),
                self.assertRaisesRegex(PackagedRouteFailure, "fresh path"),
            ):
                _validate_fixture_directory(
                    root / "Qualification.app",
                    root / "source.mkv",
                    root / "evidence.json",
                    Path("~/fixtures"),
                )

    def test_harness_evidence_binds_clean_commit_and_public_files(self) -> None:
        def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            if command[1:3] == ["status", "--porcelain=v1"]:
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
            if command[1:3] == ["rev-parse", "HEAD"]:
                return subprocess.CompletedProcess(command, 0, stdout="a" * 40 + "\n", stderr="")
            return subprocess.CompletedProcess(command, 0, stdout=command[-1] + "\n", stderr="")

        with (
            patch("scripts.qualify_packaged_video_quality_routes.subprocess.run", side_effect=fake_run),
            patch("scripts.qualify_packaged_video_quality_routes.sha256_file", return_value="b" * 64),
        ):
            evidence = _harness_evidence()

        self.assertEqual(evidence["source_commit"], "a" * 40)
        self.assertFalse(evidence["source_tree_dirty"])
        self.assertEqual(
            [entry["path"] for entry in evidence["files"]],
            [path.relative_to(REPOSITORY_ROOT).as_posix() for path in HARNESS_PATHS],
        )
        self.assertEqual({entry["sha256"] for entry in evidence["files"]}, {"b" * 64})

    def test_qualification_publishes_fresh_read_only_receipt_and_fixtures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            app = root / "Qualification.app"
            app.mkdir()
            source = root / "source.mkv"
            source.write_bytes(b"source")
            output = root / "evidence.json"
            fixtures = root / "fixtures"

            def fake_verify(
                _app: Path,
                _source: Path,
                *,
                fixture_directory: Path,
                expected_source_sha256: str,
            ) -> dict[str, object]:
                self.assertEqual(expected_source_sha256, "expected")
                fixture = fixture_directory / "anchor.mov"
                fixture.write_bytes(b"fixture")
                fixture.chmod(0o444)
                return {"schema_version": 1, "acceptance": {"passed": True}}

            with patch(
                "scripts.qualify_packaged_video_quality_routes.verify_packaged_video_quality_routes",
                side_effect=fake_verify,
            ):
                evidence = run_qualification(app, source, output, fixtures, "expected")

            self.assertTrue(evidence["acceptance"]["passed"])
            self.assertTrue(output.is_file())
            self.assertTrue((fixtures / "anchor.mov").is_file())
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o444)
            self.assertEqual(stat.S_IMODE(fixtures.stat().st_mode), 0o555)

            with self.assertRaisesRegex(PackagedRouteFailure, "already exists"):
                run_qualification(app, source, output, root / "other-fixtures", "expected")


if __name__ == "__main__":
    unittest.main()
