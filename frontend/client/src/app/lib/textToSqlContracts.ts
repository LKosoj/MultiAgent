import { isTextToSqlConnectionReference, type TextToSqlTerminalOutcome } from "./textToSqlClient";

export type TextToSqlHistorySummary = {
  run_id: string;
  session_id: string;
  workflow_name: string;
  timestamp: string;
  status: TextToSqlTerminalOutcome["status"];
  success: boolean;
  reason_code: string;
  generated: boolean;
  approved: boolean;
  executed: boolean;
  dry_run: boolean;
  audited: boolean;
  sql_query: string;
  row_count: number;
  rows_affected: number;
  column_count: number;
  execution: Record<string, unknown>;
  audit: Record<string, unknown>;
  persistence: Record<string, unknown>;
  error: string | null;
  natural_query?: string;
  connection_ref?: string;
  dsn?: string;
  dialect?: string;
  max_rows?: number;
  flags?: Record<string, boolean | string>;
};

// Authoritative terminal statuses/reason codes for the Text-to-SQL contract.
// Mirrors workflow/text_to_sql_contract.py (TextToSqlTerminalStatus /
// TextToSqlTerminalReasonCode); kept in sync by the schema sync test.
export const TEXT_TO_SQL_TERMINAL_OUTCOME_STATUSES = [
  "succeeded",
  "abstained",
  "failed",
  "cancelled",
  "timed_out",
] as const;

export type TextToSqlTerminalOutcomeStatus =
  (typeof TEXT_TO_SQL_TERMINAL_OUTCOME_STATUSES)[number];

export const TEXT_TO_SQL_REASON_CODES = [
  "VERIFIER_CONTRACT_INVALID",
  "VERIFIER_REJECTED",
  "DETERMINISTIC_CHECK_REJECTED",
  "SCHEMA_CLARIFICATION_REQUIRED",
  "SCHEMA_GROUNDING_FAILED",
  "SCHEMA_CONTEXT_BUDGET_EXCEEDED",
  "RESEARCH_AMBIGUOUS",
  "RESEARCH_UNSUPPORTED",
  "RESEARCH_STAGNATED",
  "RESEARCH_BUDGET_EXHAUSTED",
  "RESEARCH_TOOL_FAILURE",
  "RESEARCH_PROTOCOL_FAILURE",
  "EXECUTION_UNKNOWN",
  "STALE_REQUIRED_EVIDENCE",
  "EXECUTOR_CONTRACT_INVALID",
  "AUDIT_CONTRACT_INVALID",
  "AUDIT_FAILED",
  "EXECUTION_FAILED",
  "PERSISTENCE_CONTRACT_INVALID",
  "DB_AUDIT_MISSING",
  "DB_AUDIT_FAILED",
  "DB_AUDIT_NOT_TERMINAL",
  "DB_AUDIT_OUTPUT_INVALID",
  "DB_AUDIT_RUN_ID_MISMATCH",
  "DB_AUDIT_SKIPPED_WITHOUT_ABSTENTION",
  "DB_AUDIT_SKIPPED_AFTER_APPROVAL",
  "MANDATORY_STEP_NOT_COMPLETED",
  "SQL_GENERATION_OUTPUT_MISMATCH",
  "SQL_VERIFICATION_OUTPUT_MISMATCH",
  "CANCELLED",
  "TIMED_OUT",
  "RESULT_AGGREGATION_FAILED",
  "RESULT_PERSISTENCE_FAILED",
  "RESULT_RECONCILIATION_FAILED",
  "OUTPUT_RETRY_CHAIN_FAILED",
] as const;

export type TextToSqlReasonCode = (typeof TEXT_TO_SQL_REASON_CODES)[number];

const textToSqlReasonCodeSet = new Set<string>(TEXT_TO_SQL_REASON_CODES);

/** Soft membership check: an unknown reason_code is not a validation failure. */
export function isKnownTextToSqlReasonCode(
  code: unknown,
): code is TextToSqlReasonCode {
  return typeof code === "string" && textToSqlReasonCodeSet.has(code);
}

export const terminalFields = [
  "run_id", "status", "reason_code", "sql", "generated", "approved",
  "executed", "dry_run", "audited", "data", "columns", "rows_affected",
  "error", "execution", "audit", "persistence",
] as const;

const maxTerminalErrorLength = 4096;

const historySummaryRequiredFields = [
  "run_id", "session_id", "workflow_name", "timestamp", "status", "success",
  "reason_code", "generated", "approved", "executed", "dry_run", "audited",
  "sql_query", "row_count", "rows_affected", "column_count", "execution",
  "audit", "persistence", "error",
] as const;
const historyExecutionRequiredFields = [
  "success", "dry_run_only", "skipped_execution", "rows_affected",
  "execution_time_ms",
] as const;
const historyExecutionFieldNames = new Set([
  ...historyExecutionRequiredFields,
  "error_message",
]);
const historyFlagTypes: Record<string, "boolean" | "string"> = {
  dry_run_only: "boolean",
  include_explanation: "boolean",
  safety_level: "string",
  validate_schema: "boolean",
};

export function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

export function hasOwnField(value: Record<string, unknown>, field: string): boolean {
  return Object.prototype.hasOwnProperty.call(value, field);
}

function isJsonValue(value: unknown, ancestors = new Set<object>()): boolean {
  if (value === null || typeof value === "string" || typeof value === "boolean") {
    return true;
  }
  if (typeof value === "number") return Number.isFinite(value);
  if (!Array.isArray(value) && !isRecord(value)) return false;
  if (ancestors.has(value)) return false;
  if (!Array.isArray(value)) {
    const prototype = Object.getPrototypeOf(value);
    if (prototype !== Object.prototype && prototype !== null) return false;
  }
  ancestors.add(value);
  const valid = Array.isArray(value)
    ? value.every((item) => isJsonValue(item, ancestors))
    : Object.values(value).every((item) => isJsonValue(item, ancestors));
  ancestors.delete(value);
  return valid;
}

function isNonNegativeInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value >= 0;
}

const textEncoder = new TextEncoder();

function isBoundedHistoryString(
  value: unknown,
  maxBytes: number,
  { nonEmpty = false }: { nonEmpty?: boolean } = {},
): value is string {
  return typeof value === "string"
    && (!nonEmpty || value.trim().length > 0)
    && textEncoder.encode(value).length <= maxBytes;
}

function isHistoryFlags(value: unknown): value is Record<string, boolean | string> {
  if (!isRecord(value)) return false;
  const entries = Object.entries(value);
  return entries.length > 0 && entries.every(([field, fieldValue]) => (
    hasOwnField(historyFlagTypes, field)
    && typeof fieldValue === historyFlagTypes[field]
  ));
}

export function isTextToSqlHistorySummary(
  value: unknown,
): value is TextToSqlHistorySummary {
  if (
    !isRecord(value)
    || !historySummaryRequiredFields.every((field) => hasOwnField(value, field))
  ) return false;
  if (
    !(TEXT_TO_SQL_TERMINAL_OUTCOME_STATUSES as readonly unknown[]).includes(value.status)
    || !isBoundedHistoryString(value.run_id, 512, { nonEmpty: true })
    || !isBoundedHistoryString(value.session_id, 512, { nonEmpty: true })
    || value.workflow_name !== "text_to_sql_pipeline"
    || !isBoundedHistoryString(value.timestamp, 128, { nonEmpty: true })
    || Number.isNaN(Date.parse(value.timestamp as string))
    || typeof value.success !== "boolean"
    || typeof value.reason_code !== "string"
    || typeof value.generated !== "boolean"
    || typeof value.approved !== "boolean"
    || typeof value.executed !== "boolean"
    || typeof value.dry_run !== "boolean"
    || typeof value.audited !== "boolean"
    || !isBoundedHistoryString(value.sql_query, 8192)
    || !isNonNegativeInteger(value.row_count)
    || !isNonNegativeInteger(value.rows_affected)
    || !isNonNegativeInteger(value.column_count)
    || !isRecord(value.execution)
    || !isRecord(value.audit)
    || !isRecord(value.persistence)
    || !isJsonValue(value.execution)
    || !isJsonValue(value.audit)
    || !isJsonValue(value.persistence)
    || (
      value.error !== null
      && !isBoundedHistoryString(value.error, maxTerminalErrorLength, { nonEmpty: true })
    )
  ) return false;
  for (const field of ["natural_query", "dsn"] as const) {
    if (
      hasOwnField(value, field)
      && !isBoundedHistoryString(value[field], field === "dsn" ? 2048 : 4096)
    ) return false;
  }
  if (
    hasOwnField(value, "connection_ref")
    && !isTextToSqlConnectionReference(value.connection_ref)
  ) return false;
  if (
    hasOwnField(value, "dialect")
    && !isBoundedHistoryString(value.dialect, 128, { nonEmpty: true })
  ) return false;
  if (hasOwnField(value, "max_rows") && !isNonNegativeInteger(value.max_rows)) {
    return false;
  }
  if (value.max_rows === 0) return false;
  if (hasOwnField(value, "flags") && !isHistoryFlags(value.flags)) return false;
  return true;
}

export function isTextToSqlTerminalOutcome(value: unknown): value is TextToSqlTerminalOutcome {
  if (!isRecord(value)) return false;
  if (!terminalFields.every((field) => hasOwnField(value, field))) return false;
  if (!(TEXT_TO_SQL_TERMINAL_OUTCOME_STATUSES as readonly unknown[]).includes(value.status)) {
    return false;
  }
  return (
    typeof value.run_id === "string" && value.run_id.trim().length > 0
    && typeof value.reason_code === "string"
    && typeof value.sql === "string"
    && typeof value.generated === "boolean"
    && typeof value.approved === "boolean"
    && typeof value.executed === "boolean"
    && typeof value.dry_run === "boolean"
    && typeof value.audited === "boolean"
    && Array.isArray(value.data)
    && Array.isArray(value.columns)
    && value.columns.every((column) => typeof column === "string")
    && typeof value.rows_affected === "number"
    && Number.isInteger(value.rows_affected)
    && value.rows_affected >= 0
    && (value.error === null || typeof value.error === "string")
    && !(typeof value.error === "string" && Array.from(value.error).length > maxTerminalErrorLength)
    && isRecord(value.execution)
    && isRecord(value.audit)
    && isRecord(value.persistence)
    && isJsonValue(value.data)
    && isJsonValue(value.execution)
    && isJsonValue(value.audit)
    && isJsonValue(value.persistence)
  );
}

export function textToSqlHistoryTerminalState(payload: unknown) {
  const record = payload && typeof payload === "object"
    ? payload as Record<string, unknown>
    : null;
  const terminal = record?.terminal_outcome;
  const outcome = isTextToSqlTerminalOutcome(terminal) ? terminal : null;
  const summary = outcome === null && isTextToSqlHistorySummary(payload)
    ? payload
    : null;
  const status = outcome?.status ?? summary?.status ?? "invalid_terminal";
  return {
    status,
    success: status === "succeeded",
    executed: (outcome ?? summary)?.executed === true,
    dryRun: (outcome ?? summary)?.dry_run === true,
    error: typeof outcome?.error === "string" && outcome.error
      ? outcome.error
      : outcome?.reason_code
        ? outcome.reason_code
        : typeof summary?.error === "string" && summary.error
          ? summary.error
          : summary?.reason_code || undefined,
  };
}

export function textToSqlHistoryRowCount(payload: unknown): number | null {
  const record = isRecord(payload) ? payload : null;
  const terminal = record?.terminal_outcome;
  if (isTextToSqlTerminalOutcome(terminal)) return terminal.data.length;
  return isTextToSqlHistorySummary(payload) ? payload.row_count : null;
}

function storedHistorySummary(value: unknown): TextToSqlHistorySummary | null {
  if (!isRecord(value) || !isTextToSqlTerminalOutcome(value.terminal_snapshot)) {
    return null;
  }
  if (
    typeof value.run_id !== "string"
    || value.terminal_snapshot.run_id !== value.run_id
    || !isNonNegativeInteger(value.created_at_ms)
    || typeof value.dialect !== "string"
    || typeof value.profile_name !== "string"
  ) return null;
  const terminal = value.terminal_snapshot;
  const execution: Record<string, unknown> = {};
  for (const field of historyExecutionFieldNames) {
    const fieldValue = terminal.execution[field];
    if (fieldValue !== undefined && fieldValue !== null) execution[field] = fieldValue;
  }
  const summary: TextToSqlHistorySummary = {
    run_id: terminal.run_id,
    session_id: terminal.run_id,
    workflow_name: "text_to_sql_pipeline",
    timestamp: new Date(value.created_at_ms).toISOString(),
    status: terminal.status,
    success: terminal.status === "succeeded",
    reason_code: terminal.reason_code,
    generated: terminal.generated,
    approved: terminal.approved,
    executed: terminal.executed,
    dry_run: terminal.dry_run,
    audited: terminal.audited,
    sql_query: terminal.sql,
    row_count: terminal.data.length,
    rows_affected: terminal.rows_affected,
    column_count: terminal.columns.length,
    execution,
    audit: terminal.audit,
    persistence: terminal.persistence,
    error: terminal.error,
    dialect: value.dialect,
  };
  return isTextToSqlHistorySummary(summary) ? summary : null;
}

export function textToSqlHistoryEntries(payload: unknown): TextToSqlHistorySummary[] {
  const entries = isRecord(payload) && Array.isArray(payload.entries)
    ? payload.entries
    : Array.isArray(payload)
      ? payload
      : [];
  return entries.flatMap((entry) => {
    if (isTextToSqlHistorySummary(entry)) return [entry];
    const projected = storedHistorySummary(entry);
    return projected ? [projected] : [];
  });
}

const textToSqlRunTerminalStatuses = new Set([
  "completed",
  "succeeded",
  "abstained",
  "failed",
  "cancelled",
  "timed_out",
  "invalid_terminal",
]);

export function textToSqlRunTerminalStatus(payload: unknown): string | null {
  if (typeof payload === "string") {
    return textToSqlRunTerminalStatuses.has(payload) ? payload : null;
  }
  if (!isRecord(payload)) return null;

  const terminal = payload.terminal_outcome;
  if (isTextToSqlTerminalOutcome(terminal)) return terminal.status;

  const status = payload.status ?? payload.state;
  return typeof status === "string" && textToSqlRunTerminalStatuses.has(status)
    ? status
    : null;
}
