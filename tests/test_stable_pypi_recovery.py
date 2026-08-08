import hashlib
import json
import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from scripts import stable_pypi_recovery


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def fixture_evidence(root: Path) -> dict[str, object]:
    wheel = b"wheel-bytes"
    sdist = b"sdist-bytes"
    omitted = b"generated hidden file"
    manifest = (
        f"{sha256(omitted)}  dist/.gitignore\n"
        f"{sha256(wheel)}  dist/bd_to_avp-0.3.0-py3-none-any.whl\n"
        f"{sha256(sdist)}  dist/bd_to_avp-0.3.0.tar.gz\n"
    ).encode()
    (root / "dist").mkdir(parents=True)
    (root / "dist" / "bd_to_avp-0.3.0-py3-none-any.whl").write_bytes(wheel)
    (root / "dist" / "bd_to_avp-0.3.0.tar.gz").write_bytes(sdist)
    (root / "SHA256SUMS").write_bytes(manifest)
    return {
        "schema": "bd_to_avp.pypi_recovery",
        "schema_version": 1,
        "repository": {"id": 771225421, "full_name": "cbusillo/BD_to_AVP"},
        "release": {
            "id": 20,
            "tag": "v0.3.0",
            "name": "v0.3.0",
            "version": "0.3.0",
            "source_sha": "a" * 40,
            "published_at": "2026-08-07T21:42:28Z",
            "immutable": True,
            "draft": False,
            "prerelease": False,
            "receipt_asset_id": 30,
            "receipt_sha256": "placeholder",
            "receipt_size": 0,
        },
        "failed_run": {
            "id": 31219050718,
            "workflow_id": 101708423,
            "run_number": 231,
            "run_attempt": 1,
            "workflow_name": "Stable",
            "workflow_path": ".github/workflows/briefcase.yml",
            "event": "workflow_dispatch",
            "head_branch": "main",
            "head_sha": "a" * 40,
            "actor": "shiny-code-bot",
            "triggering_actor": "shiny-code-bot",
            "status": "completed",
            "conclusion": "failure",
            "failed_job_id": 40,
            "failed_job": "Publish stable Python distribution with trusted publishing",
            "failed_step": "Verify Python distribution transfer",
            "skipped_publish_step": "Publish with PyPI trusted publishing and attestations",
        },
        "artifact": {
            "id": 9009656530,
            "name": "python-distributions",
            "digest": "b" * 64,
            "size": 123,
            "manifest_sha256": sha256(manifest),
            "omitted_hidden_file": {"path": "dist/.gitignore", "sha256": sha256(omitted)},
        },
        "distributions": [
            {
                "path": "dist/bd_to_avp-0.3.0-py3-none-any.whl",
                "filename": "bd_to_avp-0.3.0-py3-none-any.whl",
                "packagetype": "bdist_wheel",
                "sha256": sha256(wheel),
                "size": len(wheel),
            },
            {
                "path": "dist/bd_to_avp-0.3.0.tar.gz",
                "filename": "bd_to_avp-0.3.0.tar.gz",
                "packagetype": "sdist",
                "sha256": sha256(sdist),
                "size": len(sdist),
            },
        ],
    }


class StablePyPIRecoveryTests(unittest.TestCase):
    def test_checked_evidence_is_digest_pinned(self) -> None:
        evidence = stable_pypi_recovery.load_evidence()

        self.assertEqual(evidence["release"]["source_sha"], "a9abbcf6cd1281d2c701e0c050b68fdafc5b9522")
        self.assertEqual(evidence["artifact"]["id"], 9009656530)
        self.assertEqual(len(evidence["distributions"]), 2)

        with tempfile.TemporaryDirectory() as directory:
            changed = Path(directory) / "evidence.json"
            changed.write_bytes(stable_pypi_recovery.EVIDENCE_PATH.read_bytes() + b"\n")
            with self.assertRaisesRegex(stable_pypi_recovery.StablePyPIRecoveryError, "digest"):
                stable_pypi_recovery.load_evidence(changed)

    def test_payload_verifier_accepts_only_the_reviewed_failure_shape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = fixture_evidence(root)

            stable_pypi_recovery.verify_payload(root, evidence)

            (root / "dist" / "unexpected.txt").write_text("unexpected", encoding="utf-8")
            with self.assertRaisesRegex(stable_pypi_recovery.StablePyPIRecoveryError, "file set"):
                stable_pypi_recovery.verify_payload(root, evidence)

    def test_payload_verifier_rejects_distribution_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = fixture_evidence(root)
            (root / "dist" / "bd_to_avp-0.3.0.tar.gz").write_bytes(b"changed")

            with self.assertRaisesRegex(stable_pypi_recovery.StablePyPIRecoveryError, "size differs"):
                stable_pypi_recovery.verify_payload(root, evidence)

    def test_pypi_verifier_checks_exact_file_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            evidence = fixture_evidence(Path(directory))
        payload = {
            "urls": [
                {
                    "filename": item["filename"],
                    "digests": {"sha256": item["sha256"]},
                    "size": item["size"],
                    "packagetype": item["packagetype"],
                }
                for item in evidence["distributions"]
            ]
        }

        with patch.object(stable_pypi_recovery, "_fetch_pypi", return_value=(200, payload)):
            stable_pypi_recovery.verify_pypi("published", evidence)
            self.assertEqual(stable_pypi_recovery.verify_pypi("recoverable", evidence), "published")
            with self.assertRaisesRegex(stable_pypi_recovery.StablePyPIRecoveryError, "already exists"):
                stable_pypi_recovery.verify_pypi("absent", evidence)

        payload["urls"][0]["digests"]["sha256"] = "0" * 64
        with patch.object(stable_pypi_recovery, "_fetch_pypi", return_value=(200, payload)):
            with self.assertRaisesRegex(stable_pypi_recovery.StablePyPIRecoveryError, "differ"):
                stable_pypi_recovery.verify_pypi("published", evidence)

    def test_pypi_verifier_retries_transient_fetch_failures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            evidence = fixture_evidence(Path(directory))
        with patch.object(
            stable_pypi_recovery,
            "_fetch_pypi",
            side_effect=[stable_pypi_recovery.StablePyPIRecoveryError("transient"), (404, None)],
        ):
            self.assertEqual(
                stable_pypi_recovery.verify_pypi("recoverable", evidence, retries=2),
                "absent",
            )

    def test_pypi_action_outputs_make_partial_publication_reentrant(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = fixture_evidence(root / "artifact")
            output = root / "github-output"
            output.touch()
            with patch.object(stable_pypi_recovery, "_fetch_pypi", return_value=(404, None)):
                stable_pypi_recovery.write_pypi_action_outputs(output, evidence, retries=1, delay=0)
            self.assertEqual(
                output.read_text(encoding="utf-8"),
                "publish_required=true\npublication_action=published\n",
            )

            output.write_text("", encoding="utf-8")
            payload = {
                "urls": [
                    {
                        "filename": item["filename"],
                        "digests": {"sha256": item["sha256"]},
                        "size": item["size"],
                        "packagetype": item["packagetype"],
                    }
                    for item in evidence["distributions"]
                ]
            }
            with patch.object(stable_pypi_recovery, "_fetch_pypi", return_value=(200, payload)):
                stable_pypi_recovery.write_pypi_action_outputs(output, evidence, retries=1, delay=0)
            self.assertEqual(
                output.read_text(encoding="utf-8"),
                "publish_required=false\npublication_action=already_present\n",
            )

            output.write_text("", encoding="utf-8")
            payload["urls"] = payload["urls"][:1]
            with patch.object(stable_pypi_recovery, "_fetch_pypi", return_value=(200, payload)):
                stable_pypi_recovery.write_pypi_action_outputs(output, evidence, retries=1, delay=0)
            self.assertEqual(
                output.read_text(encoding="utf-8"),
                "publish_required=true\npublication_action=published\n",
            )

    def test_remote_verifier_requires_publish_step_to_have_been_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact_root = root / "artifact-root"
            evidence = fixture_evidence(artifact_root)
            receipt = {"source_sha": "a" * 40, "workflow": {"run_id": 31219050718}}
            receipt_bytes = (json.dumps(receipt) + "\n").encode()
            evidence["release"]["receipt_sha256"] = sha256(receipt_bytes)
            evidence["release"]["receipt_size"] = len(receipt_bytes)
            workflow_run = {
                "id": 31219050718,
                "workflow_id": 101708423,
                "run_number": 231,
                "run_attempt": 1,
                "name": "Stable",
                "path": ".github/workflows/briefcase.yml",
                "event": "workflow_dispatch",
                "head_branch": "main",
                "head_sha": "a" * 40,
                "status": "completed",
                "conclusion": "failure",
                "actor": {"login": "shiny-code-bot"},
                "triggering_actor": {"login": "shiny-code-bot"},
            }
            jobs = {
                "jobs": [
                    {
                        "id": 40,
                        "name": "Publish stable Python distribution with trusted publishing",
                        "conclusion": "failure",
                        "steps": [
                            {"name": "Verify Python distribution transfer", "conclusion": "failure"},
                            {
                                "name": "Publish with PyPI trusted publishing and attestations",
                                "conclusion": "skipped",
                            },
                        ],
                    }
                ]
            }
            artifact = {
                "id": 9009656530,
                "name": "python-distributions",
                "digest": f"sha256:{evidence['artifact']['digest']}",
                "size_in_bytes": 123,
                "expired": False,
                "workflow_run": {"id": 31219050718, "head_sha": "a" * 40},
            }
            release = {
                "id": 20,
                "tag_name": "v0.3.0",
                "name": "v0.3.0",
                "target_commitish": "a" * 40,
                "draft": False,
                "prerelease": False,
                "immutable": True,
                "published_at": "2026-08-07T21:42:28Z",
                "assets": [
                    {
                        "id": 30,
                        "digest": f"sha256:{evidence['release']['receipt_sha256']}",
                        "size": len(receipt_bytes),
                    }
                ],
            }
            paths = {}
            for name, value in {
                "workflow-run": workflow_run,
                "jobs": jobs,
                "artifact": artifact,
                "release": release,
            }.items():
                paths[name] = root / f"{name}.json"
                paths[name].write_text(json.dumps(value), encoding="utf-8")
            receipt_path = root / "receipt.json"
            receipt_path.write_bytes(receipt_bytes)

            stable_pypi_recovery.verify_remote_files(
                paths["workflow-run"], paths["jobs"], paths["artifact"], paths["release"], receipt_path, evidence
            )

            jobs["jobs"][0]["steps"][1]["conclusion"] = "success"
            paths["jobs"].write_text(json.dumps(jobs), encoding="utf-8")
            with self.assertRaisesRegex(stable_pypi_recovery.StablePyPIRecoveryError, "publication was skipped"):
                stable_pypi_recovery.verify_remote_files(
                    paths["workflow-run"],
                    paths["jobs"],
                    paths["artifact"],
                    paths["release"],
                    receipt_path,
                    evidence,
                )


if __name__ == "__main__":
    unittest.main()
