import importlib
import re
import subprocess
import tempfile
import tomllib
import unittest

from pathlib import Path

from scripts import release


REPO_ROOT = Path(__file__).resolve().parents[1]
briefcase = importlib.import_module("briefcase")
briefcase_config = importlib.import_module("briefcase.config")
briefcase_console = importlib.import_module("briefcase.console")


def make_release_files(root: Path, *, version: str = "1.2.3", build: str = "10") -> tuple[Path, Path]:
    pyproject_path = root / "pyproject.toml"
    lock_path = root / "uv.lock"
    pyproject_path.write_text(
        f"""\
[project]
name = "bd_to_avp"
version = "{version}"

[tool.briefcase]
project_name = "Test"

[tool.briefcase.app.bd-to-avp.macOS.info]
CFBundleVersion = "{build}"
""",
        encoding="utf-8",
    )
    lock_path.write_text(
        f"""\
version = 1

[[package]]
name = "bd-to-avp"
version = "{version}"
source = {{ editable = "." }}
""",
        encoding="utf-8",
    )
    return pyproject_path, lock_path


def fake_lock_runner(stage_root: Path, _uv_executable: str) -> None:
    pyproject = tomllib.loads((stage_root / "pyproject.toml").read_text(encoding="utf-8"))
    version = pyproject["project"]["version"]
    lock_path = stage_root / "uv.lock"
    lock_text = lock_path.read_text(encoding="utf-8")
    lock_text = re.sub(
        r'(?m)(^\[\[package\]\]\nname = "bd-to-avp"\n)version = "[^"]+"',
        rf'\g<1>version = "{version}"',
        lock_text,
        count=1,
    )
    lock_path.write_text(lock_text, encoding="utf-8")


class ReleaseMetadataTests(unittest.TestCase):
    def test_repository_uses_project_version_for_briefcase_043(self) -> None:
        with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
            pyproject = tomllib.load(handle)

        self.assertEqual(briefcase.__version__, "0.4.3")
        self.assertNotIn("version", pyproject["tool"]["briefcase"])
        _, apps = briefcase_config.parse_config(
            REPO_ROOT / "pyproject.toml",
            "macOS",
            "dmg",
            briefcase_console.Console(input_enabled=False),
        )
        self.assertEqual(str(apps["bd-to-avp"]["version"]), pyproject["project"]["version"])
        self.assertEqual(
            apps["bd-to-avp"]["info"]["CFBundleVersion"],
            pyproject["tool"]["briefcase"]["app"]["bd-to-avp"]["macOS"]["info"]["CFBundleVersion"],
        )

    def test_repository_is_prepared_for_first_sparkle_rc(self) -> None:
        metadata = release.load_release_metadata()

        self.assertEqual(metadata.package_version, "0.2.143rc4")
        self.assertEqual(metadata.build_version, "144")
        self.assertEqual(metadata.channel, "rc")
        self.assertFalse(metadata.publish_pypi)

    def test_metadata_derives_release_policy_from_committed_version(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            pyproject_path, lock_path = make_release_files(Path(temp_dir), version="1.2.4rc2", build="11")

            metadata = release.load_release_metadata(pyproject_path, lock_path)

        self.assertEqual(metadata.release_tag, "v1.2.4rc2")
        self.assertEqual(metadata.channel, "rc")
        self.assertTrue(metadata.prerelease)
        self.assertFalse(metadata.make_latest)
        self.assertFalse(metadata.publish_pypi)

    def test_metadata_rejects_lockfile_version_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pyproject_path, lock_path = make_release_files(root)
            lock_text = lock_path.read_text(encoding="utf-8")
            lock_path.write_text(
                lock_text.replace('version = "1.2.3"', 'version = "1.2.2"'),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(release.ReleaseError, "does not match"):
                release.load_release_metadata(pyproject_path, lock_path)

    def test_metadata_rejects_noncanonical_release_versions(self) -> None:
        for value in ("1.2", "1.2.3.post1", "01.2.3", "1.2.3RC1"):
            with self.subTest(value=value), self.assertRaises(release.ReleaseError):
                release.parse_release_version(value)


class ReleasePreparationTests(unittest.TestCase):
    def test_prepare_updates_version_build_and_lock_together(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            pyproject_path, lock_path = make_release_files(Path(temp_dir))

            metadata = release.prepare_release(
                "1.2.4rc1",
                "11",
                pyproject_path=pyproject_path,
                lock_path=lock_path,
                lock_runner=fake_lock_runner,
            )

            with pyproject_path.open("rb") as handle:
                pyproject = tomllib.load(handle)
            with lock_path.open("rb") as handle:
                lock = tomllib.load(handle)
        self.assertEqual(metadata.package_version, "1.2.4rc1")
        self.assertEqual(metadata.build_version, "11")
        self.assertEqual(pyproject["project"]["version"], "1.2.4rc1")
        self.assertEqual(
            pyproject["tool"]["briefcase"]["app"]["bd-to-avp"]["macOS"]["info"]["CFBundleVersion"],
            "11",
        )
        self.assertEqual(lock["package"][0]["version"], "1.2.4rc1")

    def test_prepare_leaves_files_unchanged_when_lock_refresh_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            pyproject_path, lock_path = make_release_files(Path(temp_dir))
            original_pyproject = pyproject_path.read_bytes()
            original_lock = lock_path.read_bytes()

            def fail_lock(_stage_root: Path, _uv_executable: str) -> None:
                raise subprocess.CalledProcessError(1, ["uv", "lock"])

            with self.assertRaises(subprocess.CalledProcessError):
                release.prepare_release(
                    "1.2.4rc1",
                    "11",
                    pyproject_path=pyproject_path,
                    lock_path=lock_path,
                    lock_runner=fail_lock,
                )

            self.assertEqual(pyproject_path.read_bytes(), original_pyproject)
            self.assertEqual(lock_path.read_bytes(), original_lock)

    def test_prepare_requires_monotonic_version_and_build(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            pyproject_path, lock_path = make_release_files(Path(temp_dir))

            with self.assertRaisesRegex(release.ReleaseError, "must be newer"):
                release.prepare_release(
                    "1.2.3",
                    "11",
                    pyproject_path=pyproject_path,
                    lock_path=lock_path,
                    lock_runner=fake_lock_runner,
                )
            with self.assertRaisesRegex(release.ReleaseError, "must be greater"):
                release.prepare_release(
                    "1.2.4rc1",
                    "10",
                    pyproject_path=pyproject_path,
                    lock_path=lock_path,
                    lock_runner=fake_lock_runner,
                )

    def test_prepare_supports_rc_to_rc_to_stable_but_not_back_to_rc(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            pyproject_path, lock_path = make_release_files(
                Path(temp_dir),
                version="1.2.4rc1",
                build="11",
            )

            rc2 = release.prepare_release(
                "1.2.4rc2",
                "12",
                pyproject_path=pyproject_path,
                lock_path=lock_path,
                lock_runner=fake_lock_runner,
            )
            stable = release.prepare_release(
                "1.2.4",
                "13",
                pyproject_path=pyproject_path,
                lock_path=lock_path,
                lock_runner=fake_lock_runner,
            )

            self.assertEqual(rc2.package_version, "1.2.4rc2")
            self.assertEqual(stable.package_version, "1.2.4")
            with self.assertRaisesRegex(release.ReleaseError, "must be newer"):
                release.prepare_release(
                    "1.2.4rc3",
                    "14",
                    pyproject_path=pyproject_path,
                    lock_path=lock_path,
                    lock_runner=fake_lock_runner,
                )


if __name__ == "__main__":
    unittest.main()
