import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import requests

from bd_to_avp.modules.config import config
from bd_to_avp.modules.command import run_command


CASK_APP_PATHS = {
    "makemkv": [Path("/Applications/MakeMKV.app")],
    "wine-stable": [Path("/Applications/Wine Stable.app"), Path("/Applications/Wine.app")],
}


def prompt_for_password() -> tuple[Path, dict[str, str]]:
    script = """
    with timeout of 3600 seconds
        tell app "System Events"
            activate
            set pw to text returned of (display dialog "Enter your password:" default answer "" with hidden answer)
        end tell
        return pw
    end timeout
    """
    with tempfile.NamedTemporaryFile(suffix=".sh", delete=False) as pw_file:
        pw_file.write('#!/bin/bash\necho "$HOMEBREW_PASSWORD"\n'.encode())
        pw_file_path = Path(pw_file.name)
    pw_file_path.chmod(0o700)
    password_correct = False
    sudo_env = {}
    while not password_correct:
        password = subprocess.check_output(["osascript", "-e", script], text=True).strip()

        sudo_env = os.environ.copy()
        sudo_env["HOMEBREW_PASSWORD"] = password

        sudo_env["SUDO_ASKPASS"] = pw_file_path.as_posix()
        check_sudo_password = subprocess.run(
            ["/usr/bin/sudo", "-A", "ls"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            input=password,
            env=sudo_env,
        )
        password_correct = check_sudo_password.returncode == 0
    return pw_file_path, sudo_env


def add_homebrew_to_path() -> None:
    zshrc_path = Path.home() / ".zshrc"
    if not zshrc_path.exists():
        zshrc_path.touch()
    with open(zshrc_path, "a") as zshrc_file:
        zshrc_file.write(f'export PATH="{config.HOMEBREW_PREFIX_BIN}:$PATH"\n')
    os.environ["PATH"] = f"{config.HOMEBREW_PREFIX_BIN}:{os.environ.get('PATH', '')}"


def check_for_homebrew_in_path() -> bool:
    zshrc = Path.home() / ".zshrc"
    if not zshrc.exists():
        return False
    zshrc_contents = zshrc.read_text()
    if config.HOMEBREW_PREFIX_BIN.as_posix() in zshrc_contents:
        return True
    return False


def install_deps() -> None:
    if not is_arm64():
        raise ValueError("This script is only supported on Apple Silicon Macs.")
    print("Installing dependencies...")
    pw_file_path = None
    sudo_env = os.environ.copy()

    if config.app.is_gui:
        try:
            pw_file_path, sudo_env = prompt_for_password()
        except subprocess.CalledProcessError:
            on_error_string("Password prompt", "Failed to get password.")
            sys.exit(1)

    if not Path(config.HOMEBREW_PREFIX_BIN / "brew").exists():
        install_brew(sudo_env)
    else:
        update_brew(sudo_env)

    if not check_for_homebrew_in_path():
        add_homebrew_to_path()

    for package in config.BREW_CASKS_TO_INSTALL:
        if not check_is_package_installed(package):
            manage_brew_package(package, sudo_env, True)

    manage_brew_package(config.BREW_PACKAGES_TO_INSTALL, sudo_env)

    verify_dependency_binaries()

    if not check_rosetta():
        install_rosetta()

    wine_boot()

    if pw_file_path:
        pw_file_path.unlink()


def check_rosetta() -> bool:
    process = subprocess.run(
        ["arch", "-x86_64", "echo", "hello"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return process.stdout.strip() == "hello"


def install_rosetta() -> None:
    print("Installing Rosetta...")
    process = subprocess.run(
        ["softwareupdate", "--install-rosetta", "--agree-to-license"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    if process.returncode != 0:
        on_error_process("Rosetta", process)
    print("Rosetta installed.")


def verify_dependency_binaries() -> None:
    missing_binaries = [
        path for path in [config.MAKEMKVCON_PATH, config.MP4BOX_PATH, config.WINE_PATH] if not path.exists()
    ]
    wineboot_path = config.HOMEBREW_PREFIX_BIN / "wineboot"
    if not wineboot_path.exists():
        missing_binaries.append(wineboot_path)

    if not missing_binaries:
        return

    missing_list = "\n".join(f"- {path}" for path in missing_binaries)
    on_error_string(
        "Dependencies",
        "Required command-line tools are missing after dependency installation:\n"
        f"{missing_list}\n\n"
        "Install or repair MakeMKV and Wine, then run this app again. "
        "MP4Box is provided by the Homebrew gpac formula.",
    )


def check_install_version() -> bool:
    installed_version = config.app.load_version_from_file()
    print(f"Installed bd-to-avp version: {installed_version}\nCode bd-to-avp version: {config.app.code_version}")
    if installed_version == config.app.code_version:
        return True

    return False


def show_message(title: str, message: str) -> None:
    script = f"""
    tell app "System Events"
        display dialog "{message}" buttons {{"OK"}} default button "OK" with title "{title}" with icon caution
    end tell
    """
    subprocess.call(["osascript", "-e", script])


def on_error_process(package: str, process: subprocess.CompletedProcess) -> None:
    command = " ".join(process.args) if isinstance(process.args, list) else str(process.args)
    if config.app.is_gui:
        show_message(
            f"Failed {package} processing",
            f"Command:{command}\nOutput:{process.stderr}\nError:{process.stdout}",
        )
    raise subprocess.CalledProcessError(
        process.returncode,
        command,
        output=process.stdout,
        stderr=process.stderr,
    )


def on_error_string(package: str, error: str) -> None:
    if config.app.is_gui:
        show_message(f"Failed to install {package}.", error)
    raise ValueError(error)


def is_arm64() -> bool:
    return platform.machine() == "arm64"


def check_is_package_installed(package: str) -> bool:
    process = subprocess.run(
        ["/opt/homebrew/bin/brew", "list", "--cask", "--formula", package],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if package not in process.stdout:
        return False

    app_paths = [app_path for app_path in get_cask_app_paths(package) if app_path.exists()]
    if not app_paths:
        return False

    return any(not is_file_quarantined(app_path) for app_path in app_paths)


def get_cask_app_paths(package: str) -> list[Path]:
    return CASK_APP_PATHS.get(package, [Path("/Applications") / f"{package.replace('-', ' ')}.app"])


def is_file_quarantined(file_path: Path) -> bool:
    try:
        result = subprocess.run(["xattr", "-p", "com.apple.quarantine", file_path], capture_output=True, text=True)
        return result.returncode == 0
    except subprocess.CalledProcessError as e:
        print(f"Error checking quarantine status: {e}")
        return False


def clear_cask_quarantine(packages: list[str], sudo_env: dict[str, str]) -> None:
    for package in packages:
        for app_path in get_cask_app_paths(package):
            if not app_path.exists() or not is_file_quarantined(app_path):
                continue

            process = subprocess.run(
                ["xattr", "-dr", "com.apple.quarantine", app_path.as_posix()],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=sudo_env,
            )
            if process.returncode != 0:
                process = subprocess.run(
                    ["/usr/bin/sudo", "-A", "xattr", "-dr", "com.apple.quarantine", app_path.as_posix()],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=sudo_env,
                )

            if process.returncode != 0:
                on_error_process(f"{package} quarantine cleanup", process)


def manage_brew_package(
    packages: str | list[str], sudo_env: dict[str, str], cask: bool = False, operation: str = "install"
) -> None:
    if isinstance(packages, str):
        packages = [packages]
    packages_str = " ".join(packages)
    print(f"{operation.title()}ing {packages_str}...")

    brew_command = build_brew_command(packages, cask, operation)

    process = subprocess.run(
        brew_command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=sudo_env,
    )

    if operation == "uninstall" and process.returncode == 1:
        print(f"{packages_str} not installed.")
        return

    if process.returncode != 0:
        on_error_process(packages_str, process)

    if cask and operation == "install":
        clear_cask_quarantine(packages, sudo_env)

    print(f"{packages_str} {operation}ed.")


def build_brew_command(packages: list[str], cask: bool = False, operation: str = "install") -> list[str]:
    brew_command = ["/opt/homebrew/bin/brew", operation, "--force"]
    if cask:
        brew_command.append("--cask")

    return brew_command + packages


def update_brew(sudo_env: dict[str, str]) -> None:
    print("Updating Homebrew...")
    brew_command = ["/opt/homebrew/bin/brew", "update"]
    process = subprocess.run(
        brew_command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=sudo_env,
    )

    if process.returncode != 0:
        on_error_process("Homebrew", process)
    print("Homebrew updated.")


def wine_boot() -> None:
    print("Booting Wine...")
    wineboot_path = config.HOMEBREW_PREFIX_BIN / "wineboot"
    if not config.WINE_PATH.exists() or not wineboot_path.exists():
        on_error_string(
            "Wine",
            "Wine command-line tools were not found. Install or repair Wine, then run the program again. "
            "The Homebrew wine-stable cask is deprecated, so manual Wine installation may be required.",
        )
    process = subprocess.run(
        [wineboot_path.as_posix()],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    if process.returncode != 0:
        on_error_process("Wine", process)
    print("Wine booted.")


def install_brew(sudo_env: dict[str, str]) -> None:
    print("Installing Homebrew for arm64...")

    response = requests.get("https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh")
    if response.status_code != 200:
        on_error_string("Homebrew", "Failed to download Homebrew install script.")
    brew_install_script = response.text

    with tempfile.NamedTemporaryFile(suffix=".sh", delete=False) as brew_install_file:
        brew_install_file.write(brew_install_script.encode())
        brew_install_file_path = Path(brew_install_file.name)

    brew_install_command = ["/bin/bash", brew_install_file_path.as_posix()]
    process = subprocess.run(
        brew_install_command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=sudo_env,
    )

    if process.returncode != 0:
        on_error_process("Homebrew", process)
    print("Homebrew installed.")
    brew_install_file_path.unlink()


def setup_frim() -> None:
    wine_prefix = Path(os.environ.get("WINEPREFIX", "~/.wine")).expanduser()
    frim_destination_path = wine_prefix / "drive_c/UTL/FRIM"

    if frim_destination_path.exists():
        print(f"{frim_destination_path} already exists. Skipping install.")
        return

    shutil.copytree(config.FRIM_PATH, frim_destination_path)
    print(f"Copied FRIM directory to {frim_destination_path}")

    reg_file_path = config.FRIM_PATH / "plugins64.reg"
    if not reg_file_path.exists():
        print(f"Registry file {reg_file_path} not found. Skipping registry update.")
        return

    regedit_command = [config.WINE_PATH, "regedit", reg_file_path]
    regedit_env = {"WINEPREFIX": str(wine_prefix)}
    run_command(regedit_command, "Update the Windows registry for FRIM plugins.", regedit_env)
    print("Updated the Windows registry for FRIM plugins.")
