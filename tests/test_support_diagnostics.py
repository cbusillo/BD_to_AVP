from __future__ import annotations

import contextlib
import email.message
import hashlib
import io
import json
import tarfile
import tempfile
import unittest
import urllib.error
import urllib.request

from pathlib import Path

from scripts.support_diagnostics import (
    ClientConfiguration,
    SupportDiagnosticsError,
    delete_report,
    fetch_report,
    main,
)


SUPPORT_CODE = "BDAVP-0123456789ABCDEF"
TOKEN = "maintainer-token-with-at-least-thirty-two-characters"


class FakeResponse:
    def __init__(self, data: bytes = b"", headers: dict[str, str] | None = None, status: int = 200) -> None:
        self.data = data
        self.headers = headers or {}
        self.status = status
        self.read_sizes: list[int] = []

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        return None

    def read(self, amount: int = -1) -> bytes:
        self.read_sizes.append(amount)
        return self.data if amount < 0 else self.data[:amount]


class CapturingOpener:
    def __init__(self, response: FakeResponse | Exception) -> None:
        self.response = response
        self.requests: list[tuple[urllib.request.Request, int]] = []

    def __call__(self, request: urllib.request.Request, timeout: int) -> FakeResponse:
        self.requests.append((request, timeout))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def make_bundle(schema_version: int = 1) -> bytes:
    contents = io.BytesIO()
    payload = json.dumps({"schema_version": schema_version, "state": "complete"}).encode("utf-8")
    with tarfile.open(fileobj=contents, mode="w:gz") as archive:
        member = tarfile.TarInfo("diagnostic.json")
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))
    return contents.getvalue()


def bundle_headers(bundle: bytes, schema_version: int = 1) -> dict[str, str]:
    return {
        "Content-Length": str(len(bundle)),
        "Content-Type": "application/gzip",
        "X-Diagnostic-Schema-Version": str(schema_version),
        "X-Diagnostic-SHA256": hashlib.sha256(bundle).hexdigest(),
    }


class SupportDiagnosticsCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.configuration = ClientConfiguration(endpoint="https://diagnostics.example.test/private", token=TOKEN)

    def test_fetch_writes_checksum_and_schema_validated_bundle(self) -> None:
        bundle = make_bundle()
        opener = CapturingOpener(FakeResponse(bundle, bundle_headers(bundle)))
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "bundle.tar.gz"
            result = fetch_report(self.configuration, SUPPORT_CODE, output, opener)

            self.assertEqual(output.read_bytes(), bundle)
            self.assertEqual(result.sha256, hashlib.sha256(bundle).hexdigest())
            self.assertEqual(result.schema_version, 1)

        request, timeout = opener.requests[0]
        self.assertEqual(timeout, 30)
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(
            request.full_url, f"https://diagnostics.example.test/private/v1/maintainer/reports/{SUPPORT_CODE}"
        )
        self.assertEqual(request.get_header("Authorization"), f"Bearer {TOKEN}")

    def test_fetch_rejects_checksum_mismatch_without_writing_output(self) -> None:
        bundle = make_bundle()
        headers = bundle_headers(bundle)
        headers["X-Diagnostic-SHA256"] = "0" * 64
        opener = CapturingOpener(FakeResponse(bundle, headers))
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "bundle.tar.gz"
            with self.assertRaisesRegex(SupportDiagnosticsError, "checksum"):
                fetch_report(self.configuration, SUPPORT_CODE, output, opener)
            self.assertFalse(output.exists())

    def test_fetch_rejects_malformed_archive_without_writing_output(self) -> None:
        bundle = b"not-a-gzip-tar"
        opener = CapturingOpener(FakeResponse(bundle, bundle_headers(bundle)))
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "bundle.tar.gz"
            with self.assertRaisesRegex(SupportDiagnosticsError, "valid gzip tar"):
                fetch_report(self.configuration, SUPPORT_CODE, output, opener)
            self.assertFalse(output.exists())

    def test_fetch_rejects_schema_mismatch_without_writing_output(self) -> None:
        bundle = make_bundle(schema_version=2)
        opener = CapturingOpener(FakeResponse(bundle, bundle_headers(bundle)))
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "bundle.tar.gz"
            with self.assertRaisesRegex(SupportDiagnosticsError, "schema verification"):
                fetch_report(self.configuration, SUPPORT_CODE, output, opener)
            self.assertFalse(output.exists())

    def test_delete_uses_maintainer_authorization(self) -> None:
        opener = CapturingOpener(FakeResponse(status=204))
        delete_report(self.configuration, SUPPORT_CODE, opener)

        request, timeout = opener.requests[0]
        self.assertEqual(timeout, 30)
        self.assertEqual(request.get_method(), "DELETE")
        self.assertEqual(request.get_header("Authorization"), f"Bearer {TOKEN}")

    def test_main_requires_delete_confirmation(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = main(
                ["delete", SUPPORT_CODE],
                {
                    "SUPPORT_DIAGNOSTICS_ENDPOINT": "https://diagnostics.example.test",
                    "SUPPORT_DIAGNOSTICS_TOKEN": TOKEN,
                },
            )

        self.assertEqual(result, 1)
        self.assertIn("Deletion requires --yes", stderr.getvalue())

    def test_main_redacts_service_failure_body_and_token(self) -> None:
        error = urllib.error.HTTPError(
            "https://diagnostics.example.test/v1/maintainer/reports/example",
            503,
            "Service unavailable",
            email.message.Message(),
            io.BytesIO(b"sensitive response content"),
        )
        opener = CapturingOpener(error)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = main(
                ["fetch", SUPPORT_CODE],
                {
                    "SUPPORT_DIAGNOSTICS_ENDPOINT": "https://diagnostics.example.test",
                    "SUPPORT_DIAGNOSTICS_TOKEN": TOKEN,
                },
                opener,
            )

        self.assertEqual(result, 1)
        self.assertIn("HTTP 503", stderr.getvalue())
        self.assertNotIn("sensitive response content", stderr.getvalue())
        self.assertNotIn(TOKEN, stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
