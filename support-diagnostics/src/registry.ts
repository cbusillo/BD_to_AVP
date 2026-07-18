import {
  MAX_ACTIVE_REPORTS,
  MAX_DAILY_REPORTS_PER_CLIENT,
  type ClientUsage,
  type CreateReportRecordInput,
  type RegistryErrorCode,
  RegistryError,
  type ReportRecord,
  type ReportStatus,
} from "./protocol.js";
import type { DurableObjectStorage } from "./types.js";

const CLIENT_USAGE_PREFIX = "client:";
const REPORT_PREFIX = "report:";
const SUPPORT_CODE_PREFIX = "support:";
const DAY_MS = 24 * 60 * 60 * 1000;

function reportKey(reportId: string): string {
  return `${REPORT_PREFIX}${reportId}`;
}

function supportCodeKey(supportCode: string): string {
  return `${SUPPORT_CODE_PREFIX}${supportCode}`;
}

function clientUsageKey(clientFingerprint: string): string {
  return `${CLIENT_USAGE_PREFIX}${clientFingerprint}`;
}

export interface Registry {
  authorizeUpload(
    reportId: string,
    uploadTokenHash: string,
    now: number,
  ): Promise<ReportRecord>;
  beginUpload(
    reportId: string,
    uploadTokenHash: string,
    now: number,
  ): Promise<ReportRecord>;
  completeUpload(reportId: string, now: number): Promise<ReportRecord>;
  create(input: CreateReportRecordInput, now: number): Promise<void>;
  deleteForMaintainer(supportCode: string, now: number): Promise<ReportRecord>;
  failUpload(reportId: string, now: number): Promise<void>;
  getForMaintainer(supportCode: string, now: number): Promise<ReportRecord>;
  getStatus(
    reportId: string,
    statusTokenHash: string,
    now: number,
  ): Promise<ReportStatus>;
  prune(now: number): Promise<string[]>;
}

export class ReportRegistryService implements Registry {
  constructor(private readonly storage: DurableObjectStorage) {}

  async create(input: CreateReportRecordInput, now: number): Promise<void> {
    const records = await this.storage.list<ReportRecord>({
      prefix: REPORT_PREFIX,
    });
    const activeReports = Array.from(records.values()).filter(
      (record) => record.expiresAt > now,
    ).length;
    if (activeReports >= MAX_ACTIVE_REPORTS) {
      throw new RegistryError("total_capacity_reached");
    }

    const existingReport = await this.storage.get<ReportRecord>(
      reportKey(input.reportId),
    );
    const existingSupportCode = await this.storage.get<string>(
      supportCodeKey(input.supportCode),
    );
    if (existingReport !== undefined || existingSupportCode !== undefined) {
      throw new RegistryError("support_code_collision");
    }

    const [usageKey, usage] = await this.nextClientUsage(
      input.clientFingerprint,
      now,
    );
    await this.storage.put({
      [usageKey]: usage,
      [reportKey(input.reportId)]: input,
      [supportCodeKey(input.supportCode)]: input.reportId,
    });
    await this.scheduleNextExpiry().catch(() => undefined);
  }

  async authorizeUpload(
    reportId: string,
    uploadTokenHash: string,
    now: number,
  ): Promise<ReportRecord> {
    const record = await this.getLiveReport(reportId, now);
    this.assertUploadAuthorization(record, uploadTokenHash, now);
    return record;
  }

  async beginUpload(
    reportId: string,
    uploadTokenHash: string,
    now: number,
  ): Promise<ReportRecord> {
    const record = await this.getLiveReport(reportId, now);
    this.assertUploadAuthorization(record, uploadTokenHash, now);
    record.uploadState = "uploading";
    await this.storage.put(reportKey(record.reportId), record);
    return record;
  }

  async completeUpload(reportId: string, now: number): Promise<ReportRecord> {
    const record = await this.getLiveReport(reportId, now);
    if (record.uploadState !== "uploading") {
      throw new RegistryError("upload_consumed");
    }
    if (record.uploadExpiresAt <= now) {
      record.uploadState = "failed";
      await this.storage.put(reportKey(record.reportId), record);
      throw new RegistryError("upload_expired");
    }
    record.uploadState = "uploaded";
    await this.storage.put(reportKey(record.reportId), record);
    return record;
  }

  async failUpload(reportId: string, now: number): Promise<void> {
    const record = await this.getLiveReport(reportId, now);
    if (record.uploadState !== "uploading") {
      return;
    }
    record.uploadState = "failed";
    await this.storage.put(reportKey(record.reportId), record);
  }

  async getStatus(
    reportId: string,
    statusTokenHash: string,
    now: number,
  ): Promise<ReportStatus> {
    const record = await this.getLiveReport(reportId, now);
    if (
      record.statusExpiresAt <= now ||
      record.statusTokenHash !== statusTokenHash
    ) {
      throw new RegistryError("not_found");
    }
    return {
      expiresAt: record.expiresAt,
      reportId: record.reportId,
      uploadState: record.uploadState,
    };
  }

  async getForMaintainer(
    supportCode: string,
    now: number,
  ): Promise<ReportRecord> {
    const reportId = await this.storage.get<string>(
      supportCodeKey(supportCode),
    );
    if (reportId === undefined) {
      throw new RegistryError("not_found");
    }
    return this.getLiveReport(reportId, now);
  }

  async deleteForMaintainer(
    supportCode: string,
    now: number,
  ): Promise<ReportRecord> {
    const record = await this.getForMaintainer(supportCode, now);
    await this.deleteRecord(record);
    await this.scheduleNextExpiry().catch(() => undefined);
    return record;
  }

  async prune(now: number): Promise<string[]> {
    const records = await this.storage.list<ReportRecord>({
      prefix: REPORT_PREFIX,
    });
    const expiredObjectKeys: string[] = [];
    for (const record of records.values()) {
      if (record.expiresAt <= now) {
        expiredObjectKeys.push(record.objectKey);
        await this.deleteRecord(record);
      }
    }

    const usages = await this.storage.list<ClientUsage>({
      prefix: CLIENT_USAGE_PREFIX,
    });
    for (const [key, usage] of usages) {
      if (usage.windowStartedAt + DAY_MS <= now) {
        await this.storage.delete(key);
      }
    }

    await this.scheduleNextExpiry().catch(() => undefined);
    return expiredObjectKeys;
  }

  private async nextClientUsage(
    clientFingerprint: string,
    now: number,
  ): Promise<readonly [string, ClientUsage]> {
    const key = clientUsageKey(clientFingerprint);
    const existing = await this.storage.get<ClientUsage>(key);
    const usage =
      existing === undefined || existing.windowStartedAt + DAY_MS <= now
        ? { count: 0, windowStartedAt: now }
        : existing;

    if (usage.count >= MAX_DAILY_REPORTS_PER_CLIENT) {
      throw new RegistryError("client_daily_limit");
    }

    usage.count += 1;
    return [key, usage] as const;
  }

  private assertUploadAuthorization(
    record: ReportRecord,
    uploadTokenHash: string,
    now: number,
  ): void {
    if (record.uploadTokenHash !== uploadTokenHash) {
      throw new RegistryError("not_found");
    }
    if (record.uploadExpiresAt <= now) {
      throw new RegistryError("upload_expired");
    }
    if (record.uploadState !== "pending") {
      throw new RegistryError("upload_consumed");
    }
  }

  private async getLiveReport(
    reportId: string,
    now: number,
  ): Promise<ReportRecord> {
    const record = await this.storage.get<ReportRecord>(reportKey(reportId));
    if (record === undefined) {
      throw new RegistryError("not_found");
    }
    if (record.expiresAt <= now) {
      await this.deleteRecord(record);
      throw new RegistryError("not_found", record.objectKey);
    }
    return record;
  }

  private async deleteRecord(record: ReportRecord): Promise<void> {
    await this.storage.delete([
      reportKey(record.reportId),
      supportCodeKey(record.supportCode),
    ]);
  }

  private async scheduleNextExpiry(): Promise<void> {
    const records = await this.storage.list<ReportRecord>({
      prefix: REPORT_PREFIX,
    });
    let nextExpiry: number | undefined;
    for (const record of records.values()) {
      if (nextExpiry === undefined || record.expiresAt < nextExpiry) {
        nextExpiry = record.expiresAt;
      }
    }
    if (nextExpiry !== undefined) {
      await this.storage.setAlarm(nextExpiry);
    }
  }
}

export function registryErrorCode(
  value: unknown,
): RegistryErrorCode | undefined {
  if (typeof value !== "string") {
    return undefined;
  }
  const codes: readonly RegistryErrorCode[] = [
    "client_daily_limit",
    "not_found",
    "support_code_collision",
    "total_capacity_reached",
    "upload_consumed",
    "upload_expired",
  ];
  return codes.includes(value as RegistryErrorCode)
    ? (value as RegistryErrorCode)
    : undefined;
}
