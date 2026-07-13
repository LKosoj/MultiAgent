export const STATIC_REPORT_VERSION = "static-v1" as const;
export const STATIC_REPORT_MIME = "text/html; charset=utf-8" as const;
export const STATIC_REPORT_ENCODING = "gzip+base64" as const;

export type StaticReportEnvelope = {
  renderer_version: typeof STATIC_REPORT_VERSION;
  content_sha256: string;
  mime_type: typeof STATIC_REPORT_MIME;
  content_encoding: typeof STATIC_REPORT_ENCODING;
  content_b64_gzip: string;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

export function parseStaticReportEnvelope(value: unknown): StaticReportEnvelope | null {
  if (!isRecord(value)) return null;
  const fields = [
    "renderer_version",
    "content_sha256",
    "mime_type",
    "content_encoding",
    "content_b64_gzip",
  ];
  if (Object.keys(value).length !== fields.length || fields.some((field) => !(field in value))) {
    return null;
  }
  if (
    value.renderer_version !== STATIC_REPORT_VERSION
    || value.mime_type !== STATIC_REPORT_MIME
    || value.content_encoding !== STATIC_REPORT_ENCODING
    || typeof value.content_sha256 !== "string"
    || !/^[0-9a-f]{64}$/.test(value.content_sha256)
    || typeof value.content_b64_gzip !== "string"
    || !value.content_b64_gzip
    || value.content_b64_gzip.length % 4 !== 0
    || !/^[A-Za-z0-9+/]+={0,2}$/.test(value.content_b64_gzip)
  ) {
    return null;
  }
  return value as StaticReportEnvelope;
}

export async function staticReportDigestMatches(
  html: string,
  expectedDigest: string,
): Promise<boolean> {
  if (!globalThis.crypto?.subtle) return false;
  const digest = await globalThis.crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(html),
  );
  const actual = Array.from(new Uint8Array(digest), (byte) => (
    byte.toString(16).padStart(2, "0")
  )).join("");
  return actual === expectedDigest;
}

