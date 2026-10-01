import os
import plistlib
import re
import shutil
import subprocess
import tempfile
import unittest

from pathlib import Path
from unittest.mock import call, patch

from bd_to_avp.modules.config import config
from scripts import smoke_release_app, verify_app_tools
from scripts.native_app import MV_HEVC_ENCODER_NAME, NATIVE_MINIMUM_SYSTEM_VERSION, normalized_version
from scripts.vendor_ffmpeg_macos import load_manifest

REPOSITORY_BIN = Path(__file__).resolve().parents[1] / "bd_to_avp" / "bin"


def packaged_app_with_bundled_tools(root: Path, minimum_system_version: str) -> Path:
    app_path = root / "Test.app"
    bin_path = app_path / "Contents" / "Resources" / "app" / "bd_to_avp" / "bin"
    shutil.copytree(REPOSITORY_BIN, bin_path)
    with (app_path / "Contents" / "Info.plist").open("wb") as handle:
        plistlib.dump({"LSMinimumSystemVersion": minimum_system_version}, handle)
    return app_path


class VerifyAppToolsTests(unittest.TestCase):
    def test_tool_lists_agree_with_shipped_tools_and_runtime_lookups(self) -> None:
        committed_tools = {
            path.name for path in REPOSITORY_BIN.iterdir() if path.is_file() and os.access(path, os.X_OK)
        }
        vendored_tools = {asset.name for asset in load_manifest().assets}
        shipped_tools = committed_tools | vendored_tools | {MV_HEVC_ENCODER_NAME}
        runtime_bundled_tools = {
            value.name
            for value in vars(type(config)).values()
            if isinstance(value, Path) and value.parent == config.SCRIPT_PATH_BIN
        }
        smoke_tools = smoke_release_app.REQUIRED_BUNDLED_TOOLS | smoke_release_app.OPTIONAL_BUNDLED_TOOLS

        self.assertEqual(set(verify_app_tools.REQUIRED_TOOLS), shipped_tools)
        self.assertLessEqual(runtime_bundled_tools, shipped_tools)
        self.assertLessEqual(set(smoke_tools), set(verify_app_tools.REQUIRED_TOOLS))
        for tool_name, probe_args in smoke_tools.items():
            with self.subTest(tool=tool_name):
                self.assertEqual(probe_args, verify_app_tools.REQUIRED_TOOLS[tool_name])

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

    def test_rejects_missing_app_minimum(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app_path = Path(temp_dir) / "Test.app"
            info_path = app_path / "Contents" / "Info.plist"
            info_path.parent.mkdir(parents=True)
            with info_path.open("wb") as handle:
                plistlib.dump({}, handle)

            with self.assertRaisesRegex(RuntimeError, "must define LSMinimumSystemVersion"):
                verify_app_tools.verify_mach_o_minimum_versions(app_path)

    def test_bundled_mach_o_tools_fit_the_app_minimum(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app_path = packaged_app_with_bundled_tools(Path(temp_dir), NATIVE_MINIMUM_SYSTEM_VERSION)
            bundled = [path for path in sorted(REPOSITORY_BIN.iterdir()) if verify_app_tools.is_mach_o(path)]
            self.assertTrue(bundled, "expected Mach-O tools in bd_to_avp/bin")

            for path in bundled:
                with self.subTest(tool=path.name):
                    versions = verify_app_tools.minimum_macos_versions(path)
                    self.assertTrue(versions)
                    self.assertLessEqual(
                        max(normalized_version(version) for version in versions),
                        normalized_version(NATIVE_MINIMUM_SYSTEM_VERSION),
                    )

            verify_app_tools.verify_mach_o_minimum_versions(app_path)

    def test_rejects_bundled_mach_o_newer_than_app_minimum(self) -> None:
        newest_tool_minimum = max(
            normalized_version(version)
            for path in REPOSITORY_BIN.iterdir()
            if verify_app_tools.is_mach_o(path)
            for version in verify_app_tools.minimum_macos_versions(path)
        )
        older_app_minimum = f"{newest_tool_minimum[0] - 1}.0"

        with tempfile.TemporaryDirectory() as temp_dir:
            app_path = packaged_app_with_bundled_tools(Path(temp_dir), older_app_minimum)

            with self.assertRaisesRegex(RuntimeError, f"newer macOS version than {re.escape(older_app_minimum)}"):
                verify_app_tools.verify_mach_o_minimum_versions(app_path)


if __name__ == "__main__":
    unittest.main()
