import signal

from bd_to_avp.modules.process import start_process
from bd_to_avp.modules.config import config
from bd_to_avp.vendor.pgsrip.ocr import AppleVisionOcr


GUI_DEPENDENCY_MESSAGE = (
    'GUI dependencies are not installed. Install them with `pip install "bd_to_avp[gui]"` or use the release DMG.'
)


def _start_gui() -> None:
    try:
        from bd_to_avp.app import start_gui
    except ModuleNotFoundError as error:
        if error.name != "PySide6" and not (error.name or "").startswith("PySide6."):
            raise
        raise SystemExit(GUI_DEPENDENCY_MESSAGE) from None

    start_gui()


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
        _start_gui()
    else:
        start_process()


if __name__ == "__main__":
    main()
