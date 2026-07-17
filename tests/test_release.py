import importlib
import re
import subprocess
import tempfile
import tomllib
import unittest

from pathlib import Path

from scripts import briefcase_macos_signing, release


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


def published_release(tag_name: str, *, prerelease: bool = False, draft: bool = False) -> dict[str, object]:
    return {
        "tag_name": tag_name,
        "draft": draft,
        "prerelease": prerelease,
        "published_at": None if draft else "2026-07-11T00:00:00Z",
    }


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
    def test_repository_keeps_gui_dependency_out_of_cli_base(self) -> None:
        with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
            pyproject = tomllib.load(handle)

        pyside_requirement = "pyside6>=6.7.1,<6.10"
        self.assertNotIn(pyside_requirement, pyproject["project"]["dependencies"])
        self.assertEqual(pyproject["project"]["optional-dependencies"]["gui"], [pyside_requirement])
        self.assertIn(pyside_requirement, pyproject["dependency-groups"]["dev"])
        self.assertIn(pyside_requirement, pyproject["tool"]["briefcase"]["app"]["bd-to-avp"]["requires"])

    def test_repository_uses_expected_briefcase_version(self) -> None:
        with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
            pyproject = tomllib.load(handle)

        expected_version = briefcase_macos_signing.EXPECTED_BRIEFCASE_VERSION
        self.assertEqual(briefcase.__version__, expected_version)
        self.assertIn(f"briefcase=={expected_version}", pyproject["dependency-groups"]["dev"])
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
        self.assertEqual(apps["bd-to-avp"]["min_os_version"], "14.0")

    def test_repository_is_prepared_for_stable_release(self) -> None:
        metadata = release.load_release_metadata()

        self.assertEqual(metadata.package_version, "0.2.143")
        self.assertEqual(metadata.build_version, "146")
        self.assertEqual(metadata.release_name, "v0.2.143")
        self.assertEqual(metadata.channel, "stable")
        self.assertTrue(metadata.publish_pypi)

    def test_metadata_derives_release_policy_from_committed_version(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            pyproject_path, lock_path = make_release_files(Path(temp_dir), version="1.2.4rc2", build="11")

            metadata = release.load_release_metadata(pyproject_path, lock_path)

        self.assertEqual(metadata.release_tag, "v1.2.4rc2")
        self.assertEqual(metadata.release_name, "v1.2.4rc2")
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


class ReleaseNotesBaseTests(unittest.TestCase):
    def test_stable_uses_previous_stable_even_when_legacy_history_diverged(self) -> None:
        history = [
            [
                published_release("v0.2.142"),
                published_release("v0.2.143rc4", prerelease=True),
                published_release("v0.2.143rc5", prerelease=True),
            ]
        ]

        def unexpected_ancestor_check(_tag_name: str, _head_ref: str) -> bool:
            self.fail("Stable release-note selection must not require commit ancestry.")

        selected = release.select_release_notes_base(
            "v0.2.143",
            history,
            "stable-head",
            tag_exists=lambda tag_name: tag_name == "v0.2.142",
            is_ancestor=unexpected_ancestor_check,
        )

        self.assertEqual(selected, "v0.2.142")

    def test_rc_uses_latest_published_ancestor(self) -> None:
        history = [
            published_release("v1.2.3"),
            published_release("v1.2.4rc1", prerelease=True),
            published_release("v1.2.4rc2", prerelease=True),
        ]
        ancestors = {"v1.2.3", "v1.2.4rc1"}

        selected = release.select_release_notes_base(
            "v1.2.4rc3",
            history,
            "rc-head",
            tag_exists=lambda _tag_name: True,
            is_ancestor=lambda tag_name, _head_ref: tag_name in ancestors,
        )

        self.assertEqual(selected, "v1.2.4rc1")

    def test_first_rc_after_stable_uses_stable_ancestor(self) -> None:
        history = [
            published_release("v1.2.3rc9", prerelease=True),
            published_release("v1.2.3"),
        ]

        selected = release.select_release_notes_base(
            "v1.2.4rc1",
            history,
            "rc-head",
            tag_exists=lambda _tag_name: True,
            is_ancestor=lambda _tag_name, _head_ref: True,
        )

        self.assertEqual(selected, "v1.2.3")

    def test_selection_ignores_drafts_and_non_project_tags(self) -> None:
        history = [
            published_release("untagged-stale-draft", draft=True),
            published_release("safety/pre-toolchain-state"),
            published_release("v1.2.3"),
        ]

        selected = release.select_release_notes_base(
            "v1.2.4",
            history,
            "stable-head",
            tag_exists=lambda tag_name: tag_name == "v1.2.3",
            is_ancestor=lambda _tag_name, _head_ref: False,
        )

        self.assertEqual(selected, "v1.2.3")

    def test_selection_rejects_missing_or_duplicate_published_tags(self) -> None:
        with self.assertRaisesRegex(release.ReleaseError, "missing from the checkout"):
            release.select_release_notes_base(
                "v1.2.4",
                [published_release("v1.2.3")],
                "stable-head",
                tag_exists=lambda _tag_name: False,
                is_ancestor=lambda _tag_name, _head_ref: False,
            )

        duplicate_history = [published_release("v1.2.3"), published_release("v1.2.3")]
        with self.assertRaisesRegex(release.ReleaseError, "Multiple published GitHub Releases"):
            release.select_release_notes_base(
                "v1.2.4",
                duplicate_history,
                "stable-head",
                tag_exists=lambda _tag_name: True,
                is_ancestor=lambda _tag_name, _head_ref: False,
            )

    def test_first_stable_release_without_stable_history_has_no_base(self) -> None:
        selected = release.select_release_notes_base(
            "v1.0.0",
            [published_release("v1.0.0rc1", prerelease=True)],
            "stable-head",
            tag_exists=lambda _tag_name: True,
            is_ancestor=lambda _tag_name, _head_ref: True,
        )

        self.assertEqual(selected, "")


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
