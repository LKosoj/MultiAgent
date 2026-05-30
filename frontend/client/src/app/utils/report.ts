"use client";
// Модуль использует исключительно browser-only API (window, atob, DecompressionStream,
// URL.createObjectURL, document). Директива 'use client' держит явную клиентскую границу;
// все текущие импортёры — сами client-компоненты, поэтому жёсткой server-границы не возникает.

export const openReportFromPayload = async (runId: string, report: {
  base64_gzip?: string;
  content_b64_gzip?: string;
  report_b64_gzip?: string;
  base64?: string;
  mime_type?: string;
  filename?: string;
}): Promise<void> => {
  const payload =
    report?.base64_gzip ??
    report?.content_b64_gzip ??
    report?.report_b64_gzip ??
    report?.base64;
  if (!payload) {
    throw new Error("Пустой отчёт");
  }
  const html = await decodeGzipBase64(payload);
  const mimeType = report?.mime_type ?? "text/html";
  const filename = report?.filename ?? `report_${runId}.html`;
  const blob = new Blob([html], { type: mimeType });
  const url = URL.createObjectURL(blob);
  const opened = window.open(url, "_blank", "noopener,noreferrer");
  if (!opened) {
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    link.click();
  }
  window.setTimeout(() => URL.revokeObjectURL(url), 10000);
};

export const decodeGzipBase64 = async (b64: string): Promise<string> => {
  const binary = atob(b64);
  const bytes = Uint8Array.from(binary, (char) => char.charCodeAt(0));
  if (!("DecompressionStream" in window)) {
    throw new Error("Браузер не поддерживает распаковку gzip");
  }
  const stream = new DecompressionStream("gzip");
  const body = new Response(bytes).body;
  if (!body) {
    throw new Error("Не удалось распаковать отчёт");
  }
  const decompressed = body.pipeThrough(stream);
  return new Response(decompressed).text();
};
