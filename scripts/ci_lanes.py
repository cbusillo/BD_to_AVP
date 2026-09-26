"""Decide which expensive CI lanes a change needs.

A pull request pays only for the lanes whose inputs it touches. Every lane still
runs on a push to main and on a manual run, so nothing reaches a release
unexercised. Anything this script does not recognise runs everything.
"""

import argparse
import os
import subprocess
from collections.abc import Iterable, Mapping
from pathlib import Path, PurePosixPath

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PLAYER_TARGET = "BDToAVPPlayer"
# Changes confined to these never alter a built product: documents, recorded
# evidence, and the Python tests themselves (which always run).
INERT_PREFIXES = ("docs/", "tests/", "screenshots/", "design/")
INERT_SUFFIXES = (".md",)
# Inputs to every Xcode build, and the lane definitions themselves.
SHARED_BUILD_INPUTS = (
    "macos/project.yml",
    ".github/workflows/ci.yml",
    ".github/ci-compilation-cache.xcconfig",
    "scripts/ci_lanes.py",
)
ALL_LANES = ("native", "player")


def player_source_prefixes(project: Mapping[str, object]) -> tuple[str, ...]:
    """Source paths of the visionOS player and its test targets, read from the Xcode project spec."""
    targets = project["targets"]
    assert isinstance(targets, Mapping)
    prefixes: list[str] = []
    for name, target in targets.items():
        if not str(name).startswith(PLAYER_TARGET):
            continue
        for source in target.get("sources", []):
            path = source if isinstance(source, str) else source["path"]
            prefixes.append(str(PurePosixPath("macos") / path))
    return tuple(prefixes)


def is_inert(path: str) -> bool:
    return path.startswith(INERT_PREFIXES) or path.endswith(INERT_SUFFIXES)


def is_under(path: str, prefixes: Iterable[str]) -> bool:
    return any(path == prefix or path.startswith(prefix.rstrip("/") + "/") for prefix in prefixes)


def lanes_for(changed_paths: Iterable[str], player_prefixes: Iterable[str]) -> dict[str, bool]:
    player_prefixes = tuple(player_prefixes)
    lanes: dict[str, bool] = dict.fromkeys(ALL_LANES, False)
    for path in changed_paths:
        if is_inert(path):
            continue
        if path in SHARED_BUILD_INPUTS:
            return dict.fromkeys(ALL_LANES, True)
        if is_under(path, player_prefixes):
            lanes["player"] = True
            # RelaySessionCore and the shared relay file also build into the Mac app.
            if not path.startswith(f"macos/{PLAYER_TARGET}"):
                lanes["native"] = True
            continue
        # Everything else (the worker, the Mac app, scripts, bundled tools, packaging
        # metadata) can change the Mac app bundle, but never the player.
        lanes["native"] = True
    return lanes


def changed_paths(base: str) -> list[str]:
    output = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...HEAD"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [line for line in output.splitlines() if line]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", required=True, help="GitHub event name.")
    parser.add_argument("--base", default="", help="Pull-request base commit.")
    arguments = parser.parse_args()

    if arguments.event == "pull_request" and arguments.base:
        project = yaml.safe_load((REPOSITORY_ROOT / "macos/project.yml").read_text(encoding="utf-8"))
        lanes = lanes_for(changed_paths(arguments.base), player_source_prefixes(project))
    else:
        lanes = dict.fromkeys(ALL_LANES, True)

    lines = [f"{lane}={'true' if wanted else 'false'}" for lane, wanted in lanes.items()]
    print("\n".join(lines))
    if output_path := os.environ.get("GITHUB_OUTPUT"):
        with open(output_path, "a", encoding="utf-8") as output:
            output.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
