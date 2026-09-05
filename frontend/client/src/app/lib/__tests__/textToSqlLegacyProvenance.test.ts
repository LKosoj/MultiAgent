import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import {
  normalizeTextToSqlTerminalOutcome,
  TEXT_TO_SQL_LEGACY_TERMINAL_FIELD_DEFAULTS,
} from "../textToSqlClient";
import { isTextToSqlTerminalOutcome } from "../textToSqlContracts";

// W4-4.1 added the required `provenance` field to an already-shipped
// terminal contract (see workflow/text_to_sql_contract.py
// _LEGACY_OPTIONAL_TERMINAL_FIELDS). Stored history / older backends can
// still send a terminal outcome without the key; normalizeTextToSqlTerminalOutcome
// must fill in the same legacy default so isTextToSqlTerminalOutcome does
// not reject an otherwise-valid payload.
const vectors = JSON.parse(readFileSync(fileURLToPath(new URL(
  "../../../../../../tests/fixtures/text_to_sql_terminal_contract_vectors.json",
  import.meta.url,
)), "utf8")) as { base: Record<string, unknown> };

function legacyPayloadMissingProvenance(): Record<string, unknown> {
  const payload = { ...vectors.base };
  delete payload.provenance;
  return payload;
}

describe("normalizeTextToSqlTerminalOutcome", () => {
  it("defaults a legacy payload's missing provenance to {}", () => {
    const legacy = legacyPayloadMissingProvenance();
    expect(legacy).not.toHaveProperty("provenance");

    const normalized = normalizeTextToSqlTerminalOutcome(legacy) as Record<string, unknown>;

    expect(normalized.provenance).toEqual({});
    expect(legacy).not.toHaveProperty("provenance");
  });

  it("leaves a payload that already has provenance untouched", () => {
    const payload = { ...vectors.base, provenance: { run_id: "run-vector-1" } };

    const normalized = normalizeTextToSqlTerminalOutcome(payload);

    expect(normalized).toBe(payload);
  });

  it("passes non-record values through unchanged", () => {
    expect(normalizeTextToSqlTerminalOutcome(null)).toBeNull();
    expect(normalizeTextToSqlTerminalOutcome("not-a-record")).toBe("not-a-record");
  });

  it("only knows about the fields the Python side documents as legacy-optional", () => {
    expect(Object.keys(TEXT_TO_SQL_LEGACY_TERMINAL_FIELD_DEFAULTS)).toEqual(["provenance"]);
  });
});

describe("isTextToSqlTerminalOutcome with legacy payloads", () => {
  it("rejects a legacy payload missing provenance before normalization", () => {
    expect(isTextToSqlTerminalOutcome(legacyPayloadMissingProvenance())).toBe(false);
  });

  it("accepts the same payload once normalized", () => {
    const normalized = normalizeTextToSqlTerminalOutcome(legacyPayloadMissingProvenance());
    expect(isTextToSqlTerminalOutcome(normalized)).toBe(true);
  });

  it("still rejects a payload missing a non-legacy required field", () => {
    const payload = { ...vectors.base };
    delete payload.sql;

    const normalized = normalizeTextToSqlTerminalOutcome(payload);

    expect(isTextToSqlTerminalOutcome(normalized)).toBe(false);
  });
});
