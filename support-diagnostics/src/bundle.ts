import { MAX_BUNDLE_BYTES } from "./protocol.js";

const CENTRAL_DIRECTORY_SIGNATURE = 0x02014b50;
const END_OF_CENTRAL_DIRECTORY_SIGNATURE = 0x06054b50;
const LOCAL_FILE_HEADER_SIGNATURE = 0x04034b50;
const UTF8_FLAG = 0x0800;
const DEFLATE_METHOD = 8;
const MAX_UNCOMPRESSED_BUNDLE_BYTES = 1_500_000;
const ENTRY_LIMITS = new Map<string, number>([
  ["manifest.json", 64 * 1024],
  ["events.jsonl", 320 * 1024],
  ["storage.json", 160 * 1024],
  ["tool-tail.txt", 640 * 1024],
]);

interface ZipEntry {
  compressedSize: number;
  crc32: number;
  flags: number;
  localHeaderOffset: number;
  name: string;
  uncompressedSize: number;
}

export class InvalidDiagnosticBundleError extends Error {
  constructor() {
    super("invalid_diagnostic_bundle");
  }
}

function invalid(): never {
  throw new InvalidDiagnosticBundleError();
}

function requireRange(bytes: Uint8Array, offset: number, length: number): void {
  if (
    !Number.isSafeInteger(offset) ||
    !Number.isSafeInteger(length) ||
    offset < 0 ||
    length < 0 ||
    offset + length > bytes.byteLength
  ) {
    invalid();
  }
}

function readUint16(bytes: Uint8Array, offset: number): number {
  requireRange(bytes, offset, 2);
  return bytes[offset]! | (bytes[offset + 1]! << 8);
}

function readUint32(bytes: Uint8Array, offset: number): number {
  requireRange(bytes, offset, 4);
  return (
    (bytes[offset]! |
      (bytes[offset + 1]! << 8) |
      (bytes[offset + 2]! << 16) |
      (bytes[offset + 3]! << 24)) >>>
    0
  );
}

function decodeUTF8(bytes: Uint8Array): string {
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    return invalid();
  }
}

function validFlags(flags: number): boolean {
  return flags === 0 || flags === UTF8_FLAG;
}

function findEndOfCentralDirectory(bytes: Uint8Array): number {
  if (bytes.byteLength < 22 || bytes.byteLength > MAX_BUNDLE_BYTES) {
    return invalid();
  }
  const offset = bytes.byteLength - 22;
  if (readUint32(bytes, offset) !== END_OF_CENTRAL_DIRECTORY_SIGNATURE) {
    return invalid();
  }
  return offset;
}

function parseCentralDirectory(bytes: Uint8Array): ZipEntry[] {
  const endOffset = findEndOfCentralDirectory(bytes);
  const diskNumber = readUint16(bytes, endOffset + 4);
  const centralDirectoryDisk = readUint16(bytes, endOffset + 6);
  const diskEntryCount = readUint16(bytes, endOffset + 8);
  const entryCount = readUint16(bytes, endOffset + 10);
  const centralDirectorySize = readUint32(bytes, endOffset + 12);
  const centralDirectoryOffset = readUint32(bytes, endOffset + 16);
  const commentLength = readUint16(bytes, endOffset + 20);
  if (
    diskNumber !== 0 ||
    centralDirectoryDisk !== 0 ||
    diskEntryCount !== ENTRY_LIMITS.size ||
    entryCount !== ENTRY_LIMITS.size ||
    commentLength !== 0 ||
    endOffset + 22 !== bytes.byteLength ||
    centralDirectoryOffset + centralDirectorySize !== endOffset
  ) {
    return invalid();
  }

  const entries: ZipEntry[] = [];
  const names = new Set<string>();
  let uncompressedBytes = 0;
  let offset = centralDirectoryOffset;
  while (offset < endOffset) {
    requireRange(bytes, offset, 46);
    if (readUint32(bytes, offset) !== CENTRAL_DIRECTORY_SIGNATURE) {
      return invalid();
    }
    const versionNeeded = readUint16(bytes, offset + 6);
    const flags = readUint16(bytes, offset + 8);
    const method = readUint16(bytes, offset + 10);
    const crc32 = readUint32(bytes, offset + 16);
    const compressedSize = readUint32(bytes, offset + 20);
    const uncompressedSize = readUint32(bytes, offset + 24);
    const nameLength = readUint16(bytes, offset + 28);
    const extraLength = readUint16(bytes, offset + 30);
    const entryCommentLength = readUint16(bytes, offset + 32);
    const diskStart = readUint16(bytes, offset + 34);
    const localHeaderOffset = readUint32(bytes, offset + 42);
    const variableLength = nameLength + extraLength + entryCommentLength;
    requireRange(bytes, offset + 46, variableLength);
    const name = decodeUTF8(bytes.subarray(offset + 46, offset + 46 + nameLength));
    const entryLimit = ENTRY_LIMITS.get(name);
    if (
      versionNeeded !== 20 ||
      !validFlags(flags) ||
      method !== DEFLATE_METHOD ||
      extraLength !== 0 ||
      entryCommentLength !== 0 ||
      diskStart !== 0 ||
      entryLimit === undefined ||
      names.has(name) ||
      uncompressedSize > entryLimit
    ) {
      return invalid();
    }
    names.add(name);
    uncompressedBytes += uncompressedSize;
    if (uncompressedBytes > MAX_UNCOMPRESSED_BUNDLE_BYTES) {
      return invalid();
    }
    entries.push({
      compressedSize,
      crc32,
      flags,
      localHeaderOffset,
      name,
      uncompressedSize,
    });
    offset += 46 + variableLength;
  }
  if (offset !== endOffset || entries.length !== ENTRY_LIMITS.size) {
    return invalid();
  }
  return entries;
}

function compressedEntry(
  bytes: Uint8Array,
  entry: ZipEntry,
  centralDirectoryOffset: number,
): { rangeEnd: number; rangeStart: number } {
  const offset = entry.localHeaderOffset;
  requireRange(bytes, offset, 30);
  if (
    readUint32(bytes, offset) !== LOCAL_FILE_HEADER_SIGNATURE ||
    readUint16(bytes, offset + 4) !== 20 ||
    readUint16(bytes, offset + 6) !== entry.flags ||
    readUint16(bytes, offset + 8) !== DEFLATE_METHOD ||
    readUint32(bytes, offset + 14) !== entry.crc32 ||
    readUint32(bytes, offset + 18) !== entry.compressedSize ||
    readUint32(bytes, offset + 22) !== entry.uncompressedSize
  ) {
    return invalid();
  }
  const nameLength = readUint16(bytes, offset + 26);
  const extraLength = readUint16(bytes, offset + 28);
  if (extraLength !== 0) {
    return invalid();
  }
  requireRange(bytes, offset + 30, nameLength);
  const name = decodeUTF8(bytes.subarray(offset + 30, offset + 30 + nameLength));
  if (name !== entry.name) {
    return invalid();
  }
  const dataOffset = offset + 30 + nameLength;
  requireRange(bytes, dataOffset, entry.compressedSize);
  const rangeEnd = dataOffset + entry.compressedSize;
  if (rangeEnd > centralDirectoryOffset) {
    return invalid();
  }
  return {
    rangeEnd,
    rangeStart: offset,
  };
}

export function validateDiagnosticBundleEnvelope(bytes: Uint8Array): void {
  try {
    const entries = parseCentralDirectory(bytes);
    const endOffset = findEndOfCentralDirectory(bytes);
    const centralDirectoryOffset = readUint32(bytes, endOffset + 16);
    const ranges: Array<{ end: number; start: number }> = [];
    for (const entry of entries) {
      const compressed = compressedEntry(bytes, entry, centralDirectoryOffset);
      ranges.push({ end: compressed.rangeEnd, start: compressed.rangeStart });
    }
    ranges.sort((left, right) => left.start - right.start);
    if (
      ranges[0]?.start !== 0 ||
      ranges.at(-1)?.end !== centralDirectoryOffset
    ) {
      return invalid();
    }
    for (let index = 1; index < ranges.length; index += 1) {
      if (ranges[index - 1]!.end !== ranges[index]!.start) {
        return invalid();
      }
    }
  } catch (error) {
    if (error instanceof InvalidDiagnosticBundleError) {
      throw error;
    }
    throw new InvalidDiagnosticBundleError();
  }
}
