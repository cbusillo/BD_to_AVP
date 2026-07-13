from __future__ import annotations

import concurrent.futures
import importlib

from pathlib import Path
from typing import Any

EXPECTED_BRIEFCASE_VERSION = "0.4.4"
SPARKLE_FRAMEWORK_NAME = "Sparkle.framework"


briefcase = importlib.import_module("briefcase")
macos_module = importlib.import_module("briefcase.platforms.macOS")
macos_utils = importlib.import_module("briefcase.platforms.macOS.utils")
macOSSigningMixin = macos_module.macOSSigningMixin
is_mach_o_binary = macos_utils.is_mach_o_binary


def collect_sign_targets(command: Any, app: Any) -> list[Path]:
    bundle_path = command.package_path(app)
    resources_path = bundle_path / "Contents/Resources"
    frameworks_path = bundle_path / "Contents/Frameworks"
    sign_targets: list[Path] = []

    for folder in (resources_path, frameworks_path):
        if not folder.exists():
            continue
        sign_targets.extend(
            path for path in folder.rglob("*") if not path.is_dir() and not path.is_symlink() and is_mach_o_binary(path)
        )
        sign_targets.extend(folder.rglob("*.framework"))
        sign_targets.extend(folder.rglob("*.app"))
        sign_targets.extend(folder.rglob("*.xpc"))

    sign_targets.append(bundle_path)
    return list(dict.fromkeys(sign_targets))


def is_sparkle_target(path: Path, app_bundle_path: Path) -> bool:
    sparkle_framework_path = app_bundle_path / "Contents/Frameworks" / SPARKLE_FRAMEWORK_NAME
    return path == sparkle_framework_path or sparkle_framework_path in path.parents


def sign_app_with_xpc(command: Any, app: Any, identity: Any) -> None:
    bundle_path = command.package_path(app)
    sign_targets = collect_sign_targets(command, app)
    progress_bar = command.console.progress_bar()
    task_id = progress_bar.add_task("Signing App", total=len(sign_targets))

    with progress_bar:
        for group in command.tools.file.sorted_depth_first_groups(sign_targets):
            group_paths = list(group)
            serialize_group = command.console.is_deep_debug or any(
                is_sparkle_target(path, bundle_path) for path in group_paths
            )
            with concurrent.futures.ThreadPoolExecutor(max_workers=1 if serialize_group else None) as executor:
                futures = []
                for path in group_paths:
                    entitlements = None if is_sparkle_target(path, bundle_path) else command.entitlements_path(app)
                    futures.append(
                        executor.submit(
                            command.sign_file,
                            path,
                            entitlements=entitlements,
                            identity=identity,
                        )
                    )
                for future in concurrent.futures.as_completed(futures):
                    future.result()
                    progress_bar.update(task_id, advance=1)


def install_patch() -> None:
    if briefcase.__version__ != EXPECTED_BRIEFCASE_VERSION:
        raise RuntimeError(
            f"Sparkle signing integration requires Briefcase {EXPECTED_BRIEFCASE_VERSION}; "
            f"found {briefcase.__version__}."
        )
    if getattr(macOSSigningMixin.sign_app, "_bd_to_avp_xpc_patch", False):
        return
    sign_app_with_xpc._bd_to_avp_xpc_patch = True  # type: ignore[attr-defined]
    macOSSigningMixin.sign_app = sign_app_with_xpc
