import shutil
import tempfile
import unittest
from pathlib import Path

from scripts import framework_dsyms

VENDOR = framework_dsyms.REPOSITORY_ROOT / "macos/Vendor"


class FrameworkDsymTests(unittest.TestCase):
    def test_every_player_framework_ships_matching_dsyms(self) -> None:
        xcframeworks = sorted(VENDOR.glob("*.xcframework"), key=lambda path: path.name)
        self.assertTrue(xcframeworks)
        for xcframework in xcframeworks:
            with self.subTest(xcframework.name):
                framework_dsyms.verify(xcframework)

    def test_dsym_from_another_slice_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            copy = Path(temporary) / "Edge264MVC.xcframework"
            shutil.copytree(VENDOR / copy.name, copy, symlinks=True)
            device = copy / "xros-arm64/dSYMs/Edge264MVC.framework.dSYM"
            shutil.rmtree(device)
            shutil.copytree(copy / "xros-arm64_x86_64-simulator/dSYMs/Edge264MVC.framework.dSYM", device)

            with self.assertRaisesRegex(RuntimeError, "does not match"):
                framework_dsyms.verify(copy)


if __name__ == "__main__":
    unittest.main()
