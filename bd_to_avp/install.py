import os
import platform
import shutil
import subprocess
from pathlib import Path

import requests

from bd_to_avp.modules.config import config
from bd_to_avp.modules.util import run_command


def install_deps(is_gui: bool) -> None:
    if not is_arm64():
        raise ValueError("This script is only supported on Apple Silicon Macs.")
    print("Installing dependencies...")
    password: str = ""

    if is_gui:
        password = prompt_for_password()

    if not Path("/opt/homebrew/bin/brew").exists():
        install_brew(password)
    else:
        update_brew(is_gui)

    for package in config.BREW_CASKS_TO_INSTALL:
        uninstall_brew_package(package, is_gui)

    for package in config.BREW_CASKS_TO_INSTALL:
        install_brew_package(package, is_gui, cask=True)
    for package in config.BREW_PACKAGES_TO_INSTALL:
        install_brew_package(package, is_gui)

    if not config.MP4BOX_PATH.exists():
        if not check_mp4box_version(config.MP4BOX_VERSION):
            shutil.rmtree("/Applications/GPAC.app", ignore_errors=True)
        install_mp4box(password)

    wine_boot()


def check_install() -> bool:
    installed_version = config.load_version()
    print(
        f"Installed bd-to-avp version: {installed_version}\nbd-to-avp version: {config.code_version}"
    )
    if installed_version == config.code_version:
        return True

    return False


def prompt_for_password() -> str:
    script = """
    tell app "System Events"
        text returned of (display dialog "Please enter your password:" default answer "" with title "Password Installer" with hidden answer)
    end tell
    """
    password = subprocess.check_output(
        ["osascript", "-e", script], universal_newlines=True
    ).strip()
    if not password:
        on_error_string(
            "Password",
            "Password is required for install.  If you have a blank password, please set one",
            True,
        )
    return password


def show_message(title: str, message: str) -> None:
    script = f"""
    tell app "System Events"
        display dialog "{message}" buttons {{"OK"}} default button "OK" with title "{title}" with icon caution
    end tell
    """
    subprocess.call(["osascript", "-e", script])


def on_error_process(
    package: str, process: subprocess.CompletedProcess, is_gui: bool
) -> None:
    command = (
        " ".join(process.args) if isinstance(process.args, list) else str(process.args)
    )
    if is_gui:
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


def on_error_string(package: str, error: str, is_gui: bool) -> None:
    if is_gui:
        show_message(f"Failed to install {package}.", error)
    raise ValueError(error)


def is_arm64() -> bool:
    return platform.machine() == "arm64"


def install_brew_package(package: str, is_gui: bool, cask: bool = False) -> None:
    brew_command = ["/opt/homebrew/bin/brew", "install", "--no-quarantine"]
    if cask:
        brew_command.append("--cask")
    brew_command.append(package)

    process = subprocess.run(
        brew_command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    if process.returncode != 0:
        on_error_process(package, process, is_gui)


def uninstall_brew_package(package: str, is_gui: bool) -> None:
    brew_command = ["/opt/homebrew/bin/brew", "remove", package]

    process = subprocess.run(
        brew_command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    if process.returncode != 0:
        if "is not installed" in process.stderr:
            return
        on_error_process(package, process, is_gui)


def update_brew(is_gui: bool) -> None:
    print("Updating Homebrew...")
    brew_command = ["/opt/homebrew/bin/brew", "update"]
    process = subprocess.run(
        brew_command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    if process.returncode != 0:
        on_error_process("Homebrew", process, is_gui)
    print("Homebrew updated.")


def check_mp4box_version(version: str) -> bool:
    processs = subprocess.run(
        [config.MP4BOX_PATH, "-version"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return version in processs.stdout


def install_mp4box(password: str) -> None:
    print("Installing MP4Box...")
    sudo_env = os.environ.copy()
    if password:
        sudo_env["SUDO_ASKPASS"] = f"echo {password}"

    command = [
        "sudo",
        "installer",
        "-pkg",
        (config.SCRIPT_PATH / "installers" / "gpac-2.2.1.pkg").as_posix(),
    ]
    process = subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=sudo_env,
    )

    if process.returncode != 0:
        on_error_process("MP4Box", process, bool(password))
    print("MP4Box installed.")


def wine_boot() -> None:
    print("Booting Wine...")
    process = subprocess.run(
        [(config.HOMEBREW_PREFIX_BIN / "wineboot").as_posix()],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    if process.returncode != 0:
        on_error_process("Wine", process, False)
    print("Wine booted.")


def install_brew(password: str) -> None:
    print("Installing Homebrew for arm64...")
    sudo_env = os.environ.copy()
    if password:
        sudo_env["SUDO_ASKPASS"] = f"echo {password}"

    response = requests.get(
        "https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh"
    )
    if response.status_code != 200:
        on_error_string(
            "Homebrew", "Failed to download Homebrew install script.", bool(password)
        )
    brew_install_script = response.text
    brew_install_command = ["/bin/bash", "-c", brew_install_script]
    process = subprocess.run(
        brew_install_command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=sudo_env,
    )

    if process.returncode != 0:
        on_error_process("Homebrew", process, bool(password))
    print("Homebrew installed.")


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
    run_command(
        regedit_command, "Update the Windows registry for FRIM plugins.", regedit_env
    )
    print("Updated the Windows registry for FRIM plugins.")
