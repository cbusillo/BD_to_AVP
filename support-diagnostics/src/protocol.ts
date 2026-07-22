export const API_SCHEMA_VERSION = 1;
export const BUNDLE_CONTENT_TYPE = "application/zip";
export const BUNDLE_FILENAME_SUFFIX = ".zip";
export const MAX_ACTIVE_REPORTS = 500;
export const MAX_BUNDLE_BYTES = 2 * 1024 * 1024;
export const MAX_CREATE_REQUEST_BYTES = 4 * 1024;
export const MAX_DAILY_REPORTS_PER_CLIENT = 5;
export const MINIMUM_PRIVACY_RULES_VERSION = 4;
export const RETENTION_MS = 30 * 24 * 60 * 60 * 1000;
export const STATUS_AUTH_TTL_MS = 10 * 60 * 1000;
export const UPLOAD_AUTH_TTL_MS = 10 * 60 * 1000;

export type UploadState = "failed" | "pending" | "uploaded" | "uploading";

export interface ReportRecord {
  bundleSchemaVersion: number;
  clientFingerprint: string;
  contentType: string;
  createdAt: number;
  expiresAt: number;
  objectKey: string;
  privacyRulesVersion?: number;
  reportId: string;
  sha256: string;
  sizeBytes: number;
  statusExpiresAt: number;
  statusTokenHash: string;
  supportCode: string;
  uploadExpiresAt: number;
  uploadState: UploadState;
  uploadTokenHash?: string;
}

export interface ClientUsage {
  count: number;
  windowStartedAt: number;
}

export interface CreateReportRecordInput extends ReportRecord {}

export type RegistryErrorCode =
  | "client_daily_limit"
  | "not_found"
  | "support_code_collision"
  | "total_capacity_reached"
  | "upload_consumed"
  | "upload_expired";

export class RegistryError extends Error {
  constructor(
    readonly code: RegistryErrorCode,
    readonly expiredObjectKey?: string,
  ) {
    super(code);
  }
}

export interface ReportStatus {
  expiresAt: number;
  reportId: string;
  uploadState: UploadState;
}

export interface MaintainerReportSummary {
  bundleSchemaVersion: number;
  createdAt: number;
  expiresAt: number;
  privacyRulesVersion: number | undefined;
  sizeBytes: number;
  supportCode: string;
  uploadState: UploadState;
}
