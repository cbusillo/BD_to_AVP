export interface R2HttpMetadata {
  cacheControl?: string;
  cacheExpiry?: Date;
  contentDisposition?: string;
  contentType?: string;
}

export interface R2PutOptions {
  customMetadata?: Record<string, string>;
  httpMetadata?: R2HttpMetadata;
  sha256?: ArrayBuffer | string;
}

export interface R2ObjectBody {
  body: ReadableStream<Uint8Array>;
  customMetadata?: Record<string, string>;
  key: string;
  size: number;
}

export interface R2Bucket {
  delete(key: string): Promise<void>;
  get(key: string): Promise<R2ObjectBody | null>;
  put(
    key: string,
    value: ArrayBuffer,
    options?: R2PutOptions,
  ): Promise<unknown>;
}

export interface RateLimitBinding {
  limit(options: { key: string }): Promise<{ success: boolean }>;
}

export interface DurableObjectId {
  toString(): string;
}

export interface DurableObjectStub {
  fetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response>;
}

export interface DurableObjectNamespace {
  get(id: DurableObjectId): DurableObjectStub;
  idFromName(name: string): DurableObjectId;
}

export interface DurableObjectStorage {
  delete(keys: string[]): Promise<number>;
  delete(key: string): Promise<boolean>;
  get<T>(key: string): Promise<T | undefined>;
  list<T>(options?: { prefix?: string }): Promise<Map<string, T>>;
  put(entries: Record<string, unknown>): Promise<void>;
  put<T>(key: string, value: T): Promise<void>;
  setAlarm(scheduledTime: number): Promise<void>;
}

export interface DurableObjectState {
  storage: DurableObjectStorage;
}

export interface ExecutionContext {
  waitUntil(promise: Promise<unknown>): void;
}

export interface ScheduledController {
  scheduledTime: number;
}

export interface Env {
  CREATE_RATE_LIMITER: RateLimitBinding;
  DIAGNOSTIC_BUNDLES: R2Bucket;
  MAINTAINER_TOKEN: string;
  RATE_LIMIT_SALT: string;
  REPORT_REGISTRY: DurableObjectNamespace;
  SERVICE_ENVIRONMENT: string;
  UPLOAD_RATE_LIMITER: RateLimitBinding;
}

export interface ExportedHandler<Environment> {
  fetch(
    request: Request,
    env: Environment,
    context: ExecutionContext,
  ): Promise<Response>;
  scheduled?(
    controller: ScheduledController,
    env: Environment,
    context: ExecutionContext,
  ): Promise<void>;
}

export interface ServiceLogger {
  error(event: string, fields: Record<string, string | number>): void;
  info(event: string, fields: Record<string, string | number>): void;
}
