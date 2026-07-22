# Private Diagnostic Upload Service

This directory is a self-contained Cloudflare Worker for user-initiated,
private BD_to_AVP diagnostic bundles. It has no public read or list endpoint,
does not embed a service credential in the macOS application, and stores
bundles only in the private `DIAGNOSTIC_BUNDLES` R2 binding. Maintainers can
list bounded report metadata only through the same authenticated private API
used for retrieval and deletion.

The service uses a SQLite-backed Durable Object to serialize report state
transitions. This makes each upload authorization single-use even when two
clients race to use the same token. It uses two Cloudflare Rate Limiting
bindings plus a 24-hour hashed-client quota and a 500 live-report limit to
bound abuse and storage exposure.

## Bundle Contract

- Content type: `application/zip`
- Maximum compressed size: `2 MiB` (`2,097,152` bytes)
- Archive format: ZIP with raw-DEFLATE entries
- Required top-level members: `manifest.json`, `events.jsonl`, `storage.json`,
  and `tool-tail.txt`
- Uncompressed limits: 64 KiB manifest, 320 KiB events, 160 KiB storage,
  640 KiB tool tail, and 1,500,000 bytes total
- Required schema field: integer `{"schema_version": 1, ...}` in the manifest,
  storage document, and each event; the tool tail carries the matching header
- The client declares the exact size, SHA-256, content type, and bundle schema
  before receiving upload authorization.

The Worker validates size, content type, request checksum header, SHA-256,
archive structure, exact member set, compression method, matching local and
central headers, and declared decompressed limits before storage. It does not
inflate entries, compute ZIP CRCs, or parse bundle schemas on the public upload
path so the service can target the Workers Free CPU budget. It
records the checksum and expiry in R2 metadata. The trusted macOS client creates
the bounded archive, and the maintainer CLI verifies the response, checksum,
archive safety, actual CRCs, decompressed limits, and schema before writing an
archive to disk. A shared native-client fixture keeps the Swift producer,
Worker envelope validator, and Python maintainer validator aligned.

## HTTP Contract

All production requests must use HTTPS. Responses are `Cache-Control:
no-store`; the Worker deliberately emits no CORS headers.

### Create a report

`POST /v1/reports`

```json
{
  "bundle_schema_version": 1,
  "content_type": "application/zip",
  "privacy_rules_version": 4,
  "sha256": "<64-character lowercase SHA-256 hex>",
  "size_bytes": 12345
}
```

The service rejects missing or pre-v4 privacy rules before issuing upload
credentials. This is a fail-closed admission gate for official clients with
known-incomplete redaction boundaries, not attestation of arbitrary callers or
bundle contents. Rejected app clients preserve their local Save/Share fallback.
Upload authorization records also retain the admitted privacy-rules version, so
authorizations created before this gate was deployed fail at upload time.

On `201 Created`, the service returns an opaque UUID `report_id`, a random
`BDAVP-...` support code, a ten-minute single-use upload authorization, and a
ten-minute status authorization. Both authorizations are random bearer values
returned only in this response. A support code is an identifier for a
maintainer; it never authorizes reads.

```json
{
  "expires_at": "2026-08-17T12:00:00.000Z",
  "report_id": "<opaque UUID>",
  "schema_version": 1,
  "status": {
    "expires_at": "2026-07-18T12:10:00.000Z",
    "headers": { "Authorization": "Bearer <status-token>" },
    "method": "GET",
    "url": "https://support.example/v1/reports/<opaque UUID>/status"
  },
  "support_code": "BDAVP-0123456789ABCDEF",
  "upload": {
    "expires_at": "2026-07-18T12:10:00.000Z",
    "headers": {
      "Authorization": "Bearer <upload-token>",
      "Content-Length": "12345",
      "Content-Type": "application/zip",
      "X-Content-SHA256": "<64-character lowercase SHA-256 hex>"
    },
    "method": "PUT",
    "url": "https://support.example/v1/reports/<opaque UUID>/upload"
  }
}
```

The public endpoint rate limits creates and uploads using a salted hash of
`CF-Connecting-IP`; no raw IP address is persisted or logged. A shared-network
user can receive `429` after the configured quota, which is intentional for
this unauthenticated, abuse-bounded endpoint.

### Upload and finalize

`PUT /v1/reports/{report_id}/upload`

Use the exact authorization and headers returned from creation. The successful
`201` PUT is the finalization step. After headers match, the Worker atomically
reserves the single-use authorization before reading the body, validates the
bounded ZIP envelope and its checksum, writes the private R2 object, and changes
the Durable Object state to `uploaded`. Structurally invalid body bytes fail the
reserved report and require a fresh report; concurrent or later replays return
`409 upload_consumed`. Semantic content, CRC, and schema validation is performed
by the maintainer CLI before any fetched archive is written to disk.

`GET /v1/reports/{report_id}/status` requires the returned short-lived status
bearer token. It returns only the opaque report ID, upload state, and retention
expiry; it does not return the bundle, support code, or metadata.

### Maintainer inventory, retrieval, and deletion

All three routes require `Authorization: Bearer $SUPPORT_DIAGNOSTICS_TOKEN`, which
is checked with fixed-length SHA-256 digests and a constant-time comparison.

```text
GET    /v1/maintainer/reports
GET    /v1/maintainer/reports/{support_code}
DELETE /v1/maintainer/reports/{support_code}
```

The inventory GET returns every live report, bounded by the 500-report service
capacity and ordered by creation time descending with support code as the tie
breaker. Each item contains only support code, upload state, creation and expiry
times, compressed size, bundle schema, and privacy-rules version. It never
returns bundle content, checksums, report IDs, object keys, client fingerprints,
or authorization material.

The support-code GET response is the ZIP archive with `X-Diagnostic-SHA256`,
`X-Diagnostic-Schema-Version`, and `X-Diagnostic-Privacy-Rules-Version`
headers. The DELETE response is `204 No Content`. Unknown support codes return
`404` only after maintainer authentication. There is no unauthenticated list
endpoint, public object route, or support-code-only read route.

## Retention and Storage

Each object is stored under the private `reports/` prefix with `created_at`,
`expires_at`, report ID, bundle schema, privacy-rules version, checksum, and size
as R2 custom metadata. The Worker deletes expired records from its Durable
Object and R2 through both a Durable Object alarm and an hourly cron.
`r2-lifecycle.json` defines a second 30-day R2 lifecycle deletion rule
(`2,592,000` seconds) for the same prefix.

Keep the R2 bucket private: do not attach a public custom domain, do not
configure a public bucket route, and do not grant anonymous R2 access. R2
lifecycle deletion is asynchronous, so Worker expiry cleanup is intentionally
the first enforcement layer; the lifecycle rule is the storage-layer backstop.

## Maintainer CLI

The repository-native CLI uses only Python's standard library. It reads its
endpoint and bearer token exclusively from the environment; it never accepts a
maintainer token on the command line.

```sh
export SUPPORT_DIAGNOSTICS_ENDPOINT='https://support.example'
export SUPPORT_DIAGNOSTICS_TOKEN='<maintainer bearer token>'

uv run python scripts/support_diagnostics.py list
uv run python scripts/support_diagnostics.py fetch BDAVP-0123456789ABCDEF \
  --output /secure/path/report.zip
uv run python scripts/support_diagnostics.py delete BDAVP-0123456789ABCDEF --yes
```

`list` reads at most 256 KiB, validates the complete metadata schema and
newest-first ordering, and rejects unexpected fields before printing normalized
JSON. The CLI rejects redirects so its bearer token cannot be forwarded to a
different origin. `fetch` refuses non-HTTPS endpoints, does not overwrite an existing file,
streams at most 2 MiB from the response, verifies the response checksum/schema,
validates the ZIP archive without extracting it, and writes only a
validated bundle. `delete` requires `--yes`.

## Local Checks

```sh
cd support-diagnostics
npm ci
npm run check
npm run deploy:dry-run

cd ..
uv run python -m unittest tests.test_support_diagnostics
```

The TypeScript tests use an in-memory R2 and Durable Object storage equivalent.
They cover authorization replay, authorization expiry, support-code guessing,
wrong content type/size/checksum, ZIP envelope and declared expansion limits,
rate limits, public-read denial, maintainer authentication, deletion, expiry
cleanup, and safe failure logging. The Python tests cover valid retrieval plus
checksum, CRC, decompressed limits, malformed archive, schema, delete,
confirmation, inventory validation, and HTTP-failure handling.

## Production Deployment

Cloudflare authentication and Worker secrets are not provisioned by this
repository. Before any deployment:

1. Create a private R2 bucket named `bd-to-avp-support-diagnostics`, or update
   `wrangler.jsonc` to the approved private bucket name.
2. Reserve the two positive-integer rate-limit namespace IDs in
   `wrangler.jsonc` so they are unique in the target Cloudflare account.
3. Configure an HTTPS custom Worker route for the approved support domain while
   leaving `workers_dev` disabled.
4. Apply the 30-day `reports/` lifecycle rule represented by
   `r2-lifecycle.json` through the Cloudflare dashboard/API or Wrangler's
   `r2 bucket lifecycle add` command. For example:

   ```sh
   npx wrangler r2 bucket lifecycle add bd-to-avp-support-diagnostics \
     expire-diagnostic-bundles reports/ --expire-days 30
   ```

5. Set the required Worker secrets without adding them to a file or command
   history:

   ```sh
   npx wrangler secret put MAINTAINER_TOKEN
   npx wrangler secret put RATE_LIMIT_SALT
   ```

6. Run `npm run deploy:dry-run`, review the route, binding, lifecycle, and
   secret names, then deploy only with explicit Cloudflare authorization.
7. For the privacy-rules v4 cutoff, deploy the Worker before distributing the
   updated native client. This intentionally disables Beta 3 online submission,
   invalidates its outstanding upload authorizations, and leaves local
   Save/Share available. The updated client cannot submit to the old Worker.
8. Submit representative and maximum-size bundles, then verify request CPU in
   Cloudflare analytics remains within the Workers Free limit before setting
   the production release endpoint. Do not enable a paid Workers plan as an
   automatic fallback.

The checked-in `wrangler.jsonc` declares a new SQLite Durable Object through
the modern `exports` lifecycle block and two Worker Rate Limiting bindings.
Rate-limit namespace IDs are positive integers represented as strings.
