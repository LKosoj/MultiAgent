import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import {
  beginTextToSqlRunSelection,
  cancelTextToSqlResultRetries,
  commitTextToSqlRunSelection,
  createTextToSqlResultLoadState,
  invalidateTextToSqlRunSelection,
  isTextToSqlHistorySummary,
  loadTextToSqlResultSingleFlight,
  loadTextToSqlResultThenArtifacts,
  loadTextToSqlStatusAfterCurrent,
  loadTextToSqlStatusSingleFlight,
  requestTextToSqlCancellation,
  scheduleTextToSqlResultRetry,
  selectTextToSqlRun,
  startTextToSqlStatusPolling,
  textToSqlHistoryEntries,
  textToSqlHistoryRowCount,
  textToSqlHistoryTerminalState,
  textToSqlRunCanCancel,
  textToSqlStatusForRun,
} from "../TextToSqlSection";
import {
  isKnownTextToSqlReasonCode,
  isTextToSqlTerminalOutcome,
} from "../../../lib/textToSqlContracts";

type TerminalVectorCase = {
  name: string;
  valid: boolean;
  overrides: Record<string, unknown>;
};

type TerminalReasonEvidenceCase = {
  reason_code: string;
  profile: string;
  valid_overrides: Record<string, unknown>;
  invalid_overrides: Record<string, unknown>;
};

type HistorySummaryVectorCase = {
  name: string;
  summary: Record<string, unknown>;
};

function deepMergeRecord(
  base: Record<string, unknown>,
  overrides: Record<string, unknown>,
): Record<string, unknown> {
  const merged = structuredClone(base);
  for (const [key, value] of Object.entries(overrides)) {
    const current = merged[key];
    if (
      value !== null
      && typeof value === "object"
      && !Array.isArray(value)
      && Object.keys(value).length === 1
      && Object.prototype.hasOwnProperty.call(value, "$replace")
    ) {
      merged[key] = structuredClone((value as Record<string, unknown>).$replace);
      continue;
    }
    if (
      value !== null
      && typeof value === "object"
      && !Array.isArray(value)
      && current !== null
      && typeof current === "object"
      && !Array.isArray(current)
    ) {
      merged[key] = deepMergeRecord(
        current as Record<string, unknown>,
        value as Record<string, unknown>,
      );
    } else {
      merged[key] = structuredClone(value);
    }
  }
  return merged;
}

const terminalVectorPath = fileURLToPath(new URL(
  "../../../../../../../tests/fixtures/text_to_sql_terminal_contract_vectors.json",
  import.meta.url,
));
const terminalVectorSet = JSON.parse(
  readFileSync(terminalVectorPath, "utf8"),
) as {
  base: Record<string, unknown>;
  cases: TerminalVectorCase[];
  reason_evidence_cases: TerminalReasonEvidenceCase[];
};
const historySummaryVectorPath = fileURLToPath(new URL(
  "../../../../../../../tests/fixtures/text_to_sql_history_summary_contract_vectors.json",
  import.meta.url,
));
const historySummaryVectorSet = JSON.parse(
  readFileSync(historySummaryVectorPath, "utf8"),
) as { cases: HistorySummaryVectorCase[] };

function terminalOutcome(status: string) {
  const succeeded = status === "succeeded";
  const hasGeneratedSql = status !== "cancelled" && status !== "timed_out";
  const reasonCode = status === "succeeded"
    ? ""
    : status === "abstained"
      ? "VERIFIER_REJECTED"
      : status === "cancelled"
        ? "CANCELLED"
        : status === "timed_out"
          ? "TIMED_OUT"
          : "DB_AUDIT_MISSING";
  return {
    run_id: "run-1",
    status,
    reason_code: reasonCode,
    sql: hasGeneratedSql ? "SELECT 1" : "",
    generated: hasGeneratedSql,
    approved: succeeded,
    executed: succeeded,
    dry_run: false,
    audited: succeeded,
    data: succeeded ? [[1]] : [],
    columns: succeeded ? ["value"] : [],
    rows_affected: succeeded ? 1 : 0,
    error: succeeded ? null : status,
    execution: succeeded ? {
      success: true,
      data: [[1]],
      columns: ["value"],
      rows_affected: 1,
      execution_time_ms: 1,
      dry_run_only: false,
      skipped_execution: false,
      sql_query: "SELECT 1",
      applied_row_limit: 10,
    } : {},
    audit: succeeded ? { status: "logged", log_id: "audit-1" } : {},
    persistence: succeeded ? {
      status: "saved",
      filename: "query.md",
      path: "/tmp/query.md",
    } : { status: "not_attempted" },
  };
}

function failedExecutionOutcome(
  executionOverrides: Record<string, unknown> = {},
  outcomeOverrides: Record<string, unknown> = {},
) {
  return {
    ...terminalOutcome("failed"),
    reason_code: "EXECUTION_FAILED",
    generated: true,
    approved: true,
    executed: true,
    audited: true,
    audit: { status: "logged", log_id: "audit-1" },
    ...outcomeOverrides,
    execution: {
      success: false,
      data: [],
      columns: [],
      rows_affected: 0,
      execution_time_ms: 1,
      dry_run_only: false,
      skipped_execution: false,
      error_message: "database unavailable",
      sql_query: "SELECT 1",
      applied_row_limit: 10,
      ...executionOverrides,
    },
  };
}

function auditFailureOutcome(audit: Record<string, unknown>) {
  return {
    ...terminalOutcome("failed"),
    reason_code: "AUDIT_FAILED",
    generated: true,
    approved: true,
    executed: true,
    data: [[1]],
    columns: ["value"],
    rows_affected: 1,
    execution: {
      success: true,
      data: [[1]],
      columns: ["value"],
      rows_affected: 1,
      execution_time_ms: 1,
      dry_run_only: false,
      skipped_execution: false,
      sql_query: "SELECT 1",
      applied_row_limit: 10,
    },
    audit,
  };
}

function successfulExecutionWithout(field: string) {
  const execution: Record<string, unknown> = {
    ...terminalOutcome("succeeded").execution,
  };
  delete execution[field];
  return execution;
}

function strictSuccessfulOutcome(overrides: Record<string, unknown> = {}) {
  return {
    ...terminalOutcome("succeeded"),
    audit: { status: "logged", log_id: "audit-1" },
    persistence: {
      status: "saved",
      filename: "query.md",
      path: "/tmp/query.md",
    },
    ...overrides,
  };
}

function boundedHistorySummary(overrides: Record<string, unknown> = {}) {
  return {
    run_id: "run-history-summary",
    session_id: "session-history-summary",
    workflow_name: "text_to_sql_pipeline",
    timestamp: "2026-07-11T08:00:00",
    status: "succeeded",
    success: true,
    reason_code: "",
    generated: true,
    approved: true,
    executed: true,
    dry_run: false,
    audited: true,
    sql_query: "SELECT value FROM items",
    row_count: 2,
    rows_affected: 2,
    column_count: 1,
    execution: {
      success: true,
      dry_run_only: false,
      skipped_execution: false,
      rows_affected: 2,
      execution_time_ms: 1,
    },
    audit: { status: "logged", log_id: "audit-history" },
    persistence: {
      status: "saved",
      filename: "query.md",
      path: "/tmp/query.md",
    },
    error: null,
    natural_query: "show values",
    dialect: "sqlite",
    max_rows: 100,
    ...overrides,
  };
}

function failedExecutionSummary() {
  return {
    success: false,
    dry_run_only: false,
    skipped_execution: false,
    rows_affected: 0,
    execution_time_ms: 1,
    error_message: "execution failed",
  };
}

function failedHistorySummary(overrides: Record<string, unknown> = {}) {
  return boundedHistorySummary({
    status: "failed",
    success: false,
    reason_code: "EXECUTION_FAILED",
    executed: true,
    dry_run: false,
    audited: true,
    row_count: 0,
    rows_affected: 0,
    column_count: 0,
    execution: failedExecutionSummary(),
    audit: { status: "logged", log_id: "audit-failed" },
    persistence: { status: "not_attempted" },
    error: "execution failed",
    ...overrides,
  });
}

describe("Text-to-SQL history result counts", () => {
  it("uses terminal data length instead of rows_affected or max_rows", () => {
    const data = [[1], [2]];
    expect(textToSqlHistoryRowCount({
      max_rows: 500,
      terminal_outcome: strictSuccessfulOutcome({
        data,
        rows_affected: 99,
        execution: {
          ...terminalOutcome("succeeded").execution,
          data,
          rows_affected: 99,
        },
      }),
    })).toBe(2);
  });

  it("does not derive a result count from an invalid or missing terminal", () => {
    expect(textToSqlHistoryRowCount({ max_rows: 500 })).toBeNull();
    expect(textToSqlHistoryRowCount({
      max_rows: 500,
      terminal_outcome: { status: "succeeded", data: [[1]] },
    })).toBeNull();
  });

  it("uses the bounded backend summary row_count when terminal rows are omitted", () => {
    expect(textToSqlHistoryRowCount(boundedHistorySummary())).toBe(2);
  });

  it("projects a validated bounded backend summary after history reload", () => {
    expect(textToSqlHistoryTerminalState(boundedHistorySummary())).toMatchObject({
      status: "succeeded",
      success: true,
      executed: true,
      dryRun: false,
    });
  });

  it("filters malformed entries at the typed history-list boundary", () => {
    const [entry] = textToSqlHistoryEntries({
      entries: [boundedHistorySummary(), { run_id: "malformed" }],
    });

    expect(isTextToSqlHistorySummary(entry)).toBe(true);
    expect(textToSqlHistoryRowCount(entry)).toBe(2);
    expect(textToSqlHistoryEntries({ entries: [entry] })).toEqual([entry]);
  });

  it("accepts a coherent compact failed-execution summary", () => {
    expect(isTextToSqlHistorySummary(failedHistorySummary())).toBe(true);
  });

  it.each([
    ["abstained", "VERIFIER_REJECTED", true, "SELECT 1"],
    ["cancelled", "CANCELLED", false, ""],
    ["timed_out", "TIMED_OUT", false, ""],
  ])(
    "accepts %s summary with previously-forbidden runtime evidence (soft evidence coupling)",
    (status, reason_code, generated, sql_query) => {
      const softened = boundedHistorySummary({
        status,
        success: false,
        reason_code,
        generated,
        approved: false,
        executed: false,
        dry_run: false,
        audited: false,
        sql_query,
        row_count: 2,
        rows_affected: 1,
        column_count: 1,
        execution: {
          success: false,
          dry_run_only: false,
          skipped_execution: true,
          rows_affected: 1,
          execution_time_ms: 1,
          error_message: "execution evidence is no longer cross-checked",
        },
        audit: { status: "error", error: "audit evidence is no longer cross-checked" },
        persistence: { status: "not_attempted" },
        error: status,
      });

      expect(isTextToSqlHistorySummary(softened)).toBe(true);
      expect(textToSqlHistoryTerminalState(softened)).toMatchObject({
        status,
        success: false,
      });
    },
  );

  it("accepts a failure summary regardless of its nested execution evidence", () => {
    const failedAfterExecution = boundedHistorySummary({
      status: "failed",
      success: false,
      reason_code: "RESULT_AGGREGATION_FAILED",
      error: "aggregation failed",
    });

    expect(isTextToSqlHistorySummary(failedAfterExecution)).toBe(true);
    expect(isTextToSqlHistorySummary({
      ...failedAfterExecution,
      execution: {
        success: true,
        dry_run_only: false,
        skipped_execution: false,
        rows_affected: 2,
        execution_time_ms: 1,
        error_message: "no longer contradictory: evidence blocks are unchecked",
      },
    })).toBe(true);
  });

  it.each([
    { unknown_field: true },
    { execution: { success: false } },
    { audit: { status: "logged" } },
    {
      status: "failed",
      success: false,
      reason_code: "EXECUTION_FAILED",
      error: "execution failed",
      execution: {},
    },
    {
      status: "failed",
      success: false,
      reason_code: "AUDIT_FAILED",
      error: "audit failed",
      audit: {},
    },
    {
      status: "failed",
      success: false,
      reason_code: "EXECUTOR_CONTRACT_INVALID",
      error: "invalid executor output",
      execution: { success: true },
    },
    { success: false },
  ])("accepts previously-rejected open or incoherent backend summaries %#", (overrides) => {
    expect(isTextToSqlHistorySummary(boundedHistorySummary(overrides))).toBe(true);
  });

  it.each([
    { max_rows: 0 },
    { timestamp: "not-a-timestamp" },
    { flags: {} },
    { workflow_name: "generic_pipeline" },
  ])("rejects incoherent or open backend summaries %#", (overrides) => {
    expect(isTextToSqlHistorySummary(boundedHistorySummary(overrides))).toBe(false);
  });

  it.each([
    { execution: { success: false } },
    {
      execution: {
        ...failedExecutionSummary(),
        dry_run_only: true,
      },
    },
    {
      execution: {
        success: false,
        dry_run_only: false,
        skipped_execution: false,
        rows_affected: 0,
        execution_time_ms: 1,
      },
    },
  ])("accepts a failed execution summary with previously-incomplete nested evidence %#", (overrides) => {
    expect(isTextToSqlHistorySummary(failedHistorySummary(overrides))).toBe(true);
  });

  it.each([true, -1, 1.5, "2", null])(
    "rejects malformed bounded summary row_count %#",
    (row_count) => {
      const malformed = boundedHistorySummary({ row_count });
      expect(textToSqlHistoryRowCount(malformed)).toBeNull();
      expect(textToSqlHistoryTerminalState(malformed)).toMatchObject({
        status: "invalid_terminal",
        success: false,
      });
    },
  );
});

describe("shared backend-to-UI history summary contract vectors", () => {
  it.each(historySummaryVectorSet.cases)(
    "accepts Python producer vector '$name'",
    ({ summary }) => {
      expect(isTextToSqlHistorySummary(summary)).toBe(true);
      expect(textToSqlHistoryEntries({ entries: [summary] })).toEqual([summary]);
      expect(textToSqlHistoryRowCount(summary)).toBe(summary.row_count);
      expect(textToSqlHistoryTerminalState(summary)).toMatchObject({
        status: summary.status,
        success: summary.success,
        executed: summary.executed,
        dryRun: summary.dry_run,
      });
    },
  );

});

// The softened validators no longer enforce evidence-profile coupling between
// reason_code/status and the execution/audit/persistence blocks (see
// isTextToSqlTerminalOutcome / isTextToSqlHistorySummary). What remains a firm
// regression guarantee is that anything the Python producer considers valid
// must still validate on the frontend.
describe("shared Text-to-SQL terminal contract vectors", () => {
  it.each(terminalVectorSet.cases.filter((vectorCase) => vectorCase.valid))(
    "still accepts previously-valid Python producer vector '$name'",
    (vectorCase) => {
      const terminal_outcome = deepMergeRecord(
        terminalVectorSet.base,
        vectorCase.overrides,
      );
      const state = textToSqlHistoryTerminalState({ terminal_outcome });

      expect(state.status).not.toBe("invalid_terminal");
    },
  );

  it.each(terminalVectorSet.reason_evidence_cases)(
    "still accepts previously-valid evidence for $reason_code",
    (vectorCase) => {
      const validOutcome = deepMergeRecord(
        terminalVectorSet.base,
        vectorCase.valid_overrides,
      );

      expect(textToSqlHistoryTerminalState({ terminal_outcome: validOutcome }).status)
        .not.toBe("invalid_terminal");
    },
  );
});


describe("textToSqlHistoryTerminalState", () => {
  const expectInvalidTerminal = (terminal_outcome: unknown) => {
    expect(textToSqlHistoryTerminalState({ terminal_outcome })).toMatchObject({
      status: "invalid_terminal",
      success: false,
      executed: false,
      dryRun: false,
    });
  };

  it.each(["abstained", "failed", "cancelled", "timed_out"])(
    "does not mark %s as successful",
    (status) => {
      expect(textToSqlHistoryTerminalState({
        status: "completed",
        result: "non-empty output",
        terminal_outcome: terminalOutcome(status),
      })).toMatchObject({ status, success: false });
    },
  );

  it("keeps successful dry-run distinct from execution", () => {
    expect(textToSqlHistoryTerminalState({
      terminal_outcome: {
        ...terminalOutcome("succeeded"),
        executed: false,
        dry_run: true,
        data: [],
        columns: [],
        rows_affected: 0,
        execution: {
          success: true,
          data: [],
          columns: [],
          rows_affected: 0,
          execution_time_ms: 1,
          dry_run_only: true,
          skipped_execution: true,
          sql_query: "SELECT 1",
          applied_row_limit: 10,
        },
        persistence: { status: "not_attempted" },
      },
    })).toEqual({
      status: "succeeded",
      success: true,
      executed: false,
      dryRun: true,
      error: undefined,
    });
  });

  it("fails closed for output without a valid terminal outcome", () => {
    expect(textToSqlHistoryTerminalState({
      status: "completed",
      result: "unvalidated output",
    })).toMatchObject({
      status: "invalid_terminal",
      success: false,
      executed: false,
      dryRun: false,
    });
  });

  it.each([
    { status: "succeeded" },
    { ...terminalOutcome("succeeded"), approved: "true" },
  ])("fails closed for partial or malformed terminal outcome", (terminal_outcome) => {
    expectInvalidTerminal(terminal_outcome);
  });

  it.each(["reason", " "])(
    "accepts succeeded with a non-empty reason (reason_code is no longer status-coupled) %j",
    (reason_code) => {
      expect(isTextToSqlTerminalOutcome({ ...terminalOutcome("succeeded"), reason_code }))
        .toBe(true);
    },
  );

  it.each([
    { executed: false, dry_run: false },
    { executed: true, dry_run: true },
    { generated: false },
    { approved: false },
    { audited: false },
  ])("accepts succeeded evidence that used to be incoherent %#", (overrides) => {
    expect(isTextToSqlTerminalOutcome({ ...terminalOutcome("succeeded"), ...overrides }))
      .toBe(true);
  });

  it("accepts a failed execution that was successfully audited", () => {
    expect(textToSqlHistoryTerminalState({
      terminal_outcome: {
        ...terminalOutcome("failed"),
        reason_code: "EXECUTION_FAILED",
        generated: true,
        approved: true,
        executed: true,
        execution: {
          success: false,
          data: [],
          columns: [],
          rows_affected: 0,
          execution_time_ms: 1,
          dry_run_only: false,
          skipped_execution: false,
          error_message: "database unavailable",
          sql_query: "SELECT 1",
          applied_row_limit: 10,
        },
        audit: { status: "logged", log_id: "audit-1" },
        audited: true,
      },
    })).toMatchObject({
      status: "failed",
      success: false,
      executed: true,
      dryRun: false,
      error: "failed",
    });
  });

  it("accepts an audit failure after successful execution", () => {
    expect(textToSqlHistoryTerminalState({
      terminal_outcome: {
        ...terminalOutcome("failed"),
        reason_code: "AUDIT_FAILED",
        generated: true,
        approved: true,
        executed: true,
        data: [[1]],
        columns: ["value"],
        rows_affected: 1,
        execution: {
          success: true,
          data: [[1]],
          columns: ["value"],
          rows_affected: 1,
          execution_time_ms: 1,
          dry_run_only: false,
          skipped_execution: false,
          sql_query: "SELECT 1",
          applied_row_limit: 10,
        },
        audit: { status: "error", error: "audit unavailable" },
      },
    })).toMatchObject({
      status: "failed",
      success: false,
      executed: true,
      dryRun: false,
      error: "failed",
    });
  });

  it("accepts untrusted raw evidence for an executor contract failure", () => {
    expect(textToSqlHistoryTerminalState({
      terminal_outcome: {
        ...terminalOutcome("failed"),
        reason_code: "EXECUTOR_CONTRACT_INVALID",
        generated: true,
        approved: true,
        audited: true,
        execution: {
          success: "yes",
          data: "raw",
          columns: null,
          rows_affected: "one",
          dry_run_only: "banana",
          skipped_execution: null,
          error_message: { raw: true },
        },
        audit: { status: "logged", log_id: "audit-1" },
      },
    })).toMatchObject({
      status: "failed",
      success: false,
      executed: false,
      dryRun: false,
    });

    expect(isTextToSqlTerminalOutcome({
      ...terminalOutcome("failed"),
      reason_code: "EXECUTOR_CONTRACT_INVALID",
      generated: true,
      approved: true,
      executed: true,
      data: [[1]],
      columns: ["value"],
      rows_affected: 1,
      execution: { raw_success: "yes" },
    })).toBe(true);
  });

  it.each([
    ["non-empty", "execution failed"],
    ["whitespace", "   "],
    ["non-string", 7],
    ["over-bound", "x".repeat(4097)],
  ])(
    "accepts successful execution with a previously-forbidden %s error_message",
    (_case, error_message) => {
      expect(isTextToSqlTerminalOutcome({
        ...terminalOutcome("succeeded"),
        execution: {
          ...terminalOutcome("succeeded").execution,
          error_message,
        },
      })).toBe(true);
    },
  );

  it.each([null, ""])(
    "accepts successful execution with an empty error_message %#",
    (error_message) => {
      expect(textToSqlHistoryTerminalState({
        terminal_outcome: {
          ...terminalOutcome("succeeded"),
          execution: {
            ...terminalOutcome("succeeded").execution,
            error_message,
          },
        },
      })).toMatchObject({ status: "succeeded", success: true });
    },
  );

  it.each([
    "dry_run_only",
    "skipped_execution",
    "sql_query",
    "applied_row_limit",
  ])(
    "accepts trusted execution missing a previously-required %s field",
    (field) => {
      expect(isTextToSqlTerminalOutcome({
        ...terminalOutcome("succeeded"),
        execution: successfulExecutionWithout(field),
      })).toBe(true);
    },
  );

  it.each([
    ["logged missing log_id", { status: "logged" }],
    ["logged empty log_id", { status: "logged", log_id: "" }],
    ["logged non-string log_id", { status: "logged", log_id: 7 }],
    ["logged unknown field", { status: "logged", log_id: "audit-1", extra: true }],
    ["logged error field", { status: "logged", log_id: "audit-1", error: null }],
  ])("accepts previously-malformed closed audit schema: %s", (_case, audit) => {
    expect(isTextToSqlTerminalOutcome(strictSuccessfulOutcome({ audit }))).toBe(true);
  });

  it("accepts unknown fields in audit error schema", () => {
    expect(isTextToSqlTerminalOutcome({
      ...auditFailureOutcome({
        status: "error",
        error: "audit unavailable",
        extra: true,
      }),
      reason_code: "AUDIT_FAILED",
    })).toBe(true);
  });

  it.each([
    ["unknown status", { status: "banana" }],
    ["not_attempted unknown field", { status: "not_attempted", extra: true }],
    ["saved missing filename", { status: "saved", path: "/tmp/query.md" }],
    ["saved missing path", { status: "saved", filename: "query.md" }],
    ["saved empty filename", { status: "saved", filename: "", path: "/tmp/query.md" }],
    ["saved empty path", { status: "saved", filename: "query.md", path: "" }],
    [
      "saved unknown field",
      { status: "saved", filename: "query.md", path: "/tmp/query.md", extra: true },
    ],
    ["error missing error", { status: "error" }],
    ["error empty error", { status: "error", error: "" }],
    ["error whitespace error", { status: "error", error: "   " }],
    ["error non-string error", { status: "error", error: 7 }],
    ["error over-bound error", { status: "error", error: "x".repeat(4097) }],
    ["error unknown field", { status: "error", error: "disk full", extra: true }],
  ])("accepts previously-malformed closed persistence schema: %s", (_case, persistence) => {
    expect(isTextToSqlTerminalOutcome(strictSuccessfulOutcome({ persistence }))).toBe(true);
  });

  it("still rejects a persistence path that is not finite JSON (NaN) via isJsonValue", () => {
    expectInvalidTerminal(strictSuccessfulOutcome({
      persistence: { status: "saved", filename: "query.md", path: NaN },
    }));
  });

  it("accepts exact persistence error evidence without hiding runtime success", () => {
    expect(textToSqlHistoryTerminalState({
      terminal_outcome: strictSuccessfulOutcome({
        persistence: { status: "error", error: "storage unavailable" },
      }),
    })).toMatchObject({ status: "succeeded", success: true, executed: true });
  });

  it.each([
    [
      "saved",
      { status: "saved", filename: "query.md", path: "/tmp/query.md" },
    ],
    ["error", { status: "error", error: "storage unavailable" }],
  ])(
    "accepts a successful dry-run with a previously-contradictory persistence %s",
    (_case, persistence) => {
      expect(isTextToSqlTerminalOutcome({
        ...strictSuccessfulOutcome(),
        executed: false,
        dry_run: true,
        data: [],
        columns: [],
        rows_affected: 0,
        execution: {
          success: true,
          data: [],
          columns: [],
          rows_affected: 0,
          execution_time_ms: 1,
          dry_run_only: true,
          skipped_execution: true,
          sql_query: "SELECT 1",
          applied_row_limit: 10,
        },
        persistence,
      })).toBe(true);
    },
  );

  it("accepts a successful real execution with a previously-required persistence attempt missing", () => {
    expect(isTextToSqlTerminalOutcome(strictSuccessfulOutcome({
      persistence: { status: "not_attempted" },
    }))).toBe(true);
  });

  it.each(["abstained", "cancelled", "timed_out"])(
    "no longer requires persistence=not_attempted for %s (soft evidence coupling)",
    (status) => {
      expect(textToSqlHistoryTerminalState({
        terminal_outcome: terminalOutcome(status),
      })).toMatchObject({ status, success: false });

      expect(isTextToSqlTerminalOutcome({
        ...terminalOutcome(status),
        persistence: { status: "error", error: "storage unavailable" },
      })).toBe(true);
      expect(isTextToSqlTerminalOutcome({
        ...terminalOutcome(status),
        persistence: {
          status: "saved",
          filename: "query.md",
          path: "/tmp/query.md",
        },
      })).toBe(true);
    },
  );

  it("accepts saved persistence without real execution", () => {
    expect(isTextToSqlTerminalOutcome({
      ...terminalOutcome("failed"),
      persistence: {
        status: "saved",
        filename: "query.md",
        path: "/tmp/query.md",
      },
    })).toBe(true);
  });

  it("accepts failed aggregation after a persisted real execution", () => {
    expect(textToSqlHistoryTerminalState({
      terminal_outcome: {
        ...strictSuccessfulOutcome(),
        status: "failed",
        reason_code: "RESULT_AGGREGATION_FAILED",
        error: "result aggregation failed",
      },
    })).toMatchObject({ status: "failed", success: false, executed: true });
  });

  it.each([
    ["not_attempted", "AUDIT_FAILED", { status: "not_attempted" }],
    [
      "error",
      "RESULT_PERSISTENCE_FAILED",
      { status: "error", error: "result persistence failed" },
    ],
  ])("accepts failed dry-run with persistence %s", (_case, reason_code, persistence) => {
    expect(textToSqlHistoryTerminalState({
      terminal_outcome: {
        ...terminalOutcome("failed"),
        reason_code,
        generated: true,
        approved: true,
        dry_run: true,
        execution: {
          success: true,
          data: [],
          columns: [],
          rows_affected: 0,
          execution_time_ms: 1,
          dry_run_only: true,
          skipped_execution: true,
          sql_query: "SELECT 1",
          applied_row_limit: 10,
        },
        audit: { status: "error", error: "audit unavailable" },
        persistence,
      },
    })).toMatchObject({ status: "failed", success: false, dryRun: true });
  });

  it.each([
    [true, 1],
    [false, 0],
    [{ value: true }, { value: 1 }],
    [{ value: false }, { value: 0 }],
  ])(
    "no longer requires top-level and nested evidence JSON values to match: %# versus %#",
    (topValue, nestedValue) => {
      expect(isTextToSqlTerminalOutcome(strictSuccessfulOutcome({
        data: [[topValue]],
        execution: {
          ...terminalOutcome("succeeded").execution,
          data: [[nestedValue]],
        },
      }))).toBe(true);
    },
  );

  it.each([NaN, Infinity, -Infinity])(
    "rejects non-finite JSON evidence %#",
    (value) => {
      expectInvalidTerminal(strictSuccessfulOutcome({
        data: [[value]],
        execution: {
          ...terminalOutcome("succeeded").execution,
          data: [[value]],
        },
      }));
    },
  );

  it.each([
    ["unknown failed reason", "failed", "BANANA"],
    ["failed with cancellation reason", "failed", "CANCELLED"],
    ["abstained with failure reason", "abstained", "DB_AUDIT_MISSING"],
    ["cancelled with execution reason", "cancelled", "EXECUTION_FAILED"],
    ["timed_out with cancellation reason", "timed_out", "CANCELLED"],
  ])("accepts a previously-mismatched %s (reason_code is no longer status-coupled)", (_case, status, reason_code) => {
    expect(isTextToSqlTerminalOutcome({ ...terminalOutcome(status), reason_code })).toBe(true);
  });

  it("recognizes audit and persistence contract failure reasons", () => {
    expect(textToSqlHistoryTerminalState({
      terminal_outcome: {
        ...auditFailureOutcome({
          status: "error",
          error: "invalid audit response",
        }),
        reason_code: "AUDIT_CONTRACT_INVALID",
      },
    })).toMatchObject({ status: "failed", success: false });

    expect(textToSqlHistoryTerminalState({
      terminal_outcome: {
        ...strictSuccessfulOutcome(),
        status: "failed",
        reason_code: "PERSISTENCE_CONTRACT_INVALID",
        error: "invalid persistence response",
        persistence: {
          status: "error",
          error: "invalid persistence response",
        },
      },
    })).toMatchObject({ status: "failed", success: false });
  });

  it("recognizes result reconciliation failure", () => {
    expect(textToSqlHistoryTerminalState({
      terminal_outcome: {
        ...terminalOutcome("failed"),
        reason_code: "RESULT_RECONCILIATION_FAILED",
        error: "terminal result reconciliation failed",
        persistence: {
          status: "error",
          error: "terminal result reconciliation failed",
        },
      },
    })).toMatchObject({ status: "failed", success: false });
  });

  it("recognizes output retry chain failure", () => {
    expect(textToSqlHistoryTerminalState({
      terminal_outcome: {
        ...failedExecutionOutcome(),
        reason_code: "OUTPUT_RETRY_CHAIN_FAILED",
        error: "corrective retry chain failed",
      },
    })).toMatchObject({ status: "failed", success: false });
  });

  it.each([
    ["dry_run_only mismatch", { dry_run_only: true }],
    ["skipped_execution mismatch", { skipped_execution: true }],
    ["dry_run_only non-boolean", { dry_run_only: "false" }],
    ["skipped_execution non-boolean", { skipped_execution: 0 }],
  ])("accepts trusted execution with a previously-invalid %s", (_case, markerOverride) => {
    expect(isTextToSqlTerminalOutcome({
      ...terminalOutcome("succeeded"),
      execution: {
        ...terminalOutcome("succeeded").execution,
        ...markerOverride,
      },
    })).toBe(true);
  });

  it.each([
    ["missing", undefined],
    ["fractional", 1.5],
    ["negative", -1],
  ])("accepts trusted execution with a previously-invalid %s execution_time_ms", (_case, value) => {
    const execution: Record<string, unknown> = {
      ...terminalOutcome("succeeded").execution,
      execution_time_ms: value,
    };
    if (value === undefined) delete execution.execution_time_ms;
    expect(isTextToSqlTerminalOutcome({
      ...terminalOutcome("succeeded"),
      execution,
    })).toBe(true);
  });

  it("still rejects a non-finite execution_time_ms (isJsonValue guards non-finite numbers)", () => {
    const execution: Record<string, unknown> = {
      ...terminalOutcome("succeeded").execution,
      execution_time_ms: NaN,
    };
    expectInvalidTerminal({
      ...terminalOutcome("succeeded"),
      execution,
    });
  });

  it("still rejects an execution block with an explicit undefined error_message (isJsonValue rejects undefined)", () => {
    expectInvalidTerminal(failedExecutionOutcome({ error_message: undefined }, {}));
  });

  it.each([
    ["null", { error_message: null }, {}],
    ["empty", { error_message: "" }, {}],
    ["whitespace", { error_message: "   " }, {}],
    ["non-string", { error_message: 7 }, {}],
    ["over-bound", { error_message: "x".repeat(4097) }, {}],
    ["row data", { data: [[1]] }, { data: [[1]] }],
    ["columns", { columns: ["value"] }, { columns: ["value"] }],
    ["rows affected", { rows_affected: 1 }, { rows_affected: 1 }],
  ])(
    "accepts failed execution with previously-malformed %s evidence",
    (_case, executionOverrides, outcomeOverrides) => {
      expect(isTextToSqlTerminalOutcome(failedExecutionOutcome(
        executionOverrides,
        outcomeOverrides,
      ))).toBe(true);
    },
  );

  it("accepts bounded failed execution error_message", () => {
    expect(textToSqlHistoryTerminalState({
      terminal_outcome: failedExecutionOutcome({
        error_message: "x".repeat(4096),
      }),
    })).toMatchObject({ status: "failed", success: false });
  });

  it.each([
    ["empty status", { status: "", error: "audit unavailable" }],
    ["banana status", { status: "banana", error: "audit unavailable" }],
    ["missing error", { status: "error" }],
    ["null error", { status: "error", error: null }],
    ["empty error", { status: "error", error: "" }],
    ["whitespace error", { status: "error", error: "   " }],
    ["non-string error", { status: "error", error: 7 }],
    ["over-bound error", { status: "error", error: "x".repeat(4097) }],
  ])("accepts previously-malformed audit %s", (_case, audit) => {
    expect(isTextToSqlTerminalOutcome(auditFailureOutcome(audit))).toBe(true);
  });

  it("accepts bounded audit failure error", () => {
    expect(textToSqlHistoryTerminalState({
      terminal_outcome: auditFailureOutcome({
        status: "error",
        error: "x".repeat(4096),
      }),
    })).toMatchObject({ status: "failed", success: false });
  });

  it.each([null, ""])(
    "accepts logged audit with a previously-forbidden error field %#",
    (error) => {
      expect(isTextToSqlTerminalOutcome({
        ...strictSuccessfulOutcome(),
        audit: { status: "logged", log_id: "audit-1", error },
      })).toBe(true);
    },
  );

  it.each([
    {
      execution: {
        ...terminalOutcome("succeeded").execution,
        success: false,
      },
    },
    { audit: { status: "error", error: "audit unavailable" } },
    { audit: { status: "logged", error: "contradictory audit error" } },
    {
      execution: {
        ...terminalOutcome("succeeded").execution,
        data: [[2]],
      },
    },
    {
      execution: {
        ...terminalOutcome("succeeded").execution,
        columns: ["other"],
      },
    },
    {
      execution: {
        ...terminalOutcome("succeeded").execution,
        rows_affected: 2,
      },
    },
    {
      execution: {
        ...terminalOutcome("succeeded").execution,
        dry_run_only: true,
      },
    },
  ])("accepts succeeded with previously-contradictory nested evidence %#", (overrides) => {
    expect(isTextToSqlTerminalOutcome({ ...terminalOutcome("succeeded"), ...overrides })).toBe(true);
  });

  it.each([
    {
      executed: true,
      execution: {
        success: false,
        data: [],
        columns: [],
        rows_affected: 0,
        execution_time_ms: 1,
        dry_run_only: false,
        skipped_execution: false,
        error_message: "database unavailable",
      },
    },
    {
      executed: false,
      execution: {
        success: true,
        data: [],
        columns: [],
        rows_affected: 0,
        execution_time_ms: 1,
        dry_run_only: false,
        skipped_execution: false,
      },
    },
    {
      audited: true,
      audit: { status: "error", error: "audit unavailable" },
    },
    {
      audited: false,
      audit: { status: "logged" },
    },
    {
      audited: true,
      audit: { status: "logged", error: "contradictory audit error" },
    },
  ])("accepts failed with previously-contradictory nested evidence %#", (overrides) => {
    expect(isTextToSqlTerminalOutcome({
      ...terminalOutcome("failed"),
      generated: true,
      approved: true,
      ...overrides,
    })).toBe(true);
  });

  it.each(["abstained", "cancelled", "timed_out"])(
    "accepts previously-forbidden nested runtime evidence for %s",
    (status) => {
      expect(isTextToSqlTerminalOutcome({
        ...terminalOutcome(status),
        execution: {
          success: false,
          data: [],
          columns: [],
          rows_affected: 0,
          error_message: "database unavailable",
        },
        audit: { status: "error", error: "audit unavailable" },
      })).toBe(true);
    },
  );

  it("accepts failed with previously-contradictory execution modes", () => {
    expect(isTextToSqlTerminalOutcome({
      ...terminalOutcome("failed"),
      generated: true,
      approved: true,
      executed: true,
      dry_run: true,
      audited: true,
    })).toBe(true);
  });

  it("enforces the terminal error length boundary", () => {
    expect(textToSqlHistoryTerminalState({
      terminal_outcome: {
        ...terminalOutcome("failed"),
        error: "x".repeat(4096),
      },
    })).toMatchObject({ status: "failed", success: false });
    expectInvalidTerminal({
      ...terminalOutcome("failed"),
      error: "x".repeat(4097),
    });
    expect(textToSqlHistoryTerminalState({
      terminal_outcome: {
        ...terminalOutcome("failed"),
        error: "💥".repeat(4096),
      },
    })).toMatchObject({ status: "failed", success: false });
  });
});

describe("softened contract constants and validators (T10-frontend)", () => {
  describe("isTextToSqlTerminalOutcome", () => {
    it("accepts an unknown field", () => {
      expect(isTextToSqlTerminalOutcome({
        ...terminalOutcome("succeeded"),
        some_future_field: "not yet known to the frontend",
      })).toBe(true);
    });

    it("accepts an unknown reason_code, and flags it as unknown via isKnownTextToSqlReasonCode", () => {
      const outcome = { ...terminalOutcome("failed"), reason_code: "SOME_FUTURE_REASON" };
      expect(isTextToSqlTerminalOutcome(outcome)).toBe(true);
      expect(isKnownTextToSqlReasonCode(outcome.reason_code)).toBe(false);
      expect(isKnownTextToSqlReasonCode("EXECUTION_FAILED")).toBe(true);
    });

    it("rejects a terminal outcome missing a required field", () => {
      const withoutSql: Record<string, unknown> = { ...terminalOutcome("succeeded") };
      delete withoutSql.sql;
      expect(isTextToSqlTerminalOutcome(withoutSql)).toBe(false);
    });

    it("rejects an invalid status", () => {
      expect(isTextToSqlTerminalOutcome({
        ...terminalOutcome("succeeded"),
        status: "not_a_real_status",
      })).toBe(false);
    });

    it("rejects a cyclic reference inside a known JSON field (isJsonValue anti-cycle guard)", () => {
      const cyclicAudit: Record<string, unknown> = { status: "logged" };
      cyclicAudit.self = cyclicAudit;
      expect(isTextToSqlTerminalOutcome(strictSuccessfulOutcome({
        audit: cyclicAudit,
      }))).toBe(false);
    });
  });

  describe("isTextToSqlHistorySummary", () => {
    it("accepts an unknown field", () => {
      expect(isTextToSqlHistorySummary(boundedHistorySummary({
        some_future_field: "not yet known to the frontend",
      }))).toBe(true);
    });

    it("accepts an unknown field holding a non-JSON value (symmetric with isTextToSqlTerminalOutcome)", () => {
      expect(isTextToSqlHistorySummary(boundedHistorySummary({
        some_future_field: () => "not a JSON value",
      }))).toBe(true);
      expect(isTextToSqlHistorySummary(boundedHistorySummary({
        some_future_field: undefined,
      }))).toBe(true);
    });

    it("still rejects a known JSON field (execution) holding a non-JSON value", () => {
      expect(isTextToSqlHistorySummary(boundedHistorySummary({
        execution: {
          success: true,
          dry_run_only: false,
          skipped_execution: false,
          rows_affected: 2,
          execution_time_ms: 1,
          bad: () => "not a JSON value",
        },
      }))).toBe(false);
    });

    it("accepts an unknown reason_code, and flags it as unknown via isKnownTextToSqlReasonCode", () => {
      const summary = boundedHistorySummary({ reason_code: "SOME_FUTURE_REASON" });
      expect(isTextToSqlHistorySummary(summary)).toBe(true);
      expect(isKnownTextToSqlReasonCode(summary.reason_code)).toBe(false);
    });

    it("rejects a history summary missing a required field", () => {
      const withoutSqlQuery: Record<string, unknown> = { ...boundedHistorySummary() };
      delete withoutSqlQuery.sql_query;
      expect(isTextToSqlHistorySummary(withoutSqlQuery)).toBe(false);
    });

    it("rejects an invalid status", () => {
      expect(isTextToSqlHistorySummary(boundedHistorySummary({
        status: "not_a_real_status",
      }))).toBe(false);
    });
  });

  it("accepts all four schema-abstention reason codes for status=abstained (previously only VERIFIER_REJECTED)", () => {
    const abstainReasonCodes = [
      "VERIFIER_REJECTED",
      "SCHEMA_CLARIFICATION_REQUIRED",
      "SCHEMA_GROUNDING_FAILED",
      "SCHEMA_CONTEXT_BUDGET_EXCEEDED",
    ];
    for (const reason_code of abstainReasonCodes) {
      const outcome = { ...terminalOutcome("abstained"), reason_code };
      expect(isTextToSqlTerminalOutcome(outcome)).toBe(true);
      expect(textToSqlHistoryTerminalState({ terminal_outcome: outcome })).toMatchObject({
        status: "abstained",
        success: false,
      });
    }
  });
});

describe("Text-to-SQL status polling", () => {
  it.each([
    ["invalid_terminal", { status: "invalid_terminal" }],
    ["cancelled", { status: "cancelled", terminal_outcome: terminalOutcome("cancelled") }],
    ["timed_out", { status: "failed", terminal_outcome: terminalOutcome("timed_out") }],
  ])(
    "stops immediately when %s is observed",
    async (_terminalStatus, terminalResponse) => {
      const statuses = [{ status: "running" }, terminalResponse];
      let pollCount = 0;
      let scheduledTick: (() => Promise<void>) | undefined;
      let cancelCount = 0;

      const stop = startTextToSqlStatusPolling(
        async () => statuses[pollCount++],
        (tick) => {
          scheduledTick = tick;
          return () => {
            cancelCount += 1;
          };
        },
      );

      await scheduledTick?.();
      expect(pollCount).toBe(1);
      expect(cancelCount).toBe(0);

      await scheduledTick?.();
      expect(pollCount).toBe(2);
      expect(cancelCount).toBe(1);

      await scheduledTick?.();
      expect(pollCount).toBe(2);
      expect(cancelCount).toBe(1);

      stop();
      expect(cancelCount).toBe(1);
    },
  );
});

describe("Text-to-SQL cancellation", () => {
  it("dispatches cancellation for the authoritative run id and confirms status", async () => {
    const calls: Array<[string, Record<string, unknown>]> = [];
    const inFlight = { current: false };

    const outcome = await requestTextToSqlCancellation(
      async (action, payload) => {
        calls.push([action, payload]);
        return { cancelled: true };
      },
      async (runId) => ({ run_id: runId, status: "cancelled" }),
      "run-1",
      { status: "running" },
      inFlight,
    );

    expect(calls).toEqual([["workflows.cancel", { run_id: "run-1" }]]);
    expect(outcome).toMatchObject({
      kind: "cancelled",
      requested: true,
      terminalStatus: "cancelled",
    });
    expect(inFlight.current).toBe(false);
  });

  it("keeps an authoritative cancellation when an older poll returns running", async () => {
    const outcome = await requestTextToSqlCancellation(
      async () => ({ cancelled: true }),
      async () => ({ status: "running" }),
      "run-1",
      { status: "running" },
      { current: false },
    );

    expect(outcome).toMatchObject({
      kind: "cancelled",
      requested: true,
      terminalStatus: "cancelled",
      status: { status: "cancelled" },
    });
    expect(textToSqlRunCanCancel("run-1", outcome.status, false)).toBe(false);
  });

  it("keeps an authoritative cancellation when the follow-up status read fails", async () => {
    const outcome = await requestTextToSqlCancellation(
      async () => ({ cancelled: true }),
      async () => {
        throw new Error("status unavailable");
      },
      "run-1",
      { status: "running" },
      { current: false },
    );

    expect(outcome).toMatchObject({
      kind: "cancelled",
      terminalStatus: "cancelled",
      status: { status: "cancelled" },
    });
  });

  it("deduplicates cancellation while the first request is pending", async () => {
    let releaseCancel: ((value: { cancelled: boolean }) => void) | undefined;
    const cancelResponse = new Promise<{ cancelled: boolean }>((resolve) => {
      releaseCancel = resolve;
    });
    let calls = 0;
    const inFlight = { current: false };
    const runServiceAction = async () => {
      calls += 1;
      return await cancelResponse;
    };
    const loadStatus = async () => ({ status: "cancelled" });

    const first = requestTextToSqlCancellation(
      runServiceAction,
      loadStatus,
      "run-1",
      { status: "running" },
      inFlight,
    );
    const duplicate = await requestTextToSqlCancellation(
      runServiceAction,
      loadStatus,
      "run-1",
      { status: "running" },
      inFlight,
    );

    expect(duplicate.kind).toBe("skipped");
    expect(calls).toBe(1);
    releaseCancel?.({ cancelled: true });
    await expect(first).resolves.toMatchObject({ kind: "cancelled" });
  });

  it("does not claim cancellation when the service returns false and the run remains active", async () => {
    const outcome = await requestTextToSqlCancellation(
      async () => ({ cancelled: false }),
      async () => ({ status: "running" }),
      "run-1",
      { status: "running" },
      { current: false },
    );

    expect(outcome).toMatchObject({
      kind: "not_confirmed",
      requested: false,
      terminalStatus: null,
    });
  });

  it("reports the authoritative terminal state instead of a false cancellation", async () => {
    const outcome = await requestTextToSqlCancellation(
      async () => ({ cancelled: false }),
      async () => ({ run_id: "run-1", status: "completed" }),
      "run-1",
      { status: "running" },
      { current: false },
    );

    expect(outcome).toMatchObject({
      kind: "terminal",
      requested: false,
      terminalStatus: "completed",
    });
  });

  it("does not dispatch cancellation for an already terminal run", async () => {
    let cancelCalls = 0;
    let statusCalls = 0;

    const outcome = await requestTextToSqlCancellation(
      async () => {
        cancelCalls += 1;
        return { cancelled: true };
      },
      async () => {
        statusCalls += 1;
        return { status: "completed" };
      },
      "run-1",
      { run_id: "run-1", status: "completed" },
      { current: false },
    );

    expect(outcome).toMatchObject({
      kind: "skipped",
      terminalStatus: "completed",
    });
    expect(cancelCalls).toBe(0);
    expect(statusCalls).toBe(0);
  });

  it("waits for an old status poll and then performs one fresh post-cancel read", async () => {
    let releaseOld: ((value: { status: string }) => void) | undefined;
    const oldStatus = new Promise<{ status: string }>((resolve) => {
      releaseOld = resolve;
    });
    const inFlight = new Map<string, Promise<unknown>>();
    let loads = 0;
    const oldPoll = loadTextToSqlStatusSingleFlight(inFlight, "run-1", async () => {
      loads += 1;
      return await oldStatus;
    });
    const postCancel = loadTextToSqlStatusAfterCurrent(
      inFlight,
      "run-1",
      () => loadTextToSqlStatusSingleFlight(inFlight, "run-1", async () => {
        loads += 1;
        return { status: "cancelled" };
      }),
    );

    await Promise.resolve();
    expect(loads).toBe(1);
    releaseOld?.({ status: "running" });

    await expect(oldPoll).resolves.toEqual({ status: "running" });
    await expect(postCancel).resolves.toEqual({ status: "cancelled" });
    expect(loads).toBe(2);
    expect(inFlight.size).toBe(0);
  });

  it("propagates cancellation errors and releases the in-flight guard", async () => {
    const inFlight = { current: false };
    let statusCalls = 0;

    await expect(requestTextToSqlCancellation(
      async () => {
        throw new Error("cancel unavailable");
      },
      async () => {
        statusCalls += 1;
        return { status: "running" };
      },
      "run-1",
      { status: "running" },
      inFlight,
    )).rejects.toThrow("cancel unavailable");

    expect(statusCalls).toBe(0);
    expect(inFlight.current).toBe(false);
  });

  it.each([
    [null, null, false, false],
    ["", { status: "running" }, false, false],
    ["run-1", null, false, true],
    ["run-1", { run_id: "run-1", status: "pending" }, false, true],
    ["run-1", { run_id: "run-1", status: "queued" }, false, true],
    ["run-1", { status: "running" }, false, true],
    ["run-1", { status: "cancelled" }, false, false],
    ["run-1", { run_id: "run-1", status: "completed" }, false, false],
    ["run-1", { status: "failed" }, false, false],
    ["run-1", { status: "running" }, true, false],
  ])(
    "allows cancellation only for an active run %#",
    (runId, status, inFlight, expected) => {
      expect(textToSqlRunCanCancel(runId, status, inFlight)).toBe(expected);
    },
  );
});

describe("Text-to-SQL active run correlation", () => {
  function deferred<T>() {
    let resolve: ((value: T) => void) | undefined;
    let reject: ((error: Error) => void) | undefined;
    const promise = new Promise<T>((resolvePromise, rejectPromise) => {
      resolve = resolvePromise;
      reject = rejectPromise;
    });
    return {
      promise,
      resolve: (value: T) => resolve?.(value),
      reject: (error: Error) => reject?.(error),
    };
  }

  it("blocks stale status, artifacts, result, and logs after run-2 is selected", async () => {
    const selection = { generation: 0, runId: null as string | null };
    const run1Generation = beginTextToSqlRunSelection(selection);
    expect(selectTextToSqlRun(selection, run1Generation, "run-1")).toBe(true);
    const view: Record<string, unknown> = {
      status: null,
      artifacts: null,
      result: null,
      logs: null,
      error: null,
      notification: null,
    };
    const barriers = {
      status: deferred<unknown>(),
      artifacts: deferred<unknown>(),
      result: deferred<unknown>(),
      logs: deferred<unknown>(),
      error: deferred<unknown>(),
      notification: deferred<unknown>(),
    };
    const staleWrites = Object.entries(barriers).map(async ([field, barrier]) => {
      const value = await barrier.promise;
      commitTextToSqlRunSelection(selection, "run-1", () => {
        view[field] = value;
      });
    });

    const run2Generation = beginTextToSqlRunSelection(selection);
    expect(selectTextToSqlRun(selection, run2Generation, "run-2")).toBe(true);
    for (const field of Object.keys(view)) view[field] = null;
    barriers.status.resolve({ status: "completed" });
    barriers.artifacts.resolve({ step_outputs: { old: true } });
    barriers.result.resolve({ terminal_outcome: terminalOutcome("succeeded") });
    barriers.logs.resolve([{ message: "old" }]);
    barriers.error.resolve("old error");
    barriers.notification.resolve("old notification");
    await Promise.all(staleWrites);

    expect(view).toEqual({
      status: null,
      artifacts: null,
      result: null,
      logs: null,
      error: null,
      notification: null,
    });
    for (const [field, value] of Object.entries({
      status: { status: "running" },
      artifacts: { step_outputs: { current: true } },
      result: { result: "current" },
      logs: [{ message: "current" }],
      error: null,
      notification: "current notification",
    })) {
      expect(commitTextToSqlRunSelection(selection, "run-2", () => {
        view[field] = value;
      })).toBe(true);
    }
    expect(view.status).toEqual({ status: "running" });
    expect(view.logs).toEqual([{ message: "current" }]);
  });

  it("invalidates run-1 before a pending start and does not restore it on failure", () => {
    const selection = { generation: 0, runId: null as string | null };
    const run1Generation = beginTextToSqlRunSelection(selection);
    selectTextToSqlRun(selection, run1Generation, "run-1");

    const failedGeneration = beginTextToSqlRunSelection(selection);
    expect(selection.runId).toBeNull();
    expect(commitTextToSqlRunSelection(selection, "run-1", () => {
      throw new Error("stale write must not execute");
    })).toBe(false);
    expect(selectTextToSqlRun(selection, run1Generation, "run-1")).toBe(false);
    expect(selection).toEqual({ generation: failedGeneration, runId: null });
  });

  it("blocks a deferred status continuation after unmount invalidation", async () => {
    const selection = { generation: 0, runId: null as string | null };
    const generation = beginTextToSqlRunSelection(selection);
    selectTextToSqlRun(selection, generation, "run-1");
    const response = deferred<unknown>();
    let status: unknown = null;
    let downstreamLoads = 0;
    const continuation = response.promise.then((value) => {
      const committed = commitTextToSqlRunSelection(
        selection,
        "run-1",
        () => { status = value; },
      );
      if (committed) downstreamLoads += 1;
      return committed;
    });

    const invalidatedGeneration = invalidateTextToSqlRunSelection(selection);
    response.resolve({ run_id: "run-1", status: "completed" });

    await expect(continuation).resolves.toBe(false);
    expect(selection).toEqual({ generation: invalidatedGeneration, runId: null });
    expect(status).toBeNull();
    expect(downstreamLoads).toBe(0);
  });

  it("joins concurrent terminal result reads and caches only the confirmed result", async () => {
    const loads = createTextToSqlResultLoadState();
    const response = deferred<unknown>();
    let fetches = 0;
    const load = () => {
      fetches += 1;
      return response.promise;
    };
    const first = loadTextToSqlResultSingleFlight(loads, "run-1", load);
    const concurrent = loadTextToSqlResultSingleFlight(loads, "run-1", load);
    expect(concurrent).toBe(first);
    response.resolve({ terminal_outcome: terminalOutcome("succeeded") });

    const [firstResult, concurrentResult] = await Promise.all([first, concurrent]);
    expect(concurrentResult).toBe(firstResult);
    expect(fetches).toBe(1);
    await expect(loadTextToSqlResultSingleFlight(
      loads,
      "run-1",
      async () => {
        fetches += 1;
        throw new Error("must not replace confirmed result");
      },
    )).resolves.toBe(firstResult);
    expect(fetches).toBe(1);
  });

  it("rejects a schema-valid terminal result for another run and retries", async () => {
    const loads = createTextToSqlResultLoadState();
    let fetches = 0;
    let artifacts = 0;
    let handled = 0;
    const mismatched = {
      terminal_outcome: {
        ...terminalOutcome("succeeded"),
        run_id: "run-2",
      },
    };
    const result = await loadTextToSqlResultThenArtifacts(
      () => loadTextToSqlResultSingleFlight(loads, "run-1", async () => {
        fetches += 1;
        return mismatched;
      }),
      async () => { artifacts += 1; },
      async () => { handled += 1; },
    );

    expect(result).toBeNull();
    expect(loads.confirmed.has("run-1")).toBe(false);
    expect(handled).toBe(0);
    expect(artifacts).toBe(0);

    const exact = { terminal_outcome: terminalOutcome("succeeded") };
    await expect(loadTextToSqlResultSingleFlight(
      loads,
      "run-1",
      async () => {
        fetches += 1;
        return exact;
      },
    )).resolves.toBe(exact);
    expect(fetches).toBe(2);
  });

  it("rejects status responses correlated to another run", () => {
    const matching = {
      run_id: "run-1",
      status: "completed",
      terminal_outcome: terminalOutcome("succeeded"),
    };
    expect(textToSqlStatusForRun({ status: matching }, "run-1")).toBe(matching);
    expect(textToSqlStatusForRun({
      run_id: "run-2",
      status: matching,
    }, "run-1")).toBeNull();
    expect(textToSqlStatusForRun({
      status: { ...matching, run_id: "run-2" },
    }, "run-1")).toBeNull();
    expect(textToSqlStatusForRun({
      status: {
        ...matching,
        terminal_outcome: {
          ...terminalOutcome("succeeded"),
          run_id: "run-2",
        },
      },
    }, "run-1")).toBeNull();
  });

  it("retries result reads after a failure and an early null response", async () => {
    const loads = createTextToSqlResultLoadState();
    let fetches = 0;
    await expect(loadTextToSqlResultSingleFlight(
      loads,
      "run-1",
      async () => {
        fetches += 1;
        throw new Error("result unavailable");
      },
    )).rejects.toThrow("result unavailable");
    await expect(loadTextToSqlResultSingleFlight(
      loads,
      "run-1",
      async () => {
        fetches += 1;
        return { result: null, status: "running" };
      },
    )).resolves.toEqual({ result: null, status: "running" });
    const terminal = { terminal_outcome: terminalOutcome("succeeded") };
    await expect(loadTextToSqlResultSingleFlight(
      loads,
      "run-1",
      async () => {
        fetches += 1;
        return terminal;
      },
    )).resolves.toBe(terminal);
    expect(fetches).toBe(3);
  });

  it("keeps stale terminal bookkeeping on run-1 and never poisons run-2", async () => {
    const selection = { generation: 0, runId: null as string | null };
    const run1Generation = beginTextToSqlRunSelection(selection);
    selectTextToSqlRun(selection, run1Generation, "run-1");
    const loads = createTextToSqlResultLoadState();
    const staleTerminal = deferred<unknown>();
    const staleWork = staleTerminal.promise.then(async () => {
      const result = await loadTextToSqlResultSingleFlight(
        loads,
        "run-1",
        async () => ({ terminal_outcome: terminalOutcome("succeeded") }),
      );
      return commitTextToSqlRunSelection(selection, "run-1", () => result);
    });

    const run2Generation = beginTextToSqlRunSelection(selection);
    selectTextToSqlRun(selection, run2Generation, "run-2");
    staleTerminal.resolve({ status: "completed" });

    await expect(staleWork).resolves.toBe(false);
    expect(loads.confirmed.has("run-1")).toBe(true);
    expect(loads.confirmed.has("run-2")).toBe(false);
  });

  it("handles the terminal result before queued artifacts settle", async () => {
    const terminal = { terminal_outcome: terminalOutcome("succeeded") };
    const calls: string[] = [];
    const artifactBarrier = deferred<void>();
    const loaded = loadTextToSqlResultThenArtifacts(
      async () => {
        calls.push("result");
        return terminal;
      },
      async () => {
        calls.push("artifacts");
        await artifactBarrier.promise;
        return { step_outputs: {} };
      },
      async (result) => {
        expect(result).toBe(terminal);
        calls.push("history");
      },
    );

    await expect(loaded).resolves.toBe(terminal);
    expect(calls).toEqual(["result", "history", "artifacts"]);
    artifactBarrier.resolve();
  });

  it.each(["failure", "early-null"])(
    "retries one selected terminal result after %s",
    async (firstOutcome) => {
      const selection = { generation: 0, runId: null as string | null };
      const generation = beginTextToSqlRunSelection(selection);
      selectTextToSqlRun(selection, generation, "run-1");
      const loads = createTextToSqlResultLoadState();
      const retries = new Map<string, () => void>();
      let scheduled: (() => void) | undefined;
      let fetches = 0;
      let finishRetry: (() => void) | undefined;
      const retryFinished = new Promise<void>((resolve) => {
        finishRetry = resolve;
      });
      const load = async () => {
        fetches += 1;
        if (fetches === 1) {
          if (firstOutcome === "failure") throw new Error("result unavailable");
          return { result: null, status: "running" };
        }
        return { terminal_outcome: terminalOutcome("succeeded") };
      };
      const attempt = async (allowRetry: boolean) => {
        await loadTextToSqlResultSingleFlight(loads, "run-1", load)
          .catch(() => undefined);
        if (
          allowRetry
          && !loads.confirmed.has("run-1")
        ) {
          scheduleTextToSqlResultRetry(
            retries,
            "run-1",
            () => selection.runId === "run-1",
            async () => {
              await attempt(false);
              finishRetry?.();
            },
            (retry) => {
              scheduled = retry;
              return () => undefined;
            },
          );
        }
      };

      await attempt(true);
      expect(fetches).toBe(1);
      expect(retries.size).toBe(1);
      scheduled?.();
      await retryFinished;
      expect(fetches).toBe(2);
      expect(loads.confirmed.has("run-1")).toBe(true);
      expect(retries.size).toBe(0);
    },
  );

  it("cancels a scheduled result retry when its run is no longer selected", async () => {
    const retries = new Map<string, () => void>();
    let scheduled: (() => void) | undefined;
    let cancelled = 0;
    let selectedRunId: string | null = "run-1";
    let retryCalls = 0;
    scheduleTextToSqlResultRetry(
      retries,
      "run-1",
      () => selectedRunId === "run-1",
      async () => { retryCalls += 1; },
      (retry) => {
        scheduled = retry;
        return () => { cancelled += 1; };
      },
    );

    selectedRunId = "run-2";
    cancelTextToSqlResultRetries(retries);
    scheduled?.();
    await Promise.resolve();
    expect(cancelled).toBe(1);
    expect(retryCalls).toBe(0);
    expect(retries.size).toBe(0);
  });

  it("does not schedule or execute a retry for an unselected run", async () => {
    const retries = new Map<string, () => void>();
    let selected = false;
    let scheduled: (() => void) | undefined;
    let schedules = 0;
    let retryCalls = 0;
    const schedule = (retry: () => void) => {
      schedules += 1;
      scheduled = retry;
      return () => undefined;
    };

    scheduleTextToSqlResultRetry(
      retries,
      "run-1",
      () => selected,
      async () => { retryCalls += 1; },
      schedule,
    );
    expect(schedules).toBe(0);

    selected = true;
    scheduleTextToSqlResultRetry(
      retries,
      "run-1",
      () => selected,
      async () => { retryCalls += 1; },
      schedule,
    );
    selected = false;
    scheduled?.();
    await Promise.resolve();
    expect(schedules).toBe(1);
    expect(retryCalls).toBe(0);
    expect(retries.size).toBe(0);
  });

  it("does not schedule a retry after a confirmed terminal result", async () => {
    const loads = createTextToSqlResultLoadState();
    const retries = new Map<string, () => void>();
    await loadTextToSqlResultSingleFlight(
      loads,
      "run-1",
      async () => ({ terminal_outcome: terminalOutcome("succeeded") }),
    );
    let schedules = 0;
    if (!loads.confirmed.has("run-1")) {
      scheduleTextToSqlResultRetry(
        retries,
        "run-1",
        () => true,
        async () => undefined,
        () => {
          schedules += 1;
          return () => undefined;
        },
      );
    }
    expect(schedules).toBe(0);
    expect(retries.size).toBe(0);
  });
});
