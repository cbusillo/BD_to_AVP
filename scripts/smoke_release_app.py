from __future__ import annotations

import argparse
import os
import plistlib
import re
import shutil
import subprocess
import sys

from dataclasses import dataclass
from pathlib import Path


DEFAULT_APP_PATHS = [
    Path("/Applications/3D Blu-ray to Vision Pro.app"),
    Path("/Applications/Blu-ray to AVP.app"),
]
APP_RESOURCE_APP_PATH = Path("Contents/Resources/app")
APP_BIN_PATH = APP_RESOURCE_APP_PATH / "bd_to_avp" / "bin"
PACKAGE_VERSION_RE = re.compile(r"\bVersion\s+(?P<version>\S+)")
REQUIRED_BUNDLED_TOOLS = {
    "ffmpeg": ["-hide_banner", "-version"],
    "ffprobe": ["-hide_banner", "-version"],
    "edge264_test": ["--help"],
    "MP4Box": ["-version"],
    "spatial-media-kit-tool": ["--help"],
}
OPTIONAL_BUNDLED_TOOLS = {
    "fx-upscale": ["--help"],
}
MINIMAL_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


@dataclass(frozen=True)
class AppBundle:
    path: Path
    executable: Path
    resources_app: Path
    bin_dir: Path
    bundle_identifier: str
    short_version: str


class SmokeFailure(RuntimeError):
    pass


def run(command: list[str | Path], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(item) for item in command],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
    )


def build_clean_env() -> dict[str, str]:
    env = {
        "HOME": os.environ.get("HOME", "/var/empty"),
        "PATH": MINIMAL_PATH,
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        "__CFBundleIdentifier": "com.shinycomputers.bd-to-avp.release-smoke",
    }
    if "SYSTEM_VERSION_COMPAT" in os.environ:
        env["SYSTEM_VERSION_COMPAT"] = os.environ["SYSTEM_VERSION_COMPAT"]
    return env


def find_default_app() -> Path:
    for app_path in DEFAULT_APP_PATHS:
        if app_path.is_dir():
            return app_path
    searched_paths = ", ".join(path.as_posix() for path in DEFAULT_APP_PATHS)
    raise SmokeFailure(f"No default app bundle found. Pass --app-path. Searched: {searched_paths}")


def read_bundle(app_path: Path) -> AppBundle:
    info_plist_path = app_path / "Contents" / "Info.plist"
    if not info_plist_path.is_file():
        raise SmokeFailure(f"Missing app Info.plist: {info_plist_path}")

    with info_plist_path.open("rb") as plist_file:
        info = plistlib.load(plist_file)

    executable_name = require_plist_string(info, "CFBundleExecutable", info_plist_path)
    bundle_identifier = require_plist_string(info, "CFBundleIdentifier", info_plist_path)
    short_version = require_plist_string(info, "CFBundleShortVersionString", info_plist_path)
    executable = app_path / "Contents" / "MacOS" / executable_name
    resources_app = app_path / APP_RESOURCE_APP_PATH
    bin_dir = app_path / APP_BIN_PATH
    return AppBundle(
        path=app_path,
        executable=executable,
        resources_app=resources_app,
        bin_dir=bin_dir,
        bundle_identifier=bundle_identifier,
        short_version=short_version,
    )


def require_plist_string(info: dict[str, object], key: str, plist_path: Path) -> str:
    value = info.get(key)
    if not isinstance(value, str) or not value:
        raise SmokeFailure(f"{plist_path} is missing string key {key}")
    return value


def verify_app_layout(bundle: AppBundle) -> None:
    for path in [bundle.executable, bundle.resources_app, bundle.bin_dir]:
        if not path.exists():
            raise SmokeFailure(f"Missing expected app bundle path: {path}")
    if not os.access(bundle.executable, os.X_OK):
        raise SmokeFailure(f"App executable is not executable: {bundle.executable}")


def verify_gatekeeper(app_path: Path, *, skip_spctl: bool) -> None:
    if skip_spctl:
        return
    try:
        run(["spctl", "--assess", "--type", "execute", "--verbose=4", app_path])
    except (FileNotFoundError, subprocess.CalledProcessError) as error:
        raise SmokeFailure(f"Gatekeeper assessment failed for {app_path}: {error}") from error


def developer_tools_available() -> bool:
    try:
        run(["xcode-select", "-p"])
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False
    return shutil.which("otool", path=MINIMAL_PATH) is not None


def verify_bundled_tool(
    tool_path: Path, probe_args: list[str], *, clean_env: dict[str, str], check_links: bool
) -> None:
    if not tool_path.is_file():
        raise SmokeFailure(f"Missing bundled tool: {tool_path}")
    if not os.access(tool_path, os.X_OK):
        raise SmokeFailure(f"Bundled tool is not executable: {tool_path}")
    try:
        run([tool_path, *probe_args], env=clean_env)
    except (FileNotFoundError, subprocess.CalledProcessError) as error:
        output = getattr(error, "output", "") or ""
        raise SmokeFailure(f"Bundled tool did not run: {tool_path}\n{output.strip()}") from error

    if tool_path.name == "ffmpeg":
        encoders = run([tool_path, "-hide_banner", "-encoders"], env=clean_env).stdout
        if "libsvtav1" not in encoders:
            raise SmokeFailure("Bundled FFmpeg does not expose the required libsvtav1 encoder.")
        bitstream_filters = run([tool_path, "-hide_banner", "-bsfs"], env=clean_env).stdout
        if "av1_metadata" not in bitstream_filters:
            raise SmokeFailure("Bundled FFmpeg does not expose the required av1_metadata bitstream filter.")

    if not check_links:
        return

    try:
        linked_libraries = run(["otool", "-L", tool_path], env=clean_env).stdout
    except (FileNotFoundError, subprocess.CalledProcessError) as error:
        raise SmokeFailure(f"Could not inspect linked libraries for {tool_path}: {error}") from error
    for forbidden_path in ["/opt/homebrew", "/usr/local"]:
        if forbidden_path in linked_libraries:
            raise SmokeFailure(f"Bundled {tool_path.name} links to {forbidden_path}:\n{linked_libraries}")


def verify_bundled_tools(bundle: AppBundle, clean_env: dict[str, str]) -> None:
    check_links = developer_tools_available()
    if not check_links:
        print("Developer tools unavailable: skipping otool linkage checks")

    for tool_name, probe_args in REQUIRED_BUNDLED_TOOLS.items():
        verify_bundled_tool(bundle.bin_dir / tool_name, probe_args, clean_env=clean_env, check_links=check_links)

    for tool_name, probe_args in OPTIONAL_BUNDLED_TOOLS.items():
        tool_path = bundle.bin_dir / tool_name
        if tool_path.exists():
            verify_bundled_tool(tool_path, probe_args, clean_env=clean_env, check_links=check_links)


def verify_no_homebrew_requirement(bundle: AppBundle) -> None:
    homebrew_path = Path("/opt/homebrew")
    if not homebrew_path.exists():
        print("Homebrew absent: /opt/homebrew does not exist")
        return

    print("Homebrew present on this machine; smoke used sanitized PATH to avoid it")


def verify_cli_version(bundle: AppBundle, clean_env: dict[str, str]) -> None:
    output = run([bundle.executable, "--version"], env=clean_env).stdout.strip()
    match = PACKAGE_VERSION_RE.search(output)
    if not match:
        raise SmokeFailure(f"Could not parse app version output: {output}")
    package_version = match.group("version")
    if package_version != bundle.short_version:
        raise SmokeFailure(
            f"App version mismatch: Info.plist has {bundle.short_version}, CLI reported {package_version}"
        )
    print(f"CLI version smoke passed: {package_version}")


def verify_cli_help(bundle: AppBundle, clean_env: dict[str, str]) -> None:
    output = run([bundle.executable, "--help"], env=clean_env).stdout
    if "Process 3D Blu-ray" not in output or "--source" not in output:
        raise SmokeFailure("CLI help output did not include expected BD_to_AVP options")


def verify_apple_vision_ocr(bundle: AppBundle, clean_env: dict[str, str]) -> None:
    output = run([bundle.executable, "--smoke-apple-vision-ocr"], env=clean_env).stdout
    if "Apple Vision OCR import smoke passed" not in output:
        raise SmokeFailure(f"Apple Vision OCR smoke output was unexpected: {output}")


def verify_makemkv_probe(bundle: AppBundle, clean_env: dict[str, str]) -> None:
    makemkv_path = Path("/Applications/MakeMKV.app/Contents/MacOS/makemkvcon")
    if not makemkv_path.exists():
        print("MakeMKV absent: expected first-run GUI recovery path should ask the user to install MakeMKV")
        return

    if not os.access(makemkv_path, os.X_OK):
        raise SmokeFailure(f"MakeMKV exists but is not executable: {makemkv_path}")
    print("MakeMKV app bundle will be discovered outside PATH by BD_to_AVP preflight")
    print(f"MakeMKV present: {makemkv_path}")


def smoke_app(app_path: Path, *, skip_spctl: bool) -> None:
    bundle = read_bundle(app_path)
    clean_env = build_clean_env()
    print(f"Smoking app bundle: {bundle.path}")
    print(f"Bundle identifier: {bundle.bundle_identifier}")
    verify_app_layout(bundle)
    verify_gatekeeper(bundle.path, skip_spctl=skip_spctl)
    verify_no_homebrew_requirement(bundle)
    verify_bundled_tools(bundle, clean_env)
    verify_cli_version(bundle, clean_env)
    verify_cli_help(bundle, clean_env)
    verify_apple_vision_ocr(bundle, clean_env)
    verify_makemkv_probe(bundle, clean_env)
    print("Release app smoke passed")


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke-test an installed BD_to_AVP macOS app bundle.")
    parser.add_argument("--app-path", type=Path, help="Path to the .app bundle to smoke.")
    parser.add_argument(
        "--skip-spctl", action="store_true", help="Skip Gatekeeper assessment for local unsigned builds."
    )
    args = parser.parse_args()

    app_path = args.app_path or find_default_app()
    try:
        smoke_app(app_path, skip_spctl=args.skip_spctl)
    except SmokeFailure as error:
        print(f"Release app smoke failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
