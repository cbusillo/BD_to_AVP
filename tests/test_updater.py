import os
import tempfile
import unittest

from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, QSettings, Signal
from PySide6.QtWidgets import QApplication

from bd_to_avp.gui.dialog import AboutDialog
from bd_to_avp.gui.main_window import MainWindow
from bd_to_avp.gui import updater


class FakeProcessingController(QObject):
    processing_became_idle = Signal()

    def __init__(self, is_active: bool) -> None:
        super().__init__()
        self.is_active = is_active


class FakeUserDefaults:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.synchronize_calls = 0

    def objectForKey_(self, key: str) -> str | None:
        return self.values.get(key)

    def setObject_forKey_(self, value: str, key: str) -> None:
        self.values[key] = value

    def synchronize(self) -> None:
        self.synchronize_calls += 1


class UpdaterDecisionTests(unittest.TestCase):
    def test_update_mode_matrix(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            framework_path = Path(temp_dir) / "Sparkle.framework"
            framework_path.mkdir()
            complete = updater.UpdaterEnvironment(
                is_gui=True,
                distribution_channel="direct",
                framework_path=framework_path,
                feed_url="https://example.invalid/appcast.xml",
                public_key="public-key",
            )

            self.assertEqual(updater.resolve_update_mode(complete), updater.UpdateMode.SPARKLE)
            self.assertEqual(
                updater.resolve_update_mode(updater.UpdaterEnvironment(False, "direct", framework_path, "feed", "key")),
                updater.UpdateMode.DISABLED,
            )
            self.assertEqual(
                updater.resolve_update_mode(updater.UpdaterEnvironment(True, "app-store", None, None, None)),
                updater.UpdateMode.APP_STORE,
            )
            self.assertEqual(
                updater.resolve_update_mode(updater.UpdaterEnvironment(True, "direct", None, "feed", "key")),
                updater.UpdateMode.MANUAL,
            )
            self.assertEqual(
                updater.resolve_update_mode(updater.UpdaterEnvironment(True, None, None, None, None)),
                updater.UpdateMode.MANUAL,
            )

    def test_allowed_channels_keep_stable_as_default(self) -> None:
        self.assertEqual(updater.allowed_sparkle_channels(updater.UpdateChannel.STABLE), frozenset())
        self.assertEqual(
            updater.allowed_sparkle_channels(updater.UpdateChannel.RELEASE_CANDIDATES),
            frozenset({"rc"}),
        )

    def test_preferences_default_to_stable_and_persist_rc(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = QSettings((Path(temp_dir) / "updates.ini").as_posix(), QSettings.Format.IniFormat)
            preferences = updater.UpdatePreferences(settings)

            self.assertEqual(preferences.channel, updater.UpdateChannel.STABLE)
            preferences.channel = updater.UpdateChannel.RELEASE_CANDIDATES

            reloaded = updater.UpdatePreferences(
                QSettings((Path(temp_dir) / "updates.ini").as_posix(), QSettings.Format.IniFormat)
            )
            self.assertEqual(reloaded.channel, updater.UpdateChannel.RELEASE_CANDIDATES)

    def test_invalid_persisted_channel_falls_back_to_stable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = QSettings((Path(temp_dir) / "updates.ini").as_posix(), QSettings.Format.IniFormat)
            settings.setValue(updater.CHANNEL_SETTINGS_KEY, "nightly")

            self.assertEqual(updater.UpdatePreferences(settings).channel, updater.UpdateChannel.STABLE)

    def test_preferences_support_app_user_defaults(self) -> None:
        settings = FakeUserDefaults()
        preferences = updater.UpdatePreferences(settings)

        self.assertEqual(preferences.channel, updater.UpdateChannel.STABLE)
        preferences.channel = updater.UpdateChannel.RELEASE_CANDIDATES

        self.assertEqual(preferences.channel, updater.UpdateChannel.RELEASE_CANDIDATES)
        self.assertEqual(settings.synchronize_calls, 1)


class InstallationPostponementTests(unittest.TestCase):
    def test_idle_processing_does_not_postpone(self) -> None:
        processing = FakeProcessingController(False)
        install = Mock()

        postponed = updater.InstallationPostponement(processing).postpone_if_processing(install)

        self.assertFalse(postponed)
        install.assert_not_called()

    def test_active_processing_resumes_install_once_when_idle(self) -> None:
        processing = FakeProcessingController(True)
        install = Mock()
        postponement = updater.InstallationPostponement(processing)

        self.assertTrue(postponement.postpone_if_processing(install))
        processing.is_active = False
        processing.processing_became_idle.emit()
        processing.processing_became_idle.emit()

        install.assert_called_once_with()


class UpdaterManagerTests(unittest.TestCase):
    def test_manual_mode_opens_release_page_without_github_api(self) -> None:
        processing = FakeProcessingController(False)
        environment = updater.UpdaterEnvironment(True, None, None, None, None)
        manager = updater.UpdaterManager(processing, environment=environment)

        with patch.object(updater.QDesktopServices, "openUrl", return_value=True) as open_url:
            self.assertTrue(manager.check_for_updates())

        open_url.assert_called_once()
        self.assertEqual(open_url.call_args.args[0].toString(), updater.RELEASES_URL)

    def test_sparkle_initialization_failure_falls_back_to_manual(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            framework_path = Path(temp_dir) / "Sparkle.framework"
            framework_path.mkdir()
            environment = updater.UpdaterEnvironment(True, "direct", framework_path, "feed", "key")
            processing = FakeProcessingController(False)

            with patch.object(updater.UpdaterManager, "_initialize_sparkle", side_effect=RuntimeError("failed")):
                manager = updater.UpdaterManager(processing, environment=environment)

        self.assertEqual(manager.mode, updater.UpdateMode.MANUAL)
        self.assertIsInstance(manager.initialization_error, RuntimeError)

    def test_channel_change_resets_sparkle_update_cycle(self) -> None:
        processing = FakeProcessingController(False)
        environment = updater.UpdaterEnvironment(True, None, None, None, None)
        settings = FakeUserDefaults()
        manager = updater.UpdaterManager(
            processing,
            environment=environment,
            preferences=updater.UpdatePreferences(settings),
        )
        sparkle_updater = Mock()
        manager._sparkle_controller = Mock()
        manager._sparkle_controller.updater.return_value = sparkle_updater

        manager.set_channel(updater.UpdateChannel.RELEASE_CANDIDATES)

        sparkle_updater.resetUpdateCycleAfterShortDelay.assert_called_once_with()


class UpdaterGuiTests(unittest.TestCase):
    app: QApplication

    @classmethod
    def setUpClass(cls) -> None:
        app = QApplication.instance()
        cls.app = app if isinstance(app, QApplication) else QApplication([])
        cls.app.setApplicationName("bd-to-avp-tests")
        cls.app.setApplicationDisplayName("BD_to_AVP Test")

    def test_help_menu_exposes_check_and_channel_actions(self) -> None:
        window = MainWindow()
        fake_manager = Mock()
        fake_manager.supports_channels = True
        fake_manager.channel = updater.UpdateChannel.STABLE
        window.updater_manager = fake_manager

        window.create_menu_bar()

        self.assertEqual(window.update_action.text(), "Check for Updates…")
        channel_actions = {action.text(): action for action in window.update_channel_actions.actions()}
        self.assertEqual(set(channel_actions), {"Stable", "Release Candidates"})
        self.assertTrue(channel_actions["Stable"].isChecked())
        channel_actions["Release Candidates"].trigger()
        fake_manager.set_channel.assert_called_once_with(updater.UpdateChannel.RELEASE_CANDIDATES)
        window.close()

    def test_about_dialog_has_no_legacy_network_checker(self) -> None:
        dialog = AboutDialog()

        self.assertFalse(hasattr(dialog, "prerelease_checkbox"))
        self.assertFalse(hasattr(dialog, "update_label"))
        self.assertFalse(hasattr(dialog, "fetch_latest_release"))
        dialog.close()


class LegacyDependencyTests(unittest.TestCase):
    def test_runtime_sources_no_longer_import_github_or_packaging(self) -> None:
        source_root = Path(__file__).resolve().parents[1] / "bd_to_avp"
        source = "\n".join(path.read_text(encoding="utf-8") for path in source_root.rglob("*.py"))

        self.assertNotIn("from github", source)
        self.assertNotIn("import github", source)
        self.assertNotIn("from packaging", source)


if __name__ == "__main__":
    unittest.main()
