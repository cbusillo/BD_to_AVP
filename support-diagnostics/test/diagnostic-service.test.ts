import { describe, expect, it } from "vitest";
import { deflateRawSync } from "node:zlib";
import { readFileSync } from "node:fs";

import { sha256Hex } from "../src/crypto.js";
import {
  createDiagnosticService,
  DurableObjectRegistryClient,
} from "../src/index.js";
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
  DurableObjectStub,
  R2Bucket,
  R2HttpMetadata,
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

  async delete(keys: string[]): Promise<number>;
  async delete(key: string): Promise<boolean>;
  async delete(keyOrKeys: string | string[]): Promise<boolean | number> {
    if (Array.isArray(keyOrKeys)) {
      let count = 0;
      for (const key of keyOrKeys) {
        if (this.values.delete(key)) {
          count += 1;
        }
      }
      return count;
    }
    return this.values.delete(keyOrKeys);
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

  async put(entries: Record<string, unknown>): Promise<void>;
  async put<Value>(key: string, value: Value): Promise<void>;
  async put<Value>(
    keyOrEntries: string | Record<string, unknown>,
    value?: Value,
  ): Promise<void> {
    if (typeof keyOrEntries === "string") {
      this.values.set(keyOrEntries, structuredClone(value));
      return;
    }
    for (const [key, entry] of Object.entries(keyOrEntries)) {
      this.values.set(key, structuredClone(entry));
    }
  }

  async setAlarm(scheduledTime: number): Promise<void> {
    this.alarm = scheduledTime;
  }
}

interface MemoryObject {
  bytes: Uint8Array;
  customMetadata?: Record<string, string>;
  httpMetadata?: R2HttpMetadata;
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
    if (options?.httpMetadata !== undefined) {
      object.httpMetadata = options.httpMetadata;
    }
    this.objects.set(key, object);
  }
}

function crc32(bytes: Uint8Array): number {
  let value = 0xffffffff;
  for (const byte of bytes) {
    value ^= byte;
    for (let bit = 0; bit < 8; bit += 1) {
      value = (value & 1) === 1 ? 0xedb88320 ^ (value >>> 1) : value >>> 1;
    }
  }
  return (value ^ 0xffffffff) >>> 0;
}

function makeDiagnosticBundle(
  options: {
    manifestSchemaVersion?: unknown;
    toolTail?: string;
    utf8Flag?: boolean;
  } = {},
): Uint8Array {
  const schemaVersion = options.manifestSchemaVersion ?? API_SCHEMA_VERSION;
  const entries = [
    {
      data: Buffer.from(JSON.stringify({ schema_version: schemaVersion })),
      name: "manifest.json",
    },
    {
      data: Buffer.from(
        `${JSON.stringify({ schema_version: API_SCHEMA_VERSION, source: "client" })}\n`,
      ),
      name: "events.jsonl",
    },
    {
      data: Buffer.from(
        JSON.stringify({ schema_version: API_SCHEMA_VERSION, probes: [] }),
      ),
      name: "storage.json",
    },
    {
      data: Buffer.from(
        `# bd_to_avp_support_tool_tail schema_version=${API_SCHEMA_VERSION}\n${options.toolTail ?? ""}`,
      ),
      name: "tool-tail.txt",
    },
  ];
  const localParts: Buffer[] = [];
  const centralParts: Buffer[] = [];
  const flags = options.utf8Flag === false ? 0 : 0x0800;
  let localOffset = 0;
  for (const entry of entries) {
    const name = Buffer.from(entry.name);
    const compressed = deflateRawSync(entry.data);
    const checksum = crc32(entry.data);
    const localHeader = Buffer.alloc(30);
    localHeader.writeUInt32LE(0x04034b50, 0);
    localHeader.writeUInt16LE(20, 4);
    localHeader.writeUInt16LE(flags, 6);
    localHeader.writeUInt16LE(8, 8);
    localHeader.writeUInt32LE(checksum, 14);
    localHeader.writeUInt32LE(compressed.byteLength, 18);
    localHeader.writeUInt32LE(entry.data.byteLength, 22);
    localHeader.writeUInt16LE(name.byteLength, 26);
    localParts.push(localHeader, name, compressed);

    const centralHeader = Buffer.alloc(46);
    centralHeader.writeUInt32LE(0x02014b50, 0);
    centralHeader.writeUInt16LE(0x0314, 4);
    centralHeader.writeUInt16LE(20, 6);
    centralHeader.writeUInt16LE(flags, 8);
    centralHeader.writeUInt16LE(8, 10);
    centralHeader.writeUInt32LE(checksum, 16);
    centralHeader.writeUInt32LE(compressed.byteLength, 20);
    centralHeader.writeUInt32LE(entry.data.byteLength, 24);
    centralHeader.writeUInt16LE(name.byteLength, 28);
    centralHeader.writeUInt32LE(localOffset, 42);
    centralParts.push(centralHeader, name);
    localOffset += localHeader.byteLength + name.byteLength + compressed.byteLength;
  }
  const centralDirectory = Buffer.concat(centralParts);
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0);
  end.writeUInt16LE(entries.length, 8);
  end.writeUInt16LE(entries.length, 10);
  end.writeUInt32LE(centralDirectory.byteLength, 12);
  end.writeUInt32LE(localOffset, 16);
  return new Uint8Array(
    Buffer.concat([...localParts, centralDirectory, end]),
  );
}

function nativeSwiftDiagnosticBundle(): Uint8Array {
  const encoded = readFileSync(
    new URL(
      "../../tests/fixtures/support_diagnostics_native_v1.b64",
      import.meta.url,
    ),
    "utf8",
  ).trim();
  return new Uint8Array(Buffer.from(encoded, "base64"));
}

function withFirstEntryUncompressedSize(
  bytes: Uint8Array,
  size: number,
): Uint8Array {
  const mutated = Buffer.from(bytes);
  const centralDirectoryOffset = mutated.indexOf(
    Buffer.from([0x50, 0x4b, 0x01, 0x02]),
  );
  if (centralDirectoryOffset < 0) {
    throw new Error("fixture is missing its central directory");
  }
  mutated.writeUInt32LE(size, 22);
  mutated.writeUInt32LE(size, centralDirectoryOffset + 24);
  return new Uint8Array(mutated);
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
    const bytes = makeDiagnosticBundle();
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
    expect(object?.httpMetadata?.cacheExpiry).toEqual(
      new Date(harness.clock.now + RETENTION_MS),
    );

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

  it("rejects wrong headers without consuming authorization", async () => {
    const harness = makeHarness();
    const bytes = makeDiagnosticBundle();
    const report = await createReport(harness.service, bytes);

    const wrongContentType = await harness.service.fetch(
      uploadRequest(report, bytes, {
        "Content-Type": "application/octet-stream",
      }),
    );
    expect(wrongContentType.status).toBe(415);

    const wrongSize = await harness.service.fetch(
      uploadRequest(report, bytes, {
        "Content-Length": String(bytes.byteLength - 1),
      }),
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

  it("rejects malformed archives and consumes the reserved upload", async () => {
    const harness = makeHarness();
    const bytes = new TextEncoder().encode("not a diagnostic ZIP");
    const report = await createReport(harness.service, bytes);

    const invalid = await harness.service.fetch(uploadRequest(report, bytes));
    expect(invalid.status).toBe(422);
    expect(await invalid.json()).toEqual({ error: "invalid_bundle" });

    const status = await harness.service.fetch(
      new Request(report.status.url, {
        headers: report.status.headers,
        method: "GET",
      }),
    );
    expect(await status.json()).toMatchObject({ status: "failed" });

    const replay = await harness.service.fetch(uploadRequest(report, bytes));
    expect(replay.status).toBe(409);
  });

  it("reserves upload authorization before reading a streaming body", async () => {
    const harness = makeHarness();
    const bytes = makeDiagnosticBundle();
    const report = await createReport(harness.service, bytes);
    let bodyController: ReadableStreamDefaultController<Uint8Array> | undefined;
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        bodyController = controller;
      },
    });
    const request = new Request(report.upload.url, {
      body,
      duplex: "half",
      headers: report.upload.headers,
      method: "PUT",
    } as RequestInit & { duplex: "half" });
    const inFlight = harness.service.fetch(request);

    for (let attempt = 0; attempt < 10; attempt += 1) {
      const status = await harness.service.fetch(
        new Request(report.status.url, {
          headers: report.status.headers,
          method: "GET",
        }),
      );
      if ((await status.json() as { status: string }).status === "uploading") {
        break;
      }
      await new Promise((resolve) => setTimeout(resolve, 0));
    }

    const replay = await harness.service.fetch(uploadRequest(report, bytes));
    expect(replay.status).toBe(409);
    bodyController!.enqueue(bytes);
    bodyController!.close();
    expect((await inFlight).status).toBe(201);
  });

  it("defers semantic schema validation to the client and maintainer CLI", async () => {
    const harness = makeHarness();
    const bytes = makeDiagnosticBundle({ manifestSchemaVersion: true });
    const report = await createReport(harness.service, bytes);

    const uploaded = await harness.service.fetch(uploadRequest(report, bytes));
    expect(uploaded.status).toBe(201);
  });

  it("rejects ZIP envelopes with declared expansion beyond entry limits", async () => {
    const harness = makeHarness();
    const bytes = withFirstEntryUncompressedSize(
      makeDiagnosticBundle(),
      64 * 1024 + 1,
    );
    const report = await createReport(harness.service, bytes);

    const invalid = await harness.service.fetch(uploadRequest(report, bytes));
    expect(invalid.status).toBe(422);
    expect(await invalid.json()).toEqual({ error: "invalid_bundle" });
  });

  it("accepts standard ASCII ZIP entries without the UTF-8 filename flag", async () => {
    const harness = makeHarness();
    const bytes = makeDiagnosticBundle({ utf8Flag: false });
    const report = await createReport(harness.service, bytes);

    const uploaded = await harness.service.fetch(uploadRequest(report, bytes));
    expect(uploaded.status).toBe(201);
  });

  it("accepts the native Swift diagnostic archive fixture", async () => {
    const harness = makeHarness();
    const bytes = nativeSwiftDiagnosticBundle();
    const report = await createReport(harness.service, bytes);

    const uploaded = await harness.service.fetch(uploadRequest(report, bytes));
    expect(uploaded.status).toBe(201);
  });

  it("expires upload authorization and denies support-code guessing", async () => {
    const harness = makeHarness();
    const bytes = makeDiagnosticBundle();
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
    const bytes = makeDiagnosticBundle();
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
    const bytes = makeDiagnosticBundle({
      toolTail: "bundle content that must never be logged",
    });
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
      objectKey: "reports/expired.zip",
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

  it("rejects finalization after the upload authorization expires", async () => {
    const harness = makeHarness();
    const record: CreateReportRecordInput = {
      bundleSchemaVersion: API_SCHEMA_VERSION,
      clientFingerprint: "client",
      contentType: BUNDLE_CONTENT_TYPE,
      createdAt: harness.clock.now,
      expiresAt: harness.clock.now + RETENTION_MS,
      objectKey: "reports/late.zip",
      reportId: "00000000-0000-4000-8000-000000000001",
      sha256: "c".repeat(64),
      sizeBytes: 1,
      statusExpiresAt: harness.clock.now + RETENTION_MS,
      statusTokenHash: "status",
      supportCode: "BDAVP-1123456789ABCDEF",
      uploadExpiresAt: harness.clock.now + UPLOAD_AUTH_TTL_MS,
      uploadState: "pending",
      uploadTokenHash: "upload",
    };
    await harness.registry.create(record, harness.clock.now);
    await harness.registry.beginUpload(
      record.reportId,
      record.uploadTokenHash!,
      harness.clock.now,
    );

    await expect(
      harness.registry.completeUpload(
        record.reportId,
        record.uploadExpiresAt,
      ),
    ).rejects.toMatchObject({ code: "upload_expired" });
    await expect(
      harness.registry.getStatus(
        record.reportId,
        record.statusTokenHash,
        record.uploadExpiresAt,
      ),
    ).resolves.toMatchObject({ uploadState: "failed" });
  });

  it("maps registry transport and serialization failures to service unavailable", async () => {
    const stub: DurableObjectStub = {
      async fetch() {
        return new Response("not JSON", { status: 500 });
      },
    };
    const harness = makeHarness();
    const service = createDiagnosticService({
      bucket: harness.bucket,
      clock: () => harness.clock.now,
      createRateLimiter: new FixedRateLimiter(),
      maintainerToken: "maintainer-token-with-at-least-thirty-two-characters",
      rateLimitSalt: "test-rate-limit-salt",
      registry: new DurableObjectRegistryClient(stub),
      serviceEnvironment: "test",
      uploadRateLimiter: new FixedRateLimiter(),
    });

    const response = await service.fetch(
      new Request(
        "https://diagnostics.example.test/v1/reports/00000000-0000-4000-8000-000000000000/status",
        {
          headers: { authorization: "Bearer status-token" },
          method: "GET",
        },
      ),
    );
    expect(response.status).toBe(503);
    expect(await response.json()).toEqual({ error: "service_unavailable" });
  });
});
