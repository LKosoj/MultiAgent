import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import { createTextToSqlClient } from "../textToSqlClient";
import {
  isConfirmedTextToSqlResult,
  isConfirmedTextToSqlResultForRun,
  textToSqlStatusForRun,
} from "../textToSqlRunState";

// Second review round of W4-4.1: normalizeTextToSqlTerminalOutcome existed but
// was only ever called from textToSqlContracts.ts. The production hook
// (useTextToSqlRun.ts) reads terminal outcomes through
// textToSqlClient.getStatus/getResult and textToSqlRunState.ts's
// isConfirmedTextToSqlResult(ForRun)/textToSqlStatusForRun helpers, which
// never normalized — so a legacy terminal outcome (missing `provenance`,
// e.g. from a backend that predates W4-4.1) made the run look unconfirmed/
// statusless in the UI. The fix normalizes once, at the boundary, inside
// createTextToSqlClient's getStatus/getResult so every downstream reader
// sees an already-normalized payload.
const vectors = JSON.parse(readFileSync(fileURLToPath(new URL(
  "../../../../../../tests/fixtures/text_to_sql_terminal_contract_vectors.json",
  import.meta.url,
)), "utf8")) as { base: Record<string, unknown> };

function legacyTerminalOutcome(): Record<string, unknown> {
  const outcome = { ...vectors.base };
  delete outcome.provenance;
  return outcome;
}

const noopStart = () => Promise.reject(new Error("start() is unused in this test"));

describe("createTextToSqlClient normalizes legacy terminal outcomes at the source", () => {
  it("getResult: a flat legacy payload becomes a confirmed result", async () => {
    const runId = vectors.base.run_id as string;
    const rawPayload = { run_id: runId, terminal_outcome: legacyTerminalOutcome() };
    const client = createTextToSqlClient(
      async () => rawPayload,
      noopStart,
    );

    const result = await client.getResult(runId);

    expect(isConfirmedTextToSqlResult(result)).toBe(true);
    expect(isConfirmedTextToSqlResultForRun(result, runId)).toBe(true);
    // The raw payload handed back by the transport must stay untouched.
    expect(rawPayload.terminal_outcome).not.toHaveProperty("provenance");
  });

  it("getStatus: a flat legacy payload resolves to a non-null status", async () => {
    const runId = vectors.base.run_id as string;
    const rawPayload = { run_id: runId, terminal_outcome: legacyTerminalOutcome() };
    const client = createTextToSqlClient(
      async () => rawPayload,
      noopStart,
    );

    const status = await client.getStatus(runId);
    const resolved = textToSqlStatusForRun(status, runId);

    expect(resolved).not.toBeNull();
    expect(rawPayload.terminal_outcome).not.toHaveProperty("provenance");
  });

  it("getStatus: a legacy payload nested under `status` resolves to a non-null status", async () => {
    const runId = vectors.base.run_id as string;
    const rawStatus = { run_id: runId, terminal_outcome: legacyTerminalOutcome() };
    const rawPayload = { run_id: runId, status: rawStatus };
    const client = createTextToSqlClient(
      async () => rawPayload,
      noopStart,
    );

    const status = await client.getStatus(runId);
    const resolved = textToSqlStatusForRun(status, runId);

    expect(resolved).not.toBeNull();
    // Neither the outer payload nor the nested status object was mutated.
    expect(rawStatus.terminal_outcome).not.toHaveProperty("provenance");
    expect(rawPayload.status).toBe(rawStatus);
  });

  it("getResult: an already-normalized payload is returned unchanged (idempotent)", async () => {
    const runId = vectors.base.run_id as string;
    const rawPayload = { run_id: runId, terminal_outcome: { ...vectors.base } };
    const client = createTextToSqlClient(
      async () => rawPayload,
      noopStart,
    );

    const result = await client.getResult(runId);

    expect(result).toBe(rawPayload);
  });
});
