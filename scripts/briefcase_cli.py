from __future__ import annotations

import importlib

from scripts.briefcase_macos_signing import install_patch


if __name__ == "__main__":
    install_patch()
    raise SystemExit(importlib.import_module("briefcase.__main__").main())
