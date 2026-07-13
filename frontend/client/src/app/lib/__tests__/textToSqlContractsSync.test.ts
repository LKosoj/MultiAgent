import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import {
  TEXT_TO_SQL_REASON_CODES,
  TEXT_TO_SQL_TERMINAL_OUTCOME_STATUSES,
  terminalFields,
} from "../textToSqlContracts";

// Source of truth generated from workflow/text_to_sql_contract.py
// (TextToSqlTerminalStatus / TextToSqlTerminalReasonCode / required terminal
// outcome fields). This test guards against the frontend contract constants
// drifting from the backend enum definitions.
const schemaPath = fileURLToPath(new URL(
  "../../../../../../tests/fixtures/text_to_sql_contract_schema.json",
  import.meta.url,
));
const schema = JSON.parse(readFileSync(schemaPath, "utf8")) as {
  reason_codes: string[];
  required_fields: string[];
  terminal_statuses: string[];
};

describe("Text-to-SQL contract constants stay in sync with the Python schema", () => {
  it("mirrors TextToSqlTerminalStatus", () => {
    expect([...TEXT_TO_SQL_TERMINAL_OUTCOME_STATUSES].sort())
      .toEqual([...schema.terminal_statuses].sort());
  });

  it("mirrors TextToSqlTerminalReasonCode", () => {
    expect([...TEXT_TO_SQL_REASON_CODES].sort())
      .toEqual([...schema.reason_codes].sort());
  });

  it("mirrors the required terminal outcome fields", () => {
    expect([...terminalFields].sort())
      .toEqual([...schema.required_fields].sort());
  });
});
