import plistlib
import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from bd_to_avp.modules.config import Config
from bd_to_avp.modules.util import get_bundled_app_version


class BundledAppVersionTests(unittest.TestCase):
    def test_reads_version_from_packaged_app_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source_path = self._make_packaged_source(
                Path(temporary_directory),
                {
                    "BluRayToVisionProEngineBundled": True,
                    "CFBundleShortVersionString": "0.3.2b6",
                },
            )

            self.assertEqual(get_bundled_app_version(source_path), "0.3.2b6")

    def test_rejects_unmarked_app_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source_path = self._make_packaged_source(
                Path(temporary_directory),
                {"CFBundleShortVersionString": "0.3.2b6"},
            )

            self.assertIsNone(get_bundled_app_version(source_path))

    def test_config_prefers_packaged_app_version(self) -> None:
        app = object.__new__(Config.App)
        app.shortname = "bd_to_avp"

        with (
            patch("bd_to_avp.modules.config.get_bundled_app_version", return_value="0.3.2b6"),
            patch("bd_to_avp.modules.config.get_pyproject_data") as get_pyproject_data,
        ):
            self.assertEqual(app.code_version, "0.3.2b6")

        get_pyproject_data.assert_not_called()

    @staticmethod
    def _make_packaged_source(root: Path, info: dict[str, object]) -> Path:
        contents = root / "3D Blu-ray to Vision Pro.app" / "Contents"
        source_path = contents / "Resources" / "app" / "bd_to_avp" / "modules" / "config.py"
        source_path.parent.mkdir(parents=True)
        source_path.write_text("", encoding="utf-8")
        with (contents / "Info.plist").open("wb") as info_file:
            plistlib.dump(info, info_file)
        return source_path


if __name__ == "__main__":
    unittest.main()
