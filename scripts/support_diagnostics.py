from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping, Protocol, cast


MAX_BUNDLE_BYTES = 2 * 1024 * 1024
MAX_DIAGNOSTIC_JSON_BYTES = 1024 * 1024
MAX_ARCHIVE_MEMBERS = 16
MAX_UNCOMPRESSED_ARCHIVE_BYTES = 8 * 1024 * 1024
SCHEMA_VERSION = 1
SUPPORT_CODE_PATTERN = re.compile(r"^BDAVP-[0-9ABCDEFGHJKMNPQRSTVWXYZ]{16}$")
SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")


class SupportDiagnosticsError(RuntimeError):
    pass


class ResponseLike(Protocol):
    headers: Mapping[str, str]

    def __enter__(self) -> ResponseLike: ...

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None: ...

    def read(self, amount: int = -1) -> bytes: ...


ResponseOpener = Callable[..., object]


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


def _request(url: str, method: str, token: str) -> urllib.request.Request:
    return urllib.request.Request(
        url,
        headers={
            "Accept": "application/gzip",
            "Authorization": f"Bearer {token}",
            "User-Agent": "bd-to-avp-support-diagnostics-cli/1",
        },
        method=method,
    )


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


def _safe_member_name(name: str) -> bool:
    path = PurePosixPath(name)
    return bool(name) and not path.is_absolute() and ".." not in path.parts


def verify_bundle(data: bytes, expected_sha256: str, expected_schema_version: int) -> None:
    if len(data) == 0 or len(data) > MAX_BUNDLE_BYTES:
        raise SupportDiagnosticsError("Diagnostic bundle has an invalid size.")
    if SHA256_PATTERN.fullmatch(expected_sha256) is None:
        raise SupportDiagnosticsError("Service response has an invalid checksum.")
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise SupportDiagnosticsError("Diagnostic bundle checksum verification failed.")
    if expected_schema_version != SCHEMA_VERSION:
        raise SupportDiagnosticsError("Diagnostic bundle uses an unsupported schema version.")

    diagnostic_payload: bytes | None = None
    member_count = 0
    uncompressed_size = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            for member in archive:
                member_count += 1
                if member_count > MAX_ARCHIVE_MEMBERS:
                    raise SupportDiagnosticsError("Diagnostic bundle contains too many files.")
                if not _safe_member_name(member.name) or not member.isreg():
                    raise SupportDiagnosticsError("Diagnostic bundle contains an unsafe archive entry.")
                uncompressed_size += member.size
                if uncompressed_size > MAX_UNCOMPRESSED_ARCHIVE_BYTES:
                    raise SupportDiagnosticsError("Diagnostic bundle expands beyond the allowed size.")
                if member.name == "diagnostic.json":
                    if diagnostic_payload is not None or member.size > MAX_DIAGNOSTIC_JSON_BYTES:
                        raise SupportDiagnosticsError("Diagnostic bundle has an invalid diagnostic.json entry.")
                    member_file = archive.extractfile(member)
                    if member_file is None:
                        raise SupportDiagnosticsError("Diagnostic bundle has an unreadable diagnostic.json entry.")
                    diagnostic_payload = member_file.read(MAX_DIAGNOSTIC_JSON_BYTES + 1)
                    if len(diagnostic_payload) > MAX_DIAGNOSTIC_JSON_BYTES:
                        raise SupportDiagnosticsError("Diagnostic bundle has an oversized diagnostic.json entry.")
    except (OSError, tarfile.TarError) as error:
        raise SupportDiagnosticsError("Diagnostic bundle is not a valid gzip tar archive.") from error

    if diagnostic_payload is None:
        raise SupportDiagnosticsError("Diagnostic bundle is missing diagnostic.json.")
    try:
        diagnostic = json.loads(diagnostic_payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SupportDiagnosticsError("diagnostic.json is not valid UTF-8 JSON.") from error
    if not isinstance(diagnostic, dict) or diagnostic.get("schema_version") != expected_schema_version:
        raise SupportDiagnosticsError("diagnostic.json schema verification failed.")


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
    opener: ResponseOpener = urllib.request.urlopen,
) -> FetchResult:
    code = validate_support_code(support_code)
    request = _request(report_url(configuration, code), "GET", configuration.token)
    response = _open(opener, request)
    with response:
        content_type = _required_header(response.headers, "Content-Type").split(";", 1)[0].strip().lower()
        if content_type != "application/gzip":
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


def delete_report(
    configuration: ClientConfiguration,
    support_code: str,
    opener: ResponseOpener = urllib.request.urlopen,
) -> None:
    code = validate_support_code(support_code)
    request = _request(report_url(configuration, code), "DELETE", configuration.token)
    response = _open(opener, request)
    with response:
        status = getattr(response, "status", 204)
        if status != 204:
            raise SupportDiagnosticsError(f"Service returned unexpected HTTP {status}.")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch or delete private BD_to_AVP diagnostic bundles by support code."
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

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
    opener: ResponseOpener = urllib.request.urlopen,
) -> int:
    try:
        arguments = parse_args(argv)
        configuration = load_configuration(environ)
        support_code = validate_support_code(arguments.support_code)
        if arguments.command == "fetch":
            output = arguments.output or Path(f"{support_code}.tar.gz")
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
