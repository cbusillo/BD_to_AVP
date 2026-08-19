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
PYSIDE_RUNTIME_PACKAGE_NAMES = frozenset({"pyside6", "pyside6-addons", "pyside6-essentials", "shiboken6"})


def normalized_requirement_name(requirement: str) -> str:
    package_name = re.split(r"[\[<>=!~;@\s]", requirement.strip(), maxsplit=1)[0]
    return re.sub(r"[-_.]+", "-", package_name).lower()


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
    def test_repository_requires_dependabot_compatible_uv(self) -> None:
        with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
            pyproject = tomllib.load(handle)

        self.assertEqual(pyproject["tool"]["uv"]["required-version"], ">=0.11.31")

    def test_repository_keeps_gui_dependency_out_of_cli_base(self) -> None:
        with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
            pyproject = tomllib.load(handle)

        gui_requirements = pyproject["project"]["optional-dependencies"]["gui"]
        gui_package_names = {normalized_requirement_name(requirement) for requirement in gui_requirements}
        base_package_names = {
            normalized_requirement_name(requirement) for requirement in pyproject["project"]["dependencies"]
        }
        self.assertIn("pyside6", gui_package_names)
        self.assertTrue(gui_package_names.isdisjoint(base_package_names))
        self.assertTrue(PYSIDE_RUNTIME_PACKAGE_NAMES.isdisjoint(base_package_names))

        dev_requirements = pyproject["dependency-groups"]["dev"]
        briefcase_requirements = pyproject["tool"]["briefcase"]["app"]["bd-to-avp"]["requires"]
        for requirement in gui_requirements:
            self.assertIn(requirement, dev_requirements)
        briefcase_package_names = {normalized_requirement_name(requirement) for requirement in briefcase_requirements}
        self.assertTrue(PYSIDE_RUNTIME_PACKAGE_NAMES.isdisjoint(briefcase_package_names))

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

    def test_repository_is_prepared_for_beta(self) -> None:
        metadata = release.load_release_metadata()

        self.assertEqual(metadata.package_version, "0.3.2b2")
        self.assertEqual(metadata.public_version, "0.3.2-beta.2")
        self.assertEqual(metadata.build_version, "164")
        self.assertEqual(metadata.release_tag, "v0.3.2-beta.2")
        self.assertEqual(metadata.release_name, "v0.3.2-beta.2")
        self.assertEqual(metadata.dmg_name, "3D-Blu-ray-to-Vision-Pro-0.3.2-beta.2.dmg")
        self.assertEqual(metadata.channel, "beta")
        self.assertTrue(metadata.prerelease)
        self.assertFalse(metadata.first_candidate_of_cycle)
        self.assertFalse(metadata.make_latest)
        self.assertFalse(metadata.publish_pypi)

        freeze_policy = json.loads((REPO_ROOT / ".github" / "release-freezes.json").read_text(encoding="utf-8"))
        self.assertNotIn("v0.3.2-beta.2", freeze_policy["frozen_release_tags"])

        cut_packet = (REPO_ROOT / "docs" / "0.3.2-beta.2-cut-packet.md").read_text(encoding="utf-8")
        self.assertIn("`0.3.2b2`", cut_packet)
        self.assertIn("Build `164`", cut_packet)
        self.assertIn("#593", cut_packet)
        self.assertIn("Privacy rules version `5`", cut_packet)
        receipt_exists = (REPO_ROOT / "docs" / "release-evidence" / "v0.3.2-beta.2" / "release-receipt.json").exists()
        expected_state = "Published and immutable." if receipt_exists else "not yet authorized or published."
        self.assertIn(expected_state, cut_packet)
        self.assertIn("post-publication", cut_packet)
        self.assertIn("PyPI", cut_packet)
        self.assertIn("Homebrew", cut_packet)

        qualification = json.loads(
            (REPO_ROOT / "docs" / "qualification" / "stable-signed-qualification-v1.json").read_text(encoding="utf-8")
        )
        self.assertEqual(qualification["candidate"]["package_version"], "0.3.1")
        self.assertEqual(qualification["candidate"]["build_version"], "162")
        self.assertEqual(qualification["candidate"]["worker_protocol_version"], 12)
        self.assertEqual(qualification["candidate"]["mapping_version"], 2)
        self.assertEqual(
            qualification["candidate"]["route_table_sha256"],
            "37756b7327cffe22a5c6d80ec6e69c67324e731aba87f2ebe815b065989ce214",
        )
        expected_case_ids = {
            "release-workflow-identity",
            "updater-route-v0.3.0-to-v0.3.1",
            "native-sparkle-notes-stable",
            "profile-save-action-accessibility",
            "signed-packaged-route-parity",
            "gui-preview-low-local-ample-destination",
            "gui-preview-cancel-cleanup",
            "gui-preview-failure-cleanup",
            "capacity-known-low",
            "capacity-unknown-and-conflicting",
            "network-generated-final-output",
            "overwrite-and-conversion-cancel",
            "malformed-pgs-parser-recovery",
            "subtitle-partial-output-diagnostics",
            "clean-machine-signed-update",
            "installed-ui-accessibility",
            "usb-bluray-makemkv",
            "protected-real-media-conversion",
            "vision-pro-physical-playback",
            "public-diagnostics-and-field-closure",
        }
        self.assertEqual({case["id"] for case in qualification["matrix"]}, expected_case_ids)
        self.assertEqual(
            set(qualification["acceptance"]["required_case_ids"]),
            {
                "release-workflow-identity",
                "sparkle-update-route",
                "profile-save-action-accessibility",
                "signed-packaged-route-parity",
                "gui-preview-low-local-ample-destination",
                "gui-preview-cancel-cleanup",
                "gui-preview-failure-cleanup",
                "capacity-known-low",
                "capacity-unknown-and-conflicting",
                "network-generated-final-output",
                "overwrite-and-conversion-cancel",
                "malformed-pgs-parser-recovery",
                "subtitle-partial-output-diagnostics",
                "clean-machine-signed-update",
                "installed-ui-accessibility",
            },
        )
        self.assertEqual(set(qualification["acceptance"]["preregistered_matrix_case_ids"]), expected_case_ids)
        self.assertEqual(
            set(qualification["acceptance"]["nonblocking_case_ids"]),
            {
                "native-sparkle-release-notes",
                "usb-bluray-makemkv",
                "protected-real-media-conversion",
                "vision-pro-physical-playback",
                "public-diagnostics-and-field-closure",
            },
        )
        self.assertEqual(
            set(qualification["acceptance"]["blocking_case_ids"]),
            {"sparkle-update-route", "clean-machine-signed-update", "installed-ui-accessibility"},
        )
        self.assertEqual(qualification["qualification_policy"]["id"], "release-qualification-policy-v1")
        self.assertEqual(
            {case["migration"] for case in qualification["matrix"]},
            {"release_run_receipt", "fresh_retest", "scope_evaluated", "external_nonblocking"},
        )
        candidate_identity_fields = (
            "source_git_sha",
            "dmg_sha256",
            "signed_app_tree_sha256",
            "release_run_id",
            "release_id",
            "appcast_sha256",
        )
        stable_receipt_path = (
            REPO_ROOT / "docs" / "release-evidence" / qualification["candidate"]["release_tag"] / "release-receipt.json"
        )
        if stable_receipt_path.exists():
            stable_receipt = json.loads(stable_receipt_path.read_text(encoding="utf-8"))
            artifacts_by_kind = {artifact["kind"]: artifact for artifact in stable_receipt["artifacts"]}
            self.assertEqual(
                {field: qualification["candidate"][field] for field in candidate_identity_fields},
                {
                    "source_git_sha": stable_receipt["source_sha"],
                    "dmg_sha256": artifacts_by_kind["dmg"]["sha256"],
                    "signed_app_tree_sha256": stable_receipt["signed_app_tree_sha256"],
                    "release_run_id": stable_receipt["workflow"]["run_id"],
                    "release_id": stable_receipt["release"]["id"],
                    "appcast_sha256": artifacts_by_kind["appcast"]["sha256"],
                },
            )
        else:
            for field in candidate_identity_fields:
                self.assertIsNone(qualification["candidate"][field])
        self.assertEqual(qualification["status"], "preregistered_pending_exact_candidate")
        self.assertEqual(
            set(qualification["acceptance"]["blocking_case_ids"]),
            {"sparkle-update-route", "clean-machine-signed-update", "installed-ui-accessibility"},
        )
        self.assertFalse(qualification["acceptance"]["milestone_complete"])
        self.assertTrue(
            qualification["execution_policy"]["macos_signing_requires_fresh_explicit_run_bound_authorization"]
        )
        self.assertEqual(qualification["execution_policy"]["approval_required_exit_code"], 20)
        self.assertFalse(qualification["acceptance"]["passed"])

        targeted = json.loads(
            (REPO_ROOT / "docs" / "qualification" / "rc3-targeted-qualification-v1.json").read_text(encoding="utf-8")
        )
        self.assertEqual(targeted["result"], "passed")
        self.assertEqual(targeted["acceptance"]["blocking_case_ids"], [])
        self.assertFalse(targeted["acceptance"]["field_case_open"])
        notes_case = next(case for case in targeted["cases"] if case["id"] == "native-sparkle-notes-rc3")
        self.assertEqual(notes_case["result"], "passed")
        self.assertEqual(
            notes_case["observations"]["content_aware_link_qualification"]["issue_result"], "not_applicable"
        )

        publication = json.loads(
            (REPO_ROOT / "docs" / "release-evidence" / "v0.3.0-rc.3" / "publication-record.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(publication["receipt_origin"], "post_publication_generated_from_verified_public_facts")
        self.assertFalse(publication["immutable_release_receipt_asset"])
        self.assertIsNone(publication["receipt_asset_id"])

        sparkle_qualification = json.loads(
            (REPO_ROOT / "docs" / "qualification" / "rc1-to-rc2-sparkle-qualification-v2.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(sparkle_qualification["status"], "passed_exact_signed_artifacts")
        self.assertEqual(sparkle_qualification["candidate"]["to"]["build_version"], "159")
        self.assertEqual(sparkle_qualification["result"], "passed")

    def test_rc2_records_immutable_published_identity(self) -> None:
        cut_packet = (REPO_ROOT / "docs" / "0.3.0-rc.2-cut-packet.md").read_text(encoding="utf-8")
        qualification = json.loads(
            (REPO_ROOT / "docs" / "qualification" / "rc2-signed-qualification-v1.json").read_text(encoding="utf-8")
        )

        self.assertIn("Published and immutable", cut_packet)
        self.assertIn("`30944931796`", cut_packet)
        self.assertIn("`cd56f02bab8589f527af6e45fe94b2ffcce473dc`", cut_packet)
        self.assertIn("`e39e81b99cf9c7bd272095d7a3f96de378e0a251334e9a4c5a83c54b8f4c1d45`", cut_packet)
        self.assertIn("`961b5e3bb0c2aba4b5ce474a0f0a559b80a037712df1f743427f2bf2b9cc48b6`", cut_packet)
        self.assertIn("must not be rebuilt", cut_packet)
        self.assertEqual(qualification["status"], "published_partial_exact_artifact")
        self.assertEqual(qualification["candidate"]["release_id"], 365132159)
        self.assertEqual(qualification["candidate"]["release_run_id"], 30944931796)
        self.assertTrue(qualification["immutable_publication"]["must_not_rebuild"])
        self.assertEqual(
            set(qualification["immutable_publication"]["failed_case_ids"]),
            {"malformed-pgs-recovery", "public-diagnostics-and-field-closure"},
        )

    def test_rc1_records_immutable_published_identity(self) -> None:
        cut_packet = (REPO_ROOT / "docs" / "0.3.0-rc.1-cut-packet.md").read_text(encoding="utf-8")
        qualification = json.loads(
            (REPO_ROOT / "docs" / "qualification" / "rc1-signed-qualification-v1.json").read_text(encoding="utf-8")
        )

        self.assertIn("Published and immutable", cut_packet)
        self.assertIn("`30865530971`", cut_packet)
        self.assertIn("`96146ac1b5f747dd78440761ad16e73d591fec4b`", cut_packet)
        self.assertIn("`0d8aab0e63a4097aa7cc1c7df511dc0582aa767c7dbe81c75971815af3df162c`", cut_packet)
        self.assertIn("`a88b258708a049e960fcb4f8985b5eb7eab50f539a882a2402b947187ba2e70b`", cut_packet)
        self.assertIn("must not be rebuilt", cut_packet)
        self.assertEqual(qualification["status"], "published_partial_exact_artifact")
        self.assertEqual(qualification["candidate"]["release_id"], 364562591)
        self.assertEqual(qualification["candidate"]["release_run_id"], 30865530971)
        self.assertTrue(qualification["immutable_publication"]["must_not_rebuild"])

    def test_beta11_qualification_remains_historical_receipt(self) -> None:
        qualification = json.loads(
            (REPO_ROOT / "docs" / "qualification" / "beta11-shared-signed-qualification-v1.json").read_text(
                encoding="utf-8"
            )
        )

        self.assertEqual(qualification["candidate"]["package_version"], "0.3.0b11")
        self.assertEqual(qualification["candidate"]["build_version"], "156")
        self.assertEqual(qualification["candidate"]["worker_protocol_version"], 12)
        self.assertEqual(qualification["candidate"]["mapping_version"], 2)
        self.assertEqual(
            qualification["candidate"]["route_table_sha256"],
            "37756b7327cffe22a5c6d80ec6e69c67324e731aba87f2ebe815b065989ce214",
        )
        self.assertFalse(qualification["acceptance"]["signed_beta_complete"])
        self.assertFalse(qualification["acceptance"]["passed"])

    def test_beta12_cut_packet_records_abandoned_metadata(self) -> None:
        cut_packet = (REPO_ROOT / "docs" / "0.3.0-beta.12-cut-packet.md").read_text(encoding="utf-8")

        self.assertIn("Abandoned metadata; never dispatched or published", cut_packet)
        self.assertIn("`0.3.0b12`", cut_packet)
        self.assertIn("Build `157`", cut_packet)
        self.assertIn("No tag, draft, release", cut_packet)
        self.assertIn("DMG, or appcast item was created", cut_packet)

    def test_beta10_cut_packet_records_immutable_published_identity(self) -> None:
        cut_packet = (REPO_ROOT / "docs" / "0.3.0-beta.10-cut-packet.md").read_text(encoding="utf-8")

        self.assertIn("Published and immutable", cut_packet)
        self.assertIn("`30445073119`", cut_packet)
        self.assertIn("`50b874a4ad681762f3aa94e02926b8a82f0aa221`", cut_packet)
        self.assertIn("`6fed922114e152be4f2e95ad7ee597465ae8d550539e7566ed05a64d8176d91c`", cut_packet)
        self.assertIn("`d89840da944b3a4519d68e84549ac7a69a9b2ffc5d2ec5717eabf6f0382151b0`", cut_packet)
        self.assertIn("must not be rebuilt", cut_packet)
        self.assertNotIn("The exact Beta 10 artifact remains pending", cut_packet)

    def test_beta9_cut_packet_records_failed_burned_identity(self) -> None:
        cut_packet = (REPO_ROOT / "docs" / "0.3.0-beta.9-cut-packet.md").read_text(encoding="utf-8")

        self.assertIn("Failed, unpublished, and permanently burned", cut_packet)
        self.assertIn("`30426833488`", cut_packet)
        self.assertIn("`355a5f559ba36d4e6862ad93c7d48527f8c7d5c0`", cut_packet)
        self.assertIn("Build `154`", cut_packet)
        self.assertIn("`1d8ca100cc43bdcf6dc678838de2cb99cf5c018024e871b06dec87b606a6f2a2`", cut_packet)
        self.assertIn("`4313b95146c4e7ca89c6cc0fd6838708a3d4a904`", cut_packet)
        self.assertIn("No tag, draft, release, DMG, appcast item", cut_packet)
        self.assertIn("must not be appended to the appcast", cut_packet)
        self.assertNotIn("Authorized metadata; publication pending", cut_packet)
        self.assertNotIn("The exact Beta 9 artifact remains pending", cut_packet)

    def test_beta8_cut_packet_records_immutable_published_identity(self) -> None:
        cut_packet = (REPO_ROOT / "docs" / "0.3.0-beta.8-cut-packet.md").read_text(encoding="utf-8")

        self.assertIn("Published and immutable", cut_packet)
        self.assertIn("`30341766419`", cut_packet)
        self.assertIn("`8e10dc38f935fe7deb7bbe4f6e1095f18b6cf328`", cut_packet)
        self.assertIn("`f16bd1c6f2d4820b0bdb985d8e9fc9617a7f7601ed69d0c203302063c29c23cc`", cut_packet)
        self.assertIn("`7e9d66d372bd94ea1d11a0990c3e070ba97268b2ce21794f1c46f04235dc7b93`", cut_packet)
        self.assertIn("Issue #382 is complete", cut_packet)
        self.assertIn("publication-time snapshot", cut_packet)
        self.assertIn("must not be rebuilt", cut_packet)
        self.assertNotIn("Authorized metadata; publication pending", cut_packet)
        self.assertNotIn("The exact Beta 8 artifact remains pending", cut_packet)

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
        self.assertFalse(metadata.first_candidate_of_cycle)
        self.assertFalse(metadata.make_latest)
        self.assertFalse(metadata.publish_pypi)

    def test_metadata_maps_internal_versions_to_public_release_identity(self) -> None:
        cases = (
            ("1.2.4a1", "1.2.4-alpha.1", "alpha", True, True),
            ("1.2.4b2", "1.2.4-beta.2", "beta", True, False),
            ("1.2.4rc1", "1.2.4-rc.1", "rc", True, True),
            ("1.2.4rc3", "1.2.4-rc.3", "rc", True, False),
            ("1.2.4", "1.2.4", "stable", False, False),
        )
        for package_version, public_version, channel, prerelease, first_candidate in cases:
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
            self.assertEqual(metadata.first_candidate_of_cycle, first_candidate)
            self.assertEqual(metadata.github_outputs()["first_candidate_of_cycle"], str(first_candidate).lower())
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


class QualificationUpdateBaseTests(unittest.TestCase):
    def test_sparkle_route_uses_candidate_channel_for_prereleases(self) -> None:
        self.assertEqual(
            release.qualification_sparkle_route(
                release.parse_release_tag("v1.2.4-beta.1"),
                release.parse_release_tag("v1.2.3"),
            ),
            "beta",
        )
        self.assertEqual(
            release.qualification_sparkle_route(
                release.parse_release_tag("v1.2.4-rc.1"),
                release.parse_release_tag("v1.2.4-beta.3"),
            ),
            "rc",
        )

    def test_sparkle_route_uses_prior_channel_for_stable_candidates(self) -> None:
        self.assertEqual(
            release.qualification_sparkle_route(
                release.parse_release_tag("v1.2.4"),
                release.parse_release_tag("v1.2.4-rc.2"),
            ),
            "rc",
        )
        self.assertEqual(
            release.qualification_sparkle_route(
                release.parse_release_tag("v1.2.4"),
                release.parse_release_tag("v1.2.3"),
            ),
            "stable",
        )

    def test_stable_uses_latest_same_version_prerelease_and_its_route(self) -> None:
        history = [
            published_release("v1.2.3"),
            published_release("v1.2.4-beta.2", prerelease=True),
            published_release("v1.2.4-rc.1", prerelease=True),
            published_release("v1.2.4-rc.2", prerelease=True),
        ]

        selection = release.select_qualification_update_base(
            "v1.2.4",
            history,
            "stable-head",
            tag_exists=lambda _tag_name: True,
            is_ancestor=lambda _tag_name, _head_ref: True,
        )

        self.assertEqual(selection.prior_tag, "v1.2.4-rc.2")
        self.assertEqual(selection.sparkle_route, "rc")

    def test_stable_patch_uses_previous_stable_release(self) -> None:
        selection = release.select_qualification_update_base(
            "v1.2.4",
            [published_release("v1.2.3")],
            "stable-head",
            tag_exists=lambda _tag_name: True,
            is_ancestor=lambda _tag_name, _head_ref: True,
        )

        self.assertEqual(selection.prior_tag, "v1.2.3")
        self.assertEqual(selection.sparkle_route, "stable")

    def test_prerelease_uses_candidate_route_with_latest_ancestor(self) -> None:
        history = [
            published_release("v1.2.3"),
            published_release("v1.2.4-beta.1", prerelease=True),
            published_release("v1.2.4-beta.2", prerelease=True),
        ]

        selection = release.select_qualification_update_base(
            "v1.2.4-rc.1",
            history,
            "rc-head",
            tag_exists=lambda _tag_name: True,
            is_ancestor=lambda tag_name, _head_ref: tag_name != "v1.2.4-beta.2",
        )

        self.assertEqual(selection.prior_tag, "v1.2.4-beta.1")
        self.assertEqual(selection.sparkle_route, "rc")

    def test_first_prerelease_uses_candidate_route_after_stable(self) -> None:
        selection = release.select_qualification_update_base(
            "v1.2.4-beta.1",
            [published_release("v1.2.3")],
            "beta-head",
            tag_exists=lambda _tag_name: True,
            is_ancestor=lambda _tag_name, _head_ref: True,
        )

        self.assertEqual(selection.prior_tag, "v1.2.3")
        self.assertEqual(selection.sparkle_route, "beta")

    def test_missing_prior_release_returns_empty_tag_and_candidate_route(self) -> None:
        selection = release.select_qualification_update_base(
            "v1.0.0-alpha.1",
            [],
            "alpha-head",
            tag_exists=lambda _tag_name: True,
            is_ancestor=lambda _tag_name, _head_ref: True,
        )

        self.assertEqual(selection.prior_tag, "")
        self.assertEqual(selection.sparkle_route, "alpha")


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
