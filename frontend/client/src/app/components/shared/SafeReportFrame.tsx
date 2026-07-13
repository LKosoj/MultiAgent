"use client";

import React, { useEffect, useMemo, useState } from "react";
import { decodeGzipBase64 } from "../../utils/report";
import {
  parseStaticReportEnvelope,
  staticReportDigestMatches,
  STATIC_REPORT_MIME,
} from "../../utils/staticReport";


type SafeReportFrameProps = {
  report: unknown;
  structuredFallback: React.ReactNode;
  title?: string;
};

export function releaseReportBlobUrl(
  blobUrl: string | null,
  revoke: (url: string) => void = URL.revokeObjectURL,
): void {
  if (blobUrl) revoke(blobUrl);
}

export function SafeReportIframe({
  blobUrl,
  title,
}: {
  blobUrl: string;
  title: string;
}) {
  return (
    <iframe
      src={blobUrl}
      title={title}
      sandbox=""
      className="h-[70vh] w-full rounded border border-slate-700 bg-white"
    />
  );
}

export function SafeReportFrame({
  report,
  structuredFallback,
  title = "Static report preview",
}: SafeReportFrameProps) {
  const envelope = useMemo(() => parseStaticReportEnvelope(report), [report]);
  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  const [previewError, setPreviewError] = useState(false);

  useEffect(() => {
    let cancelled = false;
    let createdUrl: string | null = null;
    setBlobUrl(null);
    setPreviewError(false);
    if (!envelope) return undefined;

    void (async () => {
      try {
        const html = await decodeGzipBase64(envelope.content_b64_gzip);
        if (!await staticReportDigestMatches(html, envelope.content_sha256)) {
          throw new Error("Static report digest mismatch");
        }
        createdUrl = URL.createObjectURL(new Blob([html], { type: STATIC_REPORT_MIME }));
        if (cancelled) {
          releaseReportBlobUrl(createdUrl);
          createdUrl = null;
          return;
        }
        setBlobUrl(createdUrl);
      } catch {
        if (!cancelled) setPreviewError(true);
      }
    })();

    return () => {
      cancelled = true;
      releaseReportBlobUrl(createdUrl);
    };
  }, [envelope]);

  return (
    <section aria-label="Safe static report">
      {envelope && blobUrl ? (
        <SafeReportIframe blobUrl={blobUrl} title={title} />
      ) : envelope && !previewError ? (
        <p role="status">Preparing static report preview…</p>
      ) : null}
      {previewError ? (
        <p role="status">Static preview unavailable. Structured report data follows.</p>
      ) : null}
      <div data-testid="structured-report-fallback">{structuredFallback}</div>
    </section>
  );
}

