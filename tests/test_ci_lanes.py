import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from scripts.ci_lanes import ALL_LANES, changed_paths, lanes_for, player_source_prefixes, selected_lanes

PROJECT_SPEC = Path(__file__).resolve().parents[1] / "macos/project.yml"
PROJECT = yaml.safe_load(PROJECT_SPEC.read_text(encoding="utf-8"))
PLAYER = player_source_prefixes(PROJECT)


class CILaneSelectionTests(unittest.TestCase):
    def lanes(self, *paths: str) -> dict[str, bool]:
        return lanes_for(paths, PLAYER)

    def test_recorded_evidence_and_documents_need_no_product_build(self) -> None:
        self.assertEqual(
            self.lanes(
                "docs/qualification/release-evidence-v1.json",
                "docs/release-evidence/v0.3.3-beta.5/release-receipt.json",
                "docs/release-process.md",
                "README.md",
                "tests/test_release.py",
            ),
            {"native": False, "player": False},
        )

    def test_worker_and_mac_app_changes_build_the_mac_app_but_not_the_player(self) -> None:
        for path in (
            "bd_to_avp/modules/video.py",
            "bd_to_avp/bin/edge264_test",
            "macos/BluRayToVisionPro/Worker/WorkerProcessClient.swift",
            "scripts/native_app.py",
            "pyproject.toml",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.lanes(path), {"native": True, "player": False})

    def test_player_only_changes_build_the_player_but_not_the_mac_app(self) -> None:
        for path in (
            "macos/BDToAVPPlayer/Library/LibraryView.swift",
            "macos/BDToAVPPlayerUITests/BDToAVPPlayerUITests.swift",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.lanes(path), {"native": False, "player": True})

    def test_sources_shared_by_both_apps_build_both(self) -> None:
        for path in (
            "macos/RelaySessionCore/MovieLibraryTrustStore.swift",
            "macos/BluRayToVisionPro/Relay/RelayHTTP.swift",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.lanes(path), {"native": True, "player": True})

    def test_the_project_spec_and_the_lane_definitions_build_everything(self) -> None:
        for path in ("macos/project.yml", ".github/workflows/ci.yml", "scripts/ci_lanes.py"):
            with self.subTest(path=path):
                self.assertEqual(self.lanes("docs/notes.md", path), {"native": True, "player": True})

    def test_an_unrecognised_path_is_never_treated_as_inert(self) -> None:
        self.assertTrue(self.lanes("some/new/top-level/file.bin")["native"])

    def test_every_source_the_player_compiles_selects_the_player_lane(self) -> None:
        # Read from the Xcode project spec, so a new player source folder cannot be missed.
        self.assertTrue(PLAYER)
        for prefix in PLAYER:
            with self.subTest(source=prefix):
                self.assertTrue(self.lanes(f"{prefix.rstrip('/')}/Example.swift")["player"])

    def test_ci_build_configuration_changes_select_every_lane_that_uses_them(self) -> None:
        workflow = yaml.safe_load(
            (Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        )
        for lane, job in workflow["jobs"].items():
            if lane not in self.lanes():
                continue
            for step in job.get("steps", []):
                configuration = step.get("env", {}).get("XCODE_XCCONFIG_FILE")
                if configuration:
                    path = configuration.removeprefix("${{ github.workspace }}/")
                    with self.subTest(lane=lane, configuration=path):
                        self.assertTrue(self.lanes(path)[lane])


class ChangedPathDiscoveryTests(unittest.TestCase):
    """Run the real Git diff, configured to detect renames, against a scratch repository."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="ci-lanes-")
        self.addCleanup(directory.cleanup)
        self.repository = Path(directory.name)
        # Keep the developer's own Git configuration out of the scratch repository.
        isolated = patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
        isolated.start()
        self.addCleanup(isolated.stop)
        self.git("init", "--quiet", "--initial-branch=main")
        self.git("config", "user.name", "CI lanes test")
        self.git("config", "user.email", "ci-lanes@example.invalid")
        self.git("config", "diff.renames", "true")

    def git(self, *arguments: str) -> None:
        subprocess.run(["git", *arguments], cwd=self.repository, check=True, capture_output=True)

    def commit_files(self, files: dict[str, str]) -> None:
        for path, text in files.items():
            (self.repository / path).parent.mkdir(parents=True, exist_ok=True)
            (self.repository / path).write_text(text, encoding="utf-8")
        self.git("add", "--all")
        self.git("commit", "--quiet", "--message", "base")
        self.git("branch", "base")

    def move(self, source: str, destination: str) -> list[str]:
        (self.repository / destination).parent.mkdir(parents=True, exist_ok=True)
        self.git("mv", source, destination)
        self.git("commit", "--quiet", "--message", "move")
        return changed_paths("base", self.repository)

    def test_moving_a_player_source_out_of_the_player_still_builds_the_player(self) -> None:
        self.commit_files({"macos/BDToAVPPlayer/Library/Example.swift": "struct Example {}\n"})
        paths = self.move("macos/BDToAVPPlayer/Library/Example.swift", "macos/BluRayToVisionPro/Example.swift")
        self.assertEqual(lanes_for(paths, PLAYER), {"native": True, "player": True})

    def test_moving_a_product_source_into_docs_still_builds_the_mac_app(self) -> None:
        self.commit_files({"bd_to_avp/modules/example.py": "VALUE = 1\n"})
        paths = self.move("bd_to_avp/modules/example.py", "docs/example.py")
        self.assertEqual(lanes_for(paths, PLAYER), {"native": True, "player": False})

    def test_renaming_a_document_still_needs_no_product_build(self) -> None:
        self.commit_files({"docs/old-notes.md": "notes\n"})
        paths = self.move("docs/old-notes.md", "docs/new-notes.md")
        self.assertEqual(lanes_for(paths, PLAYER), {"native": False, "player": False})

    def test_file_names_with_tabs_and_newlines_are_single_paths(self) -> None:
        unusual = "macos/BDToAVPPlayer/Library/Odd\tname\nhere.swift"
        self.commit_files({"README.md": "readme\n"})
        (self.repository / unusual).parent.mkdir(parents=True)
        (self.repository / unusual).write_text("struct Odd {}\n", encoding="utf-8")
        self.git("add", "--all")
        self.git("commit", "--quiet", "--message", "add")
        paths = changed_paths("base", self.repository)
        self.assertEqual(paths, [unusual])
        self.assertEqual(lanes_for(paths, PLAYER), {"native": False, "player": True})


class TrainCandidateLaneSelectionTests(unittest.TestCase):
    """A merge-train candidate is main plus the batch; it is diffed against the main it was built on."""

    TRAIN_REF = "refs/heads/launchplane/train/cbusillo/bd_to_avp/main/merge-train-batch-example"
    EVERY_LANE = dict.fromkeys(ALL_LANES, True)

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="ci-lanes-train-")
        self.addCleanup(directory.cleanup)
        self.repository = Path(directory.name)
        isolated = patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
        isolated.start()
        self.addCleanup(isolated.stop)
        self.git("init", "--quiet", "--initial-branch=main")
        self.git("config", "user.name", "CI lanes test")
        self.git("config", "user.email", "ci-lanes@example.invalid")
        # The real project spec, so the player's sources are read the way CI reads them.
        self.commit({"macos/project.yml": PROJECT_SPEC.read_text(encoding="utf-8"), "README.md": "readme\n"})
        # What a full-history checkout fetches: main as the remote-tracking ref.
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        self.git("checkout", "--quiet", "-b", "candidate")

    def git(self, *arguments: str) -> None:
        subprocess.run(["git", *arguments], cwd=self.repository, check=True, capture_output=True)

    def commit(self, files: dict[str, str]) -> None:
        for path, text in files.items():
            (self.repository / path).parent.mkdir(parents=True, exist_ok=True)
            (self.repository / path).write_text(text, encoding="utf-8")
        self.git("add", "--all")
        self.git("commit", "--quiet", "--message", "change")

    def lanes(self, event: str = "push", ref: str = TRAIN_REF) -> dict[str, bool]:
        return selected_lanes(event, ref, "", self.repository)

    def test_a_candidate_that_does_not_touch_the_player_skips_the_player(self) -> None:
        self.commit({"bd_to_avp/modules/example.py": "VALUE = 1\n"})
        self.assertEqual(self.lanes(), {"native": True, "player": False})

    def test_a_candidate_that_touches_the_player_runs_the_player(self) -> None:
        self.commit({"bd_to_avp/modules/example.py": "VALUE = 1\n"})
        self.commit({"macos/BDToAVPPlayer/Library/Example.swift": "struct Example {}\n"})
        self.assertTrue(self.lanes()["player"])

    def test_main_moving_after_the_candidate_was_built_does_not_hide_its_changes(self) -> None:
        self.commit({"macos/BDToAVPPlayer/Library/Example.swift": "struct Example {}\n"})
        self.git("checkout", "--quiet", "main")
        self.commit({"docs/later.md": "later\n"})
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        self.git("checkout", "--quiet", "candidate")
        self.assertTrue(self.lanes()["player"])

    def test_a_candidate_without_a_known_base_on_main_runs_every_lane(self) -> None:
        self.commit({"docs/notes.md": "notes\n"})
        self.git("update-ref", "-d", "refs/remotes/origin/main")
        self.assertEqual(self.lanes(), self.EVERY_LANE)

    def test_a_candidate_with_no_changes_of_its_own_runs_every_lane(self) -> None:
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        self.assertEqual(self.lanes(), self.EVERY_LANE)

    def test_a_pull_request_uses_the_same_rule_against_its_own_base(self) -> None:
        base = subprocess.run(
            ["git", "rev-parse", "main"], cwd=self.repository, check=True, capture_output=True, text=True
        ).stdout.strip()
        self.commit({"bd_to_avp/modules/example.py": "VALUE = 1\n"})
        self.assertEqual(
            selected_lanes("pull_request", "refs/pull/1/merge", base, self.repository),
            {"native": True, "player": False},
        )

    def test_pushes_to_main_and_manual_runs_still_run_every_lane(self) -> None:
        self.commit({"docs/notes.md": "notes\n"})
        for event, ref in (
            ("push", "refs/heads/main"),
            ("push", "refs/heads/launchplane/construct/example"),
            ("workflow_dispatch", self.TRAIN_REF),
        ):
            with self.subTest(event=event, ref=ref):
                self.assertEqual(self.lanes(event, ref), self.EVERY_LANE)


if __name__ == "__main__":
    unittest.main()
