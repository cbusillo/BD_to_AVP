import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from bd_to_avp import install


class BrewCommandTests(unittest.TestCase):
    def test_cask_install_command_does_not_use_no_quarantine(self) -> None:
        command = install.build_brew_command(["makemkv"], cask=True)

        self.assertEqual(command, ["/opt/homebrew/bin/brew", "install", "--force", "--cask", "makemkv"])
        self.assertNotIn("--no-quarantine", command)

    def test_cask_reinstall_command_does_not_use_no_quarantine(self) -> None:
        command = install.build_brew_command(["wine-stable"], cask=True, operation="reinstall")

        self.assertEqual(command, ["/opt/homebrew/bin/brew", "reinstall", "--force", "--cask", "wine-stable"])
        self.assertNotIn("--no-quarantine", command)

    def test_formula_install_command_is_not_a_cask_command(self) -> None:
        command = install.build_brew_command(["ffmpeg", "gpac"])

        self.assertEqual(command, ["/opt/homebrew/bin/brew", "install", "--force", "ffmpeg", "gpac"])
        self.assertNotIn("--cask", command)


class BrewPackageManagementTests(unittest.TestCase):
    def test_failed_required_cask_install_raises_before_quarantine_cleanup(self) -> None:
        failed_process = subprocess.CompletedProcess(
            ["/opt/homebrew/bin/brew", "install", "--force", "--cask", "makemkv"],
            1,
            stdout="",
            stderr="invalid option",
        )

        with (
            patch("bd_to_avp.install.subprocess.run", return_value=failed_process),
            patch("bd_to_avp.install.clear_cask_quarantine") as clear_cask_quarantine,
            patch("builtins.print"),
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                install.manage_brew_package("makemkv", {}, cask=True)

        clear_cask_quarantine.assert_not_called()

    def test_successful_cask_install_clears_quarantine(self) -> None:
        succeeded_process = subprocess.CompletedProcess(
            ["/opt/homebrew/bin/brew", "install", "--force", "--cask", "makemkv"],
            0,
            stdout="installed",
            stderr="",
        )

        with (
            patch("bd_to_avp.install.subprocess.run", return_value=succeeded_process),
            patch("bd_to_avp.install.clear_cask_quarantine") as clear_cask_quarantine,
            patch("builtins.print"),
        ):
            install.manage_brew_package("makemkv", {"SUDO_ASKPASS": "/tmp/askpass"}, cask=True)

        clear_cask_quarantine.assert_called_once_with(["makemkv"], {"SUDO_ASKPASS": "/tmp/askpass"})


class CaskDetectionTests(unittest.TestCase):
    def test_installed_cask_without_expected_app_bundle_needs_repair(self) -> None:
        brew_process = subprocess.CompletedProcess(
            ["/opt/homebrew/bin/brew", "list", "--cask", "--formula", "makemkv"],
            0,
            stdout="makemkv\n",
            stderr="",
        )

        with (
            patch("bd_to_avp.install.subprocess.run", return_value=brew_process),
            patch("bd_to_avp.install.get_cask_app_paths", return_value=[Path("/missing/MakeMKV.app")]),
        ):
            self.assertFalse(install.check_is_package_installed("makemkv"))

    def test_installed_cask_with_unquarantined_app_bundle_is_ready(self) -> None:
        brew_process = subprocess.CompletedProcess(
            ["/opt/homebrew/bin/brew", "list", "--cask", "--formula", "makemkv"],
            0,
            stdout="makemkv\n",
            stderr="",
        )
        app_path = Mock(spec=Path)
        app_path.exists.return_value = True

        with (
            patch("bd_to_avp.install.subprocess.run", return_value=brew_process),
            patch("bd_to_avp.install.get_cask_app_paths", return_value=[app_path]),
            patch("bd_to_avp.install.is_file_quarantined", return_value=False),
        ):
            self.assertTrue(install.check_is_package_installed("makemkv"))

    def test_installed_cask_with_any_quarantined_app_bundle_needs_repair(self) -> None:
        brew_process = subprocess.CompletedProcess(
            ["/opt/homebrew/bin/brew", "list", "--cask", "--formula", "wine-stable"],
            0,
            stdout="wine-stable\n",
            stderr="",
        )
        app_paths = [Mock(spec=Path), Mock(spec=Path)]
        for app_path in app_paths:
            app_path.exists.return_value = True

        with (
            patch("bd_to_avp.install.subprocess.run", return_value=brew_process),
            patch("bd_to_avp.install.get_cask_app_paths", return_value=app_paths),
            patch("bd_to_avp.install.is_file_quarantined", side_effect=[False, True]),
        ):
            self.assertFalse(install.check_is_package_installed("wine-stable"))


class DependencyVerificationTests(unittest.TestCase):
    def test_missing_required_dependency_binaries_raise_clear_error(self) -> None:
        fake_homebrew_bin = Path("/missing")

        with (
            patch.object(install.config, "HOMEBREW_PREFIX_BIN", fake_homebrew_bin),
            patch.object(install.config, "MAKEMKVCON_PATH", Path("/missing/makemkvcon")),
            patch.object(install.config, "MP4BOX_PATH", Path("/missing/MP4Box")),
            patch.object(install.config, "WINE_PATH", Path("/missing/wine")),
            self.assertRaisesRegex(ValueError, "Required command-line tools are missing"),
        ):
            install.verify_dependency_binaries()

    def test_native_mvc_helper_avoids_wine_cask_requirement(self) -> None:
        with tempfile.NamedTemporaryFile() as helper_file:
            helper_path = Path(helper_file.name)
            helper_path.chmod(0o755)

            with (
                patch.object(install.config, "EDGE264_TEST_PATH", helper_path),
                patch.object(install.config, "source_path", Path("/movie/source.mkv")),
                patch.object(install.config, "MTS_EXTENSIONS", [".mts", ".m2ts"]),
            ):
                self.assertFalse(install.needs_legacy_frim_stack())
                self.assertEqual(install.get_required_casks(), ["makemkv"])

    def test_mts_sources_do_not_require_legacy_wine_stack_when_native_helper_is_ready(self) -> None:
        with tempfile.NamedTemporaryFile() as helper_file:
            helper_path = Path(helper_file.name)
            helper_path.chmod(0o755)

            with (
                patch.object(install.config, "EDGE264_TEST_PATH", helper_path),
                patch.object(install.config, "source_path", Path("/movie/source.m2ts")),
                patch.object(install.config, "MTS_EXTENSIONS", [".mts", ".m2ts"]),
            ):
                self.assertFalse(install.needs_legacy_frim_stack())
                self.assertEqual(install.get_required_casks(), ["makemkv"])

    def test_missing_native_mvc_helper_requires_legacy_wine_stack(self) -> None:
        with patch.object(install.config, "EDGE264_TEST_PATH", Path("/missing/edge264_test")):
            self.assertTrue(install.needs_legacy_frim_stack())
            self.assertEqual(install.get_required_casks(), ["makemkv", "wine-stable"])

    def test_native_mvc_helper_repairs_missing_execute_bit(self) -> None:
        with tempfile.NamedTemporaryFile() as helper_file:
            helper_path = Path(helper_file.name)
            helper_path.chmod(0o644)

            with patch.object(install.config, "EDGE264_TEST_PATH", helper_path):
                self.assertTrue(install.ensure_native_mvc_splitter_executable())

            self.assertTrue(helper_path.stat().st_mode & 0o111)


if __name__ == "__main__":
    unittest.main()
