import os
import shutil
from pathlib import Path

from bd_to_avp.config import config
from bd_to_avp.util import run_command


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
