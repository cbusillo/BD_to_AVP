from __future__ import annotations

import base64
import contextlib
import email.message
import hashlib
import io
import json
import tempfile
import unittest
import urllib.error
import urllib.request
import zipfile

from pathlib import Path

from scripts.support_diagnostics import (
    ClientConfiguration,
    MAX_INVENTORY_BYTES,
    MAX_JAVASCRIPT_SAFE_INTEGER,
    SupportDiagnosticsError,
    _RejectRedirects,
    delete_report,
    fetch_report,
    list_reports,
    main,
)


SUPPORT_CODE = "BDAVP-0123456789ABCDEF"
TOKEN = "maintainer-token-with-at-least-thirty-two-characters"
NATIVE_FIXTURE = Path(__file__).parent / "fixtures" / "support_diagnostics_native_v1.b64"


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


def make_bundle(schema_version: object = 1) -> bytes:
    contents = io.BytesIO()
    with zipfile.ZipFile(contents, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps({"schema_version": schema_version, "state": "complete"}))
        archive.writestr("events.jsonl", json.dumps({"schema_version": 1, "source": "client"}) + "\n")
        archive.writestr("storage.json", json.dumps({"schema_version": 1, "probes": []}))
        archive.writestr("tool-tail.txt", "# bd_to_avp_support_tool_tail schema_version=1\n")
    return contents.getvalue()


def native_swift_bundle() -> bytes:
    return base64.b64decode(NATIVE_FIXTURE.read_text(encoding="utf-8").strip(), validate=True)


def bundle_headers(bundle: bytes, schema_version: int = 1) -> dict[str, str]:
    return {
        "Content-Length": str(len(bundle)),
        "Content-Type": "application/zip",
        "X-Diagnostic-Schema-Version": str(schema_version),
        "X-Diagnostic-SHA256": hashlib.sha256(bundle).hexdigest(),
    }


def inventory_body(reports: list[dict[str, object]] | None = None) -> bytes:
    payload = {
        "reports": reports
        if reports is not None
        else [
            {
                "bundle_schema_version": 1,
                "created_at": "2026-07-22T13:30:00.000Z",
                "expires_at": "2026-08-21T13:30:00.000Z",
                "privacy_rules_version": 4,
                "size_bytes": 4096,
                "support_code": SUPPORT_CODE,
                "upload_state": "uploaded",
            }
        ],
        "schema_version": 1,
    }
    return json.dumps(payload).encode("utf-8")


def inventory_headers(body: bytes) -> dict[str, str]:
    return {
        "Content-Length": str(len(body)),
        "Content-Type": "application/json; charset=utf-8",
    }


def with_first_entry_crc(bundle: bytes, checksum: int) -> bytes:
    mutated = bytearray(bundle)
    central_directory_offset = mutated.find(b"PK\x01\x02")
    if central_directory_offset < 0:
        raise AssertionError("fixture is missing its central directory")
    mutated[14:18] = checksum.to_bytes(4, "little")
    mutated[central_directory_offset + 16 : central_directory_offset + 20] = checksum.to_bytes(4, "little")
    return bytes(mutated)


def with_first_entry_uncompressed_size(bundle: bytes, size: int) -> bytes:
    mutated = bytearray(bundle)
    central_directory_offset = mutated.find(b"PK\x01\x02")
    if central_directory_offset < 0:
        raise AssertionError("fixture is missing its central directory")
    mutated[22:26] = size.to_bytes(4, "little")
    mutated[central_directory_offset + 24 : central_directory_offset + 28] = size.to_bytes(4, "little")
    return bytes(mutated)


class SupportDiagnosticsCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.configuration = ClientConfiguration(endpoint="https://diagnostics.example.test/private", token=TOKEN)

    def test_fetch_writes_checksum_and_schema_validated_bundle(self) -> None:
        bundle = make_bundle()
        opener = CapturingOpener(FakeResponse(bundle, bundle_headers(bundle)))
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "bundle.zip"
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

    def test_fetch_accepts_native_swift_archive_fixture(self) -> None:
        bundle = native_swift_bundle()
        opener = CapturingOpener(FakeResponse(bundle, bundle_headers(bundle)))
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "bundle.zip"
            fetch_report(self.configuration, SUPPORT_CODE, output, opener)

            self.assertEqual(output.read_bytes(), bundle)

    def test_list_reports_validates_and_normalizes_inventory(self) -> None:
        body = inventory_body()
        opener = CapturingOpener(FakeResponse(body, inventory_headers(body)))

        inventory = list_reports(self.configuration, opener)

        self.assertEqual(inventory.schema_version, 1)
        self.assertEqual(len(inventory.reports), 1)
        report = inventory.reports[0]
        self.assertEqual(report.support_code, SUPPORT_CODE)
        self.assertEqual(report.upload_state, "uploaded")
        self.assertEqual(report.privacy_rules_version, 4)
        request, timeout = opener.requests[0]
        self.assertEqual(timeout, 30)
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(
            request.full_url,
            "https://diagnostics.example.test/private/v1/maintainer/reports",
        )
        self.assertEqual(request.get_header("Authorization"), f"Bearer {TOKEN}")
        self.assertEqual(request.get_header("Accept"), "application/json")
        self.assertNotIn("Authorization", request.headers)
        self.assertEqual(
            request.unredirected_hdrs["Authorization"],
            f"Bearer {TOKEN}",
        )
        self.assertEqual(opener.response.read_sizes, [MAX_INVENTORY_BYTES + 1])

    def test_maintainer_requests_reject_redirects(self) -> None:
        request = urllib.request.Request("https://diagnostics.example.test/v1/maintainer/reports")
        request.add_unredirected_header("Authorization", f"Bearer {TOKEN}")

        redirected = _RejectRedirects().redirect_request(
            request,
            None,
            302,
            "Found",
            email.message.Message(),
            "https://attacker.example/collect",
        )

        self.assertIsNone(redirected)

    def test_list_reports_accepts_future_privacy_rules_versions(self) -> None:
        reports = json.loads(inventory_body())["reports"]
        reports[0]["privacy_rules_version"] = MAX_JAVASCRIPT_SAFE_INTEGER
        body = inventory_body(reports)

        inventory = list_reports(
            self.configuration,
            CapturingOpener(FakeResponse(body, inventory_headers(body))),
        )

        self.assertEqual(
            inventory.reports[0].privacy_rules_version,
            MAX_JAVASCRIPT_SAFE_INTEGER,
        )

    def test_list_reports_accepts_legacy_privacy_metadata(self) -> None:
        reports = [
            {
                "bundle_schema_version": 1,
                "created_at": "2026-07-22T13:30:00.000Z",
                "expires_at": "2026-08-21T13:30:00.000Z",
                "privacy_rules_version": None,
                "size_bytes": 4096,
                "support_code": SUPPORT_CODE,
                "upload_state": "pending",
            }
        ]
        body = inventory_body(reports)
        inventory = list_reports(
            self.configuration,
            CapturingOpener(FakeResponse(body, inventory_headers(body))),
        )

        self.assertIsNone(inventory.reports[0].privacy_rules_version)

    def test_list_reports_rejects_sensitive_or_unknown_fields(self) -> None:
        reports = json.loads(inventory_body())["reports"]
        reports[0]["sha256"] = "a" * 64
        body = inventory_body(reports)

        with self.assertRaisesRegex(SupportDiagnosticsError, "invalid report entry"):
            list_reports(
                self.configuration,
                CapturingOpener(FakeResponse(body, inventory_headers(body))),
            )

    def test_list_reports_rejects_unsorted_or_oversized_responses(self) -> None:
        reports = [
            {
                "bundle_schema_version": 1,
                "created_at": "2026-07-21T13:30:00.000Z",
                "expires_at": "2026-08-20T13:30:00.000Z",
                "privacy_rules_version": 4,
                "size_bytes": 4096,
                "support_code": SUPPORT_CODE,
                "upload_state": "uploaded",
            },
            {
                "bundle_schema_version": 1,
                "created_at": "2026-07-22T13:30:00.000Z",
                "expires_at": "2026-08-21T13:30:00.000Z",
                "privacy_rules_version": 4,
                "size_bytes": 4096,
                "support_code": "BDAVP-1123456789ABCDEF",
                "upload_state": "uploaded",
            },
        ]
        body = inventory_body(reports)
        with self.assertRaisesRegex(SupportDiagnosticsError, "not ordered newest-first"):
            list_reports(
                self.configuration,
                CapturingOpener(FakeResponse(body, inventory_headers(body))),
            )

    def test_list_reports_rejects_duplicate_codes_and_noncanonical_timestamps(self) -> None:
        reports = json.loads(inventory_body())["reports"]
        body = inventory_body([reports[0], reports[0].copy()])
        with self.assertRaisesRegex(SupportDiagnosticsError, "duplicate support codes"):
            list_reports(
                self.configuration,
                CapturingOpener(FakeResponse(body, inventory_headers(body))),
            )

        reports[0]["created_at"] = "2026-07-22 13:30:00Z"
        body = inventory_body(reports)
        with self.assertRaisesRegex(SupportDiagnosticsError, "creation timestamp"):
            list_reports(
                self.configuration,
                CapturingOpener(FakeResponse(body, inventory_headers(body))),
            )

    def test_list_reports_maps_json_resource_errors(self) -> None:
        deeply_nested = b"[" * 10_000 + b"0" + b"]" * 10_000
        with self.assertRaisesRegex(SupportDiagnosticsError, "not valid JSON"):
            list_reports(
                self.configuration,
                CapturingOpener(FakeResponse(deeply_nested, inventory_headers(deeply_nested))),
            )

        oversized_integer = b'{"reports":[],"schema_version":' + b"9" * 5_000 + b"}"
        with self.assertRaisesRegex(SupportDiagnosticsError, "not valid JSON"):
            list_reports(
                self.configuration,
                CapturingOpener(
                    FakeResponse(
                        oversized_integer,
                        inventory_headers(oversized_integer),
                    )
                ),
            )

        oversized = b"x" * (MAX_INVENTORY_BYTES + 1)
        with self.assertRaisesRegex(SupportDiagnosticsError, "exceeds the response limit"):
            list_reports(
                self.configuration,
                CapturingOpener(FakeResponse(oversized, inventory_headers(oversized))),
            )

    def test_main_lists_normalized_inventory(self) -> None:
        body = inventory_body()
        opener = CapturingOpener(FakeResponse(body, inventory_headers(body)))
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = main(
                ["list"],
                {
                    "SUPPORT_DIAGNOSTICS_ENDPOINT": "https://diagnostics.example.test",
                    "SUPPORT_DIAGNOSTICS_TOKEN": TOKEN,
                },
                opener,
            )

        self.assertEqual(result, 0)
        self.assertEqual(json.loads(stdout.getvalue()), json.loads(body))

    def test_fetch_rejects_checksum_mismatch_without_writing_output(self) -> None:
        bundle = make_bundle()
        headers = bundle_headers(bundle)
        headers["X-Diagnostic-SHA256"] = "0" * 64
        opener = CapturingOpener(FakeResponse(bundle, headers))
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "bundle.zip"
            with self.assertRaisesRegex(SupportDiagnosticsError, "checksum"):
                fetch_report(self.configuration, SUPPORT_CODE, output, opener)
            self.assertFalse(output.exists())

    def test_fetch_rejects_malformed_archive_without_writing_output(self) -> None:
        bundle = b"not-a-zip"
        opener = CapturingOpener(FakeResponse(bundle, bundle_headers(bundle)))
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "bundle.zip"
            with self.assertRaisesRegex(SupportDiagnosticsError, "valid ZIP"):
                fetch_report(self.configuration, SUPPORT_CODE, output, opener)
            self.assertFalse(output.exists())

    def test_fetch_rejects_local_and_central_header_mismatch(self) -> None:
        bundle = bytearray(make_bundle())
        compressed_size = int.from_bytes(bundle[18:22], "little")
        bundle[18:22] = (compressed_size + 1).to_bytes(4, "little")
        headers = bundle_headers(bundle)
        opener = CapturingOpener(FakeResponse(bytes(bundle), headers))
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "bundle.zip"
            with self.assertRaisesRegex(SupportDiagnosticsError, "invalid archive entry"):
                fetch_report(self.configuration, SUPPORT_CODE, output, opener)
            self.assertFalse(output.exists())

    def test_fetch_rejects_crc_mismatch_without_writing_output(self) -> None:
        bundle = make_bundle()
        checksum = int.from_bytes(bundle[14:18], "little") ^ 1
        bundle = with_first_entry_crc(bundle, checksum)
        opener = CapturingOpener(FakeResponse(bundle, bundle_headers(bundle)))
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "bundle.zip"
            with self.assertRaisesRegex(SupportDiagnosticsError, "invalid archive entry"):
                fetch_report(self.configuration, SUPPORT_CODE, output, opener)
            self.assertFalse(output.exists())

    def test_fetch_rejects_declared_expansion_without_writing_output(self) -> None:
        bundle = with_first_entry_uncompressed_size(make_bundle(), 64 * 1024 + 1)
        opener = CapturingOpener(FakeResponse(bundle, bundle_headers(bundle)))
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "bundle.zip"
            with self.assertRaisesRegex(SupportDiagnosticsError, "invalid archive entry"):
                fetch_report(self.configuration, SUPPORT_CODE, output, opener)
            self.assertFalse(output.exists())

    def test_fetch_rejects_schema_mismatch_without_writing_output(self) -> None:
        bundle = make_bundle(schema_version=2)
        opener = CapturingOpener(FakeResponse(bundle, bundle_headers(bundle)))
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "bundle.zip"
            with self.assertRaisesRegex(SupportDiagnosticsError, "schema verification"):
                fetch_report(self.configuration, SUPPORT_CODE, output, opener)
            self.assertFalse(output.exists())

    def test_fetch_rejects_boolean_schema_without_writing_output(self) -> None:
        bundle = make_bundle(schema_version=True)
        opener = CapturingOpener(FakeResponse(bundle, bundle_headers(bundle)))
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "bundle.zip"
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
