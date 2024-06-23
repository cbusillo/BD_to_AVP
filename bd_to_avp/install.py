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


def prompt_for_password() -> tuple[Path, dict[str, str]]:
    script = f"""
    with timeout of 3600 seconds
        tell app "System Events"
            activate
            set pw to text returned of (display dialog "Enter your password: (This will take a while)" default answer "" with hidden answer)
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
    os.environ["PATH"] = f"{config.HOMEBREW_PREFIX_BIN}:{os.environ.get("PATH", "")}"


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

    upgrade_brew(sudo_env)

    manage_brew_package("makemkv", sudo_env, True, "uninstall")

    for package in config.BREW_CASKS_TO_INSTALL:
        if not check_is_package_installed(package):
            manage_brew_package(package, sudo_env, True, "reinstall")

    manage_brew_package(config.BREW_PACKAGES_TO_INSTALL, sudo_env)

    if not check_rosetta():
        install_rosetta()

    if should_install_mp4box():
        install_mp4box(sudo_env)

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


def should_install_mp4box() -> bool:
    if not config.MP4BOX_PATH.exists() or not check_mp4box_version(config.MP4BOX_VERSION):
        if config.MP4BOX_PATH.exists():
            print("Removing old MP4Box...")
            shutil.rmtree("/Applications/GPAC.app", ignore_errors=True)
        print("Installing MP4Box...")
        return True
    return False


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
    app_dir_path = next(Path("/Applications").glob(f"{package.replace('-', ' ')}.app"), None)
    if package in process.stdout and app_dir_path and app_dir_path.exists() and not is_file_quarantined(app_dir_path):
        return True

    return False


def is_file_quarantined(file_path: Path) -> bool:
    try:
        result = subprocess.run(["xattr", "-p", "com.apple.quarantine", file_path], capture_output=True, text=True)
        return result.returncode == 0
    except subprocess.CalledProcessError as e:
        print(f"Error checking quarantine status: {e}")
        return False


def manage_brew_package(
    packages: str | list[str], sudo_env: dict[str, str], cask: bool = False, operation: str = "install"
) -> None:
    if isinstance(packages, str):
        packages = [packages]
    packages_str = " ".join(packages)
    print(f"{operation.title()}ing {packages_str}...")

    brew_command = ["/opt/homebrew/bin/brew", operation, "--force"]
    if operation in ["install", "reinstall"]:
        brew_command.append("--no-quarantine")

    if cask:
        brew_command.append("--cask")

    brew_command += packages

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

    print(f"{packages_str} {operation}ed.")


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


def upgrade_brew(sudo_env: dict[str, str]) -> None:
    print("Upgrading Homebrew...")
    brew_command = ["/opt/homebrew/bin/brew", "upgrade"]
    process = subprocess.run(
        brew_command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=sudo_env,
    )

    if process.returncode != 0:
        on_error_process("Homebrew", process)
    print("Homebrew upgraded.")


def check_mp4box_version(version: str) -> bool:
    processs = subprocess.run(
        [config.MP4BOX_PATH, "-version"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return version in processs.stderr


def install_mp4box(sudo_env: dict[str, str]) -> None:
    print("Installing MP4Box...")

    response = requests.get(
        "https://download.tsi.telecom-paristech.fr/gpac/release/2.2.1/gpac-2.2.1-rev0-gb34e3851-release-2.2.pkg"
    )
    if response.status_code != 200:
        on_error_string("MP4Box", "Failed to download MP4Box installer.")
    with tempfile.NamedTemporaryFile(suffix=".pkg", delete=False) as mp4box_file:
        mp4box_file.write(response.content)
    mp4box_file_path = Path(mp4box_file.name)

    command = [
        "sudo",
        "-A",
        "installer",
        "-pkg",
        mp4box_file_path.as_posix(),
        "-target",
        "/",
    ]
    process = subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=sudo_env,
    )

    if process.returncode != 0:
        on_error_process("MP4Box", process)
    print("MP4Box installed.")


def wine_boot() -> None:
    print("Booting Wine...")
    if not config.WINE_PATH.exists():
        on_error_string(
            "Wine",
            "Wine not found in Homebrew.  If you have Wine Stable installed in Applications, please remove it and run the program again.",
        )
    process = subprocess.run(
        [(config.HOMEBREW_PREFIX_BIN / "wineboot").as_posix()],
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
