from __future__ import annotations

import argparse
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile

from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

REPO_ROOT = Path(__file__).resolve().parents[1]
MACOS_ROOT = REPO_ROOT / "macos"
PROJECT_SPEC = MACOS_ROOT / "project.yml"
NATIVE_PROJECT_NAME = "BluRayToVisionPro"
NATIVE_PRODUCT_NAME = "3D Blu-ray to Vision Pro"
NATIVE_BUNDLE_IDENTIFIER = "com.shinycomputers.bd-to-avp"
NATIVE_EXECUTABLE_NAME = NATIVE_PRODUCT_NAME
PROJECT_PATH = MACOS_ROOT / f"{NATIVE_PROJECT_NAME}.xcodeproj"
SCHEME = NATIVE_PROJECT_NAME
DERIVED_DATA = MACOS_ROOT / "build" / "DerivedData"
NATIVE_APP_NAME = f"{NATIVE_PRODUCT_NAME}.app"
BRIEFCASE_APP = REPO_ROOT / "build" / "bd-to-avp" / "macos" / "app" / "3D Blu-ray to Vision Pro.app"
PACKAGE_ROOT = MACOS_ROOT / "build" / "package"
PACKAGED_APP = PACKAGE_ROOT / NATIVE_APP_NAME
WORKER_EXECUTABLE_NAME = "BluRayToVisionProEngine"
WORKER_ENTITLEMENTS = MACOS_ROOT / "BluRayToVisionPro" / "Worker.entitlements"
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
    "native-prototype",
    "Native worker prototype",
    "Protocol v1",
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


def run(
    command: list[str],
    *,
    cwd: Path = REPO_ROOT,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=True,
        cwd=cwd,
        env=environment,
        text=True,
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
        raise RuntimeError("User-facing native copy contains internal terminology:\n" + "\n".join(violations))


def xcodebuild(action: str, configuration: str) -> None:
    generate_project()
    build_settings = ["CODE_SIGNING_ALLOWED=NO"]
    if configuration == "Release":
        build_settings.extend(["ARCHS=arm64", "ENABLE_CODE_COVERAGE=NO", "ONLY_ACTIVE_ARCH=NO"])
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


def prepare_briefcase_runtime() -> None:
    command = "update" if BRIEFCASE_APP.is_dir() else "create"
    run([sys.executable, "-m", "scripts.briefcase_app", command, "--no-input"])
    run([sys.executable, "-m", "scripts.briefcase_app", "build", "--no-input"])
    if not BRIEFCASE_APP.is_dir():
        raise RuntimeError(f"Briefcase did not create the expected runtime at {BRIEFCASE_APP}")


def assemble_package() -> Path:
    source_app = DERIVED_DATA / "Build" / "Products" / "Release" / NATIVE_APP_NAME
    if not source_app.is_dir():
        raise RuntimeError(f"Native build product is missing at {source_app}")

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
    verify_layout(PACKAGED_APP)
    return PACKAGED_APP


def copy_tree(source: Path, destination: Path) -> None:
    if not source.is_dir():
        raise RuntimeError(f"Required runtime directory is missing: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, symlinks=True, dirs_exist_ok=True)


def verify_layout(app_path: Path) -> None:
    verify_product_identity(app_path)
    native_executable = app_path / "Contents" / "MacOS" / NATIVE_EXECUTABLE_NAME
    worker_executable = app_path / "Contents" / "MacOS" / WORKER_EXECUTABLE_NAME
    ffprobe_executable = app_path / "Contents" / "Resources" / "app" / "bd_to_avp" / "bin" / "ffprobe"
    required_paths = [
        native_executable,
        worker_executable,
        app_path / "Contents" / "Frameworks" / "Python.framework" / "Python",
        app_path / "Contents" / "Resources" / "app" / "bd_to_avp" / "worker" / "__main__.py",
        app_path / "Contents" / "Resources" / "app_packages",
        app_path / "Contents" / "Resources" / "app_icon.icns",
        ffprobe_executable,
    ]
    missing = [path for path in required_paths if not path.exists()]
    if missing:
        raise RuntimeError("Packaged native app is missing:\n" + "\n".join(str(path) for path in missing))
    for executable in (native_executable, worker_executable, ffprobe_executable):
        if executable_architectures(executable) != {"arm64"}:
            raise RuntimeError(f"Packaged executable must be arm64-only: {executable}")
    verify_native_binary_paths(native_executable)
    verify_package_paths(app_path)


def verify_product_identity(app_path: Path) -> None:
    if app_path.name != NATIVE_APP_NAME:
        raise RuntimeError(f"Native app must use the product name: {NATIVE_APP_NAME}")

    info_path = app_path / "Contents" / "Info.plist"
    if not info_path.is_file():
        raise RuntimeError(f"Native app Info.plist is missing: {info_path}")
    with info_path.open("rb") as info_file:
        info = plistlib.load(info_file)

    expected_values = {
        "CFBundleDisplayName": NATIVE_PRODUCT_NAME,
        "CFBundleName": NATIVE_PRODUCT_NAME,
        "CFBundleExecutable": NATIVE_EXECUTABLE_NAME,
        "CFBundleIdentifier": NATIVE_BUNDLE_IDENTIFIER,
        "MainModule": "bd_to_avp.worker",
        "BluRayToVisionProEngineBundled": True,
    }
    mismatches = [
        f"{key}: expected {expected!r}, found {info.get(key)!r}"
        for key, expected in expected_values.items()
        if info.get(key) != expected
    ]
    development_keys = [key for key in info if "DevelopmentRepositoryRoot" in key]
    if development_keys:
        mismatches.append("development repository metadata is present")

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
        raise RuntimeError("Native app identity validation failed:\n" + "\n".join(mismatches))


def executable_architectures(path: Path) -> set[str]:
    completed = subprocess.run(
        ["lipo", "-archs", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return set(completed.stdout.split())


def verify_native_binary_paths(native_executable: Path) -> None:
    executable_bytes = native_executable.read_bytes()
    if os.fsencode(REPO_ROOT) in executable_bytes:
        raise RuntimeError("Native Release executable contains the development repository path.")


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


def sign_package(app_path: Path, identity: str) -> None:
    sign_options = ["codesign", "--force", "--sign", identity, "--options", "runtime"]
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


def smoke_packaged_worker(app_path: Path) -> None:
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
            "protocol_version": 1,
            "type": "job.start",
            "job_id": job_id,
            "operation": "inspect_source",
            "source": {"path": str(source_path)},
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
    if any(event.get("protocol_version") != 1 for event in typed_events):
        raise ValueError("Worker smoke protocol version did not match v1.")
    if any(event.get("job_id") != job_id for event in typed_events):
        raise ValueError("Worker smoke returned an event for another job.")
    event_types = [event.get("type") for event in typed_events]
    if event_types[:3] != ["worker.ready", "job.started", "stage.started"]:
        raise ValueError("Worker smoke lifecycle prefix was incomplete.")
    if event_types[-1] != "job.completed":
        raise ValueError("Worker smoke did not complete.")

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


def package(identity: str) -> None:
    prepare_briefcase_runtime()
    xcodebuild("build", "Release")
    app_path = assemble_package()
    sign_package(app_path, identity)
    smoke_packaged_worker(app_path)
    verify_codesign(app_path)
    print(app_path)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build and package the native macOS application.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("generate", help="Generate the Xcode project from macos/project.yml.")
    commands.add_parser("test", help="Run the native unit tests.")
    commands.add_parser("build", help="Build the native Debug app without embedding Python.")
    package_parser = commands.add_parser("package", help="Build, embed, sign, and smoke the Python worker.")
    package_parser.add_argument(
        "--sign-identity",
        default=os.environ.get("BD_TO_AVP_NATIVE_SIGN_IDENTITY", "-"),
        help="codesign identity; defaults to ad-hoc signing (-).",
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
    elif args.command == "package":
        package(args.sign_identity)


if __name__ == "__main__":
    main()
