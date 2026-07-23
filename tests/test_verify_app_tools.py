import plistlib
import subprocess
import tempfile
import unittest

from pathlib import Path
from unittest.mock import call, patch

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
                "mv-hevc-encoder",
                "MP4Box",
                "spatial-media-kit-tool",
            },
        )

    def test_release_profile_adds_gui_runtime_dependencies(self) -> None:
        self.assertLess(set(verify_app_tools.CORE_TOOLS), set(verify_app_tools.REQUIRED_TOOLS))
        self.assertIn("edge264_test", verify_app_tools.REQUIRED_TOOLS)
        self.assertNotIn("edge264_test", verify_app_tools.CORE_TOOLS)
        self.assertEqual(verify_app_tools.REQUIRED_TOOLS["mv-hevc-encoder"], ["--capability-probe"])

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

    def test_mv_hevc_encoder_probe_requires_the_supported_capability_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tool_path = Path(temp_dir) / "mv-hevc-encoder"
            tool_path.write_text("tool")
            tool_path.chmod(0o755)
            malformed_probe = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout='{"schema_version":1}\n',
            )
            with (
                patch.object(verify_app_tools, "run", return_value=malformed_probe) as run_mock,
                self.assertRaisesRegex(RuntimeError, "capability probe failed"),
            ):
                verify_app_tools.verify_tool(tool_path, ["--capability-probe"])

        run_mock.assert_called_once_with([tool_path, "--capability-probe"], check=False)

    def test_mv_hevc_encoder_probe_accepts_valid_unsupported_hardware_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tool_path = Path(temp_dir) / "mv-hevc-encoder"
            tool_path.write_text("tool")
            tool_path.chmod(0o755)
            unsupported_probe = subprocess.CompletedProcess(
                args=[],
                returncode=2,
                stdout='{"schema_version":1,"stereo_mv_hevc_encode_supported":false}\n',
            )
            linked_libraries = subprocess.CompletedProcess(args=[], returncode=0, stdout="")
            with patch.object(
                verify_app_tools,
                "run",
                side_effect=[unsupported_probe, linked_libraries],
            ) as run_mock:
                verify_app_tools.verify_tool(tool_path, ["--capability-probe"])

        self.assertEqual(
            run_mock.call_args_list,
            [
                call([tool_path, "--capability-probe"], check=False),
                call(["otool", "-L", tool_path]),
            ],
        )

    def test_verify_ffmpeg_requires_libsvtav1(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tool_path = Path(temp_dir) / "ffmpeg"
            tool_path.write_text("tool")
            tool_path.chmod(0o755)

            def fake_run(command: list[str | Path]):
                if command[:2] == ["otool", "-L"]:
                    return subprocess.CompletedProcess(args=command, returncode=0, stdout="")
                if "-encoders" in command:
                    return subprocess.CompletedProcess(args=command, returncode=0, stdout=" V..... libaom-av1\n")
                return subprocess.CompletedProcess(args=command, returncode=0, stdout="")

            with (
                patch.object(verify_app_tools, "run", side_effect=fake_run),
                self.assertRaisesRegex(RuntimeError, "libsvtav1"),
            ):
                verify_app_tools.verify_tool(tool_path, ["-version"])

    def test_verify_ffmpeg_requires_av1_metadata_filter(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tool_path = Path(temp_dir) / "ffmpeg"
            tool_path.write_text("tool")
            tool_path.chmod(0o755)

            def fake_run(command: list[str | Path]):
                if command[:2] == ["otool", "-L"]:
                    return subprocess.CompletedProcess(args=command, returncode=0, stdout="")
                if "-encoders" in command:
                    return subprocess.CompletedProcess(args=command, returncode=0, stdout=" V..... libsvtav1\n")
                if "-bsfs" in command:
                    return subprocess.CompletedProcess(args=command, returncode=0, stdout="extract_extradata\n")
                return subprocess.CompletedProcess(args=command, returncode=0, stdout="")

            with (
                patch.object(verify_app_tools, "run", side_effect=fake_run),
                self.assertRaisesRegex(RuntimeError, "av1_metadata"),
            ):
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
