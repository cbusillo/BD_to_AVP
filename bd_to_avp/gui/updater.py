from __future__ import annotations

import importlib
import logging
import types

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QObject, QSettings, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QMessageBox, QWidget


RELEASES_URL = "https://github.com/cbusillo/BD_to_AVP/releases"
CHANNEL_SETTINGS_KEY = "BDToAVPUpdateChannel"
logger = logging.getLogger(__name__)


class UpdateChannel(StrEnum):
    STABLE = "stable"
    RELEASE_CANDIDATES = "rc"


class UpdateMode(StrEnum):
    SPARKLE = "sparkle"
    MANUAL = "manual"
    APP_STORE = "app-store"
    DISABLED = "disabled"


@dataclass(frozen=True)
class UpdaterEnvironment:
    is_gui: bool
    distribution_channel: str | None
    framework_path: Path | None
    feed_url: str | None
    public_key: str | None


def resolve_update_mode(environment: UpdaterEnvironment) -> UpdateMode:
    if not environment.is_gui:
        return UpdateMode.DISABLED
    if environment.distribution_channel == "app-store":
        return UpdateMode.APP_STORE
    if (
        environment.distribution_channel == "direct"
        and environment.framework_path is not None
        and environment.framework_path.is_dir()
        and environment.feed_url
        and environment.public_key
    ):
        return UpdateMode.SPARKLE
    return UpdateMode.MANUAL


def allowed_sparkle_channels(channel: UpdateChannel) -> frozenset[str]:
    if channel is UpdateChannel.RELEASE_CANDIDATES:
        return frozenset({"rc"})
    return frozenset()


class UpdatePreferences:
    def __init__(self, settings: Any | None = None) -> None:
        if settings is not None:
            self._settings = settings
            return
        try:
            foundation = importlib.import_module("Foundation")
            self._settings = foundation.NSUserDefaults.standardUserDefaults()
        except ImportError:
            self._settings = QSettings()

    def _value(self) -> object:
        if hasattr(self._settings, "objectForKey_"):
            return self._settings.objectForKey_(CHANNEL_SETTINGS_KEY)
        return self._settings.value(CHANNEL_SETTINGS_KEY, UpdateChannel.STABLE.value)

    def _set_value(self, value: str) -> None:
        if hasattr(self._settings, "setObject_forKey_"):
            self._settings.setObject_forKey_(value, CHANNEL_SETTINGS_KEY)
            self._settings.synchronize()
            return
        self._settings.setValue(CHANNEL_SETTINGS_KEY, value)
        self._settings.sync()

    @property
    def channel(self) -> UpdateChannel:
        stored_value = self._value()
        value = str(stored_value) if stored_value is not None else UpdateChannel.STABLE.value
        try:
            return UpdateChannel(value)
        except ValueError:
            return UpdateChannel.STABLE

    @channel.setter
    def channel(self, channel: UpdateChannel) -> None:
        self._set_value(channel.value)


class InstallationPostponement:
    def __init__(self, processing_controller: Any) -> None:
        self._processing_controller = processing_controller
        self._install_handler: Callable[[], None] | None = None
        self._connected = False

    def postpone_if_processing(self, install_handler: Callable[[], None]) -> bool:
        if not self._processing_controller.is_active:
            return False
        self._install_handler = install_handler
        if not self._connected:
            self._processing_controller.processing_became_idle.connect(self._resume_installation)
            self._connected = True
        return True

    def _resume_installation(self) -> None:
        install_handler = self._install_handler
        if install_handler is None:
            return
        self._install_handler = None
        if self._connected:
            self._processing_controller.processing_became_idle.disconnect(self._resume_installation)
            self._connected = False
        install_handler()


def read_updater_environment(*, is_gui: bool = True) -> UpdaterEnvironment:
    try:
        foundation = importlib.import_module("Foundation")
    except ImportError:
        return UpdaterEnvironment(is_gui, None, None, None, None)

    bundle = foundation.NSBundle.mainBundle()

    def info_value(key: str) -> str | None:
        value = bundle.objectForInfoDictionaryKey_(key)
        return str(value).strip() if value is not None else None

    frameworks_path = bundle.privateFrameworksPath()
    framework_path = Path(str(frameworks_path)) / "Sparkle.framework" if frameworks_path else None
    return UpdaterEnvironment(
        is_gui=is_gui,
        distribution_channel=info_value("BDToAVPDistributionChannel"),
        framework_path=framework_path,
        feed_url=info_value("SUFeedURL"),
        public_key=info_value("SUPublicEDKey"),
    )


_sparkle_delegate_class: Any = None


def _make_sparkle_delegate_class(objc_module: Any, foundation: Any) -> Any:
    global _sparkle_delegate_class
    if _sparkle_delegate_class is not None:
        return _sparkle_delegate_class

    updater_protocol = objc_module.protocolNamed("SPUUpdaterDelegate")

    def configure(self: Any, preferences: UpdatePreferences, postponement: InstallationPostponement) -> None:
        self._preferences = preferences
        self._postponement = postponement

    def allowed_channels(self: Any, _updater: Any) -> Any:
        channels = sorted(allowed_sparkle_channels(self._preferences.channel))
        return foundation.NSSet.setWithArray_(channels)

    def postpone_relaunch(
        self: Any,
        _updater: Any,
        _item: Any,
        install_handler: Callable[[], None],
    ) -> bool:
        return self._postponement.postpone_if_processing(install_handler)

    def populate(namespace: dict[str, Any]) -> None:
        namespace["configure"] = objc_module.python_method(configure)
        namespace["allowedChannelsForUpdater_"] = allowed_channels
        namespace["updater_shouldPostponeRelaunchForUpdate_untilInvokingBlock_"] = postpone_relaunch

    _sparkle_delegate_class = types.new_class(
        "BDToAVPSparkleDelegate",
        (foundation.NSObject,),
        {"protocols": [updater_protocol]},
        populate,
    )
    return _sparkle_delegate_class


class UpdaterManager(QObject):
    channel_changed = Signal(object)

    def __init__(
        self,
        processing_controller: Any,
        parent: QObject | None = None,
        *,
        environment: UpdaterEnvironment | None = None,
        preferences: UpdatePreferences | None = None,
    ) -> None:
        super().__init__(parent)
        self.preferences = preferences or UpdatePreferences()
        self.environment = environment or read_updater_environment()
        self.mode = resolve_update_mode(self.environment)
        self.initialization_error: Exception | None = None
        self._sparkle_controller: Any = None
        self._sparkle_delegate: Any = None
        self._postponement = InstallationPostponement(processing_controller)
        if self.mode is UpdateMode.SPARKLE:
            try:
                self._initialize_sparkle()
            except Exception as error:
                self.initialization_error = error
                logger.error("Sparkle initialization failed; falling back to manual updates: %s", error)
                self.mode = UpdateMode.MANUAL

    @property
    def channel(self) -> UpdateChannel:
        return self.preferences.channel

    @property
    def supports_channels(self) -> bool:
        return self.mode is UpdateMode.SPARKLE

    def set_channel(self, channel: UpdateChannel) -> None:
        if channel is self.preferences.channel:
            return
        self.preferences.channel = channel
        if self._sparkle_controller is not None:
            self._sparkle_controller.updater().resetUpdateCycleAfterShortDelay()
        self.channel_changed.emit(channel)

    def _initialize_sparkle(self) -> None:
        framework_path = self.environment.framework_path
        if framework_path is None:
            raise RuntimeError("Sparkle framework path is unavailable.")
        objc_module = importlib.import_module("objc")
        foundation = importlib.import_module("Foundation")
        objc_module.loadBundle("Sparkle", {}, bundle_path=framework_path.as_posix())
        delegate_class = _make_sparkle_delegate_class(objc_module, foundation)
        delegate = delegate_class.alloc().init()
        delegate.configure(self.preferences, self._postponement)
        controller_class = objc_module.lookUpClass("SPUStandardUpdaterController")
        controller = controller_class.alloc().initWithStartingUpdater_updaterDelegate_userDriverDelegate_(
            True,
            delegate,
            None,
        )
        if controller is None:
            raise RuntimeError("Sparkle updater controller initialization failed.")
        self._sparkle_delegate = delegate
        self._sparkle_controller = controller

    def check_for_updates(self, parent: QWidget | None = None) -> bool:
        if self.mode is UpdateMode.SPARKLE and self._sparkle_controller is not None:
            self._sparkle_controller.checkForUpdates_(None)
            return True
        if self.mode is UpdateMode.APP_STORE:
            QMessageBox.information(parent, "Updates", "Updates for this build are managed by the App Store.")
            return True
        if self.mode is UpdateMode.MANUAL:
            return QDesktopServices.openUrl(QUrl(RELEASES_URL))
        return False
