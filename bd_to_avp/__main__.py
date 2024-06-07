import sys

from bd_to_avp.app import start_gui
from bd_to_avp.process import start_process
from bd_to_avp import install
from bd_to_avp.modules.config import config


def main() -> None:
    is_gui = len(sys.argv) == 1

    if not install.check_install():
        install.install_deps(is_gui)

    if is_gui:
        start_gui()
    else:
        config.parse_args()
        start_process()


if __name__ == "__main__":
    main()
