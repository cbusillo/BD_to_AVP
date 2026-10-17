"""Decide which expensive CI lanes a change needs.

A pull request, and a merge-train candidate built from pull requests, pays only
for the lanes whose inputs it touches. Every lane still runs on a push to main
and on a manual run, so nothing reaches a release unexercised. Anything this
script does not recognise, including a candidate whose base on main cannot be
found, runs everything.
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
# Launchplane pushes merge-train candidates here: main plus the pull requests in the batch.
TRAIN_CANDIDATE_REF_PREFIX = "refs/heads/launchplane/train/"
MAIN_REMOTE_REF = "refs/remotes/origin/main"


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


def changed_paths(base: str, repository: Path = REPOSITORY_ROOT) -> list[str]:
    # A rename removes an input as surely as a deletion, so list both of its paths
    # whatever diff.renames says. NUL separation keeps unusual file names whole.
    output = subprocess.run(
        ["git", "diff", "--name-only", "--no-renames", "-z", f"{base}...HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [path for path in output.split("\0") if path]


def git_output(arguments: list[str], repository: Path) -> str | None:
    result = subprocess.run(["git", *arguments], cwd=repository, capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def train_candidate_base(repository: Path = REPOSITORY_ROOT) -> str | None:
    """The main commit a merge-train candidate was built on, or None when it cannot be trusted."""
    base = git_output(["merge-base", MAIN_REMOTE_REF, "HEAD"], repository)
    head = git_output(["rev-parse", "HEAD"], repository)
    # A candidate that is already main, or main itself, has no pull-request changes to
    # select lanes from; treat that like an unknown base rather than run nothing.
    if not base or not head or base == head:
        return None
    return base


def lane_base(event: str, ref: str, pull_request_base: str, repository: Path = REPOSITORY_ROOT) -> str | None:
    """The commit to diff against, or None to run every lane."""
    if event == "pull_request":
        return pull_request_base or None
    if event == "push" and ref.startswith(TRAIN_CANDIDATE_REF_PREFIX):
        return train_candidate_base(repository)
    return None


def selected_lanes(
    event: str,
    ref: str,
    pull_request_base: str,
    repository: Path = REPOSITORY_ROOT,
) -> dict[str, bool]:
    base = lane_base(event, ref, pull_request_base, repository)
    if base is None:
        return dict.fromkeys(ALL_LANES, True)
    project = yaml.safe_load((repository / "macos/project.yml").read_text(encoding="utf-8"))
    return lanes_for(changed_paths(base, repository), player_source_prefixes(project))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", required=True, help="GitHub event name.")
    parser.add_argument("--ref", default="", help="Git ref the run was triggered for (github.ref).")
    parser.add_argument("--base", default="", help="Pull-request base commit.")
    arguments = parser.parse_args()

    lanes = selected_lanes(arguments.event, arguments.ref, arguments.base)

    lines = [f"{lane}={'true' if wanted else 'false'}" for lane, wanted in lanes.items()]
    print("\n".join(lines))
    if output_path := os.environ.get("GITHUB_OUTPUT"):
        with open(output_path, "a", encoding="utf-8") as output:
            output.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
