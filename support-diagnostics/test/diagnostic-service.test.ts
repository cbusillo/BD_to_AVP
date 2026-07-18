import { describe, expect, it } from "vitest";

import { sha256Hex } from "../src/crypto.js";
import { createDiagnosticService } from "../src/index.js";
import {
  API_SCHEMA_VERSION,
  BUNDLE_CONTENT_TYPE,
  MAX_DAILY_REPORTS_PER_CLIENT,
  RETENTION_MS,
  UPLOAD_AUTH_TTL_MS,
  type CreateReportRecordInput,
} from "../src/protocol.js";
import { ReportRegistryService } from "../src/registry.js";
import type {
  DurableObjectStorage,
  R2Bucket,
  R2ObjectBody,
  R2PutOptions,
  RateLimitBinding,
  ServiceLogger,
} from "../src/types.js";

interface CreatedReport {
  report_id: string;
  status: {
    headers: {
      Authorization: string;
    };
    url: string;
  };
  support_code: string;
  upload: {
    headers: {
      Authorization: string;
      "Content-Length": string;
      "Content-Type": string;
      "X-Content-SHA256": string;
    };
    url: string;
  };
}

class MemoryStorage implements DurableObjectStorage {
  readonly values = new Map<string, unknown>();
  alarm: number | undefined;

  async delete(key: string): Promise<boolean> {
    return this.values.delete(key);
  }

  async get<Value>(key: string): Promise<Value | undefined> {
    return this.values.get(key) as Value | undefined;
  }

  async list<Value>(options?: {
    prefix?: string;
  }): Promise<Map<string, Value>> {
    const values = new Map<string, Value>();
    for (const [key, value] of this.values) {
      if (options?.prefix === undefined || key.startsWith(options.prefix)) {
        values.set(key, value as Value);
      }
    }
    return values;
  }

  async put<Value>(key: string, value: Value): Promise<void> {
    this.values.set(key, structuredClone(value));
  }

  async setAlarm(scheduledTime: number): Promise<void> {
    this.alarm = scheduledTime;
  }
}

interface MemoryObject {
  bytes: Uint8Array;
  customMetadata?: Record<string, string>;
}

class MemoryBucket implements R2Bucket {
  readonly objects = new Map<string, MemoryObject>();
  failPuts = false;
  failDeletes = false;

  async delete(key: string): Promise<void> {
    if (this.failDeletes) {
      throw new Error("delete failed");
    }
    this.objects.delete(key);
  }

  async get(key: string): Promise<R2ObjectBody | null> {
    const object = this.objects.get(key);
    if (object === undefined) {
      return null;
    }
    const response: R2ObjectBody = {
      body: new ReadableStream<Uint8Array>({
        start(controller) {
          controller.enqueue(object.bytes);
          controller.close();
        },
      }),
      key,
      size: object.bytes.byteLength,
    };
    if (object.customMetadata !== undefined) {
      response.customMetadata = object.customMetadata;
    }
    return response;
  }

  async put(
    key: string,
    value: ArrayBuffer,
    options?: R2PutOptions,
  ): Promise<void> {
    if (this.failPuts) {
      throw new Error("put failed");
    }
    const object: MemoryObject = { bytes: new Uint8Array(value.slice(0)) };
    if (options?.customMetadata !== undefined) {
      object.customMetadata = options.customMetadata;
    }
    this.objects.set(key, object);
  }
}

class FixedRateLimiter implements RateLimitBinding {
  constructor(private readonly allowed = true) {}

  async limit(): Promise<{ success: boolean }> {
    return { success: this.allowed };
  }
}

interface TestHarness {
  bucket: MemoryBucket;
  clock: { now: number };
  logs: Array<{ event: string; fields: Record<string, string | number> }>;
  registry: ReportRegistryService;
  service: ReturnType<typeof createDiagnosticService>;
  storage: MemoryStorage;
}

function makeHarness(
  options: { createAllowed?: boolean; uploadAllowed?: boolean } = {},
): TestHarness {
  const clock = { now: 1_700_000_000_000 };
  const bucket = new MemoryBucket();
  const storage = new MemoryStorage();
  const registry = new ReportRegistryService(storage);
  const logs: Array<{
    event: string;
    fields: Record<string, string | number>;
  }> = [];
  const logger: ServiceLogger = {
    error(event, fields) {
      logs.push({ event, fields });
    },
    info(event, fields) {
      logs.push({ event, fields });
    },
  };
  const service = createDiagnosticService({
    bucket,
    clock: () => clock.now,
    createRateLimiter: new FixedRateLimiter(options.createAllowed),
    logger,
    maintainerToken: "maintainer-token-with-at-least-thirty-two-characters",
    rateLimitSalt: "test-rate-limit-salt",
    registry,
    serviceEnvironment: "test",
    uploadRateLimiter: new FixedRateLimiter(options.uploadAllowed),
  });
  return { bucket, clock, logs, registry, service, storage };
}

async function createReport(
  service: ReturnType<typeof createDiagnosticService>,
  bytes: Uint8Array,
): Promise<CreatedReport> {
  const response = await service.fetch(
    new Request("https://diagnostics.example.test/v1/reports", {
      body: JSON.stringify({
        bundle_schema_version: API_SCHEMA_VERSION,
        content_type: BUNDLE_CONTENT_TYPE,
        sha256: await sha256Hex(bytes),
        size_bytes: bytes.byteLength,
      }),
      headers: { "content-type": "application/json" },
      method: "POST",
    }),
  );
  expect(response.status).toBe(201);
  return (await response.json()) as CreatedReport;
}

function uploadRequest(
  report: CreatedReport,
  bytes: Uint8Array,
  headers: Record<string, string> = {},
): Request {
  const body = new Uint8Array(bytes.byteLength);
  body.set(bytes);
  return new Request(report.upload.url, {
    body: body.buffer,
    headers: { ...report.upload.headers, ...headers },
    method: "PUT",
  });
}

function maintainerRequest(
  method: "DELETE" | "GET",
  report: CreatedReport,
  token?: string,
): Request {
  return new Request(
    `https://diagnostics.example.test/v1/maintainer/reports/${report.support_code}`,
    {
      headers: token === undefined ? {} : { authorization: `Bearer ${token}` },
      method,
    },
  );
}

describe("private diagnostic service", () => {
  it("creates a bounded report, finalizes upload, and rejects replay", async () => {
    const harness = makeHarness();
    const bytes = new TextEncoder().encode(
      "small diagnostic archive placeholder",
    );
    const report = await createReport(harness.service, bytes);

    expect(report.support_code).toMatch(
      /^BDAVP-[0-9ABCDEFGHJKMNPQRSTVWXYZ]{16}$/u,
    );
    expect(report.upload.headers["Content-Length"]).toBe(
      String(bytes.byteLength),
    );
    expect(JSON.stringify(harness.logs)).not.toContain(
      report.upload.headers.Authorization,
    );

    const uploaded = await harness.service.fetch(uploadRequest(report, bytes));
    expect(uploaded.status).toBe(201);
    expect(await uploaded.json()).toMatchObject({
      report_id: report.report_id,
      status: "uploaded",
    });

    const object = [...harness.bucket.objects.values()][0];
    expect(object?.customMetadata).toMatchObject({
      bundle_schema_version: "1",
      expires_at: new Date(harness.clock.now + RETENTION_MS).toISOString(),
      report_id: report.report_id,
      sha256: await sha256Hex(bytes),
    });

    const status = await harness.service.fetch(
      new Request(report.status.url, {
        headers: report.status.headers,
        method: "GET",
      }),
    );
    expect(status.status).toBe(200);
    expect(await status.json()).toMatchObject({
      report_id: report.report_id,
      status: "uploaded",
    });

    const replay = await harness.service.fetch(uploadRequest(report, bytes));
    expect(replay.status).toBe(409);
    expect(await replay.json()).toEqual({ error: "upload_consumed" });
  });

  it("rejects wrong content, size, and checksum without consuming authorization", async () => {
    const harness = makeHarness();
    const bytes = new TextEncoder().encode("diagnostic bytes");
    const report = await createReport(harness.service, bytes);

    const wrongContentType = await harness.service.fetch(
      uploadRequest(report, bytes, {
        "Content-Type": "application/octet-stream",
      }),
    );
    expect(wrongContentType.status).toBe(415);

    const wrongSize = await harness.service.fetch(
      uploadRequest(report, new TextEncoder().encode("wrong bytes")),
    );
    expect(wrongSize.status).toBe(422);
    expect(await wrongSize.json()).toEqual({
      error: "content_length_mismatch",
    });

    const wrongChecksum = await harness.service.fetch(
      uploadRequest(report, bytes, { "X-Content-SHA256": "a".repeat(64) }),
    );
    expect(wrongChecksum.status).toBe(422);
    expect(await wrongChecksum.json()).toEqual({ error: "checksum_mismatch" });

    const success = await harness.service.fetch(uploadRequest(report, bytes));
    expect(success.status).toBe(201);
  });

  it("expires upload authorization and denies support-code guessing", async () => {
    const harness = makeHarness();
    const bytes = new TextEncoder().encode("expiring diagnostic");
    const report = await createReport(harness.service, bytes);
    harness.clock.now += UPLOAD_AUTH_TTL_MS + 1;

    const expired = await harness.service.fetch(uploadRequest(report, bytes));
    expect(expired.status).toBe(410);
    expect(await expired.json()).toEqual({ error: "upload_expired" });

    const guessed = await harness.service.fetch(
      new Request(
        "https://diagnostics.example.test/v1/maintainer/reports/BDAVP-0123456789ABCDEF",
        {
          headers: {
            authorization:
              "Bearer maintainer-token-with-at-least-thirty-two-characters",
          },
          method: "GET",
        },
      ),
    );
    expect(guessed.status).toBe(404);

    const withoutToken = await harness.service.fetch(
      maintainerRequest("GET", report),
    );
    expect(withoutToken.status).toBe(401);
  });

  it("uses both the configured rate-limit binding and daily client quota", async () => {
    const limitedHarness = makeHarness({ createAllowed: false });
    const bytes = new TextEncoder().encode("rate limit");
    const limited = await limitedHarness.service.fetch(
      new Request("https://diagnostics.example.test/v1/reports", {
        body: JSON.stringify({
          bundle_schema_version: API_SCHEMA_VERSION,
          content_type: BUNDLE_CONTENT_TYPE,
          sha256: await sha256Hex(bytes),
          size_bytes: bytes.byteLength,
        }),
        headers: { "content-type": "application/json" },
        method: "POST",
      }),
    );
    expect(limited.status).toBe(429);

    const harness = makeHarness();
    for (let index = 0; index < MAX_DAILY_REPORTS_PER_CLIENT; index += 1) {
      await createReport(harness.service, bytes);
    }
    const excess = await harness.service.fetch(
      new Request("https://diagnostics.example.test/v1/reports", {
        body: JSON.stringify({
          bundle_schema_version: API_SCHEMA_VERSION,
          content_type: BUNDLE_CONTENT_TYPE,
          sha256: await sha256Hex(bytes),
          size_bytes: bytes.byteLength,
        }),
        headers: { "content-type": "application/json" },
        method: "POST",
      }),
    );
    expect(excess.status).toBe(429);
  });

  it("requires the maintainer token to fetch and delete without public read routes", async () => {
    const harness = makeHarness();
    const bytes = new TextEncoder().encode("maintainer-only diagnostic");
    const report = await createReport(harness.service, bytes);
    expect(
      (await harness.service.fetch(uploadRequest(report, bytes))).status,
    ).toBe(201);

    const publicRead = await harness.service.fetch(
      new Request(
        `https://diagnostics.example.test/v1/reports/${report.report_id}/bundle`,
        { method: "GET" },
      ),
    );
    expect(publicRead.status).toBe(404);

    const wrongToken = await harness.service.fetch(
      maintainerRequest(
        "GET",
        report,
        "wrong-maintainer-token-with-at-least-thirty-two",
      ),
    );
    expect(wrongToken.status).toBe(401);

    const fetched = await harness.service.fetch(
      maintainerRequest(
        "GET",
        report,
        "maintainer-token-with-at-least-thirty-two-characters",
      ),
    );
    expect(fetched.status).toBe(200);
    expect(new Uint8Array(await fetched.arrayBuffer())).toEqual(bytes);
    expect(fetched.headers.get("x-diagnostic-sha256")).toBe(
      await sha256Hex(bytes),
    );

    const deleted = await harness.service.fetch(
      maintainerRequest(
        "DELETE",
        report,
        "maintainer-token-with-at-least-thirty-two-characters",
      ),
    );
    expect(deleted.status).toBe(204);

    const afterDelete = await harness.service.fetch(
      maintainerRequest(
        "GET",
        report,
        "maintainer-token-with-at-least-thirty-two-characters",
      ),
    );
    expect(afterDelete.status).toBe(404);
  });

  it("records storage failure safely and exposes no token or bundle content in logs", async () => {
    const harness = makeHarness();
    const bytes = new TextEncoder().encode(
      "bundle content that must never be logged",
    );
    const report = await createReport(harness.service, bytes);
    harness.bucket.failPuts = true;

    const failed = await harness.service.fetch(uploadRequest(report, bytes));
    expect(failed.status).toBe(503);
    expect(await failed.json()).toEqual({ error: "service_unavailable" });

    const status = await harness.service.fetch(
      new Request(report.status.url, {
        headers: report.status.headers,
        method: "GET",
      }),
    );
    expect(await status.json()).toMatchObject({ status: "failed" });
    const serializedLogs = JSON.stringify(harness.logs);
    expect(serializedLogs).toContain(report.report_id);
    expect(serializedLogs).not.toContain(report.upload.headers.Authorization);
    expect(serializedLogs).not.toContain(
      "bundle content that must never be logged",
    );
  });

  it("prunes expired report metadata and identifies the private object for deletion", async () => {
    const harness = makeHarness();
    const record: CreateReportRecordInput = {
      bundleSchemaVersion: API_SCHEMA_VERSION,
      clientFingerprint: "client",
      contentType: BUNDLE_CONTENT_TYPE,
      createdAt: harness.clock.now,
      expiresAt: harness.clock.now + 1,
      objectKey: "reports/expired.tar.gz",
      reportId: "00000000-0000-4000-8000-000000000000",
      sha256: "b".repeat(64),
      sizeBytes: 1,
      statusExpiresAt: harness.clock.now + 1,
      statusTokenHash: "status",
      supportCode: "BDAVP-0123456789ABCDEF",
      uploadExpiresAt: harness.clock.now + 1,
      uploadState: "uploaded",
      uploadTokenHash: "upload",
    };
    await harness.registry.create(record, harness.clock.now);
    expect(await harness.registry.prune(harness.clock.now + 1)).toEqual([
      record.objectKey,
    ]);
    await expect(
      harness.registry.getForMaintainer(
        record.supportCode,
        harness.clock.now + 1,
      ),
    ).rejects.toMatchObject({
      code: "not_found",
    });
  });
});
