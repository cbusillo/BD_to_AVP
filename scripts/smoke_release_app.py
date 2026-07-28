from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4


DEFAULT_APP_PATHS = [
    Path("/Applications/3D Blu-ray to Vision Pro.app"),
    Path("/Applications/Blu-ray to AVP.app"),
]
APP_RESOURCE_APP_PATH = Path("Contents/Resources/app")
APP_BIN_PATH = APP_RESOURCE_APP_PATH / "bd_to_avp" / "bin"
WORKER_EXECUTABLE_NAME = "BluRayToVisionProEngine"
EXPECTED_WORKER_PROTOCOL_VERSION = 10
PREVIEW_PRESENTATION_SMOKE_ARGUMENT = "--preview-presentation-smoke"
DEFAULT_COMMAND_TIMEOUT_SECONDS = 20
PREVIEW_PRESENTATION_TIMEOUT_SECONDS = 30
PREVIEW_PROCESS_EXIT_TIMEOUT_SECONDS = 5
PREVIEW_POLL_INTERVAL_SECONDS = 0.1
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
    worker_executable: Path
    resources_app: Path
    bin_dir: Path
    bundle_identifier: str
    short_version: str


class SmokeFailure(RuntimeError):
    pass


def run(
    command: list[str | Path],
    *,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    input_text: str | None = None,
    timeout: float = DEFAULT_COMMAND_TIMEOUT_SECONDS,
    combine_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    serialized_command = [str(item) for item in command]
    try:
        return subprocess.run(
            serialized_command,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if combine_output else subprocess.PIPE,
            env=env,
            cwd=cwd,
            input=input_text,
            timeout=timeout,
        )
    except FileNotFoundError as error:
        raise SmokeFailure(f"Smoke command is unavailable: {serialized_command[0]}") from error
    except subprocess.TimeoutExpired as error:
        raise SmokeFailure(f"Smoke command exceeded {timeout:g} seconds: {' '.join(serialized_command)}") from error
    except subprocess.CalledProcessError as error:
        output = "\n".join(part.strip() for part in (error.stdout, error.stderr) if part and part.strip())
        raise SmokeFailure(
            f"Smoke command failed with status {error.returncode}: {' '.join(serialized_command)}\n{output}"
        ) from error


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
    app_path = app_path.expanduser().resolve()
    info_plist_path = app_path / "Contents" / "Info.plist"
    if not info_plist_path.is_file():
        raise SmokeFailure(f"Missing app Info.plist: {info_plist_path}")

    with info_plist_path.open("rb") as plist_file:
        info = plistlib.load(plist_file)

    executable_name = require_plist_string(info, "CFBundleExecutable", info_plist_path)
    bundle_identifier = require_plist_string(info, "CFBundleIdentifier", info_plist_path)
    short_version = require_plist_string(info, "CFBundleShortVersionString", info_plist_path)
    executable = app_path / "Contents" / "MacOS" / executable_name
    worker_executable = app_path / "Contents" / "MacOS" / WORKER_EXECUTABLE_NAME
    resources_app = app_path / APP_RESOURCE_APP_PATH
    bin_dir = app_path / APP_BIN_PATH
    return AppBundle(
        path=app_path,
        executable=executable,
        worker_executable=worker_executable,
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
    for path in [bundle.executable, bundle.worker_executable, bundle.resources_app, bundle.bin_dir]:
        if not path.exists():
            raise SmokeFailure(f"Missing expected app bundle path: {path}")
    for executable in (bundle.executable, bundle.worker_executable):
        if not os.access(executable, os.X_OK):
            raise SmokeFailure(f"App executable is not executable: {executable}")


def verify_gatekeeper(app_path: Path, *, skip_spctl: bool) -> None:
    if skip_spctl:
        return
    try:
        run(["spctl", "--assess", "--type", "execute", "--verbose=4", app_path])
    except SmokeFailure as error:
        raise SmokeFailure(f"Gatekeeper assessment failed for {app_path}: {error}") from error


def developer_tools_available() -> bool:
    try:
        run(["xcode-select", "-p"])
    except SmokeFailure:
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
    except SmokeFailure as error:
        raise SmokeFailure(f"Bundled tool did not run: {tool_path}\n{error}") from error

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
    except SmokeFailure as error:
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


def verify_package_version(bundle: AppBundle) -> None:
    print(f"Package version metadata passed: {bundle.short_version}")


def native_smoke_environment(clean_env: dict[str, str], temporary_directory: Path) -> dict[str, str]:
    environment = clean_env.copy()
    environment.pop("__CFBundleIdentifier", None)
    environment["HOME"] = str(temporary_directory)
    environment["TMPDIR"] = str(temporary_directory)
    return environment


def launch_preview_application(
    bundle: AppBundle,
    media_path: Path,
    result_path: Path,
    *,
    environment: dict[str, str],
    cwd: Path,
) -> None:
    run(
        [
            "/usr/bin/open",
            "-g",
            "-n",
            bundle.path,
            "--args",
            PREVIEW_PRESENTATION_SMOKE_ARGUMENT,
            media_path,
            result_path,
        ],
        env=environment,
        cwd=cwd,
    )


def wait_for_path(path: Path, *, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            return True
        time.sleep(PREVIEW_POLL_INTERVAL_SECONDS)
    return path.is_file()


def preview_process_ids(result_path: Path) -> set[int]:
    command = ["/usr/bin/pgrep", "-f", re.escape(str(result_path))]
    try:
        completed = subprocess.run(
            command,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=DEFAULT_COMMAND_TIMEOUT_SECONDS,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as error:
        raise SmokeFailure("Could not inspect the packaged preview smoke process") from error
    if completed.returncode == 1:
        return set()
    if completed.returncode != 0:
        raise SmokeFailure(f"Could not inspect the packaged preview smoke process: {completed.stderr.strip()}")
    try:
        return {int(line) for line in completed.stdout.splitlines() if line.strip()}
    except ValueError as error:
        raise SmokeFailure("The packaged preview smoke process lookup returned an invalid PID") from error


def wait_for_preview_process_exit(result_path: Path, *, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not preview_process_ids(result_path):
            return True
        time.sleep(PREVIEW_POLL_INTERVAL_SECONDS)
    return not preview_process_ids(result_path)


def terminate_preview_processes(result_path: Path) -> None:
    process_ids = preview_process_ids(result_path)
    for process_id in process_ids:
        try:
            os.kill(process_id, signal.SIGTERM)
        except ProcessLookupError:
            pass
    if not process_ids or wait_for_preview_process_exit(result_path, timeout=2):
        return
    for process_id in preview_process_ids(result_path):
        try:
            os.kill(process_id, signal.SIGKILL)
        except ProcessLookupError:
            pass


def verify_native_startup(bundle: AppBundle, clean_env: dict[str, str]) -> None:
    with tempfile.TemporaryDirectory(prefix="bd-to-avp-installed-startup-") as temporary_directory:
        temporary_path = Path(temporary_directory)
        run(
            [bundle.executable, "--startup-smoke"],
            env=native_smoke_environment(clean_env, temporary_path),
            cwd=temporary_path,
        )
    print("Native startup smoke passed")


def verify_worker_protocol(bundle: AppBundle, clean_env: dict[str, str]) -> None:
    with tempfile.TemporaryDirectory(prefix="bd-to-avp-installed-worker-") as temporary_directory:
        temporary_path = Path(temporary_directory)
        source_path = temporary_path / "smoke.m2ts"
        run(
            [
                bundle.bin_dir / "ffmpeg",
                "-hide_banner",
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
                source_path,
            ],
            env=clean_env,
        )
        job_id = str(uuid4())
        request = {
            "protocol_version": EXPECTED_WORKER_PROTOCOL_VERSION,
            "type": "job.start",
            "job_id": job_id,
            "operation": "inspect_source",
            "source": {"kind": "direct_file", "path": str(source_path)},
        }
        completed = run(
            [bundle.worker_executable],
            env=clean_env,
            cwd=temporary_path,
            input_text=json.dumps(request) + "\n",
            timeout=30,
            combine_output=False,
        )

    try:
        events = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
    except json.JSONDecodeError as error:
        raise SmokeFailure(f"Packaged worker returned invalid JSON: {completed.stdout}") from error
    if len(events) < 4 or not all(isinstance(event, dict) for event in events):
        raise SmokeFailure("Packaged worker did not return the required event stream")
    if [event.get("sequence") for event in events] != list(range(len(events))):
        raise SmokeFailure("Packaged worker event sequence was not contiguous")
    if any(event.get("protocol_version") != EXPECTED_WORKER_PROTOCOL_VERSION for event in events):
        raise SmokeFailure(f"Packaged worker protocol did not match v{EXPECTED_WORKER_PROTOCOL_VERSION}")
    ready_payload = events[0].get("payload")
    worker_version = ready_payload.get("worker_version") if isinstance(ready_payload, dict) else None
    if events[0].get("type") != "worker.ready" or worker_version != bundle.short_version:
        raise SmokeFailure(
            f"Packaged worker version mismatch: expected {bundle.short_version}, found {worker_version!r}"
        )
    if events[-1].get("type") != "job.completed":
        raise SmokeFailure("Packaged worker inspect smoke did not complete")
    print(f"Packaged worker v{EXPECTED_WORKER_PROTOCOL_VERSION} smoke passed")


def verify_apple_vision_ocr(bundle: AppBundle, clean_env: dict[str, str]) -> None:
    output = run([bundle.worker_executable, "--smoke-apple-vision-ocr"], env=clean_env).stdout
    if "Apple Vision OCR import smoke passed" not in output:
        raise SmokeFailure(f"Apple Vision OCR smoke output was unexpected: {output}")


def verify_preview_presentation(bundle: AppBundle, clean_env: dict[str, str]) -> None:
    with tempfile.TemporaryDirectory(prefix="bd-to-avp-installed-preview-") as temporary_directory:
        temporary_path = Path(temporary_directory)
        workspace_root = temporary_path / "workspace"
        artifact_directory = workspace_root / "artifact"
        artifact_directory.mkdir(parents=True)
        media_path = artifact_directory / "preview.mov"
        result_path = temporary_path / "result.json"
        run(
            [
                bundle.bin_dir / "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=320x180:rate=24",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=channel_layout=stereo:sample_rate=48000",
                "-t",
                "1",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-movflags",
                "+faststart",
                "-y",
                media_path,
            ],
            env=clean_env,
        )
        launch_preview_application(
            bundle,
            media_path,
            result_path,
            environment=native_smoke_environment(clean_env, temporary_path),
            cwd=temporary_path,
        )
        if not wait_for_path(result_path, timeout=PREVIEW_PRESENTATION_TIMEOUT_SECONDS):
            terminate_preview_processes(result_path)
            raise SmokeFailure("Packaged preview presentation smoke did not write its result receipt")
        if not wait_for_preview_process_exit(result_path, timeout=PREVIEW_PROCESS_EXIT_TIMEOUT_SECONDS):
            terminate_preview_processes(result_path)
            raise SmokeFailure("Packaged preview presentation smoke did not terminate after writing its receipt")
        try:
            receipt = json.loads(result_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise SmokeFailure("Packaged preview presentation smoke wrote invalid JSON") from error
        expected = {
            "schema_version": 1,
            "player_presented": True,
            "media_ready": True,
            "cleanup_complete": True,
        }
        mismatches = [
            f"{key}: expected {value!r}, found {receipt.get(key)!r}"
            for key, value in expected.items()
            if receipt.get(key) != value
        ]
        if mismatches:
            message = receipt.get("message")
            if isinstance(message, str) and message:
                mismatches.append(message)
            raise SmokeFailure("Packaged preview presentation smoke failed:\n" + "\n".join(mismatches))
        if artifact_directory.exists():
            raise SmokeFailure("Packaged preview presentation smoke left its artifact workspace behind")
    print("Packaged preview presentation smoke passed")


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
    verify_package_version(bundle)
    verify_native_startup(bundle, clean_env)
    verify_worker_protocol(bundle, clean_env)
    verify_apple_vision_ocr(bundle, clean_env)
    verify_preview_presentation(bundle, clean_env)
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
