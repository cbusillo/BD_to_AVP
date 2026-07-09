import plistlib
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

        self.assertEqual(bundle.path, app_path)
        self.assertEqual(bundle.executable.name, "SmokeApp")
        self.assertEqual(bundle.bundle_identifier, "com.example.smoke")
        self.assertEqual(bundle.short_version, "1.2.3")

    def test_smoke_app_passes_with_fake_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app_path = make_fake_app(Path(temp_dir), version="1.2.3")

            smoke_release_app.smoke_app(app_path, skip_spctl=True)

    def test_smoke_app_fails_when_default_gui_tool_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app_path = make_fake_app(Path(temp_dir), version="1.2.3")
            (app_path / smoke_release_app.APP_BIN_PATH / "MP4Box").unlink()

            with self.assertRaisesRegex(smoke_release_app.SmokeFailure, "MP4Box"):
                smoke_release_app.smoke_app(app_path, skip_spctl=True)

    def test_cli_version_must_match_info_plist(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app_path = make_fake_app(Path(temp_dir), version="1.2.3", executable_version="9.9.9")
            bundle = smoke_release_app.read_bundle(app_path)

            with self.assertRaisesRegex(smoke_release_app.SmokeFailure, "version mismatch"):
                smoke_release_app.verify_cli_version(bundle, smoke_release_app.build_clean_env())

    def test_apple_vision_ocr_smoke_requires_expected_cli_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app_path = make_fake_app(Path(temp_dir), version="1.2.3", apple_vision_smoke=False)
            bundle = smoke_release_app.read_bundle(app_path)

            with self.assertRaisesRegex(smoke_release_app.SmokeFailure, "Apple Vision OCR"):
                smoke_release_app.verify_apple_vision_ocr(bundle, smoke_release_app.build_clean_env())

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

            with patch.object(smoke_release_app, "run") as run:
                smoke_release_app.verify_bundled_tool(
                    bundle.bin_dir / "ffmpeg",
                    ["-version"],
                    clean_env=smoke_release_app.build_clean_env(),
                    check_links=False,
                )

        self.assertEqual(run.call_count, 1)


def make_fake_app(
    root: Path,
    *,
    version: str,
    executable_version: str | None = None,
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
            'if [ "$1" = "--version" ]; then',
            f"  echo 'BD-to_AVP Version {executable_version or version}'",
            "  exit 0",
            "fi",
            'if [ "$1" = "--help" ]; then',
            "  echo 'Process 3D Blu-ray to MV-HEVC compatible with the Apple Vision Pro.'",
            "  echo '--source SOURCE'",
            "  exit 0",
            "fi",
            'if [ "$1" = "-c" ]; then',
            f"  echo '{'Apple Vision OCR import smoke passed' if apple_vision_smoke else 'Apple Vision unavailable'}'",
            "  exit 0",
            "fi",
            "exit 0",
        ],
    )

    for tool_name in [*smoke_release_app.REQUIRED_BUNDLED_TOOLS, *smoke_release_app.OPTIONAL_BUNDLED_TOOLS]:
        write_executable(bin_path / tool_name, ["#!/bin/sh", "echo ok"])

    return app_path


def write_executable(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n")
    path.chmod(path.stat().st_mode | 0o111)


if __name__ == "__main__":
    unittest.main()
