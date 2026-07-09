import atexit
import signal

import psutil

from bd_to_avp.app import start_gui
from bd_to_avp.modules.process import start_process
from bd_to_avp.modules.config import config
from bd_to_avp.vendor.pgsrip.ocr import AppleVisionOcr


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
    config.configure_tool_environment()

    if not config.app.is_gui:
        config.parse_args()
        if config.smoke_apple_vision_ocr:
            AppleVisionOcr._load_frameworks()
            print("Apple Vision OCR import smoke passed")
            return

    if config.app.is_gui:
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        start_gui()
    else:
        start_process()


if __name__ == "__main__":
    main()
