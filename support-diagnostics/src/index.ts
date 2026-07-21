import {
  API_SCHEMA_VERSION,
  BUNDLE_CONTENT_TYPE,
  BUNDLE_FILENAME_SUFFIX,
  MAX_BUNDLE_BYTES,
  MAX_CREATE_REQUEST_BYTES,
  MINIMUM_PRIVACY_RULES_VERSION,
  RETENTION_MS,
  STATUS_AUTH_TTL_MS,
  UPLOAD_AUTH_TTL_MS,
  type CreateReportRecordInput,
  type RegistryErrorCode,
  RegistryError,
  type ReportRecord,
} from "./protocol.js";
import {
  InvalidDiagnosticBundleError,
  validateDiagnosticBundleEnvelope,
} from "./bundle.js";
import {
  constantTimeTextEqual,
  generateSupportCode,
  generateToken,
  hexToArrayBuffer,
  isSha256,
  isSupportCode,
  sha256Hex,
} from "./crypto.js";
import {
  registryErrorCode,
  type Registry,
  ReportRegistryService,
} from "./registry.js";
import type {
  DurableObjectState,
  DurableObjectStub,
  Env,
  ExecutionContext,
  ExportedHandler,
  R2Bucket,
  RateLimitBinding,
  ServiceLogger,
} from "./types.js";

const INTERNAL_ORIGIN = "https://report-registry.internal";
const REGISTRY_INSTANCE_NAME = "support-diagnostics-v1";
const REPORT_ID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/u;

declare const FixedLengthStream: {
  new (expectedLength: number | bigint): {
    readable: ReadableStream<Uint8Array>;
    writable: WritableStream<Uint8Array>;
  };
};

class PublicError extends Error {
  constructor(
    readonly code: string,
    readonly status: number,
  ) {
    super(code);
  }
}

interface CreateReportRequest {
  bundle_schema_version: number;
  content_type: string;
  privacy_rules_version: number;
  sha256: string;
  size_bytes: number;
}

interface ServiceDependencies {
  bucket: R2Bucket;
  clock?: () => number;
  createRateLimiter: RateLimitBinding;
  logger?: ServiceLogger;
  maintainerToken: string;
  rateLimitSalt: string;
  registry: Registry;
  serviceEnvironment: string;
  uploadRateLimiter: RateLimitBinding;
}

interface RegistryOperationPayload {
  input?: CreateReportRecordInput;
  now: number;
  reportId?: string;
  statusTokenHash?: string;
  supportCode?: string;
  uploadTokenHash?: string;
}

interface RegistryOperationResponse {
  error?: string;
  result?: unknown;
}

function serviceLogger(): ServiceLogger {
  return {
    error(event, fields) {
      console.error(JSON.stringify({ event, ...fields }));
    },
    info(event, fields) {
      console.log(JSON.stringify({ event, ...fields }));
    },
  };
}

function secureHeaders(): Headers {
  return new Headers({
    "cache-control": "no-store",
    "content-security-policy":
      "default-src 'none'; frame-ancestors 'none'; base-uri 'none'",
    "referrer-policy": "no-referrer",
    "x-content-type-options": "nosniff",
  });
}

function jsonResponse(
  payload: Record<string, unknown>,
  status = 200,
): Response {
  const headers = secureHeaders();
  headers.set("content-type", "application/json; charset=utf-8");
  return new Response(JSON.stringify(payload), { headers, status });
}

function emptyResponse(status: number): Response {
  return new Response(null, { headers: secureHeaders(), status });
}

function errorResponse(error: PublicError): Response {
  return jsonResponse({ error: error.code }, error.status);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function nowFrom(dependencies: ServiceDependencies): number {
  return dependencies.clock?.() ?? Date.now();
}

function toIso(timestamp: number): string {
  return new Date(timestamp).toISOString();
}

function requestContentType(request: Request): string {
  return (
    request.headers
      .get("content-type")
      ?.split(";", 1)[0]
      ?.trim()
      .toLowerCase() ?? ""
  );
}

function requireContentType(request: Request, expected: string): void {
  if (requestContentType(request) !== expected) {
    throw new PublicError("unsupported_content_type", 415);
  }
}

function parseContentLength(request: Request): number | undefined {
  const rawLength = request.headers.get("content-length");
  if (rawLength === null) {
    return undefined;
  }
  if (!/^[0-9]+$/u.test(rawLength)) {
    throw new PublicError("invalid_content_length", 400);
  }
  const length = Number(rawLength);
  if (!Number.isSafeInteger(length)) {
    throw new PublicError("invalid_content_length", 400);
  }
  return length;
}

async function readLimitedBody(
  request: Request,
  maximumBytes: number,
): Promise<Uint8Array> {
  const declaredLength = parseContentLength(request);
  if (declaredLength !== undefined && declaredLength > maximumBytes) {
    throw new PublicError("payload_too_large", 413);
  }
  if (request.body === null) {
    return new Uint8Array();
  }

  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let totalBytes = 0;
  try {
    while (true) {
      const chunk = await reader.read();
      if (chunk.done) {
        break;
      }
      totalBytes += chunk.value.byteLength;
      if (totalBytes > maximumBytes) {
        await reader.cancel();
        throw new PublicError("payload_too_large", 413);
      }
      chunks.push(chunk.value);
    }
  } finally {
    reader.releaseLock();
  }

  const body = new Uint8Array(totalBytes);
  let offset = 0;
  for (const chunk of chunks) {
    body.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return body;
}

async function readCreateRequest(
  request: Request,
): Promise<CreateReportRequest> {
  requireContentType(request, "application/json");
  const bytes = await readLimitedBody(request, MAX_CREATE_REQUEST_BYTES);
  let payload: unknown;
  try {
    payload = JSON.parse(new TextDecoder().decode(bytes));
  } catch {
    throw new PublicError("invalid_json", 400);
  }
  if (!isRecord(payload)) {
    throw new PublicError("invalid_request", 400);
  }

  const keys = Object.keys(payload).sort();
  const expectedKeys = [
    "bundle_schema_version",
    "content_type",
    "privacy_rules_version",
    "sha256",
    "size_bytes",
  ];
  const legacyKeys = expectedKeys.filter(
    (key) => key !== "privacy_rules_version",
  );
  if (
    keys.length === legacyKeys.length &&
    keys.every((key, index) => key === legacyKeys[index])
  ) {
    throw new PublicError("unsupported_privacy_rules_version", 422);
  }
  if (
    keys.length !== expectedKeys.length ||
    keys.some((key, index) => key !== expectedKeys[index])
  ) {
    throw new PublicError("invalid_request", 400);
  }

  const sizeBytes = payload.size_bytes;
  const bundleSchemaVersion = payload.bundle_schema_version;
  const contentType = payload.content_type;
  const privacyRulesVersion = payload.privacy_rules_version;
  const sha256 = payload.sha256;
  if (
    typeof privacyRulesVersion !== "number" ||
    !Number.isSafeInteger(privacyRulesVersion) ||
    privacyRulesVersion < MINIMUM_PRIVACY_RULES_VERSION
  ) {
    throw new PublicError("unsupported_privacy_rules_version", 422);
  }
  if (
    typeof sizeBytes !== "number" ||
    typeof bundleSchemaVersion !== "number" ||
    typeof contentType !== "string" ||
    !Number.isSafeInteger(sizeBytes) ||
    sizeBytes <= 0 ||
    sizeBytes > MAX_BUNDLE_BYTES ||
    bundleSchemaVersion !== API_SCHEMA_VERSION ||
    contentType !== BUNDLE_CONTENT_TYPE ||
    !isSha256(sha256)
  ) {
    throw new PublicError("invalid_request", 400);
  }

  return {
    bundle_schema_version: bundleSchemaVersion,
    content_type: contentType,
    privacy_rules_version: privacyRulesVersion,
    sha256,
    size_bytes: sizeBytes,
  };
}

async function readExactBody(
  request: Request,
  expectedBytes: number,
): Promise<Uint8Array> {
  if (expectedBytes <= 0 || expectedBytes > MAX_BUNDLE_BYTES) {
    throw new PublicError("payload_too_large", 413);
  }
  if (request.body === null) {
    throw new PublicError("content_length_mismatch", 422);
  }

  const stream = new FixedLengthStream(expectedBytes);
  try {
    const bodyPromise = new Response(stream.readable).arrayBuffer();
    const pipePromise = request.body.pipeTo(stream.writable);
    const [body] = await Promise.all([bodyPromise, pipePromise]);
    return new Uint8Array(body);
  } catch {
    throw new PublicError("content_length_mismatch", 422);
  }
}

function arrayBufferFromBytes(bytes: Uint8Array): ArrayBuffer {
  if (
    bytes.byteOffset === 0 &&
    bytes.byteLength === bytes.buffer.byteLength &&
    bytes.buffer instanceof ArrayBuffer
  ) {
    return bytes.buffer;
  }
  const copy = new Uint8Array(bytes.byteLength);
  copy.set(bytes);
  return copy.buffer;
}

function bearerToken(request: Request): string | undefined {
  const header = request.headers.get("authorization");
  if (header === null) {
    return undefined;
  }
  const match = /^Bearer ([^\s]{1,512})$/u.exec(header);
  return match?.[1];
}

function requireReportId(value: string | undefined): string {
  if (value === undefined || !REPORT_ID_PATTERN.test(value)) {
    throw new PublicError("not_found", 404);
  }
  return value;
}

function requireSupportCode(value: string | undefined): string {
  if (value === undefined || !isSupportCode(value)) {
    throw new PublicError("not_found", 404);
  }
  return value;
}

function requireSecureTransport(
  request: Request,
  serviceEnvironment: string,
): void {
  const url = new URL(request.url);
  if (url.protocol === "https:") {
    return;
  }
  const localDevelopmentHost =
    url.hostname === "127.0.0.1" || url.hostname === "localhost";
  if (serviceEnvironment !== "production" && localDevelopmentHost) {
    return;
  }
  throw new PublicError("https_required", 400);
}

async function clientFingerprint(
  request: Request,
  rateLimitSalt: string,
): Promise<string> {
  const clientAddress =
    request.headers.get("cf-connecting-ip") ?? "unavailable";
  return sha256Hex(`${rateLimitSalt}:${clientAddress}`);
}

async function applyRateLimit(
  binding: RateLimitBinding,
  key: string,
): Promise<void> {
  try {
    if (!(await binding.limit({ key })).success) {
      throw new PublicError("rate_limited", 429);
    }
  } catch (error) {
    if (error instanceof PublicError) {
      throw error;
    }
    throw new PublicError("service_unavailable", 503);
  }
}

function registryPublicError(error: RegistryError): PublicError {
  switch (error.code) {
    case "client_daily_limit":
      return new PublicError("rate_limited", 429);
    case "not_found":
      return new PublicError("not_found", 404);
    case "total_capacity_reached":
      return new PublicError("service_unavailable", 503);
    case "upload_consumed":
      return new PublicError("upload_consumed", 409);
    case "upload_expired":
      return new PublicError("upload_expired", 410);
    case "support_code_collision":
      return new PublicError("service_unavailable", 503);
  }
}

function registryErrorResponse(error: unknown): PublicError | undefined {
  return error instanceof RegistryError
    ? registryPublicError(error)
    : undefined;
}

function bundleHeaders(record: ReportRecord): Headers {
  const headers = secureHeaders();
  headers.set(
    "content-disposition",
    `attachment; filename="${record.supportCode}${BUNDLE_FILENAME_SUFFIX}"`,
  );
  headers.set("content-length", String(record.sizeBytes));
  headers.set("content-type", record.contentType);
  headers.set(
    "x-diagnostic-schema-version",
    String(record.bundleSchemaVersion),
  );
  if (record.privacyRulesVersion !== undefined) {
    headers.set(
      "x-diagnostic-privacy-rules-version",
      String(record.privacyRulesVersion),
    );
  }
  headers.set("x-diagnostic-sha256", record.sha256);
  return headers;
}

async function requireMaintainer(
  request: Request,
  dependencies: ServiceDependencies,
): Promise<void> {
  const token = bearerToken(request);
  if (
    token === undefined ||
    dependencies.maintainerToken.length === 0 ||
    !(await constantTimeTextEqual(token, dependencies.maintainerToken))
  ) {
    throw new PublicError("unauthorized", 401);
  }
}

async function createReport(
  request: Request,
  dependencies: ServiceDependencies,
): Promise<Response> {
  const payload = await readCreateRequest(request);
  const now = nowFrom(dependencies);
  const fingerprint = await clientFingerprint(
    request,
    dependencies.rateLimitSalt,
  );
  await applyRateLimit(dependencies.createRateLimiter, `create:${fingerprint}`);

  for (let attempt = 0; attempt < 3; attempt += 1) {
    const reportId = crypto.randomUUID();
    const supportCode = generateSupportCode();
    const uploadToken = generateToken();
    const statusToken = generateToken();
    const [uploadTokenHash, statusTokenHash] = await Promise.all([
      sha256Hex(uploadToken),
      sha256Hex(statusToken),
    ]);
    const uploadExpiresAt = now + UPLOAD_AUTH_TTL_MS;
    const expiresAt = now + RETENTION_MS;
    const input: CreateReportRecordInput = {
      bundleSchemaVersion: payload.bundle_schema_version,
      clientFingerprint: fingerprint,
      contentType: payload.content_type,
      createdAt: now,
      expiresAt,
      objectKey: `reports/${reportId}${BUNDLE_FILENAME_SUFFIX}`,
      privacyRulesVersion: payload.privacy_rules_version,
      reportId,
      sha256: payload.sha256,
      sizeBytes: payload.size_bytes,
      statusExpiresAt: now + STATUS_AUTH_TTL_MS,
      statusTokenHash,
      supportCode,
      uploadExpiresAt,
      uploadState: "pending",
      uploadTokenHash,
    };

    try {
      await dependencies.registry.create(input, now);
    } catch (error) {
      if (
        error instanceof RegistryError &&
        error.code === "support_code_collision"
      ) {
        continue;
      }
      throw error;
    }

    const url = new URL(request.url);
    dependencies.logger?.info("report_created", {
      outcome: "accepted",
      report_id: reportId,
    });
    return jsonResponse(
      {
        expires_at: toIso(expiresAt),
        report_id: reportId,
        schema_version: API_SCHEMA_VERSION,
        status: {
          expires_at: toIso(now + STATUS_AUTH_TTL_MS),
          headers: {
            Authorization: `Bearer ${statusToken}`,
          },
          method: "GET",
          url: `${url.origin}/v1/reports/${reportId}/status`,
        },
        support_code: supportCode,
        upload: {
          expires_at: toIso(uploadExpiresAt),
          headers: {
            Authorization: `Bearer ${uploadToken}`,
            "Content-Length": String(payload.size_bytes),
            "Content-Type": payload.content_type,
            "X-Content-SHA256": payload.sha256,
          },
          method: "PUT",
          url: `${url.origin}/v1/reports/${reportId}/upload`,
        },
      },
      201,
    );
  }

  throw new PublicError("service_unavailable", 503);
}

async function uploadBundle(
  request: Request,
  reportId: string,
  dependencies: ServiceDependencies,
): Promise<Response> {
  const uploadToken = bearerToken(request);
  if (uploadToken === undefined) {
    throw new PublicError("not_found", 404);
  }
  requireContentType(request, BUNDLE_CONTENT_TYPE);
  const suppliedSha256 = request.headers.get("x-content-sha256");
  if (!isSha256(suppliedSha256)) {
    throw new PublicError("invalid_request", 400);
  }

  const uploadTokenHash = await sha256Hex(uploadToken);
  const authorized = await dependencies.registry.authorizeUpload(
    reportId,
    uploadTokenHash,
    nowFrom(dependencies),
  );
  if (
    typeof authorized.privacyRulesVersion !== "number" ||
    authorized.privacyRulesVersion < MINIMUM_PRIVACY_RULES_VERSION
  ) {
    dependencies.logger?.info("report_upload_rejected", {
      outcome: "unsupported_privacy_rules_version",
      report_id: authorized.reportId,
    });
    throw new PublicError("unsupported_privacy_rules_version", 422);
  }
  const declaredLength = parseContentLength(request);
  if (declaredLength === undefined || declaredLength !== authorized.sizeBytes) {
    throw new PublicError("content_length_mismatch", 422);
  }
  if (suppliedSha256 !== authorized.sha256) {
    throw new PublicError("checksum_mismatch", 422);
  }

  await applyRateLimit(
    dependencies.uploadRateLimiter,
    `upload:${authorized.clientFingerprint}`,
  );
  const record = await dependencies.registry.beginUpload(
    reportId,
    uploadTokenHash,
    nowFrom(dependencies),
  );
  let bytes: Uint8Array;
  try {
    bytes = await readExactBody(request, authorized.sizeBytes);
    if ((await sha256Hex(bytes)) !== authorized.sha256) {
      throw new PublicError("checksum_mismatch", 422);
    }
    validateDiagnosticBundleEnvelope(bytes);
    if (record.uploadExpiresAt <= nowFrom(dependencies)) {
      throw new PublicError("upload_expired", 410);
    }
  } catch (error) {
    await dependencies.registry
      .failUpload(reportId, nowFrom(dependencies))
      .catch(() => undefined);
    dependencies.logger?.error("report_upload_failed", {
      outcome:
        error instanceof InvalidDiagnosticBundleError
          ? "invalid_bundle"
          : "invalid_upload",
      report_id: record.reportId,
    });
    if (error instanceof InvalidDiagnosticBundleError) {
      throw new PublicError("invalid_bundle", 422);
    }
    throw error;
  }

  try {
    await dependencies.bucket.put(
      record.objectKey,
      arrayBufferFromBytes(bytes),
      {
        customMetadata: {
          bundle_schema_version: String(record.bundleSchemaVersion),
          created_at: toIso(record.createdAt),
          expires_at: toIso(record.expiresAt),
          privacy_rules_version: String(authorized.privacyRulesVersion),
          report_id: record.reportId,
          sha256: record.sha256,
          size_bytes: String(record.sizeBytes),
        },
        httpMetadata: {
          cacheControl: "no-store",
          contentDisposition: `attachment; filename="${record.supportCode}${BUNDLE_FILENAME_SUFFIX}"`,
          contentType: record.contentType,
          cacheExpiry: new Date(record.expiresAt),
        },
        sha256: hexToArrayBuffer(record.sha256),
      },
    );
  } catch {
    await dependencies.registry
      .failUpload(reportId, nowFrom(dependencies))
      .catch(() => undefined);
    dependencies.logger?.error("report_upload_failed", {
      outcome: "storage_unavailable",
      report_id: record.reportId,
    });
    throw new PublicError("service_unavailable", 503);
  }

  try {
    const completed = await dependencies.registry.completeUpload(
      reportId,
      nowFrom(dependencies),
    );
    dependencies.logger?.info("report_uploaded", {
      outcome: "stored",
      report_id: completed.reportId,
    });
    return jsonResponse(
      {
        expires_at: toIso(completed.expiresAt),
        report_id: completed.reportId,
        status: "uploaded",
      },
      201,
    );
  } catch (error) {
    await dependencies.bucket.delete(record.objectKey).catch(() => undefined);
    await dependencies.registry
      .failUpload(reportId, nowFrom(dependencies))
      .catch(() => undefined);
    dependencies.logger?.error("report_upload_failed", {
      outcome: "finalization_unavailable",
      report_id: record.reportId,
    });
    throw error;
  }
}

async function getStatus(
  request: Request,
  reportId: string,
  dependencies: ServiceDependencies,
): Promise<Response> {
  const statusToken = bearerToken(request);
  if (statusToken === undefined) {
    throw new PublicError("not_found", 404);
  }
  const statusTokenHash = await sha256Hex(statusToken);
  const result = await dependencies.registry.getStatus(
    reportId,
    statusTokenHash,
    nowFrom(dependencies),
  );
  return jsonResponse({
    expires_at: toIso(result.expiresAt),
    report_id: result.reportId,
    status: result.uploadState,
  });
}

async function fetchForMaintainer(
  request: Request,
  supportCode: string,
  dependencies: ServiceDependencies,
): Promise<Response> {
  await requireMaintainer(request, dependencies);
  const record = await dependencies.registry.getForMaintainer(
    supportCode,
    nowFrom(dependencies),
  );
  if (record.uploadState !== "uploaded") {
    throw new PublicError("not_found", 404);
  }

  const object = await dependencies.bucket.get(record.objectKey);
  if (
    object === null ||
    object.size !== record.sizeBytes ||
    object.customMetadata?.sha256 !== record.sha256 ||
    object.customMetadata.bundle_schema_version !==
      String(record.bundleSchemaVersion) ||
    (record.privacyRulesVersion !== undefined &&
      object.customMetadata.privacy_rules_version !==
        String(record.privacyRulesVersion))
  ) {
    dependencies.logger?.error("maintainer_fetch_failed", {
      outcome: "object_unavailable",
      report_id: record.reportId,
    });
    throw new PublicError("service_unavailable", 503);
  }

  dependencies.logger?.info("maintainer_fetch", {
    outcome: "served",
    report_id: record.reportId,
  });
  return new Response(object.body, {
    headers: bundleHeaders(record),
    status: 200,
  });
}

async function deleteForMaintainer(
  request: Request,
  supportCode: string,
  dependencies: ServiceDependencies,
): Promise<Response> {
  await requireMaintainer(request, dependencies);
  const now = nowFrom(dependencies);
  const record = await dependencies.registry.getForMaintainer(supportCode, now);
  try {
    await dependencies.bucket.delete(record.objectKey);
  } catch {
    dependencies.logger?.error("maintainer_delete_failed", {
      outcome: "storage_unavailable",
      report_id: record.reportId,
    });
    throw new PublicError("service_unavailable", 503);
  }
  await dependencies.registry.deleteForMaintainer(supportCode, now);
  dependencies.logger?.info("maintainer_delete", {
    outcome: "deleted",
    report_id: record.reportId,
  });
  return emptyResponse(204);
}

function route(request: Request): string[] {
  return new URL(request.url).pathname.split("/").filter(Boolean);
}

async function handleRequest(
  request: Request,
  dependencies: ServiceDependencies,
): Promise<Response> {
  try {
    requireSecureTransport(request, dependencies.serviceEnvironment);
    const path = route(request);
    if (
      request.method === "POST" &&
      path.length === 2 &&
      path[0] === "v1" &&
      path[1] === "reports"
    ) {
      return await createReport(request, dependencies);
    }
    if (
      request.method === "PUT" &&
      path.length === 4 &&
      path[0] === "v1" &&
      path[1] === "reports" &&
      path[3] === "upload"
    ) {
      return await uploadBundle(
        request,
        requireReportId(path[2]),
        dependencies,
      );
    }
    if (
      request.method === "GET" &&
      path.length === 4 &&
      path[0] === "v1" &&
      path[1] === "reports" &&
      path[3] === "status"
    ) {
      return await getStatus(request, requireReportId(path[2]), dependencies);
    }
    if (
      request.method === "GET" &&
      path.length === 4 &&
      path[0] === "v1" &&
      path[1] === "maintainer" &&
      path[2] === "reports"
    ) {
      return await fetchForMaintainer(
        request,
        requireSupportCode(path[3]),
        dependencies,
      );
    }
    if (
      request.method === "DELETE" &&
      path.length === 4 &&
      path[0] === "v1" &&
      path[1] === "maintainer" &&
      path[2] === "reports"
    ) {
      return await deleteForMaintainer(
        request,
        requireSupportCode(path[3]),
        dependencies,
      );
    }
    throw new PublicError("not_found", 404);
  } catch (error) {
    if (error instanceof PublicError) {
      return errorResponse(error);
    }
    const registryError = registryErrorResponse(error);
    if (registryError !== undefined) {
      return errorResponse(registryError);
    }
    return errorResponse(new PublicError("service_unavailable", 503));
  }
}

export function createDiagnosticService(dependencies: ServiceDependencies): {
  fetch(request: Request): Promise<Response>;
} {
  const logger = dependencies.logger ?? serviceLogger();
  return {
    fetch(request) {
      return handleRequest(request, { ...dependencies, logger });
    },
  };
}

export class DurableObjectRegistryClient implements Registry {
  constructor(private readonly stub: DurableObjectStub) {}

  async create(input: CreateReportRecordInput, now: number): Promise<void> {
    await this.call("create", { input, now });
  }

  async authorizeUpload(
    reportId: string,
    uploadTokenHash: string,
    now: number,
  ): Promise<ReportRecord> {
    return this.call("authorize-upload", { now, reportId, uploadTokenHash });
  }

  async beginUpload(
    reportId: string,
    uploadTokenHash: string,
    now: number,
  ): Promise<ReportRecord> {
    return this.call("begin-upload", { now, reportId, uploadTokenHash });
  }

  async completeUpload(reportId: string, now: number): Promise<ReportRecord> {
    return this.call("complete-upload", { now, reportId });
  }

  async failUpload(reportId: string, now: number): Promise<void> {
    await this.call("fail-upload", { now, reportId });
  }

  async getStatus(
    reportId: string,
    statusTokenHash: string,
    now: number,
  ): Promise<{
    expiresAt: number;
    reportId: string;
    uploadState: ReportRecord["uploadState"];
  }> {
    return this.call("status", { now, reportId, statusTokenHash });
  }

  async getForMaintainer(
    supportCode: string,
    now: number,
  ): Promise<ReportRecord> {
    return this.call("maintainer-get", { now, supportCode });
  }

  async deleteForMaintainer(
    supportCode: string,
    now: number,
  ): Promise<ReportRecord> {
    return this.call("maintainer-delete", { now, supportCode });
  }

  async prune(now: number): Promise<string[]> {
    return this.call("prune", { now });
  }

  private async call<Result>(
    operation: string,
    payload: RegistryOperationPayload,
  ): Promise<Result> {
    const response = await this.stub.fetch(`${INTERNAL_ORIGIN}/${operation}`, {
      body: JSON.stringify(payload),
      headers: { "content-type": "application/json" },
      method: "POST",
    });
    let body: RegistryOperationResponse;
    try {
      body = (await response.json()) as RegistryOperationResponse;
    } catch {
      throw new Error("registry_unavailable");
    }
    const errorCode = registryErrorCode(body.error);
    if (errorCode !== undefined) {
      throw new RegistryError(errorCode);
    }
    if (
      !response.ok ||
      body.error !== undefined ||
      !Object.hasOwn(body, "result")
    ) {
      throw new Error("registry_unavailable");
    }
    return body.result as Result;
  }
}

function internalPayload(request: Request): Promise<RegistryOperationPayload> {
  return request.json() as Promise<RegistryOperationPayload>;
}

function internalErrorResponse(error: unknown): Response {
  if (error instanceof RegistryError) {
    return jsonResponse({ error: error.code }, 400);
  }
  return jsonResponse({ error: "internal_error" }, 500);
}

export class ReportRegistry {
  private readonly service: ReportRegistryService;

  constructor(
    private readonly state: DurableObjectState,
    private readonly env: Env,
  ) {
    this.service = new ReportRegistryService(state.storage);
  }

  async fetch(request: Request): Promise<Response> {
    if (request.method !== "POST") {
      return jsonResponse({ error: "not_found" }, 404);
    }
    const operation = new URL(request.url).pathname.slice(1);
    try {
      const payload = await internalPayload(request);
      const now = payload.now;
      if (!Number.isSafeInteger(now)) {
        return jsonResponse({ error: "not_found" }, 400);
      }
      switch (operation) {
        case "create":
          await this.service.create(
            payload.input as CreateReportRecordInput,
            now,
          );
          return jsonResponse({ result: null });
        case "authorize-upload":
          return jsonResponse({
            result: await this.service.authorizeUpload(
              payload.reportId!,
              payload.uploadTokenHash!,
              now,
            ),
          });
        case "begin-upload":
          return jsonResponse({
            result: await this.service.beginUpload(
              payload.reportId!,
              payload.uploadTokenHash!,
              now,
            ),
          });
        case "complete-upload":
          return jsonResponse({
            result: await this.service.completeUpload(payload.reportId!, now),
          });
        case "fail-upload":
          await this.service.failUpload(payload.reportId!, now);
          return jsonResponse({ result: null });
        case "status":
          return jsonResponse({
            result: await this.service.getStatus(
              payload.reportId!,
              payload.statusTokenHash!,
              now,
            ),
          });
        case "maintainer-get":
          return jsonResponse({
            result: await this.service.getForMaintainer(
              payload.supportCode!,
              now,
            ),
          });
        case "maintainer-delete":
          return jsonResponse({
            result: await this.service.deleteForMaintainer(
              payload.supportCode!,
              now,
            ),
          });
        case "prune":
          return jsonResponse({ result: await this.service.prune(now) });
        default:
          return jsonResponse({ error: "not_found" }, 404);
      }
    } catch (error) {
      if (
        error instanceof RegistryError &&
        error.expiredObjectKey !== undefined
      ) {
        await this.env.DIAGNOSTIC_BUNDLES.delete(error.expiredObjectKey).catch(
          () => undefined,
        );
      }
      return internalErrorResponse(error);
    }
  }

  async alarm(): Promise<void> {
    const expiredObjectKeys = await this.service.prune(Date.now());
    await Promise.all(
      expiredObjectKeys.map((key) => this.env.DIAGNOSTIC_BUNDLES.delete(key)),
    );
  }
}

function runtimeDependencies(env: Env): ServiceDependencies {
  const namespace = env.REPORT_REGISTRY;
  const registry = new DurableObjectRegistryClient(
    namespace.get(namespace.idFromName(REGISTRY_INSTANCE_NAME)),
  );
  return {
    bucket: env.DIAGNOSTIC_BUNDLES,
    createRateLimiter: env.CREATE_RATE_LIMITER,
    maintainerToken: env.MAINTAINER_TOKEN,
    rateLimitSalt: env.RATE_LIMIT_SALT,
    registry,
    serviceEnvironment: env.SERVICE_ENVIRONMENT,
    uploadRateLimiter: env.UPLOAD_RATE_LIMITER,
  };
}

const worker: ExportedHandler<Env> = {
  async fetch(request, env) {
    return createDiagnosticService(runtimeDependencies(env)).fetch(request);
  },
  async scheduled(controller, env, context) {
    const registry = new DurableObjectRegistryClient(
      env.REPORT_REGISTRY.get(
        env.REPORT_REGISTRY.idFromName(REGISTRY_INSTANCE_NAME),
      ),
    );
    const cleanup = registry
      .prune(controller.scheduledTime)
      .then((keys) =>
        Promise.all(keys.map((key) => env.DIAGNOSTIC_BUNDLES.delete(key))),
      )
      .then(() => undefined);
    context.waitUntil(cleanup);
  },
};

export default worker;
