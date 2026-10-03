import contextlib
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from scripts.beta_change_scoped_evidence import (
    DEFAULT_SUPPORT_DIAGNOSTICS_ENDPOINT,
    EVIDENCE_INDEX_PATH,
    PACKAGED_SMOKE_PROOFS,
    POLICY_PATH,
    PROOF_CATALOG,
    SUPPORT_DIAGNOSTICS_ENDPOINT_ENV,
    BetaEvidenceError,
    CommandResult,
    Runners,
    eligible_case_ids,
    parse_unittest_results,
    produce_change_scoped_evidence,
    python_proof_target,
    swift_proof_key,
)
from scripts.qualify_release_scope import load_policy
from scripts.release import ReleaseMetadata, parse_release_version
from scripts.release_milestone_context import (
    _validate_append_only_evidence_index,
    _validate_beta_change_scoped_evidence_append,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
POLICY = load_policy(REPO_ROOT / POLICY_PATH)
QUALIFICATION_RELATIVE = "docs/qualification/candidate-signed-qualification-v1.json"


def release_metadata(version_text: str, build_version: str = "900") -> ReleaseMetadata:
    version = parse_release_version(version_text)
    return ReleaseMetadata(
        package_version=version.text,
        public_version=version.public_version,
        build_version=build_version,
        release_tag=version.release_tag,
        release_name=version.release_tag,
        dmg_name=f"app-{version.public_version}.dmg",
        channel=version.channel,
        prerelease=version.prerelease,
        first_candidate_of_cycle=version.first_candidate_of_cycle,
        make_latest=not version.prerelease,
        publish_pypi=not version.prerelease,
    )


BETA = release_metadata("9.8.7b1")


def git(root: Path, *arguments: str) -> str:
    return subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


def build_repository(root: Path, branch: str) -> str:
    git(root, "init", "-q", "-b", branch)
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    swift_sources = {proof.split(":", 1)[0] for entry in PROOF_CATALOG.values() for proof in entry["proof"]}
    for relative in (
        POLICY_PATH,
        EVIDENCE_INDEX_PATH,
        *sorted(path for path in swift_sources if path.startswith("macos/")),
    ):
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO_ROOT / relative, root / relative)
    git(root, "add", ".")
    git(root, "commit", "-qm", "candidate")
    return git(root, "rev-parse", "HEAD")


def xctest_output(proofs: Sequence[str], *, failed: Sequence[str] = ()) -> str:
    lines = ["Test Case '-[BluRayToVisionProTests.OtherTests testUnrelated]' passed (0.001 seconds)."]
    for proof in proofs:
        if not proof.startswith("macos/"):
            continue
        key = swift_proof_key(proof, REPO_ROOT)
        assert key is not None
        status = "failed" if proof in failed else "passed"
        lines.append(f"Test Case '-[{key}]' started.")
        lines.append(f"Test Case '-[{key}]' {status} (0.010 seconds).")
    return "\n".join(lines) + "\n** TEST SUCCEEDED **\n"


def unittest_output(proofs: Sequence[str], *, statuses: Mapping[str, str] | None = None) -> str:
    statuses = statuses or {}
    lines = ["test_unrelated (tests.test_other.OtherTests.test_unrelated) ... ok"]
    skipped = 0
    for proof in proofs:
        target = python_proof_target(proof)
        if target is None:
            continue
        module, method = target
        status = statuses.get(proof, "ok")
        skipped += status.startswith("skipped")
        lines.append(f"{method} ({module}.CaseTests.{method}) ... {status}")
    count = len(lines)
    summary = f"OK (skipped={skipped})" if skipped else "OK"
    return "\n".join(lines) + f"\n\n{'-' * 70}\nRan {count} tests in 0.100s\n\n{summary}\n"


class FakeRunners:
    def __init__(
        self,
        blocking_case_ids: Sequence[str],
        *,
        classifier_exit: int = 2,
        native_failed: Sequence[str] = (),
        native_omitted: Sequence[str] = (),
        python_statuses: Mapping[str, str] | None = None,
        package_exit: int = 0,
    ) -> None:
        self.blocking_case_ids = list(blocking_case_ids)
        self.classifier_exit = classifier_exit
        self.native_failed = native_failed
        self.native_omitted = native_omitted
        self.python_statuses = python_statuses
        self.package_exit = package_exit
        self.calls: list[str] = []
        self.package_environment: Mapping[str, str] | None = None

    @property
    def proofs(self) -> list[str]:
        return [proof for case_id in self.blocking_case_ids for proof in PROOF_CATALOG[case_id]["proof"]]

    def runners(self, candidate_sha: Callable[[], str]) -> Runners:
        def classify(command: Sequence[str], environment: Mapping[str, str] | None) -> CommandResult:
            self.calls.append("classify")
            report = {
                "candidate_sha": candidate_sha(),
                "workflow_phase": "preparation",
                "passed": self.classifier_exit == 0,
                "blocking_retests": self.blocking_case_ids if self.classifier_exit else [],
                "cases": [],
            }
            return CommandResult(self.classifier_exit, json.dumps(report, indent=2, sort_keys=True) + "\n", "")

        def native_tests(command: Sequence[str], environment: Mapping[str, str] | None) -> CommandResult:
            self.calls.append("native")
            proofs = [proof for proof in self.proofs if proof not in self.native_omitted]
            return CommandResult(1 if self.native_failed else 0, xctest_output(proofs, failed=self.native_failed), "")

        def python_tests(command: Sequence[str], environment: Mapping[str, str] | None) -> CommandResult:
            self.calls.append("python")
            return CommandResult(0, "", unittest_output(self.proofs, statuses=self.python_statuses))

        def package(command: Sequence[str], environment: Mapping[str, str] | None) -> CommandResult:
            self.calls.append("package")
            self.package_environment = environment
            return CommandResult(self.package_exit, "Packaged smoke output\n/build/App.app\n", "")

        return Runners(
            classify=classify,
            native_tests=native_tests,
            python_tests=python_tests,
            package=package,
            app_facts=lambda app_path: {
                "app_tree_sha256": "e" * 64,
                "architecture": "arm64",
                "bundle_identifier": "com.example.app",
                "build_version": BETA.build_version,
            },
            toolchain=lambda: "Xcode 1.0 (1A1)",
        )


class BetaChangeScopedEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.root = Path(temporary_directory.name) / "repo"
        self.root.mkdir()
        self.log_dir = Path(temporary_directory.name) / "logs"
        self.candidate_sha = build_repository(self.root, f"qualify/{BETA.release_tag}")
        self.original_index = (self.root / EVIDENCE_INDEX_PATH).read_bytes()

    def produce(self, fake: FakeRunners, metadata: ReleaseMetadata = BETA):
        with contextlib.redirect_stdout(io.StringIO()):
            return produce_change_scoped_evidence(
                self.root,
                metadata,
                QUALIFICATION_RELATIVE,
                runners=fake.runners(lambda: git(self.root, "rev-parse", "HEAD")),
                log_dir=self.log_dir,
                now=lambda: datetime(2026, 1, 2, 3, 4, 5, 678901, tzinfo=UTC),
            )

    def assert_nothing_written(self) -> None:
        self.assertEqual((self.root / EVIDENCE_INDEX_PATH).read_bytes(), self.original_index)
        self.assertEqual(git(self.root, "status", "--porcelain", "--untracked-files=all"), "")

    def test_produced_evidence_passes_pull_request_validator(self) -> None:
        fake = FakeRunners(sorted(PROOF_CATALOG))

        result = self.produce(fake)

        assert result is not None
        document_text = (self.root / result.document_path).read_text(encoding="utf-8")
        self.assertNotIn(str(self.root), document_text)
        self.assertNotIn(str(self.log_dir), document_text)
        assert fake.package_environment is not None
        self.assertEqual(
            fake.package_environment[SUPPORT_DIAGNOSTICS_ENDPOINT_ENV], DEFAULT_SUPPORT_DIAGNOSTICS_ENDPOINT
        )
        git(self.root, "add", ".")
        git(self.root, "commit", "-qm", "record change-scoped evidence")
        changed_paths = git(self.root, "diff", "--name-only", self.candidate_sha, "HEAD").splitlines()
        appended = _validate_append_only_evidence_index(self.root, base_sha=self.candidate_sha)
        _validate_beta_change_scoped_evidence_append(
            self.root,
            base_sha=self.candidate_sha,
            head_branch=f"qualify/{BETA.release_tag}",
            changed_paths=changed_paths,
            policy_relative=POLICY_PATH,
            appended_receipts=appended,
        )
        self.assertEqual([receipt["receipt_id"] for receipt in appended], list(result.receipt_ids))

    def test_no_blocking_cases_exits_without_running_tests_or_writing(self) -> None:
        fake = FakeRunners([], classifier_exit=0)

        self.assertIsNone(self.produce(fake))

        self.assertEqual(fake.calls, ["classify"])
        self.assert_nothing_written()

    def test_ineligible_blocking_case_fails_closed_before_running_tests(self) -> None:
        ineligible = sorted(
            str(case["id"]) for case in POLICY["cases"] if str(case["id"]) not in eligible_case_ids(POLICY)
        )
        fake = FakeRunners([next(iter(PROOF_CATALOG)), ineligible[0]])

        with self.assertRaisesRegex(BetaEvidenceError, "not eligible"):
            self.produce(fake)

        self.assertEqual(fake.calls, ["classify"])
        self.assert_nothing_written()

    def test_missing_native_proof_fails_closed(self) -> None:
        case_id = "gui-preview-failure-cleanup"
        swift_proof = next(proof for proof in PROOF_CATALOG[case_id]["proof"] if proof.startswith("macos/"))
        fake = FakeRunners([case_id], native_omitted=[swift_proof])

        with self.assertRaisesRegex(BetaEvidenceError, re.escape(swift_proof)):
            self.produce(fake)

        self.assertNotIn("package", fake.calls)
        self.assert_nothing_written()

    def test_skipped_python_proof_fails_closed(self) -> None:
        case_id = "overwrite-and-conversion-cancel"
        python_proof = next(proof for proof in PROOF_CATALOG[case_id]["proof"] if python_proof_target(proof))
        fake = FakeRunners([case_id], python_statuses={python_proof: "skipped 'unavailable'"})

        with self.assertRaisesRegex(BetaEvidenceError, re.escape(python_proof)):
            self.produce(fake)

        self.assert_nothing_written()

    def test_failed_package_fails_closed(self) -> None:
        fake = FakeRunners(["gui-preview-cancel-cleanup"], package_exit=1)

        with self.assertRaises(BetaEvidenceError):
            self.produce(fake)

        self.assert_nothing_written()

    def test_refuses_dirty_worktree(self) -> None:
        (self.root / "untracked.txt").write_text("dirty\n", encoding="utf-8")

        with self.assertRaisesRegex(BetaEvidenceError, "clean"):
            self.produce(FakeRunners(["gui-preview-cancel-cleanup"]))

    def test_refuses_wrong_branch(self) -> None:
        git(self.root, "checkout", "-q", "-b", "main")

        with self.assertRaisesRegex(BetaEvidenceError, "branch"):
            self.produce(FakeRunners(["gui-preview-cancel-cleanup"]))

    def test_refuses_non_beta_release(self) -> None:
        fake = FakeRunners(["gui-preview-cancel-cleanup"])

        with self.assertRaisesRegex(BetaEvidenceError, "Beta"):
            self.produce(fake, release_metadata("9.8.7rc1"))

        self.assertEqual(fake.calls, [])


class ProofCatalogTests(unittest.TestCase):
    def test_catalog_covers_only_cases_the_lane_may_prove(self) -> None:
        self.assertLessEqual(set(PROOF_CATALOG), eligible_case_ids(POLICY))

    def test_every_catalogued_proof_names_an_existing_test_or_packaged_smoke(self) -> None:
        for case_id, entry in PROOF_CATALOG.items():
            for proof in entry["proof"]:
                with self.subTest(case_id=case_id, proof=proof):
                    source, name = proof.split(":", 1)
                    if proof in PACKAGED_SMOKE_PROOFS:
                        self.assertTrue((REPO_ROOT / source).is_file())
                    elif proof.startswith("macos/"):
                        self.assertIsNotNone(swift_proof_key(proof, REPO_ROOT))
                    else:
                        self.assertIsNotNone(python_proof_target(proof))
                        text = (REPO_ROOT / source).read_text(encoding="utf-8")
                        self.assertRegex(text, rf"\bdef {re.escape(name)}\(")

    def test_prior_evidence_references_name_committed_records(self) -> None:
        for case_id, entry in PROOF_CATALOG.items():
            for key, reference in entry.get("scope", {}).items():
                if not key.endswith("_evidence"):
                    continue
                with self.subTest(case_id=case_id, key=key):
                    self.assertTrue((REPO_ROOT / reference).is_file())

    def test_prior_network_evidence_proves_a_network_destination(self) -> None:
        reference = PROOF_CATALOG["network-generated-final-output"]["scope"]["prior_real_network_evidence"]
        record = json.loads((REPO_ROOT / reference).read_text(encoding="utf-8"))
        network_cases = [
            case
            for case in record["cases"]
            if case.get("id") == "network-generated-final-output"
            and case.get("result") == "passed"
            and case.get("observations", {}).get("network_destination") is True
        ]
        self.assertEqual(len(network_cases), 1)


class SwiftProofResolutionTests(unittest.TestCase):
    def test_resolves_the_type_enclosing_the_test_method(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "macos/ModuleTests/ScreenTests.swift"
            source.parent.mkdir(parents=True)
            source.write_text(
                "@MainActor\n"
                "final class ScreenTests: XCTestCase {\n"
                "    func testScreen() {}\n"
                "}\n\n"
                "final class CacheTests: XCTestCase {\n"
                "    func testCache() async throws {}\n"
                "}\n\n"
                "extension ScreenTests {\n"
                "    func testExtended() {}\n"
                "}\n",
                encoding="utf-8",
            )

            keys = {
                name: swift_proof_key(f"macos/ModuleTests/ScreenTests.swift:{name}", root)
                for name in ("testScreen", "testCache", "testExtended", "testMissing")
            }

        self.assertEqual(keys["testScreen"], "ModuleTests.ScreenTests testScreen")
        self.assertEqual(keys["testCache"], "ModuleTests.CacheTests testCache")
        self.assertEqual(keys["testExtended"], "ModuleTests.ScreenTests testExtended")
        self.assertIsNone(keys["testMissing"])


class UnittestOutputParsingTests(unittest.TestCase):
    def test_reads_real_verbose_unittest_statuses(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "sample_tests.py").write_text(
                "import logging\n"
                "import unittest\n\n\n"
                "class SampleTests(unittest.TestCase):\n"
                "    def test_passes(self):\n"
                "        print('noise on stdout')\n\n"
                "    def test_documented(self):\n"
                '        """First docstring line."""\n\n'
                "    def test_fails(self):\n"
                "        self.fail('no')\n\n"
                "    def test_errors(self):\n"
                "        raise RuntimeError('no')\n\n"
                "    def test_logs_to_stderr(self):\n"
                "        logging.getLogger('sample').warning('Ignoring truncated segment ... ok')\n\n"
                "    @unittest.skip('unavailable')\n"
                "    def test_skipped(self):\n"
                "        pass\n",
                encoding="utf-8",
            )
            completed = subprocess.run(
                [sys.executable, "-m", "unittest", "-v", "sample_tests"],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
            )

        results = parse_unittest_results(completed.stderr)

        prefix = "sample_tests.SampleTests."
        self.assertEqual(results[f"{prefix}test_passes"], "ok")
        self.assertEqual(results[f"{prefix}test_documented"], "ok")
        self.assertEqual(results[f"{prefix}test_logs_to_stderr"], "ok")
        self.assertEqual(results[f"{prefix}test_fails"], "failed")
        self.assertEqual(results[f"{prefix}test_errors"], "failed")
        self.assertEqual(results[f"{prefix}test_skipped"], "skipped")


if __name__ == "__main__":
    unittest.main()
