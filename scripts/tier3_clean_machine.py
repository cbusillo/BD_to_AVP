from __future__ import annotations

import argparse
import enum
import hashlib
import json
import os
import platform
import plistlib
import pwd
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import uuid

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import quote
from xml.etree import ElementTree

from scripts.artifact_identity import app_tree_sha256
from scripts.qualify_release_scope import DEFAULT_POLICY_PATH, load_policy
from scripts.release_receipt import ReleaseReceiptError, validate_receipt as validate_release_receipt
from scripts.sparkle_appcast import AppcastError, SPARKLE, validate_appcast_channel
from scripts.tier3_receipt import (
    Tier3ReceiptError,
    build_receipt as build_tier3_receipt,
    validate_receipt as validate_tier3_receipt,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
CLEAN_MACHINE_CASE_ID = "clean-machine-signed-update"
INSTALLED_UI_CASE_ID = "installed-ui-accessibility"
APP_NAME = "3D Blu-ray to Vision Pro.app"
BUNDLE_IDENTIFIER = "com.shinycomputers.bd-to-avp"
RESTORABLE_LOCATION_ENVIRONMENT_CLASS = "restorable-location"
RESETTABLE_VM_ENVIRONMENT_CLASS = "resettable-vm"
SYSTEM_APPLICATIONS_DIRECTORY = Path("/Applications")
PREFERENCES_DOMAIN = BUNDLE_IDENTIFIER
UPDATE_ROUTE_KEY = "BDToAVPUpdateChannel"
SENTINEL_KEY = "BDToAVPTier3Sentinel"
AUTOMATIC_CHECKS_KEY = "SUEnableAutomaticChecks"
SENTINEL_VALUE = "tier3-preserve"
LIVE_FEED_URL = "https://cbusillo.github.io/BD_to_AVP/appcast.xml"
RELEASES_URL = "https://github.com/cbusillo/BD_to_AVP/releases"
PROFILE_DOCUMENT = {"version": 5, "profiles": []}
PROFILE_RELATIVE_PATH = Path("Library/Application Support/3D Blu-ray to Vision Pro/profiles.json")
MAX_FEED_BYTES = 5 * 1024 * 1024
BASE_FREE_SPACE_BYTES = 2 * 1024 * 1024 * 1024
NETWORK_TIMEOUT_SECONDS = 30
COMMAND_TIMEOUT_SECONDS = 60
SMOKE_TIMEOUT_SECONDS = 15 * 60
GUI_TIMEOUT_SECONDS = 20 * 60
UPDATE_TIMEOUT_SECONDS = 5 * 60
UI_TEST_TIMEOUT_SECONDS = 15 * 60
ACCESSIBILITY_COLLECTOR_FINISH_TIMEOUT_SECONDS = 15
MAX_UI_SCREENSHOT_BYTES = 20 * 1024 * 1024
SPARKLE_INSTALL_IDENTIFIER = "SPUUserUpdateChoiceInstall"
SPARKLE_STATUS_INSTALL_IDENTIFIER = "SUStatusInstallAndRelaunch"
SPARKLE_INSTALL_ACTIONS = (
    SPARKLE_INSTALL_IDENTIFIER,
    "Install Update",
    "Install and Relaunch",
    "Install on Quit",
)
MAX_UPDATE_ACTIONS = 2
MAX_UPDATE_TRANSITIONS = 32
UPDATE_POLL_SECONDS = 0.5
RETRYABLE_UPDATE_FAILURES = frozenset(
    {
        "application-process-timeout",
        "update-menu-timeout",
        "updater-state-changed",
        "update-window-timeout",
    }
)
ROUTE_CHANNELS: dict[str, set[str | None]] = {
    "stable": {None},
    "rc": {None, "rc"},
    "beta": {None, "rc", "beta"},
    "alpha": {None, "rc", "beta", "alpha"},
}
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
APPLE_BUILD_PATTERN = re.compile(r"^[0-9]{2}[A-Z][0-9A-Za-z]{1,12}$")
SYSTEM_TOOL_PATHS = {
    "defaults": Path("/usr/bin/defaults"),
    "ditto": Path("/usr/bin/ditto"),
    "hdiutil": Path("/usr/bin/hdiutil"),
    "log": Path("/usr/bin/log"),
    "open": Path("/usr/bin/open"),
    "osascript": Path("/usr/bin/osascript"),
    "xcrun": Path("/usr/bin/xcrun"),
    "xcodebuild": Path("/usr/bin/xcodebuild"),
}


class CleanMachineError(RuntimeError):
    pass


class SparkleUpdateFailure(CleanMachineError):
    def __init__(
        self,
        message: str,
        *,
        reason_code: str,
        state: SparkleUpdateState,
        action_pressed: bool,
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.state = state
        self.action_pressed = action_pressed

    @property
    def retryable(self) -> bool:
        return not self.action_pressed and self.reason_code in RETRYABLE_UPDATE_FAILURES


class SparkleUpdateState(str, enum.Enum):
    WAITING_FOR_WINDOW = "waiting-for-window"
    DOWNLOADING = "downloading"
    READY_INSTALL_UPDATE = "ready-install-update"
    READY_INSTALL_RELAUNCH = "ready-install-relaunch"
    READY_INSTALL_ON_QUIT = "ready-install-on-quit"
    INSTALLING = "installing"
    CANCELLED = "cancelled"
    TERMINAL_FAILURE = "terminal-failure"
    UNKNOWN = "unknown"


class SparkleUpdateOutcome(str, enum.Enum):
    INSTALL_AND_RELAUNCH = "install-and-relaunch"
    INSTALL_ON_QUIT = "install-on-quit"


@dataclass(frozen=True)
class BundleIdentity:
    bundle_identifier: str
    package_version: str
    build_version: str
    distribution_channel: str
    feed_url: str


@dataclass(frozen=True)
class ReleaseArtifact:
    receipt: Mapping[str, Any]
    receipt_path: Path
    receipt_reference: str
    receipt_file_sha256: str
    dmg_path: Path
    dmg_name: str
    dmg_sha256: str
    dmg_size: int
    source_sha: str
    release_tag: str
    package_version: str
    public_version: str
    build_version: str
    signed_app_tree_sha256: str


@dataclass(frozen=True)
class EnvironmentFacts:
    environment_class: str
    architecture: str
    macos_version: str
    macos_build: str
    free_bytes: int
    accessibility_enabled: bool
    homebrew_present: bool
    app_running: bool
    required_tools: tuple[str, ...]


@dataclass(frozen=True)
class RuntimeLayout:
    app_path: Path
    home: Path
    kind: str
    smoke_home: Path


@dataclass(frozen=True)
class SparkleDiagnosticFacts:
    cache_root_state: str
    classifications: tuple[str, ...]
    home_library_state: str
    log_event_count: int
    log_query_status: str


@dataclass(frozen=True)
class FeedCandidate:
    feed_sha256: str
    build_version: str
    short_version: str
    channel: str | None
    download_url: str
    length: int
    minimum_system_version: str
    release_notes_url: str


@dataclass(frozen=True)
class QualificationConfig:
    repo: Path
    candidate_receipt: Path
    candidate_dmg: Path
    prior_receipt: Path
    prior_dmg: Path
    qualification_root: Path
    route: str
    environment_class: str
    output_receipt: Path | None = None
    ui_output_receipt: Path | None = None
    evidence_directory: Path | None = None
    diagnostics_output: Path | None = None


@dataclass(frozen=True)
class UpdateObservation:
    state: SparkleUpdateState
    window_match: str
    action_identifier: str
    action_title: str


@dataclass(frozen=True)
class UpdateInteraction:
    clicked_button: str
    outcome: SparkleUpdateOutcome
    action_identifier: str
    window_match: str
    states_observed: tuple[str, ...]
    intent_checkpoint_sha256: str
    journal_sha256: str
    attempt: int
    retry_reason_code: str | None
    prior_process_id: int


class QualificationOperations(Protocol):
    def inspect_environment(self, qualification_root: Path, environment_class: str) -> EnvironmentFacts:
        raise NotImplementedError

    def fetch_live_feed(self) -> bytes:
        raise NotImplementedError

    def install_app(self, dmg_path: Path, destination: Path, mount_point: Path) -> None:
        raise NotImplementedError

    def smoke_app(self, app_path: Path, synthetic_home: Path, log_path: Path) -> str:
        raise NotImplementedError

    def write_preferences(self, synthetic_home: Path, route: str) -> None:
        raise NotImplementedError

    def read_preference(self, synthetic_home: Path, key: str) -> str:
        raise NotImplementedError

    def launch_app(self, app_path: Path, synthetic_home: Path) -> None:
        raise NotImplementedError

    def open_updater(self, app_path: Path, synthetic_home: Path) -> None:
        raise NotImplementedError

    def observe_updater(self) -> UpdateObservation:
        raise NotImplementedError

    def press_updater_action(self, observation: UpdateObservation) -> None:
        raise NotImplementedError

    def collect_ui_evidence(
        self,
        *,
        repo: Path,
        phase: str,
        app_path: Path,
        synthetic_home: Path,
        output_directory: Path,
        release_notes_url: str,
    ) -> None:
        raise NotImplementedError

    def app_running(self, app_path: Path | None = None) -> bool:
        raise NotImplementedError

    def app_process_id(self, app_path: Path | None = None) -> int | None:
        raise NotImplementedError

    def quit_app(self) -> None:
        raise NotImplementedError

    def clear_preferences(self, runtime_home: Path) -> None:
        raise NotImplementedError

    def collect_sparkle_diagnostics(self, runtime_home: Path) -> SparkleDiagnosticFacts:
        raise NotImplementedError


def _mapping(value: object, description: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CleanMachineError(f"{description} must be a JSON object.")
    return value


def _sequence(value: object, description: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise CleanMachineError(f"{description} must be a JSON array.")
    return value


def _string(value: object, description: str) -> str:
    if not isinstance(value, str) or not value:
        raise CleanMachineError(f"{description} must be a non-empty string.")
    return value


def _integer(value: object, description: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise CleanMachineError(f"{description} must be a positive integer.")
    return value


def _sha256(value: object, description: str) -> str:
    digest = _string(value, description)
    if SHA256_PATTERN.fullmatch(digest) is None:
        raise CleanMachineError(f"{description} must be a lowercase SHA-256 digest.")
    return digest


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


def _write_json(path: Path, value: Mapping[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(value, indent=2, sort_keys=True) + "\n"
    path.write_text(content, encoding="utf-8")
    return hashlib.sha256(content.encode()).hexdigest()


def _write_durable_json(path: Path, value: Mapping[str, Any]) -> str:
    content = _canonical_json_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    file_descriptor = os.open(temporary_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        destination = os.fdopen(file_descriptor, "wb")
        file_descriptor = -1
        with destination:
            destination.write(content)
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary_path, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)
        temporary_path.unlink(missing_ok=True)
    return hashlib.sha256(content).hexdigest()


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _uses_real_runner_home(home: Path) -> bool:
    return home.resolve() == _account_home()


def _account_home() -> Path:
    return Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()


def _runtime_layout(config: QualificationConfig) -> RuntimeLayout:
    qualification_root = config.qualification_root.resolve()
    smoke_home = qualification_root / "SmokeHome"
    if config.environment_class == RESTORABLE_LOCATION_ENVIRONMENT_CLASS:
        return RuntimeLayout(
            app_path=qualification_root / "Applications" / APP_NAME,
            home=qualification_root / "Home",
            kind="owned-root-synthetic-home",
            smoke_home=smoke_home,
        )
    if config.environment_class != RESETTABLE_VM_ENVIRONMENT_CLASS:
        raise CleanMachineError("This runner does not support the requested qualification environment class.")
    required_environment = {
        "BD_TO_AVP_TIER3_RUNNER_ENVIRONMENT": "github-hosted",
        "CI": "true",
        "GITHUB_ACTIONS": "true",
        "RUNNER_OS": "macOS",
    }
    if any(os.environ.get(key) != value for key, value in required_environment.items()):
        raise CleanMachineError("The resettable-vm lane requires an ephemeral GitHub-hosted macOS runner.")
    environment_home = os.environ.get("HOME")
    account_home = _account_home()
    if environment_home is None or Path(environment_home).resolve() != account_home:
        raise CleanMachineError("The resettable-vm lane requires the runner's unchanged real home directory.")
    runner_temp = os.environ.get("RUNNER_TEMP")
    if runner_temp is None:
        raise CleanMachineError("The resettable-vm lane requires the GitHub runner temporary directory.")
    try:
        qualification_root.relative_to(Path(runner_temp).resolve())
    except ValueError as error:
        raise CleanMachineError(
            "The resettable-vm qualification root must be inside the runner temporary directory."
        ) from error
    if not SYSTEM_APPLICATIONS_DIRECTORY.is_dir():
        raise CleanMachineError("The resettable-vm lane requires the normal system Applications directory.")
    return RuntimeLayout(
        app_path=SYSTEM_APPLICATIONS_DIRECTORY / APP_NAME,
        home=account_home,
        kind="system-applications-real-home",
        smoke_home=smoke_home,
    )


def _directory_state(path: Path) -> str:
    if not path.exists():
        return "missing"
    if not path.is_dir():
        return "not-directory"
    return "writable" if os.access(path, os.W_OK | os.X_OK) else "read-only"


def _managed_real_home_paths(home: Path) -> tuple[Path, ...]:
    return (
        home / "Library" / "Application Support" / "3D Blu-ray to Vision Pro",
        home / "Library" / "Preferences" / f"{BUNDLE_IDENTIFIER}.plist",
        home / "Library" / "Caches" / BUNDLE_IDENTIFIER,
        home / "Library" / "HTTPStorages" / BUNDLE_IDENTIFIER,
        home / "Library" / "Saved Application State" / f"{BUNDLE_IDENTIFIER}.savedState",
        home / "Library" / "WebKit" / BUNDLE_IDENTIFIER,
    )


def _remove_managed_real_home_state(home: Path) -> None:
    for path in _managed_real_home_paths(home):
        if path.is_symlink():
            raise CleanMachineError("Refusing to remove symlinked application state from the runner home.")
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()


def classify_sparkle_log_output(output: str) -> tuple[str, ...]:
    normalized = output.lower()
    classifications: set[str] = set()
    if (
        "failed to create installation cache directory" in normalized
        or "failed to create cache directory" in normalized
    ):
        classifications.add("installation-cache-create-failed")
    if "couldn't access its bundle info for app-bound domains" in normalized:
        classifications.add("app-bound-domain-provenance-failed")
    if "operation not permitted" in normalized or "permission denied" in normalized:
        classifications.add("permission-denied")
    if ("signature" in normalized or "code sign" in normalized) and any(
        marker in normalized for marker in ("failed", "invalid", "rejected")
    ):
        classifications.add("candidate-signature-validation-failed")
    if any(marker in normalized for marker in ("failed to extract", "extraction failed", "unarchive failed")):
        classifications.add("archive-extraction-failed")
    if any(marker in normalized for marker in ("failed to launch installer", "installer launch failed")):
        classifications.add("installer-launch-failed")
    if any(marker in normalized for marker in ("failed to replace", "could not replace", "replacement failed")):
        classifications.add("bundle-replacement-failed")
    if "install on quit" in normalized:
        classifications.add("install-on-quit-observed")
    if not output.strip():
        classifications.add("no-sparkle-log-events")
    elif not classifications:
        classifications.add("unclassified-sparkle-events")
    return tuple(sorted(classifications))


def _parse_update_observation(output: str) -> UpdateObservation:
    fields = output.rstrip("\n").split("\t")
    if len(fields) != 4:
        raise SparkleUpdateFailure(
            "Sparkle updater observation returned an invalid field count.",
            reason_code="updater-observation-invalid",
            state=SparkleUpdateState.UNKNOWN,
            action_pressed=False,
        )
    state_value, window_match, action_identifier, action_title = fields
    try:
        state = SparkleUpdateState(state_value)
    except ValueError as error:
        raise SparkleUpdateFailure(
            f"Sparkle updater observation returned an unknown state: {state_value!r}.",
            reason_code="updater-state-unknown",
            state=SparkleUpdateState.UNKNOWN,
            action_pressed=False,
        ) from error
    if window_match not in {"none", "identifier", "button-identifier", "owned-dialog"}:
        raise SparkleUpdateFailure(
            f"Sparkle updater observation returned an unsupported window match: {window_match!r}.",
            reason_code="updater-observation-invalid",
            state=SparkleUpdateState.UNKNOWN,
            action_pressed=False,
        )
    for value, description in (
        (action_identifier, "action identifier"),
        (action_title, "action title"),
    ):
        if len(value) > 200 or any(character in value for character in ("\r", "\n", "\t")):
            raise SparkleUpdateFailure(
                f"Sparkle updater observation returned an invalid {description}.",
                reason_code="updater-observation-invalid",
                state=SparkleUpdateState.UNKNOWN,
                action_pressed=False,
            )
    return UpdateObservation(
        state=state,
        window_match=window_match,
        action_identifier=action_identifier,
        action_title=action_title,
    )


def _sparkle_script_failure(error: CleanMachineError, *, action_pressed: bool) -> SparkleUpdateFailure:
    message = str(error)
    markers = {
        "Timed out waiting for the application process.": "application-process-timeout",
        "Timed out waiting for Check for Updates.": "update-menu-timeout",
        "Updater state changed before the guarded press.": "updater-state-changed",
    }
    reason_code = next((code for marker, code in markers.items() if marker in message), "updater-script-failure")
    state = SparkleUpdateState.UNKNOWN
    if reason_code in {"application-process-timeout", "update-menu-timeout"}:
        state = SparkleUpdateState.WAITING_FOR_WINDOW
    return SparkleUpdateFailure(
        message,
        reason_code=reason_code,
        state=state,
        action_pressed=action_pressed,
    )


def _relative_checked_path(repo: Path, path: Path, description: str) -> str:
    repo = repo.resolve()
    resolved = path.resolve()
    try:
        reference = resolved.relative_to(repo).as_posix()
    except ValueError as error:
        raise CleanMachineError(f"{description} must be inside the repository.") from error
    result = subprocess.run(
        ["git", "show", f"HEAD:{reference}"],
        cwd=repo,
        capture_output=True,
        timeout=COMMAND_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise CleanMachineError(f"{description} must be checked at repository HEAD: {reference}")
    local_bytes = resolved.read_bytes()
    if result.stdout != local_bytes:
        raise CleanMachineError(f"{description} differs from the checked repository HEAD: {reference}")
    return reference


def load_release_artifact(
    repo: Path,
    receipt_path: Path,
    dmg_path: Path,
    *,
    description: str,
) -> ReleaseArtifact:
    reference = _relative_checked_path(repo, receipt_path, f"{description} release receipt")
    try:
        receipt = _mapping(json.loads(receipt_path.read_text(encoding="utf-8")), f"{description} release receipt")
    except (OSError, json.JSONDecodeError) as error:
        raise CleanMachineError(f"Unable to load {description} release receipt: {error}") from error
    try:
        validate_release_receipt(receipt)
    except ReleaseReceiptError as error:
        raise CleanMachineError(f"{description.capitalize()} release receipt is invalid: {error}") from error

    dmg_artifacts = [
        _mapping(item, f"{description} release artifact")
        for item in _sequence(receipt.get("artifacts"), f"{description} release artifacts")
        if _mapping(item, f"{description} release artifact").get("kind") == "dmg"
    ]
    if len(dmg_artifacts) != 1:
        raise CleanMachineError(f"{description.capitalize()} release receipt must contain exactly one DMG.")
    dmg = dmg_artifacts[0]
    expected_name = _string(dmg.get("name"), f"{description} DMG name")
    expected_size = _integer(dmg.get("size_bytes"), f"{description} DMG size")
    expected_sha256 = _sha256(dmg.get("sha256"), f"{description} DMG sha256")
    if dmg_path.name != expected_name:
        raise CleanMachineError(f"{description.capitalize()} DMG name does not match its release receipt.")
    try:
        actual_size = dmg_path.stat().st_size
    except OSError as error:
        raise CleanMachineError(f"Unable to inspect {description} DMG: {error}") from error
    if actual_size != expected_size or file_sha256(dmg_path) != expected_sha256:
        raise CleanMachineError(f"{description.capitalize()} DMG size or digest does not match its release receipt.")

    release = _mapping(receipt.get("release"), f"{description} release identity")
    versions = _mapping(receipt.get("versions"), f"{description} release versions")
    source_sha = _string(receipt.get("source_sha"), f"{description} source SHA")
    if SHA_PATTERN.fullmatch(source_sha) is None:
        raise CleanMachineError(f"{description.capitalize()} source SHA is not canonical.")
    return ReleaseArtifact(
        receipt=receipt,
        receipt_path=receipt_path.resolve(),
        receipt_reference=reference,
        receipt_file_sha256=file_sha256(receipt_path),
        dmg_path=dmg_path.resolve(),
        dmg_name=expected_name,
        dmg_sha256=expected_sha256,
        dmg_size=expected_size,
        source_sha=source_sha,
        release_tag=_string(release.get("tag"), f"{description} release tag"),
        package_version=_string(versions.get("package"), f"{description} package version"),
        public_version=_string(versions.get("public"), f"{description} public version"),
        build_version=_string(versions.get("build"), f"{description} build version"),
        signed_app_tree_sha256=_sha256(
            receipt.get("signed_app_tree_sha256"),
            f"{description} signed app tree sha256",
        ),
    )


def validate_release_order(prior: ReleaseArtifact, candidate: ReleaseArtifact) -> None:
    try:
        prior_build = int(prior.build_version)
        candidate_build = int(candidate.build_version)
    except ValueError as error:
        raise CleanMachineError("Prior and candidate build versions must be numeric.") from error
    if candidate_build <= prior_build:
        raise CleanMachineError("Candidate build version must be newer than the prior installed release.")
    if prior.source_sha == candidate.source_sha or prior.dmg_sha256 == candidate.dmg_sha256:
        raise CleanMachineError("Prior and candidate releases must bind different immutable artifacts.")


def parse_feed_candidate(feed_bytes: bytes, candidate: ReleaseArtifact, route: str) -> FeedCandidate:
    if len(feed_bytes) > MAX_FEED_BYTES:
        raise CleanMachineError("Live appcast exceeds the bounded feed size.")
    try:
        root = ElementTree.fromstring(feed_bytes)
    except ElementTree.ParseError as error:
        raise CleanMachineError(f"Live appcast is invalid XML: {error}") from error
    if root.tag != "rss" or root.get("version") != "2.0":
        raise CleanMachineError("Live appcast root must be RSS 2.0.")
    channel = root.find("channel")
    if channel is None:
        raise CleanMachineError("Live appcast is missing its channel.")
    try:
        validate_appcast_channel(channel)
    except AppcastError as error:
        raise CleanMachineError(f"Live appcast validation failed: {error}") from error

    matches = [
        item
        for item in channel.findall("item")
        if (item.findtext(f"{SPARKLE}shortVersionString") or "").strip() == candidate.package_version
    ]
    if len(matches) != 1:
        detail = f"found {len(matches)}"
        raise CleanMachineError(
            f"Live appcast must contain exactly one candidate item for {candidate.package_version}; {detail}."
        )
    item = matches[0]
    build_version = (item.findtext(f"{SPARKLE}version") or "").strip()
    if build_version != candidate.build_version:
        raise CleanMachineError("Live appcast candidate build does not match the release receipt.")
    channel_name = (item.findtext(f"{SPARKLE}channel") or "").strip() or None
    if channel_name not in ROUTE_CHANNELS[route]:
        display_channel = channel_name or "stable"
        raise CleanMachineError(f"Candidate channel {display_channel} is not eligible for route {route}.")
    enclosure = item.find("enclosure")
    if enclosure is None:
        raise CleanMachineError("Live appcast candidate is missing its enclosure.")
    expected_url = (
        f"https://github.com/cbusillo/BD_to_AVP/releases/download/{candidate.release_tag}/{quote(candidate.dmg_name)}"
    )
    download_url = enclosure.get("url", "")
    if download_url != expected_url:
        raise CleanMachineError("Live appcast candidate enclosure is not bound to the exact release DMG.")
    if enclosure.get("length") != str(candidate.dmg_size):
        raise CleanMachineError("Live appcast candidate enclosure length does not match the exact release DMG.")
    minimum_system_version = (item.findtext(f"{SPARKLE}minimumSystemVersion") or "").strip()
    if not minimum_system_version:
        raise CleanMachineError("Live appcast candidate is missing minimum system version.")
    release_notes_url = (
        item.findtext(f"{SPARKLE}fullReleaseNotesLink") or item.findtext(f"{SPARKLE}releaseNotesLink") or ""
    ).strip()
    if not release_notes_url:
        raise CleanMachineError("Live appcast candidate is missing its release-notes URL.")
    return FeedCandidate(
        feed_sha256=hashlib.sha256(feed_bytes).hexdigest(),
        build_version=build_version,
        short_version=candidate.package_version,
        channel=channel_name,
        download_url=download_url,
        length=candidate.dmg_size,
        minimum_system_version=minimum_system_version,
        release_notes_url=release_notes_url,
    )


def _version_tuple(value: str, description: str) -> tuple[int, ...]:
    try:
        parts = tuple(int(part) for part in value.split("."))
    except ValueError as error:
        raise CleanMachineError(f"{description} must contain numeric dot-separated components.") from error
    if not parts or any(part < 0 for part in parts):
        raise CleanMachineError(f"{description} is invalid.")
    return parts


def read_bundle_identity(app_path: Path) -> BundleIdentity:
    plist_path = app_path / "Contents" / "Info.plist"
    try:
        info = plistlib.loads(plist_path.read_bytes())
    except (OSError, plistlib.InvalidFileException) as error:
        raise CleanMachineError(f"Unable to read installed app identity: {error}") from error
    return BundleIdentity(
        bundle_identifier=_string(info.get("CFBundleIdentifier"), "CFBundleIdentifier"),
        package_version=_string(info.get("CFBundleShortVersionString"), "CFBundleShortVersionString"),
        build_version=_string(info.get("CFBundleVersion"), "CFBundleVersion"),
        distribution_channel=_string(info.get("BDToAVPDistributionChannel"), "BDToAVPDistributionChannel"),
        feed_url=_string(info.get("SUFeedURL"), "SUFeedURL"),
    )


def verify_installed_app(app_path: Path, release: ReleaseArtifact) -> BundleIdentity:
    identity = read_bundle_identity(app_path)
    expected = BundleIdentity(
        bundle_identifier=BUNDLE_IDENTIFIER,
        package_version=release.package_version,
        build_version=release.build_version,
        distribution_channel="direct",
        feed_url=LIVE_FEED_URL,
    )
    if identity != expected:
        raise CleanMachineError(
            f"Installed app identity mismatch: expected {expected.package_version}/{expected.build_version}, "
            f"found {identity.package_version}/{identity.build_version}."
        )
    if app_tree_sha256(app_path) != release.signed_app_tree_sha256:
        raise CleanMachineError("Installed app tree does not match the exact signed release receipt.")
    return identity


def validate_environment(
    facts: EnvironmentFacts,
    case: Mapping[str, Any],
    *,
    required_free_bytes: int,
) -> None:
    environment = _mapping(case.get("environment"), "Tier 3 case environment")
    if facts.environment_class not in set(cast(Sequence[str], environment.get("classes", []))):
        raise CleanMachineError("Qualification environment class is not allowed by policy.")
    if facts.environment_class not in {
        RESTORABLE_LOCATION_ENVIRONMENT_CLASS,
        RESETTABLE_VM_ENVIRONMENT_CLASS,
    }:
        raise CleanMachineError("This runner does not support the requested qualification environment class.")
    if facts.architecture not in set(cast(Sequence[str], environment.get("architectures", []))):
        raise CleanMachineError("Qualification architecture is not allowed by policy.")
    try:
        macos_major = int(facts.macos_version.split(".", maxsplit=1)[0])
    except ValueError as error:
        raise CleanMachineError("Qualification macOS version is not canonical.") from error
    required_versions = tuple(cast(Sequence[int], environment.get("macos_major_versions", [])))
    if macos_major not in set(required_versions):
        raise CleanMachineError(
            f"Qualification requires macOS major versions {required_versions}, found {facts.macos_version}."
        )
    if APPLE_BUILD_PATTERN.fullmatch(facts.macos_build) is None:
        raise CleanMachineError("Qualification macOS build is not a public Apple build identifier.")
    if facts.free_bytes < required_free_bytes:
        raise CleanMachineError(
            f"Qualification location requires at least {required_free_bytes} free bytes; found {facts.free_bytes}."
        )
    if not facts.accessibility_enabled:
        raise CleanMachineError("Accessibility control is required for bounded Sparkle UI interaction.")
    if facts.app_running:
        raise CleanMachineError("The production app must not be running before qualification starts.")
    missing_tools = [tool for tool in facts.required_tools if not SYSTEM_TOOL_PATHS[tool].is_file()]
    if missing_tools:
        raise CleanMachineError(f"Qualification host is missing required tools: {', '.join(missing_tools)}.")


class MacOSOperations:
    required_tools = ("defaults", "ditto", "hdiutil", "log", "open", "osascript", "xcrun", "xcodebuild")

    @staticmethod
    def _run(
        command: Sequence[str],
        *,
        timeout: int = COMMAND_TIMEOUT_SECONDS,
        env: Mapping[str, str] | None = None,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=dict(env) if env is not None else None,
            input=input_text,
        )
        if result.returncode != 0:
            details = []
            for label, output in (("stdout", result.stdout), ("stderr", result.stderr)):
                if output := output.strip():
                    if len(output) > 20_000:
                        output = f"[truncated to final 20000 characters]\n{output[-20_000:]}"
                    details.append(f"--- {label} ---\n{output}")
            detail = "\n".join(details) or "command failed"
            raise CleanMachineError(f"Command failed ({command[0]}): {detail}")
        return result

    def _start_accessibility_collector(
        self,
        *,
        repo: Path,
        phase: str,
        output_directory: Path,
        expected_url: str,
    ) -> subprocess.Popen[str]:
        collector_binary = output_directory.parent / "installed-ui-accessibility"
        self._run(
            [
                str(SYSTEM_TOOL_PATHS["xcrun"]),
                "swiftc",
                str(repo / "scripts" / "installed_ui_accessibility.swift"),
                "-o",
                str(collector_binary),
            ],
            timeout=SMOKE_TIMEOUT_SECONDS,
        )
        collector_output = output_directory / (
            "accessibility-tree.json" if phase == "candidate" else "updater-accessibility.json"
        )
        return subprocess.Popen(
            [
                str(collector_binary),
                "--mode",
                phase,
                "--bundle-identifier",
                BUNDLE_IDENTIFIER,
                "--expected-url",
                expected_url,
                "--output",
                str(collector_output),
                "--timeout-seconds",
                str(UI_TEST_TIMEOUT_SECONDS),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    @staticmethod
    def _stop_accessibility_collector(collector: subprocess.Popen[str]) -> tuple[str, str]:
        if collector.poll() is None:
            collector.terminate()
            try:
                return collector.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                collector.kill()
        return collector.communicate()

    @staticmethod
    def _finish_accessibility_collector(collector: subprocess.Popen[str]) -> None:
        try:
            stdout, stderr = collector.communicate(timeout=ACCESSIBILITY_COLLECTOR_FINISH_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as error:
            MacOSOperations._stop_accessibility_collector(collector)
            raise CleanMachineError("Installed UI accessibility collector did not finish after the UI test.") from error
        if collector.returncode != 0:
            detail = stderr.strip() or stdout.strip() or "collector failed"
            raise CleanMachineError(f"Installed UI accessibility collector failed: {detail}")

    def _extract_ui_attachments(
        self,
        *,
        result_bundle: Path,
        output_directory: Path,
        expected_names: Sequence[str],
    ) -> None:
        attachments_directory = result_bundle.parent / f"{result_bundle.stem}-attachments"
        self._run(
            [
                str(SYSTEM_TOOL_PATHS["xcrun"]),
                "xcresulttool",
                "export",
                "attachments",
                "--path",
                str(result_bundle),
                "--output-path",
                str(attachments_directory),
            ],
            timeout=SMOKE_TIMEOUT_SECONDS,
        )
        manifest = json.loads((attachments_directory / "manifest.json").read_text(encoding="utf-8"))
        if not isinstance(manifest, list):
            raise CleanMachineError("Installed UI attachment manifest must be a JSON array.")
        records = [
            attachment
            for test_record in manifest
            if isinstance(test_record, Mapping)
            for attachment in test_record.get("attachments", [])
            if isinstance(attachment, Mapping)
        ]
        for expected_name in expected_names:
            expected_path = Path(expected_name)
            generated_prefix = f"{expected_path.stem}_"
            matches = [
                record
                for record in records
                if record.get("suggestedHumanReadableName") == expected_name
                or (
                    isinstance(record.get("suggestedHumanReadableName"), str)
                    and cast(str, record["suggestedHumanReadableName"]).startswith(generated_prefix)
                    and cast(str, record["suggestedHumanReadableName"]).endswith(expected_path.suffix)
                )
            ]
            if len(matches) != 1:
                raise CleanMachineError(
                    f"Installed UI result must contain exactly one {expected_name} attachment; found {len(matches)}."
                )
            exported_name = matches[0].get("exportedFileName")
            if not isinstance(exported_name, str) or Path(exported_name).name != exported_name:
                raise CleanMachineError(f"Installed UI {expected_name} attachment has an invalid exported filename.")
            source = attachments_directory / exported_name
            if not source.is_file():
                raise CleanMachineError(f"Installed UI {expected_name} attachment export is missing.")
            shutil.copyfile(source, output_directory / expected_name)

    @staticmethod
    def _runtime_env(synthetic_home: Path) -> dict[str, str]:
        env = dict(os.environ)
        if not _uses_real_runner_home(synthetic_home):
            env["HOME"] = str(synthetic_home)
            env["CFFIXED_USER_HOME"] = str(synthetic_home)
        env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
        return env

    def inspect_environment(self, qualification_root: Path, environment_class: str) -> EnvironmentFacts:
        if platform.system() != "Darwin":
            raise CleanMachineError("Tier 3 clean-machine qualification requires macOS.")
        version = self._run(["sw_vers", "-productVersion"]).stdout.strip()
        build = self._run(["sw_vers", "-buildVersion"]).stdout.strip()
        accessibility = (
            self._run(["osascript", "-e", 'tell application "System Events" to return UI elements enabled'])
            .stdout.strip()
            .lower()
        )
        return EnvironmentFacts(
            environment_class=environment_class,
            architecture=platform.machine(),
            macos_version=version,
            macos_build=build,
            free_bytes=shutil.disk_usage(qualification_root.parent).free,
            accessibility_enabled=accessibility == "true",
            homebrew_present=Path("/opt/homebrew/bin/brew").exists() or Path("/usr/local/bin/brew").exists(),
            app_running=self.app_running(),
            required_tools=self.required_tools,
        )

    @staticmethod
    def fetch_live_feed() -> bytes:
        request = urllib.request.Request(LIVE_FEED_URL, headers={"User-Agent": "BD-to-AVP-Tier3/1"})
        try:
            with urllib.request.urlopen(request, timeout=NETWORK_TIMEOUT_SECONDS) as response:
                content = response.read(MAX_FEED_BYTES + 1)
        except OSError as error:
            raise CleanMachineError(f"Unable to download live appcast: {error}") from error
        if len(content) > MAX_FEED_BYTES:
            raise CleanMachineError("Live appcast exceeds the bounded feed size.")
        return content

    @staticmethod
    def _mount_dmg(dmg_path: Path, mount_point: Path) -> Path:
        mount_point.mkdir(parents=True)
        result = subprocess.run(
            [
                "hdiutil",
                "attach",
                "-nobrowse",
                "-readonly",
                "-mountpoint",
                str(mount_point),
                str(dmg_path),
            ],
            capture_output=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            shutil.rmtree(mount_point, ignore_errors=True)
            raise CleanMachineError(f"Unable to mount release DMG: {result.stderr.decode(errors='replace').strip()}")
        return mount_point

    @staticmethod
    def _detach_mounts(mount_points: Sequence[Path]) -> None:
        failures: list[str] = []
        for mount_point in reversed(mount_points):
            result = subprocess.run(
                ["hdiutil", "detach", str(mount_point)],
                capture_output=True,
                text=True,
                timeout=COMMAND_TIMEOUT_SECONDS,
            )
            if result.returncode != 0:
                failures.append(result.stderr.strip() or str(mount_point))
            elif mount_point.exists():
                mount_point.rmdir()
        if failures:
            raise CleanMachineError(f"Unable to detach qualification mounts: {'; '.join(failures)}")

    def install_app(self, dmg_path: Path, destination: Path, mount_point: Path) -> None:
        if destination.exists():
            raise CleanMachineError("Qualification app destination already exists before DMG installation.")
        if mount_point.exists():
            raise CleanMachineError("Qualification mount point already exists before DMG installation.")
        self._mount_dmg(dmg_path, mount_point)
        try:
            candidates = [
                path
                for path in mount_point.glob("*.app")
                if (path / "Contents" / "Info.plist").exists()
                and read_bundle_identity(path).bundle_identifier == BUNDLE_IDENTIFIER
            ]
            if len(candidates) != 1:
                raise CleanMachineError("Mounted release DMG must contain exactly one production app bundle.")
            if destination.exists():
                raise CleanMachineError("Qualification app destination appeared before DMG installation.")
            destination.parent.mkdir(parents=True, exist_ok=True)
            self._run(["ditto", str(candidates[0]), str(destination)], timeout=SMOKE_TIMEOUT_SECONDS)
        finally:
            self._detach_mounts([mount_point])

    def smoke_app(self, app_path: Path, synthetic_home: Path, log_path: Path) -> str:
        env = self._runtime_env(synthetic_home)
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "smoke_release_app.py"), "--app-path", str(app_path)],
            capture_output=True,
            timeout=SMOKE_TIMEOUT_SECONDS,
            env=env,
        )
        log_path.parent.mkdir(parents=True, exist_ok=True)
        raw_log = result.stdout + b"\n--- stderr ---\n" + result.stderr
        log_path.write_bytes(raw_log)
        if result.returncode != 0:
            raise CleanMachineError("Maintained installed-app package smoke failed.")
        return hashlib.sha256(raw_log).hexdigest()

    def write_preferences(self, synthetic_home: Path, route: str) -> None:
        synthetic_home.mkdir(parents=True, exist_ok=True)
        env = self._runtime_env(synthetic_home)
        self._run(["defaults", "write", PREFERENCES_DOMAIN, UPDATE_ROUTE_KEY, route], env=env)
        self._run(["defaults", "write", PREFERENCES_DOMAIN, SENTINEL_KEY, SENTINEL_VALUE], env=env)
        self._run(
            ["defaults", "write", PREFERENCES_DOMAIN, AUTOMATIC_CHECKS_KEY, "-bool", "false"],
            env=env,
        )

    def read_preference(self, synthetic_home: Path, key: str) -> str:
        result = self._run(
            ["defaults", "read", PREFERENCES_DOMAIN, key],
            env=self._runtime_env(synthetic_home),
        )
        return result.stdout.strip()

    def clear_preferences(self, runtime_home: Path) -> None:
        subprocess.run(
            [str(SYSTEM_TOOL_PATHS["defaults"]), "delete", PREFERENCES_DOMAIN],
            capture_output=True,
            env=self._runtime_env(runtime_home),
            timeout=COMMAND_TIMEOUT_SECONDS,
        )

    def launch_app(self, app_path: Path, synthetic_home: Path) -> None:
        env = self._runtime_env(synthetic_home)
        command = ["open", "-n", "-F"]
        if not _uses_real_runner_home(synthetic_home):
            command.extend(
                [
                    "--env",
                    f"HOME={synthetic_home}",
                    "--env",
                    f"CFFIXED_USER_HOME={synthetic_home}",
                ]
            )
        command.append(str(app_path))
        self._run(command, env=env)

    def open_updater(self, app_path: Path, synthetic_home: Path) -> None:
        self.launch_app(app_path, synthetic_home)
        try:
            self._run(
                ["osascript", "-", BUNDLE_IDENTIFIER],
                timeout=COMMAND_TIMEOUT_SECONDS * 3,
                input_text=self._updater_open_script(),
            )
        except CleanMachineError as error:
            raise _sparkle_script_failure(error, action_pressed=False) from error

    def observe_updater(self) -> UpdateObservation:
        try:
            result = self._run(
                ["osascript", "-", BUNDLE_IDENTIFIER],
                timeout=COMMAND_TIMEOUT_SECONDS,
                input_text=self._updater_observe_script(),
            )
        except CleanMachineError as error:
            raise _sparkle_script_failure(error, action_pressed=False) from error
        return _parse_update_observation(result.stdout)

    def press_updater_action(self, observation: UpdateObservation) -> None:
        try:
            self._run(
                [
                    "osascript",
                    "-",
                    BUNDLE_IDENTIFIER,
                    observation.window_match,
                    observation.action_identifier,
                    observation.action_title,
                ],
                timeout=COMMAND_TIMEOUT_SECONDS,
                input_text=self._updater_press_script(),
            )
        except CleanMachineError as error:
            raise _sparkle_script_failure(error, action_pressed=False) from error

    def collect_ui_evidence(
        self,
        *,
        repo: Path,
        phase: str,
        app_path: Path,
        synthetic_home: Path,
        output_directory: Path,
        release_notes_url: str,
    ) -> None:
        test_names = {
            "updater": "testPriorUpdaterControlsAndReleaseLinks",
            "candidate": "testCandidateMainWindowProfileAndSettings",
        }
        try:
            test_name = test_names[phase]
        except KeyError as error:
            raise CleanMachineError(f"Unsupported installed UI phase: {phase}") from error
        output_directory.mkdir(parents=True, exist_ok=True)
        derived_data = output_directory.parent / f"InstalledUIDerivedData-{phase}"
        result_bundle = output_directory.parent / f"InstalledUI-{phase}.xcresult"
        build_settings = {
            "BD_TO_AVP_UI_APP_PATH": str(app_path),
            "BD_TO_AVP_UI_BUNDLE_IDENTIFIER": BUNDLE_IDENTIFIER,
            "BD_TO_AVP_UI_HOME": str(synthetic_home),
            "BD_TO_AVP_UI_OUTPUT_DIRECTORY": str(output_directory),
            "BD_TO_AVP_UI_PHASE": phase,
            "BD_TO_AVP_UI_RELEASE_NOTES_URL": release_notes_url,
            "BD_TO_AVP_UI_RELEASES_URL": RELEASES_URL,
        }
        collector = self._start_accessibility_collector(
            repo=repo,
            phase=phase,
            output_directory=output_directory,
            expected_url=RELEASES_URL if phase == "candidate" else release_notes_url,
        )
        try:
            self._run(
                [
                    str(SYSTEM_TOOL_PATHS["xcodebuild"]),
                    "-project",
                    str(repo / "macos" / "BluRayToVisionPro.xcodeproj"),
                    "-scheme",
                    "BluRayToVisionProInstalledUI",
                    "-derivedDataPath",
                    str(derived_data),
                    "-resultBundlePath",
                    str(result_bundle),
                    "-destination",
                    "platform=macOS,arch=arm64",
                    f"-only-testing:BluRayToVisionProUITests/InstalledUIAcceptanceTests/{test_name}",
                    *(f"{key}={value}" for key, value in build_settings.items()),
                    "test",
                ],
                timeout=UI_TEST_TIMEOUT_SECONDS,
            )
        except BaseException as error:
            stdout, stderr = self._stop_accessibility_collector(collector)
            detail = stderr.strip() or stdout.strip()
            if detail:
                raise CleanMachineError(f"{error}\nInstalled UI accessibility collector: {detail}") from error
            raise
        self._finish_accessibility_collector(collector)
        expected_attachments = (
            ("candidate-ui.json", "screenshot-light.png", "screenshot-dark.png")
            if phase == "candidate"
            else ("updater-ui.json",)
        )
        self._extract_ui_attachments(
            result_bundle=result_bundle,
            output_directory=output_directory,
            expected_names=expected_attachments,
        )
        expected_evidence = "candidate-ui.json" if phase == "candidate" else "updater-ui.json"
        if not (output_directory / expected_evidence).is_file():
            raise CleanMachineError(
                f"Installed UI {phase} test completed without required evidence: {expected_evidence}."
            )
        if phase == "candidate" and not (output_directory / "accessibility-tree.json").is_file():
            raise CleanMachineError("Installed UI candidate test completed without accessibility-tree.json.")
        if phase == "updater":
            updater_accessibility = _load_ui_json(
                output_directory / "updater-accessibility.json",
                {"release_notes_url", "release_notes_url_observed", "schema_version"},
            )
            if updater_accessibility != {
                "release_notes_url": release_notes_url,
                "release_notes_url_observed": True,
                "schema_version": 1,
            }:
                raise CleanMachineError("Installed UI updater accessibility evidence is not source-bound.")
            updater_path = output_directory / "updater-ui.json"
            updater = dict(
                _load_ui_json(
                    updater_path,
                    {"install_action", "release_notes_url", "release_notes_url_observed", "schema_version", "status"},
                )
            )
            if updater.get("release_notes_url_observed") is not False:
                raise CleanMachineError("Installed UI updater test claimed accessibility evidence before collection.")
            updater["release_notes_url_observed"] = True
            updater_path.write_bytes(_canonical_json_bytes(updater))
        if phase == "candidate":
            accessibility = _load_ui_json(
                output_directory / "accessibility-tree.json",
                {"elements", "schema_version"},
            )
            elements = accessibility["elements"]
            if not isinstance(elements, list):
                raise CleanMachineError("Installed UI candidate accessibility elements must be a JSON array.")
            release_records = [
                element
                for element in elements
                if isinstance(element, Mapping) and element.get("identifier") == "all-releases-link"
            ]
            if len(release_records) != 1 or release_records[0].get("url") != RELEASES_URL:
                raise CleanMachineError("Installed UI candidate release-link evidence is not source-bound.")

    @staticmethod
    def _application_process_ids() -> tuple[int, ...]:
        script = (
            'tell application "System Events" to return unix id of every '
            f'application process whose bundle identifier is "{BUNDLE_IDENTIFIER}"'
        )
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            return ()
        output = result.stdout.strip()
        if not output:
            return ()
        try:
            return tuple(int(value.strip()) for value in output.split(",") if value.strip())
        except ValueError:
            return ()

    @classmethod
    def app_process_id(cls, app_path: Path | None = None) -> int | None:
        process_ids = cls._application_process_ids()
        if app_path is None:
            return process_ids[0] if len(process_ids) == 1 else None
        if len(process_ids) != 1:
            return None
        try:
            info = _mapping(
                plistlib.loads((app_path / "Contents" / "Info.plist").read_bytes()),
                "installed app Info.plist",
            )
            executable_name = _string(info.get("CFBundleExecutable"), "installed app executable name")
        except (OSError, plistlib.InvalidFileException, CleanMachineError):
            return None
        executable_path = (app_path / "Contents" / "MacOS" / executable_name).resolve()
        process = subprocess.run(
            ["/bin/ps", "-p", str(process_ids[0]), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
        command = process.stdout.strip()
        if process.returncode != 0 or not (
            command == str(executable_path) or command.startswith(f"{executable_path} ")
        ):
            return None
        return process_ids[0]

    @classmethod
    def app_running(cls, app_path: Path | None = None) -> bool:
        if app_path is None:
            return bool(cls._application_process_ids())
        return cls.app_process_id(app_path) is not None

    def quit_app(self) -> None:
        script = f'''tell application "System Events"
    set matches to every application process whose bundle identifier is "{BUNDLE_IDENTIFIER}"
    repeat with targetProcess in matches
        try
            tell targetProcess to set frontmost to true
            keystroke "q" using command down
        end try
    end repeat
end tell'''
        subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
        deadline = time.monotonic() + 20
        while self.app_running() and time.monotonic() < deadline:
            time.sleep(0.5)
        if self.app_running():
            force_script = f'''tell application "System Events"
    set matches to every application process whose bundle identifier is "{BUNDLE_IDENTIFIER}"
    repeat with targetProcess in matches
        try
            set unixID to unix id of targetProcess
            do shell script "kill -TERM " & unixID
        end try
    end repeat
end tell'''
            subprocess.run(
                ["osascript", "-e", force_script],
                capture_output=True,
                timeout=COMMAND_TIMEOUT_SECONDS,
            )
        deadline = time.monotonic() + 10
        while self.app_running() and time.monotonic() < deadline:
            time.sleep(0.5)
        if self.app_running():
            raise CleanMachineError("Production app did not terminate during cleanup.")

    def collect_sparkle_diagnostics(self, runtime_home: Path) -> SparkleDiagnosticFacts:
        predicate = (
            'subsystem == "org.sparkle-project.Sparkle" OR '
            'process == "InstallerLauncher" OR process == "Autoupdate" OR process == "Updater"'
        )
        result = subprocess.run(
            [
                str(SYSTEM_TOOL_PATHS["log"]),
                "show",
                "--style",
                "compact",
                "--last",
                "15m",
                "--predicate",
                predicate,
            ],
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
        if result.returncode == 0:
            output = result.stdout[-200_000:]
            classifications = classify_sparkle_log_output(output)
            log_query_status = "passed"
            log_event_count = min(sum(1 for line in output.splitlines() if line.strip()), 10_000)
        else:
            classifications = ("log-query-failed",)
            log_query_status = "failed"
            log_event_count = 0
        return SparkleDiagnosticFacts(
            cache_root_state=_directory_state(runtime_home / "Library" / "Caches"),
            classifications=classifications,
            home_library_state=_directory_state(runtime_home / "Library"),
            log_event_count=log_event_count,
            log_query_status=log_query_status,
        )

    @staticmethod
    def _updater_open_script() -> str:
        return """on run argv
    set targetBundleID to item 1 of argv
    set processDeadline to (current date) + 60
    tell application "System Events"
        repeat
            set processMatches to every application process whose bundle identifier is targetBundleID
            if (count of processMatches) is 1 then exit repeat
            if (count of processMatches) > 1 then
                error "Multiple application processes matched the qualification bundle identifier."
            end if
            if (current date) > processDeadline then error "Timed out waiting for the application process."
            delay 0.5
        end repeat
        set targetProcess to item 1 of processMatches
        tell targetProcess
            set frontmost to true
            set menuDeadline to (current date) + 60
            repeat until exists menu item "Check for Updates…" of menu 1 of menu bar item "Help" of menu bar 1
                if (current date) > menuDeadline then error "Timed out waiting for Check for Updates."
                delay 0.5
            end repeat
            click menu item "Check for Updates…" of menu 1 of menu bar item "Help" of menu bar 1
        end tell
    end tell
end run"""

    @staticmethod
    def _updater_observe_script() -> str:
        return """on run argv
    set targetBundleID to item 1 of argv
    tell application "System Events"
        set processMatches to every application process whose bundle identifier is targetBundleID
        if (count of processMatches) is 0 then
            return "waiting-for-window" & tab & "none" & tab & "" & tab & ""
        end if
        if (count of processMatches) > 1 then
            return "unknown" & tab & "none" & tab & "" & tab & ""
        end if
        set targetProcess to item 1 of processMatches
        tell targetProcess
            set identifiedWindows to {}
            repeat with candidateWindow in windows
                try
                    if (value of attribute "AXIdentifier" of candidateWindow) is "SUUpdateAlert" then
                        set end of identifiedWindows to candidateWindow
                    end if
                end try
            end repeat
            if (count of identifiedWindows) > 1 then
                return "unknown" & tab & "identifier" & tab & "" & tab & ""
            end if

            set targetWindow to missing value
            set windowMatch to "none"
            if (count of identifiedWindows) is 1 then
                set targetWindow to item 1 of identifiedWindows
                set windowMatch to "identifier"
            else
                set fallbackWindows to {}
                repeat with candidateWindow in windows
                    repeat with candidateButton in buttons of candidateWindow
                        try
                            set candidateIdentifier to value of attribute "AXIdentifier" of candidateButton
                            set isInstallChoice to candidateIdentifier is "SPUUserUpdateChoiceInstall"
                            set isFinalInstallChoice to candidateIdentifier is "SUStatusInstallAndRelaunch"
                            if isInstallChoice or isFinalInstallChoice then
                                set end of fallbackWindows to candidateWindow
                                exit repeat
                            end if
                        end try
                    end repeat
                end repeat
                if (count of fallbackWindows) > 1 then
                    return "unknown" & tab & "button-identifier" & tab & "" & tab & ""
                else if (count of fallbackWindows) is 1 then
                    set targetWindow to item 1 of fallbackWindows
                    set windowMatch to "button-identifier"
                end if
            end if

            if targetWindow is missing value then
                repeat with candidateWindow in windows
                    try
                        set windowSubrole to value of attribute "AXSubrole" of candidateWindow
                        if windowSubrole is "AXDialog" or windowSubrole is "AXSystemDialog" then
                            return "terminal-failure" & tab & "owned-dialog" & tab & "" & tab & ""
                        end if
                    end try
                end repeat
                return "waiting-for-window" & tab & "none" & tab & "" & tab & ""
            end if

            set selectedButton to missing value
            set selectedIdentifier to ""
            repeat with candidateButton in buttons of targetWindow
                try
                    set buttonIdentifier to value of attribute "AXIdentifier" of candidateButton
                    set isInstallChoice to buttonIdentifier is "SPUUserUpdateChoiceInstall"
                    set isFinalInstallChoice to buttonIdentifier is "SUStatusInstallAndRelaunch"
                    if isInstallChoice or isFinalInstallChoice then
                        set selectedButton to candidateButton
                        set selectedIdentifier to buttonIdentifier
                        exit repeat
                    end if
                end try
            end repeat
            if selectedButton is missing value then
                repeat with buttonTitle in {"Install and Relaunch", "Install Update", "Install on Quit"}
                    if exists button (buttonTitle as text) of targetWindow then
                        set selectedButton to button (buttonTitle as text) of targetWindow
                        exit repeat
                    end if
                end repeat
            end if

            if selectedButton is missing value then
                if exists progress indicator 1 of targetWindow then
                    return "downloading" & tab & windowMatch & tab & "" & tab & ""
                end if
                return "terminal-failure" & tab & windowMatch & tab & "" & tab & ""
            end if

            set selectedTitle to name of selectedButton as text
            if not (enabled of selectedButton) then
                return "downloading" & tab & windowMatch & tab & selectedIdentifier & tab & selectedTitle
            end if
            if selectedTitle is "Install Update" then
                return "ready-install-update" & tab & windowMatch & tab & selectedIdentifier & tab & selectedTitle
            else if selectedTitle is "Install on Quit" then
                return "ready-install-on-quit" & tab & windowMatch & tab & selectedIdentifier & tab & selectedTitle
            else if selectedTitle is "Install and Relaunch" then
                return "ready-install-relaunch" & tab & windowMatch & tab & selectedIdentifier & tab & selectedTitle
            end if
            return "unknown" & tab & windowMatch & tab & selectedIdentifier & tab & selectedTitle
        end tell
    end tell
end run"""

    @staticmethod
    def _updater_press_script() -> str:
        return """on run argv
    set targetBundleID to item 1 of argv
    set expectedWindowMatch to item 2 of argv
    set expectedIdentifier to item 3 of argv
    set expectedTitle to item 4 of argv
    tell application "System Events"
        set processMatches to every application process whose bundle identifier is targetBundleID
        if (count of processMatches) is not 1 then
            error "Updater state changed before the guarded press."
        end if
        set targetProcess to item 1 of processMatches
        tell targetProcess
            set targetWindow to missing value
            set actualWindowMatch to "none"
            repeat with candidateWindow in windows
                try
                    if (value of attribute "AXIdentifier" of candidateWindow) is "SUUpdateAlert" then
                        if targetWindow is not missing value then
                            error "Updater state changed before the guarded press."
                        end if
                        set targetWindow to candidateWindow
                        set actualWindowMatch to "identifier"
                    end if
                end try
            end repeat
            if targetWindow is missing value then
                repeat with candidateWindow in windows
                    repeat with candidateButton in buttons of candidateWindow
                        try
                            set candidateIdentifier to value of attribute "AXIdentifier" of candidateButton
                            set isInstallChoice to candidateIdentifier is "SPUUserUpdateChoiceInstall"
                            set isFinalInstallChoice to candidateIdentifier is "SUStatusInstallAndRelaunch"
                            if isInstallChoice or isFinalInstallChoice then
                                if targetWindow is not missing value then
                                    error "Updater state changed before the guarded press."
                                end if
                                set targetWindow to candidateWindow
                                set actualWindowMatch to "button-identifier"
                                exit repeat
                            end if
                        end try
                    end repeat
                end repeat
            end if
            if targetWindow is missing value or actualWindowMatch is not expectedWindowMatch then
                error "Updater state changed before the guarded press."
            end if

            set selectedButton to missing value
            if expectedIdentifier is not "" then
                repeat with candidateButton in buttons of targetWindow
                    try
                        if (value of attribute "AXIdentifier" of candidateButton) is expectedIdentifier then
                            if selectedButton is not missing value then
                                error "Updater state changed before the guarded press."
                            end if
                            set selectedButton to candidateButton
                        end if
                    end try
                end repeat
            else if exists button expectedTitle of targetWindow then
                set selectedButton to button expectedTitle of targetWindow
            end if
            if selectedButton is missing value then error "Updater state changed before the guarded press."
            if (name of selectedButton as text) is not expectedTitle then
                error "Updater state changed before the guarded press."
            end if
            if not (enabled of selectedButton) then error "Updater state changed before the guarded press."
            perform action "AXPress" of selectedButton
        end tell
    end tell
end run"""


def _case_policy(policy: Mapping[str, Any], case_id: str) -> Mapping[str, Any]:
    matches = [
        _mapping(item, "qualification case")
        for item in _sequence(policy.get("cases"), "qualification cases")
        if _mapping(item, "qualification case").get("id") == case_id
    ]
    if len(matches) != 1:
        raise CleanMachineError(f"Policy must define exactly one {case_id!r} case.")
    return matches[0]


def prepare_qualification(
    config: QualificationConfig,
    operations: QualificationOperations,
) -> tuple[
    Mapping[str, Any],
    Mapping[str, Any],
    Mapping[str, Any],
    ReleaseArtifact,
    ReleaseArtifact,
    EnvironmentFacts,
    FeedCandidate,
]:
    repo = config.repo.resolve()
    policy = load_policy(repo / DEFAULT_POLICY_PATH.relative_to(REPO_ROOT))
    clean_machine_case = _case_policy(policy, CLEAN_MACHINE_CASE_ID)
    installed_ui_case = _case_policy(policy, INSTALLED_UI_CASE_ID)
    candidate = load_release_artifact(
        repo,
        config.candidate_receipt,
        config.candidate_dmg,
        description="candidate",
    )
    prior = load_release_artifact(
        repo,
        config.prior_receipt,
        config.prior_dmg,
        description="prior",
    )
    validate_release_order(prior, candidate)
    if config.route not in ROUTE_CHANNELS:
        raise CleanMachineError(f"Unsupported update route: {config.route}")
    qualification_root = config.qualification_root.resolve()
    if qualification_root.exists():
        raise CleanMachineError("Qualification root must not exist before the runner starts.")
    if config.environment_class == RESTORABLE_LOCATION_ENVIRONMENT_CLASS and (
        qualification_root == Path.home().resolve() or Path.home().resolve() not in qualification_root.parents
    ):
        raise CleanMachineError("Qualification root must be a dedicated location inside the current home directory.")
    if config.output_receipt is not None and qualification_root in config.output_receipt.resolve().parents:
        raise CleanMachineError("Output receipt must be outside the disposable qualification root.")
    if config.ui_output_receipt is not None and qualification_root in config.ui_output_receipt.resolve().parents:
        raise CleanMachineError("Installed UI output receipt must be outside the disposable qualification root.")
    if (
        config.output_receipt is not None
        and config.ui_output_receipt is not None
        and config.output_receipt.resolve() == config.ui_output_receipt.resolve()
    ):
        raise CleanMachineError("Clean-machine and installed UI receipts must use different output paths.")
    if config.evidence_directory is not None and qualification_root in config.evidence_directory.resolve().parents:
        raise CleanMachineError("Evidence directory must be outside the disposable qualification root.")
    if config.diagnostics_output is not None and qualification_root in config.diagnostics_output.resolve().parents:
        raise CleanMachineError("Diagnostics output must be outside the disposable qualification root.")
    file_output_paths = [
        path.resolve()
        for path in (config.output_receipt, config.ui_output_receipt, config.diagnostics_output)
        if path is not None
    ]
    if len(file_output_paths) != len(set(file_output_paths)):
        raise CleanMachineError("Qualification receipt and diagnostics outputs must use distinct paths.")
    if config.evidence_directory is not None:
        evidence_directory = config.evidence_directory.resolve()
        if any(path == evidence_directory or evidence_directory in path.parents for path in file_output_paths):
            raise CleanMachineError("Qualification file outputs must be outside the evidence directory.")
    runtime_layout = _runtime_layout(config)
    if runtime_layout.app_path.exists():
        raise CleanMachineError("The qualification app destination must not exist before the runner starts.")
    if config.environment_class == RESETTABLE_VM_ENVIRONMENT_CLASS and any(
        path.exists() or path.is_symlink() for path in _managed_real_home_paths(runtime_layout.home)
    ):
        raise CleanMachineError("The resettable-vm runner has preexisting managed application state.")
    required_free_bytes = BASE_FREE_SPACE_BYTES + prior.dmg_size + (candidate.dmg_size * 2)
    environment = operations.inspect_environment(qualification_root, config.environment_class)
    validate_environment(environment, clean_machine_case, required_free_bytes=required_free_bytes)
    validate_environment(environment, installed_ui_case, required_free_bytes=required_free_bytes)
    feed = parse_feed_candidate(operations.fetch_live_feed(), candidate, config.route)
    if _version_tuple(environment.macos_version, "Environment macOS version") < _version_tuple(
        feed.minimum_system_version,
        "Appcast minimum system version",
    ):
        raise CleanMachineError("Qualification environment is older than the candidate appcast minimum system version.")
    return policy, clean_machine_case, installed_ui_case, prior, candidate, environment, feed


def preflight_report(
    config: QualificationConfig,
    operations: QualificationOperations,
) -> Mapping[str, Any]:
    _, _, _, prior, candidate, environment, feed = prepare_qualification(config, operations)
    runtime_layout = _runtime_layout(config)
    return {
        "candidate": {
            "build_version": candidate.build_version,
            "dmg_sha256": candidate.dmg_sha256,
            "package_version": candidate.package_version,
            "release_tag": candidate.release_tag,
            "source_sha": candidate.source_sha,
        },
        "developer_state": {
            "homebrew_present": environment.homebrew_present,
            "runtime_layout": runtime_layout.kind,
            "runtime_path": "sanitized-system-only",
        },
        "environment": {
            "architecture": environment.architecture,
            "environment_class": environment.environment_class,
            "free_bytes": environment.free_bytes,
            "macos_build": environment.macos_build,
            "macos_version": environment.macos_version,
        },
        "feed": {
            "candidate_channel": feed.channel,
            "candidate_download_url_sha256": hashlib.sha256(feed.download_url.encode()).hexdigest(),
            "feed_sha256": feed.feed_sha256,
            "route": config.route,
        },
        "prior": {
            "build_version": prior.build_version,
            "dmg_sha256": prior.dmg_sha256,
            "package_version": prior.package_version,
            "release_tag": prior.release_tag,
            "source_sha": prior.source_sha,
        },
        "preflight": "passed",
        "schema_version": 1,
    }


def _perform_sparkle_update(
    *,
    app_path: Path,
    synthetic_home: Path,
    prior: ReleaseArtifact,
    candidate: ReleaseArtifact,
    operations: QualificationOperations,
    checkpoint_path: Path,
    run_id: str,
    attempt: int,
    retry_reason_code: str | None,
) -> UpdateInteraction:
    prior_identity = verify_installed_app(app_path, prior)
    journal: dict[str, Any] = {
        "attempt": attempt,
        "candidate": {
            "build_version": candidate.build_version,
            "package_version": candidate.package_version,
            "release_tag": candidate.release_tag,
            "signed_app_tree_sha256": candidate.signed_app_tree_sha256,
        },
        "prior": {
            "build_version": prior_identity.build_version,
            "package_version": prior_identity.package_version,
            "release_tag": prior.release_tag,
            "signed_app_tree_sha256": prior.signed_app_tree_sha256,
        },
        "retry_reason_code": retry_reason_code,
        "run_id": run_id,
        "schema_version": 1,
        "transitions": [],
    }
    _write_durable_json(checkpoint_path, journal)
    operations.open_updater(app_path, synthetic_home)
    prior_process_id = operations.app_process_id(app_path)
    if prior_process_id is None:
        raise SparkleUpdateFailure(
            "Sparkle updater did not retain the exact prior application process after opening.",
            reason_code="application-process-timeout",
            state=SparkleUpdateState.WAITING_FOR_WINDOW,
            action_pressed=False,
        )

    deadline = time.monotonic() + GUI_TIMEOUT_SECONDS
    states_observed: list[str] = []
    last_recorded_state: SparkleUpdateState | None = None
    saw_owned_window = False
    install_update_pressed = False
    action_count = 0
    intent_checkpoint_sha256 = ""

    def record_transition(phase: str, observation: UpdateObservation) -> str:
        transitions = cast(list[dict[str, Any]], journal["transitions"])
        if len(transitions) >= MAX_UPDATE_TRANSITIONS:
            raise SparkleUpdateFailure(
                "Sparkle updater exceeded the bounded transition count.",
                reason_code="updater-transition-limit",
                state=observation.state,
                action_pressed=action_count > 0,
            )
        transitions.append(
            {
                "action_identifier": observation.action_identifier or None,
                "action_title": observation.action_title or None,
                "phase": phase,
                "sequence": len(transitions) + 1,
                "state": observation.state.value,
                "timestamp": _utc_timestamp(),
                "window_match": observation.window_match,
            }
        )
        return _write_durable_json(checkpoint_path, journal)

    while time.monotonic() < deadline:
        observation = operations.observe_updater()
        state = observation.state
        if state != last_recorded_state:
            states_observed.append(state.value)
            record_transition("observed", observation)
            last_recorded_state = state

        if observation.window_match in {"identifier", "button-identifier"}:
            saw_owned_window = True

        if state == SparkleUpdateState.WAITING_FOR_WINDOW:
            if install_update_pressed:
                if operations.app_process_id(app_path) is None:
                    raise SparkleUpdateFailure(
                        "The prior application terminated before Sparkle exposed the final install action.",
                        reason_code="application-terminated-before-install-ready",
                        state=state,
                        action_pressed=True,
                    )
                time.sleep(UPDATE_POLL_SECONDS)
                continue
            if saw_owned_window and action_count == 0:
                cancelled = UpdateObservation(
                    state=SparkleUpdateState.CANCELLED,
                    window_match="none",
                    action_identifier="",
                    action_title="",
                )
                states_observed.append(cancelled.state.value)
                record_transition("terminal", cancelled)
                raise SparkleUpdateFailure(
                    "Sparkle update window closed before an install action was selected.",
                    reason_code="updater-cancelled",
                    state=cancelled.state,
                    action_pressed=False,
                )
            time.sleep(UPDATE_POLL_SECONDS)
            continue

        if state in {SparkleUpdateState.DOWNLOADING, SparkleUpdateState.INSTALLING}:
            time.sleep(UPDATE_POLL_SECONDS)
            continue
        if state == SparkleUpdateState.READY_INSTALL_UPDATE and install_update_pressed:
            time.sleep(UPDATE_POLL_SECONDS)
            continue

        if state == SparkleUpdateState.TERMINAL_FAILURE:
            raise SparkleUpdateFailure(
                "Sparkle presented a terminal updater failure before the candidate was installed.",
                reason_code="updater-reported-failure",
                state=state,
                action_pressed=action_count > 0,
            )
        if state == SparkleUpdateState.CANCELLED:
            raise SparkleUpdateFailure(
                "Sparkle update was cancelled before the candidate was installed.",
                reason_code="updater-cancelled",
                state=state,
                action_pressed=action_count > 0,
            )
        if state == SparkleUpdateState.UNKNOWN:
            action_detail = (
                f" unsupported install action: {observation.action_title!r}." if observation.action_title else ""
            )
            raise SparkleUpdateFailure(
                f"Sparkle updater entered an unknown state; no action was taken.{action_detail}",
                reason_code="updater-state-unknown",
                state=state,
                action_pressed=action_count > 0,
            )

        outcomes: dict[SparkleUpdateState, SparkleUpdateOutcome | None] = {
            SparkleUpdateState.READY_INSTALL_UPDATE: None,
            SparkleUpdateState.READY_INSTALL_RELAUNCH: SparkleUpdateOutcome.INSTALL_AND_RELAUNCH,
            SparkleUpdateState.READY_INSTALL_ON_QUIT: SparkleUpdateOutcome.INSTALL_ON_QUIT,
        }
        try:
            outcome = outcomes[state]
        except KeyError as error:
            raise SparkleUpdateFailure(
                f"Sparkle updater state is not actionable: {state.value}.",
                reason_code="updater-state-unknown",
                state=SparkleUpdateState.UNKNOWN,
                action_pressed=action_count > 0,
            ) from error
        if observation.action_title not in {"Install and Relaunch", "Install Update", "Install on Quit"}:
            raise SparkleUpdateFailure(
                f"Sparkle updater exposed an unsupported action title: {observation.action_title!r}.",
                reason_code="updater-action-unknown",
                state=SparkleUpdateState.UNKNOWN,
                action_pressed=action_count > 0,
            )
        if observation.action_identifier not in {
            "",
            SPARKLE_INSTALL_IDENTIFIER,
            SPARKLE_STATUS_INSTALL_IDENTIFIER,
        }:
            raise SparkleUpdateFailure(
                f"Sparkle updater exposed an unsupported action identifier: {observation.action_identifier!r}.",
                reason_code="updater-action-unknown",
                state=SparkleUpdateState.UNKNOWN,
                action_pressed=action_count > 0,
            )
        if action_count >= MAX_UPDATE_ACTIONS:
            raise SparkleUpdateFailure(
                "Sparkle updater exceeded the bounded install-action count.",
                reason_code="updater-action-limit",
                state=state,
                action_pressed=True,
            )

        intent_checkpoint_sha256 = record_transition("intent", observation)
        try:
            operations.press_updater_action(observation)
        except SparkleUpdateFailure as error:
            if error.reason_code != "updater-state-changed":
                raise
            record_transition("intent-invalidated", observation)
            raise SparkleUpdateFailure(
                "Sparkle updater changed after intent was durably recorded; no action was pressed.",
                reason_code="updater-state-changed",
                state=observation.state,
                action_pressed=action_count > 0,
            ) from error
        pressed_journal_sha256 = record_transition("pressed", observation)
        action_count += 1

        if state == SparkleUpdateState.READY_INSTALL_UPDATE:
            install_update_pressed = True
            downloading = UpdateObservation(
                state=SparkleUpdateState.DOWNLOADING,
                window_match=observation.window_match,
                action_identifier=observation.action_identifier,
                action_title=observation.action_title,
            )
            states_observed.append(downloading.state.value)
            record_transition("derived", downloading)
            last_recorded_state = downloading.state
            time.sleep(UPDATE_POLL_SECONDS)
            continue

        if outcome is None:
            raise AssertionError("non-terminal Sparkle action reached terminal interaction")

        return UpdateInteraction(
            clicked_button=observation.action_title,
            outcome=outcome,
            action_identifier=observation.action_identifier,
            window_match=observation.window_match,
            states_observed=tuple(states_observed),
            intent_checkpoint_sha256=intent_checkpoint_sha256,
            journal_sha256=pressed_journal_sha256,
            attempt=attempt,
            retry_reason_code=retry_reason_code,
            prior_process_id=prior_process_id,
        )

    current_process_id = operations.app_process_id(app_path)
    if current_process_id is None:
        process_relation = "none"
    elif current_process_id == prior_process_id:
        process_relation = "same-prior"
    else:
        process_relation = "replacement"
    raise SparkleUpdateFailure(
        "Sparkle updater did not reach a bounded install state before timeout "
        f"(last_state={(last_recorded_state or SparkleUpdateState.WAITING_FOR_WINDOW).value}, "
        f"install_update_pressed={str(install_update_pressed).lower()}, "
        f"action_count={action_count}, "
        f"process_relation={process_relation}, "
        f"states_observed={','.join(states_observed)}).",
        reason_code="update-window-timeout",
        state=last_recorded_state or SparkleUpdateState.WAITING_FOR_WINDOW,
        action_pressed=action_count > 0,
    )


def _wait_for_candidate(
    app_path: Path,
    candidate: ReleaseArtifact,
    operations: QualificationOperations,
    prior_process_id: int,
) -> BundleIdentity:
    deadline = time.monotonic() + UPDATE_TIMEOUT_SECONDS
    last_error: CleanMachineError | None = None
    while time.monotonic() < deadline:
        if app_path.exists():
            try:
                identity = verify_installed_app(app_path, candidate)
            except CleanMachineError as error:
                last_error = error
            else:
                current_process_id = operations.app_process_id(app_path)
                if current_process_id is not None and current_process_id != prior_process_id:
                    return identity
        time.sleep(1)
    detail = f" Last identity error: {last_error}" if last_error is not None else ""
    raise CleanMachineError(f"Sparkle did not install and relaunch the exact candidate before timeout.{detail}")


def _wait_for_candidate_on_disk(app_path: Path, candidate: ReleaseArtifact) -> BundleIdentity:
    deadline = time.monotonic() + UPDATE_TIMEOUT_SECONDS
    last_error: CleanMachineError | None = None
    previous_identity: BundleIdentity | None = None
    while time.monotonic() < deadline:
        if app_path.exists():
            try:
                identity = verify_installed_app(app_path, candidate)
            except CleanMachineError as error:
                last_error = error
                previous_identity = None
            else:
                if identity == previous_identity:
                    return identity
                previous_identity = identity
        time.sleep(1)
    detail = f" Last identity error: {last_error}" if last_error is not None else ""
    raise CleanMachineError(f"Sparkle did not install the exact candidate before timeout.{detail}")


def _seed_profile(synthetic_home: Path) -> tuple[Path, str]:
    profile_path = synthetic_home / PROFILE_RELATIVE_PATH
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_bytes(_canonical_json_bytes(PROFILE_DOCUMENT))
    return profile_path, file_sha256(profile_path)


def _reset_runtime_workspace(app_path: Path, synthetic_home: Path, installed_release: ReleaseArtifact) -> None:
    if app_path.exists():
        verify_installed_app(app_path, installed_release)
        shutil.rmtree(app_path)
    if synthetic_home.exists():
        shutil.rmtree(synthetic_home)
    synthetic_home.mkdir(parents=True)


def _diagnostic_app_state(app_path: Path, prior: ReleaseArtifact, candidate: ReleaseArtifact) -> str:
    if not app_path.exists():
        return "missing"
    try:
        identity = read_bundle_identity(app_path)
    except CleanMachineError:
        return "unreadable"
    if identity.package_version == candidate.package_version and identity.build_version == candidate.build_version:
        return "candidate"
    if identity.package_version == prior.package_version and identity.build_version == prior.build_version:
        return "prior"
    if identity.bundle_identifier == BUNDLE_IDENTIFIER:
        return "other-production-build"
    return "unexpected-bundle"


def _record_sparkle_failure_diagnostics(
    *,
    config: QualificationConfig,
    operations: QualificationOperations,
    runtime_layout: RuntimeLayout,
    prior: ReleaseArtifact,
    candidate: ReleaseArtifact,
    failure_stage: str,
    reason_code: str,
) -> str:
    try:
        facts = operations.collect_sparkle_diagnostics(runtime_layout.home)
    except (CleanMachineError, OSError, subprocess.TimeoutExpired):
        facts = SparkleDiagnosticFacts(
            cache_root_state="unknown",
            classifications=("diagnostic-collection-failed",),
            home_library_state="unknown",
            log_event_count=0,
            log_query_status="failed",
        )
    payload: Mapping[str, Any] = {
        "app_state": _diagnostic_app_state(runtime_layout.app_path, prior, candidate),
        "cache_root_state": facts.cache_root_state,
        "classifications": list(facts.classifications),
        "environment_class": config.environment_class,
        "failure_stage": failure_stage,
        "home_library_state": facts.home_library_state,
        "log_event_count": facts.log_event_count,
        "log_query_status": facts.log_query_status,
        "process_state": "running" if operations.app_running(runtime_layout.app_path) else "not-running",
        "reason_code": reason_code,
        "runtime_layout": runtime_layout.kind,
        "schema_version": 1,
        "status": "failed",
    }
    if config.diagnostics_output is not None:
        _write_json(config.diagnostics_output.resolve(), payload)
    return (
        "Sparkle diagnostics: "
        f"stage={failure_stage}, reason={reason_code}, app_state={payload['app_state']}, "
        f"process_state={payload['process_state']}, home_library={facts.home_library_state}, "
        f"cache_root={facts.cache_root_state}, log_query={facts.log_query_status}, "
        f"classifications={','.join(facts.classifications)}."
    )


def _remove_resettable_vm_app(
    app_path: Path,
    prior: ReleaseArtifact,
    candidate: ReleaseArtifact,
) -> None:
    if not app_path.exists():
        return
    for release in (prior, candidate):
        try:
            verify_installed_app(app_path, release)
        except CleanMachineError:
            continue
        break
    else:
        raise CleanMachineError("Refusing to remove an unverified bundle from the system Applications directory.")
    shutil.rmtree(app_path)


def _load_ui_json(path: Path, expected_keys: set[str]) -> Mapping[str, Any]:
    try:
        value = _mapping(json.loads(path.read_text(encoding="utf-8")), path.name)
    except (OSError, json.JSONDecodeError) as error:
        raise CleanMachineError(f"Installed UI evidence is missing or invalid: {path.name}") from error
    if set(value) != expected_keys:
        raise CleanMachineError(f"Installed UI evidence has unexpected fields: {path.name}")
    return value


def _copy_ui_screenshot(source: Path, destination: Path) -> str:
    try:
        payload = source.read_bytes()
    except OSError as error:
        raise CleanMachineError(f"Installed UI screenshot is missing: {source.name}") from error
    if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
        raise CleanMachineError(f"Installed UI screenshot is not a PNG: {source.name}")
    if not 64 <= len(payload) <= MAX_UI_SCREENSHOT_BYTES:
        raise CleanMachineError(f"Installed UI screenshot size is outside the accepted bounds: {source.name}")
    destination.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


def _normalize_installed_ui_evidence(
    raw_directory: Path,
    evidence_directory: Path,
    feed: FeedCandidate,
) -> Mapping[str, str]:
    updater = _load_ui_json(
        raw_directory / "updater-ui.json",
        {"install_action", "release_notes_url", "release_notes_url_observed", "schema_version", "status"},
    )
    if updater != {
        "install_action": updater.get("install_action"),
        "release_notes_url": feed.release_notes_url,
        "release_notes_url_observed": True,
        "schema_version": 1,
        "status": "passed",
    }:
        raise CleanMachineError("Installed UI updater evidence does not match the exact appcast candidate.")
    install_action = updater.get("install_action")
    if install_action not in SPARKLE_INSTALL_ACTIONS:
        raise CleanMachineError(
            f"Installed UI updater evidence contains an unsupported install action: {install_action!r}; "
            f"expected one of {list(SPARKLE_INSTALL_ACTIONS)!r}."
        )

    return normalize_installed_ui_candidate_evidence(
        raw_directory,
        evidence_directory,
        release_notes_url=feed.release_notes_url,
    )


def normalize_installed_ui_candidate_evidence(
    raw_directory: Path,
    evidence_directory: Path,
    *,
    release_notes_url: str,
) -> Mapping[str, str]:
    _string(release_notes_url, "installed UI release notes URL")

    candidate = _load_ui_json(
        raw_directory / "candidate-ui.json",
        {
            "main_window_ready",
            "profile_document_version",
            "profile_save_accessible",
            "profile_save_succeeded",
            "profiles_after",
            "release_page_url",
            "release_page_url_observed",
            "schema_version",
            "status",
            "updater_controls_accessible",
        },
    )
    expected_candidate = {
        "main_window_ready": True,
        "profile_document_version": 6,
        "profile_save_accessible": True,
        "profile_save_succeeded": True,
        "profiles_after": 1,
        "release_page_url": RELEASES_URL,
        "release_page_url_observed": True,
        "schema_version": 1,
        "status": "passed",
        "updater_controls_accessible": True,
    }
    if candidate != expected_candidate:
        raise CleanMachineError("Installed UI candidate evidence did not satisfy the maintained acceptance contract.")

    accessibility = _load_ui_json(raw_directory / "accessibility-tree.json", {"elements", "schema_version"})
    if accessibility.get("schema_version") != 1:
        raise CleanMachineError("Installed UI accessibility evidence uses an unsupported schema version.")
    records = [
        _mapping(item, "installed UI accessibility element")
        for item in _sequence(accessibility.get("elements"), "installed UI accessibility elements")
    ]
    expected_identifiers = {
        "all-releases-link",
        "save-profile-action",
        "update-action",
        "update-route-picker",
    }
    normalized_records: list[Mapping[str, Any]] = []
    observed_identifiers: set[str] = set()
    for record in records:
        allowed_keys = {"actions", "enabled", "help", "identifier", "label", "role", "url"}
        if not set(record).issubset(allowed_keys):
            raise CleanMachineError("Installed UI accessibility evidence contains unsupported fields.")
        identifier = _string(record.get("identifier"), "installed UI accessibility identifier")
        if identifier not in expected_identifiers or identifier in observed_identifiers:
            raise CleanMachineError("Installed UI accessibility evidence contains an unexpected or duplicate element.")
        observed_identifiers.add(identifier)
        role = _string(record.get("role"), f"{identifier} accessibility role")
        label_value = record.get("label")
        if not isinstance(label_value, str):
            raise CleanMachineError(f"{identifier} accessibility label must be a string.")
        if identifier == "update-route-picker":
            if label_value not in {"", "Update route"}:
                raise CleanMachineError("Update route picker accessibility label is unexpected.")
            label = label_value
        else:
            label = _string(label_value, f"{identifier} accessibility label")
        help_text = record.get("help")
        actions = record.get("actions")
        enabled = record.get("enabled")
        if not isinstance(help_text, str) or len(help_text) > 300:
            raise CleanMachineError(f"{identifier} accessibility help is invalid.")
        if (
            not isinstance(enabled, bool)
            or not isinstance(actions, list)
            or not all(isinstance(item, str) for item in actions)
        ):
            raise CleanMachineError(f"{identifier} accessibility state is invalid.")
        if len(label) > 300 or any(character in label for character in ("\n", "\r")):
            raise CleanMachineError(f"{identifier} accessibility label is invalid.")
        normalized: dict[str, Any] = {
            "enabled": enabled,
            "help": help_text,
            "identifier": identifier,
            "label": label,
            "press_action": "AXPress" in actions,
            "role": role,
        }
        if identifier == "all-releases-link":
            if record.get("url") != RELEASES_URL:
                raise CleanMachineError("Installed UI releases link is not bound to the public releases page.")
            normalized["url_sha256"] = hashlib.sha256(RELEASES_URL.encode()).hexdigest()
        if identifier == "save-profile-action":
            if (
                role != "AXButton"
                or label != "Save current settings as new profile"
                or help_text != "Opens a form to name and save these settings as a reusable profile"
                or not enabled
                or "AXPress" not in actions
            ):
                raise CleanMachineError("Profile save accessibility semantics do not match the maintained contract.")
        if identifier == "update-route-picker" and (role != "AXPopUpButton" or not enabled or "AXPress" not in actions):
            raise CleanMachineError("Update route picker accessibility semantics do not match the maintained contract.")
        normalized_records.append(normalized)
    if observed_identifiers != expected_identifiers:
        raise CleanMachineError("Installed UI accessibility evidence is incomplete.")

    evidence_directory.mkdir(parents=True, exist_ok=True)
    accessibility_digest = _write_json(
        evidence_directory / "accessibility-tree.json",
        {"elements": sorted(normalized_records, key=lambda item: cast(str, item["identifier"])), "schema_version": 1},
    )
    ui_result_digest = _write_json(
        evidence_directory / "ui-result.json",
        {
            "main_window_ready": True,
            "profile_document_version": 6,
            "profile_save_accessible": True,
            "profile_save_succeeded": True,
            "release_notes_url_sha256": hashlib.sha256(release_notes_url.encode()).hexdigest(),
            "release_page_url_sha256": hashlib.sha256(RELEASES_URL.encode()).hexdigest(),
            "schema_version": 1,
            "status": "passed",
            "updater_controls_accessible": True,
        },
    )
    light_digest = _copy_ui_screenshot(
        raw_directory / "screenshot-light.png",
        evidence_directory / "screenshot-light.png",
    )
    dark_digest = _copy_ui_screenshot(
        raw_directory / "screenshot-dark.png",
        evidence_directory / "screenshot-dark.png",
    )
    return {
        "accessibility-tree": accessibility_digest,
        "ui-result": ui_result_digest,
        "screenshot-light": light_digest,
        "screenshot-dark": dark_digest,
    }


def _build_checked_receipt(
    *,
    policy: Mapping[str, Any],
    case: Mapping[str, Any],
    candidate: ReleaseArtifact,
    environment: EnvironmentFacts,
    assertions: Mapping[str, str],
    evidence: Sequence[Mapping[str, str]],
    cleanup_status: str,
    cleanup_digest: str,
    started_at: str,
    completed_at: str,
    repo: Path,
) -> Mapping[str, Any]:
    required_assertions = set(cast(Sequence[str], case["required_assertions"]))
    if set(assertions) != required_assertions:
        raise CleanMachineError(
            f"Runner assertion contract changed; code={sorted(assertions)}, policy={sorted(required_assertions)}."
        )
    receipt = build_tier3_receipt(
        {
            "assertions": dict(assertions),
            "cleanup": {"status": cleanup_status, "evidence_sha256": cleanup_digest},
            "completed_at": completed_at,
            "environment": {
                "architecture": environment.architecture,
                "environment_class": environment.environment_class,
                "macos_build": environment.macos_build,
                "macos_version": environment.macos_version,
            },
            "evidence": list(evidence),
            "evidence_source": "tier3_automation_receipt",
            "hardware": {"class": "none", "identity": {}},
            "release_receipt_file_sha256": candidate.receipt_file_sha256,
            "release_receipt_reference": candidate.receipt_reference,
            "result": {"status": "passed", "reason_code": "all_assertions_passed"},
            "started_at": started_at,
        },
        policy_id=_string(policy.get("policy_id"), "policy_id"),
        case=case,
        release_receipt=candidate.receipt,
    )
    try:
        validate_tier3_receipt(
            receipt,
            policy_id=_string(policy.get("policy_id"), "policy_id"),
            case=case,
            reference_content=lambda reference: (repo / reference).read_bytes(),
        )
    except Tier3ReceiptError as error:
        raise CleanMachineError(f"Generated Tier 3 receipt is invalid: {error}") from error
    return receipt


def _run_qualification(
    config: QualificationConfig,
    operations: QualificationOperations,
    *,
    attempt: int = 1,
    retry_reason_code: str | None = None,
) -> Mapping[str, Mapping[str, Any]]:
    if config.output_receipt is None or config.ui_output_receipt is None or config.evidence_directory is None:
        raise CleanMachineError("Run mode requires both output receipts and the evidence directory path.")
    output_receipt = config.output_receipt.resolve()
    ui_output_receipt = config.ui_output_receipt.resolve()
    evidence_directory = config.evidence_directory.resolve()
    if output_receipt.exists() or ui_output_receipt.exists() or evidence_directory.exists():
        raise CleanMachineError("Output receipts and evidence directory must not already exist.")

    policy, clean_machine_case, installed_ui_case, prior, candidate, environment, feed = prepare_qualification(
        config, operations
    )
    started_at = _utc_timestamp()
    qualification_root = config.qualification_root.resolve()
    runtime_layout = _runtime_layout(config)
    synthetic_home = runtime_layout.home
    app_path = runtime_layout.app_path
    log_path = qualification_root / "Logs" / "package-smoke.log"
    raw_ui_directory = qualification_root / "InstalledUIEvidence"
    marker_path = qualification_root / ".bd-to-avp-tier3-owned.json"
    qualification_root.mkdir(parents=True)
    marker = {"owner": "bd-to-avp-tier3-clean-machine", "run_id": str(uuid.uuid4())}
    marker_path.write_text(json.dumps(marker, sort_keys=True) + "\n", encoding="utf-8")
    try:
        runtime_layout.smoke_home.mkdir(parents=True)
        operations.install_app(candidate.dmg_path, app_path, qualification_root / "Mount")
        candidate_install_identity = verify_installed_app(app_path, candidate)
        package_smoke_log_sha256 = operations.smoke_app(app_path, runtime_layout.smoke_home, log_path)
        operations.quit_app()
        if operations.app_running():
            raise CleanMachineError("Candidate package smoke left the production app running.")

        _reset_runtime_workspace(app_path, runtime_layout.smoke_home, candidate)
        synthetic_home.mkdir(parents=True, exist_ok=True)
        operations.install_app(prior.dmg_path, app_path, qualification_root / "Mount")
        prior_identity = verify_installed_app(app_path, prior)
        profile_path, profile_before_sha256 = _seed_profile(synthetic_home)
        operations.write_preferences(synthetic_home, config.route)
        if operations.read_preference(synthetic_home, UPDATE_ROUTE_KEY) != config.route:
            raise CleanMachineError("Update route did not persist before launching the prior release.")
        if operations.read_preference(synthetic_home, SENTINEL_KEY) != SENTINEL_VALUE:
            raise CleanMachineError("Preference sentinel did not persist before the Sparkle update.")

        operations.collect_ui_evidence(
            repo=config.repo.resolve(),
            phase="updater",
            app_path=app_path,
            synthetic_home=synthetic_home,
            output_directory=raw_ui_directory,
            release_notes_url=feed.release_notes_url,
        )
        operations.quit_app()
        if operations.app_running():
            raise CleanMachineError("Installed UI updater inspection left the production app running.")

        failure_stage = "updater-state-machine"
        try:
            interaction = _perform_sparkle_update(
                app_path=app_path,
                synthetic_home=synthetic_home,
                prior=prior,
                candidate=candidate,
                operations=operations,
                checkpoint_path=qualification_root / ".bd-to-avp-sparkle-update.json",
                run_id=marker["run_id"],
                attempt=attempt,
                retry_reason_code=retry_reason_code,
            )
            if interaction.outcome == SparkleUpdateOutcome.INSTALL_ON_QUIT:
                failure_stage = "install-on-quit-wait"
                operations.quit_app()
                _wait_for_candidate_on_disk(app_path, candidate)
                operations.launch_app(app_path, synthetic_home)
            failure_stage = "candidate-relaunch-wait"
            candidate_identity = _wait_for_candidate(
                app_path,
                candidate,
                operations,
                interaction.prior_process_id,
            )
        except SparkleUpdateFailure as error:
            diagnostic_summary = _record_sparkle_failure_diagnostics(
                config=config,
                operations=operations,
                runtime_layout=runtime_layout,
                prior=prior,
                candidate=candidate,
                failure_stage=failure_stage,
                reason_code=error.reason_code,
            )
            raise SparkleUpdateFailure(
                f"{error} {diagnostic_summary}",
                reason_code=error.reason_code,
                state=error.state,
                action_pressed=error.action_pressed,
            ) from error
        except (CleanMachineError, OSError, subprocess.TimeoutExpired) as error:
            diagnostic_summary = _record_sparkle_failure_diagnostics(
                config=config,
                operations=operations,
                runtime_layout=runtime_layout,
                prior=prior,
                candidate=candidate,
                failure_stage=failure_stage,
                reason_code="post-action-verification-failed",
            )
            raise CleanMachineError(f"{error} {diagnostic_summary}") from error
        route_after = operations.read_preference(synthetic_home, UPDATE_ROUTE_KEY)
        sentinel_after = operations.read_preference(synthetic_home, SENTINEL_KEY)
        profile_after_sha256 = file_sha256(profile_path)
        if route_after != config.route or sentinel_after != SENTINEL_VALUE:
            raise CleanMachineError("Update route or unrelated preference changed across Sparkle relaunch.")
        if profile_after_sha256 != profile_before_sha256:
            raise CleanMachineError("Profile library changed across Sparkle relaunch.")

        operations.quit_app()
        operations.collect_ui_evidence(
            repo=config.repo.resolve(),
            phase="candidate",
            app_path=app_path,
            synthetic_home=synthetic_home,
            output_directory=raw_ui_directory,
            release_notes_url=feed.release_notes_url,
        )
        operations.quit_app()
        if operations.app_running():
            raise CleanMachineError("Installed UI candidate inspection left the production app running.")

        evidence_directory.mkdir(parents=True)
        install_digest = _write_json(
            evidence_directory / "install-log.json",
            {
                "candidate": {
                    "build_version": candidate_install_identity.build_version,
                    "package_version": candidate_install_identity.package_version,
                    "signed_app_tree_sha256": candidate.signed_app_tree_sha256,
                },
                "prior": {
                    "build_version": prior_identity.build_version,
                    "package_version": prior_identity.package_version,
                    "signed_app_tree_sha256": prior.signed_app_tree_sha256,
                },
                "status": "passed",
            },
        )
        package_digest = _write_json(
            evidence_directory / "package-smoke.json",
            {
                "maintained_smoke": "scripts/smoke_release_app.py",
                "raw_log_sha256": package_smoke_log_sha256,
                "status": "passed",
            },
        )
        update_digest = _write_json(
            evidence_directory / "sparkle-update.json",
            {
                "action_identifier": interaction.action_identifier or None,
                "attempt": interaction.attempt,
                "button": interaction.clicked_button,
                "candidate": {
                    "build_version": candidate_identity.build_version,
                    "package_version": candidate_identity.package_version,
                    "signed_app_tree_sha256": candidate.signed_app_tree_sha256,
                },
                "feed_sha256": feed.feed_sha256,
                "intent_checkpoint_sha256": interaction.intent_checkpoint_sha256,
                "journal_sha256": interaction.journal_sha256,
                "outcome": interaction.outcome.value,
                "retry_reason_code": interaction.retry_reason_code,
                "route": config.route,
                "states_observed": list(interaction.states_observed),
                "status": "passed",
                "window_match": interaction.window_match,
            },
        )
        profile_digest = _write_json(
            evidence_directory / "profile-snapshot.json",
            {
                "profile_after_sha256": profile_after_sha256,
                "profile_before_sha256": profile_before_sha256,
                "profile_preserved": True,
                "route_after": route_after,
                "route_preserved": True,
                "sentinel_preserved": sentinel_after == SENTINEL_VALUE,
            },
        )
        ui_evidence_digests = _normalize_installed_ui_evidence(raw_ui_directory, evidence_directory, feed)
    finally:
        try:
            operations.quit_app()
        finally:
            try:
                if config.environment_class == RESETTABLE_VM_ENVIRONMENT_CLASS:
                    try:
                        _remove_resettable_vm_app(app_path, prior, candidate)
                    finally:
                        operations.clear_preferences(synthetic_home)
                        _remove_managed_real_home_state(synthetic_home)
            finally:
                if qualification_root.exists():
                    try:
                        observed_marker = json.loads(marker_path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError) as error:
                        raise CleanMachineError(
                            "Qualification cleanup ownership marker is missing or invalid."
                        ) from error
                    if observed_marker != marker:
                        raise CleanMachineError("Qualification cleanup ownership marker changed during the run.")
                    shutil.rmtree(qualification_root)
                cleanup_status = "disposed"

    managed_state_exists = config.environment_class == RESETTABLE_VM_ENVIRONMENT_CLASS and any(
        path.exists() or path.is_symlink() for path in _managed_real_home_paths(synthetic_home)
    )
    if operations.app_running() or app_path.exists() or managed_state_exists or qualification_root.exists():
        raise CleanMachineError("Qualification cleanup did not remove every runner-owned runtime surface.")
    cleanup_digest = _write_json(
        evidence_directory / "cleanup.json",
        {
            "app_running": False,
            "app_target_exists": False,
            "managed_home_state_exists": False,
            "qualification_root_exists": False,
            "runtime_layout": runtime_layout.kind,
            "status": cleanup_status,
        },
    )

    completed_at = _utc_timestamp()
    clean_machine_assertions = {
        "exact-app-installed": "passed",
        "final-identity-matched": "passed",
        "package-smoke-passed": "passed",
        "preconditions-proven": "passed",
        "profile-preserved": "passed",
        "sparkle-relaunch-passed": "passed",
    }
    clean_machine_receipt = _build_checked_receipt(
        policy=policy,
        case=clean_machine_case,
        candidate=candidate,
        environment=environment,
        assertions=clean_machine_assertions,
        evidence=[
            {"kind": "install-log", "sha256": install_digest},
            {"kind": "package-smoke", "sha256": package_digest},
            {"kind": "sparkle-update", "sha256": update_digest},
            {"kind": "profile-snapshot", "sha256": profile_digest},
            {"kind": "cleanup", "sha256": cleanup_digest},
        ],
        cleanup_status=cleanup_status,
        cleanup_digest=cleanup_digest,
        started_at=started_at,
        completed_at=completed_at,
        repo=config.repo.resolve(),
    )
    installed_ui_receipt = _build_checked_receipt(
        policy=policy,
        case=installed_ui_case,
        candidate=candidate,
        environment=environment,
        assertions={
            "main-window-ready": "passed",
            "profile-save-accessible": "passed",
            "profile-save-succeeded": "passed",
            "release-note-links-bound": "passed",
            "updater-controls-accessible": "passed",
        },
        evidence=[{"kind": kind, "sha256": digest} for kind, digest in sorted(ui_evidence_digests.items())],
        cleanup_status=cleanup_status,
        cleanup_digest=cleanup_digest,
        started_at=started_at,
        completed_at=completed_at,
        repo=config.repo.resolve(),
    )
    _write_json(output_receipt, clean_machine_receipt)
    _write_json(ui_output_receipt, installed_ui_receipt)
    return {
        CLEAN_MACHINE_CASE_ID: clean_machine_receipt,
        INSTALLED_UI_CASE_ID: installed_ui_receipt,
    }


def run_qualification(
    config: QualificationConfig, operations: QualificationOperations
) -> Mapping[str, Mapping[str, Any]]:
    def remove_partial_outputs() -> None:
        if config.output_receipt is not None and config.output_receipt.exists():
            config.output_receipt.unlink()
        if config.ui_output_receipt is not None and config.ui_output_receipt.exists():
            config.ui_output_receipt.unlink()
        if config.evidence_directory is not None and config.evidence_directory.exists():
            shutil.rmtree(config.evidence_directory)

    if config.diagnostics_output is not None and config.diagnostics_output.exists():
        raise CleanMachineError("Diagnostics output must not already exist.")

    retry_reason_code: str | None = None
    for attempt in (1, 2):
        try:
            return _run_qualification(
                config,
                operations,
                attempt=attempt,
                retry_reason_code=retry_reason_code,
            )
        except SparkleUpdateFailure as error:
            remove_partial_outputs()
            runtime_layout = _runtime_layout(config)
            managed_state_exists = config.environment_class == RESETTABLE_VM_ENVIRONMENT_CLASS and any(
                path.exists() or path.is_symlink() for path in _managed_real_home_paths(runtime_layout.home)
            )
            clean_retry_environment = (
                not config.qualification_root.resolve().exists()
                and not runtime_layout.app_path.exists()
                and not managed_state_exists
                and not operations.app_running()
            )
            if attempt == 1 and error.retryable and clean_retry_environment:
                if config.diagnostics_output is not None:
                    config.diagnostics_output.unlink(missing_ok=True)
                retry_reason_code = error.reason_code
                continue
            raise
        except BaseException:
            remove_partial_outputs()
            raise
    raise AssertionError("bounded Sparkle qualification retry loop exhausted")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Qualify exact signed-app installation and Sparkle update evidence.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("preflight", "run"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--repo", type=Path, default=REPO_ROOT)
        subparser.add_argument("--candidate-release-receipt", type=Path, required=True)
        subparser.add_argument("--candidate-dmg", type=Path, required=True)
        subparser.add_argument("--prior-release-receipt", type=Path, required=True)
        subparser.add_argument("--prior-dmg", type=Path, required=True)
        subparser.add_argument("--qualification-root", type=Path, required=True)
        subparser.add_argument("--route", choices=tuple(ROUTE_CHANNELS), required=True)
        subparser.add_argument(
            "--environment-class",
            choices=(RESTORABLE_LOCATION_ENVIRONMENT_CLASS, RESETTABLE_VM_ENVIRONMENT_CLASS),
            required=True,
        )
        if command == "run":
            subparser.add_argument("--output-receipt", type=Path, required=True)
            subparser.add_argument("--ui-output-receipt", type=Path, required=True)
            subparser.add_argument("--evidence-directory", type=Path, required=True)
            subparser.add_argument("--diagnostics-output", type=Path)
    return parser


def _config_from_args(args: argparse.Namespace) -> QualificationConfig:
    return QualificationConfig(
        repo=args.repo,
        candidate_receipt=args.candidate_release_receipt,
        candidate_dmg=args.candidate_dmg,
        prior_receipt=args.prior_release_receipt,
        prior_dmg=args.prior_dmg,
        qualification_root=args.qualification_root,
        route=args.route,
        environment_class=args.environment_class,
        output_receipt=getattr(args, "output_receipt", None),
        ui_output_receipt=getattr(args, "ui_output_receipt", None),
        evidence_directory=getattr(args, "evidence_directory", None),
        diagnostics_output=getattr(args, "diagnostics_output", None),
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = _config_from_args(args)
    operations = MacOSOperations()
    try:
        if args.command == "preflight":
            payload = preflight_report(config, operations)
        else:
            receipts = run_qualification(config, operations)
            payload = {
                "receipts": [
                    {
                        "case_id": receipt["case_id"],
                        "receipt_sha256": receipt["receipt_sha256"],
                        "result": receipt["result"],
                    }
                    for _, receipt in sorted(receipts.items())
                ]
            }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    except (CleanMachineError, OSError, subprocess.TimeoutExpired) as error:
        print(f"tier3 clean-machine error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
