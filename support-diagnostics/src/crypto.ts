const SUPPORT_CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";
const textEncoder = new TextEncoder();

function bytesToHex(bytes: Uint8Array): string {
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join(
    "",
  );
}

function bytesToBase64Url(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }
  return btoa(binary)
    .replaceAll("+", "-")
    .replaceAll("/", "_")
    .replace(/=+$/u, "");
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

export async function sha256Bytes(
  value: ArrayBuffer | Uint8Array | string,
): Promise<Uint8Array> {
  const source =
    typeof value === "string"
      ? textEncoder.encode(value).buffer
      : value instanceof Uint8Array
        ? arrayBufferFromBytes(value)
        : value;
  return new Uint8Array(await crypto.subtle.digest("SHA-256", source));
}

export async function sha256Hex(
  value: ArrayBuffer | Uint8Array | string,
): Promise<string> {
  return bytesToHex(await sha256Bytes(value));
}

export async function constantTimeTextEqual(
  left: string,
  right: string,
): Promise<boolean> {
  const [leftDigest, rightDigest] = await Promise.all([
    sha256Bytes(left),
    sha256Bytes(right),
  ]);
  let difference = 0;
  for (let index = 0; index < leftDigest.length; index += 1) {
    difference |= leftDigest[index]! ^ rightDigest[index]!;
  }
  return difference === 0;
}

export function generateToken(byteLength = 32): string {
  const bytes = new Uint8Array(byteLength);
  crypto.getRandomValues(bytes);
  return bytesToBase64Url(bytes);
}

export function generateSupportCode(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  const code = Array.from(
    bytes,
    (byte) => SUPPORT_CODE_ALPHABET[byte & 31]!,
  ).join("");
  return `BDAVP-${code}`;
}

export function hexToArrayBuffer(value: string): ArrayBuffer {
  const bytes = new Uint8Array(value.length / 2);
  for (let index = 0; index < value.length; index += 2) {
    bytes[index / 2] = Number.parseInt(value.slice(index, index + 2), 16);
  }
  return bytes.buffer;
}

export function isSha256(value: unknown): value is string {
  return typeof value === "string" && /^[a-f0-9]{64}$/u.test(value);
}

export function isSupportCode(value: string): boolean {
  return /^BDAVP-[0-9ABCDEFGHJKMNPQRSTVWXYZ]{16}$/u.test(value);
}
