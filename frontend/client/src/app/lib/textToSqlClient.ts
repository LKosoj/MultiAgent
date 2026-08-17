import type { TextToSqlStartResponse } from "../utils/textToSqlStart";

export type ServiceActionTransport = (
  action: string,
  payload: Record<string, unknown>,
) => Promise<unknown>;

export type TextToSqlStartTransport = (
  payload: Record<string, unknown>,
) => Promise<TextToSqlStartResponse>;

export type TextToSqlConnection = {
  connection_ref: string;
  display_name: string;
  owner_subject?: string | null;
  tenant_id?: string;
  target_kind?: string;
  dialect?: string;
  target_description?: string;
  created_at?: string;
  enabled_for_user?: boolean;
};

export type TextToSqlStartPayloadInput = {
  query: string;
  connectionRef: string;
  maxRows: number;
  sessionId?: string;
  enableTelemetry: boolean;
  safetyLevel: string;
  includeExplanation: boolean;
  validateSchema: boolean;
  dryRunOnly: boolean;
};

export type TextToSqlTerminalOutcome = {
  run_id: string;
  status: "succeeded" | "abstained" | "failed" | "cancelled" | "timed_out";
  reason_code: string;
  sql: string;
  generated: boolean;
  approved: boolean;
  executed: boolean;
  dry_run: boolean;
  audited: boolean;
  data: unknown[];
  columns: string[];
  rows_affected: number;
  error: string | null;
  execution: Record<string, unknown>;
  audit: Record<string, unknown>;
  persistence: Record<string, unknown>;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

const connectionRefPattern = /^conn-[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export function isTextToSqlConnectionReference(value: unknown): value is string {
  return typeof value === "string" && connectionRefPattern.test(value);
}

export function textToSqlConnectionEntries(payload: unknown): TextToSqlConnection[] {
  const result = isRecord(payload) && isRecord(payload.result)
    ? payload.result
    : payload;
  const values = Array.isArray(result)
    ? result
    : isRecord(result) && Array.isArray(result.connections)
      ? result.connections
      : [];

  return values.flatMap((value): TextToSqlConnection[] => {
    if (
      !isRecord(value)
      || !isTextToSqlConnectionReference(value.connection_ref)
      || typeof value.display_name !== "string"
      || !value.display_name.trim()
      || value.enabled_for_user === false
    ) return [];
    return [{
      connection_ref: value.connection_ref,
      display_name: value.display_name,
      ...(typeof value.owner_subject === "string" || value.owner_subject === null
        ? { owner_subject: value.owner_subject }
        : {}),
      ...(typeof value.tenant_id === "string" ? { tenant_id: value.tenant_id } : {}),
      ...(typeof value.target_kind === "string" ? { target_kind: value.target_kind } : {}),
      ...(typeof value.dialect === "string" ? { dialect: value.dialect } : {}),
      ...(typeof value.target_description === "string"
        ? { target_description: value.target_description }
        : {}),
      ...(typeof value.created_at === "string" ? { created_at: value.created_at } : {}),
      ...(typeof value.enabled_for_user === "boolean"
        ? { enabled_for_user: value.enabled_for_user }
        : {}),
    }];
  });
}

export function buildTextToSqlStartPayload(
  input: TextToSqlStartPayloadInput,
): Record<string, unknown> {
  if (!isTextToSqlConnectionReference(input.connectionRef)) {
    throw new Error("Select an authorized database connection");
  }
  return {
    query: input.query,
    connection_ref: input.connectionRef,
    max_rows: input.maxRows,
    session_id: input.sessionId,
    enable_telemetry: input.enableTelemetry,
    safety_level: input.safetyLevel,
    include_explanation: input.includeExplanation,
    validate_schema: input.validateSchema,
    dry_run_only: input.dryRunOnly,
  };
}

export function buildTextToSqlSchemaPayload(
  connectionRef: string,
  schema: string,
  tableName: string,
  allowDbSchemaFallback: boolean,
): Record<string, unknown> {
  if (!isTextToSqlConnectionReference(connectionRef)) {
    throw new Error("Select an authorized database connection");
  }
  return {
    connection_ref: connectionRef,
    schema: schema || undefined,
    table_name: tableName || undefined,
    allow_db_schema_fallback: allowDbSchemaFallback,
  };
}

export function createTextToSqlClient(
  runServiceAction: ServiceActionTransport,
  startTextToSqlRun: TextToSqlStartTransport,
) {
  return {
    start: startTextToSqlRun,
    async listConnections(): Promise<TextToSqlConnection[]> {
      return textToSqlConnectionEntries(
        await runServiceAction("db.connections.list", {}),
      );
    },
    loadSchema(payload: Record<string, unknown>): Promise<unknown> {
      return runServiceAction("text_to_sql.schema.load", payload);
    },
    getStatus(runId: string): Promise<unknown> {
      return runServiceAction("workflows.status", { run_id: runId });
    },
    getResult(runId: string): Promise<unknown> {
      return runServiceAction("workflows.result", { run_id: runId });
    },
    getArtifacts(runId: string): Promise<unknown> {
      return runServiceAction("workflows.artifacts", { run_id: runId });
    },
    cancel(runId: string): Promise<unknown> {
      return runServiceAction("workflows.cancel", { run_id: runId });
    },
    listHistory(limit = 100, offset = 0): Promise<unknown> {
      return runServiceAction("text_to_sql.history.list", { limit, offset });
    },
    historyAnalytics(): Promise<unknown> {
      return runServiceAction("text_to_sql.history.analytics", {});
    },
    clearHistory(): Promise<unknown> {
      return runServiceAction("text_to_sql.history.clear", { confirm: true });
    },
  };
}
