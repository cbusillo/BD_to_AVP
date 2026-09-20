import unittest
from pathlib import Path

import yaml

from scripts.ci_lanes import lanes_for, player_source_prefixes

PROJECT = yaml.safe_load((Path(__file__).resolve().parents[1] / "macos/project.yml").read_text(encoding="utf-8"))
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


if __name__ == "__main__":
    unittest.main()
