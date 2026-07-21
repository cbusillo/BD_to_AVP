import hashlib
import importlib
import json
import re
import stat
import subprocess
import sys
import tempfile
import tomllib
import unittest

from pathlib import Path

from scripts import briefcase_macos_signing, release
from scripts.beta3_recovery_evidence import BETA3_RECOVERY_EVIDENCE_PATH, Beta3RecoveryEvidenceError
from scripts.production_identity import PRODUCTION_SPARKLE_PUBLIC_KEY


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
project_name = "3D Blu-ray to Vision Pro"
bundle = "com.shinycomputers"

[tool.briefcase.app.bd-to-avp]
formal_name = "3D Blu-ray to Vision Pro"

[tool.briefcase.app.bd-to-avp.macOS.info]
CFBundleVersion = "{build}"
BDToAVPDistributionChannel = "direct"
SUFeedURL = "https://cbusillo.github.io/BD_to_AVP/appcast.xml"
SUPublicEDKey = "{PRODUCTION_SPARKLE_PUBLIC_KEY}"
SUAllowsAutomaticUpdates = false
SUVerifyUpdateBeforeExtraction = true
""",
        encoding="utf-8",
    )
    (root / "sparkle-public-ed-key.txt").write_text(f"{PRODUCTION_SPARKLE_PUBLIC_KEY}\n", encoding="utf-8")
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


def make_macos_project(root: Path, *, version: str = "1.2.3", build: str = "10") -> Path:
    project_path = root / "project.yml"
    project_path.write_text(
        f"""\
targets:
  BluRayToVisionPro:
    settings:
      base:
        CURRENT_PROJECT_VERSION: {build}
        BD_TO_AVP_SUPPORT_DIAGNOSTICS_ENDPOINT: ""
        MARKETING_VERSION: {version}
        PRODUCT_BUNDLE_IDENTIFIER: com.shinycomputers.bd-to-avp
        PRODUCT_NAME: 3D Blu-ray to Vision Pro
      configs:
        Release:
          INFOPLIST_FILE: BluRayToVisionPro/Info-Release.plist
""",
        encoding="utf-8",
    )
    return project_path


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


def make_recovery_evidence(root: Path, content: bytes | None = None) -> Path:
    evidence_path = root / "v0.3.0-beta.3-recovery.json"
    evidence_path.write_bytes(content if content is not None else BETA3_RECOVERY_EVIDENCE_PATH.read_bytes())
    return evidence_path


def skip_remote_verification(_evidence: object) -> None:
    return None


class ReleaseMetadataTests(unittest.TestCase):
    def test_repository_pins_uv_for_reproducible_lock_refresh(self) -> None:
        with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
            pyproject = tomllib.load(handle)

        self.assertEqual(pyproject["tool"]["uv"]["required-version"], "==0.11.29")

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

    def test_repository_is_prepared_and_frozen_for_beta4(self) -> None:
        metadata = release.load_release_metadata()

        self.assertEqual(metadata.package_version, "0.3.0b4")
        self.assertEqual(metadata.public_version, "0.3.0-beta.4")
        self.assertEqual(metadata.build_version, "149")
        self.assertEqual(metadata.release_tag, "v0.3.0-beta.4")
        self.assertEqual(metadata.release_name, "v0.3.0-beta.4")
        self.assertEqual(metadata.dmg_name, "3D-Blu-ray-to-Vision-Pro-0.3.0-beta.4.dmg")
        self.assertEqual(metadata.channel, "beta")
        self.assertTrue(metadata.prerelease)
        self.assertFalse(metadata.make_latest)
        self.assertFalse(metadata.publish_pypi)

        freeze_policy = json.loads((REPO_ROOT / ".github" / "release-freezes.json").read_text(encoding="utf-8"))
        self.assertEqual(freeze_policy["frozen_release_tags"]["v0.3.0-beta.4"]["issue"], 316)

        cut_packet = (REPO_ROOT / "docs" / "0.3.0-beta.4-cut-packet.md").read_text(encoding="utf-8")
        self.assertIn("`0.3.0b4`", cut_packet)
        self.assertIn("Build `149`", cut_packet)
        self.assertIn("PR #313", cut_packet)
        self.assertIn("Privacy rules version `4`", cut_packet)
        self.assertIn("issue #316", cut_packet)

    def test_repository_beta3_recovery_evidence_is_exact(self) -> None:
        evidence = release.validate_beta3_recovery_evidence()

        self.assertEqual(evidence["schema_version"], 2)
        self.assertEqual(evidence["repository"], "cbusillo/BD_to_AVP")
        self.assertEqual(evidence["transition"]["target"]["release_tag"], "v0.3.0-beta.3")

    def test_reviewed_source_identity_matches_the_pre_recovery_base_commit(self) -> None:
        evidence = release.validate_beta3_recovery_evidence()
        source_identity = evidence["source_identity"]
        base_commit = source_identity["base_commit"]

        self.assertEqual(
            subprocess.check_output(
                ["git", "show", "-s", "--format=%T", base_commit],
                cwd=REPO_ROOT,
                text=True,
            ).strip(),
            source_identity["tree"],
        )
        for relative_path, expected_digest in source_identity["files"].items():
            content = subprocess.check_output(
                ["git", "show", f"{base_commit}:{relative_path}"],
                cwd=REPO_ROOT,
            )
            self.assertEqual(hashlib.sha256(content).hexdigest(), expected_digest)

    def test_beta3_seed_metadata_uses_the_production_beta_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pyproject_path, lock_path = make_release_files(root, version="0.3.0b3", build="148")
            macos_project_path = make_macos_project(root, version="0.3.0b3", build="148")

            metadata = release.load_release_metadata(pyproject_path, lock_path, macos_project_path)

        self.assertEqual(metadata.package_version, "0.3.0b3")
        self.assertEqual(metadata.public_version, "0.3.0-beta.3")
        self.assertEqual(metadata.build_version, "148")
        self.assertEqual(metadata.release_tag, "v0.3.0-beta.3")
        self.assertEqual(metadata.release_name, "v0.3.0-beta.3")
        self.assertEqual(metadata.dmg_name, "3D-Blu-ray-to-Vision-Pro-0.3.0-beta.3.dmg")
        self.assertEqual(metadata.channel, "beta")
        self.assertTrue(metadata.prerelease)
        self.assertFalse(metadata.make_latest)
        self.assertFalse(metadata.publish_pypi)

    def test_metadata_maps_internal_versions_to_public_release_identity(self) -> None:
        cases = (
            ("1.2.4a1", "1.2.4-alpha.1", "alpha", True),
            ("1.2.4b2", "1.2.4-beta.2", "beta", True),
            ("1.2.4rc3", "1.2.4-rc.3", "rc", True),
            ("1.2.4", "1.2.4", "stable", False),
        )
        for package_version, public_version, channel, prerelease in cases:
            with self.subTest(package_version=package_version), tempfile.TemporaryDirectory() as temp_dir:
                pyproject_path, lock_path = make_release_files(
                    Path(temp_dir),
                    version=package_version,
                    build="11",
                )

                metadata = release.load_release_metadata(pyproject_path, lock_path)

            self.assertEqual(metadata.package_version, package_version)
            self.assertEqual(metadata.public_version, public_version)
            self.assertEqual(metadata.release_tag, f"v{public_version}")
            self.assertEqual(metadata.release_name, f"v{public_version}")
            self.assertEqual(metadata.dmg_name, f"3D-Blu-ray-to-Vision-Pro-{public_version}.dmg")
            self.assertEqual(metadata.channel, channel)
            self.assertEqual(metadata.prerelease, prerelease)
            self.assertEqual(metadata.make_latest, not prerelease)
            self.assertEqual(metadata.publish_pypi, not prerelease)

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

    def test_metadata_rejects_macos_project_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pyproject_path, lock_path = make_release_files(root)
            macos_project_path = make_macos_project(root, build="9")

            with self.assertRaisesRegex(release.ReleaseError, "CURRENT_PROJECT_VERSION"):
                release.load_release_metadata(pyproject_path, lock_path, macos_project_path)

    def test_metadata_rejects_noncanonical_release_versions(self) -> None:
        for value in (
            "1.2",
            "1.2.3.post1",
            "01.2.3",
            "1.2.3RC1",
            "1.2.3a0",
            "1.2.3b01",
            "1.2.3rc0",
        ):
            with self.subTest(value=value), self.assertRaises(release.ReleaseError):
                release.parse_release_version(value)

    def test_release_tags_use_public_syntax_and_accept_legacy_rc_history(self) -> None:
        self.assertEqual(release.parse_release_tag("v1.2.3-alpha.1").text, "1.2.3a1")
        self.assertEqual(release.parse_release_tag("v1.2.3-beta.2").text, "1.2.3b2")
        self.assertEqual(release.parse_release_tag("v1.2.3-rc.3").text, "1.2.3rc3")
        self.assertEqual(release.parse_release_tag("v0.2.143rc5").text, "0.2.143rc5")
        with self.assertRaises(release.ReleaseError):
            release.parse_release_tag("v1.2.3rc3", allow_legacy_rc=False)

    def test_metadata_rejects_retired_preview_versions(self) -> None:
        for version in ("0.3.0b1", "0.3.0b2"):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as temp_dir:
                pyproject_path, lock_path = make_release_files(Path(temp_dir), version=version)

                with self.assertRaisesRegex(release.ReleaseError, "retired preview identity"):
                    release.load_release_metadata(pyproject_path, lock_path)


class ReleaseNotesBaseTests(unittest.TestCase):
    def test_legacy_stable_form_prereleases_keep_their_github_classification(self) -> None:
        history = [
            published_release("v0.2.137"),
            published_release("v0.2.139", prerelease=True),
            published_release("v0.2.141", prerelease=True),
        ]

        stable_base = release.select_release_notes_base(
            "v0.2.142",
            history,
            "stable-head",
            tag_exists=lambda _tag_name: True,
            is_ancestor=lambda _tag_name, _head_ref: True,
        )
        prerelease_base = release.select_release_notes_base(
            "v0.2.140-rc.1",
            history,
            "rc-head",
            tag_exists=lambda _tag_name: True,
            is_ancestor=lambda _tag_name, _head_ref: True,
        )

        self.assertEqual(stable_base, "v0.2.137")
        self.assertEqual(prerelease_base, "v0.2.139")

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
            published_release("v1.2.4-rc.1", prerelease=True),
            published_release("v1.2.4-rc.2", prerelease=True),
        ]
        ancestors = {"v1.2.3", "v1.2.4-rc.1"}

        selected = release.select_release_notes_base(
            "v1.2.4-rc.3",
            history,
            "rc-head",
            tag_exists=lambda _tag_name: True,
            is_ancestor=lambda tag_name, _head_ref: tag_name in ancestors,
        )

        self.assertEqual(selected, "v1.2.4-rc.1")

    def test_first_rc_after_stable_uses_stable_ancestor(self) -> None:
        history = [
            published_release("v1.2.3rc9", prerelease=True),
            published_release("v1.2.3"),
        ]

        selected = release.select_release_notes_base(
            "v1.2.4-rc.1",
            history,
            "rc-head",
            tag_exists=lambda _tag_name: True,
            is_ancestor=lambda _tag_name, _head_ref: True,
        )

        self.assertEqual(selected, "v1.2.3")

    def test_beta3_live_history_shape_selects_latest_production_ancestor(self) -> None:
        history = [
            published_release("v0.2.139", prerelease=True),
            published_release("v0.2.140"),
            published_release("v0.2.141", prerelease=True),
            published_release("v0.2.142"),
            published_release("v0.2.143"),
            published_release("native-ui-preview-1", prerelease=True),
            published_release("v0.3.0-beta.1", prerelease=True),
            published_release("v0.3.0-beta.2", prerelease=True),
            published_release("v0.3.0-beta.2", prerelease=True),
        ]

        selected = release.select_release_notes_base(
            "v0.3.0-beta.3",
            history,
            "beta-head",
            tag_exists=lambda tag_name: tag_name == "v0.2.143",
            is_ancestor=lambda tag_name, _head_ref: tag_name == "v0.2.143",
        )

        self.assertEqual(selected, "v0.2.143")

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

    def test_selection_rejects_prerelease_flag_that_disagrees_with_tag(self) -> None:
        for tag_name, prerelease in (
            ("v1.2.3-beta.1", False),
            ("v1.2.3", True),
            ("v0.2.140", True),
            ("v0.2.142", True),
        ):
            with (
                self.subTest(tag_name=tag_name),
                self.assertRaisesRegex(
                    release.ReleaseError,
                    "prerelease state disagrees",
                ),
            ):
                release.select_release_notes_base(
                    "v1.2.4",
                    [published_release(tag_name, prerelease=prerelease)],
                    "stable-head",
                    tag_exists=lambda _tag_name: True,
                    is_ancestor=lambda _tag_name, _head_ref: True,
                )

        legacy_duplicate_history = [
            published_release("v0.2.139", prerelease=True),
            published_release("v0.2.139", prerelease=True),
        ]
        with self.assertRaisesRegex(release.ReleaseError, "Multiple published GitHub Releases"):
            release.select_release_notes_base(
                "v0.2.140-rc.1",
                legacy_duplicate_history,
                "rc-head",
                tag_exists=lambda _tag_name: True,
                is_ancestor=lambda _tag_name, _head_ref: True,
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
    def test_atomic_write_preserves_existing_file_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "metadata.toml"
            path.write_text("before\n", encoding="utf-8")
            path.chmod(0o640)

            release._atomic_write(path, b"after\n")

            self.assertEqual(path.read_bytes(), b"after\n")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)

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

    def test_prepare_updates_macos_project_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pyproject_path, lock_path = make_release_files(root)
            macos_project_path = make_macos_project(root)

            metadata = release.prepare_release(
                "1.2.4rc1",
                "11",
                pyproject_path=pyproject_path,
                lock_path=lock_path,
                macos_project_path=macos_project_path,
                lock_runner=fake_lock_runner,
            )

            project_text = macos_project_path.read_text(encoding="utf-8")

        self.assertEqual(metadata.package_version, "1.2.4rc1")
        self.assertIn("MARKETING_VERSION: 1.2.4rc1", project_text)
        self.assertIn("CURRENT_PROJECT_VERSION: 11", project_text)

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

    def test_prepare_rejects_dependency_marker_drift_and_leaves_files_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            pyproject_path, lock_path = make_release_files(Path(temp_dir))
            lock_path.write_text(
                lock_path.read_text(encoding="utf-8")
                + """\

[[package]]
name = "parent"
version = "1.0"
source = { registry = "https://pypi.org/simple" }
dependencies = [
    { name = "child", marker = "sys_platform == 'darwin'" },
]

[[package]]
name = "child"
version = "1.0"
source = { registry = "https://pypi.org/simple" }
""",
                encoding="utf-8",
            )
            original_pyproject = pyproject_path.read_bytes()
            original_lock = lock_path.read_bytes()

            def normalize_dependency_marker(stage_root: Path, uv_executable: str) -> None:
                fake_lock_runner(stage_root, uv_executable)
                staged_lock = stage_root / "uv.lock"
                staged_lock.write_text(
                    staged_lock.read_text(encoding="utf-8").replace(
                        '{ name = "child", marker = "sys_platform == \'darwin\'" }',
                        '{ name = "child" }',
                    ),
                    encoding="utf-8",
                )

            with self.assertRaisesRegex(release.ReleaseError, "changed data other than"):
                release.prepare_release(
                    "1.2.4rc1",
                    "11",
                    pyproject_path=pyproject_path,
                    lock_path=lock_path,
                    lock_runner=normalize_dependency_marker,
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

    def test_prepare_supports_forward_alpha_beta_rc_stable_train(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            pyproject_path, lock_path = make_release_files(
                Path(temp_dir),
                version="1.2.4a1",
                build="11",
            )

            alpha2 = release.prepare_release(
                "1.2.4a2",
                "12",
                pyproject_path=pyproject_path,
                lock_path=lock_path,
                lock_runner=fake_lock_runner,
            )
            beta1 = release.prepare_release(
                "1.2.4b1",
                "13",
                pyproject_path=pyproject_path,
                lock_path=lock_path,
                lock_runner=fake_lock_runner,
            )
            rc1 = release.prepare_release(
                "1.2.4rc1",
                "14",
                pyproject_path=pyproject_path,
                lock_path=lock_path,
                lock_runner=fake_lock_runner,
            )
            stable = release.prepare_release(
                "1.2.4",
                "15",
                pyproject_path=pyproject_path,
                lock_path=lock_path,
                lock_runner=fake_lock_runner,
            )

            self.assertEqual(alpha2.public_version, "1.2.4-alpha.2")
            self.assertEqual(beta1.public_version, "1.2.4-beta.1")
            self.assertEqual(rc1.public_version, "1.2.4-rc.1")
            self.assertEqual(stable.package_version, "1.2.4")
            with self.assertRaisesRegex(release.ReleaseError, "must be newer"):
                release.prepare_release(
                    "1.2.4a3",
                    "16",
                    pyproject_path=pyproject_path,
                    lock_path=lock_path,
                    lock_runner=fake_lock_runner,
                )

    def test_prepare_fails_closed_on_burned_rc_to_beta_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            pyproject_path, lock_path = make_release_files(
                Path(temp_dir),
                version="0.3.0rc1",
                build="147",
            )

            with self.assertRaisesRegex(release.ReleaseError, "must be newer"):
                release.prepare_release(
                    "0.3.0b3",
                    "148",
                    pyproject_path=pyproject_path,
                    lock_path=lock_path,
                    lock_runner=fake_lock_runner,
                )


class Beta3RecoveryTests(unittest.TestCase):
    def test_release_cli_runs_as_a_module_from_a_clean_checkout_root(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-m", "scripts.release", "--help"],
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("recover-beta3", completed.stdout)

    def test_recovery_cli_exposes_no_version_or_build_override(self) -> None:
        args = release.build_parser().parse_args(["recover-beta3"])

        self.assertEqual(args.command, "recover-beta3")
        self.assertFalse(hasattr(args, "version"))
        self.assertFalse(hasattr(args, "build"))
        self.assertFalse(hasattr(args, "pyproject"))
        self.assertFalse(hasattr(args, "lock"))
        self.assertFalse(hasattr(args, "macos_project"))
        self.assertFalse(hasattr(args, "uv"))

    def test_recovery_accepts_only_the_exact_source_target_and_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pyproject_path, lock_path = make_release_files(root, version="0.3.0rc1", build="147")
            macos_project_path = make_macos_project(root, version="0.3.0rc1", build="147")
            evidence_path = make_recovery_evidence(root)

            metadata = release.recover_beta3(
                pyproject_path=pyproject_path,
                lock_path=lock_path,
                macos_project_path=macos_project_path,
                evidence_path=evidence_path,
                lock_runner=fake_lock_runner,
                remote_verifier=skip_remote_verification,
            )

            with pyproject_path.open("rb") as handle:
                pyproject = tomllib.load(handle)
            with lock_path.open("rb") as handle:
                lock = tomllib.load(handle)
            project_text = macos_project_path.read_text(encoding="utf-8")

        self.assertEqual(metadata.package_version, "0.3.0b3")
        self.assertEqual(metadata.public_version, "0.3.0-beta.3")
        self.assertEqual(metadata.build_version, "148")
        self.assertEqual(metadata.release_tag, "v0.3.0-beta.3")
        self.assertEqual(metadata.channel, "beta")
        self.assertEqual(pyproject["project"]["version"], "0.3.0b3")
        self.assertEqual(
            pyproject["tool"]["briefcase"]["app"]["bd-to-avp"]["macOS"]["info"]["CFBundleVersion"],
            "148",
        )
        self.assertEqual(lock["package"][0]["version"], "0.3.0b3")
        self.assertIn("MARKETING_VERSION: 0.3.0b3", project_text)
        self.assertIn("CURRENT_PROJECT_VERSION: 148", project_text)

    def test_recovery_rejects_bad_evidence_before_writes(self) -> None:
        reviewed_evidence = BETA3_RECOVERY_EVIDENCE_PATH.read_bytes()
        evidence_cases: list[tuple[str, bytes | None]] = [
            ("missing", None),
            ("malformed", b"{"),
            (
                "mismatched",
                reviewed_evidence.replace(b'"artifact_count": 0', b'"artifact_count": 1'),
            ),
        ]

        for name, content in evidence_cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                pyproject_path, lock_path = make_release_files(root, version="0.3.0rc1", build="147")
                macos_project_path = make_macos_project(root, version="0.3.0rc1", build="147")
                evidence_path = root / "evidence.json"
                if content is not None:
                    evidence_path.write_bytes(content)
                originals = {path: path.read_bytes() for path in (pyproject_path, lock_path, macos_project_path)}
                lock_called = False

                def unexpected_lock(_stage_root: Path, _uv_executable: str) -> None:
                    nonlocal lock_called
                    lock_called = True

                with self.assertRaises(release.ReleaseError):
                    release.recover_beta3(
                        pyproject_path=pyproject_path,
                        lock_path=lock_path,
                        macos_project_path=macos_project_path,
                        evidence_path=evidence_path,
                        lock_runner=unexpected_lock,
                        remote_verifier=skip_remote_verification,
                    )

                self.assertFalse(lock_called)
                for path, original in originals.items():
                    self.assertEqual(path.read_bytes(), original)

    def test_recovery_rejects_wrong_current_metadata_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pyproject_path, lock_path = make_release_files(root, version="0.3.0rc1", build="146")
            macos_project_path = make_macos_project(root, version="0.3.0rc1", build="146")
            evidence_path = make_recovery_evidence(root)
            originals = {path: path.read_bytes() for path in (pyproject_path, lock_path, macos_project_path)}
            lock_called = False

            def unexpected_lock(_stage_root: Path, _uv_executable: str) -> None:
                nonlocal lock_called
                lock_called = True

            with self.assertRaisesRegex(release.ReleaseError, "requires exact source metadata"):
                release.recover_beta3(
                    pyproject_path=pyproject_path,
                    lock_path=lock_path,
                    macos_project_path=macos_project_path,
                    evidence_path=evidence_path,
                    lock_runner=unexpected_lock,
                    remote_verifier=skip_remote_verification,
                )

            self.assertFalse(lock_called)
            for path, original in originals.items():
                self.assertEqual(path.read_bytes(), original)

    def test_recovery_is_atomic_when_lock_refresh_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pyproject_path, lock_path = make_release_files(root, version="0.3.0rc1", build="147")
            macos_project_path = make_macos_project(root, version="0.3.0rc1", build="147")
            evidence_path = make_recovery_evidence(root)
            originals = {path: path.read_bytes() for path in (pyproject_path, lock_path, macos_project_path)}

            def fail_lock(_stage_root: Path, _uv_executable: str) -> None:
                raise subprocess.CalledProcessError(1, ["uv", "lock"])

            with self.assertRaises(subprocess.CalledProcessError):
                release.recover_beta3(
                    pyproject_path=pyproject_path,
                    lock_path=lock_path,
                    macos_project_path=macos_project_path,
                    evidence_path=evidence_path,
                    lock_runner=fail_lock,
                    remote_verifier=skip_remote_verification,
                )

            for path, original in originals.items():
                self.assertEqual(path.read_bytes(), original)

    def test_recovery_rejects_remote_drift_before_lock_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pyproject_path, lock_path = make_release_files(root, version="0.3.0rc1", build="147")
            macos_project_path = make_macos_project(root, version="0.3.0rc1", build="147")
            evidence_path = make_recovery_evidence(root)
            originals = {path: path.read_bytes() for path in (pyproject_path, lock_path, macos_project_path)}
            lock_called = False

            def reject_remote_state(_evidence: object) -> None:
                raise Beta3RecoveryEvidenceError("remote release state changed")

            def unexpected_lock(_stage_root: Path, _uv_executable: str) -> None:
                nonlocal lock_called
                lock_called = True

            with self.assertRaisesRegex(release.ReleaseError, "remote release state changed"):
                release.recover_beta3(
                    pyproject_path=pyproject_path,
                    lock_path=lock_path,
                    macos_project_path=macos_project_path,
                    evidence_path=evidence_path,
                    lock_runner=unexpected_lock,
                    remote_verifier=reject_remote_state,
                )

            self.assertFalse(lock_called)
            for path, original in originals.items():
                self.assertEqual(path.read_bytes(), original)

    def test_recovery_rechecks_remote_state_immediately_before_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pyproject_path, lock_path = make_release_files(root, version="0.3.0rc1", build="147")
            macos_project_path = make_macos_project(root, version="0.3.0rc1", build="147")
            evidence_path = make_recovery_evidence(root)
            originals = {path: path.read_bytes() for path in (pyproject_path, lock_path, macos_project_path)}
            verification_count = 0
            events: list[str] = []

            def remote_changes_after_staging(_evidence: object) -> None:
                nonlocal verification_count
                verification_count += 1
                events.append(f"verify-{verification_count}")
                if verification_count == 2:
                    raise Beta3RecoveryEvidenceError("remote state changed before commit")

            def observe_transaction(event: str, _path: Path) -> None:
                events.append(event)

            with self.assertRaisesRegex(release.ReleaseError, "remote state changed before commit"):
                release.recover_beta3(
                    pyproject_path=pyproject_path,
                    lock_path=lock_path,
                    macos_project_path=macos_project_path,
                    evidence_path=evidence_path,
                    lock_runner=fake_lock_runner,
                    remote_verifier=remote_changes_after_staging,
                    transaction_observer=observe_transaction,
                )

            self.assertEqual(verification_count, 2)
            self.assertGreater(
                events.index("verify-2"),
                max(index for index, event in enumerate(events) if event == "file-applied"),
            )
            self.assertNotIn("journal-committed", events)
            for path, original in originals.items():
                self.assertEqual(path.read_bytes(), original)

    def test_recovery_rejects_source_drift_during_remote_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pyproject_path, lock_path = make_release_files(root, version="0.3.0rc1", build="147")
            macos_project_path = make_macos_project(root, version="0.3.0rc1", build="147")
            evidence_path = make_recovery_evidence(root)
            original_pyproject = pyproject_path.read_bytes()

            def mutate_source(_evidence: object) -> None:
                pyproject_path.write_bytes(original_pyproject + b"\n# concurrent drift\n")

            with self.assertRaisesRegex(release.ReleaseError, "changed after identity validation"):
                release.recover_beta3(
                    pyproject_path=pyproject_path,
                    lock_path=lock_path,
                    macos_project_path=macos_project_path,
                    evidence_path=evidence_path,
                    lock_runner=fake_lock_runner,
                    remote_verifier=mutate_source,
                )

    def test_recovery_rejects_target_drift_during_commit_point_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pyproject_path, lock_path = make_release_files(root, version="0.3.0rc1", build="147")
            macos_project_path = make_macos_project(root, version="0.3.0rc1", build="147")
            evidence_path = make_recovery_evidence(root)
            originals = {path: path.read_bytes() for path in (pyproject_path, lock_path, macos_project_path)}
            verification_count = 0

            def mutate_target_at_commit(_evidence: object) -> None:
                nonlocal verification_count
                verification_count += 1
                if verification_count == 2:
                    pyproject_path.write_bytes(originals[pyproject_path])

            with self.assertRaisesRegex(release.ReleaseError, "changed during final validation"):
                release.recover_beta3(
                    pyproject_path=pyproject_path,
                    lock_path=lock_path,
                    macos_project_path=macos_project_path,
                    evidence_path=evidence_path,
                    lock_runner=fake_lock_runner,
                    remote_verifier=mutate_target_at_commit,
                )

            self.assertEqual(verification_count, 2)
            for path, original in originals.items():
                self.assertEqual(path.read_bytes(), original)
            self.assertFalse((root / release.TRANSACTION_JOURNAL_NAME).exists())

    def test_recovery_rejects_production_identity_drift(self) -> None:
        cases = (
            ("feed", "pyproject.toml", "https://cbusillo.github.io/BD_to_AVP/appcast.xml", "https://example.test/feed"),
            ("public-key", "sparkle-public-ed-key.txt", PRODUCTION_SPARKLE_PUBLIC_KEY, "invalid-key"),
            ("bundle", "project.yml", "com.shinycomputers.bd-to-avp", "com.example.changed"),
        )
        for name, filename, original_value, replacement in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                pyproject_path, lock_path = make_release_files(root, version="0.3.0rc1", build="147")
                macos_project_path = make_macos_project(root, version="0.3.0rc1", build="147")
                evidence_path = make_recovery_evidence(root)
                changed_path = root / filename
                changed_path.write_text(
                    changed_path.read_text(encoding="utf-8").replace(original_value, replacement),
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(release.ReleaseError, "source identity is invalid"):
                    release.recover_beta3(
                        pyproject_path=pyproject_path,
                        lock_path=lock_path,
                        macos_project_path=macos_project_path,
                        evidence_path=evidence_path,
                        lock_runner=fake_lock_runner,
                        remote_verifier=skip_remote_verification,
                    )

    def test_recovery_rejects_unrelated_lockfile_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pyproject_path, lock_path = make_release_files(root, version="0.3.0rc1", build="147")
            macos_project_path = make_macos_project(root, version="0.3.0rc1", build="147")
            evidence_path = make_recovery_evidence(root)

            def change_unrelated_lock_data(stage_root: Path, uv_executable: str) -> None:
                fake_lock_runner(stage_root, uv_executable)
                staged_lock = stage_root / "uv.lock"
                staged_lock.write_text(
                    staged_lock.read_text(encoding="utf-8") + '\n[unexpected]\nvalue = "drift"\n',
                    encoding="utf-8",
                )

            with self.assertRaisesRegex(release.ReleaseError, "other than the editable project version"):
                release.recover_beta3(
                    pyproject_path=pyproject_path,
                    lock_path=lock_path,
                    macos_project_path=macos_project_path,
                    evidence_path=evidence_path,
                    lock_runner=change_unrelated_lock_data,
                    remote_verifier=skip_remote_verification,
                )

    def test_recovery_rolls_back_interrupt_after_each_file_replacement(self) -> None:
        class SimulatedInterrupt(BaseException):
            pass

        for stop_after in (1, 2, 3):
            with self.subTest(stop_after=stop_after), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                pyproject_path, lock_path = make_release_files(root, version="0.3.0rc1", build="147")
                macos_project_path = make_macos_project(root, version="0.3.0rc1", build="147")
                evidence_path = make_recovery_evidence(root)
                originals = {path: path.read_bytes() for path in (pyproject_path, lock_path, macos_project_path)}
                applied = 0

                def interrupt(event: str, _path: Path, expected_stop: int = stop_after) -> None:
                    nonlocal applied
                    if event == "file-applied":
                        applied += 1
                        if applied == expected_stop:
                            raise SimulatedInterrupt

                with self.assertRaises(SimulatedInterrupt):
                    release.recover_beta3(
                        pyproject_path=pyproject_path,
                        lock_path=lock_path,
                        macos_project_path=macos_project_path,
                        evidence_path=evidence_path,
                        lock_runner=fake_lock_runner,
                        remote_verifier=skip_remote_verification,
                        transaction_observer=interrupt,
                    )

                for path, original in originals.items():
                    self.assertEqual(path.read_bytes(), original)
                self.assertFalse((root / release.TRANSACTION_JOURNAL_NAME).exists())

    def test_prepared_transaction_journal_restores_original_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pyproject_path, lock_path = make_release_files(root)
            macos_project_path = make_macos_project(root)
            files = [
                release.TransactionFile(
                    path=path,
                    original=path.read_bytes(),
                    target=path.read_bytes() + b"\n# target\n",
                )
                for path in (pyproject_path, lock_path, macos_project_path)
            ]
            journal_path = root / release.TRANSACTION_JOURNAL_NAME
            release._write_transaction_journal(journal_path, release._transaction_payload(files, "prepared"))
            release._atomic_write(files[0].path, files[0].target)

            release._recover_interrupted_transaction(journal_path, [file.path for file in files])

            for file in files:
                self.assertEqual(file.path.read_bytes(), file.original)
            self.assertFalse(journal_path.exists())

    def test_committed_transaction_journal_preserves_target_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pyproject_path, lock_path = make_release_files(root)
            macos_project_path = make_macos_project(root)
            files = [
                release.TransactionFile(
                    path=path,
                    original=path.read_bytes(),
                    target=path.read_bytes() + b"\n# target\n",
                )
                for path in (pyproject_path, lock_path, macos_project_path)
            ]
            journal_path = root / release.TRANSACTION_JOURNAL_NAME
            for file in files:
                release._atomic_write(file.path, file.target)
            release._write_transaction_journal(journal_path, release._transaction_payload(files, "committed"))

            release._recover_interrupted_transaction(journal_path, [file.path for file in files])

            for file in files:
                self.assertEqual(file.path.read_bytes(), file.target)
            self.assertFalse(journal_path.exists())

    def test_release_metadata_lock_rejects_concurrent_operation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pyproject_path, lock_path = make_release_files(root, version="1.2.3", build="10")

            with release._release_metadata_lock(pyproject_path):
                with self.assertRaisesRegex(release.ReleaseError, "already running"):
                    release.prepare_release(
                        "1.2.4",
                        "11",
                        pyproject_path=pyproject_path,
                        lock_path=lock_path,
                        lock_runner=fake_lock_runner,
                    )

    def test_recovery_rerun_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pyproject_path, lock_path = make_release_files(root, version="0.3.0rc1", build="147")
            macos_project_path = make_macos_project(root, version="0.3.0rc1", build="147")
            evidence_path = make_recovery_evidence(root)
            release.recover_beta3(
                pyproject_path=pyproject_path,
                lock_path=lock_path,
                macos_project_path=macos_project_path,
                evidence_path=evidence_path,
                lock_runner=fake_lock_runner,
                remote_verifier=skip_remote_verification,
            )
            recovered = {path: path.read_bytes() for path in (pyproject_path, lock_path, macos_project_path)}

            with self.assertRaisesRegex(release.ReleaseError, "requires exact source metadata"):
                release.recover_beta3(
                    pyproject_path=pyproject_path,
                    lock_path=lock_path,
                    macos_project_path=macos_project_path,
                    evidence_path=evidence_path,
                    lock_runner=fake_lock_runner,
                    remote_verifier=skip_remote_verification,
                )

            for path, recovered_content in recovered.items():
                self.assertEqual(path.read_bytes(), recovered_content)


if __name__ == "__main__":
    unittest.main()
