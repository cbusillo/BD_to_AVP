import json
import plistlib
import shutil
import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from scripts import smoke_release_app


class ReleaseSmokeTests(unittest.TestCase):
    def test_read_bundle_uses_info_plist_executable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app_path = make_fake_app(Path(temp_dir), version="1.2.3")

            bundle = smoke_release_app.read_bundle(app_path)

        self.assertEqual(bundle.path, app_path.resolve())
        self.assertEqual(bundle.executable.name, "SmokeApp")
        self.assertEqual(bundle.worker_executable.name, smoke_release_app.WORKER_EXECUTABLE_NAME)
        self.assertEqual(bundle.bundle_identifier, "com.example.smoke")
        self.assertEqual(bundle.short_version, "1.2.3")

    def test_smoke_app_passes_with_fake_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app_path = make_fake_app(Path(temp_dir), version="1.2.3")

            with (
                patch.object(smoke_release_app, "launch_preview_application", side_effect=complete_preview_smoke),
                patch.object(smoke_release_app, "wait_for_preview_process_exit", return_value=True),
            ):
                smoke_release_app.smoke_app(app_path, skip_spctl=True)

    def test_smoke_app_fails_when_default_gui_tool_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app_path = make_fake_app(Path(temp_dir), version="1.2.3")
            (app_path / smoke_release_app.APP_BIN_PATH / "MP4Box").unlink()

            with self.assertRaisesRegex(smoke_release_app.SmokeFailure, "MP4Box"):
                smoke_release_app.smoke_app(app_path, skip_spctl=True)

    def test_native_startup_uses_supported_smoke_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app_path = make_fake_app(Path(temp_dir), version="1.2.3")
            bundle = smoke_release_app.read_bundle(app_path)

            with patch.object(smoke_release_app, "run") as run:
                run.return_value = smoke_release_app.subprocess.CompletedProcess(args=[], returncode=0, stdout="")
                smoke_release_app.verify_native_startup(bundle, smoke_release_app.build_clean_env())

        self.assertEqual(run.call_args.args[0], [bundle.executable, "--startup-smoke"])
        self.assertNotIn("--version", run.call_args.args[0])
        self.assertNotIn("--help", run.call_args.args[0])

    def test_apple_vision_ocr_smoke_requires_expected_cli_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app_path = make_fake_app(Path(temp_dir), version="1.2.3", apple_vision_smoke=False)
            bundle = smoke_release_app.read_bundle(app_path)

            with self.assertRaisesRegex(smoke_release_app.SmokeFailure, "Apple Vision OCR"):
                smoke_release_app.verify_apple_vision_ocr(bundle, smoke_release_app.build_clean_env())

    def test_apple_vision_ocr_smoke_uses_worker_smoke_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app_path = make_fake_app(Path(temp_dir), version="1.2.3")
            bundle = smoke_release_app.read_bundle(app_path)

            with patch.object(smoke_release_app, "run") as run:
                run.return_value = smoke_release_app.subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="Apple Vision OCR import smoke passed"
                )
                smoke_release_app.verify_apple_vision_ocr(bundle, smoke_release_app.build_clean_env())

        self.assertEqual(run.call_args.args[0], [bundle.worker_executable, "--smoke-apple-vision-ocr"])

    def test_preview_presentation_uses_native_smoke_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app_path = make_fake_app(Path(temp_dir), version="1.2.3")
            bundle = smoke_release_app.read_bundle(app_path)

            with (
                patch.object(
                    smoke_release_app,
                    "launch_preview_application",
                    side_effect=complete_preview_smoke,
                ) as launch_preview,
                patch.object(smoke_release_app, "wait_for_preview_process_exit", return_value=True),
            ):
                smoke_release_app.verify_preview_presentation(bundle, smoke_release_app.build_clean_env())

        self.assertEqual(launch_preview.call_args.args[0], bundle)

    def test_preview_presentation_launches_bundle_through_launchservices(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app_path = make_fake_app(Path(temp_dir), version="1.2.3")
            bundle = smoke_release_app.read_bundle(app_path)
            media_path = Path(temp_dir) / "preview.mov"
            result_path = Path(temp_dir) / "result.json"
            stdout_path = Path(temp_dir) / "stdout.log"
            stderr_path = Path(temp_dir) / "stderr.log"
            environment = {"HOME": temp_dir}

            with patch.object(smoke_release_app, "run") as run:
                smoke_release_app.launch_preview_application(
                    bundle,
                    media_path,
                    result_path,
                    stdout_path,
                    stderr_path,
                    environment=environment,
                    cwd=Path(temp_dir),
                )

        self.assertEqual(
            run.call_args.args[0],
            [
                "/usr/bin/open",
                "-F",
                "-n",
                "-o",
                stdout_path,
                "--stderr",
                stderr_path,
                "--env",
                f"HOME={temp_dir}",
                bundle.path,
                "--args",
                smoke_release_app.PREVIEW_PRESENTATION_SMOKE_ARGUMENT,
                media_path,
                result_path,
            ],
        )
        self.assertEqual(run.call_args.kwargs["env"], environment)
        self.assertEqual(run.call_args.kwargs["timeout"], smoke_release_app.PREVIEW_LAUNCH_TIMEOUT_SECONDS)

    def test_preview_presentation_timeout_terminates_matching_process(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app_path = make_fake_app(Path(temp_dir), version="1.2.3")
            bundle = smoke_release_app.read_bundle(app_path)
            with (
                patch.object(smoke_release_app, "launch_preview_application"),
                patch.object(smoke_release_app, "wait_for_path", return_value=False),
                patch.object(smoke_release_app, "preview_process_summary", return_value="matching process IDs: 42"),
                patch.object(
                    smoke_release_app, "read_preview_log_tail", side_effect=["launch stdout", "launch stderr"]
                ),
                patch.object(smoke_release_app, "terminate_preview_processes") as terminate,
            ):
                with self.assertRaises(smoke_release_app.SmokeFailure) as raised:
                    smoke_release_app.verify_preview_presentation(bundle, smoke_release_app.build_clean_env())

        terminate.assert_called_once()
        message = str(raised.exception)
        self.assertIn("result receipt", message)
        self.assertIn("matching process IDs: 42", message)
        self.assertIn("launch stdout", message)
        self.assertIn("launch stderr", message)

    def test_preview_presentation_exit_timeout_terminates_matching_process(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app_path = make_fake_app(Path(temp_dir), version="1.2.3")
            bundle = smoke_release_app.read_bundle(app_path)
            with (
                patch.object(
                    smoke_release_app,
                    "launch_preview_application",
                    side_effect=complete_preview_smoke,
                ),
                patch.object(smoke_release_app, "wait_for_preview_process_exit", return_value=False),
                patch.object(smoke_release_app, "terminate_preview_processes") as terminate,
                self.assertRaisesRegex(smoke_release_app.SmokeFailure, "did not terminate"),
            ):
                smoke_release_app.verify_preview_presentation(bundle, smoke_release_app.build_clean_env())

        terminate.assert_called_once()

    def test_command_timeout_becomes_smoke_failure(self) -> None:
        timeout = smoke_release_app.subprocess.TimeoutExpired([], 1)
        with patch("scripts.smoke_release_app.subprocess.run", side_effect=timeout):
            with self.assertRaisesRegex(smoke_release_app.SmokeFailure, "exceeded 1 seconds"):
                smoke_release_app.run(["tool"], timeout=1)

    def test_default_app_search_uses_first_existing_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            first_path = Path(temp_dir) / "Missing.app"
            second_path = make_fake_app(Path(temp_dir), version="1.2.3")

            with patch.object(smoke_release_app, "DEFAULT_APP_PATHS", [first_path, second_path]):
                self.assertEqual(smoke_release_app.find_default_app(), second_path)

    def test_bundled_tool_linkage_check_detects_homebrew_library(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app_path = make_fake_app(Path(temp_dir), version="1.2.3")
            bundle = smoke_release_app.read_bundle(app_path)
            clean_env = smoke_release_app.build_clean_env()

            def fake_run(command: list[str | Path], *, env: dict[str, str] | None = None):
                if command[:2] == ["otool", "-L"]:
                    return smoke_release_app.subprocess.CompletedProcess(
                        args=command,
                        returncode=0,
                        stdout="/opt/homebrew/lib/libexample.dylib\n",
                    )
                if "-encoders" in command:
                    return smoke_release_app.subprocess.CompletedProcess(
                        args=command, returncode=0, stdout=" V..... libsvtav1\n"
                    )
                if "-bsfs" in command:
                    return smoke_release_app.subprocess.CompletedProcess(
                        args=command, returncode=0, stdout="av1_metadata\n"
                    )
                return smoke_release_app.subprocess.CompletedProcess(args=command, returncode=0, stdout="")

            with patch.object(smoke_release_app, "run", side_effect=fake_run):
                with self.assertRaisesRegex(smoke_release_app.SmokeFailure, "/opt/homebrew"):
                    smoke_release_app.verify_bundled_tool(
                        bundle.bin_dir / "ffmpeg",
                        ["-version"],
                        clean_env=clean_env,
                        check_links=True,
                    )

    def test_bundled_tool_linkage_check_detects_usr_local_library(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app_path = make_fake_app(Path(temp_dir), version="1.2.3")
            bundle = smoke_release_app.read_bundle(app_path)
            clean_env = smoke_release_app.build_clean_env()

            def fake_run(command: list[str | Path], *, env: dict[str, str] | None = None):
                if command[:2] == ["otool", "-L"]:
                    return smoke_release_app.subprocess.CompletedProcess(
                        args=command,
                        returncode=0,
                        stdout="/usr/local/lib/libexample.dylib\n",
                    )
                return smoke_release_app.subprocess.CompletedProcess(args=command, returncode=0, stdout="")

            with patch.object(smoke_release_app, "run", side_effect=fake_run):
                with self.assertRaisesRegex(smoke_release_app.SmokeFailure, "/usr/local"):
                    smoke_release_app.verify_bundled_tool(
                        bundle.bin_dir / "MP4Box",
                        ["-version"],
                        clean_env=clean_env,
                        check_links=True,
                    )

    def test_bundled_tool_linkage_check_can_be_skipped_without_false_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app_path = make_fake_app(Path(temp_dir), version="1.2.3")
            bundle = smoke_release_app.read_bundle(app_path)

            def fake_run(command: list[str | Path], *, env: dict[str, str] | None = None):
                output = "av1_metadata\n" if "-bsfs" in command else " V..... libsvtav1\n"
                return smoke_release_app.subprocess.CompletedProcess(args=command, returncode=0, stdout=output)

            with patch.object(smoke_release_app, "run", side_effect=fake_run) as run:
                smoke_release_app.verify_bundled_tool(
                    bundle.bin_dir / "ffmpeg",
                    ["-version"],
                    clean_env=smoke_release_app.build_clean_env(),
                    check_links=False,
                )

        self.assertEqual(run.call_count, 3)

    def test_bundled_ffmpeg_requires_av1_metadata_filter(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app_path = make_fake_app(Path(temp_dir), version="1.2.3")
            bundle = smoke_release_app.read_bundle(app_path)

            def fake_run(command: list[str | Path], *, env: dict[str, str] | None = None):
                output = "extract_extradata\n" if "-bsfs" in command else " V..... libsvtav1\n"
                return smoke_release_app.subprocess.CompletedProcess(args=command, returncode=0, stdout=output)

            with (
                patch.object(smoke_release_app, "run", side_effect=fake_run),
                self.assertRaisesRegex(smoke_release_app.SmokeFailure, "av1_metadata"),
            ):
                smoke_release_app.verify_bundled_tool(
                    bundle.bin_dir / "ffmpeg",
                    ["-version"],
                    clean_env=smoke_release_app.build_clean_env(),
                    check_links=False,
                )


def make_fake_app(
    root: Path,
    *,
    version: str,
    apple_vision_smoke: bool = True,
) -> Path:
    app_path = root / "3D Blu-ray to Vision Pro.app"
    contents_path = app_path / "Contents"
    macos_path = contents_path / "MacOS"
    resource_app_path = contents_path / "Resources" / "app"
    bin_path = resource_app_path / "bd_to_avp" / "bin"
    macos_path.mkdir(parents=True)
    bin_path.mkdir(parents=True)

    info = {
        "CFBundleExecutable": "SmokeApp",
        "CFBundleIdentifier": "com.example.smoke",
        "CFBundleShortVersionString": version,
    }
    with (contents_path / "Info.plist").open("wb") as plist_file:
        plistlib.dump(info, plist_file)

    write_executable(
        macos_path / "SmokeApp",
        [
            "#!/bin/sh",
            'if [ "$1" = "--startup-smoke" ]; then',
            "  exit 0",
            "fi",
            'if [ "$1" = "--preview-presentation-smoke" ]; then',
            '  artifact_directory="$(dirname "$2")"',
            '  rm -rf "$artifact_directory"',
            (
                "  printf '%s\\n' "
                '\'{"schema_version":1,"player_presented":true,"media_ready":true,'
                '"cleanup_complete":true,"message":null}\' '
                '> "$3"'
            ),
            "  exit 0",
            "fi",
            "exit 91",
        ],
    )

    write_executable(
        macos_path / smoke_release_app.WORKER_EXECUTABLE_NAME,
        [
            "#!/usr/bin/python3",
            "import json",
            "import sys",
            f"version = {version!r}",
            f"apple_vision_smoke = {apple_vision_smoke!r}",
            "if sys.argv[1:] == ['--smoke-apple-vision-ocr']:",
            "    print('Apple Vision OCR import smoke passed' if apple_vision_smoke else 'Apple Vision unavailable')",
            "    raise SystemExit(0)",
            "request = json.loads(sys.stdin.readline())",
            "job_id = request['job_id']",
            "event_types = ['worker.ready', 'job.started', 'stage.started', 'job.completed']",
            "for sequence, event_type in enumerate(event_types):",
            "    payload = {'worker_version': version, 'process_group_id': 1} if event_type == 'worker.ready' else {}",
            "    event = {'protocol_version': 10, 'type': event_type, 'job_id': job_id}",
            "    event.update(sequence=sequence, payload=payload)",
            "    print(json.dumps(event))",
        ],
    )

    for tool_name in [*smoke_release_app.REQUIRED_BUNDLED_TOOLS, *smoke_release_app.OPTIONAL_BUNDLED_TOOLS]:
        lines = ["#!/bin/sh", "echo ok"]
        if tool_name == "ffmpeg":
            lines = [
                "#!/bin/sh",
                'if [ "$2" = "-encoders" ]; then echo " V..... libsvtav1"; exit 0; fi',
                'if [ "$2" = "-bsfs" ]; then echo "av1_metadata"; exit 0; fi',
                'if [ "$2" = "-version" ]; then echo "ffmpeg version smoke"; exit 0; fi',
                "output=''",
                'for argument in "$@"; do output="$argument"; done',
                ': > "$output"',
                "echo ok",
            ]
        write_executable(bin_path / tool_name, lines)

    return app_path


def write_executable(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n")
    path.chmod(path.stat().st_mode | 0o111)


def complete_preview_smoke(
    bundle: smoke_release_app.AppBundle,
    media_path: Path,
    result_path: Path,
    stdout_path: Path,
    stderr_path: Path,
    *,
    environment: dict[str, str],
    cwd: Path,
) -> None:
    del bundle, stdout_path, stderr_path, environment, cwd
    shutil.rmtree(media_path.parent)
    result_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "player_presented": True,
                "media_ready": True,
                "cleanup_complete": True,
                "message": None,
            }
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    unittest.main()
