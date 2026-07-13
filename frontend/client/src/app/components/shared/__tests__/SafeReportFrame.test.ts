import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import {
  SafeReportFrame,
  SafeReportIframe,
  releaseReportBlobUrl,
} from "../SafeReportFrame";
import {
  parseStaticReportEnvelope,
  type StaticReportEnvelope,
} from "../../../utils/staticReport";


const validEnvelope: StaticReportEnvelope = {
  renderer_version: "static-v1",
  content_sha256: "a".repeat(64),
  mime_type: "text/html; charset=utf-8",
  content_encoding: "gzip+base64",
  content_b64_gzip: "H4sIAAAAAAACA7PJSM3JyddRKM8vyklRBAA8BCFjDgAAAA==",
};


describe("static report envelope", () => {
  it("accepts only static-v1 HTML gzip envelopes", () => {
    expect(parseStaticReportEnvelope(validEnvelope)).toEqual(validEnvelope);
    for (const report of [
      { ...validEnvelope, renderer_version: "legacy" },
      { ...validEnvelope, mime_type: "text/html" },
      { ...validEnvelope, content_encoding: "identity" },
      { ...validEnvelope, content_sha256: "not-a-digest" },
      { ...validEnvelope, content_b64_gzip: "" },
      { mime_type: validEnvelope.mime_type, content_b64_gzip: validEnvelope.content_b64_gzip },
    ]) {
      expect(parseStaticReportEnvelope(report)).toBeNull();
    }
  });
});


describe("SafeReportFrame", () => {
  it("never previews legacy payload and keeps structured fallback visible", () => {
    const markup = renderToStaticMarkup(
      React.createElement(SafeReportFrame, {
        report: { mime_type: "text/html", content_b64_gzip: "legacy" },
        structuredFallback: React.createElement(
          "pre",
          null,
          "<script>structured only</script>",
        ),
      }),
    );

    expect(markup).not.toContain("<iframe");
    expect(markup).toContain("&lt;script&gt;structured only&lt;/script&gt;");
  });

  it("uses an empty iframe sandbox with no script or same-origin grant", () => {
    const markup = renderToStaticMarkup(
      React.createElement(SafeReportIframe, {
        blobUrl: "blob:static-report",
        title: "Trusted report",
      }),
    );

    expect(markup).toContain('sandbox=""');
    expect(markup).not.toContain("allow-scripts");
    expect(markup).not.toContain("allow-same-origin");
    expect(markup).not.toContain("srcdoc");
  });

  it("releases blob URLs and component cleanup calls the release helper", () => {
    const revoke = vi.fn();
    releaseReportBlobUrl("blob:old", revoke);
    releaseReportBlobUrl(null, revoke);
    expect(revoke).toHaveBeenCalledTimes(1);
    expect(revoke).toHaveBeenCalledWith("blob:old");

    const source = readFileSync(fileURLToPath(new URL("../SafeReportFrame.tsx", import.meta.url)), "utf8");
    expect(source).toContain("releaseReportBlobUrl(createdUrl)");
  });
});

