from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
import re
import struct
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zlib

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Mapping, Protocol, cast


MAX_BUNDLE_BYTES = 2 * 1024 * 1024
MAX_INVENTORY_BYTES = 256 * 1024
MAX_INVENTORY_REPORTS = 500
MAX_JAVASCRIPT_SAFE_INTEGER = 9_007_199_254_740_991
MAX_UNCOMPRESSED_ARCHIVE_BYTES = 1_500_000
ENTRY_LIMITS = {
    "manifest.json": 64 * 1024,
    "events.jsonl": 320 * 1024,
    "storage.json": 160 * 1024,
    "tool-tail.txt": 640 * 1024,
}
ALLOWED_ZIP_FLAGS = {0, 0x0800}
CENTRAL_DIRECTORY_HEADER = struct.Struct("<IHHHHHHIIIHHHHHII")
CENTRAL_DIRECTORY_SIGNATURE = 0x02014B50
END_OF_CENTRAL_DIRECTORY = struct.Struct("<IHHHHIIH")
END_OF_CENTRAL_DIRECTORY_SIGNATURE = 0x06054B50
LOCAL_FILE_HEADER = struct.Struct("<IHHHHHIIIHH")
LOCAL_FILE_HEADER_SIGNATURE = 0x04034B50
ZIP_DEFLATED = 8
SCHEMA_VERSION = 1
SUPPORT_CODE_PATTERN = re.compile(r"^BDAVP-[0-9ABCDEFGHJKMNPQRSTVWXYZ]{16}$")
SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
INVENTORY_TIMESTAMP_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
UPLOAD_STATES = {"failed", "pending", "uploaded", "uploading"}


class SupportDiagnosticsError(RuntimeError):
    pass


class ResponseLike(Protocol):
    headers: Mapping[str, str]

    def __enter__(self) -> ResponseLike:
        raise NotImplementedError

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        raise NotImplementedError

    def read(self, amount: int = -1) -> bytes:
        raise NotImplementedError


ResponseOpener = Callable[..., object]


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        _request: urllib.request.Request,
        _file_pointer: object,
        _code: int,
        _message: str,
        _headers: Mapping[str, str],
        _new_url: str,
    ) -> None:
        return None


_DEFAULT_OPENER: ResponseOpener = urllib.request.build_opener(_RejectRedirects()).open


@dataclass(frozen=True)
class ClientConfiguration:
    endpoint: str
    token: str


@dataclass(frozen=True)
class FetchResult:
    output: Path
    schema_version: int
    sha256: str
    size_bytes: int
    support_code: str


@dataclass(frozen=True)
class ReportInventoryEntry:
    bundle_schema_version: int
    created_at: str
    expires_at: str
    privacy_rules_version: int | None
    size_bytes: int
    support_code: str
    upload_state: str


@dataclass(frozen=True)
class ReportInventory:
    reports: tuple[ReportInventoryEntry, ...]
    schema_version: int


@dataclass(frozen=True)
class _ZipEntry:
    name: str
    flags: int
    crc32: int
    compressed_size: int
    uncompressed_size: int
    local_header_offset: int


def _environment_value(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "").strip()
    if not value:
        raise SupportDiagnosticsError(f"{name} must be set.")
    return value


def load_configuration(environ: Mapping[str, str] | None = None) -> ClientConfiguration:
    source = os.environ if environ is None else environ
    endpoint = _environment_value(source, "SUPPORT_DIAGNOSTICS_ENDPOINT")
    token = _environment_value(source, "SUPPORT_DIAGNOSTICS_TOKEN")
    parsed = urllib.parse.urlsplit(endpoint)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise SupportDiagnosticsError(
            "SUPPORT_DIAGNOSTICS_ENDPOINT must be an HTTPS origin or path prefix without credentials."
        )
    if len(token) < 32 or any(character.isspace() for character in token):
        raise SupportDiagnosticsError("SUPPORT_DIAGNOSTICS_TOKEN must be a non-empty maintainer bearer token.")
    return ClientConfiguration(endpoint=endpoint.rstrip("/"), token=token)


def validate_support_code(support_code: str) -> str:
    if SUPPORT_CODE_PATTERN.fullmatch(support_code) is None:
        raise SupportDiagnosticsError("Support code must use the BDAVP-XXXXXXXXXXXXXXX format.")
    return support_code


def report_url(configuration: ClientConfiguration, support_code: str) -> str:
    encoded_code = urllib.parse.quote(validate_support_code(support_code), safe="")
    return f"{configuration.endpoint}/v1/maintainer/reports/{encoded_code}"


def inventory_url(configuration: ClientConfiguration) -> str:
    return f"{configuration.endpoint}/v1/maintainer/reports"


def _request(
    url: str,
    method: str,
    token: str,
    accept: str = "application/zip",
) -> urllib.request.Request:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": accept,
            "User-Agent": "bd-to-avp-support-diagnostics-cli/1",
        },
        method=method,
    )
    request.add_unredirected_header("Authorization", f"Bearer {token}")
    return request


def _open(opener: ResponseOpener, request: urllib.request.Request) -> ResponseLike:
    try:
        return cast(ResponseLike, opener(request, timeout=30))
    except urllib.error.HTTPError as error:
        raise SupportDiagnosticsError(f"Service returned HTTP {error.code}.") from error
    except urllib.error.URLError as error:
        raise SupportDiagnosticsError("Could not reach the diagnostic service.") from error


def _required_header(headers: Mapping[str, str], name: str) -> str:
    value = headers.get(name)
    if value is None:
        raise SupportDiagnosticsError(f"Service response is missing {name}.")
    return value


def _parse_content_length(headers: Mapping[str, str]) -> int:
    value = _required_header(headers, "Content-Length")
    if not value.isdecimal():
        raise SupportDiagnosticsError("Service response has an invalid Content-Length.")
    length = int(value)
    if length <= 0 or length > MAX_BUNDLE_BYTES:
        raise SupportDiagnosticsError("Service response exceeds the diagnostic bundle limit.")
    return length


def _inventory_integer(
    value: object,
    name: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if type(value) is not int or value < minimum or value > maximum:
        raise SupportDiagnosticsError(f"Inventory response has an invalid {name}.")
    return value


def _inventory_timestamp(value: object, name: str) -> tuple[str, datetime]:
    if not isinstance(value, str) or INVENTORY_TIMESTAMP_PATTERN.fullmatch(value) is None:
        raise SupportDiagnosticsError(f"Inventory response has an invalid {name}.")
    try:
        parsed = datetime.fromisoformat(f"{value[:-1]}+00:00")
    except ValueError as error:
        raise SupportDiagnosticsError(f"Inventory response has an invalid {name}.") from error
    canonical = parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    if parsed.utcoffset() != timedelta(0) or canonical != value:
        raise SupportDiagnosticsError(f"Inventory response has an invalid {name}.")
    return value, parsed


def _inventory_entry(value: object) -> tuple[ReportInventoryEntry, datetime]:
    if not isinstance(value, dict):
        raise SupportDiagnosticsError("Inventory response contains an invalid report entry.")
    expected_keys = {
        "bundle_schema_version",
        "created_at",
        "expires_at",
        "privacy_rules_version",
        "size_bytes",
        "support_code",
        "upload_state",
    }
    if set(value) != expected_keys:
        raise SupportDiagnosticsError("Inventory response contains an invalid report entry.")
    support_code = value["support_code"]
    if not isinstance(support_code, str):
        raise SupportDiagnosticsError("Inventory response has an invalid support code.")
    support_code = validate_support_code(support_code)
    created_at, created_time = _inventory_timestamp(value["created_at"], "creation timestamp")
    expires_at, expires_time = _inventory_timestamp(value["expires_at"], "expiry timestamp")
    if expires_time <= created_time or expires_time - created_time > timedelta(days=31):
        raise SupportDiagnosticsError("Inventory response has an invalid expiry timestamp.")
    upload_state = value["upload_state"]
    if not isinstance(upload_state, str) or upload_state not in UPLOAD_STATES:
        raise SupportDiagnosticsError("Inventory response has an invalid upload state.")
    privacy_rules_version = value["privacy_rules_version"]
    if privacy_rules_version is not None:
        privacy_rules_version = _inventory_integer(
            privacy_rules_version,
            "privacy rules version",
            minimum=1,
            maximum=MAX_JAVASCRIPT_SAFE_INTEGER,
        )
    entry = ReportInventoryEntry(
        bundle_schema_version=_inventory_integer(
            value["bundle_schema_version"],
            "bundle schema version",
            minimum=1,
            maximum=MAX_JAVASCRIPT_SAFE_INTEGER,
        ),
        created_at=created_at,
        expires_at=expires_at,
        privacy_rules_version=privacy_rules_version,
        size_bytes=_inventory_integer(
            value["size_bytes"],
            "report size",
            minimum=1,
            maximum=MAX_BUNDLE_BYTES,
        ),
        support_code=support_code,
        upload_state=upload_state,
    )
    return entry, created_time


def _parse_inventory(data: bytes) -> ReportInventory:
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise SupportDiagnosticsError("Inventory response is not valid JSON.") from error
    if not isinstance(payload, dict) or set(payload) != {"reports", "schema_version"}:
        raise SupportDiagnosticsError("Inventory response has an invalid schema.")
    schema_version = _inventory_integer(
        payload["schema_version"],
        "schema version",
        minimum=SCHEMA_VERSION,
        maximum=SCHEMA_VERSION,
    )
    report_values = payload["reports"]
    if not isinstance(report_values, list) or len(report_values) > MAX_INVENTORY_REPORTS:
        raise SupportDiagnosticsError("Inventory response has an invalid report list.")
    reports_with_times = [_inventory_entry(value) for value in report_values]
    support_codes = [report.support_code for report, _ in reports_with_times]
    if len(set(support_codes)) != len(support_codes):
        raise SupportDiagnosticsError("Inventory response contains duplicate support codes.")
    for (previous, previous_time), (current, current_time) in itertools.pairwise(reports_with_times):
        if current_time > previous_time or (
            current_time == previous_time and current.support_code < previous.support_code
        ):
            raise SupportDiagnosticsError("Inventory response is not ordered newest-first.")
    return ReportInventory(
        reports=tuple(report for report, _ in reports_with_times),
        schema_version=schema_version,
    )


def _schema_document(payload: bytes, name: str, expected_schema_version: int) -> dict[str, object]:
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SupportDiagnosticsError(f"{name} is not valid UTF-8 JSON.") from error
    if (
        not isinstance(document, dict)
        or type(document.get("schema_version")) is not int
        or document["schema_version"] != expected_schema_version
    ):
        raise SupportDiagnosticsError(f"{name} schema verification failed.")
    return document


def _require_archive_range(data: bytes, offset: int, length: int) -> None:
    if offset < 0 or length < 0 or offset + length > len(data):
        raise SupportDiagnosticsError("Diagnostic bundle is not a valid ZIP archive.")


def _archive_payloads(data: bytes) -> dict[str, bytes]:
    end_offset = len(data) - END_OF_CENTRAL_DIRECTORY.size
    _require_archive_range(data, end_offset, END_OF_CENTRAL_DIRECTORY.size)
    (
        signature,
        disk_number,
        central_directory_disk,
        disk_entry_count,
        entry_count,
        central_directory_size,
        central_directory_offset,
        comment_length,
    ) = END_OF_CENTRAL_DIRECTORY.unpack_from(data, end_offset)
    if (
        signature != END_OF_CENTRAL_DIRECTORY_SIGNATURE
        or disk_number != 0
        or central_directory_disk != 0
        or disk_entry_count != len(ENTRY_LIMITS)
        or entry_count != len(ENTRY_LIMITS)
        or comment_length != 0
        or central_directory_offset + central_directory_size != end_offset
    ):
        raise SupportDiagnosticsError("Diagnostic bundle is not a valid ZIP archive.")

    entries: list[_ZipEntry] = []
    names: set[str] = set()
    total_uncompressed_size = 0
    offset = central_directory_offset
    for _ in range(entry_count):
        _require_archive_range(data, offset, CENTRAL_DIRECTORY_HEADER.size)
        (
            entry_signature,
            _version_made_by,
            version_needed,
            flags,
            compression_method,
            _modification_time,
            _modification_date,
            checksum,
            compressed_size,
            uncompressed_size,
            name_length,
            extra_length,
            entry_comment_length,
            disk_start,
            _internal_attributes,
            _external_attributes,
            local_header_offset,
        ) = CENTRAL_DIRECTORY_HEADER.unpack_from(data, offset)
        variable_length = name_length + extra_length + entry_comment_length
        _require_archive_range(data, offset + CENTRAL_DIRECTORY_HEADER.size, variable_length)
        try:
            name = data[
                offset + CENTRAL_DIRECTORY_HEADER.size : offset + CENTRAL_DIRECTORY_HEADER.size + name_length
            ].decode("utf-8")
        except UnicodeDecodeError as error:
            raise SupportDiagnosticsError("Diagnostic bundle is not a valid ZIP archive.") from error
        limit = ENTRY_LIMITS.get(name)
        if (
            entry_signature != CENTRAL_DIRECTORY_SIGNATURE
            or version_needed != 20
            or flags not in ALLOWED_ZIP_FLAGS
            or compression_method != ZIP_DEFLATED
            or extra_length != 0
            or entry_comment_length != 0
            or disk_start != 0
            or limit is None
            or name in names
            or uncompressed_size > limit
        ):
            raise SupportDiagnosticsError("Diagnostic bundle contains an invalid archive entry.")
        names.add(name)
        total_uncompressed_size += uncompressed_size
        if total_uncompressed_size > MAX_UNCOMPRESSED_ARCHIVE_BYTES:
            raise SupportDiagnosticsError("Diagnostic bundle expands beyond the allowed size.")
        entries.append(
            _ZipEntry(
                name=name,
                flags=flags,
                crc32=checksum,
                compressed_size=compressed_size,
                uncompressed_size=uncompressed_size,
                local_header_offset=local_header_offset,
            )
        )
        offset += CENTRAL_DIRECTORY_HEADER.size + variable_length
    if offset != end_offset or names != set(ENTRY_LIMITS):
        raise SupportDiagnosticsError("Diagnostic bundle has an invalid file set.")

    payloads: dict[str, bytes] = {}
    ranges: list[tuple[int, int]] = []
    for entry in entries:
        offset = entry.local_header_offset
        _require_archive_range(data, offset, LOCAL_FILE_HEADER.size)
        (
            local_signature,
            version_needed,
            flags,
            compression_method,
            _modification_time,
            _modification_date,
            checksum,
            compressed_size,
            uncompressed_size,
            name_length,
            extra_length,
        ) = LOCAL_FILE_HEADER.unpack_from(data, offset)
        _require_archive_range(data, offset + LOCAL_FILE_HEADER.size, name_length + extra_length)
        try:
            local_name = data[offset + LOCAL_FILE_HEADER.size : offset + LOCAL_FILE_HEADER.size + name_length].decode(
                "utf-8"
            )
        except UnicodeDecodeError as error:
            raise SupportDiagnosticsError("Diagnostic bundle is not a valid ZIP archive.") from error
        if (
            local_signature != LOCAL_FILE_HEADER_SIGNATURE
            or version_needed != 20
            or flags != entry.flags
            or compression_method != ZIP_DEFLATED
            or checksum != entry.crc32
            or compressed_size != entry.compressed_size
            or uncompressed_size != entry.uncompressed_size
            or extra_length != 0
            or local_name != entry.name
        ):
            raise SupportDiagnosticsError("Diagnostic bundle contains an invalid archive entry.")
        data_offset = offset + LOCAL_FILE_HEADER.size + name_length
        range_end = data_offset + compressed_size
        _require_archive_range(data, data_offset, compressed_size)
        if range_end > central_directory_offset:
            raise SupportDiagnosticsError("Diagnostic bundle is not a valid ZIP archive.")
        compressed = data[data_offset:range_end]
        limit = ENTRY_LIMITS[entry.name]
        try:
            decompressor = zlib.decompressobj(-zlib.MAX_WBITS)
            payload = decompressor.decompress(compressed, limit + 1)
            if len(payload) <= limit:
                payload += decompressor.flush(limit + 1 - len(payload))
        except zlib.error as error:
            raise SupportDiagnosticsError("Diagnostic bundle is not a valid ZIP archive.") from error
        if (
            len(payload) != entry.uncompressed_size
            or len(payload) > limit
            or not decompressor.eof
            or decompressor.unconsumed_tail
            or decompressor.unused_data
            or zlib.crc32(payload) & 0xFFFFFFFF != entry.crc32
        ):
            raise SupportDiagnosticsError("Diagnostic bundle contains an invalid archive entry.")
        ranges.append((offset, range_end))
        payloads[entry.name] = payload

    ranges.sort()
    if not ranges or ranges[0][0] != 0 or ranges[-1][1] != central_directory_offset:
        raise SupportDiagnosticsError("Diagnostic bundle is not a valid ZIP archive.")
    for index in range(1, len(ranges)):
        if ranges[index - 1][1] != ranges[index][0]:
            raise SupportDiagnosticsError("Diagnostic bundle is not a valid ZIP archive.")
    return payloads


def verify_bundle(data: bytes, expected_sha256: str, expected_schema_version: int) -> None:
    if len(data) == 0 or len(data) > MAX_BUNDLE_BYTES:
        raise SupportDiagnosticsError("Diagnostic bundle has an invalid size.")
    if SHA256_PATTERN.fullmatch(expected_sha256) is None:
        raise SupportDiagnosticsError("Service response has an invalid checksum.")
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise SupportDiagnosticsError("Diagnostic bundle checksum verification failed.")
    if expected_schema_version != SCHEMA_VERSION:
        raise SupportDiagnosticsError("Diagnostic bundle uses an unsupported schema version.")

    payloads = _archive_payloads(data)

    _schema_document(payloads["manifest.json"], "manifest.json", expected_schema_version)
    _schema_document(payloads["storage.json"], "storage.json", expected_schema_version)
    try:
        events = payloads["events.jsonl"].decode("utf-8")
    except UnicodeDecodeError as error:
        raise SupportDiagnosticsError("events.jsonl is not valid UTF-8.") from error
    for line in events.splitlines():
        if line:
            _schema_document(line.encode("utf-8"), "events.jsonl entry", expected_schema_version)
    try:
        tool_tail = payloads["tool-tail.txt"].decode("utf-8")
    except UnicodeDecodeError as error:
        raise SupportDiagnosticsError("tool-tail.txt is not valid UTF-8.") from error
    if not tool_tail.startswith(f"# bd_to_avp_support_tool_tail schema_version={expected_schema_version}\n"):
        raise SupportDiagnosticsError("tool-tail.txt schema verification failed.")


def _atomic_write(output: Path, data: bytes) -> None:
    if output.exists():
        raise SupportDiagnosticsError(f"Refusing to overwrite existing file: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp", dir=output.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as destination:
            destination.write(data)
            destination.flush()
            os.fsync(destination.fileno())
        temporary_path.replace(output)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def fetch_report(
    configuration: ClientConfiguration,
    support_code: str,
    output: Path,
    opener: ResponseOpener = _DEFAULT_OPENER,
) -> FetchResult:
    code = validate_support_code(support_code)
    request = _request(report_url(configuration, code), "GET", configuration.token)
    response = _open(opener, request)
    with response:
        content_type = _required_header(response.headers, "Content-Type").split(";", 1)[0].strip().lower()
        if content_type != "application/zip":
            raise SupportDiagnosticsError("Service response has an unexpected Content-Type.")
        size_bytes = _parse_content_length(response.headers)
        checksum = _required_header(response.headers, "X-Diagnostic-SHA256")
        schema_text = _required_header(response.headers, "X-Diagnostic-Schema-Version")
        if not schema_text.isdecimal():
            raise SupportDiagnosticsError("Service response has an invalid schema version.")
        data = response.read(MAX_BUNDLE_BYTES + 1)

    if len(data) != size_bytes:
        raise SupportDiagnosticsError("Service response body size does not match Content-Length.")
    schema_version = int(schema_text)
    verify_bundle(data, checksum, schema_version)
    _atomic_write(output, data)
    return FetchResult(
        output=output,
        schema_version=schema_version,
        sha256=checksum,
        size_bytes=size_bytes,
        support_code=code,
    )


def list_reports(
    configuration: ClientConfiguration,
    opener: ResponseOpener = _DEFAULT_OPENER,
) -> ReportInventory:
    request = _request(
        inventory_url(configuration),
        "GET",
        configuration.token,
        accept="application/json",
    )
    response = _open(opener, request)
    with response:
        content_type = _required_header(response.headers, "Content-Type").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise SupportDiagnosticsError("Service response has an unexpected Content-Type.")
        declared_length = response.headers.get("Content-Length")
        if declared_length is not None:
            if len(declared_length) > len(str(MAX_INVENTORY_BYTES)) or not declared_length.isdecimal():
                raise SupportDiagnosticsError("Inventory response has an invalid Content-Length.")
            if int(declared_length) > MAX_INVENTORY_BYTES:
                raise SupportDiagnosticsError("Inventory response exceeds the response limit.")
        data = response.read(MAX_INVENTORY_BYTES + 1)
    if len(data) > MAX_INVENTORY_BYTES:
        raise SupportDiagnosticsError("Inventory response exceeds the response limit.")
    if declared_length is not None and len(data) != int(declared_length):
        raise SupportDiagnosticsError("Inventory response body size does not match Content-Length.")
    return _parse_inventory(data)


def delete_report(
    configuration: ClientConfiguration,
    support_code: str,
    opener: ResponseOpener = _DEFAULT_OPENER,
) -> None:
    code = validate_support_code(support_code)
    request = _request(report_url(configuration, code), "DELETE", configuration.token)
    response = _open(opener, request)
    with response:
        status = getattr(response, "status", 204)
        if status != 204:
            raise SupportDiagnosticsError(f"Service returned unexpected HTTP {status}.")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="List, fetch, or delete private BD_to_AVP diagnostic bundles.")
    subcommands = parser.add_subparsers(dest="command", required=True)

    subcommands.add_parser("list", help="List active private reports without downloading bundle contents.")

    fetch_parser = subcommands.add_parser("fetch", help="Fetch, checksum, and schema-validate a diagnostic bundle.")
    fetch_parser.add_argument("support_code")
    fetch_parser.add_argument("--output", type=Path)

    delete_parser = subcommands.add_parser("delete", help="Delete a diagnostic bundle from private storage.")
    delete_parser.add_argument("support_code")
    delete_parser.add_argument("--yes", action="store_true", help="Confirm permanent deletion.")
    return parser.parse_args(argv)


def main(
    argv: list[str] | None = None,
    environ: Mapping[str, str] | None = None,
    opener: ResponseOpener = _DEFAULT_OPENER,
) -> int:
    try:
        arguments = parse_args(argv)
        configuration = load_configuration(environ)
        if arguments.command == "list":
            inventory = list_reports(configuration, opener)
            print(
                json.dumps(
                    {
                        "reports": [
                            {
                                "bundle_schema_version": report.bundle_schema_version,
                                "created_at": report.created_at,
                                "expires_at": report.expires_at,
                                "privacy_rules_version": report.privacy_rules_version,
                                "size_bytes": report.size_bytes,
                                "support_code": report.support_code,
                                "upload_state": report.upload_state,
                            }
                            for report in inventory.reports
                        ],
                        "schema_version": inventory.schema_version,
                    },
                    sort_keys=True,
                )
            )
            return 0
        support_code = validate_support_code(arguments.support_code)
        if arguments.command == "fetch":
            output = arguments.output or Path(f"{support_code}.zip")
            result = fetch_report(configuration, support_code, output, opener)
            print(
                json.dumps(
                    {
                        "output": str(result.output),
                        "schema_version": result.schema_version,
                        "sha256": result.sha256,
                        "size_bytes": result.size_bytes,
                        "support_code": result.support_code,
                    },
                    sort_keys=True,
                )
            )
            return 0
        if arguments.command == "delete":
            if not arguments.yes:
                raise SupportDiagnosticsError("Deletion requires --yes.")
            delete_report(configuration, support_code, opener)
            print(json.dumps({"deleted": True, "support_code": support_code}, sort_keys=True))
            return 0
        raise SupportDiagnosticsError("Unknown command.")
    except SupportDiagnosticsError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
