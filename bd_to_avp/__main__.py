import atexit
import os
import signal

import psutil

from bd_to_avp.app import start_gui
from bd_to_avp.modules.process import start_process
from bd_to_avp import install
from bd_to_avp.modules.config import config


def kill_child_processes() -> None:
    current_process = psutil.Process()
    child_processes = current_process.children(recursive=True)

    for child in child_processes:
        if "pycharm" not in child.name().lower() and "code" not in child.name().lower():
            child.terminate()

    _, alive = psutil.wait_procs(child_processes, timeout=3)
    for p in alive:
        if "pycharm" not in p.name().lower() and "code" not in p.name().lower():
            p.kill()


atexit.register(kill_child_processes)


def main() -> None:
    if config.HOMEBREW_PREFIX_BIN.as_posix() not in os.environ["PATH"]:
        os.environ["PATH"] = f"{config.HOMEBREW_PREFIX_BIN}:{os.environ['PATH']}"

    if not install.check_install_version():
        install.install_deps()
        config.app.save_version_from_file()

    if config.app.is_gui:
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        start_gui()
    else:
        config.parse_args()
        start_process()


if __name__ == "__main__":
    main()
