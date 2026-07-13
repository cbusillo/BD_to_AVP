import plistlib
import subprocess
import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from scripts import verify_app_tools


class VerifyAppToolsTests(unittest.TestCase):
    def test_required_tools_include_mp4box(self) -> None:
        self.assertIn("MP4Box", verify_app_tools.REQUIRED_TOOLS)
        self.assertEqual(verify_app_tools.REQUIRED_TOOLS["MP4Box"], ["-version"])

    def test_core_tools_match_ci_briefcase_smoke_scope(self) -> None:
        self.assertEqual(set(verify_app_tools.CORE_TOOLS), {"ffmpeg", "ffprobe", "MP4Box"})

    def test_required_tools_cover_gui_runtime_dependencies(self) -> None:
        self.assertEqual(
            set(verify_app_tools.REQUIRED_TOOLS),
            {
                "ffmpeg",
                "ffprobe",
                "edge264_test",
                "fx-upscale",
                "MP4Box",
                "spatial-media-kit-tool",
            },
        )

    def test_release_profile_adds_gui_runtime_dependencies(self) -> None:
        self.assertLess(set(verify_app_tools.CORE_TOOLS), set(verify_app_tools.REQUIRED_TOOLS))
        self.assertIn("edge264_test", verify_app_tools.REQUIRED_TOOLS)
        self.assertNotIn("edge264_test", verify_app_tools.CORE_TOOLS)

    def test_verify_tool_uses_probe_args_and_rejects_usr_local_linkage(self) -> None:
        with tempfile.NamedTemporaryFile() as tool_file:
            tool_path = Path(tool_file.name)
            tool_path.chmod(0o755)

            def fake_run(command: list[str | Path]):
                if command[:2] == ["otool", "-L"]:
                    return subprocess.CompletedProcess(
                        args=command,
                        returncode=0,
                        stdout="/usr/local/lib/libexample.dylib\n",
                    )
                self.assertEqual(command, [tool_path, "-version"])
                return subprocess.CompletedProcess(args=command, returncode=0, stdout="")

            with patch.object(verify_app_tools, "run", side_effect=fake_run):
                with self.assertRaisesRegex(RuntimeError, "/usr/local"):
                    verify_app_tools.verify_tool(tool_path, ["-version"])

    def test_rejects_mach_o_newer_than_app_minimum(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app_path = Path(temp_dir) / "Test.app"
            info_path = app_path / "Contents" / "Info.plist"
            binary_path = app_path / "Contents" / "Resources" / "app_packages" / "example.so"
            binary_path.parent.mkdir(parents=True)
            binary_path.write_bytes(b"binary")
            with info_path.open("wb") as handle:
                plistlib.dump({"LSMinimumSystemVersion": "14.0"}, handle)

            with (
                patch.object(verify_app_tools, "is_mach_o", return_value=True),
                patch.object(verify_app_tools, "minimum_macos_versions", return_value={"15.0"}),
            ):
                with self.assertRaisesRegex(RuntimeError, "newer macOS version than 14.0"):
                    verify_app_tools.verify_mach_o_minimum_versions(app_path)

    def test_rejects_missing_app_minimum(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app_path = Path(temp_dir) / "Test.app"
            info_path = app_path / "Contents" / "Info.plist"
            info_path.parent.mkdir(parents=True)
            with info_path.open("wb") as handle:
                plistlib.dump({}, handle)

            with self.assertRaisesRegex(RuntimeError, "must define LSMinimumSystemVersion"):
                verify_app_tools.verify_mach_o_minimum_versions(app_path)

    def test_accepts_mach_o_at_or_below_app_minimum(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app_path = Path(temp_dir) / "Test.app"
            info_path = app_path / "Contents" / "Info.plist"
            binary_path = app_path / "Contents" / "Resources" / "app_packages" / "example.so"
            binary_path.parent.mkdir(parents=True)
            binary_path.write_bytes(b"binary")
            with info_path.open("wb") as handle:
                plistlib.dump({"LSMinimumSystemVersion": "14.0"}, handle)

            with (
                patch.object(verify_app_tools, "is_mach_o", return_value=True),
                patch.object(verify_app_tools, "minimum_macos_versions", return_value={"13.0", "14.0"}),
            ):
                verify_app_tools.verify_mach_o_minimum_versions(app_path)


if __name__ == "__main__":
    unittest.main()
