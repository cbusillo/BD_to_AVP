# Private Diagnostic Upload Service

This directory is a self-contained Cloudflare Worker for user-initiated,
private BD_to_AVP diagnostic bundles. It has no public read or list endpoint,
does not embed a service credential in the macOS application, and stores
bundles only in the private `DIAGNOSTIC_BUNDLES` R2 binding.

The service uses a SQLite-backed Durable Object to serialize report state
transitions. This makes each upload authorization single-use even when two
clients race to use the same token. It uses two Cloudflare Rate Limiting
bindings plus a 24-hour hashed-client quota and a 500 live-report limit to
bound abuse and storage exposure.

## Bundle Contract

- Content type: `application/gzip`
- Maximum compressed size: `2 MiB` (`2,097,152` bytes)
- Archive format: gzip-compressed tar archive
- Required archive member: a regular top-level `diagnostic.json`
- Required JSON field: `{"schema_version": 1, ...}`
- The client declares the exact size, SHA-256, content type, and bundle schema
  before receiving upload authorization.

The Worker validates size, content type, request checksum header, and the
SHA-256 of the received bytes. It records the same checksum and expiry in R2
metadata. The maintainer CLI validates those response headers, the downloaded
checksum, archive safety, and the `diagnostic.json` schema before writing an
archive to disk.

## HTTP Contract

All production requests must use HTTPS. Responses are `Cache-Control:
no-store`; the Worker deliberately emits no CORS headers.

### Create a report

`POST /v1/reports`

```json
{
  "bundle_schema_version": 1,
  "content_type": "application/gzip",
  "sha256": "<64-character lowercase SHA-256 hex>",
  "size_bytes": 12345
}
```

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
      "Content-Type": "application/gzip",
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
`201` PUT is the atomic finalization step: the Worker writes the private R2
object only after the bounded body matches the report's authorized size and
SHA-256, then changes the Durable Object state to `uploaded`. A token cannot
produce a second stored object; a replay returns `409 upload_consumed`.

`GET /v1/reports/{report_id}/status` requires the returned short-lived status
bearer token. It returns only the opaque report ID, upload state, and retention
expiry; it does not return the bundle, support code, or metadata.

### Maintainer retrieval and deletion

Both routes require `Authorization: Bearer $SUPPORT_DIAGNOSTICS_TOKEN`, which
is checked with fixed-length SHA-256 digests and a constant-time comparison.

```text
GET    /v1/maintainer/reports/{support_code}
DELETE /v1/maintainer/reports/{support_code}
```

The GET response is the gzip archive with `X-Diagnostic-SHA256` and
`X-Diagnostic-Schema-Version` headers. The DELETE response is `204 No Content`.
Unknown support codes return `404` only after maintainer authentication. There
is no public list endpoint, public object route, or support-code-only read
route.

## Retention and Storage

Each object is stored under the private `reports/` prefix with `created_at`,
`expires_at`, report ID, schema, checksum, and size as R2 custom metadata. The
Worker deletes expired records from its Durable Object and R2 through both a
Durable Object alarm and an hourly cron. `r2-lifecycle.json` defines a second
30-day R2 lifecycle deletion rule (`2,592,000` seconds) for the same prefix.

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

uv run python scripts/support_diagnostics.py fetch BDAVP-0123456789ABCDEF \
  --output /secure/path/report.tar.gz
uv run python scripts/support_diagnostics.py delete BDAVP-0123456789ABCDEF --yes
```

`fetch` refuses non-HTTPS endpoints, does not overwrite an existing file,
streams at most 2 MiB from the response, verifies the response checksum/schema,
validates the gzip tar archive without extracting it, and writes only a
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
wrong content type/size/checksum, rate limits, public-read denial, maintainer
authentication, deletion, expiry cleanup, and safe failure logging. The Python
tests cover valid retrieval plus checksum, malformed archive, schema, delete,
confirmation, and HTTP-failure handling.

## Future Deployment

No Cloudflare account, Wrangler authentication, R2 bucket, route/domain, or
Worker secrets are provisioned by this repository. Before any deployment:

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

The checked-in `wrangler.jsonc` declares a new SQLite Durable Object through
the modern `exports` lifecycle block and two Worker Rate Limiting bindings.
Rate-limit namespace IDs are positive integers represented as strings.
