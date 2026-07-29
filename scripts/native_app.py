from __future__ import annotations

import argparse
import fcntl
import json
import os
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit
from uuid import uuid4

from bd_to_avp.observability import ObservabilityEvent
from bd_to_avp.worker.protocol import PROTOCOL_VERSION
from scripts.artifact_identity import app_tree_sha256
from scripts.build_mv_hevc_encoder_macos import build_encoder as build_mv_hevc_encoder
from scripts.production_identity import (
    PRODUCTION_BUNDLE_IDENTIFIER,
    PRODUCTION_DISTRIBUTION_CHANNEL,
    PRODUCTION_FEED_URL,
    PRODUCTION_PRODUCT_NAME,
    PRODUCTION_SPARKLE_PUBLIC_KEY,
    validate_production_public_key,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
MACOS_ROOT = REPO_ROOT / "macos"
PROJECT_SPEC = MACOS_ROOT / "project.yml"
NATIVE_PROJECT_NAME = "BluRayToVisionPro"
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"
with PYPROJECT_PATH.open("rb") as pyproject_file:
    PYPROJECT = tomllib.load(pyproject_file)
PROJECT_METADATA = PYPROJECT["project"]
BRIEFCASE_METADATA = PYPROJECT["tool"]["briefcase"]
BRIEFCASE_APP_METADATA = BRIEFCASE_METADATA["app"]["bd-to-avp"]
NATIVE_PRODUCT_NAME = PRODUCTION_PRODUCT_NAME
NATIVE_BUNDLE_IDENTIFIER = PRODUCTION_BUNDLE_IDENTIFIER
NATIVE_SHORT_VERSION = str(PROJECT_METADATA["version"])
NATIVE_UPDATE_INFO = dict(BRIEFCASE_APP_METADATA["macOS"]["info"])
NATIVE_BUILD_VERSION = str(NATIVE_UPDATE_INFO["CFBundleVersion"])
NATIVE_MINIMUM_SYSTEM_VERSION = "26.0"
NATIVE_EXECUTABLE_NAME = NATIVE_PRODUCT_NAME
PROJECT_PATH = MACOS_ROOT / f"{NATIVE_PROJECT_NAME}.xcodeproj"
SCHEME = NATIVE_PROJECT_NAME
NATIVE_PACKAGE_CONFIGURATION = "Release"
DERIVED_DATA = MACOS_ROOT / "build" / "DerivedData"
NATIVE_APP_NAME = f"{NATIVE_PRODUCT_NAME}.app"
BRIEFCASE_APP = REPO_ROOT / "build" / "bd-to-avp" / "macos" / "app" / "3D Blu-ray to Vision Pro.app"
PACKAGE_ROOT = MACOS_ROOT / "build" / "package"
PACKAGED_APP = PACKAGE_ROOT / NATIVE_APP_NAME
CURRENT_BUILD_DIRECTORY_NAME = "BD to AVP Builds"
CURRENT_APP_LINK_NAME = "3D Blu-ray to Vision Pro Current.app"
CURRENT_METADATA_LINK_NAME = "3D Blu-ray to Vision Pro Current.build.json"
CURRENT_BUILD_METADATA_NAME = "build.json"
CURRENT_POINTER_LINK_NAME = ".3D Blu-ray to Vision Pro Current"
CURRENT_PUBLISH_LOCK_NAME = ".3D Blu-ray to Vision Pro Current.lock"
MV_HEVC_ENCODER_NAME = "mv-hevc-encoder"
PACKAGED_MV_HEVC_ENCODER = PACKAGE_ROOT / "native-tools" / MV_HEVC_ENCODER_NAME
WORKER_EXECUTABLE_NAME = "BluRayToVisionProEngine"
WORKER_PROTOCOL_VERSION = PROTOCOL_VERSION
WORKER_ENTITLEMENTS = MACOS_ROOT / "BluRayToVisionPro" / "Worker.entitlements"
DEPLOYMENT_TARGET_OVERRIDE_ENV = "BD_TO_AVP_MACOS_DEPLOYMENT_TARGET_OVERRIDE"
SUPPORT_DIAGNOSTICS_ENDPOINT_ENV = "BD_TO_AVP_SUPPORT_DIAGNOSTICS_ENDPOINT"
SUPPORT_DIAGNOSTICS_ENDPOINT_INFO_KEY = "BDToAVPSupportDiagnosticsEndpoint"
USER_INTERFACE_SOURCE_FILES = sorted(
    [
        *(MACOS_ROOT / "BluRayToVisionPro" / "App").glob("*.swift"),
        *(MACOS_ROOT / "BluRayToVisionPro" / "Feature").glob("*.swift"),
        *(MACOS_ROOT / "BluRayToVisionPro" / "Models").glob("*.swift"),
        *(MACOS_ROOT / "BluRayToVisionPro" / "Views").glob("*.swift"),
    ]
)
BANNED_USER_COPY = (
    "native worker prototype",
    "protocol v1",
    "native-prototype",
    "prototype slice",
    "python worker",
    "the worker",
    "worker activity",
)
BANNED_RELEASE_IDENTIFIERS = (
    "BDToAVPNative",
    "Native Preview",
    ".native-preview",
    "native-prototype",
    "Native worker prototype",
    "Protocol v1",
)
REQUIRED_NATIVE_FRAMEWORK_LINKS = frozenset(
    {
        "/System/Library/Frameworks/AVKit.framework/Versions/A/AVKit",
    }
)
MACH_O_MAGICS = {
    b"\xca\xfe\xba\xbe",
    b"\xbe\xba\xfe\xca",
    b"\xca\xfe\xba\xbf",
    b"\xbf\xba\xfe\xca",
    b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xce",
    b"\xcf\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
}


def validate_repository_production_identity() -> None:
    derived_bundle_identifier = f"{BRIEFCASE_METADATA['bundle']}.bd-to-avp"
    expected_values = {
        "Briefcase product name": (str(BRIEFCASE_APP_METADATA["formal_name"]), PRODUCTION_PRODUCT_NAME),
        "Briefcase bundle identifier": (derived_bundle_identifier, PRODUCTION_BUNDLE_IDENTIFIER),
        "distribution channel": (
            str(NATIVE_UPDATE_INFO.get("BDToAVPDistributionChannel", "")),
            PRODUCTION_DISTRIBUTION_CHANNEL,
        ),
        "Sparkle feed URL": (str(NATIVE_UPDATE_INFO.get("SUFeedURL", "")), PRODUCTION_FEED_URL),
        "Sparkle public key": (
            str(NATIVE_UPDATE_INFO.get("SUPublicEDKey", "")),
            PRODUCTION_SPARKLE_PUBLIC_KEY,
        ),
    }
    mismatches = [
        f"{description}: expected {expected!r}, found {actual!r}"
        for description, (actual, expected) in expected_values.items()
        if actual != expected
    ]
    try:
        validate_production_public_key(str(NATIVE_UPDATE_INFO.get("SUPublicEDKey", "")))
    except ValueError as error:
        mismatches.append(str(error))
    if mismatches:
        raise RuntimeError("Repository production identity validation failed:\n" + "\n".join(mismatches))


validate_repository_production_identity()


def run(
    command: list[str],
    *,
    cwd: Path = REPO_ROOT,
    environment: dict[str, str] | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=True,
        cwd=cwd,
        env=environment,
        text=True,
        timeout=timeout,
    )


def generate_project() -> None:
    verify_product_source_copy()
    run(["xcodegen", "generate", "--spec", PROJECT_SPEC.name], cwd=MACOS_ROOT)


def verify_product_source_copy() -> None:
    violations: list[str] = []
    for source_path in USER_INTERFACE_SOURCE_FILES:
        source_text = source_path.read_text(encoding="utf-8").lower()
        for marker in BANNED_USER_COPY:
            if marker.lower() in source_text:
                violations.append(f"{source_path.relative_to(REPO_ROOT)}: {marker}")
    if violations:
        raise RuntimeError("User-facing macOS copy contains internal terminology:\n" + "\n".join(violations))


def xcodebuild(action: str, configuration: str) -> None:
    generate_project()
    build_settings = native_build_settings(configuration, os.environ)
    run(
        [
            "xcodebuild",
            "-project",
            PROJECT_PATH.name,
            "-scheme",
            SCHEME,
            "-configuration",
            configuration,
            "-derivedDataPath",
            str(DERIVED_DATA),
            *build_settings,
            action,
        ],
        cwd=MACOS_ROOT,
        environment={**os.environ, "BD_TO_AVP_TEST_PYTHON": sys.executable},
    )


def validate_support_diagnostics_endpoint(endpoint: str) -> str:
    approved_endpoint = endpoint.strip()
    if not approved_endpoint:
        raise ValueError("Invalid support diagnostics endpoint")
    try:
        parsed_endpoint = urlsplit(approved_endpoint)
        endpoint_port = parsed_endpoint.port
    except ValueError as error:
        raise ValueError("Invalid support diagnostics endpoint") from error
    if (
        parsed_endpoint.scheme != "https"
        or parsed_endpoint.hostname is None
        or parsed_endpoint.username is not None
        or parsed_endpoint.password is not None
        or parsed_endpoint.path not in {"", "/"}
        or parsed_endpoint.query
        or parsed_endpoint.fragment
        or any(character.isspace() for character in approved_endpoint)
        or (endpoint_port is not None and not 1 <= endpoint_port <= 65535)
    ):
        raise ValueError("Invalid support diagnostics endpoint")
    return approved_endpoint


def verify_support_diagnostics_endpoint(
    info: Mapping[str, object],
    *,
    environment: Mapping[str, str] | None = None,
) -> str:
    invalid_endpoint_message = f"{SUPPORT_DIAGNOSTICS_ENDPOINT_INFO_KEY} must be a non-empty valid HTTPS endpoint."
    endpoint = info.get(SUPPORT_DIAGNOSTICS_ENDPOINT_INFO_KEY)
    if not isinstance(endpoint, str):
        raise ValueError(invalid_endpoint_message)
    try:
        validated_endpoint = validate_support_diagnostics_endpoint(endpoint)
    except ValueError as error:
        raise ValueError(invalid_endpoint_message) from error
    if endpoint != validated_endpoint:
        raise ValueError(invalid_endpoint_message)

    environment = os.environ if environment is None else environment
    if SUPPORT_DIAGNOSTICS_ENDPOINT_ENV in environment:
        try:
            approved_endpoint = validate_support_diagnostics_endpoint(environment[SUPPORT_DIAGNOSTICS_ENDPOINT_ENV])
        except ValueError as error:
            raise ValueError("Approved support diagnostics endpoint is invalid.") from error
        if endpoint != approved_endpoint:
            raise ValueError(
                f"{SUPPORT_DIAGNOSTICS_ENDPOINT_INFO_KEY} must exactly match the approved support diagnostics endpoint."
            )
    return endpoint


def native_build_settings(configuration: str, environment: Mapping[str, str]) -> list[str]:
    build_settings = ["CODE_SIGNING_ALLOWED=NO"]
    deployment_target = environment.get(DEPLOYMENT_TARGET_OVERRIDE_ENV, "").strip()
    if deployment_target:
        if re.fullmatch(r"\d+(?:\.\d+)+", deployment_target) is None:
            raise ValueError(f"Invalid macOS deployment target override: {deployment_target!r}")
        build_settings.append(f"MACOSX_DEPLOYMENT_TARGET={deployment_target}")
    support_endpoint = environment.get(SUPPORT_DIAGNOSTICS_ENDPOINT_ENV, "").strip()
    if configuration == "Release" and not support_endpoint:
        raise ValueError("Release builds require an approved support diagnostics endpoint")
    if support_endpoint:
        support_endpoint = validate_support_diagnostics_endpoint(support_endpoint)
        build_settings.append(f"BD_TO_AVP_SUPPORT_DIAGNOSTICS_ENDPOINT={support_endpoint}")
    if configuration == "Release":
        build_settings.extend(["ARCHS=arm64", "ENABLE_CODE_COVERAGE=NO", "ONLY_ACTIVE_ARCH=NO"])
        build_settings.extend(
            [
                f"CURRENT_PROJECT_VERSION={NATIVE_BUILD_VERSION}",
                f"MARKETING_VERSION={NATIVE_SHORT_VERSION}",
                f"PRODUCT_BUNDLE_IDENTIFIER={NATIVE_BUNDLE_IDENTIFIER}",
                f"PRODUCT_NAME={NATIVE_PRODUCT_NAME}",
            ]
        )
    return build_settings


def prepare_briefcase_runtime() -> None:
    command = "update" if BRIEFCASE_APP.is_dir() else "create"
    run([sys.executable, "-m", "scripts.briefcase_app", command, "--no-input"])
    run([sys.executable, "-m", "scripts.briefcase_app", "build", "--no-input"])
    if not BRIEFCASE_APP.is_dir():
        raise RuntimeError(f"Briefcase did not create the expected runtime at {BRIEFCASE_APP}")


def build_packaged_mv_hevc_encoder(output_path: Path = PACKAGED_MV_HEVC_ENCODER) -> Path:
    output_path.unlink(missing_ok=True)
    build_mv_hevc_encoder(output_path)
    output_path.chmod(output_path.stat().st_mode | 0o111)
    return output_path


def install_mv_hevc_encoder(app_path: Path, source_path: Path) -> Path:
    if not source_path.is_file():
        raise RuntimeError(f"MV-HEVC encoder build product is missing at {source_path}")
    destination = app_path / "Contents" / "Resources" / "app" / "bd_to_avp" / "bin" / MV_HEVC_ENCODER_NAME
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, destination)
    destination.chmod(destination.stat().st_mode | 0o111)
    return destination


def assemble_package(mv_hevc_encoder_path: Path) -> Path:
    source_app = DERIVED_DATA / "Build" / "Products" / NATIVE_PACKAGE_CONFIGURATION / NATIVE_APP_NAME
    if not source_app.is_dir():
        raise RuntimeError(f"macOS build product is missing at {source_app}")

    if PACKAGED_APP.exists():
        shutil.rmtree(PACKAGED_APP)
    PACKAGE_ROOT.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_app, PACKAGED_APP, symlinks=True)

    source_contents = BRIEFCASE_APP / "Contents"
    destination_contents = PACKAGED_APP / "Contents"
    copy_tree(
        source_contents / "Frameworks" / "Python.framework",
        destination_contents / "Frameworks" / "Python.framework",
    )
    for resource_name in ("app", "app_packages", "support"):
        copy_tree(
            source_contents / "Resources" / resource_name,
            destination_contents / "Resources" / resource_name,
        )
    for internal_document in ("README.md", "pyproject.toml"):
        (destination_contents / "Resources" / "app" / internal_document).unlink(missing_ok=True)
    shutil.rmtree(destination_contents / "Resources" / "app_packages" / "bin", ignore_errors=True)
    install_mv_hevc_encoder(PACKAGED_APP, mv_hevc_encoder_path)

    source_launcher = source_contents / "MacOS" / "3D Blu-ray to Vision Pro"
    worker_launcher = destination_contents / "MacOS" / WORKER_EXECUTABLE_NAME
    shutil.copy2(source_launcher, worker_launcher)
    worker_launcher.chmod(worker_launcher.stat().st_mode | 0o111)

    info_path = destination_contents / "Info.plist"
    with info_path.open("rb") as info_file:
        info = plistlib.load(info_file)
    info["MainModule"] = "bd_to_avp.worker"
    info["BluRayToVisionProEngineBundled"] = True
    with info_path.open("wb") as info_file:
        plistlib.dump(info, info_file, sort_keys=True)

    strip_native_executable(PACKAGED_APP)
    verify_layout(PACKAGED_APP, environment=os.environ)
    return PACKAGED_APP


def copy_tree(source: Path, destination: Path) -> None:
    if not source.is_dir():
        raise RuntimeError(f"Required runtime directory is missing: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, symlinks=True, dirs_exist_ok=True)


def verify_layout(app_path: Path, *, environment: Mapping[str, str] | None = None) -> None:
    verify_product_identity(app_path, environment=environment)
    native_executable = app_path / "Contents" / "MacOS" / NATIVE_EXECUTABLE_NAME
    worker_executable = app_path / "Contents" / "MacOS" / WORKER_EXECUTABLE_NAME
    ffprobe_executable = app_path / "Contents" / "Resources" / "app" / "bd_to_avp" / "bin" / "ffprobe"
    mv_hevc_encoder = app_path / "Contents" / "Resources" / "app" / "bd_to_avp" / "bin" / MV_HEVC_ENCODER_NAME
    required_paths = [
        native_executable,
        worker_executable,
        app_path / "Contents" / "Frameworks" / "Python.framework" / "Python",
        app_path / "Contents" / "Resources" / "app" / "bd_to_avp" / "worker" / "__main__.py",
        app_path / "Contents" / "Resources" / "app" / "bd_to_avp" / "resources" / "iso639_languages.json",
        app_path / "Contents" / "Resources" / "app_packages",
        app_path / "Contents" / "Resources" / "app_icon.icns",
        ffprobe_executable,
        mv_hevc_encoder,
    ]
    missing = [path for path in required_paths if not path.exists()]
    if missing:
        raise RuntimeError("Packaged macOS app is missing:\n" + "\n".join(str(path) for path in missing))
    for executable in (native_executable, worker_executable, ffprobe_executable, mv_hevc_encoder):
        if executable_architectures(executable) != {"arm64"}:
            raise RuntimeError(f"Packaged executable must be arm64-only: {executable}")
    verify_native_framework_links(native_executable)
    verify_mach_o_minimum_system_versions(app_path, native_executable)
    verify_exact_minimum_system_version(mv_hevc_encoder, "MV-HEVC encoder")
    verify_native_binary_paths(native_executable)
    verify_package_paths(app_path)


def verify_product_identity(app_path: Path, *, environment: Mapping[str, str] | None = None) -> None:
    if app_path.name != NATIVE_APP_NAME:
        raise RuntimeError(f"macOS app must use the product name: {NATIVE_APP_NAME}")

    info_path = app_path / "Contents" / "Info.plist"
    if not info_path.is_file():
        raise RuntimeError(f"macOS app Info.plist is missing: {info_path}")
    with info_path.open("rb") as info_file:
        info = plistlib.load(info_file)

    expected_values = {
        "CFBundleDisplayName": NATIVE_PRODUCT_NAME,
        "CFBundleName": NATIVE_PRODUCT_NAME,
        "CFBundleExecutable": NATIVE_EXECUTABLE_NAME,
        "CFBundleIdentifier": NATIVE_BUNDLE_IDENTIFIER,
        "CFBundleShortVersionString": NATIVE_SHORT_VERSION,
        "CFBundleVersion": NATIVE_BUILD_VERSION,
        "LSMinimumSystemVersion": NATIVE_MINIMUM_SYSTEM_VERSION,
        "MainModule": "bd_to_avp.worker",
        "BluRayToVisionProEngineBundled": True,
    }
    mismatches = [
        f"{key}: expected {expected!r}, found {info.get(key)!r}"
        for key, expected in expected_values.items()
        if info.get(key) != expected
    ]
    try:
        verify_support_diagnostics_endpoint(info, environment=environment)
    except ValueError as error:
        mismatches.append(str(error))
    development_keys = [key for key in info if "DevelopmentRepositoryRoot" in key]
    if development_keys:
        mismatches.append("development repository metadata is present")
    for key in (
        "BDToAVPDistributionChannel",
        "SUFeedURL",
        "SUPublicEDKey",
        "SUAllowsAutomaticUpdates",
        "SUVerifyUpdateBeforeExtraction",
    ):
        expected = NATIVE_UPDATE_INFO[key]
        if info.get(key) != expected:
            mismatches.append(f"{key}: expected {expected!r}, found {info.get(key)!r}")
    if "SUEnableAutomaticChecks" in info:
        mismatches.append("SUEnableAutomaticChecks must remain unset")

    internal_documents = [
        app_path / "Contents" / "Resources" / "app" / "README.md",
        app_path / "Contents" / "Resources" / "app" / "pyproject.toml",
    ]
    if any(path.exists() for path in internal_documents):
        mismatches.append("repository-only documents are present")

    serialized_info = json.dumps(info, default=str)
    path_names = "\n".join(path.name for path in app_path.rglob("*"))
    for marker in BANNED_RELEASE_IDENTIFIERS:
        if marker in serialized_info or marker in path_names:
            mismatches.append(f"release identifier contains {marker!r}")

    if mismatches:
        raise RuntimeError("macOS app identity validation failed:\n" + "\n".join(mismatches))


def executable_architectures(path: Path) -> set[str]:
    completed = subprocess.run(
        ["lipo", "-archs", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return set(completed.stdout.split())


def linked_libraries(path: Path) -> set[str]:
    completed = subprocess.run(
        ["otool", "-L", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return {
        line.strip().split(" (compatibility version", maxsplit=1)[0]
        for line in completed.stdout.splitlines()
        if line.strip() and not line.strip().endswith(":")
    }


def verify_native_framework_links(native_executable: Path) -> None:
    missing = sorted(REQUIRED_NATIVE_FRAMEWORK_LINKS - linked_libraries(native_executable))
    if missing:
        raise RuntimeError("macOS app is missing required framework links:\n" + "\n".join(missing))


def minimum_macos_versions(path: Path) -> set[str]:
    completed = subprocess.run(
        ["vtool", "-show-build", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    versions = set(re.findall(r"^\s*minos\s+(\d+(?:\.\d+){1,2})\s*$", completed.stdout, re.MULTILINE))
    if not versions:
        raise RuntimeError(f"Packaged Mach-O does not declare a minimum macOS version: {path}")
    return versions


def verify_mach_o_minimum_system_versions(app_path: Path, native_executable: Path) -> None:
    expected_version = normalized_version(NATIVE_MINIMUM_SYSTEM_VERSION)
    verify_exact_minimum_system_version(native_executable, "Swift executable")

    incompatible: list[str] = []
    for path in sorted(app_path.rglob("*")):
        if path == native_executable or path.suffix in {".a", ".o"} or not is_mach_o(path):
            continue
        versions = minimum_macos_versions(path)
        newer_versions = sorted(version for version in versions if normalized_version(version) > expected_version)
        if newer_versions:
            incompatible.append(f"{path.relative_to(app_path)}: {', '.join(newer_versions)}")
    if incompatible:
        raise RuntimeError(
            "Packaged Mach-O requires a newer macOS version than "
            f"{NATIVE_MINIMUM_SYSTEM_VERSION}:\n" + "\n".join(incompatible)
        )


def verify_exact_minimum_system_version(path: Path, description: str) -> None:
    expected_version = normalized_version(NATIVE_MINIMUM_SYSTEM_VERSION)
    versions = minimum_macos_versions(path)
    if {normalized_version(version) for version in versions} != {expected_version}:
        found = ", ".join(sorted(versions))
        raise RuntimeError(f"{description} must target macOS {NATIVE_MINIMUM_SYSTEM_VERSION}; found {found}: {path}")


def normalized_version(version: str) -> tuple[int, int, int]:
    if re.fullmatch(r"\d+(?:\.\d+){1,2}", version) is None:
        raise ValueError(f"Invalid macOS version: {version!r}")
    components = [int(component) for component in version.split(".")]
    components.extend([0] * (3 - len(components)))
    return components[0], components[1], components[2]


def verify_native_binary_paths(native_executable: Path) -> None:
    executable_bytes = native_executable.read_bytes()
    if os.fsencode(REPO_ROOT) in executable_bytes:
        raise RuntimeError("macOS Release executable contains the development repository path.")


def verify_package_paths(app_path: Path) -> None:
    development_path = os.fsencode(REPO_ROOT)
    leaked_paths = [
        path.relative_to(app_path)
        for path in app_path.rglob("*")
        if path.is_file() and not path.is_symlink() and file_contains(path, development_path)
    ]
    if leaked_paths:
        leaked = "\n".join(str(path) for path in leaked_paths[:20])
        raise RuntimeError(f"Packaged app contains development repository paths:\n{leaked}")


def file_contains(path: Path, needle: bytes) -> bool:
    overlap = b""
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            data = overlap + chunk
            if needle in data:
                return True
            overlap = data[-(len(needle) - 1) :] if len(needle) > 1 else b""
    return False


def strip_native_executable(app_path: Path) -> None:
    native_executable = app_path / "Contents" / "MacOS" / NATIVE_EXECUTABLE_NAME
    run(["strip", "-S", str(native_executable)])


def is_mach_o(path: Path) -> bool:
    if path.is_symlink() or not path.is_file() or any(parent.suffix == ".dSYM" for parent in path.parents):
        return False
    try:
        with path.open("rb") as candidate:
            return candidate.read(4) in MACH_O_MAGICS
    except OSError:
        return False


def sign_package(app_path: Path, identity: str, keychain: str | None = None) -> None:
    sign_options = ["codesign", "--force", "--sign", identity]
    if identity != "-":
        sign_options.extend(["--options", "runtime"])
    if keychain:
        sign_options.extend(["--keychain", keychain])
    sign_options.append("--timestamp=none" if identity == "-" else "--timestamp")

    contents = app_path / "Contents"
    top_level_macos = contents / "MacOS"
    mach_o_files = sorted(
        (path for path in contents.rglob("*") if is_mach_o(path) and path.parent != top_level_macos),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for path in mach_o_files:
        run([*sign_options, str(path)])

    nested_bundles = sorted(
        (path for path in contents.rglob("*") if path.is_dir() and path.suffix in {".framework", ".app", ".xpc"}),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for path in nested_bundles:
        run([*sign_options, str(path)])

    worker_path = app_path / "Contents" / "MacOS" / WORKER_EXECUTABLE_NAME
    run([*sign_options, "--entitlements", str(WORKER_ENTITLEMENTS), str(worker_path)])
    run([*sign_options, str(app_path)])
    verify_codesign(app_path)


def verify_codesign(app_path: Path) -> None:
    run(["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app_path)])


def smoke_packaged_native_app(app_path: Path) -> None:
    from scripts.smoke_release_app import build_clean_env, read_bundle, verify_preview_presentation

    app_path = app_path.resolve()
    native_executable = app_path / "Contents" / "MacOS" / NATIVE_EXECUTABLE_NAME
    run([str(native_executable), "--startup-smoke"], cwd=app_path, timeout=20)
    verify_preview_presentation(read_bundle(app_path), build_clean_env())


def smoke_packaged_mv_hevc_encoder(app_path: Path) -> None:
    app_path = app_path.resolve()
    encoder = app_path / "Contents" / "Resources" / "app" / "bd_to_avp" / "bin" / MV_HEVC_ENCODER_NAME
    completed = subprocess.run(
        [str(encoder), "--capability-probe"],
        cwd=app_path,
        capture_output=True,
        env=smoke_environment(),
        text=True,
        timeout=30,
    )
    supported = validate_mv_hevc_capability_probe(completed, description="Packaged MV-HEVC encoder")
    if not supported:
        print("Packaged MV-HEVC encoder is valid but unavailable on this build host.")


def validate_mv_hevc_capability_probe(
    completed: subprocess.CompletedProcess[str],
    *,
    description: str,
) -> bool:
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"{description} capability probe returned invalid JSON.\n"
            f"exit: {completed.returncode}\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        ) from error
    required_keys = {"schema_version", "stereo_mv_hevc_encode_supported"}
    optional_keys = {
        "metalfx_2x_mv_hevc_supported",
        "metalfx_spatial_scaling_supported",
        "pixel_transfer_2x_mv_hevc_supported",
    }
    if (
        not isinstance(payload, dict)
        or not required_keys.issubset(payload)
        or not set(payload).issubset(required_keys | optional_keys)
        or type(payload["schema_version"]) is not int
        or payload["schema_version"] != 1
        or type(payload["stereo_mv_hevc_encode_supported"]) is not bool
        or any(type(payload[key]) is not bool for key in optional_keys if key in payload)
    ):
        raise RuntimeError(
            f"{description} capability probe failed.\n"
            f"exit: {completed.returncode}\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )

    supported = payload["stereo_mv_hevc_encode_supported"]
    metalfx_supported = payload.get("metalfx_2x_mv_hevc_supported", False)
    metalfx_device_supported = payload.get("metalfx_spatial_scaling_supported", False)
    pixel_transfer_supported = payload.get("pixel_transfer_2x_mv_hevc_supported", False)
    contract_is_consistent = not (
        (metalfx_supported and (not supported or not metalfx_device_supported))
        or (pixel_transfer_supported and not supported)
    )
    expected_returncode = 0 if supported else 2
    if contract_is_consistent and completed.returncode == expected_returncode:
        return supported
    raise RuntimeError(
        f"{description} capability probe failed.\n"
        f"exit: {completed.returncode}\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )


def smoke_packaged_worker(app_path: Path) -> None:
    app_path = app_path.resolve()
    contents = app_path / "Contents"
    ffmpeg_path = contents / "Resources" / "app" / "bd_to_avp" / "bin" / "ffmpeg"
    worker_path = contents / "MacOS" / WORKER_EXECUTABLE_NAME

    with tempfile.TemporaryDirectory(prefix="bd-to-avp-native-smoke-") as temporary_directory:
        source_path = Path(temporary_directory) / "smoke.m2ts"
        run(
            [
                str(ffmpeg_path),
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=160x90:rate=24",
                "-t",
                "0.25",
                "-c:v",
                "mpeg2video",
                "-f",
                "mpegts",
                str(source_path),
            ]
        )
        job_id = str(uuid4())
        request = {
            "protocol_version": WORKER_PROTOCOL_VERSION,
            "type": "job.start",
            "job_id": job_id,
            "operation": "inspect_source",
            "source": {"kind": "direct_file", "path": str(source_path)},
        }
        completed = subprocess.run(
            [str(worker_path)],
            cwd=app_path,
            input=json.dumps(request) + "\n",
            capture_output=True,
            env=smoke_environment(),
            text=True,
            timeout=30,
        )
        events: list[object] = [json.loads(line) for line in completed.stdout.splitlines()]
        try:
            validate_smoke_events(events, job_id)
        except (TypeError, ValueError) as error:
            raise RuntimeError(
                "Packaged worker smoke failed.\n"
                f"exit: {completed.returncode}\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
            ) from error
        if completed.returncode != 0:
            raise RuntimeError(
                "Packaged worker smoke failed.\n"
                f"exit: {completed.returncode}\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
            )


def smoke_environment() -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith(("PYTHON", "BD_TO_AVP_", "DYLD_"))
    }
    environment["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
    return environment


def validate_smoke_events(events: list[object], job_id: str) -> None:
    if len(events) < 4 or not all(isinstance(event, Mapping) for event in events):
        raise ValueError("Worker smoke did not return the required event stream.")
    typed_events = [cast(Mapping[str, Any], event) for event in events]
    if [event.get("sequence") for event in typed_events] != list(range(len(typed_events))):
        raise ValueError("Worker smoke event sequence was not contiguous.")
    if any(event.get("protocol_version") != WORKER_PROTOCOL_VERSION for event in typed_events):
        raise ValueError(f"Worker smoke protocol version did not match v{WORKER_PROTOCOL_VERSION}.")
    if any(event.get("job_id") != job_id for event in typed_events):
        raise ValueError("Worker smoke returned an event for another job.")
    event_types = [event.get("type") for event in typed_events]
    if event_types[:3] != ["worker.ready", "job.started", "stage.started"]:
        raise ValueError("Worker smoke lifecycle prefix was incomplete.")
    if event_types[-1] != "job.completed":
        raise ValueError("Worker smoke did not complete.")

    observed_ffprobe = False
    for event in typed_events:
        if event.get("type") != "observability":
            continue
        payload = event.get("payload")
        canonical_event = payload.get("event") if isinstance(payload, Mapping) else None
        if not isinstance(canonical_event, Mapping):
            raise ValueError("Worker smoke observability payload was missing.")
        try:
            parsed_event = ObservabilityEvent.from_dict(dict(canonical_event))
        except (KeyError, OverflowError, TypeError, ValueError) as error:
            raise ValueError("Worker smoke emitted invalid canonical observability.") from error
        tool = parsed_event.context.tool
        observed_ffprobe = observed_ffprobe or (
            parsed_event.kind in {"tool.started", "tool.completed"} and tool is not None and tool.id == "ffprobe"
        )
    if not observed_ffprobe:
        raise ValueError("Worker smoke did not emit canonical FFprobe observability.")

    terminal_payload = typed_events[-1].get("payload")
    if not isinstance(terminal_payload, Mapping):
        raise TypeError("Worker smoke completion payload was missing.")
    result = terminal_payload.get("result")
    if not isinstance(result, Mapping):
        raise TypeError("Worker smoke completion result was missing.")
    if result.get("resolution") != "160x90" or result.get("frame_rate") != "24/1":
        raise ValueError("Worker smoke returned unexpected media metadata.")
    if result.get("interlaced") is not False or not isinstance(result.get("size_bytes"), int):
        raise ValueError("Worker smoke returned an invalid result shape.")


def package(identity: str, keychain: str | None = None) -> Path:
    mv_hevc_encoder_path = build_packaged_mv_hevc_encoder()
    prepare_briefcase_runtime()
    xcodebuild("build", NATIVE_PACKAGE_CONFIGURATION)
    app_path = assemble_package(mv_hevc_encoder_path)
    sign_package(app_path, identity, keychain)
    smoke_packaged_native_app(app_path)
    smoke_packaged_mv_hevc_encoder(app_path)
    smoke_packaged_worker(app_path)
    verify_codesign(app_path)
    print(app_path)
    return app_path


def git_output(*arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        check=True,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    value = result.stdout.strip()
    if not value:
        raise RuntimeError(f"Git returned no value for: {' '.join(arguments)}")
    return value


def ensure_clean_worktree() -> None:
    result = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=normal"],
        check=True,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if result.stdout.strip():
        raise RuntimeError("Publishing the current app requires a clean Git worktree.")


def validate_full_git_sha(value: str, description: str) -> str:
    if re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise ValueError(f"{description} must be a full lowercase Git SHA.")
    return value


def bundle_versions(app_path: Path) -> tuple[str, str]:
    info_path = app_path / "Contents" / "Info.plist"
    with info_path.open("rb") as info_file:
        info = plistlib.load(info_file)
    short_version = info.get("CFBundleShortVersionString")
    build_version = info.get("CFBundleVersion")
    if not isinstance(short_version, str) or not isinstance(build_version, str):
        raise RuntimeError("Packaged app is missing version identity.")
    return short_version, build_version


def require_replaceable_symlink(path: Path) -> None:
    if os.path.lexists(path) and not path.is_symlink():
        raise RuntimeError(f"Refusing to replace non-symlink path: {path}")


def replace_symlink(target: Path, link_path: Path) -> None:
    require_replaceable_symlink(link_path)
    temporary_link = link_path.with_name(f".{link_path.name}.{uuid4().hex}.tmp")
    try:
        os.symlink(target, temporary_link)
        require_replaceable_symlink(link_path)
        os.replace(temporary_link, link_path)
    finally:
        temporary_link.unlink(missing_ok=True)


def require_real_directory(path: Path, description: str) -> None:
    try:
        status = path.lstat()
    except FileNotFoundError as error:
        raise RuntimeError(f"{description} is missing: {path}") from error
    if not stat.S_ISDIR(status.st_mode):
        raise RuntimeError(f"{description} must be a real directory: {path}")


def require_real_file(path: Path, description: str) -> None:
    try:
        status = path.lstat()
    except FileNotFoundError as error:
        raise RuntimeError(f"{description} is missing: {path}") from error
    if not stat.S_ISREG(status.st_mode):
        raise RuntimeError(f"{description} must be a real file: {path}")


def validate_app_symlinks(app_path: Path) -> None:
    app_root = app_path.resolve()
    for directory, directory_names, file_names in os.walk(app_root, followlinks=False):
        parent = Path(directory)
        for name in (*directory_names, *file_names):
            path = parent / name
            if not path.is_symlink():
                continue
            try:
                target = path.resolve(strict=True)
            except (OSError, RuntimeError) as error:
                raise RuntimeError(f"App bundle contains a broken symlink: {path}") from error
            if not target.is_relative_to(app_root):
                raise RuntimeError(f"App bundle symlink escapes the bundle: {path}")


def validate_built_at_utc(value: object, metadata_path: Path) -> str:
    if not isinstance(value, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value) is None:
        raise RuntimeError(f"Existing build metadata has an invalid UTC build time: {metadata_path}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise RuntimeError(f"Existing build metadata has an invalid UTC build time: {metadata_path}") from error
    normalized = parsed.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    if normalized != value:
        raise RuntimeError(f"Existing build metadata has an invalid UTC build time: {metadata_path}")
    return value


def prepare_applications_directory(path: Path) -> Path:
    requested_path = path.expanduser()
    if os.path.lexists(requested_path) and requested_path.is_symlink():
        raise RuntimeError(f"Applications destination must not be a symlink: {requested_path}")
    resolved_path = requested_path.resolve(strict=False)
    if resolved_path == Path("/Applications"):
        raise RuntimeError("Refusing to publish a local current build into /Applications.")
    requested_path.mkdir(parents=True, exist_ok=True)
    resolved_path = requested_path.resolve()
    require_real_directory(resolved_path, "Applications destination")
    return resolved_path


@contextmanager
def current_publish_lock(applications_directory: Path) -> Iterator[None]:
    lock_path = applications_directory / CURRENT_PUBLISH_LOCK_NAME
    if os.path.lexists(lock_path) and lock_path.is_symlink():
        raise RuntimeError(f"Current-build lock must not be a symlink: {lock_path}")
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as error:
        raise RuntimeError(f"Unable to open current-build lock: {lock_path}") from error
    with os.fdopen(descriptor, "r+") as lock_file:
        if not stat.S_ISREG(os.fstat(lock_file.fileno()).st_mode):
            raise RuntimeError(f"Current-build lock must be a real file: {lock_path}")
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        yield


def resolve_symlink(path: Path, description: str) -> Path:
    if not path.is_symlink():
        raise RuntimeError(f"{description} must be a symlink: {path}")
    try:
        return path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise RuntimeError(f"{description} is broken: {path}") from error


def validate_version_directory(path: Path, build_root: Path, description: str) -> Path:
    resolved_path = path.resolve(strict=True)
    require_real_directory(resolved_path, description)
    if resolved_path.parent != build_root or re.fullmatch(r"[0-9a-f]{40}", resolved_path.name) is None:
        raise RuntimeError(f"{description} is outside the immutable build root: {resolved_path}")
    return resolved_path


def publish_stable_current_links(
    *,
    build_root: Path,
    version_directory: Path,
    stable_app: Path,
    stable_metadata: Path,
) -> None:
    current_pointer = stable_app.parent / CURRENT_POINTER_LINK_NAME
    require_replaceable_symlink(current_pointer)
    require_replaceable_symlink(stable_app)
    require_replaceable_symlink(stable_metadata)

    app_route = Path(CURRENT_POINTER_LINK_NAME) / NATIVE_APP_NAME
    metadata_route = Path(CURRENT_POINTER_LINK_NAME) / CURRENT_BUILD_METADATA_NAME
    version_route = Path(CURRENT_BUILD_DIRECTORY_NAME) / version_directory.name
    pointer_directory: Path

    if os.path.lexists(current_pointer):
        pointer_directory = validate_version_directory(
            resolve_symlink(current_pointer, "Current-build pointer"),
            build_root,
            "Current-build pointer target",
        )
    elif not os.path.lexists(stable_app) and not os.path.lexists(stable_metadata):
        replace_symlink(version_route, current_pointer)
        pointer_directory = version_directory
    elif os.path.lexists(stable_app) and os.path.lexists(stable_metadata):
        if os.readlink(stable_app) == str(app_route) and os.readlink(stable_metadata) == str(metadata_route):
            replace_symlink(version_route, current_pointer)
            pointer_directory = version_directory
        else:
            existing_app = resolve_symlink(stable_app, "Stable current app")
            existing_metadata = resolve_symlink(stable_metadata, "Stable current metadata")
            if (
                existing_app.name != NATIVE_APP_NAME
                or existing_metadata.name != CURRENT_BUILD_METADATA_NAME
                or existing_app.parent != existing_metadata.parent
            ):
                raise RuntimeError("Existing stable current links do not identify one immutable build.")
            pointer_directory = validate_version_directory(
                existing_app.parent,
                build_root,
                "Existing stable current build",
            )
            replace_symlink(
                Path(CURRENT_BUILD_DIRECTORY_NAME) / pointer_directory.name,
                current_pointer,
            )
    else:
        raise RuntimeError("Existing stable current links are incomplete.")

    for path, route, expected_target in (
        (stable_metadata, metadata_route, pointer_directory / CURRENT_BUILD_METADATA_NAME),
        (stable_app, app_route, pointer_directory / NATIVE_APP_NAME),
    ):
        if os.path.lexists(path) and os.readlink(path) != str(route):
            if resolve_symlink(path, f"Stable current path {path.name}") != expected_target:
                raise RuntimeError(f"Existing stable current path disagrees with the current pointer: {path}")
        if not os.path.lexists(path) or os.readlink(path) != str(route):
            replace_symlink(route, path)

    if stable_app.resolve(strict=True) != pointer_directory / NATIVE_APP_NAME:
        raise RuntimeError("Stable current app did not resolve through the current-build pointer.")
    if stable_metadata.resolve(strict=True) != pointer_directory / CURRENT_BUILD_METADATA_NAME:
        raise RuntimeError("Stable current metadata did not resolve through the current-build pointer.")

    if pointer_directory != version_directory:
        replace_symlink(version_route, current_pointer)
    if stable_app.resolve(strict=True) != version_directory / NATIVE_APP_NAME:
        raise RuntimeError("Stable current app did not switch to the published build.")
    if stable_metadata.resolve(strict=True) != version_directory / CURRENT_BUILD_METADATA_NAME:
        raise RuntimeError("Stable current metadata did not switch to the published build.")


def publish_current_app(
    app_path: Path,
    *,
    applications_directory: Path,
    source_commit: str,
    base_main_commit: str,
    built_at: datetime | None = None,
) -> Path:
    source_commit = validate_full_git_sha(source_commit, "Source commit")
    base_main_commit = validate_full_git_sha(base_main_commit, "Base main commit")
    require_real_directory(app_path, "Packaged app")
    validate_app_symlinks(app_path)

    applications_directory = prepare_applications_directory(applications_directory)
    stable_app = applications_directory / CURRENT_APP_LINK_NAME
    stable_metadata = applications_directory / CURRENT_METADATA_LINK_NAME

    verify_codesign(app_path)
    source_app_hash = app_tree_sha256(app_path)
    short_version, build_version = bundle_versions(app_path)
    build_time = built_at or datetime.now(UTC)
    if build_time.utcoffset() is None:
        raise ValueError("Build time must include a timezone.")
    built_at_utc = build_time.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")

    build_root = applications_directory / CURRENT_BUILD_DIRECTORY_NAME
    version_directory = build_root / source_commit
    versioned_app = version_directory / NATIVE_APP_NAME
    metadata_path = version_directory / CURRENT_BUILD_METADATA_NAME
    metadata = {
        "app_tree_sha256": source_app_hash,
        "base_main_commit": base_main_commit,
        "build_version": build_version,
        "built_at_utc": built_at_utc,
        "release_status": "not a production-signed release",
        "short_version": short_version,
        "signing": "ad-hoc local",
        "source_commit": source_commit,
        "stable_path": str(stable_app),
    }

    with current_publish_lock(applications_directory):
        require_replaceable_symlink(applications_directory / CURRENT_POINTER_LINK_NAME)
        require_replaceable_symlink(stable_app)
        require_replaceable_symlink(stable_metadata)
        if os.path.lexists(build_root):
            require_real_directory(build_root, "Current-build root")
        else:
            build_root.mkdir()
        if os.path.lexists(version_directory):
            require_real_directory(version_directory, "Existing current-build directory")
            require_real_directory(versioned_app, "Existing current-build app")
            require_real_file(metadata_path, "Existing current-build metadata")
            validate_app_symlinks(versioned_app)
            verify_codesign(versioned_app)
            if app_tree_sha256(versioned_app) != source_app_hash:
                raise RuntimeError(f"Existing build for {source_commit} has a different app tree.")
            try:
                existing_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise RuntimeError(f"Existing build metadata is invalid: {metadata_path}") from error
            if not isinstance(existing_metadata, dict) or set(existing_metadata) != set(metadata):
                raise RuntimeError(f"Existing build metadata has an unexpected schema: {metadata_path}")
            existing_built_at = validate_built_at_utc(existing_metadata["built_at_utc"], metadata_path)
            expected_metadata = {**metadata, "built_at_utc": existing_built_at}
            if existing_metadata != expected_metadata:
                mismatched_key = next(
                    key for key, expected_value in expected_metadata.items() if existing_metadata[key] != expected_value
                )
                raise RuntimeError(f"Existing build metadata does not match {mismatched_key}: {metadata_path}")
        else:
            temporary_directory = Path(tempfile.mkdtemp(prefix=f".{source_commit}.", dir=build_root))
            try:
                temporary_app = temporary_directory / NATIVE_APP_NAME
                shutil.copytree(app_path, temporary_app, symlinks=True)
                require_real_directory(temporary_app, "Copied current-build app")
                validate_app_symlinks(temporary_app)
                verify_codesign(temporary_app)
                if app_tree_sha256(temporary_app) != source_app_hash:
                    raise RuntimeError("Copied current app does not match the packaged app tree.")
                (temporary_directory / CURRENT_BUILD_METADATA_NAME).write_text(
                    json.dumps(metadata, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                if os.path.lexists(version_directory):
                    raise RuntimeError(f"Current-build destination appeared during publication: {version_directory}")
                os.replace(temporary_directory, version_directory)
            finally:
                shutil.rmtree(temporary_directory, ignore_errors=True)

        publish_stable_current_links(
            build_root=build_root,
            version_directory=version_directory,
            stable_app=stable_app,
            stable_metadata=stable_metadata,
        )
    return stable_app


def publish_current(applications_directory: Path | None = None) -> Path:
    ensure_clean_worktree()
    source_commit = git_output("rev-parse", "HEAD")
    base_main_commit = git_output("merge-base", "HEAD", "origin/main")
    app_path = package("-")
    ensure_clean_worktree()
    packaged_source_commit = git_output("rev-parse", "HEAD")
    if packaged_source_commit != source_commit:
        raise RuntimeError("Git HEAD changed while packaging the current app.")
    packaged_base_main_commit = git_output("merge-base", "HEAD", "origin/main")
    if packaged_base_main_commit != base_main_commit:
        raise RuntimeError("The origin/main merge base changed while packaging the current app.")
    stable_app = publish_current_app(
        app_path,
        applications_directory=applications_directory or Path.home() / "Applications",
        source_commit=source_commit,
        base_main_commit=base_main_commit,
    )
    print(f"Published current app: {stable_app}")
    return stable_app


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build and package the macOS application.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("generate", help="Generate the Xcode project from macos/project.yml.")
    commands.add_parser("test", help="Run the macOS application unit tests.")
    commands.add_parser("build", help="Build the macOS Development app without embedding Python.")
    commands.add_parser(
        "publish-current",
        help="Package and publish an immutable local app behind a stable Current.app link.",
        description=(
            "Package an ad-hoc local app and publish it behind a stable Current.app link. "
            f"Requires {SUPPORT_DIAGNOSTICS_ENDPOINT_ENV}."
        ),
    )
    package_parser = commands.add_parser("package", help="Build, embed, sign, and smoke the Python worker.")
    package_parser.add_argument(
        "--sign-identity",
        default=os.environ.get("BD_TO_AVP_NATIVE_SIGN_IDENTITY", "-"),
        help="codesign identity; defaults to ad-hoc signing (-).",
    )
    package_parser.add_argument(
        "--sign-keychain",
        default=os.environ.get("BD_TO_AVP_NATIVE_SIGN_KEYCHAIN"),
        help="keychain containing the signing identity.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    if args.command == "generate":
        generate_project()
    elif args.command == "test":
        xcodebuild("test", "Debug")
    elif args.command == "build":
        xcodebuild("build", "Debug")
    elif args.command == "publish-current":
        publish_current()
    elif args.command == "package":
        package(args.sign_identity, args.sign_keychain)


if __name__ == "__main__":
    main()
