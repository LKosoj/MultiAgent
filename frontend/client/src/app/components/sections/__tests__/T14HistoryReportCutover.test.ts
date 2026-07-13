import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it, vi } from "vitest";

import { createTextToSqlClient } from "../../../lib/textToSqlClient";
import { textToSqlHistoryEntries } from "../TextToSqlSection";


describe("T14 Text-to-SQL client cutover", () => {
  it("uses owner history actions and exposes no append method", async () => {
    const runServiceAction = vi.fn(async () => ({}));
    const client = createTextToSqlClient(
      runServiceAction,
      vi.fn(async () => ({ run_id: "run-1" })),
    );

    await client.listHistory(20, 4);
    await client.historyAnalytics();
    await client.clearHistory();

    expect(runServiceAction.mock.calls).toEqual([
      ["text_to_sql.history.list", { limit: 20, offset: 4 }],
      ["text_to_sql.history.analytics", {}],
      ["text_to_sql.history.clear", { confirm: true }],
    ]);
    expect(client).not.toHaveProperty("appendHistory");
  });

  it("projects the typed server history snapshot for the existing history UI", () => {
    const vectors = JSON.parse(readFileSync(fileURLToPath(new URL(
      "../../../../../../../tests/fixtures/text_to_sql_terminal_contract_vectors.json",
      import.meta.url,
    )), "utf8")) as { base: Record<string, unknown> };

    const [entry] = textToSqlHistoryEntries({
      entries: [{
        tenant_id: "tenant-a",
        owner_subject: "alice",
        run_id: "run-1",
        created_at_ms: 1_700_000_000_000,
        status: "succeeded",
        dialect: "postgres",
        profile_name: "strict",
        terminal_snapshot: { ...vectors.base, run_id: "run-1" },
      }],
    });

    expect(entry).toMatchObject({
      run_id: "run-1",
      status: "succeeded",
      dialect: "postgres",
      sql_query: vectors.base.sql,
    });
  });

  it("wires SafeReportFrame and removes the temporary adapter and active sandbox", () => {
    const source = readFileSync(fileURLToPath(new URL(
      "../TextToSqlSection.tsx",
      import.meta.url,
    )), "utf8");
    const modalSource = readFileSync(fileURLToPath(new URL(
      "../TextToSqlResultModal.tsx",
      import.meta.url,
    )), "utf8");

    expect(modalSource).toContain("<SafeReportFrame");
    expect(source).not.toContain("legacyTextToSqlHistory");
    expect(source).not.toContain("createLegacyTextToSqlHistory");
    expect(source).not.toContain("text_to_sql.history.append");
    expect(source).not.toContain("allow-scripts");
    expect(source).not.toContain("allow-same-origin");
    expect(source).not.toContain("reportPreviewUrl");
  });
});
